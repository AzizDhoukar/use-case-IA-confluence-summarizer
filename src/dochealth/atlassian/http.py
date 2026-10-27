"""Shared HTTP client for Atlassian Cloud REST APIs: auth, retries, backoff, pagination."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Iterator
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class AtlassianError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AtlassianHTTP:
    def __init__(self, site_url: str, email: str, api_token: str, max_retries: int = 5,
                 timeout: float = 60, sleep: Callable[[float], None] = time.sleep):
        self.site_url = site_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({"Accept": "application/json",
                                     "User-Agent": "dochealth/0.1"})

    def url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return urljoin(self.site_url + "/", path.lstrip("/"))

    def _backoff(self, attempt: int, resp: requests.Response | None) -> float:
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), 300.0)
                except ValueError:
                    pass
        return min(2 ** attempt, 60) + random.uniform(0, 1)

    def request(self, method: str, path: str, *, params: dict | None = None,
                json: Any = None, ok_statuses: tuple[int, ...] = ()) -> requests.Response:
        """Send a request, retrying on 429/5xx and connection errors.

        Statuses in ok_statuses are returned to the caller instead of raising.
        """
        url = self.url(path)
        for attempt in range(self.max_retries + 1):
            resp = None
            try:
                resp = self.session.request(method, url, params=params, json=json,
                                            timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise AtlassianError(f"{method} {url} failed: {exc}") from exc
                delay = self._backoff(attempt, None)
                log.warning("%s %s: %s; retrying in %.1fs", method, url, exc, delay)
                self._sleep(delay)
                continue

            if resp.status_code < 400 or resp.status_code in ok_statuses:
                return resp
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                delay = self._backoff(attempt, resp)
                log.warning("%s %s -> %s; retrying in %.1fs", method, url, resp.status_code, delay)
                self._sleep(delay)
                continue
            if resp.status_code == 401:
                raise AtlassianError(
                    "401 Unauthorized: check ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN in .env", 401)
            raise AtlassianError(
                f"{method} {url} -> {resp.status_code}: {resp.text[:500]}", resp.status_code)
        raise AtlassianError(f"{method} {url}: retries exhausted")  # pragma: no cover

    def get_json(self, path: str, params: dict | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    def post_json(self, path: str, body: Any) -> Any:
        return self.request("POST", path, json=body).json()

    def paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Yield items from a cursor-paginated endpoint, v2 or v1 (follows _links.next)."""
        data = self.get_json(path, params)
        while True:
            yield from data.get("results", [])
            nxt = (data.get("_links") or {}).get("next")
            if not nxt:
                return
            # v2 'next' links start with /wiki/...; v1 ones are relative to _links.base.
            if not nxt.startswith(("/wiki/", "http")):
                base = (data.get("_links") or {}).get("base") or self.site_url + "/wiki"
                nxt = base.rstrip("/") + nxt
            data = self.get_json(nxt)
