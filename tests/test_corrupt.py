"""Gem corrupt EV, bankroll gating, and calibration."""

from __future__ import annotations

import pytest
from conftest import build_exchange, build_gem_payload

from poeflip.config import load_config
from poeflip.models.corrupt import (
    analyse_corrupt,
    calibrate,
    exclusion_reason,
    parse_variant,
)
from poeflip.schema import parse_exchange, parse_items
from poeflip.store import Store

LEAGUE = "TestLeague"
TS = "2026-01-01T12:00:00+00:00"
ITEM_ENDPOINT = "/poe1/api/economy/stash/current/item/overview"
EXCHANGE_ENDPOINT = "/poe1/api/economy/exchange/current/overview"

# A hand-computed reference gem. Outcome values chosen so the EV can be
# checked by hand, line by line, against spec 7.3.
#
#   0.250 x  10c (no change,   20/20)  =  2.50
#   0.125 x 100c (level up,    21/20)  = 12.50
#   0.125 x   4c (level down,  19/20)  =  0.50
#   0.125 x  20c (quality up,  20/23)  =  2.50
#   0.125 x   6c (quality down,20/17)  =  0.75
#   0.250 x  40c (vaal transform)      = 10.00
#                             gross EV = 28.75
#   input 5c + vaal orb 1c    cost     =  6.00
#                             net EV   = 22.75   EV% = 22.75 / 6 = 379.17%
HAND_COMPUTED_GROSS_EV = 28.75
HAND_COMPUTED_COST = 6.0
HAND_COMPUTED_EV = 22.75

REFERENCE_GEM = [
    {"name": "Test Gem", "variant": "20/20", "corrupted": False, "chaosValue": 5.0},
    {"name": "Test Gem", "variant": "20/20", "corrupted": True, "chaosValue": 10.0},
    {"name": "Test Gem", "variant": "21/20", "corrupted": True, "chaosValue": 100.0},
    {"name": "Test Gem", "variant": "19/20", "corrupted": True, "chaosValue": 4.0},
    {"name": "Test Gem", "variant": "20/23", "corrupted": True, "chaosValue": 20.0},
    {"name": "Test Gem", "variant": "20/17", "corrupted": True, "chaosValue": 6.0},
    {"name": "Vaal Test Gem", "variant": "20/20", "corrupted": True, "chaosValue": 40.0},
]

CURRENCY_MARKET = {"Chaos Orb": 1.0, "Vaal Orb": 1.0, "Divine Orb": 200.0}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "poe.db") as s:
        yield s


def seed(store, gems, market=None):
    items = parse_items(
        build_gem_payload(gems), league=LEAGUE, type_="SkillGem", endpoint=ITEM_ENDPOINT
    )
    store.insert_items(ts=TS, league=LEAGUE, type_="SkillGem", rows=items.rows)
    exchange = parse_exchange(
        build_exchange(market=market or CURRENCY_MARKET),
        league=LEAGUE, type_="Currency", endpoint=EXCHANGE_ENDPOINT,
    )
    store.insert_exchange(ts=TS, league=LEAGUE, type_="Currency", rows=exchange.rows)


# -- variant parsing -------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("20/20", (20, 20)),
        ("21/20", (21, 20)),
        ("20/23", (20, 23)),
        ("20", (20, 0)),
        ("20/20c", (20, 20)),
        ("", None),
        (None, None),
        ("Awakened", None),
    ],
)
def test_parse_variant(text, expected):
    assert parse_variant(text) == expected


# -- EV arithmetic (spec 7.3) ---------------------------------------------
def test_ev_matches_the_hand_computed_fixture(store, cfg):
    seed(store, REFERENCE_GEM)
    result = analyse_corrupt(store, cfg, LEAGUE)

    assert result.vaal_cost_chaos == pytest.approx(1.0)
    row = next(r for r in result.rows if r.gem_name == "Test Gem")

    assert row.has_vaal_variant is True
    assert row.input_cost_chaos == pytest.approx(5.0)
    assert row.cost_per_attempt == pytest.approx(HAND_COMPUTED_COST)
    assert row.ev_chaos == pytest.approx(HAND_COMPUTED_EV)
    assert row.ev_pct == pytest.approx(HAND_COMPUTED_EV / HAND_COMPUTED_COST)
    assert row.incomplete_data is False

    gross = sum(o.probability * o.chaos_value for o in row.outcomes)
    assert gross == pytest.approx(HAND_COMPUTED_GROSS_EV)


