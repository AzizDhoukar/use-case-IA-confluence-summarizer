"""Phase 1b: rule-based prefilter of stale/unread pages, then LLM classification."""

from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from ..atlassian.confluence import Confluence
from ..config import Config
from ..db import Database, utcnow
from ..llm import schemas
from ..llm.claude_cli import BudgetExhausted, ClaudeCLI, LLMError, RateLimitReached
from ..llm.prompts import load_prompt
from ..text import truncate

log = logging.getLogger(__name__)

RULE_VERSION = "rule"


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def analytics_available(db: Database) -> bool:
    return db.get_meta("analytics_available") == "yes"


def date_stale_pages(db: Database, cfg: Config) -> list:
    cutoff = _days_ago(cfg["triage"]["stale_modified_days"])
    rows = db.query("SELECT * FROM pages WHERE active = 1 AND space_key IN (%s) ORDER BY modified_at"
                    % ",".join("?" * len(cfg.spaces)), cfg.spaces)
    return [r for r in rows if (_parse_ts(r["modified_at"]) or datetime.now(timezone.utc)) < cutoff]


def current_candidates(db: Database, cfg: Config) -> list:
    """Pages that currently pass the prefilter (also used by the report)."""
    stale = date_stale_pages(db, cfg)
    if not analytics_available(db):
        return stale
    return [r for r in stale if r["views_count"] == 0]


def _refresh_views(cfg: Config, db: Database, confluence: Confluence | None,
                   pages: list, stats: dict) -> None:
    setting = cfg["triage"]["use_analytics"]
    if setting is False or confluence is None or not pages:
        if setting is False:
            db.set_meta("analytics_available", "no")
        return

    unread_days = cfg["triage"]["unread_days"]
    since = _days_ago(unread_days).date().isoformat()
    refresh_cutoff = _days_ago(cfg["triage"]["analytics_refresh_days"])

    # Probe on the first page; degrade gracefully if the plan has no analytics API.
    probe = confluence.page_views(pages[0]["id"], since)
    if probe is None:
        db.set_meta("analytics_available", "no")
        log.warning("Page analytics API unavailable: using modification dates only")
        return
    db.set_meta("analytics_available", "yes")

    now = utcnow()
    for i, page in enumerate(pages):
        checked = _parse_ts(page["views_checked_at"])
        fresh = checked and checked >= refresh_cutoff and page["views_since"] == since
        if i > 0 and fresh:
            continue
        count = probe if i == 0 else confluence.page_views(page["id"], since)
        stats["analytics_requests"] += 1
        db.execute("UPDATE pages SET views_count = ?, views_since = ?, views_checked_at = ? "
                   "WHERE id = ?", (count, since, now, page["id"]))
    db.commit()


def historical_rule(page, cfg: Config) -> str | None:
    """Reason why labels or title mark the page as a historical record, else None."""
    labels = {lab.lower() for lab in json.loads(page["labels_json"])}
    hist_labels = {lab.lower() for lab in cfg["triage"]["historical_labels"]}
    hit = labels & hist_labels
    if hit:
        return f"label '{sorted(hit)[0]}' marks a historical record"
    for pattern in cfg["triage"]["historical_title_patterns"]:
        if re.search(pattern, page["title"]):
            return f"title matches historical-record pattern {pattern!r}"
    return None


