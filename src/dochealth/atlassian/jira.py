"""Jira Cloud client (read-only): search issues done since a date, as plain-text records."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterator

from .http import AtlassianHTTP

# JQL date field -> the issue field holding that date in search results.
DATE_FIELDS = {
    "statusCategoryChangedDate": "statuscategorychangedate",
    "resolved": "resolutiondate",
    "updated": "updated",
}
FIELDS = ["summary", "description", "issuetype", "status", "resolution", "fixVersions",
          "issuelinks", "project"]


@dataclass
class Issue:
    key: str
    url: str
    project: str
    type: str
    subtask: bool
    status: str
    resolution: str
    done_at: str
    summary: str
    description: str
    fix_versions: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def text(self, max_chars: int | None = None) -> str:
        """Plain-text form used both for embeddings and for the prompt."""
        head = [f"[{self.key}] {self.summary}",
                f"Type: {self.type} | Status: {self.status}"
                + (f" | Resolution: {self.resolution}" if self.resolution else "")
                + (f" | Fix versions: {', '.join(self.fix_versions)}" if self.fix_versions else "")]
        desc = self.description.strip()
        if max_chars and len(desc) > max_chars:
            desc = desc[:max_chars] + " [...]"
        parts = ["\n".join(head)]
        if desc:
            parts.append("Description:\n" + desc)
        if self.links:
            parts.append("Linked issues:\n" + "\n".join(f"- {link}" for link in self.links))
        return "\n\n".join(parts)


def adf_to_text(node) -> str:
    """Atlassian Document Format (Jira rich text) -> plain text."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, list):
            for item in n:
                walk(item)
            return
        if not isinstance(n, dict):
            return
        kind = n.get("type")
        attrs = n.get("attrs") or {}
        if kind == "text":
            out.append(n.get("text", ""))
        elif kind == "hardBreak":
            out.append("\n")
        elif kind in ("mention", "emoji"):
            out.append(attrs.get("text", ""))
        elif kind in ("inlineCard", "blockCard"):
            out.append(attrs.get("url", ""))
        elif kind == "listItem":
            out.append("\n- ")
            inline(n.get("content", []))
        elif kind == "tableRow":
            out.append("\n| ")
            for cell in n.get("content", []):
                inline(cell.get("content", []))
                out.append(" | ")
        elif kind in ("media", "mediaSingle", "mediaGroup"):
            return
        elif kind in ("paragraph", "heading", "codeBlock", "blockquote", "panel", "rule",
                      "bulletList", "orderedList", "table"):
            out.append("\n")
            walk(n.get("content", []))
            out.append("\n")
        else:
            walk(n.get("content", []))

    def inline(nodes):
        """List items and table cells: keep their paragraphs on one line."""
        for i, child in enumerate(nodes):
            if i:
                out.append(" ")
            walk(child.get("content", []) if child.get("type") == "paragraph" else child)

    walk(node)
    text = "".join(out)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _link_text(link: dict) -> str | None:
    other, label = None, ""
    if link.get("outwardIssue"):
        other, label = link["outwardIssue"], (link.get("type") or {}).get("outward", "")
    elif link.get("inwardIssue"):
        other, label = link["inwardIssue"], (link.get("type") or {}).get("inward", "")
    if not other:
        return None
    summary = ((other.get("fields") or {}).get("summary")) or ""
    return f"{label} {other.get('key')}: {summary}".strip()


class Jira:
    def __init__(self, http: AtlassianHTTP):
        self.http = http

    def issue_url(self, key: str) -> str:
        return f"{self.http.site_url}/browse/{key}"

    def search(self, jql: str, fields: list[str], page_size: int = 100) -> Iterator[dict]:
        """Enhanced JQL search (POST /rest/api/3/search/jql, token pagination). Read-only."""
        body: dict = {"jql": jql, "fields": fields, "maxResults": page_size}
        while True:
            data = self.http.post_json("/rest/api/3/search/jql", body)
            yield from data.get("issues", [])
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return
            body["nextPageToken"] = token

    def done_issues(self, jql: str, date_field: str, since: str,
                    page_size: int = 100) -> Iterator[Issue]:
        """Issues matching jql whose date_field is >= since ('YYYY/MM/DD HH:MM')."""
        result_field = DATE_FIELDS[date_field]
        base = re.sub(r"(?i)\s+order\s+by\s+.*$", "", jql.strip())
        full = f'({base}) AND {date_field} >= "{since}" ORDER BY {date_field} ASC'
        for item in self.search(full, FIELDS + [result_field], page_size):
            f = item.get("fields") or {}
            itype = f.get("issuetype") or {}
            yield Issue(
                key=item["key"],
                url=self.issue_url(item["key"]),
                project=(f.get("project") or {}).get("key", ""),
                type=itype.get("name", ""),
                subtask=bool(itype.get("subtask")),
                status=(f.get("status") or {}).get("name", ""),
                resolution=(f.get("resolution") or {}).get("name", ""),
                done_at=f.get(result_field) or "",
                summary=f.get("summary") or "",
                description=adf_to_text(f.get("description")) if f.get("description") else "",
                fix_versions=[v.get("name", "") for v in f.get("fixVersions") or []],
                links=[t for t in map(_link_text, f.get("issuelinks") or []) if t],
            )
