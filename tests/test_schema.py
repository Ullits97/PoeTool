"""Parsing, chaos normalisation, and loud failure on malformed payloads."""

from __future__ import annotations

import json

import pytest
from conftest import FIXTURES, REFERENCE_MARKET, build_exchange, build_stash_currency

from poeflip.errors import SchemaError
from poeflip.schema import (
    parse_exchange,
    parse_items,
    parse_leagues,
    parse_stash_currency,
)

EXCHANGE_ENDPOINT = "/poe1/api/economy/exchange/current/overview"
STASH_ENDPOINT = "/poe1/api/economy/stash/current/currency/overview"
ITEM_ENDPOINT = "/poe1/api/economy/stash/current/item/overview"


# -- leagues ---------------------------------------------------------------
def test_first_league_is_the_current_challenge_league():
    payload = [
        {"id": "Settlers", "name": "Settlers"},
        {"id": "Standard", "name": "Standard"},
    ]
    leagues = parse_leagues(payload, endpoint="/poe1/api/economy/leagues")
    assert leagues[0].id == "Settlers"


def test_empty_league_list_raises():
    with pytest.raises(SchemaError) as excinfo:
        parse_leagues([], endpoint="/poe1/api/economy/leagues")
    assert "/poe1/api/economy/leagues" in str(excinfo.value)


# -- chaos normalisation (spec 6.1) ---------------------------------------
def test_chaos_primary_is_identity():
    payload = build_exchange(primary="chaos", chaos_per_primary=1.0)
    parsed = parse_exchange(
        payload, league="Test", type_="Currency", endpoint=EXCHANGE_ENDPOINT
    )
    assert parsed.conversion.chaos_per_primary == 1.0
    by_id = {row.currency_id: row for row in parsed.rows}
    assert by_id["divine-orb"].chaos_value == pytest.approx(200.0)
    assert by_id["chromatic-orb"].chaos_value == pytest.approx(0.02)


def test_divine_primary_is_converted_using_the_stash_reference():
    """core.primary is not guaranteed to be chaos; the factor must be derived."""
    payload = build_exchange(primary="divine", chaos_per_primary=200.0)
    reference = {name: chaos for name, chaos in REFERENCE_MARKET.items()}
    parsed = parse_exchange(
        payload, league="Test", type_="Currency",
        endpoint=EXCHANGE_ENDPOINT, chaos_reference=reference,
    )
    assert parsed.conversion.chaos_per_primary == pytest.approx(200.0, rel=1e-6)
    by_id = {row.currency_id: row for row in parsed.rows}
    # A divine is 1.0 in primary units and must come back out as 200 chaos.
    assert by_id["divine-orb"].primary_value == pytest.approx(1.0)
    assert by_id["divine-orb"].chaos_value == pytest.approx(200.0)
    assert by_id["exalted-orb"].chaos_value == pytest.approx(15.0)


def test_reciprocal_primary_value_is_detected_not_assumed():
    """If primaryValue were units-per-primary, the factor must invert."""
    payload = build_exchange(primary="divine", chaos_per_primary=200.0, reciprocal=True)
    parsed = parse_exchange(
        payload, league="Test", type_="Currency",
        endpoint=EXCHANGE_ENDPOINT, chaos_reference=dict(REFERENCE_MARKET),
    )
    by_id = {row.currency_id: row for row in parsed.rows}
    assert by_id["divine-orb"].chaos_value == pytest.approx(200.0, rel=1e-6)
    assert by_id["exalted-orb"].chaos_value == pytest.approx(15.0, rel=1e-6)
    assert "units-per-primary" in parsed.conversion.method


def test_core_rates_is_used_as_the_cross_check():
    payload = build_exchange(primary="divine", chaos_per_primary=200.0, include_rates=True)
    parsed = parse_exchange(
        payload, league="Test", type_="Currency",
        endpoint=EXCHANGE_ENDPOINT, chaos_reference=dict(REFERENCE_MARKET),
    )
    assert "core.rates" in parsed.conversion.method


def test_conflicting_core_rates_anchor_aborts():
    """Two readings of the same payload must not silently disagree."""
    payload = build_exchange(primary="divine", chaos_per_primary=200.0)
    payload["core"]["rates"]["chaos-orb"] = 0.5  # nonsense: implies 1 divine = 2 chaos
    with pytest.raises(SchemaError) as excinfo:
        parse_exchange(
            payload, league="Test", type_="Currency",
            endpoint=EXCHANGE_ENDPOINT, chaos_reference=dict(REFERENCE_MARKET),
        )
    assert "disagrees" in str(excinfo.value)


