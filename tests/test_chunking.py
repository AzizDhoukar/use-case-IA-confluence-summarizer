from dochealth.text import chunk_text


def test_small_sections_are_merged_with_heading_path():
    text = "# Install\nIntro\n\n## Linux\napt install x\n\n## Windows\nuse the msi"
    chunks = chunk_text(text, max_tokens=500)
    assert len(chunks) == 1
    assert chunks[0].heading == "Install"
    assert "## Windows" in chunks[0].text and "apt install x" in chunks[0].text


def test_chunks_respect_size_and_keep_headings():
    para = "word " * 150   # ~750 chars
    text = "# A\n" + "\n\n".join([para] * 4) + "\n\n# B\n## B1\n" + para
    chunks = chunk_text(text, max_tokens=250)   # ~1000 chars
    assert all(len(c.text) <= 1000 for c in chunks)
    assert chunks[0].heading == "A"
    assert chunks[-1].heading == "B > B1"
    assert [c.idx for c in chunks] == list(range(len(chunks)))


def test_huge_paragraph_is_hard_split():
    chunks = chunk_text("x" * 5000, max_tokens=250)
    assert len(chunks) == 5 and all(len(c.text) <= 1000 for c in chunks)


def test_empty_text():
    assert chunk_text("") == []
