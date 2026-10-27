"""Render ReportData as Markdown (local copy, no row limit)."""

from __future__ import annotations

from .data import ReportData
from .storage import CATEGORY_DEFINITIONS, CATEGORY_ORDER, CATEGORY_STYLE, CHANGE_STYLE


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return lines + [""]


def render_markdown(data: ReportData) -> str:
    rs = data.run_stats
    out = [f"# {data.title}", ""]

    out += ["## Summary", ""]
    out += _table(["Metric", "Value"], [
        ["Spaces analyzed", ", ".join(data.spaces)],
        ["Pages in inventory", data.total_pages],
        ["Archive candidates", data.candidates],
        *[[CATEGORY_STYLE[c][1], len(data.triage.get(c, []))] for c in CATEGORY_ORDER],
        ["Candidates not yet classified", data.pending],
        ["Duplicate / overlapping page groups",
         "not analyzed" if data.duplicates is None else len(data.duplicates)],
        ["Contradictions found", "not analyzed" if data.contradictions is None
         else data.contradictions],
        ["Discrepancies with Jira", "not analyzed" if data.changes is None
         else sum(1 for c in data.changes if c.status in ("posted", "dry_run"))],
    ])
    out += ["### Categories", ""]
    out += _table(["Category", "Definition"],
                  [[CATEGORY_STYLE[c][1], CATEGORY_DEFINITIONS[c]] for c in CATEGORY_ORDER])
    if not data.analytics:
        out += [f"> **Note:** page view analytics are not available; candidates are selected on "
                f"modification date only (not modified for {data.stale_modified_days} days).", ""]
    if data.pending:
        out += [f"> {data.pending} candidate(s) still waiting for classification; they will be "
                "processed in the next runs.", ""]

    out += ["## Archive triage", ""]
    for cat in CATEGORY_ORDER:
        rows = data.triage.get(cat, [])
        out += [f"### {CATEGORY_STYLE[cat][1]} – {cat.replace('_', ' ').title()} ({len(rows)})", ""]
        if not rows:
            out += ["_None._", ""]
            continue
        out += _table(["Page", "Space", "Owner", "Last modified", "Confidence", "Reason"],
                      [[f"[{_cell(r.title)}]({r.url})" if r.url else r.title, r.space, r.owner,
                        r.modified, f"{r.confidence:.2f}", r.reason] for r in rows])

    out += ["## Duplicates and contradictions", ""] + _duplicates(data)
    out += ["### Contradictions between pages", ""] + _page_conflicts(data)
    out += ["## Jira vs documentation: discrepancies", ""] + _changes(data)

    out += ["## Run statistics", ""]
    out += _table(["Metric", "Value"], [
        ["Runs since previous report", rs.get("runs", 0)],
        ["Pages scanned", rs.get("pages_scanned", 0)],
        ["LLM calls", rs.get("llm_calls", 0)],
        ["LLM cache hits", rs.get("cache_hits", 0)],
        ["LLM failures", rs.get("llm_failures", 0)],
        ["Errors", rs.get("errors", 0)],
        ["Stopped early", ", ".join(rs.get("stopped") or []) or "no"],
    ])
    return "\n".join(str(x) for x in out)


def _link(p) -> str:
    return f"[{_cell(p.title)}]({p.url})" if p.url else _cell(p.title)


def _page_conflicts(data: ReportData) -> list[str]:
    if data.page_conflicts is None:
        return ["_Not analyzed in this run._", ""]
    out = []
    if data.page_conflicts_pending:
        out += [f"> {data.page_conflicts_pending} page pair(s) still waiting for analysis.", ""]
    if not data.page_conflicts:
        return out + ["_No contradiction found._", ""]
    return out + _table(["Page A says", "Page B says", "What differs", "Owners"], [
        [f"_{k.statement_a}_ ({_link(k.page_a)})", f"_{k.statement_b}_ ({_link(k.page_b)})",
         k.explanation, ", ".join(dict.fromkeys(o for o in (k.page_a.owner, k.page_b.owner) if o))]
        for k in data.page_conflicts])


def _duplicates(data: ReportData) -> list[str]:
    if data.duplicates is None:
        return ["_Not analyzed in this run._", ""]
    out = []
    if data.duplicates_pending:
        out += [f"> {data.duplicates_pending} group(s) still waiting for analysis.", ""]
    if data.duplicates_distinct:
        out += [f"_{data.duplicates_distinct} similar group(s) judged related but distinct "
                "(not listed)._", ""]
    if not data.duplicates:
        out += ["_No duplicate or overlapping pages found._", ""]
    for c in data.duplicates:
        badge = f" – {len(c.conflicts)} conflict(s)" if c.conflicts else ""
        out += [f"### {c.relationship.upper()}{badge}: {c.topic}", ""]
        canonical_id = c.canonical.id if c.canonical else None
        out += _table(["Page", "Owner", "Last modified", "Recommendation"],
                      [[_link(p), p.owner, p.modified,
                        "**CANONICAL**" if p.id == canonical_id else "merge into canonical"]
                       for p in c.pages])
        if c.canonical:
            out += [f"**Keep:** {_link(c.canonical)}: {c.canonical_reason}", ""]
        if c.conflicts:
            out += _table(["Statement A", "Statement B", "Why they conflict"],
                          [[f"_{k.statement_a}_ ({_link(k.page_a)})",
                            f"_{k.statement_b}_ ({_link(k.page_b)})", k.explanation]
                           for k in c.conflicts])
        if c.merge_notes:
            out += [f"**Merge notes:** {c.merge_notes}", ""]
        out += [f"_Similarity {c.similarity:.2f}_", ""]
    return out


def _changes(data: ReportData) -> list[str]:
    if data.changes is None:
        return ["_Not analyzed in this run._", ""]
    cs = data.changes_summary
    out = _table(["Jira issues", "Count"], [
        ["Handled in this run", cs.issues_fetched],
        ["Skipped by rules (sub-tasks, abandoned...)", cs.issues_skipped],
        ["No related documentation", cs.issues_no_match],
        ["Analyzed by AI", cs.issues_analyzed],
        ["Waiting for the next run", cs.issues_pending],
    ])
    if not data.changes:
        return out + ["_No discrepancy found._", ""]
    for c in data.changes:
        issue = f"[{c.issue_key}]({c.issue_url})" if c.issue_url else c.issue_key
        status = CHANGE_STYLE.get(c.status, ("", c.status.upper()))[1]
        where = f" ([comment]({c.location}))" if c.status == "posted" and c.location else ""
        out += [f"### {issue} → {_link(c.page)}", "",
                f"{c.issue_summary}  ", f"Owner: {c.page.owner} · confidence {c.confidence:.2f}"
                f" · **{status}**{where}", "",
                "**The page says:**", "", *[f"> {ln}" for ln in c.current_text.splitlines()], "",
                "**The issue says:**", "", *[f"> {ln}" for ln in c.ticket_text.splitlines()], "",
                "**Proposed text:**", "",
                *([f"> {ln}" for ln in c.proposed_text.splitlines()] or
                  ["_to be written by the owner_"]), "",
                f"**Why:** {c.explanation}", ""]
    return out
