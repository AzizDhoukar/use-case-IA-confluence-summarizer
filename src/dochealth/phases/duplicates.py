"""Phase 2: cluster pages on the same topic (embeddings), then let Claude find contradictions
and recommend a canonical page per cluster."""

from __future__ import annotations

import hashlib
import json
import logging

import numpy as np
from sklearn.cluster import AgglomerativeClustering

from ..config import Config
from ..db import Database, utcnow
from ..embeddings import Encoder, ensure_embeddings, page_vectors
from ..llm import schemas
from ..llm.claude_cli import BudgetExhausted, ClaudeCLI, LLMError, RateLimitReached
from ..llm.prompts import load_prompt
from ..text import truncate

log = logging.getLogger(__name__)


def eligible_pages(db: Database, cfg: Config) -> list:
    """In-scope pages with enough text to compare, minus excluded labels."""
    dcfg = cfg["duplicates"]
    exclude = {lab.lower() for lab in cfg["exclude_labels"]}
    rows = db.query("SELECT * FROM pages WHERE active = 1 AND space_key IN (%s) ORDER BY id"
                    % ",".join("?" * len(cfg.spaces)), cfg.spaces)
    return [r for r in rows
            if len(r["body_text"]) >= dcfg["min_chars"]
            and not exclude & {lab.lower() for lab in json.loads(r["labels_json"])}]


def prepare_vectors(cfg: Config, db: Database, encoder: Encoder, stats: dict | None = None
                    ) -> tuple[list[str], np.ndarray]:
    pages = eligible_pages(db, cfg)
    n = ensure_embeddings(db, encoder, pages, cfg["embeddings"]["chunk_tokens"])
    if stats is not None:
        stats["eligible_pages"] = len(pages)
        stats["pages_embedded"] = n
    return page_vectors(db, [p["id"] for p in pages], encoder.name)


def find_clusters(ids: list[str], vecs: np.ndarray, threshold: float, max_size: int
                  ) -> list[tuple[list[str], float]]:
    """Average-linkage clusters whose pages are >= threshold similar, split to max_size.

    Returns (page ids, min pairwise similarity) per cluster of 2+ pages.
    """
    if len(ids) < 2:
        return []
    sim = vecs @ vecs.T
    labels = AgglomerativeClustering(n_clusters=None, metric="precomputed", linkage="average",
                                     distance_threshold=1 - threshold
                                     ).fit_predict(np.clip(1 - sim, 0, 2))
    out = []
    for label in set(labels):
        members = [i for i, lab in enumerate(labels) if lab == label]
        for group in _split(members, sim, max_size):
            if len(group) >= 2:
                sub = sim[np.ix_(group, group)]
                out.append(([ids[i] for i in group], float(sub[np.triu_indices(len(group), 1)].min())))
    out.sort(key=lambda c: -c[1])
    return out


def _split(members: list[int], sim: np.ndarray, max_size: int) -> list[list[int]]:
    """Greedy split of a big cluster: seed = most central page + its nearest neighbours."""
    groups, rest = [], list(members)
    while len(rest) > max_size:
        sub = sim[np.ix_(rest, rest)]
        seed = int(np.argmax(sub.sum(axis=1)))
        nearest = list(np.argsort(-sub[seed])[:max_size])
        groups.append([rest[i] for i in nearest])
        rest = [m for i, m in enumerate(rest) if i not in nearest]
    groups.append(rest)
    return groups


def signature(db: Database, page_ids: list[str]) -> str:
    rows = db.query("SELECT id, content_hash FROM pages WHERE id IN (%s) ORDER BY id"
                    % ",".join("?" * len(page_ids)), page_ids)
    return hashlib.sha256(json.dumps([[r["id"], r["content_hash"]] for r in rows]).encode()
                          ).hexdigest()


def _pages_block(db: Database, page_ids: list[str], max_chars: int) -> str:
    parts = []
    for pid in page_ids:
        p = db.one("SELECT * FROM pages WHERE id = ?", (pid,))
        parts.append(
            f'<page id="{pid}">\nTitle: {p["title"]}\nSpace: {p["space_key"]}\n'
            f'Last modified: {(p["modified_at"] or "")[:10]} '
            f'by {db.user_name(p["last_modifier_id"]) or "unknown"}\n'
            f'Views (last year): {p["views_count"] if p["views_count"] is not None else "unknown"}\n'
            f'Content:\n{truncate(p["body_text"], max_chars)}\n</page>')
    return "\n\n".join(parts)


def _clean_result(result: dict, page_ids: list[str]) -> dict:
    """Drop references to pages that are not in the cluster."""
    valid = set(page_ids)
    result["conflicts"] = [c for c in result.get("conflicts", [])
                           if c["page_a"] in valid and c["page_b"] in valid]
    if result.get("canonical_page") not in valid:
        result["canonical_page"] = None
    return result


