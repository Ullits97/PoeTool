"""Gem corrupt EV, bankroll-gated, plus calibration against the user's log.

Spec 7.4 is the point of this module: corrupting is not an EV problem, it is
a **risk-of-ruin** problem. The value sits in the tail (21/20, Vaal
transforms) and at 200c the bankroll can be exhausted before the tail
arrives. So the ranked output is hard-gated on cost per attempt, and the
module's real deliverable at this bankroll is the calibration table — the
dataset, not the profit.

Where a required outcome line is missing from poe.ninja the gem is marked
`incomplete_data` and excluded. Nothing is substituted with a guess.
"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..config import VAAL_TRANSFORM_OUTCOME, Config, OutcomeRow
from ..schema import norm
from ..store import Store

VAAL_ORB_NAME = "Vaal Orb"

# Observed poe.ninja gem variant forms are "20", "20/20", "21/20", "20/23",
# optionally suffixed with 'c' for corrupted. Spec 2.2 flags this vocabulary
# as needing empirical confirmation: scripts/probe.py prints the distinct
# variants actually present so this pattern can be corrected against reality.
VARIANT_RE = re.compile(r"^\s*(\d+)\s*(?:/\s*(\d+))?\s*(c)?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class GemKey:
    name_key: str
    level: int
    quality: int
    corrupted: bool


@dataclass(frozen=True)
class OutcomeValue:
    outcome: str
    probability: float
    target_label: str
    chaos_value: float | None
    listing_count: int | None
    usable: bool
    reason: str = ""


@dataclass(frozen=True)
class CorruptRow:
    league: str
    gem_name: str
    input_variant: str
    input_cost_chaos: float
    vaal_cost_chaos: float
    cost_per_attempt: float
    has_vaal_variant: bool
    ev_chaos: float | None
    ev_pct: float | None
    max_affordable_attempts: int | None
    incomplete_data: bool
    bankroll_gate: str
    outcomes: tuple[OutcomeValue, ...] = ()

    @property
    def included(self) -> bool:
        return not self.incomplete_data and not self.bankroll_gate


@dataclass(frozen=True)
class CalibrationRow:
    outcome: str
    configured_probability: float | None
    observed_count: int
    observed_frequency: float | None
    sample_size: int
    note: str


@dataclass
class CorruptAnalysis:
    league: str
    rows: list[CorruptRow] = field(default_factory=list)
    filtered: list[CorruptRow] = field(default_factory=list)
    calibration: list[CalibrationRow] = field(default_factory=list)
    vaal_cost_chaos: float | None = None
    notes: list[str] = field(default_factory=list)


def parse_variant(variant: str | None) -> tuple[int, int] | None:
    """Turn a variant label into (level, quality), or None if unrecognised."""
    if not variant:
        return None
    match = VARIANT_RE.match(variant)
    if not match:
        return None
    level = int(match.group(1))
    quality = int(match.group(2)) if match.group(2) else 0
    return level, quality


def format_variant(level: int, quality: int) -> str:
    return f"{level}/{quality}"


def _index_gems(store: Store, cfg: Config, league: str) -> dict[GemKey, dict]:
    """Index every stored gem line by (name, level, quality, corrupted)."""
    index: dict[GemKey, dict] = {}
    for type_ in cfg.corrupt.item_types:
        for row in store.latest_items(league, type_):
            parsed = parse_variant(row["variant"])
            if parsed is None:
                continue
            level, quality = parsed
            key = GemKey(
                name_key=norm(row["name"]),
                level=level,
                quality=quality,
                corrupted=bool(row["corrupted"]),
            )
            existing = index.get(key)
            # Prefer the better-evidenced line when a key repeats across types.
            if existing is None or (row["listing_count"] or 0) > (existing["listing_count"] or 0):
                index[key] = dict(row)
    return index


def _vaal_orb_cost(store: Store, league: str) -> float | None:
    for row in store.latest_exchange(league, "Currency"):
        name = row["name"] or row["currency_id"]
        if norm(name) == norm(VAAL_ORB_NAME) and row["chaos_value"]:
            return float(row["chaos_value"])
    for row in store.latest_stash_currency(league, "Currency"):
        if norm(row["name"]) == norm(VAAL_ORB_NAME) and row["chaos_equivalent"]:
            return float(row["chaos_equivalent"])
    return None


def _resolve_outcome(
    index: dict[GemKey, dict],
    cfg: Config,
    gem_name: str,
    rule: OutcomeRow,
    has_vaal_variant: bool,
) -> OutcomeValue:
    if rule.outcome == VAAL_TRANSFORM_OUTCOME:
        target_name = f"Vaal {gem_name}"
    else:
        target_name = gem_name

    level = cfg.corrupt.input_level + rule.level_delta
    quality = cfg.corrupt.input_quality + rule.quality_delta
    label = f"{target_name} {format_variant(level, quality)} (corrupted)"

    row = index.get(GemKey(norm(target_name), level, quality, True))
    if row is None:
        return OutcomeValue(
            outcome=rule.outcome,
            probability=rule.probability,
            target_label=label,
            chaos_value=None,
            listing_count=None,
            usable=False,
            reason="no poe.ninja line for this outcome (thin market)",
        )

    listings = row["listing_count"]
    if listings is not None and listings < cfg.corrupt.min_outcome_listings:
        return OutcomeValue(
            outcome=rule.outcome,
            probability=rule.probability,
            target_label=label,
            chaos_value=row["chaos_value"],
            listing_count=listings,
            usable=False,
            reason=(
                f"only {listings} listing(s) < min_outcome_listings "
                f"{cfg.corrupt.min_outcome_listings}"
            ),
        )
    if row["chaos_value"] is None:
        return OutcomeValue(
            outcome=rule.outcome,
            probability=rule.probability,
            target_label=label,
            chaos_value=None,
            listing_count=listings,
            usable=False,
            reason="line has no chaosValue",
        )

    return OutcomeValue(
        outcome=rule.outcome,
        probability=rule.probability,
        target_label=label,
        chaos_value=float(row["chaos_value"]),
        listing_count=listings,
        usable=True,
    )


def analyse_corrupt(store: Store, cfg: Config, league: str) -> CorruptAnalysis:
    result = CorruptAnalysis(league=league)
    index = _index_gems(store, cfg, league)
    result.calibration = calibrate(cfg)

    if not index:
        result.notes.append(
            "no gem snapshots stored yet — run `python run.py fetch` first"
        )
        return result

    vaal_cost = _vaal_orb_cost(store, league)
    result.vaal_cost_chaos = vaal_cost
    if vaal_cost is None:
        result.notes.append(
            f"'{VAAL_ORB_NAME}' is absent from the stored Currency feeds, so cost per "
            "attempt cannot be computed. No EV is reported rather than assuming a price."
        )
        return result

    input_level = cfg.corrupt.input_level
    input_quality = cfg.corrupt.input_quality
    input_variant = format_variant(input_level, input_quality)
    budget = cfg.bankroll.max_corrupt_cost_chaos

    # Candidate inputs are the uncorrupted lines at the configured base variant.
    candidates = [
        row
        for key, row in index.items()
        if not key.corrupted and key.level == input_level and key.quality == input_quality
    ]

    for row in candidates:
        gem_name = row["name"]
        if gem_name.lower().startswith("vaal "):
            # A Vaal gem is already the transform product; not an input here.
            continue
        input_cost = row["chaos_value"]
        if input_cost is None or input_cost <= 0:
            continue

        has_vaal = any(
            k.name_key == norm(f"Vaal {gem_name}") for k in index
        )
        table = cfg.corrupt.table_for(has_vaal)
        outcomes = tuple(
            _resolve_outcome(index, cfg, gem_name, rule, has_vaal) for rule in table
        )

        cost_per_attempt = float(input_cost) + vaal_cost
        incomplete = any(not o.usable for o in outcomes)

        if incomplete:
            ev = ev_pct = None
        else:
            expected = sum(o.probability * (o.chaos_value or 0.0) for o in outcomes)
            ev = expected - cost_per_attempt
            ev_pct = ev / cost_per_attempt if cost_per_attempt > 0 else None

        gate = ""
        if cost_per_attempt > budget + 1e-9:
            gate = (
                f"cost/attempt {cost_per_attempt:,.1f}c exceeds bankroll limit "
                f"{budget:,.1f}c ({cfg.bankroll.max_corrupt_fraction:.0%} of "
                f"{cfg.bankroll.total_chaos:,.0f}c)"
            )

        corrupt_row = CorruptRow(
            league=league,
            gem_name=gem_name,
            input_variant=input_variant,
            input_cost_chaos=float(input_cost),
            vaal_cost_chaos=vaal_cost,
            cost_per_attempt=cost_per_attempt,
            has_vaal_variant=has_vaal,
            ev_chaos=ev,
            ev_pct=ev_pct,
            max_affordable_attempts=(
                int(math.floor(cfg.bankroll.total_chaos / cost_per_attempt))
                if cost_per_attempt > 0
                else None
            ),
            incomplete_data=incomplete,
            bankroll_gate=gate,
            outcomes=outcomes,
        )
        (result.rows if corrupt_row.included else result.filtered).append(corrupt_row)

    result.rows.sort(key=lambda r: (r.ev_pct is not None, r.ev_pct or 0.0), reverse=True)
    result.filtered.sort(key=lambda r: r.gem_name)
    return result


def exclusion_reason(row: CorruptRow) -> str:
    """Human-readable reason a gem left the ranked output."""
    reasons: list[str] = []
    if row.bankroll_gate:
        reasons.append(row.bankroll_gate)
    if row.incomplete_data:
        missing = [f"{o.outcome}: {o.reason}" for o in row.outcomes if not o.usable]
        reasons.append("incomplete_data — " + "; ".join(missing))
    return " | ".join(reasons)


# --------------------------------------------------------------------------
# Calibration (spec 7.5)
# --------------------------------------------------------------------------
CORRUPT_LOG_COLUMNS = (
    "date,gem_name,input_variant,input_cost_chaos,vaal_cost_chaos,"
    "outcome_variant,outcome_corrupted,realised_value_chaos,notes"
)


def calibrate(cfg: Config, path: Path | None = None) -> list[CalibrationRow]:
    """Compare logged outcomes against the configured probabilities.

    The file is read, never written. At a 200c bankroll the expected profit
    from corrupting is close to noise, so this table — not the EV ranking —
    is what makes the model non-generic once the bankroll grows.
    """
    log_path = path or cfg.corrupt_log_path
    configured = {r.outcome: r.probability for r in cfg.corrupt.with_vaal_variant}

    if not log_path.exists():
        return [
            CalibrationRow(
                outcome=outcome,
                configured_probability=prob,
                observed_count=0,
                observed_frequency=None,
                sample_size=0,
                note=f"no log yet — create {log_path} with header: {CORRUPT_LOG_COLUMNS}",
            )
            for outcome, prob in configured.items()
        ]

    observed: Counter[str] = Counter()
    with log_path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            bucket = _classify(cfg, record)
            if bucket:
                observed[bucket] += 1

    total = sum(observed.values())
    buckets = list(configured.keys()) + [b for b in observed if b not in configured]
    rows: list[CalibrationRow] = []
    for outcome in buckets:
        count = observed.get(outcome, 0)
        rows.append(
            CalibrationRow(
                outcome=outcome,
                configured_probability=configured.get(outcome),
                observed_count=count,
                observed_frequency=(count / total) if total else None,
                sample_size=total,
                note=_sample_note(total),
            )
        )
    return rows


def _classify(cfg: Config, record: dict[str, str]) -> str | None:
    """Map one logged attempt onto an outcome bucket."""
    gem = (record.get("gem_name") or "").strip()
    outcome_variant = (record.get("outcome_variant") or "").strip()
    parsed = parse_variant(outcome_variant)
    if not gem or parsed is None:
        return None
    level, quality = parsed

    # A logged row whose gem name gained a Vaal prefix is a transform.
    if (record.get("notes") or "").lower().find("vaal transform") >= 0:
        return VAAL_TRANSFORM_OUTCOME

    level_delta = level - cfg.corrupt.input_level
    quality_delta = quality - cfg.corrupt.input_quality
    for rule in cfg.corrupt.with_vaal_variant:
        if rule.outcome == VAAL_TRANSFORM_OUTCOME:
            continue
        if rule.level_delta == level_delta and rule.quality_delta == quality_delta:
            return rule.outcome
    if level_delta == 0 and quality_delta == 0:
        return "no_change"
    return f"other ({level_delta:+d} level, {quality_delta:+d} quality)"


def _sample_note(total: int) -> str:
    if total == 0:
        return "no attempts logged yet"
    if total < 30:
        return (
            f"{total} attempt(s) — far too few to conclude anything; "
            "treat any gap from the configured probability as noise"
        )
    if total < 200:
        return f"{total} attempts — indicative only, wide error bars remain"
    return f"{total} attempts — large enough to start trusting the direction of any gap"
