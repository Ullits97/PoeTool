"""Typed parsers for poe.ninja economy payloads.

Two rules govern this module:

* No domain logic. It turns JSON into typed rows and nothing else.
* No silent defaults. Where spec 2.2 leaves a key name genuinely unknown we
  try a short list of candidates and, if none match, raise `SchemaError`
  naming the endpoint and listing the keys that *were* present. A renamed
  upstream field must never become a zero or an empty sheet.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .direction import Orientation, QuoteSample, resolve_orientation
from .errors import SchemaError

# --------------------------------------------------------------------------
# Candidate key names for the sub-structures spec 2.2 leaves unspecified.
# Ordered most-likely-first. probe.py prints the keys actually observed so
# docs/SCHEMA.md can record the truth rather than the guess.
# --------------------------------------------------------------------------
QUOTE_VALUE_KEYS = ("value", "rate", "ratio", "primaryValue")
QUOTE_COUNT_KEYS = ("count", "data_point_count", "dataPointCount", "sampleCount", "samples")
QUOTE_LISTING_KEYS = ("listing_count", "listingCount", "listings", "listingsCount")

CHAOS_TOKENS = frozenset({"chaos", "chaosorb", "chaosorbs"})

# Numeraire conversion guards (see resolve_primary_conversion).
MIN_CONVERSION_POINTS = 8
# Log-space spread of the implied factor; ~exp(0.15)-1 = 16% scatter.
MAX_CONVERSION_DISPERSION = 0.15
# Allowed disagreement between the fitted factor and the payload's own anchor.
MAX_ANCHOR_DISAGREEMENT = 0.10


def norm(text: Any) -> str:
    """Normalise an id/name for cross-feed matching: lowercase alphanumerics."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def is_chaos(token: Any) -> bool:
    return norm(token) in CHAOS_TOKENS


# --------------------------------------------------------------------------
# Low-level accessors
# --------------------------------------------------------------------------
def _require_mapping(payload: Any, endpoint: str, what: str = "payload") -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaError(
            f"expected {what} to be a JSON object, got {type(payload).__name__}",
            endpoint=endpoint,
            payload=payload,
        )
    return payload


def _require_lines(payload: Any, endpoint: str) -> Sequence[Any]:
    body = _require_mapping(payload, endpoint)
    lines = body.get("lines")
    if lines is None:
        raise SchemaError(
            "payload has no 'lines' array — the route may have been renamed or restructured. "
            f"Top-level keys present: {sorted(body.keys())}",
            endpoint=endpoint,
            payload=payload,
        )
    if not isinstance(lines, list):
        raise SchemaError(
            f"'lines' must be an array, got {type(lines).__name__}",
            endpoint=endpoint,
            payload=payload,
        )
    return lines


