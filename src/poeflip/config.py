"""Load and validate config.yaml.

Validation is strict and happens once at startup. Anything that could later
produce a plausible-but-wrong number (an unedited User-Agent, outcome
probabilities that do not sum to 1, a sub-5-minute poll interval) is a
startup failure, not a warning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

# poe.ninja asks callers not to poll faster than every 5 minutes. This is the
# hard floor in code; config can raise it but never lower it (spec 2.3).
MIN_FETCH_INTERVAL_FLOOR_MINUTES = 5

PLACEHOLDER_MARKERS = ("CHANGEME", "example.com", "your.email", "TODO")

PROBABILITY_SUM_TOLERANCE = 1e-6

# The outcome bucket that only exists for gems with a Vaal counterpart.
VAAL_TRANSFORM_OUTCOME = "vaal_transform"


@dataclass(frozen=True)
class AppConfig:
    user_agent: str
    min_fetch_interval_minutes: int


@dataclass(frozen=True)
class LeagueConfig:
    mode: str
    pinned_id: str | None


@dataclass(frozen=True)
class BankrollConfig:
    total_chaos: float
    max_position_pct: float
    max_corrupt_fraction: float

    @property
    def max_position_chaos(self) -> float:
        return self.total_chaos * self.max_position_pct

    @property
    def max_corrupt_cost_chaos(self) -> float:
        return self.total_chaos * self.max_corrupt_fraction


@dataclass(frozen=True)
class CurrencyConfig:
    watchlist_types: tuple[str, ...]
    min_volume_chaos: float
    min_listings: int
    order_offset_pct: float
    trend_window_hours: int
    max_fills_per_day: float


@dataclass(frozen=True)
class OutcomeRow:
    outcome: str
    level_delta: int
    quality_delta: int
    probability: float


@dataclass(frozen=True)
class CorruptConfig:
    min_outcome_listings: int
    item_types: tuple[str, ...]
    input_level: int
    input_quality: int
    with_vaal_variant: tuple[OutcomeRow, ...]
    without_vaal_variant: tuple[OutcomeRow, ...]

    def table_for(self, has_vaal_variant: bool) -> tuple[OutcomeRow, ...]:
        return self.with_vaal_variant if has_vaal_variant else self.without_vaal_variant


@dataclass(frozen=True)
class ExportConfig:
    path: Path
    history_days: int


@dataclass(frozen=True)
class Config:
    app: AppConfig
    league: LeagueConfig
    bankroll: BankrollConfig
    currency: CurrencyConfig
    corrupt: CorruptConfig
    export: ExportConfig
    root: Path = field(default=Path("."))

    @property
    def db_path(self) -> Path:
        return self.root / "data" / "poe.db"

    @property
    def corrupt_log_path(self) -> Path:
        return self.root / "data" / "corrupt_log.csv"

    @property
    def log_path(self) -> Path:
        return self.root / "data" / "run.log"

    @property
    def export_path(self) -> Path:
        p = self.export.path
        return p if p.is_absolute() else self.root / p


def load_config(path: str | os.PathLike[str] = "config.yaml") -> Config:
    """Read, validate and freeze the configuration."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path.resolve()}")

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {cfg_path}\n  {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")

    root = cfg_path.resolve().parent

    return Config(
        app=_app(_section(raw, "app")),
        league=_league(_section(raw, "league")),
        bankroll=_bankroll(_section(raw, "bankroll")),
        currency=_currency(_section(raw, "currency")),
        corrupt=_corrupt(_section(raw, "corrupt")),
        export=_export(_section(raw, "export")),
        root=root,
    )


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if value is None:
        raise ConfigError(f"config is missing the required '{name}:' section")
    if not isinstance(value, dict):
        raise ConfigError(f"config section '{name}' must be a mapping, got {type(value).__name__}")
    return value


def _req(section: dict[str, Any], key: str, name: str) -> Any:
    if key not in section or section[key] is None:
        raise ConfigError(f"config is missing required key '{name}.{key}'")
    return section[key]


def _num(section: dict[str, Any], key: str, name: str, *, lo: float, hi: float) -> float:
    value = _req(section, key, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{name}.{key}' must be a number, got {value!r}")
    if not lo <= float(value) <= hi:
        raise ConfigError(f"'{name}.{key}' must be between {lo} and {hi}, got {value}")
    return float(value)


def _int(section: dict[str, Any], key: str, name: str, *, lo: int) -> int:
    value = _req(section, key, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{name}.{key}' must be an integer, got {value!r}")
    if value < lo:
        raise ConfigError(f"'{name}.{key}' must be >= {lo}, got {value}")
    return value


def _str_list(section: dict[str, Any], key: str, name: str) -> tuple[str, ...]:
    value = _req(section, key, name)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"'{name}.{key}' must be a non-empty list")
    if not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{name}.{key}' must contain only strings, got {value!r}")
    return tuple(value)


def _app(section: dict[str, Any]) -> AppConfig:
    ua = _req(section, "user_agent", "app")
    if not isinstance(ua, str) or not ua.strip():
        raise ConfigError("'app.user_agent' must be a non-empty string")
    hit = next((m for m in PLACEHOLDER_MARKERS if m.lower() in ua.lower()), None)
    if hit is not None:
        raise ConfigError(
            "'app.user_agent' still contains the placeholder "
            f"{hit!r}.\n"
            "poe.ninja asks every client to identify itself with an app name and a real\n"
            "contact address. Edit config.yaml, e.g.:\n"
            '  user_agent: "poe-flip/0.1 (contact: you@yourdomain.tld)"'
        )

    interval = _int(section, "min_fetch_interval_minutes", "app", lo=1)
    if interval < MIN_FETCH_INTERVAL_FLOOR_MINUTES:
        raise ConfigError(
            f"'app.min_fetch_interval_minutes' is {interval}, below the hard floor of "
            f"{MIN_FETCH_INTERVAL_FLOOR_MINUTES} minutes requested by poe.ninja. "
            "This floor is not configurable downward."
        )
    return AppConfig(user_agent=ua.strip(), min_fetch_interval_minutes=interval)


def _league(section: dict[str, Any]) -> LeagueConfig:
    mode = _req(section, "mode", "league")
    if mode not in ("auto", "pinned"):
        raise ConfigError(f"'league.mode' must be 'auto' or 'pinned', got {mode!r}")
    pinned = section.get("pinned_id")
    if mode == "pinned" and not pinned:
        raise ConfigError("'league.mode' is 'pinned' but 'league.pinned_id' is empty")
    if pinned is not None and not isinstance(pinned, str):
        raise ConfigError(f"'league.pinned_id' must be a string or null, got {pinned!r}")
    return LeagueConfig(mode=mode, pinned_id=pinned)


def _bankroll(section: dict[str, Any]) -> BankrollConfig:
    return BankrollConfig(
        total_chaos=_num(section, "total_chaos", "bankroll", lo=0.0, hi=1e9),
        max_position_pct=_num(section, "max_position_pct", "bankroll", lo=0.0, hi=1.0),
        max_corrupt_fraction=_num(section, "max_corrupt_fraction", "bankroll", lo=0.0, hi=1.0),
    )


def _currency(section: dict[str, Any]) -> CurrencyConfig:
    return CurrencyConfig(
        watchlist_types=_str_list(section, "watchlist_types", "currency"),
        min_volume_chaos=_num(section, "min_volume_chaos", "currency", lo=0.0, hi=1e12),
        min_listings=_int(section, "min_listings", "currency", lo=0),
        order_offset_pct=_num(section, "order_offset_pct", "currency", lo=0.0, hi=0.9),
        trend_window_hours=_int(section, "trend_window_hours", "currency", lo=1),
        max_fills_per_day=_num(section, "max_fills_per_day", "currency", lo=0.0, hi=10_000.0),
    )


def _outcome_table(raw: Any, table_name: str) -> tuple[OutcomeRow, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"'corrupt.outcome_tables.{table_name}' must be a non-empty list of outcome rows"
        )
    rows: list[OutcomeRow] = []
    for i, item in enumerate(raw):
        where = f"corrupt.outcome_tables.{table_name}[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping, got {item!r}")
        missing = {"outcome", "level_delta", "quality_delta", "probability"} - item.keys()
        if missing:
            raise ConfigError(f"{where} is missing key(s): {', '.join(sorted(missing))}")
        prob = item["probability"]
        if isinstance(prob, bool) or not isinstance(prob, (int, float)) or not 0.0 <= prob <= 1.0:
            raise ConfigError(f"{where}.probability must be a number in [0, 1], got {prob!r}")
        for key in ("level_delta", "quality_delta"):
            if isinstance(item[key], bool) or not isinstance(item[key], int):
                raise ConfigError(f"{where}.{key} must be an integer, got {item[key]!r}")
        rows.append(
            OutcomeRow(
                outcome=str(item["outcome"]),
                level_delta=int(item["level_delta"]),
                quality_delta=int(item["quality_delta"]),
                probability=float(prob),
            )
        )

    total = sum(r.probability for r in rows)
    if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise ConfigError(
            f"'corrupt.outcome_tables.{table_name}' probabilities sum to {total!r}, expected 1.0. "
            "An outcome table that does not sum to 1 silently mis-scales every EV, so this "
            "aborts rather than normalising for you."
        )

    names = [r.outcome for r in rows]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(
            f"'corrupt.outcome_tables.{table_name}' has duplicate outcome name(s): "
            f"{', '.join(sorted(dupes))}"
        )
    return tuple(rows)


def _corrupt(section: dict[str, Any]) -> CorruptConfig:
    tables = _req(section, "outcome_tables", "corrupt")
    if not isinstance(tables, dict):
        raise ConfigError("'corrupt.outcome_tables' must be a mapping")
    for required in ("with_vaal_variant", "without_vaal_variant"):
        if required not in tables:
            raise ConfigError(f"'corrupt.outcome_tables' is missing '{required}'")

    with_vaal = _outcome_table(tables["with_vaal_variant"], "with_vaal_variant")
    if not any(r.outcome == VAAL_TRANSFORM_OUTCOME for r in with_vaal):
        raise ConfigError(
            "'corrupt.outcome_tables.with_vaal_variant' must contain an outcome named "
            f"'{VAAL_TRANSFORM_OUTCOME}' — that bucket is what distinguishes it from "
            "the without_vaal_variant table."
        )
    without_vaal = _outcome_table(tables["without_vaal_variant"], "without_vaal_variant")
    if any(r.outcome == VAAL_TRANSFORM_OUTCOME for r in without_vaal):
        raise ConfigError(
            "'corrupt.outcome_tables.without_vaal_variant' contains "
            f"'{VAAL_TRANSFORM_OUTCOME}', but gems without a Vaal counterpart cannot "
            "transform. Remove it and redistribute its probability mass."
        )

    return CorruptConfig(
        min_outcome_listings=_int(section, "min_outcome_listings", "corrupt", lo=0),
        item_types=_str_list(section, "item_types", "corrupt"),
        input_level=_int(section, "input_level", "corrupt", lo=1),
        input_quality=_int(section, "input_quality", "corrupt", lo=0),
        with_vaal_variant=with_vaal,
        without_vaal_variant=without_vaal,
    )


def _export(section: dict[str, Any]) -> ExportConfig:
    path = _req(section, "path", "export")
    if not isinstance(path, str) or not path.strip():
        raise ConfigError("'export.path' must be a non-empty string")
    return ExportConfig(
        path=Path(path),
        history_days=_int(section, "history_days", "export", lo=1),
    )
