"""The pay/receive direction — spec 2.2's highest-risk unknown.

These tests exist because getting the orientation backwards does not crash
anything: it silently inverts every spread and produces plausible numbers.
So each case builds a market whose true prices are fixed by construction and
asserts the resolver recovers them, in every combination of the two unknowns.
"""

from __future__ import annotations

import json

import pytest
from conftest import (
    ASK_MULTIPLIER,
    BID_MULTIPLIER,
    FIXTURES,
    REFERENCE_MARKET,
    TRUE_SPREAD_PCT,
    build_stash_currency,
)

from poeflip.direction import QuoteSample, resolve_orientation
from poeflip.errors import DirectionError
from poeflip.schema import parse_stash_currency

ENDPOINT = "/poe1/api/economy/stash/current/currency/overview"


def samples_from(payload: dict) -> list[QuoteSample]:
    return [
        QuoteSample(
            name=line["currencyTypeName"],
            chaos_equivalent=line["chaosEquivalent"],
            pay_value=line["pay"]["value"],
            receive_value=line["receive"]["value"],
        )
        for line in payload["lines"]
    ]


@pytest.mark.parametrize("pay_is_ask", [True, False])
@pytest.mark.parametrize("pay_reciprocal", [True, False])
@pytest.mark.parametrize("receive_reciprocal", [True, False])
def test_resolver_recovers_every_orientation(pay_is_ask, pay_reciprocal, receive_reciprocal):
    payload = build_stash_currency(
        pay_is_ask=pay_is_ask,
        pay_reciprocal=pay_reciprocal,
        receive_reciprocal=receive_reciprocal,
    )
    orientation = resolve_orientation(samples_from(payload), endpoint=ENDPOINT)

    assert orientation.pay.denomination == (
        "units_per_chaos" if pay_reciprocal else "chaos_per_unit"
    )
    assert orientation.receive.denomination == (
        "units_per_chaos" if receive_reciprocal else "chaos_per_unit"
    )
    assert orientation.ask_field == ("pay" if pay_is_ask else "receive")
    assert orientation.bid_field == ("receive" if pay_is_ask else "pay")


@pytest.mark.parametrize("pay_is_ask", [True, False])
@pytest.mark.parametrize("pay_reciprocal", [True, False])
@pytest.mark.parametrize("receive_reciprocal", [True, False])
def test_spread_is_the_true_spread_in_every_orientation(
    pay_is_ask, pay_reciprocal, receive_reciprocal
):
    """The spread must come out positive and correct however the feed quotes it."""
    payload = build_stash_currency(
        pay_is_ask=pay_is_ask,
        pay_reciprocal=pay_reciprocal,
        receive_reciprocal=receive_reciprocal,
    )
    orientation = resolve_orientation(samples_from(payload), endpoint=ENDPOINT)

    for line in payload["lines"]:
        spread = orientation.spread_pct(line["pay"]["value"], line["receive"]["value"])
        assert spread == pytest.approx(TRUE_SPREAD_PCT, rel=1e-9)
        assert spread > 0


