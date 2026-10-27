"""Turn Confluence storage format into clean text, hash and truncate it."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

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


# --- chunking ---------------------------------------------------------------

CHARS_PER_TOKEN = 4
HEADING_RE = re.compile(r"^(#{1,6}) (.+)$")


@dataclass
class Chunk:
    idx: int
    heading: str   # heading path, e.g. "Install > Linux"
    text: str


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split one oversized block at paragraph, then line/sentence, then hard boundaries."""
    if len(text) <= max_chars:
        return [text]
    for sep in ("\n\n", "\n", ". "):
        parts = text.split(sep)
        if len(parts) > 1:
            out, buf = [], ""
            for part in parts:
                candidate = f"{buf}{sep}{part}" if buf else part
                if len(candidate) <= max_chars:
                    buf = candidate
                else:
                    if buf:
                        out.append(buf)
                    buf = part
            if buf:
                out.append(buf)
            return [piece for p in out for piece in _split_long(p, max_chars)]
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def chunk_text(text: str, max_tokens: int = 500) -> list[Chunk]:
    """Heading-aware chunks of ~max_tokens: small sections are merged, big ones split."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    sections: list[tuple[str, str]] = []   # (heading path, section text incl. heading line)
    stack: list[tuple[int, str]] = []
    current: list[str] = []
    path = ""

    def flush_section():
        body = "\n".join(current).strip()
        if body:
            sections.append((path, body))

    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            flush_section()
            level, title = len(m.group(1)), m.group(2).strip()
            stack = [(lvl, t) for lvl, t in stack if lvl < level] + [(level, title)]
            path = " > ".join(t for _, t in stack)
            current = [line]
        else:
            current.append(line)
    flush_section()

    chunks: list[Chunk] = []
    buf, buf_heading = "", ""
    for heading, body in sections:
        for piece in _split_long(body, max_chars):
            if buf and len(buf) + len(piece) + 2 > max_chars:
                chunks.append(Chunk(len(chunks), buf_heading, buf))
                buf = ""
            if not buf:
                buf_heading = heading
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        chunks.append(Chunk(len(chunks), buf_heading, buf))
    return chunks
