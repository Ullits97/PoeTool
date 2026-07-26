"""Excel output.

Spec 8.1: Python owns `out/poe_data.xlsx` and regenerates it in full every
run. It must never write to the user's cockpit workbook, which holds all
manual input and pulls from this file via Power Query.

**Sheet names and column headers are a stable public interface.** Changing
one breaks the user's Power Query, so the interface carries a version that
is compared against the last run and any change is announced in the run
output.

Nulls stay null. A missing trend is an empty cell, never a 0, because a
fabricated zero is indistinguishable from a measured flat market.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import xlsxwriter

from .config import Config
from .models.corrupt import CorruptAnalysis, exclusion_reason
from .models.currency import CROSS_VENUE_CAVEAT, CurrencyAnalysis
from .store import Store, window_start

TOOL_VERSION = "0.1.0"
SHEET_INTERFACE_KEY = "sheet_interface_fingerprint"

FMT_TEXT = "text"
FMT_CHAOS = "chaos"
FMT_PCT = "pct"
FMT_INT = "int"
FMT_NUM2 = "num2"


@dataclass(frozen=True)
class Column:
    header: str
    key: str
    fmt: str = FMT_TEXT
    width: int = 16
    getter: Callable[[Any], Any] | None = None

    def value(self, row: Any) -> Any:
        if self.getter is not None:
            return self.getter(row)
        if isinstance(row, dict):
            return row.get(self.key)
        return getattr(row, self.key, None)


META_COLUMNS = (
    Column("Field", "field", width=28),
    Column("Value", "value", width=72),
)

ORDER_COLUMNS = (
    Column("Rank", "rank", FMT_INT, 6),
    Column("Currency", "name", FMT_TEXT, 26),
    Column("Type", "type", FMT_TEXT, 12),
    Column("Chaos Value", "chaos_value", FMT_CHAOS),
    Column("Volume (chaos/day)", "volume_chaos", FMT_CHAOS, 20),
    Column("Trend %", "trend_pct", FMT_PCT, 11),
    Column("Volatility (chaos)", "volatility", FMT_CHAOS, 18),
    Column("Suggested Buy Rate", "suggested_buy_rate", FMT_CHAOS, 19),
    Column("Suggested Sell Rate", "suggested_sell_rate", FMT_CHAOS, 19),
    Column("Units", "units", FMT_INT, 9),
    Column("Capital Required", "capital_required", FMT_CHAOS, 18),
    Column("Margin %", "margin_pct", FMT_PCT, 11),
    Column("Est Fills/Day", "expected_fills_per_day", FMT_NUM2, 14),
    Column("Score", "score", FMT_NUM2, 12),
    Column("Observations", "observations", FMT_INT, 13),
    Column("History Points", "history_points", FMT_INT, 14),
)

CROSS_VENUE_COLUMNS = (
    Column("Currency", "name", FMT_TEXT, 26),
    Column("Exchange Chaos", "exchange_chaos", FMT_CHAOS, 17),
    Column("Stash Chaos", "stash_chaos", FMT_CHAOS, 15),
    Column("Divergence %", "divergence_pct", FMT_PCT, 14),
    Column("Stash Spread %", "stash_spread_pct", FMT_PCT, 15),
    Column("Stash Observations", "stash_observations", FMT_INT, 18),
    Column("Status", "status", FMT_TEXT, 34),
)

CORRUPT_COLUMNS = (
    Column("Rank", "rank", FMT_INT, 6),
    Column("Gem", "gem_name", FMT_TEXT, 34),
    Column("Input Variant", "input_variant", FMT_TEXT, 13),
    Column("Input Cost", "input_cost_chaos", FMT_CHAOS, 12),
    Column("Vaal Orb Cost", "vaal_cost_chaos", FMT_CHAOS, 14),
    Column("Cost/Attempt", "cost_per_attempt", FMT_CHAOS, 14),
    Column("Has Vaal Variant", "has_vaal_variant", FMT_TEXT, 16),
    Column("EV (chaos)", "ev_chaos", FMT_CHAOS, 12),
    Column("EV %", "ev_pct", FMT_PCT, 11),
    Column("Max Affordable Attempts", "max_affordable_attempts", FMT_INT, 23),
    Column("Incomplete Data", "incomplete_data", FMT_TEXT, 16),
    Column("Bankroll Gate", "bankroll_gate", FMT_TEXT, 52),
)

CALIBRATION_COLUMNS = (
    Column("Outcome", "outcome", FMT_TEXT, 22),
    Column("Configured Probability", "configured_probability", FMT_PCT, 21),
    Column("Observed Count", "observed_count", FMT_INT, 15),
    Column("Observed Frequency", "observed_frequency", FMT_PCT, 18),
    Column("Sample Size", "sample_size", FMT_INT, 12),
    Column("Note", "note", FMT_TEXT, 70),
)

FILTERED_COLUMNS = (
    Column("Module", "module", FMT_TEXT, 14),
    Column("Item", "item", FMT_TEXT, 34),
    Column("Type", "type", FMT_TEXT, 12),
    Column("Chaos Value", "chaos_value", FMT_CHAOS),
    Column("Reason Excluded", "reason", FMT_TEXT, 90),
)

HISTORY_COLUMNS = (
    Column("Timestamp (UTC)", "ts", FMT_TEXT, 22),
    Column("Type", "type", FMT_TEXT, 12),
    Column("Currency Id", "currency_id", FMT_TEXT, 24),
    Column("Currency", "name", FMT_TEXT, 26),
    Column("Chaos Value", "chaos_value", FMT_CHAOS),
    Column("Volume (primary)", "volume_primary", FMT_CHAOS, 17),
)

SHEETS: tuple[tuple[str, tuple[Column, ...]], ...] = (
    ("Meta", META_COLUMNS),
    ("Exchange_Orders", ORDER_COLUMNS),
    ("CrossVenue", CROSS_VENUE_COLUMNS),
    ("Corrupt_EV", CORRUPT_COLUMNS),
    ("Corrupt_Calibration", CALIBRATION_COLUMNS),
    ("Filtered_Out", FILTERED_COLUMNS),
    ("History_Currency", HISTORY_COLUMNS),
)


def interface_fingerprint() -> str:
    """Stable hash of every sheet name and header, for change detection."""
    parts = [f"{name}:{'|'.join(c.header for c in cols)}" for name, cols in SHEETS]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _formats(workbook: xlsxwriter.Workbook) -> dict[str, Any]:
    return {
        "header": workbook.add_format(
            {"bold": True, "bg_color": "#1F3864", "font_color": "white",
             "border": 1, "text_wrap": True, "valign": "vcenter"}
        ),
        "banner": workbook.add_format(
            {"bold": True, "bg_color": "#FFF2CC", "font_color": "#7F6000",
             "border": 1, "text_wrap": True, "valign": "vcenter", "align": "left"}
        ),
        FMT_TEXT: workbook.add_format({}),
        FMT_CHAOS: workbook.add_format({"num_format": "#,##0.0"}),
        FMT_PCT: workbook.add_format({"num_format": "0.0%"}),
        FMT_INT: workbook.add_format({"num_format": "#,##0"}),
        FMT_NUM2: workbook.add_format({"num_format": "#,##0.00"}),
    }


def _write_sheet(
    workbook: xlsxwriter.Workbook,
    formats: dict[str, Any],
    name: str,
    columns: Sequence[Column],
    rows: Sequence[Any],
    *,
    banner: str | None = None,
    colour_scale_on: Sequence[str] = (),
) -> None:
    sheet = workbook.add_worksheet(name)
    header_row = 0

    if banner:
        # The one permitted merged cell (spec 8.2). Merged cells break Power
        # Query, so this banner sits above the header and nowhere else.
        sheet.merge_range(0, 0, 0, max(len(columns) - 1, 1), banner, formats["banner"])
        sheet.set_row(0, 46)
        header_row = 2

    for col_index, column in enumerate(columns):
        sheet.write(header_row, col_index, column.header, formats["header"])
        sheet.set_column(col_index, col_index, column.width)

    for row_index, row in enumerate(rows, start=header_row + 1):
        for col_index, column in enumerate(columns):
            value = column.value(row)
            cell_format = formats[column.fmt]
            if value is None:
                # Insufficient history stays blank: not 0, not fabricated.
                sheet.write_blank(row_index, col_index, None, cell_format)
            elif isinstance(value, bool):
                sheet.write_string(row_index, col_index, "yes" if value else "no", cell_format)
            elif isinstance(value, (int, float)):
                sheet.write_number(row_index, col_index, value, cell_format)
            else:
                sheet.write_string(row_index, col_index, str(value), cell_format)

    last_row = header_row + len(rows)
    sheet.freeze_panes(header_row + 1, 0)
    if columns:
        sheet.autofilter(header_row, 0, max(last_row, header_row), len(columns) - 1)

    if rows:
        for key in colour_scale_on:
            idx = next((i for i, c in enumerate(columns) if c.key == key), None)
            if idx is None:
                continue
            sheet.conditional_format(
                header_row + 1, idx, last_row, idx,
                {
                    "type": "3_color_scale",
                    "min_color": "#F8696B",
                    "mid_color": "#FFEB84",
                    "max_color": "#63BE7B",
                },
            )


def _meta_rows(
    cfg: Config,
    store: Store,
    league_id: str,
    league_name: str,
    currency: CurrencyAnalysis,
    corrupt: CorruptAnalysis,
    *,
    primary: str | None,
    interface_changed: bool,
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    counts = store.snapshot_counts()
    rows: list[dict[str, Any]] = [
        {"field": "Generated (UTC)", "value": now.replace(microsecond=0).isoformat()},
        {"field": "Generated (local)", "value": datetime.now().replace(microsecond=0).isoformat()},
        {"field": "Tool version", "value": TOOL_VERSION},
        {"field": "Sheet interface", "value": interface_fingerprint()},
        {"field": "League id", "value": league_id},
        {"field": "League name", "value": league_name},
        {"field": "Exchange numeraire (core.primary)", "value": primary or "unknown"},
        {"field": "Bankroll (chaos)", "value": cfg.bankroll.total_chaos},
        {"field": "Max position (chaos)", "value": cfg.bankroll.max_position_chaos},
        {"field": "Max corrupt cost/attempt (chaos)", "value": cfg.bankroll.max_corrupt_cost_chaos},
        {"field": "Trend window (hours)", "value": cfg.currency.trend_window_hours},
        {"field": "Exchange orders (ranked)", "value": len(currency.orders)},
        {"field": "Exchange orders (filtered out)", "value": len(currency.filtered)},
        {"field": "Cross-venue candidates", "value": len(currency.cross_venue)},
        {"field": "Corrupt candidates (ranked)", "value": len(corrupt.rows)},
        {"field": "Corrupt candidates (filtered out)", "value": len(corrupt.filtered)},
        {"field": "Vaal Orb cost (chaos)", "value": corrupt.vaal_cost_chaos},
    ]
    if interface_changed:
        rows.append(
            {
                "field": "BREAKING CHANGE",
                "value": "Sheet names or column headers changed since the last run. "
                         "Refresh the Power Query mappings in your cockpit workbook.",
            }
        )
    for key, count in counts.items():
        rows.append({"field": f"Rows in {key}", "value": count})

    if currency.orientation is not None:
        rows.append(
            {
                "field": "Stash quote orientation",
                "value": (
                    f"ask='{currency.orientation.ask_field}', "
                    f"bid='{currency.orientation.bid_field}', "
                    f"pay={currency.orientation.pay.denomination}, "
                    f"receive={currency.orientation.receive.denomination} "
                    f"(resolved from data, {currency.orientation.role_agreement:.0%} agreement)"
                ),
            }
        )
    elif currency.orientation_error:
        rows.append({"field": "Stash quote orientation", "value": currency.orientation_error})

    for endpoint in store.last_fetch_per_endpoint(league_id):
        label = endpoint.endpoint + (f" [{endpoint.type}]" if endpoint.type else "")
        rows.append(
            {
                "field": f"Fetch {label}",
                "value": f"{endpoint.ts} -> HTTP {endpoint.status}"
                + (f" ({endpoint.note})" if endpoint.note else ""),
            }
        )

    for note in currency.notes + corrupt.notes:
        rows.append({"field": "Note", "value": note})
    return rows


def write_workbook(
    cfg: Config,
    store: Store,
    *,
    league_id: str,
    league_name: str,
    currency: CurrencyAnalysis,
    corrupt: CorruptAnalysis,
    primary: str | None = None,
) -> tuple[Path, bool]:
    """Render the workbook atomically. Returns (path, interface_changed)."""
    target = cfg.export_path
    target.parent.mkdir(parents=True, exist_ok=True)

    fingerprint = interface_fingerprint()
    previous = store.get_meta(SHEET_INTERFACE_KEY)
    interface_changed = previous is not None and previous != fingerprint

    # Write to a temp file in the destination directory, then atomically
    # replace, so a mid-run failure never leaves a corrupt workbook.
    handle, temp_name = tempfile.mkstemp(suffix=".xlsx", dir=str(target.parent))
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        workbook = xlsxwriter.Workbook(str(temp_path), {"constant_memory": False})
        formats = _formats(workbook)

        _write_sheet(
            workbook, formats, "Meta", META_COLUMNS,
            _meta_rows(
                cfg, store, league_id, league_name, currency, corrupt,
                primary=primary, interface_changed=interface_changed,
            ),
        )

        order_rows = [
            {
                **{c.key: c.value(order) for c in ORDER_COLUMNS if c.key != "rank"},
                "rank": rank,
            }
            for rank, order in enumerate(currency.orders, start=1)
        ]
        _write_sheet(
            workbook, formats, "Exchange_Orders", ORDER_COLUMNS, order_rows,
            colour_scale_on=("trend_pct", "score"),
        )

        cross_rows = [
            {
                **{c.key: c.value(row) for c in CROSS_VENUE_COLUMNS if c.key != "status"},
                "status": "candidate — verify in-game",
            }
            for row in currency.cross_venue
        ]
        _write_sheet(
            workbook, formats, "CrossVenue", CROSS_VENUE_COLUMNS, cross_rows,
            banner=CROSS_VENUE_CAVEAT,
            colour_scale_on=("divergence_pct",),
        )

        corrupt_rows = [
            {
                **{c.key: c.value(row) for c in CORRUPT_COLUMNS if c.key != "rank"},
                "rank": rank,
            }
            for rank, row in enumerate(corrupt.rows, start=1)
        ]
        _write_sheet(
            workbook, formats, "Corrupt_EV", CORRUPT_COLUMNS, corrupt_rows,
            colour_scale_on=("ev_pct",),
        )

        _write_sheet(
            workbook, formats, "Corrupt_Calibration", CALIBRATION_COLUMNS, corrupt.calibration,
        )

        filtered_rows = [
            {
                "module": "currency",
                "item": row.name,
                "type": row.type,
                "chaos_value": row.chaos_value,
                "reason": row.filter_reason,
            }
            for row in currency.filtered
        ] + [
            {
                "module": "corrupt",
                "item": f"{row.gem_name} {row.input_variant}",
                "type": "gem",
                "chaos_value": row.input_cost_chaos,
                "reason": exclusion_reason(row),
            }
            for row in corrupt.filtered
        ]
        _write_sheet(workbook, formats, "Filtered_Out", FILTERED_COLUMNS, filtered_rows)

        since = window_start(cfg.export.history_days * 24)
        history = [dict(r) for r in store.currency_history_rows(league_id, since)]
        _write_sheet(workbook, formats, "History_Currency", HISTORY_COLUMNS, history)

        workbook.close()
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    store.set_meta(SHEET_INTERFACE_KEY, fingerprint)
    return target, interface_changed
