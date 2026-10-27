"""Phase 1a: fetch the pages of the configured spaces (or page subtrees) into SQLite (incremental)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ..atlassian.confluence import Confluence, Page
from ..config import Config, SpaceScope
from ..db import Database, utcnow
from ..text import content_hash, storage_to_text

log = logging.getLogger(__name__)

USER_REFRESH_DAYS = 30


def run_inventory(cfg: Config, db: Database, confluence: Confluence, run_id: int) -> dict:
    stats = {"spaces": 0, "pages_scanned": 0, "new": 0, "changed": 0, "unchanged": 0,
             "removed": 0, "label_requests": 0, "bodies_fetched": 0, "users_fetched": 0}
    page_size = cfg["atlassian"]["page_size"]

    for scope in cfg.scopes:
        space = confluence.get_space(scope.key)
        space_id = str(space["id"])
        stats["spaces"] += 1
        log.info("Inventory: %s (%s)", scope.describe(), space.get("name", ""))
        if scope.is_scoped:
            _inventory_scoped(db, confluence, scope, space_id, run_id, page_size, stats)
        else:
            for page in confluence.iter_pages(scope.key, space_id, page_size):
                stats["pages_scanned"] += 1
                stats["bodies_fetched"] += 1
                existing = db.one("SELECT version, labels_json FROM pages WHERE id = ?",
                                  (page.id,))
                if existing and existing["version"] == page.version:
                    labels_json = existing["labels_json"]
                else:
                    labels_json = json.dumps(confluence.get_labels(page.id))
                    stats["label_requests"] += 1
                _upsert(db, page, labels_json, run_id, stats)
        db.commit()

        # Pages of this space not seen in this run were deleted or are outside the selection.
        cur = db.execute("UPDATE pages SET active = 0 WHERE space_key = ? AND active = 1 "
                         "AND (last_seen_run IS NULL OR last_seen_run != ?)",
                         (scope.key, run_id))
        stats["removed"] += cur.rowcount
        db.commit()

    stats["users_fetched"] = _refresh_users(db, confluence)
    log.info("Inventory done: %s", stats)
    return stats


def build_cql(space_key: str, include_ids: list[str], exclude_ids: list[str]) -> str:
    def subtree(ids: list[str]) -> str:
        return " OR ".join(f"id = {i} OR ancestor = {i}" for i in ids)

    cql = f'space = "{space_key}" AND type = page'
    if include_ids:
        cql += f" AND ({subtree(include_ids)})"
    if exclude_ids:
        cql += f" AND NOT ({subtree(exclude_ids)})"
    return cql


def _inventory_scoped(db: Database, confluence: Confluence, scope: SpaceScope, space_id: str,
                      run_id: int, page_size: int, stats: dict) -> None:
    """List IDs + versions with CQL, then download bodies only for new or changed pages."""
    include = [confluence.resolve_page_id(space_id, ref) for ref in scope.include_pages]
    exclude = [confluence.resolve_page_id(space_id, ref) for ref in scope.exclude_pages]
    cql = build_cql(scope.key, include, exclude)
    log.debug("CQL: %s", cql)

    to_fetch: dict[str, list[str]] = {}
    for item in confluence.search_versions(cql, page_size):
        stats["pages_scanned"] += 1
        existing = db.one("SELECT version FROM pages WHERE id = ?", (item["id"],))
        if existing and existing["version"] == item["version"]:
            stats["unchanged"] += 1
            db.execute("UPDATE pages SET last_seen_run = ?, active = 1, labels_json = ? "
                       "WHERE id = ?", (run_id, json.dumps(item["labels"]), item["id"]))
        else:
            to_fetch[item["id"]] = item["labels"]
    db.commit()

    for page in confluence.get_pages(list(to_fetch), scope.key):
        stats["bodies_fetched"] += 1
        page.space_id = page.space_id or space_id
        _upsert(db, page, json.dumps(to_fetch.get(page.id, [])), run_id, stats)
    db.commit()


def _upsert(db: Database, page: Page, labels_json: str, run_id: int, stats: dict) -> None:
    existing = db.one("SELECT content_hash FROM pages WHERE id = ?", (page.id,))
    text = storage_to_text(page.body_storage)
    h = content_hash(page.title, text)
    if not existing:
        stats["new"] += 1
    elif existing["content_hash"] != h:
        stats["changed"] += 1
    else:
        stats["unchanged"] += 1

    db.execute(
        """INSERT INTO pages(id, space_key, space_id, title, url, parent_id, author_id,
               owner_id, last_modifier_id, created_at, modified_at, version, labels_json,
               body_text, content_hash, last_seen_run, active)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
           ON CONFLICT(id) DO UPDATE SET
               space_key=excluded.space_key, space_id=excluded.space_id,
               title=excluded.title, url=excluded.url, parent_id=excluded.parent_id,
               author_id=excluded.author_id, owner_id=excluded.owner_id,
               last_modifier_id=excluded.last_modifier_id,
               created_at=excluded.created_at, modified_at=excluded.modified_at,
               version=excluded.version, labels_json=excluded.labels_json,
               body_text=excluded.body_text, content_hash=excluded.content_hash,
               last_seen_run=excluded.last_seen_run, active=1""",
        (page.id, page.space_key, page.space_id, page.title, page.url, page.parent_id,
         page.author_id, page.owner_id, page.last_modifier_id, page.created_at,
         page.modified_at, page.version, labels_json, text, h, run_id))


def _refresh_users(db: Database, confluence: Confluence) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=USER_REFRESH_DAYS)).isoformat()
    rows = db.query(
        """SELECT DISTINCT uid FROM (
               SELECT owner_id AS uid FROM pages WHERE active = 1
               UNION SELECT last_modifier_id FROM pages WHERE active = 1
               UNION SELECT author_id FROM pages WHERE active = 1)
           WHERE uid IS NOT NULL
             AND uid NOT IN (SELECT account_id FROM users WHERE fetched_at >= ?)""", (cutoff,))
    ids = [r["uid"] for r in rows]
    if not ids:
        return 0
    now = utcnow()
    users = confluence.get_users(ids)
    for u in users:
        db.execute("INSERT OR REPLACE INTO users(account_id, display_name, account_status, "
                   "fetched_at) VALUES(?,?,?,?)",
                   (u.get("accountId"), u.get("displayName") or u.get("publicName"),
                    u.get("accountStatus"), now))
    db.commit()
    return len(users)
