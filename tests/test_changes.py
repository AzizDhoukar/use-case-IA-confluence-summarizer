import json
from datetime import datetime, timezone

import responses

from dochealth.atlassian.confluence import Confluence
from dochealth.atlassian.http import AtlassianHTTP
from dochealth.atlassian.jira import Issue, Jira, adf_to_text
from dochealth.config import load_config
from dochealth.llm.claude_cli import ClaudeCLI
from dochealth.phases.changes import fetch_issues, run_changes, skip_reason
from dochealth.publisher import Publisher
from dochealth.report.data import build_report_data
from dochealth.report.markdown import render_markdown
from dochealth.report.storage import AI_NOTICE, render_storage
from dochealth.text import content_hash

from .conftest import SITE

VPN = ("# Accès VPN\n\nPour se connecter au VPN, installer le client GlobalProtect puis utiliser "
       "le portail vpn.example.com avec son compte Decade.\n\nLe support VPN est assuré par "
       "l'équipe réseau. En cas de problème de connexion, vérifier d'abord la version du client "
       "puis ouvrir un ticket au support avec le message d'erreur affiché.")
PIM = ("# PIM\n\nLe PIM centralise les fiches produits: import des catalogues fournisseurs, "
       "enrichissement des attributs, publication vers les portails adhérents.") * 2
CURRENT = "utiliser le portail vpn.example.com avec son compte Decade."


def issue(key, summary, description="", subtask=False, status="LIVRE EN PRODUCTION",
          done_at="2026-10-26T10:00:00.000+0100"):
    return Issue(key=key, url=f"{SITE}/browse/{key}", project="CDP", type="Story",
                 subtask=subtask, status=status, resolution="", done_at=done_at,
                 summary=summary, description=description)


VPN_ISSUE = issue("CDP-1", "Migration du portail VPN",
                  "Le portail VPN vpn.example.com est remplacé par vpn2.example.com. "
                  "Installer le client GlobalProtect 6.")
SUBTASK = issue("CDP-2", "Développement portail VPN", subtask=True)
UNRELATED = issue("CDP-3", "Kubernetes nodes autoscaling", "Raise EKS node group max size.")


class FakeJira:
    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    def done_issues(self, jql, date_field, since, page_size=100):
        self.calls.append((jql, date_field, since))
        return list(self.issues)


def answer(prompt):
    return {"suggestions": [
        {"excerpt": "E1", "affected": True, "confidence": 0.9, "current_text": CURRENT,
         "proposed_text": "utiliser le portail vpn2.example.com avec son compte Decade.",
         "explanation": "Le portail VPN a changé d'adresse (CDP-1)."},
        {"excerpt": "E1", "affected": True, "confidence": 0.95,
         "current_text": "Texte inventé qui n'est pas dans la page.",
         "proposed_text": "x", "explanation": "hallucination"},
        {"excerpt": "E9", "affected": False, "confidence": 0.1, "current_text": "",
         "proposed_text": "", "explanation": "non concerné"},
    ]}


def add_page(db, pid, title, body, owner="u1"):
    db.execute("""INSERT INTO pages(id, space_key, title, url, owner_id, last_modifier_id,
                     modified_at, version, labels_json, body_text, content_hash, active)
                  VALUES(?, 'TEST', ?, ?, ?, 'u2', '2025-01-01', 1, '[]', ?, ?, 1)""",
               (pid, title, f"{SITE}/wiki/pages/{pid}", owner, body, content_hash(title, body)))
    db.commit()


def setup(db, project):
    cfg = load_config(project / "config.yaml")
    cfg.data["jira"]["jql"] = "project = CDP AND statusCategory = Done"
    cfg.data["changes"]["similarity_threshold"] = 0.3
    add_page(db, "1", "Accès VPN", VPN)
    add_page(db, "2", "PIM", PIM)
    return cfg


def test_adf_to_text():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Bonjour "},
                                          {"type": "mention", "attrs": {"text": "@Bob"}}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "un"}]}]}]},
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "x"}}]}]}
    assert adf_to_text(doc) == "Bonjour @Bob\n\n- un"


