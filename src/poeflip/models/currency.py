"""Currency Exchange order recommendations and cross-venue candidates.

No HTTP in here. Everything is computed from snapshots already in SQLite,
so `analyse` and `export` work with the network disconnected (spec 12.4).

Two outputs:

* **A — Exchange orders** (spec 6.2). The primary one. Standing orders on
  the in-game Currency Exchange fill asynchronously while the user plays,
  which is what makes a ~200c bankroll workable at all: the constraint is
  order fill rate, not margin.
* **B — Cross-venue candidates** (spec 6.3). Strictly a *hypothesis to
  check in-game*. The exchange figure is an aggregate observed rate and the
  stash figure is a listing price; neither is a fill.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import Config
from ..direction import Orientation, QuoteSample, resolve_orientation
from ..errors import DirectionError
from ..schema import norm
from ..store import Store, window_start

# A trend needs two points to have a direction; volatility needs three
# before a standard deviation says anything at all. Below these the columns
# are NULL, never 0 and never fabricated (spec 12.5).
MIN_TREND_POINTS = 2
MIN_VOLATILITY_POINTS = 3


@dataclass(frozen=True)
class OrderRow:
    league: str
    type: str
    currency_id: str
    name: str
    chaos_value: float
    volume_chaos: float | None
    trend_pct: float | None
    volatility: float | None
    suggested_buy_rate: float
    suggested_sell_rate: float
    units: int
    capital_required: float
    margin_pct: float
    expected_fills_per_day: float | None
    score: float | None
    observations: int | None
    history_points: int
    liquidity_ok: bool
    filter_reason: str = ""


@dataclass(frozen=True)
class CrossVenueRow:
    league: str
    name: str
    exchange_chaos: float
    stash_chaos: float
    divergence_pct: float
    stash_spread_pct: float | None
    stash_observations: int | None


@dataclass
class CurrencyAnalysis:
    league: str
    orders: list[OrderRow] = field(default_factory=list)
    filtered: list[OrderRow] = field(default_factory=list)
    cross_venue: list[CrossVenueRow] = field(default_factory=list)
    orientation: Orientation | None = None
    orientation_error: str | None = None
    notes: list[str] = field(default_factory=list)


def expected_fills_per_day(
    volume_chaos: float | None, position_chaos: float, cap: float
) -> float | None:
    """Estimate how often a standing order of this size can round-trip in a day.

    Heuristic, and worth stating its assumptions plainly:

    * `volumePrimaryValue` is treated as a **daily** turnover figure. poe.ninja
      does not document the window it covers; if it is in fact shorter, this
      over-estimates fills, and every ranking built on it is optimistic.
    * A round trip needs two fills (one buy, one sell), hence the /2.
    * A single trader captures only a fraction of total turnover. No share
      factor is applied here beyond the cap, so treat the result as an upper
      bound on throughput rather than an expectation.
    * The result is capped by `currency.max_fills_per_day` because the real
      binding constraint at small size is attention and order placement, not
      market depth.

    Returns None when volume is unknown — never 0, which would read as a
    measured "this never fills".
    """
    if volume_chaos is None or position_chaos <= 0:
        return None
    if volume_chaos <= 0:
        return 0.0
    return min(volume_chaos / position_chaos / 2.0, cap)


def _trend_and_volatility(
    history: list[tuple[str, float]]
) -> tuple[float | None, float | None, int]:
    """Trailing-window trend and volatility, or (None, None) when too thin."""
    values = [v for _, v in history if v is not None and v > 0]
    n = len(values)
    trend = None
    if n >= MIN_TREND_POINTS and values[0] > 0:
        trend = (values[-1] - values[0]) / values[0]
    volatility = statistics.stdev(values) if n >= MIN_VOLATILITY_POINTS else None
    return trend, volatility, n


def _stash_index(store: Store, league: str) -> tuple[dict, Orientation | None, str | None]:
    """Latest stash currency rows keyed by normalised name, plus the resolved orientation.

    The orientation is re-derived from the stored snapshot rather than
    carried over from fetch time, so `analyse` stays fully offline.
    """
    rows: list = []
    for type_ in ("Currency", "Fragment"):
        rows.extend(store.latest_stash_currency(league, type_))

    index = {norm(r["name"]): r for r in rows}
    if not rows:
        return index, None, "no stash currency snapshots stored yet"

    samples = [
        QuoteSample(
            name=r["name"],
            chaos_equivalent=r["chaos_equivalent"],
            pay_value=r["pay_value"],
            receive_value=r["receive_value"],
        )
        for r in rows
        if r["chaos_equivalent"] is not None
    ]
    try:
        orientation = resolve_orientation(samples, endpoint="snap_stash_currency (stored)")
    except DirectionError as exc:
        # Spreads are omitted rather than guessed; the rest of the analysis
        # does not depend on the orientation, so it continues.
        return index, None, str(exc).splitlines()[0]
    return index, orientation, None


def _observations(row) -> int | None:
    """Best available observation/listing count for a stash currency row."""
    candidates = [
        row["pay_listings"], row["receive_listings"],
        row["pay_count"], row["receive_count"],
    ]
    present = [c for c in candidates if c is not None]
    return max(present) if present else None


def analyse_currency(store: Store, cfg: Config, league: str, *, now: datetime | None = None) -> CurrencyAnalysis:
    now = now or datetime.now(timezone.utc)
    result = CurrencyAnalysis(league=league)

    stash_by_name, orientation, orientation_note = _stash_index(store, league)
    result.orientation = orientation
    result.orientation_error = orientation_note
    if orientation_note:
        result.notes.append(f"stash spread column omitted: {orientation_note}")

    since = window_start(cfg.currency.trend_window_hours, now=now)
    max_position = cfg.bankroll.max_position_chaos
    offset = cfg.currency.order_offset_pct
    # Round-trip margin implied by quoting both sides `offset` away from mid.
    margin_pct = (2 * offset) / (1 - offset) if offset < 1 else 0.0

    seen_cross: set[str] = set()

    for type_ in cfg.currency.watchlist_types:
        latest = store.latest_exchange(league, type_)
        if not latest:
            result.notes.append(f"no exchange snapshot stored for type '{type_}'")
            continue
        history = store.exchange_history_all(league, type_, since)

        for row in latest:
            chaos_value = row["chaos_value"]
            name = row["name"] or row["currency_id"]
            if chaos_value is None or chaos_value <= 0:
                continue

            volume_chaos = _volume_chaos(row)
            trend, volatility, points = _trend_and_volatility(
                history.get(row["currency_id"], [])
            )

            units = int(math.floor(max_position / chaos_value))
            # If a single unit is unaffordable, the honest capital requirement
            # is the price of that one unit — which is what fails the gate.
            capital_required = units * chaos_value if units >= 1 else chaos_value

            stash_row = stash_by_name.get(norm(name)) or stash_by_name.get(
                norm(row["currency_id"])
            )
            observations = _observations(stash_row) if stash_row is not None else None

            fills = expected_fills_per_day(
                volume_chaos, capital_required, cfg.currency.max_fills_per_day
            )
            # Spec 6.5: flipping is throughput-limited, not margin-limited.
            score = (
                margin_pct * capital_required * fills if fills is not None else None
            )

            reasons = _gate_reasons(
                cfg,
                volume_chaos=volume_chaos,
                observations=observations,
                capital_required=capital_required,
                max_position=max_position,
                units=units,
            )

            order = OrderRow(
                league=league,
                type=type_,
                currency_id=row["currency_id"],
                name=name,
                chaos_value=chaos_value,
                volume_chaos=volume_chaos,
                trend_pct=trend,
                volatility=volatility,
                suggested_buy_rate=chaos_value * (1 - offset),
                suggested_sell_rate=chaos_value * (1 + offset),
                units=units,
                capital_required=capital_required,
                margin_pct=margin_pct,
                expected_fills_per_day=fills,
                score=score,
                observations=observations,
                history_points=points,
                liquidity_ok=not reasons,
                filter_reason="; ".join(reasons),
            )
            (result.orders if order.liquidity_ok else result.filtered).append(order)

            # -- cross-venue candidate ------------------------------------
            if stash_row is not None and stash_row["chaos_equivalent"]:
                key = norm(name)
                if key not in seen_cross:
                    seen_cross.add(key)
                    stash_chaos = float(stash_row["chaos_equivalent"])
                    spread = None
                    if orientation is not None:
                        spread = orientation.spread_pct(
                            stash_row["pay_value"], stash_row["receive_value"]
                        )
                    result.cross_venue.append(
                        CrossVenueRow(
                            league=league,
                            name=name,
                            exchange_chaos=chaos_value,
                            stash_chaos=stash_chaos,
                            divergence_pct=(stash_chaos - chaos_value) / chaos_value,
                            stash_spread_pct=spread,
                            stash_observations=observations,
                        )
                    )

    # Rank by expected chaos return per unit of capital deployed, not by margin.
    result.orders.sort(key=lambda r: (r.score is not None, r.score or 0.0), reverse=True)
    result.filtered.sort(key=lambda r: r.name)
    result.cross_venue.sort(key=lambda r: abs(r.divergence_pct), reverse=True)
    return result


def _volume_chaos(row) -> float | None:
    """Rescale stored primary-denominated volume into chaos.

    `chaos_value / primary_value` is the numeraire conversion factor that was
    resolved at fetch time, recovered here from the stored row so no extra
    column or network call is needed.
    """
    volume_primary = row["volume_primary"]
    primary_value = row["primary_value"]
    chaos_value = row["chaos_value"]
    if volume_primary is None or not primary_value or chaos_value is None:
        return None
    return volume_primary * (chaos_value / primary_value)


def _gate_reasons(
    cfg: Config,
    *,
    volume_chaos: float | None,
    observations: int | None,
    capital_required: float,
    max_position: float,
    units: int,
) -> list[str]:
    """Hard liquidity gates (spec 6.4). A failing row leaves the ranked output.

    A 40% margin on something with three listings is not a trade.
    """
    reasons: list[str] = []

    if volume_chaos is None:
        reasons.append("volume unknown (no volumePrimaryValue on this line)")
    elif volume_chaos < cfg.currency.min_volume_chaos:
        reasons.append(
            f"volume {volume_chaos:,.0f}c < min_volume_chaos {cfg.currency.min_volume_chaos:,.0f}c"
        )

    if cfg.currency.min_listings > 0:
        if observations is None:
            reasons.append(
                f"no listing/observation count available to check against min_listings "
                f"{cfg.currency.min_listings} (set currency.min_listings: 0 to disable this gate)"
            )
        elif observations < cfg.currency.min_listings:
            reasons.append(
                f"observations {observations} < min_listings {cfg.currency.min_listings}"
            )

    if units < 1:
        reasons.append(
            f"one unit costs more than the max position of {max_position:,.1f}c"
        )
    elif capital_required > max_position + 1e-9:
        reasons.append(
            f"capital required {capital_required:,.1f}c > max position {max_position:,.1f}c"
        )

    return reasons


CROSS_VENUE_CAVEAT = (
    "CANDIDATES ONLY — VERIFY IN-GAME BEFORE TRADING. "
    "The exchange figure is an observed aggregate rate, not an executable order book, "
    "and the stash figure is a listing price, not a fill price. poe.ninja data refreshes "
    "roughly every 15 minutes and is HTTP-cached for about 5, so nothing here is live. "
    "A divergence is a hypothesis to check in-game, never a confirmed arbitrage."
)
