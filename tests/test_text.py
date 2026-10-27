from dochealth.text import content_hash, storage_to_text, truncate

STORAGE = """
<h1>Deploy guide</h1>
<p>Use <strong>Jenkins</strong> to deploy.</p>
<ac:structured-macro ac:name="toc"><ac:parameter ac:name="maxLevel">2</ac:parameter></ac:structured-macro>
<h2>Steps</h2>
<ul><li>Build</li><li>Ship</li></ul>
<table><tbody><tr><th>Env</th><th>URL</th></tr><tr><td>prod</td><td>p.example.com</td></tr></tbody></table>
<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">bash</ac:parameter>
<ac:plain-text-body><![CDATA[make deploy]]></ac:plain-text-body></ac:structured-macro>
<p>Owner: <ac:link><ri:user ri:account-id="abc"/></ac:link>
<ac:structured-macro ac:name="status"><ac:parameter ac:name="title">DONE</ac:parameter></ac:structured-macro></p>
"""


def test_storage_to_text_keeps_structure():
    text = storage_to_text(STORAGE)
    assert "# Deploy guide" in text
    assert "## Steps" in text
    assert "Use Jenkins to deploy." in text
    assert "- Build" in text and "- Ship" in text
    assert "| prod | p.example.com |" in text
    assert "make deploy" in text
    assert "[DONE]" in text
    assert "maxLevel" not in text and "bash" not in text


def test_empty_storage():
    assert storage_to_text("") == ""


def test_hash_changes_with_content():
    assert content_hash("A", "x") == content_hash("A", "x")
    assert content_hash("A", "x") != content_hash("A", "y")
    assert content_hash("A", "x") != content_hash("B", "x")


def test_truncate():
    text = "para one\n\n" + "x" * 100
    assert truncate(text, 1000) == text
    out = truncate(text, 50)
    assert "truncated" in out and len(out) < len(text) + 60
