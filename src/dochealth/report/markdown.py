"""Render ReportData as Markdown (local copy, no row limit)."""

from __future__ import annotations

from .data import ReportData
from .storage import CATEGORY_ORDER, CATEGORY_STYLE


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
        ["Contradictions found", "not analyzed" if data.duplicates is None
         else sum(len(c.conflicts) for c in data.duplicates)],
    ])
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
    out += ["## Change suggestions", "", "_Not analyzed in this run._", ""]

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
