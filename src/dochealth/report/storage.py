"""Render ReportData as Confluence storage format (XHTML with macros)."""

from __future__ import annotations

from html import escape

from .data import DuplicateCluster, ReportData

CATEGORY_STYLE = {
    "obsolete": ("Red", "OBSOLETE"),
    "duplicate": ("Yellow", "DUPLICATE"),
    "historical_record": ("Blue", "HISTORICAL"),
    "relevant": ("Green", "RELEVANT"),
}
# Order sections by how actionable they are.
CATEGORY_ORDER = ["obsolete", "duplicate", "historical_record", "relevant"]


def lozenge(title: str, colour: str) -> str:
    return ('<ac:structured-macro ac:name="status">'
            f'<ac:parameter ac:name="colour">{colour}</ac:parameter>'
            f'<ac:parameter ac:name="title">{escape(title)}</ac:parameter>'
            '</ac:structured-macro>')


def panel(kind: str, html: str) -> str:
    return (f'<ac:structured-macro ac:name="{kind}"><ac:rich-text-body>{html}'
            '</ac:rich-text-body></ac:structured-macro>')


def table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tbody><tr>{head}</tr>{body}</tbody></table>"


def link(title: str, url: str) -> str:
    return f'<a href="{escape(url)}">{escape(title)}</a>' if url else escape(title)


def render_storage(data: ReportData, max_rows: int = 200) -> str:
    out: list[str] = []
    rs = data.run_stats

    # Summary
    out.append("<h1>Summary</h1>")
    counts = {c: len(data.triage.get(c, [])) for c in CATEGORY_ORDER}
    out.append(table(["Metric", "Value"], [
        ["Spaces analyzed", escape(", ".join(data.spaces))],
        ["Pages in inventory", str(data.total_pages)],
        ["Archive candidates", str(data.candidates)],
        *[[lozenge(CATEGORY_STYLE[c][1], CATEGORY_STYLE[c][0]), str(counts[c])]
          for c in CATEGORY_ORDER],
        ["Candidates not yet classified", str(data.pending)],
        ["Duplicate / overlapping page groups",
         "not analyzed" if data.duplicates is None else str(len(data.duplicates))],
        ["Contradictions found", "not analyzed" if data.duplicates is None
         else str(sum(len(c.conflicts) for c in data.duplicates))],
    ]))
    if not data.analytics:
        out.append(panel("note", "<p>Page view analytics are not available on this Confluence "
                                 "site/plan. Archive candidates are selected on modification "
                                 f"date only (not modified for {data.stale_modified_days} days)."
                                 "</p>"))
    if data.pending:
        out.append(panel("info", f"<p>{data.pending} candidate(s) are still waiting for "
                                 "classification (per-run LLM limit or usage limit reached). "
                                 "They will be processed in the next runs.</p>"))

    # Archive triage
    out.append("<h1>Archive triage</h1>")
    rule = (f"not modified for {data.stale_modified_days} days"
            + (f" and no views in the last {data.unread_days} days" if data.analytics else ""))
    out.append(f"<p>Candidates: pages {escape(rule)}. Nothing has been archived or changed; "
               "this is a recommendation for page owners and space admins.</p>")
    for cat in CATEGORY_ORDER:
        rows = data.triage.get(cat, [])
        colour, label = CATEGORY_STYLE[cat]
        out.append(f"<h2>{lozenge(label, colour)} {escape(cat.replace('_', ' ').title())} "
                   f"({len(rows)})</h2>")
        if not rows:
            out.append("<p><em>None.</em></p>")
            continue
        out.append(table(
            ["Page", "Space", "Owner", "Last modified", "Confidence", "Reason"],
            [[link(r.title, r.url), escape(r.space), escape(r.owner), escape(r.modified),
              f"{r.confidence:.2f}", escape(r.reason)] for r in rows[:max_rows]]))
        if len(rows) > max_rows:
            out.append(f"<p><em>{len(rows) - max_rows} more not shown (see the local "
                       "Markdown report).</em></p>")

    out.append("<h1>Duplicates and contradictions</h1>")
    out.append(render_duplicates(data))

    # Later phases
    out.append("<h1>Change suggestions</h1>")
    out.append("<p><em>Not analyzed in this run.</em></p>" if data.changes is None else "")

    # Run statistics
    out.append("<h1>Run statistics</h1>")
    out.append(table(["Metric", "Value"], [
        ["Runs since previous report", str(rs.get("runs", 0))],
        ["Pages scanned", str(rs.get("pages_scanned", 0))],
        ["LLM calls", str(rs.get("llm_calls", 0))],
        ["LLM cache hits", str(rs.get("cache_hits", 0))],
        ["LLM failures", str(rs.get("llm_failures", 0))],
        ["Errors", str(rs.get("errors", 0))],
        ["Stopped early", escape(", ".join(rs.get("stopped") or []) or "no")],
    ]))
    out.append("<p><em>Generated automatically by dochealth. AI-generated classifications, "
               "please verify before acting.</em></p>")
    return "".join(out)


RELATIONSHIP_STYLE = {"duplicate": ("Red", "DUPLICATE"), "overlapping": ("Yellow", "OVERLAP")}


def render_duplicates(data: ReportData) -> str:
    if data.duplicates is None:
        return "<p><em>Not analyzed in this run.</em></p>"
    out = ["<p>Groups of pages covering the same topic (semantic similarity), reviewed by AI for "
           "contradicting statements. Nothing has been merged or changed: the recommendation is "
           "for the page owners.</p>"]
    if data.duplicates_pending:
        out.append(panel("info", f"<p>{data.duplicates_pending} group(s) still waiting for "
                                 "analysis; they will be processed in the next runs.</p>"))
    if data.duplicates_distinct:
        out.append(f"<p><em>{data.duplicates_distinct} similar group(s) were judged related but "
                   "distinct and are not listed.</em></p>")
    if not data.duplicates:
        out.append("<p><em>No duplicate or overlapping pages found.</em></p>")
    for c in data.duplicates:
        out.append(_cluster(c))
    return "".join(out)


def _cluster(c: DuplicateCluster) -> str:
    colour, label = RELATIONSHIP_STYLE.get(c.relationship, ("Grey", c.relationship.upper()))
    conflict_badge = (" " + lozenge(f"{len(c.conflicts)} CONFLICT(S)", "Red")) if c.conflicts else ""
    out = [f"<h2>{lozenge(label, colour)}{conflict_badge} {escape(c.topic)}</h2>"]
    canonical_id = c.canonical.id if c.canonical else None
    out.append(table(["Page", "Owner", "Last modified", "Recommendation"], [
        [link(p.title, p.url), escape(p.owner), escape(p.modified),
         lozenge("CANONICAL", "Green") if p.id == canonical_id else "merge into canonical"]
        for p in c.pages]))
    if c.canonical:
        out.append(f"<p><strong>Keep:</strong> {link(c.canonical.title, c.canonical.url)}: "
                   f"{escape(c.canonical_reason)}</p>")
    if c.conflicts:
        out.append(table(["Statement A", "Statement B", "Why they conflict"], [
            [f"<em>{escape(k.statement_a)}</em><br/>({link(k.page_a.title, k.page_a.url)})",
             f"<em>{escape(k.statement_b)}</em><br/>({link(k.page_b.title, k.page_b.url)})",
             escape(k.explanation)] for k in c.conflicts]))
    if c.merge_notes:
        out.append(f"<p><strong>Merge notes:</strong> {escape(c.merge_notes)}</p>")
    out.append(f"<p><em>Similarity {c.similarity:.2f}</em></p>")
    return "".join(out)
