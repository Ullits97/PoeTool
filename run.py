#!/usr/bin/env python3
"""poe-flip CLI.

    python run.py fetch      # fetch + persist only
    python run.py analyse    # recompute from stored snapshots, no network
    python run.py export     # write xlsx from stored snapshots
    python run.py run        # fetch -> analyse -> export (Task Scheduler target)
    python run.py status     # league, last fetch per endpoint, counts, DB size

`--dry-run` logs intended requests without issuing them. `--verbose` adds
full request/response metadata.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from poeflip import export as export_mod  # noqa: E402
from poeflip.config import Config, load_config  # noqa: E402
from poeflip.errors import PoeFlipError  # noqa: E402
from poeflip.models.corrupt import CorruptAnalysis, analyse_corrupt  # noqa: E402
from poeflip.models.currency import CurrencyAnalysis, analyse_currency  # noqa: E402
from poeflip.ninja_client import NinjaClient  # noqa: E402
from poeflip.schema import (  # noqa: E402
    norm,
    parse_exchange,
    parse_items,
    parse_leagues,
    parse_stash_currency,
)
from poeflip.store import Store, utc_now_iso  # noqa: E402

log = logging.getLogger("poeflip")

STASH_CURRENCY_TYPES = ("Currency", "Fragment")


def setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cfg.log_path, encoding="utf-8"),
        ],
    )
    if verbose:
        logging.getLogger("httpx").setLevel(logging.DEBUG)
    else:
        logging.getLogger("httpx").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
DRY_RUN_LEAGUE = "(dry-run)"


def resolve_league(
    client: NinjaClient, store: Store, cfg: Config, run_ts: str
) -> tuple[str, str]:
    if cfg.league.mode == "pinned":
        pinned = cfg.league.pinned_id or ""
        log.info("league pinned to %s", pinned)
        return pinned, pinned

    result = client.leagues()
    store.log_fetch(
        ts=run_ts, endpoint=result.endpoint, league="-", type_=None,
        status=result.status, etag=result.etag, note=result.note or None,
    )
    if result.payload is None:
        if client.dry_run:
            # A dry run on a cold cache has nothing to read the league from.
            # Its job is to show which requests *would* go out, so carry on
            # with a placeholder rather than failing.
            log.info("[dry-run] league unknown; using %s as a placeholder", DRY_RUN_LEAGUE)
            return DRY_RUN_LEAGUE, DRY_RUN_LEAGUE
        raise PoeFlipError(
            "cannot resolve the current league: no payload from "
            f"{result.url} (status {result.status}). "
            "Set league.mode: pinned in config.yaml to work offline."
        )
    leagues = parse_leagues(result.payload, endpoint=result.endpoint)
    league = leagues[0]  # first entry is the current temporary challenge league
    log.info("league resolved to %s (%s)", league.id, league.name)
    return league.id, league.name


def do_fetch(cfg: Config, store: Store, dry_run: bool) -> tuple[str, str, str | None]:
    primary: str | None = None
    # One timestamp for the whole run, so every table's rows line up and a
    # single fetch cycle reads as a single snapshot in time.
    run_ts = utc_now_iso()
    with NinjaClient(
        user_agent=cfg.app.user_agent,
        cache=store,
        min_fetch_interval_minutes=cfg.app.min_fetch_interval_minutes,
        dry_run=dry_run,
    ) as client:
        league_id, league_name = resolve_league(client, store, cfg, run_ts)

        # Stash currency first: its chaosEquivalent figures are the independent
        # reference used to convert the exchange numeraire when it is not chaos.
        chaos_reference: dict[str, float] = {}
        for type_ in STASH_CURRENCY_TYPES:
            result = client.stash_currency(league_id, type_)
            note = result.note or None
            rows_written = 0
            if result.payload is not None:
                parsed = parse_stash_currency(
                    result.payload, league=league_id, type_=type_, endpoint=result.endpoint
                )
                log.info(
                    "%s [%s]: %d line(s); orientation ask='%s' bid='%s'",
                    result.endpoint, type_, len(parsed.rows),
                    parsed.orientation.ask_field, parsed.orientation.bid_field,
                )
                for row in parsed.rows:
                    if row.chaos_equivalent:
                        chaos_reference[norm(row.name)] = row.chaos_equivalent
                        if row.details_id:
                            chaos_reference[norm(row.details_id)] = row.chaos_equivalent
                # Spec 12.2: a 304 writes zero new snapshot rows.
                if result.status == 200 and not result.from_cache:
                    rows_written = store.insert_stash_currency(
                        ts=run_ts, league=league_id, type_=type_, rows=parsed.rows
                    )
                    note = f"{rows_written} snapshot row(s) written"
            store.log_fetch(
                ts=run_ts, endpoint=result.endpoint, league=league_id, type_=type_,
                status=result.status, etag=result.etag, note=note,
            )

        for type_ in cfg.currency.watchlist_types:
            result = client.exchange(league_id, type_)
            note = result.note or None
            if result.payload is not None:
                parsed = parse_exchange(
                    result.payload, league=league_id, type_=type_,
                    endpoint=result.endpoint, chaos_reference=chaos_reference,
                )
                primary = primary or parsed.conversion.primary
                log.info(
                    "%s [%s]: %d line(s); %s",
                    result.endpoint, type_, len(parsed.rows), parsed.conversion.method,
                )
                if result.status == 200 and not result.from_cache:
                    written = store.insert_exchange(
                        ts=run_ts, league=league_id, type_=type_, rows=parsed.rows
                    )
                    note = f"{written} snapshot row(s) written"
            store.log_fetch(
                ts=run_ts, endpoint=result.endpoint, league=league_id, type_=type_,
                status=result.status, etag=result.etag, note=note,
            )

        for type_ in cfg.corrupt.item_types:
            result = client.stash_items(league_id, type_)
            note = result.note or None
            if result.payload is not None:
                parsed_items = parse_items(
                    result.payload, league=league_id, type_=type_, endpoint=result.endpoint
                )
                log.info("%s [%s]: %d line(s)", result.endpoint, type_, len(parsed_items.rows))
                if result.status == 200 and not result.from_cache:
                    written = store.insert_items(
                        ts=run_ts, league=league_id, type_=type_, rows=parsed_items.rows
                    )
                    note = f"{written} snapshot row(s) written"
            store.log_fetch(
                ts=run_ts, endpoint=result.endpoint, league=league_id, type_=type_,
                status=result.status, etag=result.etag, note=note,
            )

    return league_id, league_name, primary


# --------------------------------------------------------------------------
# analyse
# --------------------------------------------------------------------------
def resolve_league_offline(store: Store, cfg: Config) -> tuple[str, str]:
    """Work out which league to analyse without touching the network."""
    if cfg.league.mode == "pinned" and cfg.league.pinned_id:
        return cfg.league.pinned_id, cfg.league.pinned_id
    row = store.conn.execute(
        "SELECT league FROM fetch_log WHERE league != '-' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise PoeFlipError(
            "no league recorded in the database yet — run `python run.py fetch` first, "
            "or pin one with league.mode: pinned in config.yaml."
        )
    return row["league"], row["league"]


def do_analyse(cfg: Config, store: Store, league_id: str) -> tuple[CurrencyAnalysis, CorruptAnalysis]:
    currency = analyse_currency(store, cfg, league_id)
    corrupt = analyse_corrupt(store, cfg, league_id)

    log.info(
        "currency: %d ranked, %d filtered out, %d cross-venue candidate(s)",
        len(currency.orders), len(currency.filtered), len(currency.cross_venue),
    )
    for note in currency.notes:
        log.warning("currency: %s", note)

    log.info(
        "corrupt: %d ranked, %d filtered out", len(corrupt.rows), len(corrupt.filtered)
    )
    for note in corrupt.notes:
        log.warning("corrupt: %s", note)

    if currency.orders:
        log.info("top exchange orders:")
        for row in currency.orders[:5]:
            trend = f"{row.trend_pct:+.1%}" if row.trend_pct is not None else "n/a"
            log.info(
                "  %-24s %8.1fc  buy %.1fc / sell %.1fc  trend %s  fills/day %s",
                row.name, row.chaos_value, row.suggested_buy_rate, row.suggested_sell_rate,
                trend,
                f"{row.expected_fills_per_day:.1f}" if row.expected_fills_per_day is not None else "n/a",
            )
    return currency, corrupt


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_fetch(cfg: Config, args: argparse.Namespace) -> int:
    with Store(cfg.db_path) as store:
        league_id, league_name, _ = do_fetch(cfg, store, args.dry_run)
    log.info("fetch complete for %s", league_name or league_id)
    return 0


def cmd_analyse(cfg: Config, args: argparse.Namespace) -> int:
    with Store(cfg.db_path) as store:
        league_id, _ = resolve_league_offline(store, cfg)
        do_analyse(cfg, store, league_id)
    return 0


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    with Store(cfg.db_path) as store:
        league_id, league_name = resolve_league_offline(store, cfg)
        currency, corrupt = do_analyse(cfg, store, league_id)
        path, changed = export_mod.write_workbook(
            cfg, store, league_id=league_id, league_name=league_name,
            currency=currency, corrupt=corrupt,
        )
    _report_export(path, changed)
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    with Store(cfg.db_path) as store:
        league_id, league_name, primary = do_fetch(cfg, store, args.dry_run)
        currency, corrupt = do_analyse(cfg, store, league_id)
        if args.dry_run:
            log.info("[dry-run] skipping workbook write")
            return 0
        path, changed = export_mod.write_workbook(
            cfg, store, league_id=league_id, league_name=league_name,
            currency=currency, corrupt=corrupt, primary=primary,
        )
    _report_export(path, changed)
    return 0


def _report_export(path, changed: bool) -> None:
    log.info("workbook written: %s", path)
    if changed:
        # Spec 8.1: sheet names and headers are a public interface.
        log.warning(
            "BREAKING CHANGE: sheet names or column headers differ from the previous run. "
            "Your cockpit workbook's Power Query mappings need refreshing. "
            "Interface fingerprint is now %s.",
            export_mod.interface_fingerprint(),
        )


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    with Store(cfg.db_path) as store:
        try:
            league_id, league_name = resolve_league_offline(store, cfg)
        except PoeFlipError:
            league_id = league_name = "(none yet)"

        print(f"league:        {league_name} [{league_id}]")
        print(f"database:      {store.path} ({store.db_size_bytes() / 1024:.1f} KiB)")
        print(f"config:        bankroll {cfg.bankroll.total_chaos:g}c, "
              f"max position {cfg.bankroll.max_position_chaos:g}c, "
              f"max corrupt/attempt {cfg.bankroll.max_corrupt_cost_chaos:g}c")

        counts = store.snapshot_counts()
        print("\nsnapshot rows:")
        for table, count in counts.items():
            print(f"  {table:<22} {count:>8,}")
        print(f"  distinct snapshot times {store.distinct_snapshot_times(league_id):>6,}")

        statuses = store.last_fetch_per_endpoint()
        print("\nlast fetch per endpoint:")
        if not statuses:
            print("  (nothing fetched yet)")
        for entry in statuses:
            label = entry.endpoint + (f" [{entry.type}]" if entry.type else "")
            print(f"  {label:<62} {entry.ts}  HTTP {entry.status}")
            if entry.note:
                print(f"      {entry.note}")
    return 0


COMMANDS = {
    "fetch": cmd_fetch,
    "analyse": cmd_analyse,
    "export": cmd_export,
    "run": cmd_run,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py", description="poe-flip — PoE 1 currency screener and position logbook"
    )
    parser.add_argument(
        "command", nargs="?", default="run", choices=sorted(COMMANDS),
        help="what to do (default: run)",
    )
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="path to config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true", help="log intended requests without issuing them"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="full request/response metadata"
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except PoeFlipError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg, args.verbose)

    try:
        return COMMANDS[args.command](cfg, args)
    except PoeFlipError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
