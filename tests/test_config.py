import pytest

from dochealth.config import ConfigError, check_no_api_key, load_config


def test_api_key_in_environment_is_refused():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        check_no_api_key({"ANTHROPIC_API_KEY": "sk-ant-xxx"})


def test_api_key_in_dotenv_is_refused(project):
    with open(project / ".env", "a") as f:
        f.write("ANTHROPIC_API_KEY=sk-ant-xxx\n")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config(project / "config.yaml")


def test_load_config_merges_defaults_and_reads_env(project):
    cfg = load_config(project / "config.yaml")
    assert cfg.spaces == ["TEST"]
    assert cfg["llm"]["models"]["classify"] == "haiku"   # default kept
    assert cfg["llm"]["max_calls_per_run"] == 10          # overridden
    assert cfg.credentials.site_url == "https://example.atlassian.net"
    assert cfg.path("database") == project / "data" / "test.sqlite"


def test_missing_credentials(tmp_path):
    (tmp_path / "config.yaml").write_text("spaces: [A]\n")
    with pytest.raises(ConfigError, match="credentials"):
        load_config(tmp_path / "config.yaml")
    assert load_config(tmp_path / "config.yaml", require_credentials=False).credentials is None


def test_site_url_with_wiki_suffix_is_normalized(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("spaces: [A]\n")
    monkeypatch.setenv("ATLASSIAN_SITE_URL", "https://x.atlassian.net/wiki/")
    monkeypatch.setenv("ATLASSIAN_EMAIL", "a@b.c")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "t")
    assert load_config(tmp_path / "config.yaml").credentials.site_url == "https://x.atlassian.net"