def test_every_outcome_bucket_is_resolved_to_a_line(store, cfg):
    seed(store, REFERENCE_GEM)
    result = analyse_corrupt(store, cfg, LEAGUE)
    row = next(r for r in result.rows if r.gem_name == "Test Gem")

    by_outcome = {o.outcome: o for o in row.outcomes}
    assert by_outcome["level_up"].chaos_value == pytest.approx(100.0)
    assert by_outcome["vaal_transform"].chaos_value == pytest.approx(40.0)
    assert "Vaal Test Gem" in by_outcome["vaal_transform"].target_label
    assert sum(o.probability for o in row.outcomes) == pytest.approx(1.0)


def test_max_affordable_attempts(store, cfg):
    seed(store, REFERENCE_GEM)
    result = analyse_corrupt(store, cfg, LEAGUE)
    row = next(r for r in result.rows if r.gem_name == "Test Gem")
    # 200c bankroll / 6c per attempt
    assert row.max_affordable_attempts == 33


# -- missing data is never guessed (spec 7.2) -----------------------------
def test_missing_outcome_line_marks_incomplete_and_excludes(store, cfg):
    gems = [g for g in REFERENCE_GEM if g["variant"] != "21/20"]
    seed(store, gems)
    result = analyse_corrupt(store, cfg, LEAGUE)

    assert not any(r.gem_name == "Test Gem" for r in result.rows)
    row = next(r for r in result.filtered if r.gem_name == "Test Gem")
    assert row.incomplete_data is True
    assert row.ev_chaos is None, "EV must not be computed from a partial outcome set"
    reason = exclusion_reason(row)
    assert "incomplete_data" in reason
    assert "level_up" in reason


def test_thin_outcome_market_is_gated_on_listing_count(store, cfg):
    gems = [dict(g) for g in REFERENCE_GEM]
    for gem in gems:
        if gem["variant"] == "21/20":
            gem["listingCount"] = 2  # below min_outcome_listings of 10
    seed(store, gems)
    result = analyse_corrupt(store, cfg, LEAGUE)

    row = next(r for r in result.filtered if r.gem_name == "Test Gem")
    assert row.incomplete_data is True
    assert "min_outcome_listings" in exclusion_reason(row)


def test_gem_without_a_vaal_counterpart_uses_the_other_table(store, cfg):
    gems = [g for g in REFERENCE_GEM if not g["name"].startswith("Vaal ")]
    seed(store, gems)
    result = analyse_corrupt(store, cfg, LEAGUE)

    row = next(r for r in result.rows if r.gem_name == "Test Gem")
    assert row.has_vaal_variant is False
    assert {o.outcome for o in row.outcomes} == {
        "no_change", "level_up", "level_down", "quality_up", "quality_down"
    }
    # 0.34*10 + 0.165*(100+4+20+6) = 3.4 + 21.45 = 24.85, less 6c cost.
    assert row.ev_chaos == pytest.approx(24.85 - 6.0)


def test_missing_vaal_orb_price_reports_rather_than_assumes(store, cfg):
    items = parse_items(
        build_gem_payload(REFERENCE_GEM), league=LEAGUE, type_="SkillGem",
        endpoint=ITEM_ENDPOINT,
    )
    store.insert_items(ts=TS, league=LEAGUE, type_="SkillGem", rows=items.rows)
    # No currency feed at all, so the Vaal Orb price is unknown.
    result = analyse_corrupt(store, cfg, LEAGUE)
    assert result.rows == []
    assert any("Vaal Orb" in note for note in result.notes)