def _num(value: Any) -> float | None:
    """Coerce to float, or None. Booleans are not numbers here."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else int(n)


def pick(
    mapping: Mapping[str, Any],
    candidates: Sequence[str],
    *,
    what: str,
    endpoint: str,
    required: bool,
) -> Any:
    """Return the first present candidate key, or fail loudly if required."""
    for key in candidates:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    if not required:
        return None
    raise SchemaError(
        f"could not find {what}: none of {list(candidates)} are present. "
        f"Keys actually present: {sorted(mapping.keys())}. "
        "Run scripts/probe.py to capture the current shape and update "
        "QUOTE_* candidates in src/poeflip/schema.py.",
        endpoint=endpoint,
        payload=dict(mapping),
    )


# --------------------------------------------------------------------------
# Leagues
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class League:
    id: str
    name: str


def parse_leagues(payload: Any, *, endpoint: str) -> list[League]:
    """Parse /economy/leagues. First entry is the current challenge league."""
    items = payload
    if isinstance(payload, Mapping):
        # Tolerate a wrapped array without assuming which wrapper key is used.
        for key in ("leagues", "lines", "data", "result"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    if not isinstance(items, list) or not items:
        raise SchemaError(
            "expected a non-empty array of leagues",
            endpoint=endpoint,
            payload=payload,
        )

    leagues: list[League] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            raise SchemaError(
                f"league entry must be an object, got {type(entry).__name__}",
                endpoint=endpoint,
                payload=entry,
            )
        league_id = entry.get("id") or entry.get("name")
        if not league_id:
            raise SchemaError(
                f"league entry has neither 'id' nor 'name'. Keys: {sorted(entry.keys())}",
                endpoint=endpoint,
                payload=entry,
            )
        leagues.append(League(id=str(league_id), name=str(entry.get("name") or league_id)))
    return leagues


# --------------------------------------------------------------------------
# Exchange (in-game Currency Exchange)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ExchangeRow:
    currency_id: str
    name: str | None
    primary_value: float | None
    volume_primary: float | None
    max_volume_currency: str | None
    max_volume_rate: float | None
    chaos_value: float | None

    @property
    def volume_chaos(self) -> float | None:
        """Trading volume expressed in chaos.

        `volume_primary` is quoted in the payload's numeraire, so it is
        rescaled by the same factor that produced `chaos_value`.
        """
        if self.volume_primary is None or self.primary_value in (None, 0):
            return None
        if self.chaos_value is None:
            return None
        return self.volume_primary * (self.chaos_value / self.primary_value)


@dataclass(frozen=True)
class PrimaryConversion:
    """How the payload's numeraire was converted to chaos, and on what basis."""

    primary: str
    chaos_per_primary: float
    method: str
    # True when the payload quotes `primaryValue` as units-per-primary rather
    # than primary-per-unit. Normalising here keeps the stored snapshot in one
    # consistent convention whatever upstream does.
    reciprocal: bool = False

    def normalise_primary_value(self, raw: float | None) -> float | None:
        """Re-express a raw quote as the value of one unit, in primary units."""
        if raw is None:
            return None
        if not self.reciprocal:
            return raw
        return 1.0 / raw if raw != 0 else None

    def to_chaos(self, raw: float | None) -> float | None:
        normalised = self.normalise_primary_value(raw)
        return None if normalised is None else normalised * self.chaos_per_primary


@dataclass(frozen=True)
class ExchangeParse:
    league: str
    type: str
    conversion: PrimaryConversion
    rows: tuple[ExchangeRow, ...]


