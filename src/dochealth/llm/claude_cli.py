"""Claude access through the Claude Code CLI in headless mode (`claude -p`).

Uses the user's subscription via the CLI only: no API calls, no credential handling.
Every result is cached in SQLite, keyed by (task, prompt version, model, rendered prompt).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema

from ..db import Database
from .prompts import PromptTemplate

log = logging.getLogger(__name__)

SYSTEM_PROMPT = ("You are a documentation analysis component inside an automated tool. "
                 "Answer only with a JSON object that matches the provided schema. "
                 "No prose, no markdown fences.")

RATE_LIMIT_RE = re.compile(r"usage limit|rate limit|hit your limit|limit reached|"
                           r"too many requests|overloaded", re.I)


class LLMError(Exception):
    pass


class RateLimitReached(LLMError):
    """Subscription limit hit: stop the run gracefully, resume next time."""


class BudgetExhausted(LLMError):
    """The per-run call cap (llm.max_calls_per_run) was reached."""


@dataclass
class LLMStats:
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    invalid_retries: int = 0
    rate_limited: bool = False
    budget_exhausted: bool = False
    by_model: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


Runner = Callable[..., subprocess.CompletedProcess]


class ClaudeCLI:
    def __init__(self, db: Database, claude_bin: str = "claude", max_calls: int = 40,
                 timeout: int = 300, runner: Runner = subprocess.run):
        self.db = db
        self.claude_bin = claude_bin
        self.max_calls = max_calls
        self.timeout = timeout
        self.runner = runner
        self.stats = LLMStats()

    @staticmethod
    def cache_key(task: str, version: str, model: str, prompt: str) -> str:
        payload = json.dumps([task, version, model, prompt], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def cached(self, template: PromptTemplate, model: str, prompt: str) -> dict | None:
        return self.db.cache_get(self.cache_key(template.task, template.version, model, prompt))

    def complete(self, template: PromptTemplate, model: str, schema: dict,
                 **variables: str) -> dict:
        """Render the template, return the validated JSON answer (from cache when possible)."""
        prompt = template.render(**variables)
        key = self.cache_key(template.task, template.version, model, prompt)
        hit = self.db.cache_get(key)
        if hit is not None:
            self.stats.cache_hits += 1
            return hit

        attempt_prompt = prompt
        last_error = ""
        for attempt in range(2):  # first try + one retry on invalid output
            raw = self._call(attempt_prompt, model, schema)
            try:
                result = self._parse(raw)
                jsonschema.validate(result, schema)
            except (ValueError, jsonschema.ValidationError) as exc:
                last_error = str(exc).splitlines()[0][:300]
                log.warning("Invalid JSON from Claude (%s, attempt %d): %s",
                            template.task, attempt + 1, last_error)
                if attempt == 0:
                    self.stats.invalid_retries += 1
                    attempt_prompt = (f"{prompt}\n\nYour previous answer was invalid "
                                      f"({last_error}). Reply with ONLY a JSON object "
                                      f"matching the schema.")
                continue
            self.db.cache_put(key, template.task, model, template.version, result)
            return result

        self.stats.failures += 1
        raise LLMError(f"{template.task}: invalid output after retry: {last_error}")

    def _call(self, prompt: str, model: str, schema: dict) -> dict:
        if self.stats.calls >= self.max_calls:
            self.stats.budget_exhausted = True
            raise BudgetExhausted(f"LLM call cap reached ({self.max_calls} per run)")
        if os.environ.get("ANTHROPIC_API_KEY"):  # belt and braces; checked at startup too
            raise LLMError("ANTHROPIC_API_KEY is set; refusing to call Claude")

        cmd = [self.claude_bin, "-p",
               "--output-format", "json",
               "--model", model,
               "--json-schema", json.dumps(schema),
               "--system-prompt", SYSTEM_PROMPT,
               "--no-session-persistence",
               "--strict-mcp-config",
               "--tools", ""]
        self.stats.calls += 1
        self.stats.by_model[model] = self.stats.by_model.get(model, 0) + 1
        # Run from an empty temp dir so no project files / CLAUDE.md are picked up.
        with tempfile.TemporaryDirectory(prefix="dochealth-") as cwd:
            try:
                proc = self.runner(cmd, input=prompt, capture_output=True, text=True,
                                   timeout=self.timeout, cwd=cwd)
            except FileNotFoundError as exc:
                raise LLMError(f"Claude CLI not found ('{self.claude_bin}'). Install Claude "
                               f"Code or set llm.claude_bin in config.yaml") from exc
            except subprocess.TimeoutExpired as exc:
                self.stats.failures += 1
                raise LLMError(f"Claude CLI timed out after {self.timeout}s") from exc

        envelope: dict | None = None
        try:
            envelope = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            envelope = None

        error_text = " ".join(filter(None, [
            proc.stderr or "",
            str((envelope or {}).get("result", "")) if (envelope or {}).get("is_error") else "",
        ]))
        api_status = (envelope or {}).get("api_error_status")
        if api_status == 429 or ((proc.returncode != 0 or (envelope or {}).get("is_error"))
                                 and RATE_LIMIT_RE.search(error_text)):
            self.stats.rate_limited = True
            raise RateLimitReached(f"Claude usage limit reached: {error_text.strip()[:300]}")
        if envelope is None or proc.returncode != 0 or envelope.get("is_error"):
            self.stats.failures += 1
            raise LLMError(f"Claude CLI failed (exit {proc.returncode}): "
                           f"{(error_text or proc.stdout).strip()[:500]}")
        return envelope

    @staticmethod
    def _parse(envelope: dict) -> dict:
        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            return structured
        text = str(envelope.get("result", "")).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object in output")
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"unparseable JSON: {exc}") from exc
