"""Phase 3: check Jira issues done since a date (summary, description, comments) against the
documentation; when the docs contradict what was done, alert the page owner with a footer
comment (with replacement text when the issue gives enough detail) and list it in the report."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from ..atlassian.jira import Issue, Jira
from ..config import Config
from ..db import Database, utcnow
from ..embeddings import Encoder, ensure_embeddings, from_blob
from ..llm import schemas
from ..llm.claude_cli import BudgetExhausted, ClaudeCLI, LLMError, RateLimitReached
from ..llm.prompts import load_prompt
from ..publisher import Publisher
from ..report.storage import comment_storage
from ..text import truncate
from .duplicates import eligible_pages
from .triage import historical_rule

log = logging.getLogger(__name__)


def norm(text: str) -> str:
    return " ".join(text.split())


def fingerprint(page_id: str, doc_text: str, proposed: str, ticket_text: str = "") -> str:
    """Identity of an alert: same page passage + same fix (or same finding when no fix)."""
    other = norm(proposed) if proposed.strip() else "ticket:" + norm(ticket_text)
    return hashlib.sha256(json.dumps([page_id, norm(doc_text), other],
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


# --- 1. fetch ---------------------------------------------------------------

def fetch_issues(cfg: Config, db: Database, jira: Jira, run_id: int, stats: dict,
                 now: datetime | None = None) -> None:
    """Store issues done since the start date as 'pending' (re-done issues too).

    Start = --since (explicit re-scan) > end of the previous fetch > jira.since >
    first_run_lookback_days. Issues already stored are not processed again.
    """
    jcfg = cfg["jira"]
    now = now or datetime.now(timezone.utc)
    cursor = db.get_meta("jira_since")
    if jcfg.get("since_override"):
        since = datetime.fromisoformat(jcfg["since_override"]).replace(tzinfo=timezone.utc)
    elif cursor:
        since = datetime.fromisoformat(cursor)
    elif jcfg["since"]:
        since = datetime.fromisoformat(jcfg["since"]).replace(tzinfo=timezone.utc)
    else:
        since = now - timedelta(days=jcfg["first_run_lookback_days"])
    # One day of overlap absorbs time zones (JQL dates are in the Jira user's zone);
    # issues already stored with the same done date are ignored.
    query_since = (since - timedelta(days=1)).strftime("%Y/%m/%d %H:%M")
    log.info("Jira: issues matching '%s' with %s >= %s", jcfg["jql"], jcfg["date_field"],
             query_since)
    for issue in jira.done_issues(jcfg["jql"], jcfg["date_field"], query_since,
                                  jcfg["page_size"]):
        stats["fetched"] += 1
        row = db.one("SELECT done_at FROM jira_issues WHERE key = ?", (issue.key,))
        if row and row["done_at"] == issue.done_at:
            continue
        db.execute(
            "INSERT INTO jira_issues(key, done_at, data_json, status, fetched_run) "
            "VALUES(?,?,?,'pending',?) ON CONFLICT(key) DO UPDATE SET "
            "done_at = excluded.done_at, data_json = excluded.data_json, status = 'pending', "
            "reason = NULL, fetched_run = excluded.fetched_run",
            (issue.key, issue.done_at, json.dumps(issue.to_dict(), ensure_ascii=False), run_id))
        stats["new_issues"] += 1
    db.commit()
    db.set_meta("jira_since", now.isoformat())


# --- 2. rule-based prefilter ------------------------------------------------

def skip_reason(issue: dict, ccfg: dict) -> str | None:
    """Issues that cannot make documentation outdated are dropped before any embedding/LLM."""
    def lowered(key: str) -> set[str]:
        return {v.lower() for v in ccfg[key]}

    if ccfg["skip_subtasks"] and issue["subtask"]:
        return "sub-task"
    if issue["type"].lower() in lowered("skip_issue_types"):
        return f"issue type '{issue['type']}'"
    if issue["status"].lower() in lowered("skip_statuses"):
        return f"status '{issue['status']}'"
    if issue["resolution"] and issue["resolution"].lower() in lowered("skip_resolutions"):
        return f"resolution '{issue['resolution']}'"
    return None


# --- 3. retrieval -----------------------------------------------------------

def target_pages(db: Database, cfg: Config) -> list:
    """Pages that may receive suggestions: historical records (sprint logs, meeting notes...)
    and pages triaged as obsolete are kept as they are."""
    out = []
    for page in eligible_pages(db, cfg):
        if historical_rule(page, cfg):
            continue
        t = db.one("SELECT category FROM triage WHERE page_id = ? AND content_hash = ?",
                   (page["id"], page["content_hash"]))
        if t and t["category"] in ("historical_record", "obsolete"):
            continue
        out.append(page)
    return out


def chunk_index(cfg: Config, db: Database, encoder: Encoder, stats: dict
                ) -> tuple[list, np.ndarray]:
    """All chunks of the target pages and their embedding matrix (built incrementally)."""
    pages = target_pages(db, cfg)
    stats["target_pages"] = len(pages)
    stats["pages_embedded"] = ensure_embeddings(db, encoder, pages,
                                                cfg["embeddings"]["chunk_tokens"])
    wanted = {p["id"] for p in pages}
    rows = [r for r in db.query("SELECT page_id, idx, heading, text, embedding FROM chunks "
                                "WHERE model = ? ORDER BY page_id, idx", (encoder.name,))
            if r["page_id"] in wanted]
    matrix = (np.stack([from_blob(r["embedding"]) for r in rows]) if rows
              else np.zeros((0, 0), dtype=np.float32))
    return rows, matrix


def top_chunks(vec: np.ndarray, rows: list, matrix: np.ndarray, threshold: float,
               k: int) -> list[tuple]:
    if not rows:
        return []
    sims = matrix @ vec
    return [(rows[i], float(sims[i])) for i in np.argsort(-sims)[:k] if sims[i] >= threshold]


def excerpts_block(db: Database, matches: list[tuple], max_chars: int) -> tuple[str, dict]:
    parts, by_id = [], {}
    for n, (row, sim) in enumerate(matches, 1):
        eid = f"E{n}"
        by_id[eid] = (row, sim)
        page = db.one("SELECT title FROM pages WHERE id = ?", (row["page_id"],))
        title = (page["title"] if page else row["page_id"]).replace('"', "'")
        section = (row["heading"] or "").replace('"', "'")
        parts.append(f'<excerpt id="{eid}" page="{title}" section="{section}">\n'
                     f'{truncate(row["text"], max_chars)}\n</excerpt>')
    return "\n\n".join(parts), by_id


# --- 4. main loop -----------------------------------------------------------

def run_changes(cfg: Config, db: Database, jira: Jira, llm: ClaudeCLI, encoder: Encoder,
                publisher: Publisher, run_id: int) -> dict:
    ccfg = cfg["changes"]
    stats = {"fetched": 0, "new_issues": 0, "comment_requests": 0, "processed": 0, "skipped": 0, "no_match": 0,
             "analyzed": 0, "cached": 0, "discrepancies": 0, "repeated": 0, "rejected": 0,
             "low_confidence": 0, "comments": 0, "outdated": 0, "deferred": 0,
             "target_pages": 0, "pages_embedded": 0, "errors": 0, "pending": 0, "stopped": None}
    template = load_prompt("changes")
    model = cfg["llm"]["models"]["draft"]
    db.set_meta("changes_run_id", str(run_id))

    fetch_issues(cfg, db, jira, run_id, stats)

    # Free steps first, for every pending issue: rules, then retrieval.
    issues = []
    for rec in db.query("SELECT * FROM jira_issues WHERE status = 'pending' "
                        "ORDER BY done_at, key"):
        issue = json.loads(rec["data_json"])
        reason = skip_reason(issue, ccfg)
        if reason:
            _mark(db, issue["key"], "skipped", reason, run_id)
            stats["skipped"] += 1
            continue
        if not issue.get("comments_loaded"):   # only for issues that passed the rules
            issue["comments"] = jira.comments(issue["key"])
            issue["comments_loaded"] = True
            stats["comment_requests"] += 1
            db.execute("UPDATE jira_issues SET data_json = ? WHERE key = ?",
                       (json.dumps(issue, ensure_ascii=False), issue["key"]))
            db.commit()
        issues.append(issue)
    candidates = []
    if issues:   # only load the model and chunks when an issue needs them
        rows, matrix = chunk_index(cfg, db, encoder, stats)
        texts = [Issue(**issue).text(ccfg["max_issue_chars"], ccfg["max_comment_chars"])
                 for issue in issues]
        for issue, text, vec in zip(issues, texts, encoder.encode(texts)):
            matches = top_chunks(vec, rows, matrix, ccfg["similarity_threshold"],
                                 ccfg["top_k"])
            if matches:
                candidates.append((issue, text, matches))
            else:
                _mark(db, issue["key"], "no_match", None, run_id)
                stats["no_match"] += 1
    # Closest matches first, so a limited LLM budget goes to the most likely updates.
    candidates.sort(key=lambda c: -c[2][0][1])

    for issue, text, matches in candidates:
        block, by_id = excerpts_block(db, matches, ccfg["max_chars_per_excerpt"])
        hits_before = llm.stats.cache_hits
        try:
            result = llm.complete(template, model, schemas.CHANGES, issue=text,
                                  done_at=(issue["done_at"] or "")[:10], excerpts=block)
        except (BudgetExhausted, RateLimitReached) as exc:
            log.warning("Change detection stopped: %s", exc)
            stats["stopped"] = "rate_limit" if isinstance(exc, RateLimitReached) else "budget"
            break
        except LLMError as exc:
            log.error("Issue %s failed: %s", issue["key"], exc)
            _mark(db, issue["key"], "failed", str(exc)[:300], run_id)
            stats["errors"] += 1
            continue
        stats["cached" if llm.stats.cache_hits > hits_before else "analyzed"] += 1
        for item in result["excerpts"]:
            _store(db, issue["key"], item, by_id, ccfg["min_confidence"], run_id, stats)
        _mark(db, issue["key"], "done", None, run_id)
        stats["processed"] += 1

    post_suggestions(cfg, db, publisher, run_id, stats)
    stats["pending"] = db.one("SELECT COUNT(*) AS n FROM jira_issues "
                              "WHERE status = 'pending'")["n"]
    log.info("Change detection done: %s", stats)
    return stats


def _mark(db: Database, key: str, status: str, reason: str | None, run_id: int) -> None:
    db.execute("UPDATE jira_issues SET status = ?, reason = ?, processed_run = ? WHERE key = ?",
               (status, reason, run_id, key))
    db.commit()


def _store(db: Database, issue_key: str, item: dict, by_id: dict, min_confidence: float,
           run_id: int, stats: dict) -> None:
    match = by_id.get(item["excerpt"])
    if not match or item["verdict"] != "discrepancy":
        return
    row, sim = match
    doc = item["doc_text"].strip()
    ticket = item["ticket_text"].strip()
    proposed = item["proposed_text"].strip()
    # The quoted text must really be in the page, otherwise the owner cannot find it.
    if not doc or not ticket or norm(doc) == norm(proposed) or norm(doc) not in norm(row["text"]):
        log.info("Rejected discrepancy for %s on page %s: quoted page text not found",
                 issue_key, row["page_id"])
        stats["rejected"] += 1
        return
    status = "new" if item["confidence"] >= min_confidence else "low_confidence"
    cur = db.execute(
        "INSERT OR IGNORE INTO suggestions(fingerprint, issue_key, page_id, confidence, "
        "similarity, current_text, ticket_text, proposed_text, explanation, status, "
        "created_run, last_run) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (fingerprint(row["page_id"], doc, proposed, ticket), issue_key, row["page_id"],
         item["confidence"], sim, doc, ticket, proposed, item["explanation"], status,
         run_id, run_id))
    db.commit()
    if cur.rowcount == 0:
        stats["repeated"] += 1
    elif status == "low_confidence":
        stats["low_confidence"] += 1
    else:
        stats["discrepancies"] += 1


# --- 5. comments ------------------------------------------------------------

def post_suggestions(cfg: Config, db: Database, publisher: Publisher, run_id: int,
                     stats: dict) -> None:
    """Post new alerts (and ones only written by a previous dry run) as comments."""
    limit = cfg["changes"]["max_comments_per_run"]
    rows = db.query("SELECT * FROM suggestions WHERE status IN ('new', 'dry_run') "
                    "ORDER BY confidence DESC, id")
    for s in rows:
        page = db.one("SELECT * FROM pages WHERE id = ?", (s["page_id"],))
        if page is None or not page["active"] \
                or norm(s["current_text"]) not in norm(page["body_text"]):
            _set(db, s["id"], run_id, status="outdated")
            stats["outdated"] += 1
            continue
        if db.one("SELECT 1 FROM suggestions WHERE page_id = ? AND issue_key = ? "
                  "AND status = 'posted' AND id != ?", (s["page_id"], s["issue_key"], s["id"])):
            _set(db, s["id"], run_id, status="duplicate")
            continue
        if stats["comments"] >= limit:
            stats["deferred"] += 1
            continue
        rec = db.one("SELECT data_json FROM jira_issues WHERE key = ?", (s["issue_key"],))
        issue = json.loads(rec["data_json"]) if rec else {"key": s["issue_key"], "url": "",
                                                          "summary": ""}
        account = db.owner_account(page["owner_id"], page["last_modifier_id"])
        storage = comment_storage(issue, s["current_text"], s["ticket_text"] or "",
                                  s["proposed_text"], s["explanation"] or "", s["confidence"],
                                  account)
        cid, location = publisher.post_comment(
            s["page_id"], page["url"] or "", f"{s['issue_key']}-{s['page_id']}-{s['id']}",
            storage, {"page_title": page["title"], "issue": s["issue_key"],
                      "mention": account, "confidence": s["confidence"]})
        if publisher.dry_run:
            _set(db, s["id"], run_id, status="dry_run", location=location)
        else:
            _set(db, s["id"], run_id, status="posted", comment_id=cid, location=location,
                 posted_at=utcnow())
        stats["comments"] += 1


def _set(db: Database, sid: int, run_id: int, **fields) -> None:
    fields["last_run"] = run_id
    db.execute("UPDATE suggestions SET %s WHERE id = ?" % ", ".join(f"{k} = ?" for k in fields),
               (*fields.values(), sid))
    db.commit()