def _save(db: Database, page, version: str, model: str | None, source: str,
          category: str, confidence: float, reason: str, run_id: int) -> None:
    db.execute(
        """INSERT OR REPLACE INTO triage(page_id, content_hash, prompt_version, model, source,
               category, confidence, reason, run_id, classified_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (page["id"], page["content_hash"], version, model, source, category, confidence,
         reason, run_id, utcnow()))
    db.commit()


def run_triage(cfg: Config, db: Database, confluence: Confluence | None, llm: ClaudeCLI,
               run_id: int) -> dict:
    stats = {"date_stale": 0, "candidates": 0, "classified_llm": 0, "classified_rule": 0,
             "cached": 0, "unchanged": 0, "excluded": 0, "errors": 0, "pending": 0,
             "analytics_requests": 0, "stopped": None}
    tcfg = cfg["triage"]
    template = load_prompt("triage")
    model = cfg["llm"]["models"]["classify"]
    exclude_labels = {lab.lower() for lab in cfg["exclude_labels"]}

    stale = date_stale_pages(db, cfg)
    stats["date_stale"] = len(stale)
    _refresh_views(cfg, db, confluence, stale, stats)
    candidates = current_candidates(db, cfg)
    stats["candidates"] = len(candidates)
    use_views = analytics_available(db)

    titles_by_space: dict[str, list[tuple[str, str]]] = {}
    for r in db.query("SELECT id, space_key, title FROM pages WHERE active = 1"):
        titles_by_space.setdefault(r["space_key"], []).append((r["id"], r["title"]))

    for idx, page in enumerate(candidates):
        prev = db.one("SELECT content_hash, prompt_version FROM triage WHERE page_id = ?",
                      (page["id"],))
        labels = json.loads(page["labels_json"])

        if exclude_labels & {lab.lower() for lab in labels}:
            stats["excluded"] += 1
            continue

        rule_reason = historical_rule(page, cfg)
        if rule_reason:
            if not (prev and prev["content_hash"] == page["content_hash"]
                    and prev["prompt_version"] == RULE_VERSION):
                _save(db, page, RULE_VERSION, None, "rule", "historical_record", 1.0,
                      f"Rule: {rule_reason}.", run_id)
                stats["classified_rule"] += 1
            else:
                stats["unchanged"] += 1
            continue

        if prev and prev["content_hash"] == page["content_hash"] \
                and prev["prompt_version"] == template.version:
            stats["unchanged"] += 1
            continue

        others = [t for pid, t in titles_by_space.get(page["space_key"], []) if pid != page["id"]]
        similar = difflib.get_close_matches(page["title"], others, n=5, cutoff=0.6)
        if use_views:
            views = f"{page['views_count']} in the last {tcfg['unread_days']} days"
        else:
            views = "unknown (analytics not available)"
        variables = dict(
            title=page["title"], space=page["space_key"],
            labels=", ".join(labels) or "none",
            created=(page["created_at"] or "")[:10],
            modified=(page["modified_at"] or "")[:10],
            last_modifier=db.user_name(page["last_modifier_id"]) or "unknown",
            views=views,
            similar_titles="; ".join(similar) or "none",
            content=truncate(page["body_text"], tcfg["max_chars"]) or "(empty page)",
        )

        hits_before = llm.stats.cache_hits
        try:
            result = llm.complete(template, model, schemas.TRIAGE, **variables)
        except (BudgetExhausted, RateLimitReached) as exc:
            log.warning("Triage stopped: %s", exc)
            stats["stopped"] = "rate_limit" if isinstance(exc, RateLimitReached) else "budget"
            break
        except LLMError as exc:
            log.error("Triage failed for page %s (%s): %s", page["id"], page["title"], exc)
            stats["errors"] += 1
            continue
        if llm.stats.cache_hits > hits_before:
            stats["cached"] += 1
        else:
            stats["classified_llm"] += 1
        _save(db, page, template.version, model, "llm", result["category"],
              float(result["confidence"]), result["reason"], run_id)

    stats["pending"] = pending_count(db, cfg, template.version)
    log.info("Triage done: %s", stats)
    return stats


def pending_count(db: Database, cfg: Config, prompt_version: str) -> int:
    exclude_labels = {lab.lower() for lab in cfg["exclude_labels"]}
    n = 0
    for page in current_candidates(db, cfg):
        if exclude_labels & {lab.lower() for lab in json.loads(page["labels_json"])}:
            continue
        t = db.one("SELECT content_hash, prompt_version FROM triage WHERE page_id = ?",
                   (page["id"],))
        if not t or t["content_hash"] != page["content_hash"] \
                or t["prompt_version"] not in (prompt_version, RULE_VERSION):
            n += 1
    return n
