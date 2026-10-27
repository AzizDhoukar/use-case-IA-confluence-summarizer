"""Configuration loading (config.yaml + .env) and startup guards."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


DEFAULTS: dict[str, Any] = {
    "spaces": [],
    "exclude_spaces": [],
    "exclude_labels": [],
    "database": "data/dochealth.sqlite",
    "output_dir": "output",
    "report": {
        "space": None,
        "parent_id": None,
        "parent_title": None,
        "max_rows_per_category": 200,
    },
    "llm": {
        "claude_bin": "claude",
        "max_calls_per_run": 40,
        "timeout_seconds": 300,
        "models": {"classify": "haiku", "compare": "sonnet", "draft": "sonnet"},
    },
    "atlassian": {"page_size": 100, "max_retries": 5},
    "triage": {
        "stale_modified_days": 540,
        "unread_days": 365,
        "use_analytics": "auto",
        "analytics_refresh_days": 7,
        "max_chars": 6000,
        "historical_labels": [],
        "historical_title_patterns": [],
    },
    "embeddings": {
        "model": "paraphrase-multilingual-MiniLM-L12-v2",
        "chunk_tokens": 500,
        "batch_size": 32,
    },
    "duplicates": {
        "threshold": 0.85,
        "max_cluster_size": 5,
        "min_chars": 300,
        "max_chars_per_page": 4000,
        "tune_thresholds": [0.95, 0.9, 0.85, 0.8, 0.75, 0.7],
    },
}


def check_no_api_key(environ: dict[str, str] | None = None) -> None:
    """The tool must only use the Claude subscription via the CLI, never an API key."""
    environ = os.environ if environ is None else environ
    if environ.get("ANTHROPIC_API_KEY"):
        raise ConfigError(
            "ANTHROPIC_API_KEY is set in the environment. This tool only uses Claude through "
            "your Claude Code subscription (claude -p) and refuses to run with an API key, which "
            "would bill pay-as-you-go usage. Unset it (and remove it from .env) and retry."
        )


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class SpaceScope:
    """One space to analyze, optionally narrowed to page subtrees.

    include_pages / exclude_pages hold page IDs or exact titles; each selects that page
    and all its descendants. No include_pages means the whole space.
    """
    key: str
    include_pages: list[str] = field(default_factory=list)
    exclude_pages: list[str] = field(default_factory=list)

    @property
    def is_scoped(self) -> bool:
        return bool(self.include_pages or self.exclude_pages)

    def describe(self) -> str:
        parts = []
        if self.include_pages:
            parts.append("only " + ", ".join(self.include_pages))
        if self.exclude_pages:
            parts.append("excluding " + ", ".join(self.exclude_pages))
        return f"{self.key} ({'; '.join(parts)})" if parts else self.key


def parse_scopes(raw: list) -> list[SpaceScope]:
    scopes = []
    for item in raw or []:
        if isinstance(item, str):
            scopes.append(SpaceScope(item))
        elif isinstance(item, dict) and item.get("key"):
            scopes.append(SpaceScope(
                str(item["key"]),
                [str(p) for p in item.get("include_pages") or []],
                [str(p) for p in item.get("exclude_pages") or []]))
        else:
            raise ConfigError(f"config: invalid entry in 'spaces': {item!r} "
                              "(use a space key or {key: ..., include_pages: [...]})")
    return scopes


@dataclass
class Credentials:
    site_url: str
    email: str
    api_token: str


@dataclass
class Config:
    data: dict[str, Any]
    base_dir: Path
    credentials: Credentials | None = field(default=None)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def path(self, key: str) -> Path:
        p = Path(self.data[key])
        return p if p.is_absolute() else self.base_dir / p

    @property
    def scopes(self) -> list[SpaceScope]:
        return [s for s in parse_scopes(self.data["spaces"])
                if s.key not in self.data["exclude_spaces"]]

    @property
    def spaces(self) -> list[str]:
        return [s.key for s in self.scopes]


def load_config(config_path: str | Path, env_path: str | Path | None = None,
                require_credentials: bool = True) -> Config:
    config_path = Path(config_path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path} (copy config.example.yaml)")
    base_dir = config_path.resolve().parent

    env_file = Path(env_path) if env_path else base_dir / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)
    # Checked after loading .env, so a key placed there is caught too.
    check_no_api_key()

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    data = _merge(DEFAULTS, raw)

    if not data["spaces"]:
        raise ConfigError("config: 'spaces' must list at least one space key")
    parse_scopes(data["spaces"])  # validate early
    if data["triage"]["use_analytics"] not in ("auto", True, False):
        raise ConfigError("config: triage.use_analytics must be auto, true or false")

    creds = None
    site = os.environ.get("ATLASSIAN_SITE_URL", "").rstrip("/")
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    if site and email and token:
        if site.endswith("/wiki"):
            site = site[: -len("/wiki")]
        creds = Credentials(site, email, token)
    elif require_credentials:
        raise ConfigError(
            "Missing Atlassian credentials: set ATLASSIAN_SITE_URL, ATLASSIAN_EMAIL and "
            "ATLASSIAN_API_TOKEN in .env (see .env.example)"
        )
    return Config(data=data, base_dir=base_dir, credentials=creds)