def resolve_primary_conversion(
    core: Mapping[str, Any],
    lines: Iterable[Mapping[str, Any]],
    *,
    endpoint: str,
    chaos_reference: Mapping[str, float] | None = None,
) -> PrimaryConversion:
    """Determine how many chaos one unit of `core.primary` is worth.

    Spec 6.1: the numeraire is read from the payload every run and never
    assumed.

    1. `core.primary` is already chaos -> factor 1.0, no ambiguity.
    2. Otherwise the factor is *fitted*: `primaryValue` is regressed against
       an independent absolute reference (the stash feed's `chaosEquivalent`)
       under both possible readings of `primaryValue` — primary-per-unit and
       units-per-primary — and the tighter fit wins. The factor is then
       cross-checked against the payload's own chaos anchor (`core.rates`, or
       the chaos line) and a disagreement is an error, not a shrug.
    3. No reference available and primary is not chaos -> abort, rather than
       emit mis-scaled numbers.
    """
    primary = core.get("primary")
    if primary is None:
        raise SchemaError(
            f"'core.primary' is missing; cannot establish the numeraire. "
            f"core keys: {sorted(core.keys())}",
            endpoint=endpoint,
            payload=dict(core),
        )

    if is_chaos(primary):
        return PrimaryConversion(
            primary=str(primary),
            chaos_per_primary=1.0,
            method="core.primary is chaos; values are already in chaos",
        )

    if not chaos_reference:
        raise SchemaError(
            f"'core.primary' is {primary!r} (not chaos). Converting requires an independent "
            "chaos reference to establish whether 'primaryValue' is primary-per-unit or "
            "units-per-primary; none was supplied. Fetch the stash currency feed in the "
            "same run, or pin a league whose exchange numeraire is chaos. "
            "Aborting rather than guessing (spec 6.1).",
            endpoint=endpoint,
            payload={"primary": primary},
        )

    quotes: dict[str, float] = {}
    for ln in lines:
        value = _num(ln.get("primaryValue"))
        if ln.get("id") is not None and value is not None and value > 0:
            quotes[norm(ln["id"])] = value

    reference = {norm(k): v for k, v in chaos_reference.items() if v and v > 0}

    # For each shared currency, the factor implied under each reading.
    direct: list[float] = []      # primaryValue is primary-per-unit
    reciprocal: list[float] = []  # primaryValue is units-per-primary
    for key, quote in quotes.items():
        ref = reference.get(key)
        if ref is None:
            continue
        direct.append(ref / quote)
        reciprocal.append(ref * quote)

    if len(direct) < MIN_CONVERSION_POINTS:
        raise SchemaError(
            f"'core.primary' is {primary!r} (not chaos) and only {len(direct)} currency "
            f"overlaps with the chaos reference (need {MIN_CONVERSION_POINTS}) — not enough "
            "to establish the conversion. Aborting rather than guessing (spec 6.1).",
            endpoint=endpoint,
            payload={"primary": primary, "overlap": sorted(set(quotes) & set(reference))[:20]},
        )

    # The correct reading yields a consistent factor across every currency;
    # the wrong one scatters. Dispersion in log space picks the winner.
    direct_spread = _log_dispersion(direct)
    reciprocal_spread = _log_dispersion(reciprocal)
    use_reciprocal = reciprocal_spread < direct_spread
    candidates = reciprocal if use_reciprocal else direct
    factor = statistics.median(candidates)
    dispersion = min(direct_spread, reciprocal_spread)

    if dispersion > MAX_CONVERSION_DISPERSION:
        raise SchemaError(
            f"the exchange numeraire could not be pinned down: the implied chaos-per-"
            f"{primary!r} factor scatters by {math.expm1(dispersion):.1%} across "
            f"{len(candidates)} currencies under both readings of 'primaryValue'. "
            "Emitting these values would mis-scale every number, so this run stops.",
            endpoint=endpoint,
            payload={"primary": primary, "median_factor": factor},
        )

    fitted_as = "units-per-primary" if use_reciprocal else "primary-per-unit"
    method = (
        f"primary={primary!r}; 'primaryValue' fitted as {fitted_as} against "
        f"{len(candidates)} stash chaosEquivalent reference points "
        f"(1 {primary} = {factor:,.4g} chaos, spread {math.expm1(dispersion):.2%})"
    )

    # Cross-check against the payload's own anchor, when it carries one.
    anchor, anchor_source = _chaos_anchor(core, quotes)
    if anchor is not None:
        implied = anchor if use_reciprocal else 1.0 / anchor
        if implied > 0 and abs(math.log(factor / implied)) > MAX_ANCHOR_DISAGREEMENT:
            raise SchemaError(
                f"the fitted numeraire factor ({factor:,.4g} chaos per {primary}) disagrees "
                f"with the payload's own anchor from {anchor_source} ({implied:,.4g}). "
                "Two independent readings of the same payload should not conflict; "
                "aborting rather than picking one.",
                endpoint=endpoint,
                payload={"primary": primary, "fitted": factor, "anchor": implied},
            )
        method += f"; cross-checked against {anchor_source}"

    return PrimaryConversion(
        primary=str(primary),
        chaos_per_primary=factor,
        method=method,
        reciprocal=use_reciprocal,
    )


def _log_dispersion(values: Sequence[float]) -> float:
    """Scale-free spread of a set of candidate factors."""
    logs = [math.log(v) for v in values if v > 0]
    if len(logs) < 2:
        return float("inf")
    return statistics.pstdev(logs)


def _chaos_anchor(
    core: Mapping[str, Any], quotes: Mapping[str, float]
) -> tuple[float | None, str]:
    """The payload's own quote for chaos, from core.rates or the chaos line."""
    rates = core.get("rates")
    if isinstance(rates, Mapping):
        for key, value in rates.items():
            if is_chaos(key):
                num = _num(value)
                if num and num > 0:
                    return num, "core.rates"
    for key, value in quotes.items():
        if key in CHAOS_TOKENS and value > 0:
            return value, "the chaos line's primaryValue"
    return None, ""


