"""SQLite storage: 1-minute bars and quality events (phase 1); daily and 5-minute
history, regime log and manual data (phase 2)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
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
CREATE TABLE IF NOT EXISTS bars_1d (
    symbol TEXT NOT NULL, source TEXT NOT NULL, start_utc TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL,
    PRIMARY KEY (symbol, source, start_utc)
);
CREATE TABLE IF NOT EXISTS bars_5m (
    symbol TEXT NOT NULL, source TEXT NOT NULL, start_utc TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL,
    PRIMARY KEY (symbol, source, start_utc)
);
CREATE TABLE IF NOT EXISTS regime_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_utc TEXT NOT NULL, session_date TEXT, regime TEXT NOT NULL, bias TEXT NOT NULL,
    reasons_json TEXT NOT NULL, rules_version TEXT NOT NULL, basis TEXT
);
CREATE TABLE IF NOT EXISTS events (
    date TEXT NOT NULL, type TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '', note TEXT
);
CREATE TABLE IF NOT EXISTS holidays (date TEXT PRIMARY KEY, note TEXT);
CREATE TABLE IF NOT EXISTS fii_dii (date TEXT PRIMARY KEY, fii_net_cr REAL NOT NULL, dii_net_cr REAL);
CREATE TABLE IF NOT EXISTS history_floor (
    tbl TEXT NOT NULL, symbol TEXT NOT NULL, source TEXT NOT NULL, floor_utc TEXT NOT NULL,
    PRIMARY KEY (tbl, symbol, source)
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY, created_utc TEXT NOT NULL, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
    params_json TEXT NOT NULL, report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id TEXT NOT NULL, n INTEGER NOT NULL, trade_json TEXT NOT NULL, PRIMARY KEY (run_id, n)
);
"""

HISTORY_TABLES = ("bars_1d", "bars_5m")


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v)


class SimDataInLiveStore(ValueError):
    pass


