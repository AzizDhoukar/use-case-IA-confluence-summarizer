import json

import numpy as np

from dochealth.config import load_config
from dochealth.embeddings import ensure_embeddings, page_vectors
from dochealth.llm.claude_cli import ClaudeCLI
from dochealth.phases.duplicates import (_split, find_clusters, run_duplicates, signature,
                                         tune_threshold)
from dochealth.report.data import build_report_data
from dochealth.report.markdown import render_markdown
from dochealth.report.storage import render_storage
from dochealth.text import content_hash

VPN = ("Pour se connecter au VPN, installer le client GlobalProtect, puis utiliser le portail "
       "vpn.example.com avec son compte. Le support est assuré par l'équipe réseau. ") * 4
VPN_OLD = VPN.replace("vpn.example.com", "old-vpn.example.com")
PIM = ("Le PIM centralise les fiches produits: import des catalogues fournisseurs, "
       "enrichissement des attributs, publication vers les portails adhérents. ") * 4
SHORT = "Page parent."


def add(db, pid, title, body):
    db.execute("""INSERT INTO pages(id, space_key, title, url, modified_at, version, labels_json,
                     body_text, content_hash, active)
                  VALUES(?, 'TEST', ?, ?, '2024-01-01', 1, '[]', ?, ?, 1)""",
               (pid, title, f"https://x/{pid}", body, content_hash(title, body)))
    db.commit()


def seed(db):
    add(db, "1", "Accès VPN", VPN)
    add(db, "2", "VPN (ancienne page)", VPN_OLD)
    add(db, "3", "PIM", PIM)
    add(db, "4", "Parent", SHORT)


ANSWER = {"topic": "Accès VPN", "relationship": "duplicate",
          "conflicts": [{"page_a": "1", "statement_a": "portail vpn.example.com",
                         "page_b": "2", "statement_b": "portail old-vpn.example.com",
                         "explanation": "Adresse du portail différente."},
                        {"page_a": "1", "statement_a": "x", "page_b": "999",
                         "statement_b": "y", "explanation": "page hors cluster"}],
          "canonical_page": "1", "canonical_reason": "Page la plus à jour.",
          "merge_notes": "Archiver la page 2 après vérification de l'adresse."}


def test_embeddings_are_incremental(db, fake_encoder):
    seed(db)
    pages = db.query("SELECT * FROM pages")
    assert ensure_embeddings(db, fake_encoder, pages, 500) == 4
    assert ensure_embeddings(db, fake_encoder, pages, 500) == 0
    ids, vecs = page_vectors(db, ["1", "2", "3"], fake_encoder.name)
    assert ids == ["1", "2", "3"] and np.allclose(np.linalg.norm(vecs, axis=1), 1)


def test_find_clusters_groups_similar_pages():
    vecs = np.array([[1, 0, 0], [0.99, 0.14, 0], [0, 0, 1]], dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    clusters = find_clusters(["a", "b", "c"], vecs, 0.85, 5)
    assert [sorted(c[0]) for c in clusters] == [["a", "b"]]
    assert clusters[0][1] > 0.95


def test_big_clusters_are_split():
    sim = np.ones((7, 7))
    groups = _split(list(range(7)), sim, 5)
    assert [len(g) for g in groups] == [5, 2]
    assert sorted(i for g in groups for i in g) == list(range(7))


def test_run_duplicates_analyzes_once_and_skips_unchanged(db, project, fake_claude, fake_encoder):
    cfg = load_config(project / "config.yaml")
    seed(db)
    fake_claude.answer = lambda prompt: ANSWER
    llm = ClaudeCLI(db, runner=fake_claude)
    stats = run_duplicates(cfg, db, llm, fake_encoder, run_id=1)
    assert stats["eligible_pages"] == 3          # the short parent page is ignored
    assert stats["clusters"] == 1 and stats["analyzed"] == 1
    prompt = fake_claude.calls[0]["input"]
    assert '<page id="1">' in prompt and '<page id="2">' in prompt and "PIM" not in prompt
    cmd = fake_claude.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"

    stats = run_duplicates(cfg, db, ClaudeCLI(db, runner=fake_claude), fake_encoder, run_id=2)
    assert stats["unchanged"] == 1 and len(fake_claude.calls) == 1

    # Editing a page changes the cluster signature -> re-analyzed.
    old_sig = signature(db, ["1", "2"])
    body = VPN_OLD + " Mise à jour."
    db.execute("UPDATE pages SET body_text=?, content_hash=? WHERE id='2'",
               (body, content_hash("VPN (ancienne page)", body)))
    assert signature(db, ["1", "2"]) != old_sig
    run_duplicates(cfg, db, ClaudeCLI(db, runner=fake_claude), fake_encoder, run_id=3)
    assert len(fake_claude.calls) == 2


def test_report_shows_clusters_and_drops_foreign_conflicts(db, project, fake_claude,
                                                          fake_encoder):
    cfg = load_config(project / "config.yaml")
    seed(db)
    fake_claude.answer = lambda prompt: ANSWER
    run_duplicates(cfg, db, ClaudeCLI(db, runner=fake_claude), fake_encoder, run_id=1)
    stored = json.loads(db.one("SELECT result_json FROM clusters")["result_json"])
    assert len(stored["conflicts"]) == 1         # conflict citing page 999 removed

    data = build_report_data(cfg, db)
    assert len(data.duplicates) == 1 and data.duplicates[0].canonical.id == "1"
    storage = render_storage(data)
    assert "CANONICAL" in storage and "old-vpn.example.com" in storage
    assert "1 CONFLICT(S)" in storage
    md = render_markdown(data)
    assert "### DUPLICATE – 1 conflict(s): Accès VPN" in md
    assert "| Contradictions found | 1 |" in md


def test_budget_exhausted_leaves_cluster_pending(db, project, fake_claude, fake_encoder):
    cfg = load_config(project / "config.yaml")
    seed(db)
    stats = run_duplicates(cfg, db, ClaudeCLI(db, max_calls=0, runner=fake_claude),
                           fake_encoder, run_id=1)
    assert stats["stopped"] == "budget" and stats["pending"] == 1
    assert build_report_data(cfg, db).duplicates_pending == 1


def test_tune_threshold_output(db, project, fake_encoder):
    cfg = load_config(project / "config.yaml")
    seed(db)
    out = tune_threshold(cfg, db, fake_encoder)
    assert "3 pages, 3 pairs" in out
    assert "threshold 0.85:" in out and "Accès VPN" in out


def test_report_category_order_and_definitions(db, project):
    data = build_report_data(load_config(project / "config.yaml"), db)
    for text in (render_storage(data), render_markdown(data)):
        positions = [text.rindex(label)
                     for label in ("RELEVANT", "DUPLICATE", "OBSOLETE", "HISTORICAL")]
        assert positions == sorted(positions)
        assert "Categories" in text and "Worth keeping as a record" in text
        assert text.index("Categories") < text.index("Archive triage")


def test_own_report_pages_are_never_analyzed(db, project):
    from dochealth.phases.duplicates import eligible_pages
    from dochealth.phases.triage import date_stale_pages
    seed(db)
    add(db, "9", "Documentation Health Report – 2026-10-27 (15:02:41)", VPN)
    cfg = load_config(project / "config.yaml")
    assert "9" not in [p["id"] for p in eligible_pages(db, cfg)]
    assert "9" not in [p["id"] for p in date_stale_pages(db, cfg)]
