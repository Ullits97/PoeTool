"""HTTP access to poe.ninja's PoE 1 economy endpoints.

Deliberately free of domain logic: it returns raw decoded JSON plus fetch
metadata. Parsing lives in `schema.py`, so an upstream shape change is a
one-file fix (spec 13).

Client obligations from spec 2.3 are enforced here, not left to the caller:
descriptive User-Agent, ETag / If-None-Match on every request, a hard
5-minute floor between requests to the same endpoint, sequential requests
with a delay, and no cache bypassing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from .errors import FetchError

BASE_URL = "https://poe.ninja"

LEAGUES_PATH = "/poe1/api/economy/leagues"
EXCHANGE_PATH = "/poe1/api/economy/exchange/current/overview"
STASH_CURRENCY_PATH = "/poe1/api/economy/stash/current/currency/overview"
STASH_ITEM_PATH = "/poe1/api/economy/stash/current/item/overview"

# poe.ninja asks for sequential requests with a small delay between them.
INTER_REQUEST_DELAY_SECONDS = 1.0

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (2, 4, 8, 16)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

log = logging.getLogger("poeflip.client")


class HttpCache(Protocol):
    """Persistence the client needs for conditional requests.

    Implemented by `store.Store`; declared here as a Protocol so the client
    has no import dependency on the database layer.
    """

    def get_cached(self, key: str) -> tuple[str | None, Any | None, str | None]:
        """Return (etag, payload, fetched_at_iso) for a cache key."""

    def put_cached(self, key: str, etag: str | None, payload: Any, fetched_at: str) -> None:
        """Store a fresh payload and its ETag."""


@dataclass(frozen=True)
class FetchResult:
    endpoint: str          # path only, used as the log/DB identity
    url: str
    key: str               # cache key: path + query
    status: int            # 200, 304, or an error status
    payload: Any | None
    etag: str | None
    from_cache: bool
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.payload is not None


class NinjaClient:
    def __init__(
        self,
        *,
        user_agent: str,
        cache: HttpCache,
        min_fetch_interval_minutes: int,
        base_url: str = BASE_URL,
        dry_run: bool = False,
        delay_seconds: float = INTER_REQUEST_DELAY_SECONDS,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.min_interval = timedelta(minutes=min_fetch_interval_minutes)
        self.dry_run = dry_run
        self.delay_seconds = delay_seconds
        self._sleep = sleep
        self._now = now
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
        )
        self._last_request_at: float | None = None

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "NinjaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- endpoints ---------------------------------------------------------
    def leagues(self) -> FetchResult:
        return self.fetch(LEAGUES_PATH)

    def exchange(self, league: str, type_: str) -> FetchResult:
        return self.fetch(EXCHANGE_PATH, {"league": league, "type": type_})

    def stash_currency(self, league: str, type_: str) -> FetchResult:
        return self.fetch(STASH_CURRENCY_PATH, {"league": league, "type": type_})

    def stash_items(self, league: str, type_: str) -> FetchResult:
        return self.fetch(STASH_ITEM_PATH, {"league": league, "type": type_})

    # -- core --------------------------------------------------------------
    def fetch(self, path: str, params: dict[str, str] | None = None) -> FetchResult:
        query = urlencode(sorted((params or {}).items()))
        key = f"{path}?{query}" if query else path
        url = f"{self.base_url}{key}"

        etag, cached_payload, fetched_at = self.cache.get_cached(key)

        if self.dry_run:
            log.info("[dry-run] would GET %s (If-None-Match: %s)", url, etag or "-")
            return FetchResult(
                endpoint=path,
                url=url,
                key=key,
                status=0,
                payload=cached_payload,
                etag=etag,
                from_cache=cached_payload is not None,
                note="dry-run: no request issued",
            )

        # Hard floor: never re-request an endpoint inside the minimum interval.
        # This is enforced in code, not only in config (spec 2.3).
        if cached_payload is not None and fetched_at:
            age = self._age(fetched_at)
            if age is not None and age < self.min_interval:
                remaining = self.min_interval - age
                note = (
                    f"served from local cache; no request issued "
                    f"({int(remaining.total_seconds())}s left of the "
                    f"{int(self.min_interval.total_seconds() // 60)}min minimum interval)"
                )
                log.info("%s -> cached (%s)", key, note)
                return FetchResult(
                    endpoint=path,
                    url=url,
                    key=key,
                    status=304,
                    payload=cached_payload,
                    etag=etag,
                    from_cache=True,
                    note=note,
                )

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag

        response = self._request_with_retry(url, headers, key)

        if response.status_code == 304:
            if cached_payload is None:
                raise FetchError(
                    f"{key}: upstream returned 304 Not Modified but no cached payload is "
                    "stored locally. Clear the http_cache table and re-run."
                )
            log.info("%s -> 304 not modified", key)
            return FetchResult(
                endpoint=path,
                url=url,
                key=key,
                status=304,
                payload=cached_payload,
                etag=etag,
                from_cache=True,
                note="304 Not Modified; reused cached payload",
            )

        if response.status_code != 200:
            raise FetchError(
                f"{key}: unexpected HTTP {response.status_code} from {url}\n"
                f"  body excerpt: {response.text[:400]}"
            )

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise FetchError(
                f"{key}: response was not valid JSON ({exc}).\n"
                f"  url: {url}\n"
                f"  body excerpt: {response.text[:400]}"
            ) from exc

        new_etag = response.headers.get("ETag")
        self.cache.put_cached(key, new_etag, payload, self._now().isoformat())
        log.info("%s -> 200 (%d bytes)", key, len(response.content))
        return FetchResult(
            endpoint=path,
            url=url,
            key=key,
            status=200,
            payload=payload,
            etag=new_etag,
            from_cache=False,
        )

    # -- helpers -----------------------------------------------------------
    def _age(self, fetched_at: str) -> timedelta | None:
        try:
            stamp = datetime.fromisoformat(fetched_at)
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return self._now() - stamp

    def _throttle(self) -> None:
        """Keep requests sequential with a small gap between them."""
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.delay_seconds:
                self._sleep(self.delay_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def _request_with_retry(
        self, url: str, headers: dict[str, str], key: str
    ) -> httpx.Response:
        last_error: str = ""
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            try:
                response = self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("%s: attempt %d failed (%s)", key, attempt + 1, last_error)
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                last_error = f"HTTP {response.status_code}"
                log.warning("%s: attempt %d got %s", key, attempt + 1, last_error)

            if attempt < MAX_ATTEMPTS - 1:
                delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                log.info("%s: retrying in %ds", key, delay)
                self._sleep(delay)

        raise FetchError(
            f"{key}: giving up after {MAX_ATTEMPTS} attempts. Last error: {last_error}\n"
            f"  url: {url}"
        )
