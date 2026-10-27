"""Collect everything the report shows into plain data, independent of output format."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from ..config import REPORT_TITLE, Config
from ..db import Database
from ..llm.schemas import TRIAGE_CATEGORIES
from ..phases.triage import analytics_available, current_candidates, pending_count
from ..llm.prompts import load_prompt



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
class PageRef:
    id: str
    title: str
    url: str
    owner: str
    modified: str


@dataclass
class Conflict:
    page_a: PageRef
    statement_a: str
    page_b: PageRef
    statement_b: str
    explanation: str


@dataclass
class DuplicateCluster:
    topic: str
    relationship: str
    similarity: float
    pages: list[PageRef]
    canonical: PageRef | None
    canonical_reason: str
    conflicts: list[Conflict]
    merge_notes: str


@dataclass
class ChangeRow:
    issue_key: str
    issue_url: str
    issue_summary: str
    page: PageRef
    confidence: float
    status: str          # posted | dry_run | low_confidence | outdated | new (deferred)
    location: str        # comment URL or local dry-run file
    current_text: str
    ticket_text: str
    proposed_text: str
    explanation: str


@dataclass
class ChangesSummary:
    issues_fetched: int = 0
    issues_skipped: int = 0
    issues_no_match: int = 0
    issues_analyzed: int = 0
    issues_pending: int = 0


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
    duplicates: list[DuplicateCluster] | None = None   # None = phase never run
    duplicates_pending: int = 0
    duplicates_distinct: int = 0     # clusters judged related_but_distinct (not listed)
    page_conflicts: list[Conflict] | None = None   # section-level, None = never run
    page_conflicts_pending: int = 0
    changes: list[ChangeRow] | None = None   # None = phase never run
    changes_summary: ChangesSummary = field(default_factory=ChangesSummary)
    run_stats: dict = field(default_factory=dict)

    @property
    def contradictions(self) -> int | None:
        """Contradictions inside duplicate groups plus between page sections."""
        if self.duplicates is None and self.page_conflicts is None:
            return None
        return (sum(len(c.conflicts) for c in self.duplicates or [])
                + len(self.page_conflicts or []))


def owner_name(db: Database, owner_id: str | None, last_modifier_id: str | None) -> str:
    """Page owner, falling back to the last modifier when the owner is gone or unknown."""
    return db.user_name(db.owner_account(owner_id, last_modifier_id))


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
        agg["errors"] += (s.get("duplicates") or {}).get("errors", 0)
        agg["errors"] += (s.get("changes") or {}).get("errors", 0)
        agg["errors"] += (s.get("conflicts") or {}).get("errors", 0)
        for phase in ("triage", "duplicates", "conflicts", "changes"):
            stopped = (s.get(phase) or {}).get("stopped")
            if stopped:
                agg["stopped"].append(f"{phase}: {stopped}")
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

    duplicates, dup_pending, dup_distinct = collect_duplicates(db)
    changes, changes_summary = collect_changes(db)
    page_conflicts, page_conflicts_pending = collect_page_conflicts(db)
    return ReportData(
        title=REPORT_TITLE.format(date=today.isoformat()),
        report_date=today.isoformat(),
        spaces=[s.describe() for s in cfg.scopes],
        analytics=analytics_available(db),
        stale_modified_days=cfg["triage"]["stale_modified_days"],
        unread_days=cfg["triage"]["unread_days"],
        total_pages=total,
        candidates=len(candidates),
        pending=pending_count(db, cfg, load_prompt("triage").version),
        triage=triage,
        duplicates=duplicates,
        duplicates_pending=dup_pending,
        duplicates_distinct=dup_distinct,
        page_conflicts=page_conflicts,
        page_conflicts_pending=page_conflicts_pending,
        changes=changes,
        changes_summary=changes_summary,
        run_stats=collect_run_stats(db, current_run_id, current_stats),
    )


def page_ref(db: Database, page_id: str) -> PageRef:
    p = db.one("SELECT * FROM pages WHERE id = ?", (page_id,))
    if p is None:
        return PageRef(page_id, page_id, "", "", "")
    return PageRef(page_id, p["title"], p["url"] or "",
                   owner_name(db, p["owner_id"], p["last_modifier_id"]),
                   (p["modified_at"] or "")[:10])


def collect_duplicates(db: Database) -> tuple[list[DuplicateCluster] | None, int, int]:
    """Clusters found by the latest duplicates run."""
    run_id = db.get_meta("duplicates_run_id")
    if run_id is None:
        return None, 0, 0
    rows = db.query("SELECT * FROM clusters WHERE last_seen_run = ? ORDER BY similarity DESC",
                    (int(run_id),))
    clusters, pending, distinct = [], 0, 0
    for r in rows:
        if r["status"] != "analyzed" or not r["result_json"]:
            pending += 1
            continue
        res = json.loads(r["result_json"])
        if res["relationship"] == "related_but_distinct":
            distinct += 1
            continue
        refs = {pid: page_ref(db, pid) for pid in json.loads(r["page_ids_json"])}
        clusters.append(DuplicateCluster(
            topic=res["topic"], relationship=res["relationship"], similarity=r["similarity"],
            pages=list(refs.values()),
            canonical=refs.get(res.get("canonical_page")),
            canonical_reason=res.get("canonical_reason", ""),
            conflicts=[Conflict(refs[c["page_a"]], c["statement_a"], refs[c["page_b"]],
                                c["statement_b"], c["explanation"])
                       for c in res.get("conflicts", [])
                       if c["page_a"] in refs and c["page_b"] in refs],
            merge_notes=res.get("merge_notes", "")))
    # Clusters with contradictions first, then duplicates before overlaps.
    clusters.sort(key=lambda c: (-len(c.conflicts), c.relationship != "duplicate", -c.similarity))
    return clusters, pending, distinct


CHANGE_STATUS_ORDER = {"posted": 0, "dry_run": 0, "new": 1, "low_confidence": 2, "outdated": 3}


def collect_changes(db: Database) -> tuple[list[ChangeRow] | None, ChangesSummary]:
    """Suggestions handled by the latest change-detection run, and its issue counts."""
    run_id = db.get_meta("changes_run_id")
    if run_id is None:
        return None, ChangesSummary()
    run_id = int(run_id)
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM jira_issues WHERE fetched_run = ? "
        "OR processed_run = ? GROUP BY status", (run_id, run_id))}
    summary = ChangesSummary(
        issues_fetched=sum(counts.values()),
        issues_skipped=counts.get("skipped", 0),
        issues_no_match=counts.get("no_match", 0),
        issues_analyzed=counts.get("done", 0) + counts.get("failed", 0),
        issues_pending=db.one("SELECT COUNT(*) AS n FROM jira_issues "
                              "WHERE status = 'pending'")["n"])
    rows = []
    for s in db.query("SELECT * FROM suggestions WHERE last_run = ? AND status != 'duplicate'",
                      (run_id,)):
        rec = db.one("SELECT data_json FROM jira_issues WHERE key = ?", (s["issue_key"],))
        issue = json.loads(rec["data_json"]) if rec else {}
        rows.append(ChangeRow(
            issue_key=s["issue_key"], issue_url=issue.get("url", ""),
            issue_summary=issue.get("summary", ""), page=page_ref(db, s["page_id"]),
            confidence=s["confidence"], status=s["status"], location=s["location"] or "",
            current_text=s["current_text"], ticket_text=s["ticket_text"] or "",
            proposed_text=s["proposed_text"],
            explanation=s["explanation"] or ""))
    rows.sort(key=lambda r: (CHANGE_STATUS_ORDER.get(r.status, 9), -r.confidence))
    return rows, summary


def collect_page_conflicts(db: Database) -> tuple[list[Conflict] | None, int]:
    """Section-level contradictions found by the latest conflicts run."""
    run_id = db.get_meta("conflicts_run_id")
    if run_id is None:
        return None, 0
    out, pending = [], 0
    for r in db.query("SELECT * FROM passage_pairs WHERE last_seen_run = ? "
                      "ORDER BY similarity DESC", (int(run_id),)):
        if r["status"] != "analyzed" or not r["result_json"]:
            pending += 1
            continue
        a, b = page_ref(db, r["page_a"]), page_ref(db, r["page_b"])
        out += [Conflict(a, c["statement_a"], b, c["statement_b"], c["explanation"])
                for c in json.loads(r["result_json"])["conflicts"]]
    return out, pending
