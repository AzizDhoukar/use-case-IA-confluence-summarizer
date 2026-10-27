"""Render ReportData as Confluence storage format (XHTML with macros)."""

from __future__ import annotations

from html import escape

from .data import ReportData

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

    # Later phases
    out.append("<h1>Duplicates and contradictions</h1>")
    out.append("<p><em>Not analyzed in this run.</em></p>" if data.duplicates is None else "")
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
