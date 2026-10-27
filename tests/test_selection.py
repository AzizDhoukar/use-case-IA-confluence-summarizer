"""Page-subtree selection: config parsing, CQL, scoped incremental inventory, CLI options."""

import functools
import json
import re

import pytest
import responses

from dochealth import cli
from dochealth.atlassian.confluence import Confluence
from dochealth.atlassian.http import AtlassianError, AtlassianHTTP
from dochealth.config import ConfigError, SpaceScope, load_config, parse_scopes
from dochealth.phases.inventory import build_cql, run_inventory

from .conftest import SITE


def test_parse_scopes_mixed_forms():
    scopes = parse_scopes(["A", {"key": "B", "include_pages": [123, "Specs"],
                                 "exclude_pages": ["Old"]}])
    assert scopes[0] == SpaceScope("A")
    assert scopes[1].include_pages == ["123", "Specs"] and scopes[1].is_scoped
    assert scopes[1].describe() == "B (only 123, Specs; excluding Old)"
    with pytest.raises(ConfigError):
        parse_scopes([{"include_pages": [1]}])


def test_build_cql():
    assert build_cql("~abc", [], []) == 'space = "~abc" AND type = page'
    assert build_cql("CDP", ["1", "2"], ["3"]) == (
        'space = "CDP" AND type = page AND (id = 1 OR ancestor = 1 OR id = 2 OR ancestor = 2)'
        ' AND NOT (id = 3 OR ancestor = 3)')


def v2_page(pid, title, version):
    return {"id": pid, "title": title, "spaceId": "100", "parentId": "10", "ownerId": "u1",
            "authorId": "u1", "createdAt": "2020-01-01T00:00:00Z",
            "version": {"number": version, "createdAt": "2021-01-01T00:00:00Z", "authorId": "u1"},
            "body": {"storage": {"value": f"<p>{title} body v{version}</p>"}},
            "_links": {"webui": f"/spaces/CDP/pages/{pid}"}}


def mock_scoped(versions):
    responses.get(f"{SITE}/wiki/api/v2/spaces",
                  match=[responses.matchers.query_param_matcher({"keys": "CDP"})],
                  json={"results": [{"id": "100", "key": "CDP", "name": "Portails"}]})
    responses.get(f"{SITE}/wiki/api/v2/pages",
                  match=[responses.matchers.query_param_matcher(
                      {"space-id": "100", "title": "Specs", "status": "current"})],
                  json={"results": [{"id": "10"}]})

    def search(request):
        assert "ancestor = 10" in request.params["cql"]
        results = [{"id": pid, "version": {"number": v},
                    "metadata": {"labels": {"results": [{"name": "spec"}]}}}
                   for pid, v in versions.items()]
        return 200, {}, json.dumps({"results": results, "_links": {"base": f"{SITE}/wiki"}})

    responses.add_callback(responses.GET, f"{SITE}/wiki/rest/api/content/search", search)

    def bulk(request):
        ids = request.params["id"].split(",")
        return 200, {}, json.dumps({"results": [v2_page(i, f"Page {i}", versions[i])
                                                for i in ids], "_links": {}})

    responses.add_callback(responses.GET, f"{SITE}/wiki/api/v2/pages", bulk)
    responses.post(f"{SITE}/wiki/api/v2/users-bulk", json={"results": []})


@responses.activate
def test_scoped_inventory_fetches_only_changed_bodies(project, db):
    cfg = load_config(project / "config.yaml")
    cfg.data["spaces"] = [{"key": "CDP", "include_pages": ["Specs"]}]
    conf = Confluence(AtlassianHTTP(SITE, "e", "t", sleep=lambda s: None))

    mock_scoped({"10": 1, "11": 1})
    stats = run_inventory(cfg, db, conf, run_id=1)
    assert stats["pages_scanned"] == 2 and stats["bodies_fetched"] == 2 and stats["new"] == 2
    assert json.loads(db.one("SELECT labels_json FROM pages WHERE id='11'")[0]) == ["spec"]

    responses.reset()
    mock_scoped({"10": 1, "11": 2})          # only page 11 was edited
    stats = run_inventory(cfg, db, conf, run_id=2)
    assert stats["bodies_fetched"] == 1 and stats["changed"] == 1 and stats["unchanged"] == 1
    bulk_calls = [c for c in responses.calls if "/api/v2/pages?" in c.request.url
                  and "id" in c.request.params]
    assert len(bulk_calls) == 1 and bulk_calls[0].request.params["id"] == "11"

    responses.reset()
    mock_scoped({"10": 1})                   # page 11 left the subtree
    stats = run_inventory(cfg, db, conf, run_id=3)
    assert stats["removed"] == 1
    assert db.one("SELECT active FROM pages WHERE id='11'")[0] == 0


@responses.activate
def test_unknown_space_suggests_key():
    responses.get(f"{SITE}/wiki/api/v2/spaces",
                  match=[responses.matchers.query_param_matcher({"keys": "Aziz Dhoukar"})],
                  json={"results": []})
    responses.get(f"{SITE}/wiki/api/v2/spaces",
                  json={"results": [{"id": "44", "key": "~7120ab", "name": "Aziz Dhoukar"}]})
    conf = Confluence(AtlassianHTTP(SITE, "e", "t"))
    with pytest.raises(AtlassianError, match="Did you mean key '~7120ab'"):
        conf.get_space("Aziz Dhoukar")


def test_v1_next_link_is_joined_with_base():
    http = AtlassianHTTP(SITE, "e", "t")
    with responses.RequestsMock() as rsps:
        rsps.get(f"{SITE}/wiki/rest/api/content/search",
                 match=[responses.matchers.query_param_matcher({"cql": "x"})],
                 json={"results": [{"id": 1}],
                       "_links": {"base": f"{SITE}/wiki",
                                  "next": "/rest/api/content/search?cql=x&cursor=c"}})
        rsps.get(f"{SITE}/wiki/rest/api/content/search",
                 match=[responses.matchers.query_param_matcher({"cql": "x", "cursor": "c"})],
                 json={"results": [{"id": 2}], "_links": {"base": f"{SITE}/wiki"}})
        assert [r["id"] for r in http.paginate("/wiki/rest/api/content/search", {"cql": "x"})] \
            == [1, 2]


def test_cli_page_option_requires_single_space(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    code = cli.main(["inventory", "--page", "123", "--config", str(project / "config.yaml")])
    assert code == cli.EXIT_CONFIG and "exactly one --space" in capsys.readouterr().err


@responses.activate
def test_cli_page_option_scopes_run(project, monkeypatch, fake_claude):
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli, "ClaudeCLI", functools.partial(cli.ClaudeCLI, runner=fake_claude))
    mock_scoped({"10": 1, "11": 1})
    responses.get(re.compile(rf"{SITE}/wiki/rest/api/analytics/.*"), status=404)
    code = cli.main(["run-all", "--dry-run", "--space", "CDP", "--page", "Specs",
                     "--config", str(project / "config.yaml")])
    assert code == 0
    md = next((project / "output" / "reports").glob("*.md")).read_text()
    assert "CDP (only Specs)" in md
