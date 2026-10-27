"""Confluence Cloud client.

Deliberately read-mostly: the only write operations are creating a new page (the report)
and adding footer comments. There is no code to edit, move, archive or delete pages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator

from .http import AtlassianError, AtlassianHTTP

log = logging.getLogger(__name__)

# Statuses meaning "analytics not available on this site/plan" rather than a real error.
ANALYTICS_UNAVAILABLE = (400, 403, 404, 405, 501)


@dataclass
class Page:
    id: str
    space_key: str
    space_id: str
    title: str
    url: str
    parent_id: str | None
    author_id: str | None
    owner_id: str | None
    last_modifier_id: str | None
    created_at: str | None
    modified_at: str | None
    version: int
    body_storage: str
    labels: list[str] = field(default_factory=list)


class Confluence:
    def __init__(self, http: AtlassianHTTP):
        self.http = http

    @property
    def wiki_base(self) -> str:
        return self.http.site_url + "/wiki"

    # --- reading ---------------------------------------------------------
    def get_space(self, key: str) -> dict:
        data = self.http.get_json("/wiki/api/v2/spaces", {"keys": key})
        results = data.get("results", [])
        if not results:
            raise AtlassianError(f"Space '{key}' not found or not accessible with this token."
                                 + self._space_hint(key))
        return results[0]

    def _space_hint(self, wanted: str) -> str:
        """Suggest the right key when given a space name or ID (or a key without its '~')."""
        w = wanted.strip().lower()
        try:
            for s in self.http.paginate("/wiki/api/v2/spaces", {"limit": 250}):
                key, name = s.get("key", ""), s.get("name", "")
                if w in (name.lower(), str(s.get("id")), key.lstrip("~").lower()):
                    return f" Did you mean key '{key}' ({name})? Space keys must be quoted in YAML."
        except AtlassianError:
            pass
        return " Use the space key (see the space URL: /wiki/spaces/<KEY>/...)."

    def _to_page(self, item: dict, space_key: str) -> Page:
        version = item.get("version") or {}
        body = ((item.get("body") or {}).get("storage") or {}).get("value", "")
        webui = (item.get("_links") or {}).get("webui", "")
        return Page(
            id=str(item["id"]),
            space_key=space_key,
            space_id=str(item.get("spaceId", "")),
            title=item.get("title", ""),
            url=self.wiki_base + webui if webui else "",
            parent_id=str(item["parentId"]) if item.get("parentId") else None,
            author_id=item.get("authorId"),
            owner_id=item.get("ownerId"),
            last_modifier_id=version.get("authorId"),
            created_at=item.get("createdAt"),
            modified_at=version.get("createdAt"),
            version=int(version.get("number", 0)),
            body_storage=body,
        )

    def iter_pages(self, space_key: str, space_id: str, page_size: int = 100) -> Iterator[Page]:
        params = {"body-format": "storage", "limit": page_size, "status": "current"}
        for item in self.http.paginate(f"/wiki/api/v2/spaces/{space_id}/pages", params):
            page = self._to_page(item, space_key)
            page.space_id = page.space_id or str(space_id)
            yield page

    def search_versions(self, cql: str, page_size: int = 100) -> Iterator[dict]:
        """CQL search returning only {id, version, labels} (no bodies): cheap listing."""
        params = {"cql": cql, "limit": page_size, "expand": "version,metadata.labels"}
        for item in self.http.paginate("/wiki/rest/api/content/search", params):
            labels = (((item.get("metadata") or {}).get("labels") or {}).get("results") or [])
            yield {"id": str(item["id"]),
                   "version": int((item.get("version") or {}).get("number", 0)),
                   "labels": [lab["name"] for lab in labels]}

    def get_pages(self, ids: list[str], space_key: str, batch: int = 50) -> Iterator[Page]:
        """Fetch pages (with storage bodies) by ID, in batches."""
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            params = {"id": ",".join(chunk), "body-format": "storage", "limit": len(chunk)}
            for item in self.http.paginate("/wiki/api/v2/pages", params):
                yield self._to_page(item, space_key)

    def resolve_page_id(self, space_id: str, ref: str) -> str:
        """Accept a page ID or an exact page title in the space."""
        if ref.isdigit():
            return ref
        page = self.find_page(space_id, ref)
        if not page:
            raise AtlassianError(f"Page titled '{ref}' not found in space (id {space_id})")
        return str(page["id"])

    def get_labels(self, page_id: str) -> list[str]:
        return [lab["name"] for lab in
                self.http.paginate(f"/wiki/api/v2/pages/{page_id}/labels", {"limit": 250})]

    def get_users(self, account_ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(account_ids), 50):
            data = self.http.post_json("/wiki/api/v2/users-bulk",
                                       {"accountIds": account_ids[i:i + 50]})
            out.extend(data.get("results", []))
        return out

    def page_views(self, page_id: str, from_date: str) -> int | None:
        """Total views since from_date (YYYY-MM-DD), or None if analytics is unavailable."""
        resp = self.http.request("GET", f"/wiki/rest/api/analytics/content/{page_id}/views",
                                 params={"fromDate": from_date},
                                 ok_statuses=ANALYTICS_UNAVAILABLE)
        if resp.status_code >= 400:
            log.info("Analytics unavailable (HTTP %s)", resp.status_code)
            return None
        return int(resp.json().get("count", 0))

    def find_page(self, space_id: str, title: str) -> dict | None:
        data = self.http.get_json("/wiki/api/v2/pages",
                                  {"space-id": space_id, "title": title, "status": "current"})
        results = data.get("results", [])
        return results[0] if results else None

    # --- writing (new content only) ---------------------------------------
    def create_page(self, space_id: str, parent_id: str | None, title: str, storage: str) -> dict:
        body = {"spaceId": space_id, "status": "current", "title": title,
                "body": {"representation": "storage", "value": storage}}
        if parent_id:
            body["parentId"] = parent_id
        page = self.http.post_json("/wiki/api/v2/pages", body)
        webui = (page.get("_links") or {}).get("webui", "")
        page["url"] = self.wiki_base + webui if webui else ""
        return page

    def create_footer_comment(self, page_id: str, storage: str) -> dict:
        """Add a new footer comment to a page (the page itself is not modified)."""
        return self.http.post_json("/wiki/api/v2/footer-comments", {
            "pageId": page_id, "body": {"representation": "storage", "value": storage}})
