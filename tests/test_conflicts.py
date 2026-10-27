import numpy as np

from dochealth.config import load_config
from dochealth.llm.claude_cli import ClaudeCLI
from dochealth.phases import conflicts
from dochealth.phases.conflicts import clustered_page_pairs, passage_pairs, run_conflicts
from dochealth.report.data import build_report_data
from dochealth.report.markdown import render_markdown
from dochealth.report.storage import render_storage
from dochealth.text import content_hash

PAY_A = ("# Paiement\n\nLe délai de paiement des factures adhérents est de 30 jours fin de mois. "
         "Les factures sont envoyées par mail au service comptable de l'adhérent chaque lundi.")
PAY_B = ("# Paiement\n\nLe délai de paiement des factures adhérents est de 45 jours fin de mois. "
         "Les factures sont envoyées par mail au service comptable de l'adhérent chaque lundi.")
CATALOG = ("# Catalogue\n\nL'import des catalogues fournisseurs se fait chaque nuit depuis le PIM; "
           "les attributs enrichis sont publiés vers les portails le lendemain matin à 6h.")
CONTACTS = ("# Contacts\n\nPour toute question sur le portail, ouvrir un ticket Jira dans le "
            "projet support ou appeler la hotline du lundi au vendredi de 9h à 18h.")


def add(db, pid, title, body):
    db.execute("""INSERT INTO pages(id, space_key, title, url, modified_at, version, labels_json,
                     body_text, content_hash, active)
                  VALUES(?, 'TEST', ?, ?, '2025-01-01', 1, '[]', ?, ?, 1)""",
               (pid, title, f"https://x/{pid}", body, content_hash(title, body)))
    db.commit()


def setup(db, project):
    cfg = load_config(project / "config.yaml")
    cfg.data["embeddings"]["chunk_tokens"] = 60     # one chunk per section
    cfg.data["conflicts"]["passage_threshold"] = 0.7
    add(db, "1", "Spécifications portail", PAY_A + "\n\n" + CATALOG)
    add(db, "2", "Guide support", PAY_B + "\n\n" + CONTACTS)
    return cfg


def answer(prompt):
    return {"conflicts": [
        {"statement_a": "Le délai de paiement des factures adhérents est de 30 jours fin de mois.",
         "statement_b": "Le délai de paiement des factures adhérents est de 45 jours fin de mois.",
         "explanation": "Délai de paiement différent."},
        {"statement_a": "Phrase inventée.", "statement_b": "Autre phrase inventée.",
         "explanation": "hallucination"}]}


def row(pid, idx, text="t"):
    return {"page_id": pid, "idx": idx, "heading": "", "text": text}


def test_passage_pairs_groups_by_page_pair(monkeypatch):
    monkeypatch.setattr(conflicts, "BLOCK", 2)           # exercise the block loop
    vecs = np.array([[1, 0, 0], [1, 0, 0], [0.99, 0.14, 0], [0, 1, 0], [0, 0, 1]],
                    dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    rows = [row("1", 0), row("1", 1), row("2", 0), row("2", 1), row("3", 0)]
    pairs = passage_pairs(rows, vecs, 0.9, per_pair=1)
    assert [k for k, _ in pairs] == [("1", "2")]          # same-page pair (1,1) ignored
    assert len(pairs[0][1]) == 1 and pairs[0][1][0][1]["page_id"] == "1"
    assert passage_pairs(rows, vecs, 0.9, 1, skip={("1", "2")}) == []


def test_run_conflicts_finds_quoted_contradiction_once(db, project, fake_claude, fake_encoder):
    cfg = setup(db, project)
    fake_claude.answer = answer
    stats = run_conflicts(cfg, db, ClaudeCLI(db, runner=fake_claude), fake_encoder, 1)
    assert stats["page_pairs"] == 1 and stats["analyzed"] == 1
    assert stats["conflicts"] == 1 and stats["dropped"] == 1
    prompt = fake_claude.calls[0]["input"]
    assert '<passage id="A1" page="A"' in prompt and "45 jours" in prompt
    assert "Catalogue" not in prompt and "hotline" not in prompt     # only the close sections
    cmd = fake_claude.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"

    stats = run_conflicts(cfg, db, ClaudeCLI(db, runner=fake_claude), fake_encoder, 2)
    assert stats["unchanged"] == 1 and len(fake_claude.calls) == 1

    data = build_report_data(cfg, db)
    assert len(data.page_conflicts) == 1 and data.contradictions == 1
    assert "Contradictions between pages" in render_storage(data)
    md = render_markdown(data)
    assert "| Contradictions found | 1 |" in md and "45 jours" in md


def test_pages_already_grouped_by_duplicates_are_skipped(db, project, fake_claude, fake_encoder):
    cfg = setup(db, project)
    db.execute("INSERT INTO clusters(signature, page_ids_json, status, last_seen_run) "
               "VALUES('s', '[\"2\", \"1\"]', 'analyzed', 7)")
    db.set_meta("duplicates_run_id", "7")
    assert clustered_page_pairs(db) == {("1", "2")}
    stats = run_conflicts(cfg, db, ClaudeCLI(db, runner=fake_claude), fake_encoder, 8)
    assert stats["page_pairs"] == 0 and not fake_claude.calls


def test_call_cap_leaves_pairs_pending(db, project, fake_claude, fake_encoder):
    cfg = setup(db, project)
    cfg.data["conflicts"]["max_calls_per_run"] = 0
    stats = run_conflicts(cfg, db, ClaudeCLI(db, runner=fake_claude), fake_encoder, 1)
    assert stats["stopped"] == "budget" and stats["pending"] == 1
    assert build_report_data(cfg, db).page_conflicts_pending == 1


def test_tune_passages(db, project, fake_encoder):
    cfg = setup(db, project)
    out = conflicts.tune_passages(cfg, db, fake_encoder)
    assert "Section-level conflicts: 4 passages" in out and "Guide support" in out