@responses.activate
def test_jira_search_builds_jql_and_paginates():
    page1 = {"issues": [{"key": "CDP-1", "fields": {
        "summary": "S", "issuetype": {"name": "Sous-tâche", "subtask": True},
        "status": {"name": "DONE"}, "resolution": None, "fixVersions": [{"name": "1.2"}],
        "issuelinks": [{"type": {"outward": "blocks"},
                        "outwardIssue": {"key": "CDP-9", "fields": {"summary": "Autre"}}}],
        "project": {"key": "CDP"}, "statuscategorychangedate": "2026-10-26T10:00:00.000+0100",
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Desc"}]}]}}}],
        "nextPageToken": "t2", "isLast": False}
    page2 = {"issues": [{"key": "CDP-2", "fields": {"summary": "T"}}], "isLast": True}
    responses.post(f"{SITE}/rest/api/3/search/jql", json=page1)
    responses.post(f"{SITE}/rest/api/3/search/jql", json=page2)
    jira = Jira(AtlassianHTTP(SITE, "e", "t", sleep=lambda s: None))
    issues = list(jira.done_issues("project = CDP ORDER BY created DESC",
                                   "statusCategoryChangedDate", "2026/10/01 00:00"))
    assert [i.key for i in issues] == ["CDP-1", "CDP-2"]
    first = json.loads(responses.calls[0].request.body)
    assert first["jql"] == ('(project = CDP) AND statusCategoryChangedDate >= "2026/10/01 00:00" '
                            "ORDER BY statusCategoryChangedDate ASC")
    assert json.loads(responses.calls[1].request.body)["nextPageToken"] == "t2"
    i = issues[0]
    assert i.subtask and i.description == "Desc" and i.fix_versions == ["1.2"]
    assert i.links == ["blocks CDP-9: Autre"] and i.url == f"{SITE}/browse/CDP-1"
    assert "Fix versions: 1.2" in i.text() and "Linked issues:" in i.text()


def test_fetch_is_incremental(db, project):
    cfg = setup(db, project)
    jira = FakeJira([VPN_ISSUE])
    stats = {"fetched": 0, "new_issues": 0}
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    fetch_issues(cfg, db, jira, 1, stats, now=now)
    assert jira.calls[0][2] == "2026/09/26 12:00"         # 30 days back, minus 1 day overlap
    assert stats["new_issues"] == 1
    db.execute("UPDATE jira_issues SET status = 'done'")
    fetch_issues(cfg, db, jira, 2, stats, now=now)
    assert jira.calls[1][2] == "2026/10/26 12:00"         # since the previous fetch
    assert stats["new_issues"] == 1                        # same issue, same done date
    jira.issues = [issue("CDP-1", "x", done_at="2026-10-27T09:00:00.000+0100")]
    fetch_issues(cfg, db, jira, 3, stats, now=now)
    assert db.one("SELECT status FROM jira_issues")["status"] == "pending"   # done again


def test_skip_rules(project):
    ccfg = load_config(project / "config.yaml")["changes"]
    ccfg["skip_statuses"] = ["Abandonnée"]
    assert skip_reason(SUBTASK.to_dict(), ccfg) == "sub-task"
    assert skip_reason(issue("X-1", "a", status="Abandonnée").to_dict(), ccfg)
    assert skip_reason(VPN_ISSUE.to_dict(), ccfg) is None


def test_dry_run_then_post_once(db, project, fake_claude, fake_encoder, tmp_path):
    cfg = setup(db, project)
    fake_claude.answer = answer
    jira = FakeJira([VPN_ISSUE, SUBTASK, UNRELATED])

    dry = Publisher(None, True, tmp_path / "out")
    stats = run_changes(cfg, db, jira, ClaudeCLI(db, runner=fake_claude), fake_encoder, dry, 1)
    assert stats["skipped"] == 1 and stats["no_match"] == 1 and stats["analyzed"] == 1
    assert stats["suggestions"] == 1 and stats["rejected"] == 1 and stats["comments"] == 1
    assert len(fake_claude.calls) == 1
    prompt = fake_claude.calls[0]["input"]
    assert '<excerpt id="E1" page="Accès VPN"' in prompt and "CDP-1" in prompt
    cmd = fake_claude.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"

    files = list((tmp_path / "out" / "dry-run").rglob("*.storage.html"))
    assert len(files) == 1
    html = files[0].read_text()
    assert 'ri:account-id="u1"' in html and AI_NOTICE in html
    assert f'href="{SITE}/browse/CDP-1"' in html and "vpn2.example.com" in html
    meta = json.loads(files[0].with_name(files[0].name.replace(".storage.html", ".json"))
                      .read_text())
    assert meta["action"] == "create_footer_comment" and meta["page_id"] == "1"

    data = build_report_data(cfg, db)
    assert [c.status for c in data.changes] == ["dry_run"]
    assert "DRY RUN" in render_storage(data)
    assert "Suggested edits (from Jira) | 1" in render_markdown(data)

    # A real run later posts the dry-run suggestion, exactly once.
    with responses.RequestsMock() as rsps:
        rsps.post(f"{SITE}/wiki/api/v2/footer-comments", json={"id": "77"})
        real = Publisher(Confluence(AtlassianHTTP(SITE, "e", "t")), False, tmp_path / "out")
        stats = run_changes(cfg, db, jira, ClaudeCLI(db, runner=fake_claude), fake_encoder,
                            real, 2)
        assert stats["comments"] == 1 and len(rsps.calls) == 1
        body = json.loads(rsps.calls[0].request.body)
        assert body["pageId"] == "1" and body["body"]["representation"] == "storage"
    row = db.one("SELECT * FROM suggestions")
    assert row["status"] == "posted" and row["comment_id"] == "77"
    assert row["location"].endswith("focusedCommentId=77#comment-77")

    # Nothing is posted again, and the issue is not sent to Claude again.
    with responses.RequestsMock() as rsps:
        stats = run_changes(cfg, db, jira, ClaudeCLI(db, runner=fake_claude), fake_encoder,
                            Publisher(Confluence(AtlassianHTTP(SITE, "e", "t")), False,
                                      tmp_path / "out"), 3)
        assert stats["comments"] == 0 and len(rsps.calls) == 0
    assert len(fake_claude.calls) == 1


def test_same_suggestion_from_another_issue_is_not_repeated(db, project, fake_claude,
                                                            fake_encoder, tmp_path):
    cfg = setup(db, project)
    fake_claude.answer = answer
    other = issue("CDP-4", "Nouveau portail VPN vpn2.example.com", VPN_ISSUE.description)
    dry = Publisher(None, True, tmp_path / "out")
    stats = run_changes(cfg, db, FakeJira([VPN_ISSUE, other]), ClaudeCLI(db, runner=fake_claude),
                        fake_encoder, dry, 1)
    assert stats["suggestions"] == 1 and stats["repeated"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM suggestions")["n"] == 1


def test_low_confidence_and_outdated_are_not_posted(db, project, fake_claude, fake_encoder,
                                                    tmp_path):
    cfg = setup(db, project)
    cfg.data["changes"]["min_confidence"] = 0.95
    fake_claude.answer = answer
    dry = Publisher(None, True, tmp_path / "out")
    stats = run_changes(cfg, db, FakeJira([VPN_ISSUE]), ClaudeCLI(db, runner=fake_claude),
                        fake_encoder, dry, 1)
    assert stats["low_confidence"] == 1 and stats["comments"] == 0

    # A confident suggestion whose text was meanwhile edited by the owner is not posted.
    db.execute("UPDATE suggestions SET status = 'new'")
    db.execute("UPDATE pages SET body_text = 'Page réécrite.' WHERE id = '1'")
    stats = run_changes(cfg, db, FakeJira([]), ClaudeCLI(db, runner=fake_claude),
                        fake_encoder, dry, 2)
    assert stats["outdated"] == 1 and stats["comments"] == 0


def test_budget_exhausted_keeps_issue_pending(db, project, fake_claude, fake_encoder, tmp_path):
    cfg = setup(db, project)
    stats = run_changes(cfg, db, FakeJira([VPN_ISSUE]), ClaudeCLI(db, max_calls=0,
                                                                  runner=fake_claude),
                        fake_encoder, Publisher(None, True, tmp_path / "out"), 1)
    assert stats["stopped"] == "budget" and stats["pending"] == 1
    assert build_report_data(cfg, db).changes_summary.issues_pending == 1


def test_historical_and_obsolete_pages_get_no_suggestions(db, project):
    from dochealth.phases.changes import target_pages
    cfg = setup(db, project)
    cfg.data["triage"]["historical_title_patterns"] = [r"(?i)\bsprint\b"]
    add_page(db, "3", "Préparation Sprint 37", VPN)
    add_page(db, "4", "Ancienne procédure VPN", VPN + " Ancienne.")
    page = db.one("SELECT content_hash FROM pages WHERE id = '4'")
    db.execute("INSERT INTO triage(page_id, content_hash, prompt_version, source, category, "
               "confidence, classified_at) VALUES('4', ?, '1', 'llm', 'obsolete', 0.9, 'now')",
               (page["content_hash"],))
    assert [p["id"] for p in target_pages(db, cfg)] == ["1", "2"]