def test_non_chaos_primary_without_reference_aborts():
    """Spec 6.1: abort rather than emit mis-scaled numbers."""
    payload = build_exchange(primary="divine", chaos_per_primary=200.0)
    with pytest.raises(SchemaError) as excinfo:
        parse_exchange(
            payload, league="Test", type_="Currency", endpoint=EXCHANGE_ENDPOINT
        )
    message = str(excinfo.value)
    assert "chaos reference" in message
    assert EXCHANGE_ENDPOINT in message


def test_missing_core_names_the_endpoint():
    payload = build_exchange()
    del payload["core"]
    with pytest.raises(SchemaError) as excinfo:
        parse_exchange(payload, league="T", type_="Currency", endpoint=EXCHANGE_ENDPOINT)
    assert EXCHANGE_ENDPOINT in str(excinfo.value)


def test_volume_is_rescaled_into_chaos():
    payload = build_exchange(primary="divine", chaos_per_primary=200.0, volume_chaos=5000.0)
    parsed = parse_exchange(
        payload, league="T", type_="Currency",
        endpoint=EXCHANGE_ENDPOINT, chaos_reference=dict(REFERENCE_MARKET),
    )
    row = next(r for r in parsed.rows if r.currency_id == "divine-orb")
    assert row.volume_chaos == pytest.approx(5000.0, rel=1e-6)


# -- loud failure (spec 12.6) ---------------------------------------------
def test_renamed_lines_key_produces_a_clear_error_not_an_empty_sheet():
    with pytest.raises(SchemaError) as excinfo:
        parse_stash_currency(
            {"rows": []}, league="T", type_="Currency", endpoint=STASH_ENDPOINT
        )
    message = str(excinfo.value)
    assert "no 'lines' array" in message
    assert STASH_ENDPOINT in message
    assert "rows" in message  # the keys that *were* present


def test_unknown_quote_key_lists_what_was_actually_present():
    payload = build_stash_currency(
        pay_is_ask=True, pay_reciprocal=False, receive_reciprocal=False
    )
    payload["lines"][0]["pay"] = {"amount": 1.0, "samples": 3}
    with pytest.raises(SchemaError) as excinfo:
        parse_stash_currency(
            payload, league="T", type_="Currency", endpoint=STASH_ENDPOINT
        )
    message = str(excinfo.value)
    assert "'pay' rate" in message
    assert "amount" in message
    assert "probe.py" in message


def test_payload_that_is_not_an_object_raises():
    with pytest.raises(SchemaError):
        parse_stash_currency(
            "<html>404</html>", league="T", type_="Currency", endpoint=STASH_ENDPOINT
        )


# -- items -----------------------------------------------------------------
def test_items_parse_variant_and_corrupted_flags():
    payload = {
        "lines": [
            {
                "id": 7, "name": "Vaal Haste", "baseType": "Vaal Haste",
                "variant": "20/20", "corrupted": True, "chaosValue": 30.0,
                "divineValue": 0.15, "count": 12, "listingCount": 25, "links": 0,
            }
        ]
    }
    parsed = parse_items(payload, league="T", type_="SkillGem", endpoint=ITEM_ENDPOINT)
    row = parsed.rows[0]
    assert row.item_id == 7
    assert row.variant == "20/20"
    assert row.corrupted is True
    assert row.chaos_value == pytest.approx(30.0)


def test_committed_fixtures_still_parse():
    """Guards the fixtures against silent drift."""
    exchange = json.loads((FIXTURES / "exchange_chaos_primary.json").read_text())
    parsed = parse_exchange(
        exchange, league="T", type_="Currency", endpoint=EXCHANGE_ENDPOINT
    )
    assert len(parsed.rows) == len(REFERENCE_MARKET)

    stash = json.loads((FIXTURES / "stash_currency_reference.json").read_text())
    parsed_stash = parse_stash_currency(
        stash, league="T", type_="Currency", endpoint=STASH_ENDPOINT
    )
    assert len(parsed_stash.rows) == len(REFERENCE_MARKET)
