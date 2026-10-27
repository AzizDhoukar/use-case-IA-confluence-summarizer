"""Single gateway for every write to Confluence. With dry_run, writes go to local files."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .atlassian.confluence import Confluence
from .atlassian.http import AtlassianError

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, confluence: Confluence | None, dry_run: bool, output_dir: Path,
                 stamp: str | None = None):
        if not dry_run and confluence is None:
            raise ValueError("A Confluence client is required unless dry_run is set")
        self.confluence = confluence
        self.dry_run = dry_run
        self.stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dry_dir = Path(output_dir) / "dry-run" / self.stamp

    def _write(self, name: str, content: str) -> Path:
        path = self.dry_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def publish_report(self, space_key: str, parent_id: str | None, parent_title: str | None,
                       title: str, storage: str) -> str:
        """Create the report as a new page. Returns its URL (or local path in dry-run)."""
        if self.dry_run:
            self._write("report-page.json", json.dumps(
                {"action": "create_page", "space": space_key, "parent_id": parent_id,
                 "parent_title": parent_title, "title": title}, indent=2, ensure_ascii=False))
            path = self._write("report-page.storage.html", storage)
            log.info("[dry-run] report page written to %s", path)
            return str(path)

        assert self.confluence is not None
        space = self.confluence.get_space(space_key)
        space_id = str(space["id"])
        if not parent_id and parent_title:
            parent = self.confluence.find_page(space_id, parent_title)
            if not parent:
                raise AtlassianError(f"Report parent page '{parent_title}' not found in "
                                     f"space {space_key}")
            parent_id = str(parent["id"])
        # Never touch an existing page: pick a fresh title if today's report already exists.
        final_title = title
        if self.confluence.find_page(space_id, final_title):
            final_title = f"{title} ({datetime.now().strftime('%H:%M:%S')})"
        page = self.confluence.create_page(space_id, parent_id, final_title, storage)
        log.info("Report page created: %s", page.get("url"))
        return page.get("url") or str(page.get("id"))

    def post_comment(self, page_id: str, page_url: str, name: str, storage: str,
                     meta: dict) -> tuple[str | None, str]:
        """Add a footer comment. Returns (comment id, URL), or (None, local path) in dry-run."""
        if self.dry_run:
            self._write(f"comments/{name}.json", json.dumps(
                {"action": "create_footer_comment", "page_id": page_id, **meta},
                indent=2, ensure_ascii=False))
            path = self._write(f"comments/{name}.storage.html", storage)
            log.info("[dry-run] comment for page %s written to %s", page_id, path)
            return None, str(path)

        assert self.confluence is not None
        comment = self.confluence.create_footer_comment(page_id, storage)
        cid = str(comment.get("id", ""))
        url = f"{page_url}?focusedCommentId={cid}#comment-{cid}" if page_url and cid else page_url
        log.info("Comment %s posted on page %s", cid, page_id)
        return cid, url
