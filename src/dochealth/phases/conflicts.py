"""Contradictions between sections of different pages.

The duplicates phase compares pages that are similar as a whole. This one compares passages
(chunks): two pages about different things overall can still describe the same rule, value or
procedure differently in one section. Close passage pairs (local embeddings) are sent to
Claude, one call per page pair, and only quoted contradictions are kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict

import numpy as np

from ..config import Config
from ..db import Database, utcnow
from ..embeddings import Encoder
from ..llm import schemas
from ..llm.claude_cli import BudgetExhausted, ClaudeCLI, LLMError, RateLimitReached
from ..llm.prompts import load_prompt
from ..text import truncate
from .changes import chunk_index, norm

log = logging.getLogger(__name__)

BLOCK = 1024   # rows per similarity block, so big spaces do not need an n x n matrix


def clustered_page_pairs(db: Database) -> set[tuple[str, str]]:
    """Page pairs already compared together by the duplicates phase in its latest run."""
    run_id = db.get_meta("duplicates_run_id")
    if run_id is None:
        return set()
    pairs = set()
    for r in db.query("SELECT page_ids_json FROM clusters WHERE last_seen_run = ? "
                      "AND status = 'analyzed'", (int(run_id),)):
        ids = sorted(json.loads(r["page_ids_json"]))
        pairs |= {(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]}
    return pairs


def passage_pairs(rows: list, matrix: np.ndarray, threshold: float, per_pair: int,
                  skip: set[tuple[str, str]] = frozenset()) -> list[tuple]:
    """Close passages of different pages, grouped by page pair, best page pairs first.

    Returns [((page_a, page_b), [(similarity, row_a, row_b), ...]), ...] with page_a < page_b.
    """
    groups: dict[tuple[str, str], list] = defaultdict(list)
    pages = [r["page_id"] for r in rows]
    for start in range(0, len(rows), BLOCK):
        sims = matrix[start:start + BLOCK] @ matrix.T
        for bi, j in zip(*np.nonzero(sims >= threshold)):
            i = start + int(bi)
            j = int(j)
            if j <= i or pages[i] == pages[j]:
                continue
            a, b = (i, j) if pages[i] < pages[j] else (j, i)
            key = (pages[a], pages[b])
            if key not in skip:
                groups[key].append((float(sims[bi, j]), rows[a], rows[b]))
    out = []
    for key, items in groups.items():
        items.sort(key=lambda t: -t[0])
        out.append((key, items[:per_pair]))
    out.sort(key=lambda g: -g[1][0][0])
    return out


def signature(items: list) -> str:
    parts = sorted([[ra["page_id"], ra["text"], rb["page_id"], rb["text"]] for _, ra, rb in items])
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def _passages_block(db: Database, key: tuple[str, str], items: list,
                    max_chars: int) -> str:
    def title(pid):
        p = db.one("SELECT title FROM pages WHERE id = ?", (pid,))
        return (p["title"] if p else pid).replace('"', "'")

    parts = [f'Page A: "{title(key[0])}"\nPage B: "{title(key[1])}"']
    for n, (sim, ra, rb) in enumerate(items, 1):
        for side, row in (("A", ra), ("B", rb)):
            section = (row["heading"] or "").replace('"', "'")
            parts.append(f'<passage id="{side}{n}" page="{side}" section="{section}">\n'
                         f'{truncate(row["text"], max_chars)}\n</passage>')
    return "\n\n".join(parts)


def _clean(result: dict, items: list) -> list[dict]:
    """Keep contradictions whose two statements are really in the passages."""
    a_text = norm(" ".join(ra["text"] for _, ra, _ in items))
    b_text = norm(" ".join(rb["text"] for _, _, rb in items))
    kept = []
    for c in result.get("conflicts", []):
        if norm(c["statement_a"]) in a_text and norm(c["statement_b"]) in b_text:
            kept.append(c)
        else:
            log.info("Dropped page conflict: statement not found in passages")
    return kept


def run_conflicts(cfg: Config, db: Database, llm: ClaudeCLI, encoder: Encoder,
                  run_id: int) -> dict:
    ccfg = cfg["conflicts"]
    stats = {"target_pages": 0, "pages_embedded": 0, "page_pairs": 0, "analyzed": 0,
             "cached": 0, "unchanged": 0, "conflicts": 0, "dropped": 0, "errors": 0,
             "pending": 0, "stopped": None}
    db.set_meta("conflicts_run_id", str(run_id))
    if not ccfg["enabled"]:
        return stats
    template = load_prompt("conflicts")
    model = cfg["llm"]["models"]["compare"]

    rows, matrix = chunk_index(cfg, db, encoder, stats)
    pairs = passage_pairs(rows, matrix, ccfg["passage_threshold"],
                          ccfg["max_passages_per_pair"], clustered_page_pairs(db))
    stats["page_pairs"] = len(pairs)

    calls = 0
    for key, items in pairs:
        sig = signature(items)
        row = db.one("SELECT status, prompt_version FROM passage_pairs WHERE signature = ?",
                     (sig,))
        db.execute(
            "INSERT INTO passage_pairs(signature, page_a, page_b, passages_json, similarity, "
            "status, last_seen_run) VALUES(?,?,?,?,?,'pending',?) ON CONFLICT(signature) DO "
            "UPDATE SET last_seen_run = excluded.last_seen_run",
            (sig, key[0], key[1],
             json.dumps([[ra["idx"], rb["idx"], round(s, 4)] for s, ra, rb in items]),
             items[0][0], run_id))
        db.commit()
        if row and row["status"] == "analyzed" and row["prompt_version"] == template.version:
            stats["unchanged"] += 1
            continue
        if stats["stopped"]:
            continue
        if calls >= ccfg["max_calls_per_run"]:
            stats["stopped"] = "budget"
            continue

        hits_before = llm.stats.cache_hits
        try:
            result = llm.complete(template, model, schemas.CONFLICTS,
                                  passages=_passages_block(db, key, items,
                                                           ccfg["max_chars_per_passage"]))
        except (BudgetExhausted, RateLimitReached) as exc:
            log.warning("Page conflict analysis stopped: %s", exc)
            stats["stopped"] = "rate_limit" if isinstance(exc, RateLimitReached) else "budget"
            continue
        except LLMError as exc:
            log.error("Page pair %s failed: %s", key, exc)
            stats["errors"] += 1
            db.execute("UPDATE passage_pairs SET status = 'failed' WHERE signature = ?", (sig,))
            db.commit()
            continue
        cached = llm.stats.cache_hits > hits_before
        calls += 0 if cached else 1
        stats["cached" if cached else "analyzed"] += 1
        conflicts = _clean(result, items)
        stats["conflicts"] += len(conflicts)
        stats["dropped"] += len(result.get("conflicts", [])) - len(conflicts)
        db.execute("UPDATE passage_pairs SET status = 'analyzed', prompt_version = ?, model = ?, "
                   "result_json = ?, analyzed_at = ? WHERE signature = ?",
                   (template.version, model, json.dumps({"conflicts": conflicts},
                                                        ensure_ascii=False), utcnow(), sig))
        db.commit()

    stats["pending"] = db.one("SELECT COUNT(*) AS n FROM passage_pairs WHERE last_seen_run = ? "
                              "AND status != 'analyzed'", (run_id,))["n"]
    log.info("Page conflicts done: %s", stats)
    return stats


def tune_passages(cfg: Config, db: Database, encoder: Encoder, samples: int = 3) -> str:
    """Text report: page pairs with close passages at several thresholds (no LLM)."""
    rows, matrix = chunk_index(cfg, db, encoder, {})
    if len(rows) < 2:
        return "Not enough passages to compare."
    titles = {r["id"]: r["title"] for r in db.query("SELECT id, title FROM pages")}
    lines = [f"Section-level conflicts: {len(rows)} passages", ""]
    upper = 1.01
    for t in sorted(cfg["conflicts"]["tune_thresholds"], reverse=True):
        pairs = passage_pairs(rows, matrix, t, 1)
        lines.append(f"threshold {t:.2f}: {len(pairs)} page pairs to check")
        band = [p for p in pairs if p[1][0][0] < upper][:samples]
        for (a, b), items in band:
            s, ra, rb = items[0]
            lines.append(f"    {s:.3f}  {titles.get(a, a)!r} / {ra['heading'] or '-'}  <->  "
                         f"{titles.get(b, b)!r} / {rb['heading'] or '-'}")
        if not band:
            lines.append("    (no pairs in this band)")
        upper = t
    lines += ["", f"Current setting: conflicts.passage_threshold = "
                  f"{cfg['conflicts']['passage_threshold']}"]
    return "\n".join(lines)
