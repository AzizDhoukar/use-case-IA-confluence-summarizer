"""Turn Confluence storage format into clean text, hash and truncate it."""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup, CData, Comment, NavigableString, Tag

BLOCK_TAGS = {"p", "div", "br", "pre", "blockquote", "table", "ul", "ol", "hr",
              "ac:layout", "ac:layout-section", "ac:layout-cell", "ac:task-list"}
SKIP_TAGS = {"ac:parameter", "ac:image", "ri:attachment", "style", "script"}
HEADINGS = {f"h{i}": i for i in range(1, 7)}


def _walk(node, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, (NavigableString, CData)):
            out.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in SKIP_TAGS:
            continue
        if name in HEADINGS:
            text = " ".join(child.get_text(" ").split())
            if text:
                out.append(f"\n\n{'#' * HEADINGS[name]} {text}\n\n")
            continue
        if name == "ri:user":
            out.append("@user")
            continue
        if name == "ac:structured-macro":
            macro = child.get("ac:name", "")
            if macro == "status":
                title = child.find("ac:parameter", attrs={"ac:name": "title"})
                if title:
                    out.append(f"[{title.get_text().strip()}]")
                continue
            if macro in ("toc", "children", "pagetree", "attachments", "recently-updated",
                         "contentbylabel", "jira"):
                continue
        if name == "li":
            out.append("\n- ")
            _walk(child, out)
            continue
        if name == "tr":
            cells = [" ".join(c.get_text(" ").split()) for c in child.find_all(["td", "th"],
                                                                                 recursive=False)]
            out.append("\n| " + " | ".join(cells) + " |")
            continue
        if name in BLOCK_TAGS:
            out.append("\n")
            _walk(child, out)
            out.append("\n")
            continue
        _walk(child, out)


def storage_to_text(storage: str) -> str:
    """Readable plain text with markdown-style '#' headings (used later for chunking)."""
    if not storage:
        return ""
    soup = BeautifulSoup(storage, "html.parser")
    parts: list[str] = []
    _walk(soup, parts)
    text = "".join(parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def content_hash(title: str, text: str) -> str:
    return hashlib.sha256(f"{title}\n{text}".encode("utf-8")).hexdigest()


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Prefer cutting at a paragraph or line boundary.
    for sep in ("\n\n", "\n"):
        idx = cut.rfind(sep)
        if idx > max_chars * 0.7:
            cut = cut[:idx]
            break
    return f"{cut}\n\n[... truncated, {len(text) - len(cut)} more characters ...]"