# -- bankroll gating boundaries (spec 7.4) --------------------------------
def test_bankroll_gate_boundary(store, write_config):
    """cost_per_attempt <= bankroll * max_corrupt_fraction, inclusive."""
    cfg = load_config(write_config())
    budget = cfg.bankroll.max_corrupt_cost_chaos  # 200 * 0.04 = 8c
    assert budget == pytest.approx(8.0)

    gems = list(REFERENCE_GEM)
    # Vaal Orb costs 1c, so an input at 7c lands exactly on the limit.
    gems += [
        {"name": "At Limit Gem", "variant": "20/20", "corrupted": False, "chaosValue": 7.0},
        {"name": "Over Limit Gem", "variant": "20/20", "corrupted": False, "chaosValue": 7.01},
    ]
    for name in ("At Limit Gem", "Over Limit Gem"):
        for variant in ("20/20", "21/20", "19/20", "20/23", "20/17"):
            gems.append(
                {"name": name, "variant": variant, "corrupted": True, "chaosValue": 12.0}
            )
        gems.append(
            {"name": f"Vaal {name}", "variant": "20/20", "corrupted": True, "chaosValue": 12.0}
        )
    seed(store, gems)

    result = analyse_corrupt(store, cfg, LEAGUE)
    ranked = {r.gem_name for r in result.rows}
    filtered = {r.gem_name: r for r in result.filtered}

    assert "At Limit Gem" in ranked, "a cost exactly at the limit must be admitted"
    assert "Over Limit Gem" in filtered
    assert "exceeds bankroll limit" in filtered["Over Limit Gem"].bankroll_gate


def test_gate_widens_as_bankroll_grows(store, write_config):
    gems = list(REFERENCE_GEM) + [
        {"name": "Pricey Gem", "variant": "20/20", "corrupted": False, "chaosValue": 40.0},
    ]
    for variant in ("20/20", "21/20", "19/20", "20/23", "20/17"):
        gems.append({"name": "Pricey Gem", "variant": variant, "corrupted": True, "chaosValue": 80.0})
    gems.append({"name": "Vaal Pricey Gem", "variant": "20/20", "corrupted": True, "chaosValue": 80.0})
    seed(store, gems)

    small = load_config(write_config(bankroll={"total_chaos": 200}))
    assert "Pricey Gem" in {r.gem_name for r in analyse_corrupt(store, small, LEAGUE).filtered}

    large = load_config(write_config(bankroll={"total_chaos": 5000}))
    assert "Pricey Gem" in {r.gem_name for r in analyse_corrupt(store, large, LEAGUE).rows}


def test_ranking_is_by_ev_pct_descending(store, cfg):
    seed(store, REFERENCE_GEM)
    result = analyse_corrupt(store, cfg, LEAGUE)
    values = [r.ev_pct for r in result.rows if r.ev_pct is not None]
    assert values == sorted(values, reverse=True)


# -- calibration (spec 7.5) -----------------------------------------------
def test_calibration_without_a_log_reports_zero_samples(cfg):
    rows = calibrate(cfg)
    assert rows
    assert all(row.sample_size == 0 for row in rows)
    assert all("no log yet" in row.note for row in rows)
    assert any(row.outcome == "vaal_transform" for row in rows)


def test_calibration_counts_logged_outcomes(cfg, tmp_path):
    log = tmp_path / "corrupt_log.csv"
    log.write_text(
        "date,gem_name,input_variant,input_cost_chaos,vaal_cost_chaos,"
        "outcome_variant,outcome_corrupted,realised_value_chaos,notes\n"
        "2026-01-01,Test Gem,20/20,5,1,20/20,1,10,\n"
        "2026-01-02,Test Gem,20/20,5,1,21/20,1,100,\n"
        "2026-01-03,Test Gem,20/20,5,1,19/20,1,4,\n"
        "2026-01-04,Test Gem,20/20,5,1,20/20,1,40,vaal transform\n",
        encoding="utf-8",
    )
    rows = {r.outcome: r for r in calibrate(cfg, path=log)}

    assert rows["no_change"].observed_count == 1
    assert rows["level_up"].observed_count == 1
    assert rows["level_down"].observed_count == 1
    assert rows["vaal_transform"].observed_count == 1
    assert rows["no_change"].observed_frequency == pytest.approx(0.25)
    assert rows["no_change"].configured_probability == pytest.approx(0.25)
    assert all(r.sample_size == 4 for r in rows.values())
    assert "far too few" in rows["no_change"].note


def test_calibration_never_writes_to_the_log(cfg, tmp_path):
    log = tmp_path / "corrupt_log.csv"
    content = (
        "date,gem_name,input_variant,input_cost_chaos,vaal_cost_chaos,"
        "outcome_variant,outcome_corrupted,realised_value_chaos,notes\n"
        "2026-01-01,Test Gem,20/20,5,1,21/20,1,100,\n"
    )
    log.write_text(content, encoding="utf-8")
    calibrate(cfg, path=log)
    assert log.read_text(encoding="utf-8") == content
