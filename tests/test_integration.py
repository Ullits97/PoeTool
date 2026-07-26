"""End-to-end runs against a mocked poe.ninja, covering spec 12.

Everything here goes through `run.py` exactly as Task Scheduler would.
"""

from __future__ import annotations

import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import ROOT, build_exchange, build_gem_payload, build_stash_currency

sys.path.insert(0, str(ROOT))

import run as cli  # noqa: E402
from poeflip.store import Store  # noqa: E402

LEAGUE_ID = "TestLeague"

GEMS = [
    {"name": "Test Gem", "variant": "20/20", "corrupted": False, "chaosValue": 5.0},
    {"name": "Test Gem", "variant": "20/20", "corrupted": True, "chaosValue": 10.0},
    {"name": "Test Gem", "variant": "21/20", "corrupted": True, "chaosValue": 100.0},
    {"name": "Test Gem", "variant": "19/20", "corrupted": True, "chaosValue": 4.0},
    {"name": "Test Gem", "variant": "20/23", "corrupted": True, "chaosValue": 20.0},
    {"name": "Test Gem", "variant": "20/17", "corrupted": True, "chaosValue": 6.0},
    {"name": "Vaal Test Gem", "variant": "20/20", "corrupted": True, "chaosValue": 40.0},
]


class FakeNinja:
    """A stand-in poe.ninja that honours If-None-Match."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.etag = '"snapshot-1"'

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(str(request.url))

        assert "User-Agent" in request.headers
        assert "poe-flip" in request.headers["User-Agent"]

        if request.headers.get("If-None-Match") == self.etag:
            return httpx.Response(304)

        headers = {"ETag": self.etag}
        if path.endswith("/economy/leagues"):
            return httpx.Response(
                200,
                json=[{"id": LEAGUE_ID, "name": LEAGUE_ID}, {"id": "Standard", "name": "Standard"}],
                headers=headers,
            )
        if "/stash/current/currency/" in path:
            return httpx.Response(
                200,
                json=build_stash_currency(
                    pay_is_ask=False, pay_reciprocal=True, receive_reciprocal=False
                ),
                headers=headers,
            )
        if "/stash/current/item/" in path:
            return httpx.Response(200, json=build_gem_payload(GEMS), headers=headers)
        if "/exchange/current/" in path:
            return httpx.Response(200, json=build_exchange(), headers=headers)
        return httpx.Response(404, json={"error": "unknown route"})


@pytest.fixture
def fake_ninja(monkeypatch):
    ninja = FakeNinja()
    clock = {"now": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)}
    real_client_cls = cli.NinjaClient

    def factory(**kwargs):
        kwargs["client"] = httpx.Client(
            transport=httpx.MockTransport(ninja.handler),
            headers={"User-Agent": kwargs["user_agent"]},
        )
        kwargs["sleep"] = lambda _seconds: None
        kwargs["now"] = lambda: clock["now"]
        kwargs["delay_seconds"] = 0.0
        return real_client_cls(**kwargs)

    monkeypatch.setattr(cli, "NinjaClient", factory)
    # Snapshot timestamps follow the same fake clock as the client, so a test
    # that advances time produces genuinely distinct snapshots.
    monkeypatch.setattr(
        cli, "utc_now_iso", lambda: clock["now"].replace(microsecond=0).isoformat()
    )
    ninja.clock = clock
    return ninja


@pytest.fixture
def project(write_config, tmp_path):
    config_path = write_config(export={"path": str(tmp_path / "out" / "poe_data.xlsx"),
                                       "history_days": 14})
    return config_path


def invoke(config_path: Path, command: str, *extra: str) -> int:
    return cli.main([command, "--config", str(config_path), *extra])


def db_path(config_path: Path) -> Path:
    return config_path.parent / "data" / "poe.db"


def counts(config_path: Path) -> dict[str, int]:
    with Store(db_path(config_path)) as store:
        return store.snapshot_counts()


# -- spec 12.3: rebuild from nothing --------------------------------------
def test_run_builds_the_database_from_scratch(project, fake_ninja):
    assert not db_path(project).exists()
    assert invoke(project, "run") == 0

    after = counts(project)
    assert after["snap_exchange"] > 0
    assert after["snap_stash_currency"] > 0
    assert after["snap_item"] > 0
    assert after["fetch_log"] > 0
    assert (project.parent / "out" / "poe_data.xlsx").exists()


def test_deleting_the_database_rebuilds_it_cleanly(project, fake_ninja):
    assert invoke(project, "run") == 0
    first = counts(project)

    db_path(project).unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db_path(project)) + suffix).unlink(missing_ok=True)

    assert invoke(project, "run") == 0
    second = counts(project)
    assert second["snap_exchange"] == first["snap_exchange"]
    assert second["snap_item"] == first["snap_item"]


# -- spec 12.2: a second run inside the cache window writes nothing --------
def test_second_run_writes_zero_new_snapshot_rows(project, fake_ninja):
    assert invoke(project, "run") == 0
    first = counts(project)

    # Past the 5-minute floor, so real conditional requests go out and the
    # server answers 304 for every endpoint.
    fake_ninja.clock["now"] += timedelta(minutes=6)
    fake_ninja.requests.clear()
    assert invoke(project, "run") == 0

    second = counts(project)
    assert fake_ninja.requests, "expected conditional requests on the second run"
    for table in ("snap_exchange", "snap_stash_currency", "snap_item"):
        assert second[table] == first[table], f"{table} grew on a 304 run"


def test_rate_limit_floor_suppresses_requests_entirely(project, fake_ninja):
    assert invoke(project, "run") == 0
    issued = len(fake_ninja.requests)

    # No clock advance: still inside the minimum interval.
    assert invoke(project, "run") == 0
    assert len(fake_ninja.requests) == issued, "requests were issued inside the floor"


# -- spec 12.4: analyse and export work offline ---------------------------
def test_analyse_and_export_run_without_network(project, fake_ninja, monkeypatch):
    assert invoke(project, "run") == 0

    def no_network(*args, **kwargs):
        raise AssertionError("a network call was attempted offline")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(cli, "NinjaClient", None)

    assert invoke(project, "analyse") == 0
    assert invoke(project, "export") == 0
    assert (project.parent / "out" / "poe_data.xlsx").exists()


def test_status_runs_offline(project, fake_ninja, capsys):
    assert invoke(project, "run") == 0
    assert invoke(project, "status") == 0
    out = capsys.readouterr().out
    assert LEAGUE_ID in out
    assert "snapshot rows" in out
    assert "last fetch per endpoint" in out


# -- spec 12.7 / 12.6: loud failures ---------------------------------------
def test_placeholder_user_agent_stops_startup(write_config, fake_ninja, capsys):
    path = write_config(app={"user_agent": "poe-flip/0.1 (contact: CHANGEME@example.com)"})
    assert cli.main(["run", "--config", str(path)]) == 2
    assert "placeholder" in capsys.readouterr().err


def test_renamed_endpoint_fails_loudly(project, monkeypatch, caplog):
    """Spec 12.6: a malformed payload names the endpoint, never an empty sheet."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/economy/leagues"):
            return httpx.Response(200, json=[{"id": LEAGUE_ID, "name": LEAGUE_ID}])
        return httpx.Response(200, json={"rows": [], "note": "restructured"})

    real_client_cls = cli.NinjaClient

    def factory(**kwargs):
        kwargs["client"] = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers={"User-Agent": kwargs["user_agent"]},
        )
        kwargs["sleep"] = lambda _s: None
        kwargs["delay_seconds"] = 0.0
        return real_client_cls(**kwargs)

    monkeypatch.setattr(cli, "NinjaClient", factory)

    assert invoke(project, "run") == 1
    message = caplog.text
    assert "no 'lines' array" in message
    assert "/poe1/api/economy/stash/current/currency/overview" in message


