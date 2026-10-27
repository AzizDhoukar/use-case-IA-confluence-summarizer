from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dochealth.db import Database

SITE = "https://example.atlassian.net"


class FakeClaude:
    """Stands in for subprocess.run of `claude -p`; returns queued or computed answers."""

    def __init__(self, answer=None):
        self.calls: list[dict] = []
        self.queue: list = []
        self.answer = answer or (lambda prompt: {"category": "obsolete", "confidence": 0.9,
                                                 "reason": "Describes a retired API."})

    def envelope(self, structured=None, result=None, is_error=False, api_error_status=None):
        return json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                           "api_error_status": api_error_status,
                           "result": result if result is not None else json.dumps(structured),
                           "structured_output": structured})

    def __call__(self, cmd, input=None, **kwargs):
        self.calls.append({"cmd": cmd, "input": input, "kwargs": kwargs})
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, subprocess.CompletedProcess):
                return item
            stdout = item
        else:
            stdout = self.envelope(self.answer(input))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


@pytest.fixture
def fake_claude():
    return FakeClaude()


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ATLASSIAN_SITE_URL", "ATLASSIAN_EMAIL",
                "ATLASSIAN_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)


CONFIG_YAML = """
spaces: [TEST]
exclude_labels: [confidential]
database: data/test.sqlite
output_dir: output
report:
  space: TEST
  parent_title: "Documentation Health"
llm:
  max_calls_per_run: 10
triage:
  stale_modified_days: 540
  unread_days: 365
  use_analytics: auto
  historical_labels: [meeting-notes]
  historical_title_patterns: ['(?i)\\bmeeting notes\\b']
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"ATLASSIAN_SITE_URL={SITE}\nATLASSIAN_EMAIL=me@example.com\n"
        "ATLASSIAN_API_TOKEN=secret\n", encoding="utf-8")
    return tmp_path
