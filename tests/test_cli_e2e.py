"""End-to-end: run-all --dry-run against mocked Confluence and a fake Claude CLI."""

import functools
import json
import re

import pytest
import responses

from dochealth import cli
from dochealth.db import Database

from .conftest import SITE

PAGES = [
    {"id": "1", "title": "Old API guide", "parentId": None, "authorId": "u1", "ownerId": "u1",
     "createdAt": "2019-01-01T00:00:00Z",
     "version": {"number": 3, "createdAt": "2022-01-01T00:00:00Z", "authorId": "u2"},
     "body": {"storage": {"value": "<h1>API v1</h1><p>Use the v1 endpoint.</p>"}},
     "_links": {"webui": "/spaces/TEST/pages/1/Old+API+guide"}},
    {"id": "2", "title": "Team meeting notes 2021-03", "authorId": "u2", "ownerId": "u2",
     "createdAt": "2021-03-04T00:00:00Z",
     "version": {"number": 1, "createdAt": "2021-03-04T00:00:00Z", "authorId": "u2"},
     "body": {"storage": {"value": "<p>Attendees: ...</p>"}},
     "_links": {"webui": "/spaces/TEST/pages/2"}},
    {"id": "3", "title": "Onboarding", "authorId": "u1", "ownerId": "u1",
     "createdAt": "2024-01-01T00:00:00Z",
     "version": {"number": 9, "createdAt": "2099-01-01T00:00:00Z", "authorId": "u1"},
     "body": {"storage": {"value": "<p>Welcome</p>"}},
     "_links": {"webui": "/spaces/TEST/pages/3"}},
]


def mock_confluence(analytics_status=404):
    responses.get(f"{SITE}/wiki/api/v2/spaces",
                  json={"results": [{"id": "100", "key": "TEST", "name": "Test"}]})
    responses.get(f"{SITE}/wiki/api/v2/spaces/100/pages",
                  json={"results": PAGES[:2], "_links": {"next": "/wiki/api/v2/spaces/100/pages?cursor=c2"}},
                  match=[responses.matchers.query_param_matcher(
                      {"body-format": "storage", "limit": "100", "status": "current"})])
    responses.get(f"{SITE}/wiki/api/v2/spaces/100/pages",
                  json={"results": PAGES[2:], "_links": {}},
                  match=[responses.matchers.query_param_matcher({"cursor": "c2"})])
    for p in PAGES:
        labels = [{"name": "meeting-notes"}] if p["id"] == "2" else []
        responses.get(f"{SITE}/wiki/api/v2/pages/{p['id']}/labels", json={"results": labels})
    responses.post(f"{SITE}/wiki/api/v2/users-bulk", json={"results": [
        {"accountId": "u1", "displayName": "Alice", "accountStatus": "inactive"},
        {"accountId": "u2", "displayName": "Bob", "accountStatus": "active"}]})
    responses.get(re.compile(rf"{SITE}/wiki/rest/api/analytics/content/\d+/views"),
                  status=analytics_status)


@pytest.fixture
def run(project, fake_claude, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli, "ClaudeCLI", functools.partial(cli.ClaudeCLI, runner=fake_claude))
    return lambda *args: cli.main(list(args) + ["--config", str(project / "config.yaml")])


@responses.activate
def test_run_all_dry_run(run, project, fake_claude):
    mock_confluence()
    assert run("run-all", "--dry-run") == 0

    # No write request ever reached Confluence.
    assert all(c.request.method == "GET" or c.request.url.endswith("/users-bulk")
               for c in responses.calls)

    dry = next((project / "output" / "dry-run").iterdir())
    meta = json.loads((dry / "report-page.json").read_text())
    assert meta["title"].startswith("Documentation Health Report – ")
    storage = (dry / "report-page.storage.html").read_text()
    assert 'ac:name="status"' in storage
    assert "Old API guide" in storage and "Team meeting notes" in storage
    assert "Onboarding" not in storage                       # recently modified
    assert "analytics are not available" in storage
    assert ">Bob<" in storage                                # inactive owner -> last modifier

    md = next((project / "output" / "reports").glob("*.md")).read_text()
    assert "## Archive triage" in md and "| LLM calls | 1 |" in md
    assert len(fake_claude.calls) == 1

    db = Database(project / "data" / "test.sqlite")
    run_row = db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    assert run_row["status"] == "ok" and run_row["dry_run"] == 1
    db.close()


@responses.activate
def test_second_run_makes_no_llm_calls(run, project, fake_claude):
    mock_confluence()
    assert run("run-all", "--dry-run") == 0
    assert run("run-all", "--dry-run") == 0
    assert len(fake_claude.calls) == 1


def test_api_key_blocks_startup(run, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert run("run-all", "--dry-run") == cli.EXIT_CONFIG
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


@responses.activate
def test_real_publish_creates_new_page_only(run, project, fake_claude):
    mock_confluence()
    responses.get(f"{SITE}/wiki/api/v2/pages",
                  match=[responses.matchers.query_param_matcher(
                      {"space-id": "100", "title": "Documentation Health", "status": "current"})],
                  json={"results": [{"id": "500"}]})
    responses.get(f"{SITE}/wiki/api/v2/pages", json={"results": []})
    responses.post(f"{SITE}/wiki/api/v2/pages",
                   json={"id": "900", "_links": {"webui": "/spaces/TEST/pages/900"}})
    assert run("run-all") == 0
    writes = [c for c in responses.calls if c.request.method in ("POST", "PUT", "DELETE")
              and not c.request.url.endswith("/users-bulk")]
    assert len(writes) == 1 and writes[0].request.method == "POST"
    body = json.loads(writes[0].request.body)
    assert body["parentId"] == "500" and body["body"]["representation"] == "storage"


@responses.activate
def test_run_all_without_phase2_deps_skips_duplicates(run, project, monkeypatch):
    mock_confluence()
    monkeypatch.setattr(cli, "phase2_available", lambda: False)
    assert run("run-all", "--dry-run") == 0
    md = next((project / "output" / "reports").glob("*.md")).read_text()
    assert "## Duplicates and contradictions\n\n_Not analyzed in this run._" in md
    assert run("duplicates") == cli.EXIT_ERROR


@responses.activate
def test_run_all_with_jira_dry_run_never_writes(run, project):
    mock_confluence()
    responses.post(f"{SITE}/rest/api/3/search/jql", json={"issues": [{"key": "CDP-1", "fields": {
        "summary": "Use the v2 endpoint", "issuetype": {"name": "Story", "subtask": False},
        "status": {"name": "Done"}, "statuscategorychangedate": "2026-10-26T10:00:00.000+0100"}}],
        "isLast": True})
    assert run("run-all", "--dry-run", "--jql", "project = CDP") == 0
    writes = [c for c in responses.calls if c.request.method != "GET"
              and not c.request.url.endswith(("/users-bulk", "/rest/api/3/search/jql"))]
    assert writes == []
    md = next((project / "output" / "reports").glob("*.md")).read_text()
    assert "| Handled in this run | 1 |" in md and "| No related documentation | 1 |" in md


def test_changes_needs_jql(run, project):
    assert run("changes", "--dry-run") == cli.EXIT_ERROR