def parse_exchange(
    payload: Any,
    *,
    league: str,
    type_: str,
    endpoint: str,
    chaos_reference: Mapping[str, float] | None = None,
) -> ExchangeParse:
    lines = _require_lines(payload, endpoint)
    body = _require_mapping(payload, endpoint)
    core = body.get("core")
    if not isinstance(core, Mapping):
        raise SchemaError(
            f"payload has no 'core' object, so the numeraire cannot be established. "
            f"Top-level keys: {sorted(body.keys())}",
            endpoint=endpoint,
            payload=payload,
        )

    mappings = [ln for ln in lines if isinstance(ln, Mapping)]
    conversion = resolve_primary_conversion(
        core, mappings, endpoint=endpoint, chaos_reference=chaos_reference
    )
    names = _item_names(core)

    rows: list[ExchangeRow] = []
    for ln in mappings:
        currency_id = ln.get("id")
        if currency_id is None:
            raise SchemaError(
                f"exchange line has no 'id'. Keys: {sorted(ln.keys())}",
                endpoint=endpoint,
                payload=ln,
            )
        raw_primary_value = _num(ln.get("primaryValue"))
        # Stored in one consistent convention (primary per unit), so that the
        # numeraire factor stays recoverable offline as chaos_value/primary_value.
        primary_value = conversion.normalise_primary_value(raw_primary_value)
        chaos_value = conversion.to_chaos(raw_primary_value)
        rows.append(
            ExchangeRow(
                currency_id=str(currency_id),
                name=names.get(str(currency_id)),
                primary_value=primary_value,
                volume_primary=_num(ln.get("volumePrimaryValue")),
                max_volume_currency=(
                    str(ln["maxVolumeCurrency"])
                    if ln.get("maxVolumeCurrency") is not None
                    else None
                ),
                max_volume_rate=_num(ln.get("maxVolumeRate")),
                chaos_value=chaos_value,
            )
        )

    return ExchangeParse(league=league, type=type_, conversion=conversion, rows=tuple(rows))


def _item_names(core: Mapping[str, Any]) -> dict[str, str]:
    """Extract id -> display name from core.items, tolerating either shape."""
    items = core.get("items")
    out: dict[str, str] = {}
    if isinstance(items, Mapping):
        for key, meta in items.items():
            if isinstance(meta, Mapping):
                name = meta.get("name") or meta.get("text")
                if name:
                    out[str(key)] = str(name)
            elif isinstance(meta, str):
                out[str(key)] = meta
    elif isinstance(items, list):
        for meta in items:
            if isinstance(meta, Mapping) and meta.get("id") is not None and meta.get("name"):
                out[str(meta["id"])] = str(meta["name"])
    return out


# --------------------------------------------------------------------------
# Stash currency listings
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class StashCurrencyRow:
    name: str
    details_id: str | None
    chaos_equivalent: float | None
    pay_value: float | None
    pay_count: int | None
    pay_listings: int | None
    receive_value: float | None
    receive_count: int | None
    receive_listings: int | None


@dataclass(frozen=True)
class StashCurrencyParse:
    league: str
    type: str
    orientation: Orientation
    rows: tuple[StashCurrencyRow, ...]

    def spread_pct(self, row: StashCurrencyRow) -> float | None:
        return self.orientation.spread_pct(row.pay_value, row.receive_value)


def _quote(
    block: Any, *, side: str, endpoint: str
) -> tuple[float | None, int | None, int | None]:
    if block is None:
        return None, None, None
    if not isinstance(block, Mapping):
        raise SchemaError(
            f"'{side}' should be an object of data points, got {type(block).__name__}",
            endpoint=endpoint,
            payload=block,
        )
    value = _num(pick(block, QUOTE_VALUE_KEYS, what=f"the '{side}' rate", endpoint=endpoint, required=True))
    count = _int(pick(block, QUOTE_COUNT_KEYS, what=f"the '{side}' sample count", endpoint=endpoint, required=False))
    listings = _int(
        pick(block, QUOTE_LISTING_KEYS, what=f"the '{side}' listing count", endpoint=endpoint, required=False)
    )
    return value, count, listings