class Store:
    def __init__(self, db_path: str | Path, live: bool = False):
        """live=True marks the live-data file: any write whose source is 'sim'
        (or 'sim:...') is refused, so simulated rows can never land in it."""
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.live = live
        self._lock = threading.Lock()
        # timeout: wait up to 30 s for another process (e.g. the running app) to finish a write
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._db.executescript(SCHEMA)

    def _guard(self, source) -> None:
        if self.live and str(source).strip().lower().startswith("sim"):
            raise SimDataInLiveStore(f"refusing to write source '{source}' into the live database")

    def save_bar(self, bar: Bar, source: str) -> None:
        self._guard(source)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?)",
                (bar.symbol, bar.start.isoformat(), bar.open, bar.high, bar.low,
                 bar.close, bar.volume, bar.ticks, source),
            )
            self._db.commit()

    def save_quality_event(self, at: datetime, symbol: str, reason: str,
                           last, bid, ask, source: str) -> None:
        self._guard(source)
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

    # ---------------- history (bars_1d, bars_5m) ----------------
    def save_history_bars(self, table: str, bars: list[Bar], source: str) -> int:
        if table not in HISTORY_TABLES:
            raise ValueError(f"unknown history table {table}")
        self._guard(source)
        with self._lock:
            self._db.executemany(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?)",
                [(b.symbol, source, b.start.isoformat(), b.open, b.high, b.low, b.close, b.volume)
                 for b in bars],
            )
            self._db.commit()
        return len(bars)

    def history_bars(self, table: str, symbol: str, source: str, *,
                     since: datetime | None = None, before: datetime | None = None,
                     limit: int | None = None) -> list[Bar]:
        """Bars in ascending time order. `limit` keeps the most recent ones."""
        if table not in HISTORY_TABLES:
            raise ValueError(f"unknown history table {table}")
        sql = f"SELECT symbol,start_utc,open,high,low,close,volume FROM {table} WHERE symbol=? AND source=?"
        args: list = [symbol, source]
        if since is not None:
            sql += " AND start_utc >= ?"
            args.append(since.isoformat())
        if before is not None:
            sql += " AND start_utc < ?"
            args.append(before.isoformat())
        sql += " ORDER BY start_utc DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [Bar(r[0], _ts(r[1]), r[2], r[3], r[4], r[5], r[6]) for r in reversed(rows)]

    def last_history_start(self, table: str, symbol: str, source: str) -> datetime | None:
        bars = self.history_bars(table, symbol, source, limit=1)
        return bars[0].start if bars else None

    def delete_bars_outside_session(self, source: str) -> int:
        """Remove 5-minute bars that start before 09:15 or at/after 15:30 IST (03:45 / 10:00 UTC).
        Timestamps are stored as UTC ISO strings, so the time is characters 12-16."""
        self._guard(source)
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM bars_5m WHERE source=? AND (substr(start_utc,12,5) < '03:45' OR substr(start_utc,12,5) >= '10:00')",
                (source,))
            self._db.commit()
        return cur.rowcount

    def first_history_start(self, table: str, symbol: str, source: str) -> datetime | None:
        if table not in HISTORY_TABLES:
            raise ValueError(f"unknown history table {table}")
        with self._lock:
            r = self._db.execute(f"SELECT MIN(start_utc) FROM {table} WHERE symbol=? AND source=?",
                                 (symbol, source)).fetchone()
        return _ts(r[0]) if r and r[0] else None

    def history_range(self, table: str, source: str) -> dict[str, dict]:
        """symbol -> first and last stored bar start, bar count and number of distinct sessions."""
        if table not in HISTORY_TABLES:
            raise ValueError(f"unknown history table {table}")
        with self._lock:
            rows = self._db.execute(
                f"SELECT symbol, MIN(start_utc), MAX(start_utc), COUNT(*), COUNT(DISTINCT substr(start_utc,1,10))"
                f" FROM {table} WHERE source=? GROUP BY symbol", (source,)).fetchall()
        return {r[0]: {"first": _ts(r[1]), "last": _ts(r[2]), "bars": r[3], "days": r[4]} for r in rows}

    def history_floor(self, table: str, symbol: str, source: str) -> datetime | None:
        """Earliest time the source has data for (nothing came back before it), if known."""
        with self._lock:
            r = self._db.execute("SELECT floor_utc FROM history_floor WHERE tbl=? AND symbol=? AND source=?",
                                 (table, symbol, source)).fetchone()
        return _ts(r[0]) if r else None

    def set_history_floor(self, table: str, symbol: str, source: str, floor: datetime) -> None:
        self._guard(source)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO history_floor VALUES (?,?,?,?)",
                             (table, symbol, source, floor.isoformat()))
            self._db.commit()

    # ---------------- backtests ----------------
    def save_backtest(self, run_id: str, created: datetime, strategy_id: str, version: str,
                      params: dict, report: dict, trades: list[dict]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO backtest_runs VALUES (?,?,?,?,?,?)",
                             (run_id, created.isoformat(), strategy_id, version,
                              json.dumps(params, sort_keys=True, default=str),
                              json.dumps(report, sort_keys=True, default=str)))
            self._db.execute("DELETE FROM backtest_trades WHERE run_id=?", (run_id,))
            self._db.executemany("INSERT INTO backtest_trades VALUES (?,?,?)",
                                 [(run_id, i, json.dumps(t, sort_keys=True, default=str))
                                  for i, t in enumerate(trades)])
            self._db.commit()

    def backtest_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT run_id, created_utc, strategy_id, version, report_json"
                                    " FROM backtest_runs ORDER BY created_utc DESC LIMIT ?", (limit,)).fetchall()
        return [{"run_id": r[0], "created_utc": r[1], "strategy_id": r[2], "version": r[3],
                 "report": json.loads(r[4])} for r in rows]

    def backtest_run(self, run_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT run_id, created_utc, strategy_id, version, params_json, report_json"
                                 " FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
            if not r:
                return None
            trades = [json.loads(t[0]) for t in self._db.execute(
                "SELECT trade_json FROM backtest_trades WHERE run_id=? ORDER BY n", (run_id,))]
        return {"run_id": r[0], "created_utc": r[1], "strategy_id": r[2], "version": r[3],
                "params": json.loads(r[4]), "report": json.loads(r[5]), "trades": trades}

    def history_counts(self, source: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        with self._lock:
            for table in HISTORY_TABLES:
                for sym, n in self._db.execute(
                        f"SELECT symbol, COUNT(*) FROM {table} WHERE source=? GROUP BY symbol", (source,)):
                    out.setdefault(sym, {})[table] = n
        return out

    # ---------------- manual data ----------------
    def replace_events(self, rows: list[tuple[str, str, str, str]]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM events")
            self._db.executemany("INSERT INTO events (date,type,symbol,note) VALUES (?,?,?,?)", rows)
            self._db.commit()

    def events_on(self, d: date) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT date,type,symbol,note FROM events WHERE date=? ORDER BY type,symbol,note",
                (d.isoformat(),)).fetchall()
        return [dict(zip(["date", "type", "symbol", "note"], r)) for r in rows]

    def replace_holidays(self, rows: list[tuple[str, str]]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM holidays")
            self._db.executemany("INSERT INTO holidays (date,note) VALUES (?,?)", rows)
            self._db.commit()

    def holidays(self) -> dict[date, str]:
        """Last successfully loaded holiday list."""
        with self._lock:
            rows = self._db.execute("SELECT date,note FROM holidays ORDER BY date").fetchall()
        return {date.fromisoformat(d): n for d, n in rows}

    def replace_fii_dii(self, rows: list[tuple[str, float, float | None]]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM fii_dii")
            self._db.executemany("INSERT INTO fii_dii (date,fii_net_cr,dii_net_cr) VALUES (?,?,?)", rows)
            self._db.commit()

    def fii_dii_before(self, d: date, limit: int) -> list[dict]:
        """Most recent first, strictly before date d."""
        with self._lock:
            rows = self._db.execute(
                "SELECT date,fii_net_cr,dii_net_cr FROM fii_dii WHERE date < ? ORDER BY date DESC LIMIT ?",
                (d.isoformat(), limit)).fetchall()
        return [{"date": date.fromisoformat(r[0]), "fii_net_cr": r[1], "dii_net_cr": r[2]} for r in rows]

    # ---------------- regime log ----------------
    def save_regime(self, at: datetime, session_date: date, regime: str, bias: str,
                    reasons: dict, rules_version: str, basis: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO regime_log (at_utc,session_date,regime,bias,reasons_json,rules_version,basis)"
                " VALUES (?,?,?,?,?,?,?)",
                (at.isoformat(), session_date.isoformat(), regime, bias,
                 json.dumps(reasons, sort_keys=True), rules_version, basis))
            self._db.commit()

    def regime_history(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at_utc,session_date,regime,bias,reasons_json,rules_version,basis FROM regime_log"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        keys = ["at_utc", "session_date", "regime", "bias", "reasons", "rules_version", "basis"]
        return [dict(zip(keys, (*r[:4], json.loads(r[4]), *r[5:]))) for r in rows]

    def last_regime(self, session_date: date) -> dict | None:
        """Newest stored regime entry for one session, or None. Survives restarts."""
        with self._lock:
            r = self._db.execute(
                "SELECT regime,bias,basis,rules_version FROM regime_log WHERE session_date=?"
                " ORDER BY id DESC LIMIT 1", (session_date.isoformat(),)).fetchone()
        return dict(zip(["regime", "bias", "basis", "rules_version"], r)) if r else None