def run_duplicates(cfg: Config, db: Database, llm: ClaudeCLI, encoder: Encoder,
                   run_id: int) -> dict:
    dcfg = cfg["duplicates"]
    stats = {"eligible_pages": 0, "pages_embedded": 0, "clusters": 0, "analyzed": 0,
             "unchanged": 0, "cached": 0, "errors": 0, "pending": 0, "stopped": None}
    template = load_prompt("duplicates")
    model = cfg["llm"]["models"]["compare"]

    ids, vecs = prepare_vectors(cfg, db, encoder, stats)
    clusters = find_clusters(ids, vecs, dcfg["threshold"], dcfg["max_cluster_size"])
    stats["clusters"] = len(clusters)
    db.set_meta("duplicates_run_id", str(run_id))

    stopped = False
    for page_ids, similarity in clusters:
        sig = signature(db, page_ids)
        row = db.one("SELECT status, prompt_version FROM clusters WHERE signature = ?", (sig,))
        db.execute(
            "INSERT INTO clusters(signature, page_ids_json, similarity, status, last_seen_run) "
            "VALUES(?,?,?,'pending',?) ON CONFLICT(signature) DO UPDATE SET "
            "last_seen_run = excluded.last_seen_run, similarity = excluded.similarity",
            (sig, json.dumps(page_ids), similarity, run_id))
        db.commit()
        if row and row["status"] == "analyzed" and row["prompt_version"] == template.version:
            stats["unchanged"] += 1
            continue
        if stopped:
            continue

        hits_before = llm.stats.cache_hits
        try:
            result = llm.complete(template, model, schemas.DUPLICATES,
                                  pages=_pages_block(db, page_ids, dcfg["max_chars_per_page"]))
        except (BudgetExhausted, RateLimitReached) as exc:
            log.warning("Duplicates analysis stopped: %s", exc)
            stats["stopped"] = "rate_limit" if isinstance(exc, RateLimitReached) else "budget"
            stopped = True
            continue
        except LLMError as exc:
            log.error("Cluster %s failed: %s", page_ids, exc)
            stats["errors"] += 1
            db.execute("UPDATE clusters SET status = 'failed' WHERE signature = ?", (sig,))
            db.commit()
            continue
        stats["cached" if llm.stats.cache_hits > hits_before else "analyzed"] += 1
        db.execute("UPDATE clusters SET status = 'analyzed', prompt_version = ?, model = ?, "
                   "result_json = ?, analyzed_at = ? WHERE signature = ?",
                   (template.version, model, json.dumps(_clean_result(result, page_ids)),
                    utcnow(), sig))
        db.commit()

    stats["pending"] = db.one("SELECT COUNT(*) AS n FROM clusters WHERE last_seen_run = ? "
                              "AND status != 'analyzed'", (run_id,))["n"]
    log.info("Duplicates done: %s", stats)
    return stats


def tune_threshold(cfg: Config, db: Database, encoder: Encoder, samples: int = 3) -> str:
    """Text report: for several thresholds, how many pairs/clusters and sample pairs per band."""
    ids, vecs = prepare_vectors(cfg, db, encoder)
    if len(ids) < 2:
        return "Not enough pages with text to compare (run inventory first)."
    titles = {r["id"]: r["title"] for r in db.query("SELECT id, title FROM pages")}
    sim = vecs @ vecs.T
    iu = np.triu_indices(len(ids), 1)
    pair_sims = sim[iu]
    order = np.argsort(-pair_sims)
    thresholds = sorted(cfg["duplicates"]["tune_thresholds"], reverse=True)

    lines = [f"{len(ids)} pages, {len(pair_sims)} pairs, model {encoder.name}", ""]
    upper = 1.01
    for t in thresholds:
        n_pairs = int((pair_sims >= t).sum())
        n_clusters = len(find_clusters(ids, vecs, t, cfg["duplicates"]["max_cluster_size"]))
        lines.append(f"threshold {t:.2f}: {n_pairs} pairs >= t, {n_clusters} clusters")
        band = [k for k in order if t <= pair_sims[k] < upper][:samples]
        for k in band:
            a, b = ids[iu[0][k]], ids[iu[1][k]]
            lines.append(f"    {pair_sims[k]:.3f}  {titles.get(a, a)!r}  <->  {titles.get(b, b)!r}")
        if not band:
            lines.append("    (no pairs in this band)")
        upper = t
    lines += ["", f"Current setting: duplicates.threshold = {cfg['duplicates']['threshold']}"]
    return "\n".join(lines)
