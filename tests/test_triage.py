import json

from dochealth.config import load_config
from dochealth.llm.claude_cli import ClaudeCLI
from dochealth.phases.triage import pending_count, run_triage
from dochealth.text import content_hash


def add_page(db, pid, title, modified, labels=(), body="Some content", views=None):
    db.execute(
        """INSERT INTO pages(id, space_key, space_id, title, url, modified_at, created_at, version,
               labels_json, body_text, content_hash, views_count, active)
           VALUES(?, 'TEST', '100', ?, ?, ?, '2019-01-01T00:00:00Z', 1, ?, ?, ?, ?, 1)""",
        (pid, title, f"https://x/wiki/{pid}", modified, json.dumps(list(labels)), body,
         content_hash(title, body), views))
    db.commit()


def setup(db, project):
    cfg = load_config(project / "config.yaml")
    add_page(db, "1", "Old API guide", "2022-01-01T00:00:00Z")
    add_page(db, "2", "Team meeting notes 2021-03", "2021-03-04T00:00:00Z")
    add_page(db, "3", "Onboarding", "2099-01-01T00:00:00Z")            # recently modified
    add_page(db, "4", "Secret plan", "2020-01-01T00:00:00Z", labels=["confidential"])
    return cfg


def test_prefilter_rules_and_classification(db, project, fake_claude):
    cfg = setup(db, project)
    llm = ClaudeCLI(db, max_calls=10, runner=fake_claude)
    stats = run_triage(cfg, db, None, llm, run_id=1)

    assert stats["date_stale"] == 3             # 1, 2, 4
    assert stats["classified_rule"] == 1        # meeting notes, no LLM call
    assert stats["excluded"] == 1               # confidential never sent to Claude
    assert stats["classified_llm"] == 1 and len(fake_claude.calls) == 1
    assert "Old API guide" in fake_claude.calls[0]["input"]
    rows = {r["page_id"]: r for r in db.query("SELECT * FROM triage")}
    assert rows["1"]["category"] == "obsolete" and rows["1"]["source"] == "llm"
    assert rows["2"]["category"] == "historical_record" and rows["2"]["source"] == "rule"
    assert "3" not in rows and "4" not in rows


def test_second_run_is_incremental(db, project, fake_claude):
    cfg = setup(db, project)
    run_triage(cfg, db, None, ClaudeCLI(db, runner=fake_claude), run_id=1)
    stats = run_triage(cfg, db, None, ClaudeCLI(db, runner=fake_claude), run_id=2)
    assert len(fake_claude.calls) == 1 and stats["unchanged"] == 2

    # Changing the content triggers a re-classification.
    new_hash = content_hash("Old API guide", "new body")
    db.execute("UPDATE pages SET body_text='new body', content_hash=? WHERE id='1'", (new_hash,))
    run_triage(cfg, db, None, ClaudeCLI(db, runner=fake_claude), run_id=3)
    assert len(fake_claude.calls) == 2


def test_budget_stop_leaves_pending_for_next_run(db, project, fake_claude):
    cfg = setup(db, project)
    add_page(db, "5", "Legacy batch jobs", "2021-06-01T00:00:00Z")
    stats = run_triage(cfg, db, None, ClaudeCLI(db, max_calls=1, runner=fake_claude), run_id=1)
    assert stats["stopped"] == "budget" and stats["pending"] == 1
    stats = run_triage(cfg, db, None, ClaudeCLI(db, max_calls=1, runner=fake_claude), run_id=2)
    assert stats["stopped"] is None and stats["pending"] == 0
    assert pending_count(db, cfg, "1") == 0


class FakeAnalytics:
    def __init__(self, views):
        self.views = views
        self.calls = 0

    def page_views(self, page_id, since):
        self.calls += 1
        return self.views.get(page_id)


def test_analytics_excludes_read_pages(db, project, fake_claude):
    cfg = setup(db, project)
    analytics = FakeAnalytics({"1": 12, "2": 0, "4": 0})
    stats = run_triage(cfg, db, analytics, ClaudeCLI(db, runner=fake_claude), run_id=1)
    assert db.get_meta("analytics_available") == "yes"
    assert stats["candidates"] == 2             # page 1 was read recently
    assert fake_claude.calls == []              # 2 is rule-based, 4 excluded


def test_analytics_unavailable_degrades(db, project, fake_claude):
    cfg = setup(db, project)
    analytics = FakeAnalytics({})              # every call returns None
    stats = run_triage(cfg, db, analytics, ClaudeCLI(db, runner=fake_claude), run_id=1)
    assert db.get_meta("analytics_available") == "no"
    assert analytics.calls == 1                 # only the probe
    assert stats["candidates"] == 3
