"""SQLite storage: 1-minute bars and quality events (phase 1); daily and 5-minute
history, regime log and manual data (phase 2)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

from .models import Bar, UnhedgedShort, unhedged_short_legs

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
CREATE TABLE IF NOT EXISTS instruments (
    underlying TEXT NOT NULL, source TEXT NOT NULL, expiry TEXT NOT NULL, lot_size INTEGER NOT NULL,
    kind TEXT NOT NULL, strikes_json TEXT NOT NULL, as_of_utc TEXT NOT NULL,
    PRIMARY KEY (underlying, source, expiry)
);
CREATE TABLE IF NOT EXISTS option_chain (
    underlying TEXT NOT NULL, source TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL,
    right TEXT NOT NULL, as_of_utc TEXT NOT NULL, bid REAL, ask REAL, last REAL, oi REAL,
    oi_change REAL, volume REAL, spot REAL,
    PRIMARY KEY (underlying, source, expiry, strike, right, as_of_utc)
);
CREATE TABLE IF NOT EXISTS option_bars (
    underlying TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL, right TEXT NOT NULL,
    interval TEXT NOT NULL, source TEXT NOT NULL, start_utc TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL, oi REAL,
    PRIMARY KEY (underlying, expiry, strike, right, interval, source, start_utc)
);
CREATE TABLE IF NOT EXISTS option_fetch_log (
    underlying TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL, right TEXT NOT NULL,
    interval TEXT NOT NULL, source TEXT NOT NULL, fetched_utc TEXT NOT NULL, rows INTEGER NOT NULL,
    PRIMARY KEY (underlying, expiry, strike, right, interval, source)
);
CREATE TABLE IF NOT EXISTS option_expiries (
    underlying TEXT NOT NULL, source TEXT NOT NULL, expiry TEXT NOT NULL, kind TEXT NOT NULL,
    found_utc TEXT NOT NULL, PRIMARY KEY (underlying, source, expiry)
);
CREATE TABLE IF NOT EXISTS option_expiry_misses (
    underlying TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL,
    checked_utc TEXT NOT NULL, PRIMARY KEY (underlying, source, kind, period)
);
CREATE TABLE IF NOT EXISTS margins (
    underlying TEXT NOT NULL, structure TEXT NOT NULL, width REAL NOT NULL,
    margin_per_lot_inr REAL NOT NULL, as_of TEXT NOT NULL, PRIMARY KEY (underlying, structure, width)
);
CREATE TABLE IF NOT EXISTS request_budget (
    day TEXT NOT NULL, kind TEXT NOT NULL, requests INTEGER NOT NULL, PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS shortlist (
    session_date TEXT NOT NULL, stock TEXT NOT NULL, expiry TEXT NOT NULL, side TEXT NOT NULL,
    rank INTEGER, status TEXT NOT NULL, score REAL, scores_json TEXT NOT NULL, reasons_json TEXT NOT NULL,
    created_utc TEXT NOT NULL, PRIMARY KEY (session_date, stock, expiry, side)
);
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY, stock TEXT NOT NULL, updated_utc TEXT NOT NULL, summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol TEXT NOT NULL, ex_date TEXT NOT NULL, action_type TEXT NOT NULL, purpose TEXT NOT NULL,
    record_date TEXT, announced_on TEXT, ratio TEXT, price_factor REAL, factor_basis TEXT,
    source TEXT NOT NULL, source_ref TEXT, imported_utc TEXT NOT NULL,
    PRIMARY KEY (symbol, ex_date, action_type, purpose)
);
CREATE TABLE IF NOT EXISTS positioning_daily (
    date TEXT PRIMARY KEY, data_json TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT, imported_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_bars_1d (
    symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL NOT NULL, volume REAL,
    source TEXT NOT NULL, source_ref TEXT, imported_utc TEXT NOT NULL, price_basis TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS price_crosscheck (
    symbol TEXT NOT NULL, date TEXT NOT NULL, breeze_close REAL NOT NULL, external_close REAL NOT NULL,
    diff_pct REAL NOT NULL, explained_by TEXT, source_ref TEXT, checked_utc TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS data_imports (
    dataset TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT, imported_utc TEXT NOT NULL,
    rows INTEGER NOT NULL, note TEXT
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
        """Nothing is saved if any option trade has a sold leg without its bought hedge (UnhedgedShort)."""
        for n, t in enumerate(trades):
            legs = t.get("legs") if isinstance(t, dict) else None
            if legs and unhedged_short_legs(legs, t.get("expiry"), t.get("lots"), t.get("lot_size")):
                raise UnhedgedShort(f"UNHEDGED_SHORT: backtest {run_id} trade {n} ({t.get('symbol')} {t.get('expiry')}) "
                                    f"has a sold option without a bought option of the same right and expiry; "
                                    f"nothing was saved")
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

    def events_between(self, a: date, b: date) -> list[dict]:
        """Events dated a..b (inclusive), oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT date,type,symbol,note FROM events WHERE date>=? AND date<=? ORDER BY date,type,symbol,note",
                (a.isoformat(), b.isoformat())).fetchall()
        return [{"date": date.fromisoformat(r[0]), "type": r[1], "symbol": r[2], "note": r[3]} for r in rows]

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

    def fii_dii_all(self) -> list[dict]:
        """Every stored FII/DII row, oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT date,fii_net_cr,dii_net_cr FROM fii_dii ORDER BY date").fetchall()
        return [{"date": date.fromisoformat(r[0]), "fii_net_cr": r[1], "dii_net_cr": r[2]} for r in rows]

    def all_events(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT date,type,symbol,note FROM events ORDER BY date,type,symbol,note").fetchall()
        return [{"date": date.fromisoformat(r[0]), "type": r[1], "symbol": r[2], "note": r[3]} for r in rows]

    # ---------------- imported reference data (external repo, inbox files) ----------------
    CA_COLS = ("symbol", "ex_date", "action_type", "purpose", "record_date", "announced_on", "ratio", "price_factor",
               "factor_basis", "source", "source_ref", "imported_utc")

    def save_corporate_actions(self, rows: list[dict], replace_source: bool = True) -> int:
        """Insert actions. A row already stored from another source for the same (symbol, ex_date, type,
        purpose) is kept (first source wins); `replace_source`: a row of the same source is refreshed."""
        added = 0
        with self._lock:
            for r in rows:
                key = (r["symbol"], r["ex_date"], r["action_type"], r["purpose"])
                old = self._db.execute("SELECT source FROM corporate_actions WHERE symbol=? AND ex_date=? AND "
                                       "action_type=? AND purpose=?", key).fetchone()
                if old is not None and (old[0] != r["source"] or not replace_source):
                    continue
                self._db.execute(f"INSERT OR REPLACE INTO corporate_actions VALUES ({','.join('?' * len(self.CA_COLS))})",
                                 tuple(r.get(c) for c in self.CA_COLS))
                added += old is None
            self._db.commit()
        return added

    def corporate_actions(self, symbol: str | None = None, types: tuple[str, ...] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM corporate_actions WHERE 1=1", []
        if symbol is not None:
            sql += " AND symbol=?"
            args.append(symbol)
        if types:
            sql += f" AND action_type IN ({','.join('?' * len(types))})"
            args += list(types)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY symbol, ex_date", args).fetchall()
        return [dict(zip(self.CA_COLS, r)) for r in rows]

    def save_positioning(self, rows: list[tuple[str, str, str, str, str]]) -> int:
        """(date, data_json, source, source_ref, imported_utc)."""
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO positioning_daily VALUES (?,?,?,?,?)", rows)
            self._db.commit()
        return len(rows)

    def positioning_dates(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT date FROM positioning_daily ORDER BY date")]

    def save_external_bars(self, rows: list[tuple]) -> int:
        """(symbol, date, open, high, low, close, volume, source, source_ref, imported_utc, price_basis)."""
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO external_bars_1d VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._db.commit()
        return len(rows)

    def external_bars(self, symbol: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT date,open,high,low,close,volume,source,price_basis FROM external_bars_1d "
                                    "WHERE symbol=? ORDER BY date", (symbol,)).fetchall()
        return [dict(zip(("date", "open", "high", "low", "close", "volume", "source", "price_basis"), r)) for r in rows]

    def external_bar_summary(self) -> dict[str, tuple[str, str, int]]:
        with self._lock:
            return {s: (a, b, n) for s, a, b, n in self._db.execute(
                "SELECT symbol, MIN(date), MAX(date), COUNT(*) FROM external_bars_1d GROUP BY symbol")}

    def save_crosscheck(self, rows: list[tuple]) -> int:
        """(symbol, date, breeze_close, external_close, diff_pct, explained_by, source_ref, checked_utc)."""
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO price_crosscheck VALUES (?,?,?,?,?,?,?,?)", rows)
            self._db.commit()
        return len(rows)

    def crosscheck_rows(self) -> list[dict]:
        cols = ("symbol", "date", "breeze_close", "external_close", "diff_pct", "explained_by", "source_ref", "checked_utc")
        with self._lock:
            return [dict(zip(cols, r)) for r in self._db.execute("SELECT * FROM price_crosscheck ORDER BY symbol, date")]

    def log_import(self, dataset: str, source: str, source_ref: str | None, at: datetime, rows: int, note: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO data_imports VALUES (?,?,?,?,?,?)",
                             (dataset, source, source_ref, at.isoformat(), rows, note))
            self._db.commit()

    def last_imports(self) -> dict[str, dict]:
        """The newest import of each dataset."""
        with self._lock:
            rows = self._db.execute("SELECT dataset, source, source_ref, imported_utc, rows, note FROM data_imports "
                                    "ORDER BY imported_utc").fetchall()
        out = {}
        for d, s, ref, at, n, note in rows:
            out[d] = {"source": s, "source_ref": ref, "imported_utc": at, "rows": n, "note": note}
        return out

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

    # ---------------- phase 4: instruments, option chain, option history, margins ----------------
    def replace_instruments(self, source: str, rows: list[tuple]) -> None:
        """rows: (underlying, expiry iso, lot_size, kind, strikes list, as_of_utc iso). Replaces the
        whole list for `source`: the broker's list only holds current contracts."""
        self._guard(source)
        with self._lock:
            self._db.execute("DELETE FROM instruments WHERE source=?", (source,))
            self._db.executemany(
                "INSERT INTO instruments (underlying,source,expiry,lot_size,kind,strikes_json,as_of_utc)"
                " VALUES (?,?,?,?,?,?,?)",
                [(u, source, e, lot, kind, json.dumps(strikes), asof) for u, e, lot, kind, strikes, asof in rows])
            self._db.commit()

    def instruments(self, source: str, underlying: str | None = None) -> list[dict]:
        sql = "SELECT underlying,expiry,lot_size,kind,strikes_json,as_of_utc FROM instruments WHERE source=?"
        args: list = [source]
        if underlying is not None:
            sql += " AND underlying=?"
            args.append(underlying)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY underlying, expiry", args).fetchall()
        return [{"underlying": r[0], "expiry": date.fromisoformat(r[1]), "lot_size": r[2], "kind": r[3],
                 "strikes": json.loads(r[4]), "as_of": _ts(r[5])} for r in rows]

    def save_option_chain(self, source: str, rows: list[tuple]) -> int:
        """rows: (underlying, expiry iso, strike, right, as_of_utc iso, bid, ask, last, oi, oi_change, volume, spot)."""
        self._guard(source)
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO option_chain (underlying,source,expiry,strike,right,as_of_utc,bid,ask,"
                "last,oi,oi_change,volume,spot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r[0], source, *r[1:]) for r in rows])
            self._db.commit()
        return len(rows)

    def latest_option_chain(self, source: str, underlying: str, expiry: date) -> list[dict]:
        """Rows of the newest stored snapshot for one underlying and expiry."""
        with self._lock:
            r = self._db.execute("SELECT MAX(as_of_utc) FROM option_chain WHERE underlying=? AND source=? AND expiry=?",
                                 (underlying, source, expiry.isoformat())).fetchone()
            if not r or not r[0]:
                return []
            rows = self._db.execute(
                "SELECT strike,right,as_of_utc,bid,ask,last,oi,oi_change,volume,spot FROM option_chain"
                " WHERE underlying=? AND source=? AND expiry=? AND as_of_utc=? ORDER BY strike, right",
                (underlying, source, expiry.isoformat(), r[0])).fetchall()
        keys = ["strike", "right", "as_of", "bid", "ask", "last", "oi", "oi_change", "volume", "spot"]
        return [{**dict(zip(keys, x)), "as_of": _ts(x[2])} for x in rows]

    def prune_option_chain(self, source: str, keep_after: datetime) -> int:
        self._guard(source)
        with self._lock:
            cur = self._db.execute("DELETE FROM option_chain WHERE source=? AND as_of_utc < ?",
                                   (source, keep_after.isoformat()))
            self._db.commit()
        return cur.rowcount

    def save_option_bars(self, source: str, underlying: str, expiry: date, strike: float, right: str,
                         interval: str, bars: list[tuple], fetched: datetime) -> int:
        """bars: (start_utc iso, open, high, low, close, volume, oi). Also records that this contract
        was asked for (even when nothing came back), so it is not asked for again."""
        self._guard(source)
        key = (underlying, expiry.isoformat(), float(strike), right, interval, source)
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO option_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(*key, *b) for b in bars])
            self._db.execute("INSERT OR REPLACE INTO option_fetch_log VALUES (?,?,?,?,?,?,?,?)",
                             (*key, fetched.isoformat(), len(bars)))
            self._db.commit()
        return len(bars)

    def option_bars(self, source: str, underlying: str, interval: str = "1day") -> dict[tuple, list[tuple]]:
        """Every stored bar of an underlying: (expiry, strike, right) -> [(start_utc, o, h, l, c, volume, oi)]."""
        with self._lock:
            rows = self._db.execute(
                "SELECT expiry,strike,right,start_utc,open,high,low,close,volume,oi FROM option_bars"
                " WHERE underlying=? AND source=? AND interval=? ORDER BY start_utc",
                (underlying, source, interval)).fetchall()
        out: dict[tuple, list[tuple]] = {}
        for r in rows:
            out.setdefault((date.fromisoformat(r[0]), r[1], r[2]), []).append((_ts(r[3]), *r[4:]))
        return out

    def option_contract_bars(self, source: str, underlying: str, expiry: date, strike: float, right: str,
                             interval: str) -> list[tuple]:
        """Stored bars of ONE contract, oldest first: (start_utc, o, h, l, c, volume, oi)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT start_utc,open,high,low,close,volume,oi FROM option_bars WHERE underlying=? AND expiry=?"
                " AND strike=? AND right=? AND interval=? AND source=? ORDER BY start_utc",
                (underlying, expiry.isoformat(), float(strike), right, interval, source)).fetchall()
        return [(_ts(r[0]), *r[1:]) for r in rows]

    # ---------------- request budget (option history) ----------------
    def requests_used(self, day: date, kind: str = "option_history") -> int:
        with self._lock:
            r = self._db.execute("SELECT requests FROM request_budget WHERE day=? AND kind=?",
                                 (day.isoformat(), kind)).fetchone()
        return r[0] if r else 0

    def set_day_budget(self, day: date, n: int, kind: str = "option_history") -> None:
        """Record a request budget raised (or lowered) for one IST day only; config.yaml keeps its default."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO request_budget VALUES (?,?,?)", (day.isoformat(), kind + ":budget", int(n)))
            self._db.commit()

    def day_budget(self, day: date, kind: str = "option_history") -> int | None:
        """The budget recorded for that day, or None (then the config default applies)."""
        with self._lock:
            r = self._db.execute("SELECT requests FROM request_budget WHERE day=? AND kind=?",
                                 (day.isoformat(), kind + ":budget")).fetchone()
        return r[0] if r else None

    def add_requests(self, day: date, n: int = 1, kind: str = "option_history") -> None:
        with self._lock:
            self._db.execute("INSERT INTO request_budget VALUES (?,?,?) ON CONFLICT(day, kind)"
                             " DO UPDATE SET requests = requests + excluded.requests", (day.isoformat(), kind, n))
            self._db.commit()

    # ---------------- phase 9: campaigns (an original spread and its rolls) ----------------
    def save_campaign(self, summary: dict, updated: datetime) -> None:
        """Store a campaign summary (app/options/campaign.py: campaign_summary). Replaces the campaign's row."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO campaigns VALUES (?,?,?,?)",
                             (summary["campaign_id"], summary["stock"], updated.isoformat(),
                              json.dumps(summary, sort_keys=True, default=str)))
            self._db.commit()

    def campaigns(self, stock: str | None = None) -> list[dict]:
        """Stored campaigns, most recently updated first."""
        with self._lock:
            rows = self._db.execute("SELECT summary_json, updated_utc FROM campaigns"
                                    + (" WHERE stock=?" if stock else "") + " ORDER BY updated_utc DESC",
                                    (stock,) if stock else ()).fetchall()
        return [{**json.loads(r[0]), "updated_utc": r[1]} for r in rows]

    # ---------------- phase 9: daily shortlist ----------------
    def save_shortlist(self, session_date: date, rows: list[dict], created: datetime) -> int:
        """Replace the shortlist of one session. rows: stock, expiry (iso), side, rank, status, score,
        scores (dict), reasons (list)."""
        with self._lock:
            self._db.execute("DELETE FROM shortlist WHERE session_date=?", (session_date.isoformat(),))
            self._db.executemany(
                "INSERT OR REPLACE INTO shortlist VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(session_date.isoformat(), r["stock"], r.get("expiry") or "", r.get("side") or "", r.get("rank"),
                  r["status"], r.get("score"), json.dumps(r.get("scores") or {}, sort_keys=True),
                  json.dumps(r.get("reasons") or []), created.isoformat()) for r in rows])
            self._db.commit()
        return len(rows)

    def shortlist(self, session_date: date | None = None) -> dict:
        """The shortlist of a session (default: the latest stored one), ranked rows first."""
        with self._lock:
            if session_date is None:
                r = self._db.execute("SELECT MAX(session_date) FROM shortlist").fetchone()
                if not r or r[0] is None:
                    return {"session_date": None, "created_utc": None, "rows": []}
                day = r[0]
            else:
                day = session_date.isoformat()
            rows = self._db.execute(
                "SELECT stock,expiry,side,rank,status,score,scores_json,reasons_json,created_utc FROM shortlist"
                " WHERE session_date=? ORDER BY rank IS NULL, rank, stock, side", (day,)).fetchall()
        return {"session_date": day, "created_utc": rows[0][8] if rows else None,
                "rows": [{"stock": r[0], "expiry": r[1] or None, "side": r[2] or None, "rank": r[3], "status": r[4],
                          "score": r[5], "scores": json.loads(r[6]), "reasons": json.loads(r[7])} for r in rows]}

    def option_fetch_log(self, source: str, underlying: str, interval: str = "1day") -> dict[tuple, int]:
        """(expiry, strike, right) -> rows received, for every contract already asked for."""
        with self._lock:
            rows = self._db.execute(
                "SELECT expiry,strike,right,rows FROM option_fetch_log WHERE underlying=? AND source=? AND interval=?",
                (underlying, source, interval)).fetchall()
        return {(date.fromisoformat(r[0]), r[1], r[2]): r[3] for r in rows}

    def option_history_summary(self, source: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT underlying, interval, COUNT(DISTINCT expiry||'|'||strike||'|'||right), COUNT(*),"
                " MIN(start_utc), MAX(start_utc), MIN(expiry), MAX(expiry) FROM option_bars WHERE source=?"
                " GROUP BY underlying, interval ORDER BY underlying", (source,)).fetchall()
        keys = ["underlying", "interval", "contracts", "bars", "first_bar", "last_bar", "first_expiry", "last_expiry"]
        return [dict(zip(keys, r)) for r in rows]

    def save_option_expiry(self, source: str, underlying: str, expiry: date, kind: str, found: datetime) -> None:
        self._guard(source)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO option_expiries VALUES (?,?,?,?,?)",
                             (underlying, source, expiry.isoformat(), kind, found.isoformat()))
            self._db.commit()

    def option_expiries(self, source: str, underlying: str) -> list[tuple[date, str]]:
        with self._lock:
            rows = self._db.execute("SELECT expiry, kind FROM option_expiries WHERE underlying=? AND source=?"
                                    " ORDER BY expiry", (underlying, source)).fetchall()
        return [(date.fromisoformat(e), k) for e, k in rows]

    def save_expiry_miss(self, source: str, underlying: str, kind: str, period: str, checked: datetime) -> None:
        """A week or month in which no expiry was found (so it is not searched again)."""
        self._guard(source)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO option_expiry_misses VALUES (?,?,?,?,?)",
                             (underlying, source, kind, period, checked.isoformat()))
            self._db.commit()

    def expiry_misses(self, source: str, underlying: str, kind: str) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT period FROM option_expiry_misses WHERE underlying=? AND source=? AND kind=?",
                                    (underlying, source, kind)).fetchall()
        return {r[0] for r in rows}

    def replace_margins(self, rows: list[tuple[str, str, float, float, str]]) -> None:
        """rows: (underlying, structure, width, margin_per_lot_inr, as_of iso date)."""
        with self._lock:
            self._db.execute("DELETE FROM margins")
            self._db.executemany("INSERT INTO margins VALUES (?,?,?,?,?)", rows)
            self._db.commit()

    def margins(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT underlying,structure,width,margin_per_lot_inr,as_of FROM margins"
                                    " ORDER BY underlying, structure, width").fetchall()
        return [{"underlying": r[0], "structure": r[1], "width": r[2], "margin_per_lot_inr": r[3],
                 "as_of": date.fromisoformat(r[4])} for r in rows]