def parse_stash_currency(
    payload: Any, *, league: str, type_: str, endpoint: str
) -> StashCurrencyParse:
    lines = _require_lines(payload, endpoint)

    rows: list[StashCurrencyRow] = []
    for ln in lines:
        if not isinstance(ln, Mapping):
            raise SchemaError(
                f"currency line must be an object, got {type(ln).__name__}",
                endpoint=endpoint,
                payload=ln,
            )
        name = ln.get("currencyTypeName") or ln.get("name")
        if not name:
            raise SchemaError(
                f"currency line has no 'currencyTypeName'. Keys: {sorted(ln.keys())}",
                endpoint=endpoint,
                payload=ln,
            )
        pay_value, pay_count, pay_listings = _quote(ln.get("pay"), side="pay", endpoint=endpoint)
        rec_value, rec_count, rec_listings = _quote(
            ln.get("receive"), side="receive", endpoint=endpoint
        )
        rows.append(
            StashCurrencyRow(
                name=str(name),
                details_id=str(ln["detailsId"]) if ln.get("detailsId") is not None else None,
                chaos_equivalent=_num(ln.get("chaosEquivalent")),
                pay_value=pay_value,
                pay_count=pay_count,
                pay_listings=pay_listings,
                receive_value=rec_value,
                receive_count=rec_count,
                receive_listings=rec_listings,
            )
        )

    samples = [
        QuoteSample(
            name=r.name,
            chaos_equivalent=r.chaos_equivalent,
            pay_value=r.pay_value,
            receive_value=r.receive_value,
        )
        for r in rows
        if r.chaos_equivalent is not None
    ]
    orientation = resolve_orientation(samples, endpoint=endpoint)
    return StashCurrencyParse(
        league=league, type=type_, orientation=orientation, rows=tuple(rows)
    )


# --------------------------------------------------------------------------
# Stash item listings
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ItemRow:
    item_id: int
    name: str
    base_type: str | None
    details_id: str | None
    variant: str | None
    corrupted: bool
    links: int | None
    chaos_value: float | None
    divine_value: float | None
    count: int | None
    listing_count: int | None


@dataclass(frozen=True)
class ItemParse:
    league: str
    type: str
    rows: tuple[ItemRow, ...]


def parse_items(payload: Any, *, league: str, type_: str, endpoint: str) -> ItemParse:
    lines = _require_lines(payload, endpoint)

    rows: list[ItemRow] = []
    for index, ln in enumerate(lines):
        if not isinstance(ln, Mapping):
            raise SchemaError(
                f"item line must be an object, got {type(ln).__name__}",
                endpoint=endpoint,
                payload=ln,
            )
        name = ln.get("name")
        if not name:
            raise SchemaError(
                f"item line has no 'name'. Keys: {sorted(ln.keys())}",
                endpoint=endpoint,
                payload=ln,
            )
        item_id = _int(ln.get("id"))
        rows.append(
            ItemRow(
                # `id` is the upstream row identity; fall back to position so a
                # missing id cannot collide two different gems in the DB.
                item_id=item_id if item_id is not None else -(index + 1),
                name=str(name),
                base_type=str(ln["baseType"]) if ln.get("baseType") is not None else None,
                details_id=str(ln["detailsId"]) if ln.get("detailsId") is not None else None,
                variant=str(ln["variant"]) if ln.get("variant") is not None else None,
                corrupted=bool(ln.get("corrupted")),
                links=_int(ln.get("links")),
                chaos_value=_num(ln.get("chaosValue")),
                divine_value=_num(ln.get("divineValue")),
                count=_int(ln.get("count")),
                listing_count=_int(ln.get("listingCount")),
            )
        )
    return ItemParse(league=league, type=type_, rows=tuple(rows))
