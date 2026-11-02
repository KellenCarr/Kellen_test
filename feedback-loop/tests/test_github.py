import httpx
import pytest
from conftest import FEATURE, PULL

from loop.github import GitHub, GitHubError


def client(handler, sleeps=None):
    return GitHub(token="t", transport=httpx.MockTransport(handler), sleep=sleeps.append if sleeps is not None else id)


def test_paginates_and_skips_pull_requests():
    def handler(req: httpx.Request):
        assert req.headers["Authorization"] == "Bearer t"
        if req.url.params.get("page") == "2":
            return httpx.Response(200, json=[FEATURE])
        assert req.url.params["state"] == "all" and req.url.params["since"].startswith("2026-10-01")
        nxt = "https://api.github.com/repos/acme/widget/issues?state=all&page=2"
        return httpx.Response(200, json=[PULL], headers={"Link": f'<{nxt}>; rel="next"'})

    from datetime import UTC, datetime
    items = list(client(handler).iter_issues("acme/widget", since=datetime(2026, 10, 1, tzinfo=UTC)))
    assert [i["number"] for i in items] == [FEATURE["number"]]


def test_retries_server_errors_and_rate_limits():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502)
        if len(calls) == 2:
            return httpx.Response(429, headers={"retry-after": "3"})
        return httpx.Response(200, json=[])

    sleeps = []
    assert list(client(handler, sleeps).iter_issues("acme/widget")) == []
    assert len(calls) == 3 and sleeps[1] == 3.0


def test_permission_errors_are_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(403, json={"message": "Resource not accessible"})

    with pytest.raises(GitHubError, match="403"):
        list(client(handler).iter_issues("acme/widget"))
    assert len(calls) == 1


def test_get_file_requests_raw():
    def handler(req):
        assert req.headers["Accept"] == "application/vnd.github.raw+json"
        assert req.url.path == "/repos/acme/widget/contents/CHANGELOG.md"
        return httpx.Response(200, text="## 1.0\n- Added x\n")

    assert client(handler).get_file("acme/widget", "CHANGELOG.md").startswith("## 1.0")
