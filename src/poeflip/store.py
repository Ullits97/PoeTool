"""SQLite persistence. Plain SQL, no ORM.

Snapshots are append-only: never updated, never deleted. The accumulating
snapshot history is what makes trend and volatility computable at all, and
it is the asset the rest of the tool is built on (spec 3, phase 1).

Beyond the four tables in spec 5 there is one more, `http_cache`. Spec 2.3
requires storing an ETag per endpoint and reusing the cached payload on a
304, which needs the body kept somewhere; that is all this table does. It
holds no economy data and is safe to delete at any time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema import ExchangeRow, ItemRow, StashCurrencyRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  league TEXT NOT NULL,
  type TEXT,
  status INTEGER NOT NULL,
  etag TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS snap_exchange (
  ts TEXT NOT NULL, league TEXT NOT NULL, type TEXT NOT NULL,
  currency_id TEXT NOT NULL, name TEXT,
  primary_value REAL, volume_primary REAL,
  max_volume_currency TEXT, max_volume_rate REAL,
  chaos_value REAL,
  PRIMARY KEY (ts, league, type, currency_id)
);

CREATE TABLE IF NOT EXISTS snap_stash_currency (
  ts TEXT NOT NULL, league TEXT NOT NULL, type TEXT NOT NULL,
  name TEXT NOT NULL, details_id TEXT,
  chaos_equivalent REAL,
  pay_value REAL, pay_count INTEGER, pay_listings INTEGER,
  receive_value REAL, receive_count INTEGER, receive_listings INTEGER,
  PRIMARY KEY (ts, league, type, name)
);

CREATE TABLE IF NOT EXISTS snap_item (
  ts TEXT NOT NULL, league TEXT NOT NULL, type TEXT NOT NULL,
  item_id INTEGER NOT NULL, name TEXT, base_type TEXT,
  variant TEXT, corrupted INTEGER, links INTEGER,
  chaos_value REAL, divine_value REAL,
  count INTEGER, listing_count INTEGER,
  PRIMARY KEY (ts, league, type, item_id, variant, corrupted)
);

CREATE TABLE IF NOT EXISTS http_cache (
  key TEXT PRIMARY KEY,
  etag TEXT,
  payload TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_snap_exchange_window
  ON snap_exchange (league, type, currency_id, ts);
CREATE INDEX IF NOT EXISTS ix_snap_stash_currency_window
  ON snap_stash_currency (league, type, name, ts);
CREATE INDEX IF NOT EXISTS ix_snap_item_window
  ON snap_item (league, type, name, ts);
CREATE INDEX IF NOT EXISTS ix_fetch_log_ts ON fetch_log (ts);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class EndpointStatus:
    endpoint: str
    type: str | None
    ts: str
    status: int
    note: str | None


class Store:
    """Owns the SQLite connection. One instance per run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.conn.commit()
        self.close()

    # -- HttpCache protocol -------------------------------------------------
    def get_cached(self, key: str) -> tuple[str | None, Any | None, str | None]:
        row = self.conn.execute(
            "SELECT etag, payload, fetched_at FROM http_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None, None, None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            # A corrupt cache entry must not wedge the tool; treat as a miss.
            return None, None, None
        return row["etag"], payload, row["fetched_at"]

    def put_cached(self, key: str, etag: str | None, payload: Any, fetched_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO http_cache (key, etag, payload, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              etag = excluded.etag,
              payload = excluded.payload,
              fetched_at = excluded.fetched_at
            """,
            (key, etag, json.dumps(payload, separators=(",", ":")), fetched_at),
        )
        self.conn.commit()

    # -- small key/value scratch (sheet interface version, etc.) ------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM tool_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO tool_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- fetch log ----------------------------------------------------------
    def log_fetch(
        self,
        *,
        ts: str,
        endpoint: str,
        league: str,
        type_: str | None,
        status: int,
        etag: str | None = None,
        note: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fetch_log (ts, endpoint, league, type, status, etag, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, endpoint, league, type_, status, etag, note),
        )
        self.conn.commit()

    # -- snapshot writes ----------------------------------------------------
    def insert_exchange(
        self, *, ts: str, league: str, type_: str, rows: Iterable[ExchangeRow]
    ) -> int:
        payload = [
            (
                ts, league, type_, r.currency_id, r.name,
                r.primary_value, r.volume_primary,
                r.max_volume_currency, r.max_volume_rate, r.chaos_value,
            )
            for r in rows
        ]
        # OR IGNORE keeps the table append-only: a repeat write for the same
        # (ts, league, type, id) is a no-op rather than an overwrite.
        cur = self.conn.executemany(
            """
            INSERT OR IGNORE INTO snap_exchange
              (ts, league, type, currency_id, name, primary_value, volume_primary,
               max_volume_currency, max_volume_rate, chaos_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        self.conn.commit()
        return cur.rowcount

    def insert_stash_currency(
        self, *, ts: str, league: str, type_: str, rows: Iterable[StashCurrencyRow]
    ) -> int:
        payload = [
            (
                ts, league, type_, r.name, r.details_id, r.chaos_equivalent,
                r.pay_value, r.pay_count, r.pay_listings,
                r.receive_value, r.receive_count, r.receive_listings,
            )
            for r in rows
        ]
        cur = self.conn.executemany(
            """
            INSERT OR IGNORE INTO snap_stash_currency
              (ts, league, type, name, details_id, chaos_equivalent,
               pay_value, pay_count, pay_listings,
               receive_value, receive_count, receive_listings)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        self.conn.commit()
        return cur.rowcount

    def insert_items(self, *, ts: str, league: str, type_: str, rows: Iterable[ItemRow]) -> int:
        # `variant` is part of the primary key. SQLite treats NULLs as distinct
        # in a PK, which would let duplicates in, so absent variants are stored
        # as '' and read back as None.
        payload = [
            (
                ts, league, type_, r.item_id, r.name, r.base_type,
                r.variant or "", int(r.corrupted), r.links,
                r.chaos_value, r.divine_value, r.count, r.listing_count,
            )
            for r in rows
        ]
        cur = self.conn.executemany(
            """
            INSERT OR IGNORE INTO snap_item
              (ts, league, type, item_id, name, base_type, variant, corrupted,
               links, chaos_value, divine_value, count, listing_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        self.conn.commit()
        return cur.rowcount

    # -- reads used by the model layer --------------------------------------
    def latest_exchange_ts(self, league: str, type_: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM snap_exchange WHERE league = ? AND type = ?",
            (league, type_),
        ).fetchone()
        return row["ts"] if row and row["ts"] else None

    def latest_exchange(self, league: str, type_: str) -> list[sqlite3.Row]:
        ts = self.latest_exchange_ts(league, type_)
        if ts is None:
            return []
        return list(
            self.conn.execute(
                "SELECT * FROM snap_exchange WHERE league = ? AND type = ? AND ts = ?",
                (league, type_, ts),
            )
        )

    def exchange_history(
        self, league: str, type_: str, currency_id: str, since_iso: str
    ) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT ts, chaos_value FROM snap_exchange
                WHERE league = ? AND type = ? AND currency_id = ? AND ts >= ?
                  AND chaos_value IS NOT NULL
                ORDER BY ts
                """,
                (league, type_, currency_id, since_iso),
            )
        )

    def exchange_history_all(
        self, league: str, type_: str, since_iso: str
    ) -> dict[str, list[tuple[str, float]]]:
        """Trailing-window history for every currency of one type, in one query."""
        out: dict[str, list[tuple[str, float]]] = {}
        for row in self.conn.execute(
            """
            SELECT currency_id, ts, chaos_value FROM snap_exchange
            WHERE league = ? AND type = ? AND ts >= ? AND chaos_value IS NOT NULL
            ORDER BY currency_id, ts
            """,
            (league, type_, since_iso),
        ):
            out.setdefault(row["currency_id"], []).append((row["ts"], row["chaos_value"]))
        return out

    def latest_stash_currency(self, league: str, type_: str) -> list[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM snap_stash_currency WHERE league = ? AND type = ?",
            (league, type_),
        ).fetchone()
        if not row or not row["ts"]:
            return []
        return list(
            self.conn.execute(
                "SELECT * FROM snap_stash_currency WHERE league = ? AND type = ? AND ts = ?",
                (league, type_, row["ts"]),
            )
        )

    def latest_items(self, league: str, type_: str) -> list[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM snap_item WHERE league = ? AND type = ?",
            (league, type_),
        ).fetchone()
        if not row or not row["ts"]:
            return []
        return list(
            self.conn.execute(
                "SELECT * FROM snap_item WHERE league = ? AND type = ? AND ts = ?",
                (league, type_, row["ts"]),
            )
        )

    def currency_history_rows(
        self, league: str, since_iso: str
    ) -> list[sqlite3.Row]:
        """Long-format extract for the History_Currency sheet."""
        return list(
            self.conn.execute(
                """
                SELECT ts, type, currency_id, name, chaos_value, volume_primary
                FROM snap_exchange
                WHERE league = ? AND ts >= ?
                ORDER BY ts DESC, type, currency_id
                """,
                (league, since_iso),
            )
        )

    # -- status -------------------------------------------------------------
    def last_fetch_per_endpoint(self, league: str | None = None) -> list[EndpointStatus]:
        sql = """
            SELECT endpoint, type, MAX(ts) AS ts, status, note
            FROM fetch_log
            {where}
            GROUP BY endpoint, type
            ORDER BY endpoint, type
        """.format(where="WHERE league = ?" if league else "")
        params: Sequence[Any] = (league,) if league else ()
        return [
            EndpointStatus(
                endpoint=r["endpoint"],
                type=r["type"],
                ts=r["ts"],
                status=r["status"],
                note=r["note"],
            )
            for r in self.conn.execute(sql, params)
        ]

    def snapshot_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in ("snap_exchange", "snap_stash_currency", "snap_item", "fetch_log"):
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = row["n"]
        return counts

    def distinct_snapshot_times(self, league: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT ts) AS n FROM snap_exchange WHERE league = ?", (league,)
        ).fetchone()
        return row["n"] if row else 0

    def db_size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


def window_start(hours: int, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=hours)).replace(microsecond=0).isoformat()
