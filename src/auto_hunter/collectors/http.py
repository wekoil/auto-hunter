"""Shared HTTP client for collectors: pacing, retries, honest User-Agent."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from auto_hunter.collectors.base import CollectorError
from auto_hunter.config import HttpSettings

log = logging.getLogger(__name__)


class PoliteClient:
    """httpx wrapper that waits request_delay_s between requests and retries sparingly.

    Retries network errors, 429 and 5xx with exponential backoff; other statuses are
    returned to the caller to interpret. Raises CollectorError when retries run out.
    """

    def __init__(self, base_url: str, http: HttpSettings,
                 headers: dict[str, str] | None = None, **client_kwargs: Any) -> None:
        self._http = http
        self._client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": http.user_agent, **(headers or {})},
            timeout=http.timeout_s,
            **client_kwargs,
        )
        self._last_request = 0.0

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        err = ""
        for attempt in range(1, self._http.max_retries + 1):
            wait = self._http.request_delay_s - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code != 429 and resp.status_code < 500:
                    return resp
                err = f"HTTP {resp.status_code}"
            log.warning("%s %s failed (%s), attempt %d", method, url, err, attempt)
            time.sleep(self._http.request_delay_s * 2**attempt)
        raise CollectorError(f"{method} {url} failed: {err}")