def test_dry_run_issues_no_requests(project, fake_ninja):
    assert invoke(project, "run", "--dry-run") == 0
    assert fake_ninja.requests == []
    assert not db_path(project).exists() or counts(project)["snap_exchange"] == 0


# -- spec 12.8 / 12.10: workbook content -----------------------------------
def test_workbook_carries_gates_and_the_caveat(project, fake_ninja):
    assert invoke(project, "run") == 0
    path = project.parent / "out" / "poe_data.xlsx"

    with zipfile.ZipFile(path) as archive:
        blob = archive.read("xl/sharedStrings.xml").decode("utf-8")
        for name in archive.namelist():
            if name.startswith("xl/worksheets/sheet"):
                blob += archive.read(name).decode("utf-8")

    assert "VERIFY IN-GAME" in blob
    assert "Reason Excluded" in blob
    assert "Mirror of Kalandra" in blob  # excluded, and visible with its reason
    assert "max position" in blob
    assert "Test Gem" in blob           # the corrupt module produced rows
    assert LEAGUE_ID in blob


def test_league_is_taken_from_the_first_entry(project, fake_ninja):
    assert invoke(project, "run") == 0
    with Store(db_path(project)) as store:
        row = store.conn.execute(
            "SELECT DISTINCT league FROM snap_exchange"
        ).fetchall()
    assert [r["league"] for r in row] == [LEAGUE_ID]


def test_snapshots_accumulate_across_runs(project, fake_ninja):
    assert invoke(project, "run") == 0
    first = counts(project)["snap_exchange"]

    # A new upstream snapshot with a different ETag, past the interval floor.
    fake_ninja.clock["now"] += timedelta(minutes=6)
    fake_ninja.etag = '"snapshot-2"'
    assert invoke(project, "run") == 0

    assert counts(project)["snap_exchange"] > first
