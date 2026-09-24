"""Rate-limited HTTP client shared by all sources.

Both upstreams are small public servers, so every request goes through a
single client that spaces requests out and backs off on errors.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "colombia-vote-audit/0.1 (+benthecarman1@gmail.com)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class PoliteClient:
    def __init__(
        self,
        min_interval: float = 1.0,
        max_retries: int = 4,
        timeout: float = 180.0,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._sleep = sleep
        self._last = 0.0
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def _wait_turn(self):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise
                log.warning("%s %s failed (%s), retrying", method, url, e)
            else:
                if resp.status_code not in RETRY_STATUSES or attempt == self.max_retries:
                    return resp
                log.warning("%s %s -> %s, retrying", method, url, resp.status_code)
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    self._sleep(int(retry_after))
                    continue
            self._sleep(min(2**attempt * 5, 120))
        raise AssertionError("unreachable")

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self):
        self.client.close()
