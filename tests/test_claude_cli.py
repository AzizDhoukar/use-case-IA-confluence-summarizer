import subprocess

import pytest

from dochealth.llm import schemas
from dochealth.llm.claude_cli import (BudgetExhausted, ClaudeCLI, LLMError, RateLimitReached)
from dochealth.llm.prompts import PromptTemplate, load_prompt

TPL = PromptTemplate(task="triage", version="1", text="Classify $title\n")
GOOD = {"category": "relevant", "confidence": 0.8, "reason": "Still used."}


def make(db, fake, max_calls=10):
    return ClaudeCLI(db, "claude", max_calls=max_calls, timeout=5, runner=fake)


def test_command_line_and_structured_output(db, fake_claude):
    fake_claude.answer = lambda p: GOOD
    llm = make(db, fake_claude)
    assert llm.complete(TPL, "haiku", schemas.TRIAGE, title="Page A") == GOOD
    call = fake_claude.calls[0]
    cmd = call["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--json-schema" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert call["input"] == "Classify Page A\n"          # prompt goes through stdin
    assert call["kwargs"]["cwd"]                          # isolated temp dir


def test_cache_hit_avoids_second_call(db, fake_claude):
    fake_claude.answer = lambda p: GOOD
    llm = make(db, fake_claude)
    llm.complete(TPL, "haiku", schemas.TRIAGE, title="A")
    llm.complete(TPL, "haiku", schemas.TRIAGE, title="A")
    assert len(fake_claude.calls) == 1 and llm.stats.cache_hits == 1
    # A new prompt version or model invalidates the cache.
    llm.complete(PromptTemplate("triage", "2", TPL.text), "haiku", schemas.TRIAGE, title="A")
    llm.complete(TPL, "sonnet", schemas.TRIAGE, title="A")
    assert len(fake_claude.calls) == 3


def test_invalid_json_is_retried_once(db, fake_claude):
    fake_claude.queue = [fake_claude.envelope(None, result="Sure! here you go"),
                         fake_claude.envelope(GOOD)]
    llm = make(db, fake_claude)
    assert llm.complete(TPL, "haiku", schemas.TRIAGE, title="A") == GOOD
    assert llm.stats.calls == 2 and llm.stats.invalid_retries == 1
    assert "previous answer was invalid" in fake_claude.calls[1]["input"]


def test_schema_violation_twice_fails(db, fake_claude):
    bad = {"category": "maybe", "confidence": 2, "reason": "x"}
    fake_claude.queue = [fake_claude.envelope(bad), fake_claude.envelope(bad)]
    llm = make(db, fake_claude)
    with pytest.raises(LLMError):
        llm.complete(TPL, "haiku", schemas.TRIAGE, title="A")
    assert llm.stats.failures == 1


def test_result_text_fallback_with_fences(db, fake_claude):
    fake_claude.queue = [fake_claude.envelope(
        None, result='```json\n{"category": "obsolete", "confidence": 0.7, "reason": "Old."}\n```')]
    assert make(db, fake_claude).complete(TPL, "haiku", schemas.TRIAGE, title="A")["category"] \
        == "obsolete"


def test_rate_limit_detected(db, fake_claude):
    fake_claude.queue = [subprocess.CompletedProcess(
        [], 1, stdout=fake_claude.envelope(None, result="Claude AI usage limit reached|1700000000",
                                           is_error=True), stderr="")]
    llm = make(db, fake_claude)
    with pytest.raises(RateLimitReached):
        llm.complete(TPL, "haiku", schemas.TRIAGE, title="A")
    assert llm.stats.rate_limited


def test_budget_cap(db, fake_claude):
    fake_claude.answer = lambda p: GOOD
    llm = make(db, fake_claude, max_calls=1)
    llm.complete(TPL, "haiku", schemas.TRIAGE, title="A")
    with pytest.raises(BudgetExhausted):
        llm.complete(TPL, "haiku", schemas.TRIAGE, title="B")
    assert len(fake_claude.calls) == 1


def test_refuses_when_api_key_set(db, fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    with pytest.raises(LLMError):
        make(db, fake_claude).complete(TPL, "haiku", schemas.TRIAGE, title="A")
    assert fake_claude.calls == []


def test_shipped_triage_prompt_renders():
    tpl = load_prompt("triage")
    out = tpl.render(title="T", space="S", labels="none", created="2020-01-01",
                     modified="2021-01-01", last_modifier="Bob", views="unknown",
                     similar_titles="none", content="Body")
    assert "Body" in out and "$" not in out.replace("$title", "")
