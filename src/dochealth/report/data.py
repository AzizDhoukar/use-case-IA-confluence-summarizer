"""Collect everything the report shows into plain data, independent of output format."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from ..config import Config
from ..db import Database
from ..llm.schemas import TRIAGE_CATEGORIES
from ..phases.triage import analytics_available, current_candidates, pending_count
from ..llm.prompts import load_prompt

REPORT_TITLE = "Documentation Health Report – {date}"


@dataclass
class TriageRow:
    title: str
    url: str
    space: str
    owner: str
    modified: str
    category: str
    confidence: float
    reason: str
    source: str


@dataclass
class ReportData:
    title: str
    report_date: str
    spaces: list[str]
    analytics: bool
    stale_modified_days: int
    unread_days: int
    total_pages: int
    candidates: int
    pending: int
    triage: dict[str, list[TriageRow]] = field(default_factory=dict)
    duplicates: list | None = None   # Phase 2
    changes: list | None = None      # Phase 3
    run_stats: dict = field(default_factory=dict)


def owner_name(db: Database, owner_id: str | None, last_modifier_id: str | None) -> str:
    """Page owner, falling back to the last modifier when the owner is gone or unknown."""
    if owner_id:
        row = db.one("SELECT display_name, account_status FROM users WHERE account_id = ?",
                     (owner_id,))
        if row is None or row["account_status"] in (None, "active"):
            return db.user_name(owner_id)
    return db.user_name(last_modifier_id)


def collect_run_stats(db: Database, current_run_id: int | None, current: dict | None) -> dict:
    """Aggregate stats of runs since the previous report, plus the run in progress."""
    since = int(db.get_meta("last_report_run_id", "0") or 0)
    stats_list = [json.loads(r["stats_json"]) for r in db.query(
        "SELECT stats_json FROM runs WHERE id > ? AND id != ? AND stats_json IS NOT NULL",
        (since, current_run_id or -1))]
    if current:
        stats_list.append(current)

    agg = {"runs": len(stats_list), "pages_scanned": 0, "llm_calls": 0, "cache_hits": 0,
           "llm_failures": 0, "errors": 0, "stopped": []}
    for s in stats_list:
        agg["pages_scanned"] += (s.get("inventory") or {}).get("pages_scanned", 0)
        llm = s.get("llm") or {}
        agg["llm_calls"] += llm.get("calls", 0)
        agg["cache_hits"] += llm.get("cache_hits", 0)
        agg["llm_failures"] += llm.get("failures", 0)
        agg["errors"] += len(s.get("errors") or []) + (s.get("triage") or {}).get("errors", 0)
        stopped = (s.get("triage") or {}).get("stopped")
        if stopped:
            agg["stopped"].append(stopped)
    return agg


def build_report_data(cfg: Config, db: Database, current_run_id: int | None = None,
                      current_stats: dict | None = None, today: date | None = None) -> ReportData:
    today = today or date.today()
    candidates = current_candidates(db, cfg)
    total = db.one("SELECT COUNT(*) AS n FROM pages WHERE active = 1 AND space_key IN (%s)"
                   % ",".join("?" * len(cfg.spaces)), cfg.spaces)["n"]

    triage: dict[str, list[TriageRow]] = {c: [] for c in TRIAGE_CATEGORIES}
    for page in candidates:
        t = db.one("SELECT * FROM triage WHERE page_id = ? AND content_hash = ?",
                   (page["id"], page["content_hash"]))
        if not t:
            continue
        triage[t["category"]].append(TriageRow(
            title=page["title"], url=page["url"] or "", space=page["space_key"],
            owner=owner_name(db, page["owner_id"], page["last_modifier_id"]),
            modified=(page["modified_at"] or "")[:10], category=t["category"],
            confidence=t["confidence"], reason=t["reason"] or "", source=t["source"]))
    for rows in triage.values():
        rows.sort(key=lambda r: r.confidence, reverse=True)

    return ReportData(
        title=REPORT_TITLE.format(date=today.isoformat()),
        report_date=today.isoformat(),
        spaces=cfg.spaces,
        analytics=analytics_available(db),
        stale_modified_days=cfg["triage"]["stale_modified_days"],
        unread_days=cfg["triage"]["unread_days"],
        total_pages=total,
        candidates=len(candidates),
        pending=pending_count(db, cfg, load_prompt("triage").version),
        triage=triage,
        run_stats=collect_run_stats(db, current_run_id, current_stats),
    )
