"""HTTP client obligations from spec 2.3."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from poeflip.errors import FetchError
from poeflip.ninja_client import NinjaClient
from poeflip.store import Store

UA = "poe-flip-tests/0.1 (contact: tests@poeflip.invalid)"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "poe.db") as s:
        yield s


def make_client(store, handler, clock=None, **kwargs) -> NinjaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, headers={"User-Agent": UA})
    return NinjaClient(
        user_agent=UA,
        cache=store,
        min_fetch_interval_minutes=kwargs.pop("min_fetch_interval_minutes", 5),
        client=http,
        sleep=lambda _seconds: None,
        now=clock or (lambda: datetime.now(timezone.utc)),
        delay_seconds=0.0,
        **kwargs,
    )


def test_etag_is_stored_and_replayed(store):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"lines": [1, 2, 3]}, headers={"ETag": '"v1"'})

    clock = Clock()
    client = make_client(store, handler, clock)

    first = client.fetch("/poe1/api/economy/leagues")
    assert first.status == 200
    assert first.etag == '"v1"'
    assert first.from_cache is False

    # Past the minimum interval, so a real conditional request goes out.
    clock.advance(minutes=6)
    second = client.fetch("/poe1/api/economy/leagues")
    assert second.status == 304
    assert second.from_cache is True
    assert second.payload == {"lines": [1, 2, 3]}
    assert calls[1].headers["If-None-Match"] == '"v1"'
    assert calls[0].headers["User-Agent"] == UA


def test_min_interval_floor_prevents_a_second_request(store):
    """Spec 2.3: do not poll faster than every 5 minutes — enforced in code."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"lines": []}, headers={"ETag": '"v1"'})

    clock = Clock()
    client = make_client(store, handler, clock)

    client.fetch("/poe1/api/economy/leagues")
    clock.advance(minutes=2)
    second = client.fetch("/poe1/api/economy/leagues")

    assert len(calls) == 1, "a second request was issued inside the minimum interval"
    assert second.status == 304
    assert second.from_cache is True
    assert "minimum interval" in second.note


def test_retries_then_gives_up_with_a_clear_message(store):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(503)

    client = make_client(store, handler)
    with pytest.raises(FetchError) as excinfo:
        client.fetch("/poe1/api/economy/leagues")

    assert len(attempts) == 4
    assert "503" in str(excinfo.value)
    assert "/poe1/api/economy/leagues" in str(excinfo.value)


def test_transient_failure_then_success(store):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True}, headers={"ETag": '"z"'})

    client = make_client(store, handler)
    result = client.fetch("/poe1/api/economy/leagues")
    assert result.status == 200
    assert calls["n"] == 2


def test_non_json_response_names_the_endpoint(store):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = make_client(store, handler)
    with pytest.raises(FetchError) as excinfo:
        client.fetch("/poe1/api/economy/leagues")
    message = str(excinfo.value)
    assert "not valid JSON" in message
    assert "/poe1/api/economy/leagues" in message
    assert "maintenance" in message


def test_dry_run_issues_no_requests(store):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry-run must not issue a request")

    client = make_client(store, handler, dry_run=True)
    result = client.fetch("/poe1/api/economy/leagues")
    assert result.status == 0
    assert result.payload is None
    assert "dry-run" in result.note


def test_304_without_a_cached_body_is_an_error(store):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    client = make_client(store, handler)
    with pytest.raises(FetchError) as excinfo:
        client.fetch("/poe1/api/economy/leagues")
    assert "304" in str(excinfo.value)


def test_query_params_are_separate_cache_keys(store):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"lines": []}, headers={"ETag": '"v"'})

    client = make_client(store, handler)
    client.exchange("Settlers", "Currency")
    client.exchange("Settlers", "Fragment")
    assert len(calls) == 2
    assert calls[0] != calls[1]
