"""Polite HTTP client: per-host rate limit, retries with backoff, request count.

Everything fetched through here is untrusted data. Nothing in a response is
ever executed or treated as an instruction.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class PoliteSession:
    def __init__(self, cfg, session: requests.Session | None = None, sleep=time.sleep, clock=time.monotonic):
        http = cfg["http"]
        self.min_interval = float(http["min_interval_seconds"])
        self.timeout = float(http["timeout_seconds"])
        self.max_retries = int(http["max_retries"])
        self.backoff = float(http["backoff_seconds"])
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": http["user_agent"]})
        self._sleep, self._clock = sleep, clock
        self._last: dict[str, float] = {}
        self.request_count = 0

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = self.min_interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)

    def get(self, url: str, params: dict | None = None, **kwargs) -> requests.Response:
        """GET with rate limiting. Raises requests.HTTPError on a final failure."""
        host = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._wait(host)
            try:
                self.request_count += 1
                resp = self.session.get(url, params=params, timeout=self.timeout, **kwargs)
                self._last[host] = self._clock()
                if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                    self._sleep(self.backoff * (2 ** attempt))
                    continue
                resp.raise_for_status()
                return resp
            except requests.HTTPError:
                raise
            except requests.RequestException as exc:  # network error: retry
                self._last[host] = self._clock()
                last_exc = exc
                if attempt < self.max_retries:
                    self._sleep(self.backoff * (2 ** attempt))
        raise requests.ConnectionError(f"GET {url} failed after retries: {last_exc}")
