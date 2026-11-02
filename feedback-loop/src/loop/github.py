"""Minimal GitHub REST client: issues (paginated, incremental) and raw file contents."""

import random
import time
from collections.abc import Iterator
from datetime import datetime

import httpx

API = "https://api.github.com"
MAX_ATTEMPTS = 5


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None = None, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "feedback-loop"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=API, headers=headers, timeout=30, transport=transport)
        self._sleep = sleep

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = self._http.get(url, params=params, headers=headers)
            except httpx.TransportError as e:
                if attempt == MAX_ATTEMPTS:
                    raise GitHubError(f"GET {url}: {e}") from e
                self._backoff(attempt)
                continue
            if r.status_code == 200:
                return r
            limited = r.status_code == 429 or (
                r.status_code == 403 and (r.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in r.headers)
            )
            if (limited or r.status_code >= 500) and attempt < MAX_ATTEMPTS:
                self._backoff(attempt, r)
                continue
            raise GitHubError(f"GET {url} -> {r.status_code}: {r.text[:300]}")
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int, r: httpx.Response | None = None) -> None:
        if r is not None and "retry-after" in r.headers:
            delay = float(r.headers["retry-after"])
        elif r is not None and r.headers.get("x-ratelimit-remaining") == "0":
            delay = max(0.0, float(r.headers.get("x-ratelimit-reset", time.time())) - time.time()) + 1
            if delay > 900:
                raise GitHubError(f"rate limit resets in {delay:.0f}s; set GITHUB_TOKEN or retry later")
        else:
            delay = min(60.0, 2**attempt) + random.uniform(0, 1)
        self._sleep(delay)

    def iter_issues(self, repo: str, since: datetime | None = None) -> Iterator[dict]:
        """Issues (not PRs) updated at or after `since`, oldest update first."""
        params = {"state": "all", "sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since.isoformat()
        url: str | None = f"/repos/{repo}/issues"
        while url:
            r = self._get(url, params=params)
            for item in r.json():
                if "pull_request" not in item:
                    yield item
            url, params = r.links.get("next", {}).get("url"), None  # next link already carries the query

    def get_file(self, repo: str, path: str, ref: str | None = None) -> str:
        r = self._get(f"/repos/{repo}/contents/{path}", params={"ref": ref} if ref else None,
                      headers={"Accept": "application/vnd.github.raw+json"})
        return r.text