def test_hand_verified_reference_case():
    """A single hand-computed case, worked through in full.

    Market by construction: a Divine Orb is worth 200c. You BUY one for
    200 * 1.025 = 205c and SELL one for 200 * 0.975 = 195c.

    The feed in this fixture quotes the buy side under `receive` in chaos per
    divine (205.0) and the sell side under `pay` in divines per chaos
    (1/195 = 0.00512820...). Nothing in the payload says which is which.

    The resolver must therefore conclude:
      * receive is chaos_per_unit, pay is units_per_chaos
      * receive is the ask, pay is the bid
      * spread = (205 - 195) / 195 = 5.1282%
    """
    payload = build_stash_currency(
        pay_is_ask=False, pay_reciprocal=True, receive_reciprocal=False
    )
    divine = next(l for l in payload["lines"] if l["currencyTypeName"] == "Divine Orb")

    # The fixture really does hold the numbers claimed above.
    assert divine["chaosEquivalent"] == 200.0
    assert divine["receive"]["value"] == pytest.approx(205.0)
    assert divine["pay"]["value"] == pytest.approx(1.0 / 195.0)

    orientation = resolve_orientation(samples_from(payload), endpoint=ENDPOINT)

    assert orientation.receive.denomination == "chaos_per_unit"
    assert orientation.pay.denomination == "units_per_chaos"
    assert orientation.ask_field == "receive"
    assert orientation.bid_field == "pay"

    # Both sides land back on the prices the market was built with.
    assert orientation.chaos_per_unit("receive", divine["receive"]["value"]) == pytest.approx(205.0)
    assert orientation.chaos_per_unit("pay", divine["pay"]["value"]) == pytest.approx(195.0)

    spread = orientation.spread_pct(divine["pay"]["value"], divine["receive"]["value"])
    assert spread == pytest.approx((205.0 - 195.0) / 195.0)
    assert spread == pytest.approx(0.051282, abs=1e-6)


def test_reference_fixture_on_disk_matches_the_reference_case():
    """The committed fixture is the same market, so it can't drift silently."""
    payload = json.loads(
        (FIXTURES / "stash_currency_reference.json").read_text(encoding="utf-8")
    )
    orientation = resolve_orientation(samples_from(payload), endpoint=ENDPOINT)
    assert orientation.ask_field == "receive"
    assert orientation.bid_field == "pay"

    divine = next(l for l in payload["lines"] if l["currencyTypeName"] == "Divine Orb")
    assert orientation.chaos_per_unit("receive", divine["receive"]["value"]) == pytest.approx(205.0)
    assert orientation.chaos_per_unit("pay", divine["pay"]["value"]) == pytest.approx(195.0)


def test_refuses_to_guess_when_evidence_is_thin():
    """Too few lines must raise, never fall back to an assumed convention."""
    tiny = {"Divine Orb": 200.0, "Exalted Orb": 15.0, "Vaal Orb": 2.5}
    payload = build_stash_currency(
        pay_is_ask=True, pay_reciprocal=False, receive_reciprocal=False, market=tiny
    )
    with pytest.raises(DirectionError) as excinfo:
        resolve_orientation(samples_from(payload), endpoint=ENDPOINT)
    assert "usable sample" in str(excinfo.value)
    assert ENDPOINT in str(excinfo.value)


def test_refuses_to_guess_when_neither_side_is_systematically_higher():
    """A market with no consistent bid/ask ordering is unresolvable, so it raises."""
    lines = []
    for i, (name, chaos) in enumerate(REFERENCE_MARKET.items()):
        # Alternate which side is dearer, destroying any systematic ordering.
        high, low = (ASK_MULTIPLIER, BID_MULTIPLIER) if i % 2 else (BID_MULTIPLIER, ASK_MULTIPLIER)
        lines.append(
            {
                "currencyTypeName": name,
                "chaosEquivalent": chaos,
                "pay": {"value": chaos * high, "count": 5, "listing_count": 20},
                "receive": {"value": chaos * low, "count": 5, "listing_count": 20},
            }
        )
    with pytest.raises(DirectionError) as excinfo:
        resolve_orientation(samples_from({"lines": lines}), endpoint=ENDPOINT)
    assert "ambiguous" in str(excinfo.value)


def test_parse_stash_currency_exposes_the_resolved_spread():
    payload = build_stash_currency(
        pay_is_ask=True, pay_reciprocal=False, receive_reciprocal=True
    )
    parsed = parse_stash_currency(
        payload, league="Test", type_="Currency", endpoint=ENDPOINT
    )
    assert parsed.orientation.ask_field == "pay"
    for row in parsed.rows:
        assert parsed.spread_pct(row) == pytest.approx(TRUE_SPREAD_PCT, rel=1e-9)
