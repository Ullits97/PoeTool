"""Exchange order recommendations, liquidity gating, and null history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import REFERENCE_MARKET, build_exchange, build_stash_currency

from poeflip.models.currency import (
    MIN_VOLATILITY_POINTS,
    analyse_currency,
    expected_fills_per_day,
)
from poeflip.schema import parse_exchange, parse_stash_currency
from poeflip.store import Store

EXCHANGE_ENDPOINT = "/poe1/api/economy/exchange/current/overview"
STASH_ENDPOINT = "/poe1/api/economy/stash/current/currency/overview"
LEAGUE = "TestLeague"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "poe.db") as s:
        yield s


def seed(store, *, ts: str, market=None, volume_chaos=5000.0, listings=40):
    market = market or REFERENCE_MARKET
    exchange = parse_exchange(
        build_exchange(market=market, volume_chaos=volume_chaos),
        league=LEAGUE, type_="Currency", endpoint=EXCHANGE_ENDPOINT,
    )
    store.insert_exchange(ts=ts, league=LEAGUE, type_="Currency", rows=exchange.rows)

    stash = parse_stash_currency(
        build_stash_currency(
            pay_is_ask=False, pay_reciprocal=True, receive_reciprocal=False,
            market=market, listings=listings,
        ),
        league=LEAGUE, type_="Currency", endpoint=STASH_ENDPOINT,
    )
    store.insert_stash_currency(ts=ts, league=LEAGUE, type_="Currency", rows=stash.rows)
    return exchange, stash


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# -- trend / volatility nullability (spec 12.5) ---------------------------
def test_trend_and_volatility_are_null_with_a_single_snapshot(store, cfg):
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now))

    result = analyse_currency(store, cfg, LEAGUE, now=now)
    rows = result.orders + result.filtered
    assert rows
    for row in rows:
        assert row.trend_pct is None, "trend must be NULL, not 0, without history"
        assert row.volatility is None


def test_trend_appears_once_two_snapshots_exist(store, cfg):
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now - timedelta(hours=3)))
    # Same currencies, 10% dearer.
    seed(store, ts=iso(now), market={k: v * 1.10 for k, v in REFERENCE_MARKET.items()})

    result = analyse_currency(store, cfg, LEAGUE, now=now)
    rows = {r.currency_id: r for r in result.orders + result.filtered}
    divine = rows["divine-orb"]
    assert divine.trend_pct == pytest.approx(0.10, rel=1e-6)
    # Two points is still too thin for a standard deviation.
    assert divine.volatility is None


def test_volatility_needs_three_points(store, cfg):
    now = datetime.now(timezone.utc)
    for i, factor in enumerate([1.0, 1.05, 1.02]):
        seed(
            store,
            ts=iso(now - timedelta(hours=6 - 2 * i)),
            market={k: v * factor for k, v in REFERENCE_MARKET.items()},
        )
    result = analyse_currency(store, cfg, LEAGUE, now=now)
    rows = {r.currency_id: r for r in result.orders + result.filtered}
    divine = rows["divine-orb"]
    assert divine.history_points >= MIN_VOLATILITY_POINTS
    assert divine.volatility is not None and divine.volatility > 0


def test_history_outside_the_window_is_ignored(store, cfg):
    now = datetime.now(timezone.utc)
    # 48h old, outside the default 24h trend window.
    seed(store, ts=iso(now - timedelta(hours=48)))
    seed(store, ts=iso(now), market={k: v * 2 for k, v in REFERENCE_MARKET.items()})

    result = analyse_currency(store, cfg, LEAGUE, now=now)
    rows = {r.currency_id: r for r in result.orders + result.filtered}
    assert rows["divine-orb"].trend_pct is None


# -- liquidity gating (spec 6.4) ------------------------------------------
def test_low_volume_is_excluded_with_a_readable_reason(store, cfg):
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now), volume_chaos=100.0)  # below min_volume_chaos of 500

    result = analyse_currency(store, cfg, LEAGUE, now=now)
    assert result.orders == []
    assert result.filtered
    assert all("min_volume_chaos" in row.filter_reason for row in result.filtered)


def test_too_few_listings_is_excluded(store, cfg):
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now), listings=3)  # below min_listings of 20

    result = analyse_currency(store, cfg, LEAGUE, now=now)
    assert result.orders == []
    assert any("min_listings" in row.filter_reason for row in result.filtered)


def test_unaffordable_currency_is_excluded(store, cfg):
    """A Mirror costs far more than 25% of a 200c bankroll."""
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now))

    result = analyse_currency(store, cfg, LEAGUE, now=now)
    filtered = {r.currency_id: r for r in result.filtered}
    assert "mirror-of-kalandra" in filtered
    assert "max position" in filtered["mirror-of-kalandra"].filter_reason
    assert "mirror-of-kalandra" not in {r.currency_id for r in result.orders}


def test_bankroll_boundary_is_inclusive(store, cfg):
    """A unit costing exactly the max position is affordable; a hair more is not."""
    now = datetime.now(timezone.utc)
    limit = cfg.bankroll.max_position_chaos  # 50c
    market = dict(REFERENCE_MARKET)
    market["Exactly At Limit"] = limit
    market["Just Over Limit"] = limit * 1.0001
    seed(store, ts=iso(now), market=market)

    result = analyse_currency(store, cfg, LEAGUE, now=now)
    ranked = {r.currency_id for r in result.orders}
    filtered = {r.currency_id: r for r in result.filtered}

    assert "exactly-at-limit" in ranked
    assert "just-over-limit" in filtered
    assert "one unit costs more" in filtered["just-over-limit"].filter_reason


def test_ranked_rows_all_pass_the_gate(store, cfg):
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now))
    result = analyse_currency(store, cfg, LEAGUE, now=now)
    assert result.orders
    assert all(row.liquidity_ok and not row.filter_reason for row in result.orders)


# -- ranking and order rates ----------------------------------------------
def test_suggested_rates_straddle_the_current_rate(store, cfg):
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now))
    result = analyse_currency(store, cfg, LEAGUE, now=now)
    offset = cfg.currency.order_offset_pct
    for row in result.orders:
        assert row.suggested_buy_rate == pytest.approx(row.chaos_value * (1 - offset))
        assert row.suggested_sell_rate == pytest.approx(row.chaos_value * (1 + offset))
        assert row.suggested_buy_rate < row.chaos_value < row.suggested_sell_rate


def test_ranking_is_by_score_descending(store, cfg):
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now))
    result = analyse_currency(store, cfg, LEAGUE, now=now)
    scores = [r.score for r in result.orders if r.score is not None]
    assert scores == sorted(scores, reverse=True)


def test_fill_estimator_is_capped_and_null_safe():
    assert expected_fills_per_day(None, 50.0, 24.0) is None
    assert expected_fills_per_day(0.0, 50.0, 24.0) == 0.0
    # 5000c turnover against a 50c position is 50 round trips, capped at 24.
    assert expected_fills_per_day(5000.0, 50.0, 24.0) == 24.0
    assert expected_fills_per_day(100.0, 50.0, 24.0) == pytest.approx(1.0)


# -- cross-venue (spec 6.3) -----------------------------------------------
def test_cross_venue_reports_divergence_and_spread(store, cfg):
    now = datetime.now(timezone.utc)
    seed(store, ts=iso(now))
    result = analyse_currency(store, cfg, LEAGUE, now=now)

    assert result.cross_venue
    assert result.orientation is not None
    for row in result.cross_venue:
        # Both feeds were built from the same market, so they agree.
        assert row.divergence_pct == pytest.approx(0.0, abs=1e-9)
        assert row.stash_spread_pct is not None and row.stash_spread_pct > 0


def test_cross_venue_detects_a_real_divergence(store, cfg):
    now = datetime.now(timezone.utc)
    exchange = parse_exchange(
        build_exchange(), league=LEAGUE, type_="Currency", endpoint=EXCHANGE_ENDPOINT
    )
    store.insert_exchange(ts=iso(now), league=LEAGUE, type_="Currency", rows=exchange.rows)
    # Stash listings 20% above the exchange across the board.
    stash = parse_stash_currency(
        build_stash_currency(
            pay_is_ask=False, pay_reciprocal=True, receive_reciprocal=False,
            market={k: v * 1.2 for k, v in REFERENCE_MARKET.items()},
        ),
        league=LEAGUE, type_="Currency", endpoint=STASH_ENDPOINT,
    )
    store.insert_stash_currency(ts=iso(now), league=LEAGUE, type_="Currency", rows=stash.rows)

    result = analyse_currency(store, cfg, LEAGUE, now=now)
    assert result.cross_venue
    for row in result.cross_venue:
        assert row.divergence_pct == pytest.approx(0.2, rel=1e-6)


def test_analysis_of_an_empty_database_is_empty_not_fabricated(store, cfg):
    result = analyse_currency(store, cfg, LEAGUE)
    assert result.orders == []
    assert result.cross_venue == []
    assert result.notes
