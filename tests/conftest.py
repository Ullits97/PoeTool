"""Shared fixtures.

The payload builders here construct markets whose true prices are stated up
front, so a test can assert that the tool recovers exactly the market it was
given. That is what makes the spread-direction test meaningful: the answer
is known by construction, not by inspection of the tool's own output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A spread of chaos values, deliberately straddling 1.0 so the reciprocal and
# direct readings are distinguishable on most lines.
REFERENCE_MARKET: dict[str, float] = {
    "Chaos Orb": 1.0,
    "Divine Orb": 200.0,
    "Exalted Orb": 15.0,
    "Orb of Annulment": 45.0,
    "Ancient Orb": 3.0,
    "Vaal Orb": 2.5,
    "Orb of Alteration": 0.05,
    "Orb of Fusing": 0.4,
    "Chromatic Orb": 0.02,
    "Mirror of Kalandra": 90000.0,
    "Awakened Sextant": 12.0,
    "Orb of Regret": 1.8,
    "Gemcutter's Prism": 4.5,
}

# Both sides of every quote sit this far from the mid, so the true spread is
# known exactly: (1.025 / 0.975) - 1 = 5.1282...%
ASK_MULTIPLIER = 1.025
BID_MULTIPLIER = 0.975
TRUE_SPREAD_PCT = ASK_MULTIPLIER / BID_MULTIPLIER - 1


def build_stash_currency(
    *,
    pay_is_ask: bool,
    pay_reciprocal: bool,
    receive_reciprocal: bool,
    market: dict[str, float] | None = None,
    listings: int = 40,
) -> dict:
    """Build a stash currency payload with a known, stated orientation.

    `pay_is_ask` says which named field carries the price you BUY at.
    The `*_reciprocal` flags say whether that side is quoted as units-per-
    chaos rather than chaos-per-unit. Every combination is a market the
    resolver must be able to recover without being told.
    """
    market = market or REFERENCE_MARKET
    lines = []
    for name, chaos in market.items():
        ask = chaos * ASK_MULTIPLIER
        bid = chaos * BID_MULTIPLIER
        pay_chaos = ask if pay_is_ask else bid
        receive_chaos = bid if pay_is_ask else ask
        lines.append(
            {
                "currencyTypeName": name,
                "detailsId": name.lower().replace(" ", "-").replace("'", ""),
                "chaosEquivalent": chaos,
                "pay": {
                    "value": (1.0 / pay_chaos) if pay_reciprocal else pay_chaos,
                    "count": 12,
                    "listing_count": listings,
                },
                "receive": {
                    "value": (1.0 / receive_chaos) if receive_reciprocal else receive_chaos,
                    "count": 14,
                    "listing_count": listings,
                },
                "paySparkLine": {"data": [0, 1.1, -0.4], "totalChange": 0.7},
                "receiveSparkLine": {"data": [0, 0.9, -0.2], "totalChange": 0.5},
            }
        )
    return {
        "lines": lines,
        "currencyDetails": [
            {"id": i, "name": name, "icon": "", "tradeId": name.lower()}
            for i, name in enumerate(market)
        ],
    }


def build_exchange(
    *,
    primary: str = "chaos",
    chaos_per_primary: float = 1.0,
    reciprocal: bool = False,
    market: dict[str, float] | None = None,
    volume_chaos: float = 5000.0,
    include_rates: bool = True,
) -> dict:
    """Build an exchange payload quoted in `primary`.

    `chaos_per_primary` is the true value of one primary unit in chaos, so a
    test can assert the parser recovers it from the payload alone.
    """
    market = market or REFERENCE_MARKET
    lines = []
    rates = {}
    for name, chaos in market.items():
        key = name.lower().replace(" ", "-").replace("'", "")
        primary_value = chaos / chaos_per_primary
        quoted = (1.0 / primary_value) if reciprocal else primary_value
        rates[key] = quoted
        lines.append(
            {
                "id": key,
                "primaryValue": quoted,
                "volumePrimaryValue": volume_chaos / chaos_per_primary,
                "maxVolumeCurrency": "divine",
                "maxVolumeRate": quoted * 1.01,
                "sparkline": {"data": [0, 1.0, 2.0], "totalChange": 2.0},
            }
        )
    core = {
        "primary": primary,
        "secondary": "divine",
        "items": {
            name.lower().replace(" ", "-").replace("'", ""): {"name": name, "icon": ""}
            for name in market
        },
    }
    if include_rates:
        core["rates"] = rates
    return {"lines": lines, "core": core}


def build_gem_payload(entries: list[dict]) -> dict:
    """entries: [{name, variant, corrupted, chaosValue, listingCount}]"""
    lines = []
    for i, entry in enumerate(entries):
        lines.append(
            {
                "id": 1000 + i,
                "name": entry["name"],
                "baseType": entry["name"],
                "detailsId": f"{entry['name'].lower().replace(' ', '-')}-{entry['variant']}",
                "variant": entry["variant"],
                "corrupted": entry.get("corrupted", False),
                "chaosValue": entry["chaosValue"],
                "divineValue": entry["chaosValue"] / 200.0,
                "count": entry.get("count", 50),
                "listingCount": entry.get("listingCount", 40),
                "links": 0,
                "icon": "",
                "sparkLine": {"data": [0, 1.0], "totalChange": 1.0},
            }
        )
    return {"lines": lines}


@pytest.fixture
def config_dict() -> dict:
    """A valid config, as parsed YAML, ready to be tweaked per-test."""
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) | {
        "app": {
            "user_agent": "poe-flip-tests/0.1 (contact: tests@poeflip.invalid)",
            "min_fetch_interval_minutes": 5,
        }
    }


@pytest.fixture
def write_config(tmp_path, config_dict):
    def _write(**overrides) -> Path:
        data = json.loads(json.dumps(config_dict))
        for section, values in overrides.items():
            if isinstance(values, dict) and isinstance(data.get(section), dict):
                data[section].update(values)
            else:
                data[section] = values
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        (tmp_path / "data").mkdir(exist_ok=True)
        return path

    return _write


@pytest.fixture
def cfg(write_config):
    from poeflip.config import load_config

    return load_config(write_config())
