"""Workbook rendering: the caveat, null cells, and the stable interface."""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone

import pytest
from conftest import build_exchange, build_stash_currency

from poeflip.export import SHEETS, interface_fingerprint, write_workbook
from poeflip.models.corrupt import analyse_corrupt
from poeflip.models.currency import CROSS_VENUE_CAVEAT, analyse_currency
from poeflip.schema import parse_exchange, parse_stash_currency
from poeflip.store import Store

LEAGUE = "TestLeague"
TS = "2026-01-01T12:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "poe.db") as s:
        yield s


@pytest.fixture
def populated(store):
    exchange = parse_exchange(
        build_exchange(), league=LEAGUE, type_="Currency",
        endpoint="/poe1/api/economy/exchange/current/overview",
    )
    store.insert_exchange(ts=TS, league=LEAGUE, type_="Currency", rows=exchange.rows)
    stash = parse_stash_currency(
        build_stash_currency(pay_is_ask=False, pay_reciprocal=True, receive_reciprocal=False),
        league=LEAGUE, type_="Currency",
        endpoint="/poe1/api/economy/stash/current/currency/overview",
    )
    store.insert_stash_currency(ts=TS, league=LEAGUE, type_="Currency", rows=stash.rows)
    store.log_fetch(
        ts=TS, endpoint="/poe1/api/economy/exchange/current/overview",
        league=LEAGUE, type_="Currency", status=200, etag='"v1"', note="12 rows",
    )
    return store


def workbook_strings(path) -> str:
    """All shared strings in the workbook, as one blob."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        blob = archive.read("xl/sharedStrings.xml").decode("utf-8")
        for name in names:
            if name.startswith("xl/worksheets/sheet"):
                blob += archive.read(name).decode("utf-8")
        blob += archive.read("xl/workbook.xml").decode("utf-8")
    return blob


def render(cfg, store):
    currency = analyse_currency(store, cfg, LEAGUE, now=datetime.now(timezone.utc))
    corrupt = analyse_corrupt(store, cfg, LEAGUE)
    return write_workbook(
        cfg, store, league_id=LEAGUE, league_name=LEAGUE,
        currency=currency, corrupt=corrupt, primary="chaos",
    )


def test_workbook_is_written_with_every_expected_sheet(cfg, populated):
    path, changed = render(cfg, populated)
    assert path.exists()
    assert changed is False  # first run has nothing to compare against

    blob = workbook_strings(path)
    for name, _columns in SHEETS:
        assert name in blob, f"sheet {name} missing"


def test_cross_venue_caveat_is_inside_the_workbook(cfg, populated):
    """Spec 12.10: the caveat must render in the sheet, not only in docs."""
    path, _ = render(cfg, populated)
    blob = workbook_strings(path)
    assert "VERIFY IN-GAME" in blob
    assert "hypothesis to check in-game" in blob
    assert "never a confirmed arbitrage" in blob
    # The banner rendered is the same text the module publishes.
    assert "never a confirmed arbitrage" in CROSS_VENUE_CAVEAT


def test_filtered_rows_carry_a_readable_reason(cfg, populated):
    path, _ = render(cfg, populated)
    blob = workbook_strings(path)
    assert "Reason Excluded" in blob
    # The Mirror is unaffordable at a 200c bankroll and must say so.
    assert "max position" in blob


def test_headers_are_present_for_the_cockpit_query(cfg, populated):
    path, _ = render(cfg, populated)
    blob = workbook_strings(path)
    for header in ("Suggested Buy Rate", "Suggested Sell Rate", "Divergence %",
                   "Bankroll Gate", "Max Affordable Attempts", "Est Fills/Day"):
        assert header in blob


def test_interface_change_is_flagged_on_the_next_run(cfg, populated, monkeypatch):
    render(cfg, populated)

    # Simulate a header rename, which would break the user's Power Query.
    monkeypatch.setattr("poeflip.export.interface_fingerprint", lambda: "deadbeefdeadbeef")
    _path, changed = render(cfg, populated)
    assert changed is True


def test_fingerprint_is_stable_across_calls():
    assert interface_fingerprint() == interface_fingerprint()
    assert len(interface_fingerprint()) == 16


def test_missing_trend_is_a_blank_cell_not_a_zero(cfg, populated):
    """Spec 12.5: NULL, not 0, when history is insufficient."""
    import re

    path, _ = render(cfg, populated)
    with zipfile.ZipFile(path) as archive:
        sheet_names = [n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")]
        orders = archive.read(sorted(sheet_names)[1]).decode("utf-8")

    currency = analyse_currency(populated, cfg, LEAGUE)
    assert currency.orders
    assert all(row.trend_pct is None for row in currency.orders)

    # Trend is column F and volatility column G on Exchange_Orders. Every data
    # cell in them must be empty (self-closing), never a written value.
    for column in ("F", "G"):
        written = re.findall(rf'<c r="{column}(\d+)"[^/>]*>(.*?)</c>', orders)
        data_cells = [(row, body) for row, body in written if int(row) > 1]
        assert data_cells == [], f"column {column} wrote values where history is absent: {data_cells}"
        assert re.search(rf'<c r="{column}2"[^>]*/>', orders), "expected a blank trend cell"


def test_write_is_atomic_on_failure(cfg, populated, monkeypatch):
    """A mid-run failure must not leave a corrupt workbook behind."""
    path, _ = render(cfg, populated)
    original = path.read_bytes()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-write")

    monkeypatch.setattr("poeflip.export._write_sheet", boom)
    with pytest.raises(RuntimeError):
        render(cfg, populated)

    assert path.read_bytes() == original, "the previous workbook was damaged"
    leftovers = list(path.parent.glob("*.xlsx"))
    assert leftovers == [path], f"temp files left behind: {leftovers}"


def test_export_needs_no_network(cfg, populated, monkeypatch):
    """Spec 12.4: export runs with the network disconnected."""
    import httpx

    def no_network(*args, **kwargs):
        raise AssertionError("export attempted a network call")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    path, _ = render(cfg, populated)
    assert path.exists()
