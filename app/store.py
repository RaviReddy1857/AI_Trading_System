"""SQLite storage for 1-minute bars and quality events."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .models import Bar

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol TEXT NOT NULL, start_utc TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, ticks INTEGER,
    source TEXT, PRIMARY KEY (symbol, start_utc)
);
CREATE TABLE IF NOT EXISTS quality_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_utc TEXT NOT NULL, symbol TEXT NOT NULL, reason TEXT NOT NULL,
    last REAL, bid REAL, ask REAL, source TEXT
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(SCHEMA)

    def save_bar(self, bar: Bar, source: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?)",
                (bar.symbol, bar.start.isoformat(), bar.open, bar.high, bar.low,
                 bar.close, bar.volume, bar.ticks, source),
            )
            self._db.commit()

    def save_quality_event(self, at: datetime, symbol: str, reason: str,
                           last, bid, ask, source: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO quality_events (at_utc,symbol,reason,last,bid,ask,source)"
                " VALUES (?,?,?,?,?,?,?)",
                (at.isoformat(), symbol, reason, last, bid, ask, source),
            )
            self._db.commit()

    def recent_bars(self, symbol: str, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT start_utc,open,high,low,close,volume,ticks FROM bars_1m"
                " WHERE symbol=? ORDER BY start_utc DESC LIMIT ?", (symbol, limit),
            ).fetchall()
        keys = ["start_utc", "open", "high", "low", "close", "volume", "ticks"]
        return [dict(zip(keys, r)) for r in reversed(rows)]

    def recent_quality_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at_utc,symbol,reason,last,bid,ask FROM quality_events"
                " ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        keys = ["at_utc", "symbol", "reason", "last", "bid", "ask"]
        return [dict(zip(keys, r)) for r in rows]

    def quality_event_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM quality_events").fetchone()[0]
