"""Prompt templates live in /prompts as Markdown files with a small YAML front matter.

    ---
    task: triage
    version: 1
    ---
    Prompt text with $placeholders (string.Template syntax, so JSON braces are safe).

Bump `version` whenever you change a prompt: it is part of the cache key, so results
produced by the old prompt are recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


@dataclass
class PromptTemplate:
    task: str
    version: str
    text: str

    def render(self, **variables: str) -> str:
        return Template(self.text).substitute(**variables)


def load_prompt(task: str, prompts_dir: Path | None = None) -> PromptTemplate:
    path = (prompts_dir or PROMPTS_DIR) / f"{task}.md"
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path}: missing front matter")
    _, front, body = raw.split("---", 2)
    meta = yaml.safe_load(front) or {}
    return PromptTemplate(task=meta.get("task", task), version=str(meta["version"]),
                          text=body.strip() + "\n")
