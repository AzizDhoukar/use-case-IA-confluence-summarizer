"""Phase 1a: fetch all pages of the configured spaces into SQLite (incremental)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ..atlassian.confluence import Confluence
from ..config import Config
from ..db import Database, utcnow
from ..text import content_hash, storage_to_text

log = logging.getLogger(__name__)

USER_REFRESH_DAYS = 30


def run_inventory(cfg: Config, db: Database, confluence: Confluence, run_id: int) -> dict:
    stats = {"spaces": 0, "pages_scanned": 0, "new": 0, "changed": 0, "unchanged": 0,
             "removed": 0, "label_requests": 0, "users_fetched": 0}
    page_size = cfg["atlassian"]["page_size"]

    for space_key in cfg.spaces:
        space = confluence.get_space(space_key)
        stats["spaces"] += 1
        log.info("Inventory: space %s (%s)", space_key, space.get("name", ""))
        for page in confluence.iter_pages(space_key, str(space["id"]), page_size):
            stats["pages_scanned"] += 1
            existing = db.one("SELECT version, labels_json, content_hash FROM pages WHERE id = ?",
                              (page.id,))
            if existing and existing["version"] == page.version:
                labels_json = existing["labels_json"]
            else:
                labels_json = json.dumps(confluence.get_labels(page.id))
                stats["label_requests"] += 1

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
        db.commit()

        cur = db.execute("UPDATE pages SET active = 0 WHERE space_key = ? AND active = 1 "
                         "AND (last_seen_run IS NULL OR last_seen_run != ?)", (space_key, run_id))
        stats["removed"] += cur.rowcount
        db.commit()

    stats["users_fetched"] = _refresh_users(db, confluence)
    log.info("Inventory done: %s", stats)
    return stats


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
