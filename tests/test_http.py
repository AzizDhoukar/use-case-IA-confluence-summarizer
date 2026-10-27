import pytest
import responses

from dochealth.atlassian.http import AtlassianError, AtlassianHTTP

SITE = "https://example.atlassian.net"


def client(sleeps):
    return AtlassianHTTP(SITE, "me@example.com", "tok", max_retries=3, sleep=sleeps.append)


@responses.activate
def test_retries_on_429_honouring_retry_after():
    url = f"{SITE}/wiki/api/v2/spaces"
    responses.get(url, status=429, headers={"Retry-After": "7"})
    responses.get(url, json={"results": [{"id": "1"}]})
    sleeps = []
    assert client(sleeps).get_json("/wiki/api/v2/spaces")["results"][0]["id"] == "1"
    assert sleeps == [7.0]


@responses.activate
def test_gives_up_after_max_retries():
    url = f"{SITE}/wiki/api/v2/spaces"
    for _ in range(4):
        responses.get(url, status=503)
    sleeps = []
    with pytest.raises(AtlassianError) as exc:
        client(sleeps).get_json("/wiki/api/v2/spaces")
    assert exc.value.status == 503 and len(sleeps) == 3


@responses.activate
def test_401_has_helpful_message():
    responses.get(f"{SITE}/wiki/api/v2/spaces", status=401)
    with pytest.raises(AtlassianError, match="API_TOKEN"):
        client([]).get_json("/wiki/api/v2/spaces")


@responses.activate
def test_cursor_pagination():
    responses.get(f"{SITE}/wiki/api/v2/spaces/1/pages",
                  match=[responses.matchers.query_param_matcher({"limit": "2"})],
                  json={"results": [{"id": "a"}, {"id": "b"}],
                        "_links": {"next": "/wiki/api/v2/spaces/1/pages?cursor=XYZ"}})
    responses.get(f"{SITE}/wiki/api/v2/spaces/1/pages",
                  match=[responses.matchers.query_param_matcher({"cursor": "XYZ"})],
                  json={"results": [{"id": "c"}], "_links": {}})
    ids = [r["id"] for r in client([]).paginate("/wiki/api/v2/spaces/1/pages", {"limit": 2})]
    assert ids == ["a", "b", "c"]
