"""SQLite storage: page inventory, LLM cache, triage results, run history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    dry_run     INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
    stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id               TEXT PRIMARY KEY,
    space_key        TEXT NOT NULL,
    space_id         TEXT,
    title            TEXT NOT NULL,
    url              TEXT,
    parent_id        TEXT,
    author_id        TEXT,
    owner_id         TEXT,
    last_modifier_id TEXT,
    created_at       TEXT,
    modified_at      TEXT,
    version          INTEGER,
    labels_json      TEXT NOT NULL DEFAULT '[]',
    body_text        TEXT NOT NULL DEFAULT '',
    content_hash     TEXT,
    views_count      INTEGER,          -- views since views_since (NULL = unknown)
    views_since      TEXT,
    views_checked_at TEXT,
    last_seen_run    INTEGER,
    active           INTEGER NOT NULL DEFAULT 1  -- 0 = no longer found in Confluence
);
CREATE INDEX IF NOT EXISTS idx_pages_space ON pages(space_key);

CREATE TABLE IF NOT EXISTS users (
    account_id     TEXT PRIMARY KEY,
    display_name   TEXT,
    account_status TEXT,
    fetched_at     TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key            TEXT PRIMARY KEY,
    task           TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    response_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triage (
    page_id        TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT,
    source         TEXT NOT NULL,   -- llm | rule
    category       TEXT NOT NULL,
    confidence     REAL NOT NULL,
    reason         TEXT,
    run_id         INTEGER,
    classified_at  TEXT NOT NULL
);

-- Phase 2: heading-aware chunks and their embeddings (float32 blobs, L2-normalized).
CREATE TABLE IF NOT EXISTS chunks (
    page_id      TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    heading      TEXT,
    text         TEXT NOT NULL,
    content_hash TEXT NOT NULL,   -- page content hash the chunk was built from
    model        TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    PRIMARY KEY (page_id, idx)
);

-- Phase 2: clusters of similar pages; signature = hash of (page id, content hash) pairs.
CREATE TABLE IF NOT EXISTS clusters (
    signature      TEXT PRIMARY KEY,
    page_ids_json  TEXT NOT NULL,
    similarity     REAL,
    status         TEXT NOT NULL,   -- pending | analyzed | failed
    prompt_version TEXT,
    model          TEXT,
    result_json    TEXT,
    analyzed_at    TEXT,
    last_seen_run  INTEGER
);

-- Phase 3: Jira issues done since the last run, processed once each.
CREATE TABLE IF NOT EXISTS jira_issues (
    key            TEXT PRIMARY KEY,
    done_at        TEXT,
    data_json      TEXT NOT NULL,
    status         TEXT NOT NULL,   -- pending | done | skipped | no_match | failed
    reason         TEXT,
    fetched_run    INTEGER,
    processed_run  INTEGER
);

-- Phase 3: suggested edits. fingerprint = hash(page, current text, proposed text):
-- the same suggestion is never posted twice, whichever issue produced it.
CREATE TABLE IF NOT EXISTS suggestions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint    TEXT NOT NULL UNIQUE,
    issue_key      TEXT NOT NULL,
    page_id        TEXT NOT NULL,
    confidence     REAL NOT NULL,
    similarity     REAL,
    current_text   TEXT NOT NULL,
    proposed_text  TEXT NOT NULL,
    explanation    TEXT,
    status         TEXT NOT NULL,   -- new | dry_run | posted | low_confidence | outdated | duplicate
    comment_id     TEXT,
    location       TEXT,            -- comment URL, or local file in dry-run
    created_run    INTEGER,
    last_run       INTEGER,
    posted_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_suggestions_page ON suggestions(page_id, issue_key);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def commit(self) -> None:
        self.conn.commit()

    # --- meta ------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.commit()

    # --- runs ------------------------------------------------------------
    def start_run(self, command: str, dry_run: bool) -> int:
        cur = self.execute("INSERT INTO runs(command, dry_run, started_at) VALUES(?, ?, ?)",
                           (command, int(dry_run), utcnow()))
        self.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict[str, Any]) -> None:
        self.execute("UPDATE runs SET finished_at = ?, status = ?, stats_json = ? WHERE id = ?",
                     (utcnow(), status, json.dumps(stats), run_id))
        self.commit()

    # --- llm cache -------------------------------------------------------
    def cache_get(self, key: str) -> dict | None:
        row = self.one("SELECT response_json FROM llm_cache WHERE key = ?", (key,))
        return json.loads(row["response_json"]) if row else None

    def cache_put(self, key: str, task: str, model: str, prompt_version: str, response: dict) -> None:
        self.execute(
            "INSERT OR REPLACE INTO llm_cache(key, task, model, prompt_version, response_json, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (key, task, model, prompt_version, json.dumps(response), utcnow()))
        self.commit()

    # --- users -----------------------------------------------------------
    def user_name(self, account_id: str | None) -> str:
        if not account_id:
            return ""
        row = self.one("SELECT display_name FROM users WHERE account_id = ?", (account_id,))
        return (row["display_name"] if row and row["display_name"] else account_id)

    def owner_account(self, owner_id: str | None, last_modifier_id: str | None) -> str | None:
        """Page owner, falling back to the last modifier when the owner is gone."""
        if owner_id:
            row = self.one("SELECT account_status FROM users WHERE account_id = ?", (owner_id,))
            if row is None or row["account_status"] in (None, "active"):
                return owner_id
        return last_modifier_id
