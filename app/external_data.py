"""One-time import from the external GitHub repo aswinv90/indian-market-historical-data.

LICENCE: the repo's data is for PERSONAL, NON-COMMERCIAL research use only, with no redistribution. It is
never published, never committed to git (the imported rows live in the SQLite store and in gitignored
overlay files) and never used in anything published.

The repo is cloned by hand into a folder outside the project (external_data.repo_path). Only its data files
(CSV, Parquet) are read; none of its scripts is ever run. Every imported row is tagged with source
"external_repo", the repo commit and the import time. Breeze data always takes priority where both exist.

Imported:
1. corporate actions since 2019 (splits, bonuses, demergers, rights, dividends) -> table corporate_actions;
   splits, bonuses and demergers adjust daily history (app/corporate_actions.py); raw prices stay as they are
2. RBI policy-rate dates since 2019 -> RBI_POLICY rows in data/external/events_external.csv
3. FII/DII daily cash figures -> data/external/fii_dii_external.csv (a day in your own fii_dii.csv wins)
4. derivatives positioning from 2024 (participant OI, FII index options, FII long ratio) -> positioning_daily
   (no rule uses it yet)
5. daily OHLCV since 2019-01-22 of every NSE F&O stock WITHOUT Breeze daily bars -> external_bars_1d,
   separate from the Breeze bars and labelled with their price basis
6. cross-check of the daily closes with Breeze for the priority stocks -> price_crosscheck (over 0.5%)
Nothing else is read (no mutual funds, gold, bonds or unlisted shares).

Run:  python -m app.external_data            (all steps)
      python -m app.external_data --history-report   (which INSUFFICIENT_HISTORY cases the adjustment resolves)
      python -m app.external_data --check-docs       (tracked docs must carry no value of the repo's rows)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import subprocess
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from .config import Settings, load_settings
from .corporate_actions import PRICE_ACTIONS, RECORD_ONLY, AdjustedHistory, stated_factor
from .history import bar_date
from .manual_data import Event, FiiDii, _atomic_write, events_to_csv, fii_dii_to_csv, parse_date
from .models import utcnow
from .quality import IST
from .store import Store

log = logging.getLogger("external_data")

SOURCE = "external_repo"
REPO_NAME = "aswinv90/indian-market-historical-data"
LICENCE_NOTE = "personal non-commercial research use only; no redistribution; never published"
CA_TYPES = PRICE_ACTIONS + RECORD_ONLY
FILES = {"corporate_actions": "data/CORPORATE_ACTIONS/corporate_actions.csv",
         "rbi": "data/MACRO/rbi_policy_rates.csv",
         "fii_dii": "data/FLOWS/fii_dii_cash.csv",
         "positioning": "data/FLOWS/fii_derivatives.csv",
         "stocks": "data/NSE/{symbol}_NS.parquet"}
PRICE_BASIS = "as published by the source (Close adjusted by the source for splits and bonuses; not raw prices)"


def repo_commit(repo: Path) -> tuple[str, str]:
    """(commit hash, commit date) of the local clone, read with git (none of the repo's own code is run)."""
    out = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%H %cI"], capture_output=True, text=True,
                         check=True, timeout=30).stdout.split()
    return out[0], out[1]


def _csv(repo: Path, key: str) -> list[dict]:
    with open(repo / FILES[key], encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _num(v) -> float | None:
    try:
        x = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def tag(ref: str, now: datetime) -> str:
    return f"source={SOURCE}; commit={ref[:12]}; imported={now.astimezone(IST).date().isoformat()}"


# ---------------- 1. corporate actions ----------------
def corporate_action_rows(rows: list[dict], since: date, ref: str, now: datetime) -> list[dict]:
    out = []
    for r in rows:
        t = (r.get("Action_Type") or "").strip().upper()
        if t not in CA_TYPES or (r.get("Series") or "").strip() not in ("EQ", "BE", ""):
            continue
        try:
            ex = parse_date(r["Ex_Date"])
        except (KeyError, ValueError):
            continue
        if ex < since:
            continue
        ratio, purpose = (r.get("Ratio_or_Amount") or "").strip(), (r.get("Purpose") or "").strip()
        factor, basis = stated_factor(t, ratio, purpose) if t in PRICE_ACTIONS else (None, "record only (not a price adjustment)")
        rec = (r.get("Record_Date") or "").strip()
        out.append({"symbol": r["Symbol"].strip().upper(), "ex_date": ex.isoformat(), "action_type": t,
                    "purpose": purpose or t, "record_date": rec or None, "announced_on": None, "ratio": ratio or None,
                    "price_factor": factor, "factor_basis": basis, "source": SOURCE,
                    "source_ref": f"{REPO_NAME}@{ref}", "imported_utc": now.isoformat()})
    return out


# ---------------- 2. RBI policy dates ----------------
def rbi_events(rows: list[dict], since: date, ref: str, now: datetime) -> list[Event]:
    out = []
    for r in rows:
        try:
            d = parse_date(r["Date"])
        except (KeyError, ValueError):
            continue
        if d < since:
            continue
        repo_rate = (r.get("Repo") or "").strip()
        what = (r.get("Event") or "").strip().replace(";", ",")
        note = (f"RBI policy: repo {repo_rate}% - {what} (the source's policy effective date); {tag(ref, now)}")
        out.append(Event(d, "RBI_POLICY", note, ""))
    return sorted(set(out), key=lambda e: e.date)


# ---------------- 3. FII / DII ----------------
def fii_dii_rows(rows: list[dict], ref: str, now: datetime) -> dict[date, FiiDii]:
    out = {}
    for r in rows:
        try:
            d = parse_date(r["Date"])
        except (KeyError, ValueError):
            continue
        fii, dii = _num(r.get("FII_Net_Cr")), _num(r.get("DII_Net_Cr"))
        if fii is None:
            continue
        out[d] = FiiDii(d, fii, dii, f"{SOURCE}@{ref[:12]} {now.astimezone(IST).date().isoformat()}")
    return out


# ---------------- 4. positioning ----------------
def positioning_rows(rows: list[dict], since: date, ref: str, now: datetime) -> list[tuple]:
    out = []
    for r in rows:
        try:
            d = parse_date(r["Date"])
        except (KeyError, ValueError):
            continue
        if d < since:
            continue
        data = {k: _num(v) for k, v in r.items() if k != "Date"}
        out.append((d.isoformat(), json.dumps(data, sort_keys=True), SOURCE, f"{REPO_NAME}@{ref}", now.isoformat()))
    return out


# ---------------- 5. daily bars ----------------
def read_stock_parquet(repo: Path, symbol: str) -> list[dict] | None:
    """The source's daily rows of one stock: {date, open, high, low, close, volume}; rows with a missing
    price are left out (never filled). None when the file is not there."""
    p = repo / FILES["stocks"].format(symbol=symbol)
    if not p.exists():
        return None
    import pyarrow.parquet as pq          # needed only for this import
    out = []
    for r in pq.read_table(p, columns=["Date", "Open", "High", "Low", "Close", "Volume"]).to_pylist():
        vals = [_num(r.get(k)) for k in ("Open", "High", "Low", "Close")]
        if r.get("Date") is None or any(v is None or v <= 0 for v in vals):
            continue
        d = r["Date"].date() if hasattr(r["Date"], "date") else parse_date(str(r["Date"])[:10])
        out.append({"date": d, "open": vals[0], "high": vals[1], "low": vals[2], "close": vals[3],
                    "volume": _num(r.get("Volume"))})
    return out


def fno_symbols() -> list[str]:
    """Every NSE F&O stock today: the NSE symbols (ExchangeCode) of the stock options in ICICI's public
    F&O list (no login needed)."""
    from .feeds.breeze_feed import nfo_scrip_master_lines
    reader = csv.reader(nfo_scrip_master_lines())
    head = [h.strip().strip('"') for h in next(reader)]
    ii, ei = head.index("InstrumentName"), head.index("ExchangeCode")
    return sorted({row[ei].strip().strip('"') for row in reader
                   if len(row) > ei and row[ii].strip().strip('"') == "OPTSTK" and row[ei].strip()})


# ---------------- 6. cross-check ----------------
def crosscheck(breeze: dict[date, float], external: dict[date, float], confirmed: list[tuple[date, float, str]],
               other_later: list[tuple[date, str]], limit_pct: float) -> list[dict]:
    """Days on which the two daily closes differ by more than `limit_pct`. `confirmed`: (ex-date, factor,
    name) of the splits, bonuses and demergers the Breeze prices show (applied_factors). A difference that
    those later actions explain (the source publishes adjusted closes) is labelled with them; one that
    only a later demerger or rights issue could explain is labelled "possibly ..." (the source's own
    adjustment, factor not known here). Anything else stays unexplained."""
    out = []
    for d in sorted(set(breeze) & set(external)):
        b, e = breeze[d], external[d]
        if b <= 0:
            continue
        diff = (e / b - 1) * 100
        if abs(diff) <= limit_pct:
            continue
        later = [(ex, f, n) for ex, f, n in confirmed if ex > d]
        f = 1.0
        for _, x, _ in later:
            f *= x
        explained = None
        if later and abs((e / (b * f) - 1) * 100) <= limit_pct:
            explained = ", ".join(f"{n} {ex.isoformat()}" for ex, _, n in later)
        else:
            maybe = [f"{n} {ex.isoformat()}" for ex, n in other_later if ex > d]
            if maybe:
                explained = "possibly " + ", ".join(maybe) + " (the source's adjustment; factor not known here)"
        out.append({"date": d, "breeze": b, "external": e, "diff_pct": round(diff, 3), "explained_by": explained})
    return out


# ---------------- the run ----------------
def run_import(settings: Settings, store: Store, repo: Path | None = None, now: datetime | None = None,
               fno: list[str] | None = None, priority: list[str] | None = None) -> dict:
    cfg = settings.external_data
    repo = repo or settings.path(cfg.repo_path)
    now = now or utcnow()
    if not (repo / "LICENSE").exists():
        raise FileNotFoundError(f"no clone of {REPO_NAME} at {repo}: clone it there first (see README)")
    ref, ref_date = repo_commit(repo)
    report: dict = {"repo": REPO_NAME, "commit": ref, "commit_date": ref_date, "imported_utc": now.isoformat(),
                    "licence": LICENCE_NOTE}

    # 1. corporate actions
    ca = corporate_action_rows(_csv(repo, "corporate_actions"), cfg.corporate_actions_since, ref, now)
    added = store.save_corporate_actions(ca)
    by_type: dict[str, int] = {}
    for r in ca:
        by_type[r["action_type"]] = by_type.get(r["action_type"], 0) + 1
    store.log_import("corporate_actions", SOURCE, ref, now, len(ca), json.dumps(by_type))
    report["corporate_actions"] = {"rows": len(ca), "new": added, "by_type": by_type,
                                   "price_factor_unreadable": sum(1 for r in ca if r["action_type"] in ("SPLIT", "BONUS")
                                                                  and r["price_factor"] is None)}

    # 2. RBI dates -> the events overlay (gitignored)
    ev = rbi_events(_csv(repo, "rbi"), cfg.history_since, ref, now)
    over = settings.path(settings.calendar.overlay("events"))
    _atomic_write(over, f"# Imported from {REPO_NAME}@{ref[:12]} ({LICENCE_NOTE}). Do not edit; re-run the import.\n"
                  + events_to_csv(ev))
    store.log_import("rbi_policy", SOURCE, ref, now, len(ev), str(over))
    report["rbi_policy"] = {"rows": len(ev), "first": ev[0].date.isoformat() if ev else None,
                            "last": ev[-1].date.isoformat() if ev else None, "file": str(over),
                            "note": "the source's dates are policy EFFECTIVE dates and include some holds, not every "
                                    "MPC meeting; check them against rbi.org.in"}

    # 3. FII/DII -> the fii_dii overlay (a day in the user's own file wins on load)
    fd = fii_dii_rows(_csv(repo, "fii_dii"), ref, now)
    own_path = settings.path(settings.calendar.fii_dii_csv)
    from .manual_data import parse_fii_dii
    own = parse_fii_dii(own_path.read_text(encoding="utf-8-sig"))[0] if own_path.exists() else {}
    over_fd = settings.path(settings.calendar.overlay("fii_dii"))
    _atomic_write(over_fd, fii_dii_to_csv(fd))
    store.log_import("fii_dii", SOURCE, ref, now, len(fd), str(over_fd))
    report["fii_dii"] = {"rows": len(fd), "first": min(fd).isoformat() if fd else None,
                         "last": max(fd).isoformat() if fd else None,
                         "days_you_already_had": sum(1 for d in fd if d in own), "file": str(over_fd)}

    # 4. positioning
    pos = positioning_rows(_csv(repo, "positioning"), cfg.positioning_since, ref, now)
    store.save_positioning(pos)
    store.log_import("positioning", SOURCE, ref, now, len(pos), "participant OI, FII index options, FII long ratio")
    report["positioning"] = {"rows": len(pos), "first": pos[0][0] if pos else None, "last": pos[-1][0] if pos else None,
                             "note": "stored only; no rule uses it yet"}

    # 5. daily bars of the F&O stocks without Breeze daily history
    fno = fno if fno is not None else fno_symbols()
    breeze_has = {s for s, c in store.history_counts("breeze").items() if c.get("bars_1d")}
    want = [s for s in fno if s not in breeze_has]
    stored, missing = {}, []
    for sym in want:
        rows = read_stock_parquet(repo, sym)
        if rows is None:
            missing.append(sym)
            continue
        use = [r for r in rows if r["date"] >= cfg.history_since]
        store.save_external_bars([(sym, r["date"].isoformat(), r["open"], r["high"], r["low"], r["close"], r["volume"],
                                   SOURCE, f"{REPO_NAME}@{ref}", now.isoformat(), PRICE_BASIS) for r in use])
        stored[sym] = len(use)
    store.log_import("external_bars_1d", SOURCE, ref, now, sum(stored.values()), f"{len(stored)} stocks")
    report["external_bars_1d"] = {"stocks": len(stored), "rows": sum(stored.values()), "fno_stocks": len(fno),
                                  "skipped_breeze_has_them": len(fno) - len(want), "not_in_repo": missing,
                                  "price_basis": PRICE_BASIS}

    # 6. cross-check with Breeze for the priority stocks
    pri = priority if priority is not None else _priority(settings)
    lim = cfg.crosscheck_mismatch_pct
    rows_out, per = [], {}
    for sym in pri:
        ext = read_stock_parquet(repo, sym)
        b = {bar_date(x): x.close for x in store.history_bars("bars_1d", sym, "breeze")}
        if not ext or not b:
            per[sym] = {"compared": 0, "mismatches": 0, "explained": 0, "note": "no data on one side"}
            continue
        e = {r["date"]: r["close"] for r in ext if r["date"] >= cfg.history_since}
        adj = AdjustedHistory(store.history_bars("bars_1d", sym, "breeze"), store.corporate_actions(sym, PRICE_ACTIONS),
                              settings.history.max_overnight_gap_pct)
        confirmed = [(x["ex_date"], x["factor"], x["name"]) for x in adj.factors if x["factor"]]
        other = [(date.fromisoformat(a["ex_date"]), a["action_type"]) for a in store.corporate_actions(sym, ("DEMERGER", "RIGHTS"))]
        mm = crosscheck(b, e, confirmed, other, lim)
        rows_out += [(sym, m["date"].isoformat(), m["breeze"], m["external"], m["diff_pct"], m["explained_by"],
                      f"{REPO_NAME}@{ref}", now.isoformat()) for m in mm]
        sure = [m for m in mm if m["explained_by"] and not m["explained_by"].startswith("possibly")]
        maybe = [m for m in mm if m["explained_by"] and m["explained_by"].startswith("possibly")]
        none = [m for m in mm if not m["explained_by"]]
        per[sym] = {"compared": len(set(b) & set(e)), "mismatches": len(mm), "explained": len(sure),
                    "possibly_explained": len(maybe), "unexplained": len(none),
                    "largest_unexplained_pct": max((abs(m["diff_pct"]) for m in none), default=None)}
    store.save_crosscheck(rows_out)
    store.log_import("crosscheck", SOURCE, ref, now, len(rows_out), f"over {lim}%")
    report["crosscheck"] = {"limit_pct": lim, "stocks": per, "mismatches": len(rows_out),
                            "explained_by_adjustment": sum(v["explained"] for v in per.values()),
                            "possibly_explained": sum(v["possibly_explained"] for v in per.values()),
                            "unexplained": sum(v["unexplained"] for v in per.values())}
    return report


def _priority(settings: Settings) -> list[str]:
    p = settings.path("data/option_download_status.json")
    if p.exists():
        try:
            return list(json.loads(p.read_text(encoding="utf-8")).get("stocks") or [])
        except ValueError:
            pass
    return [i.symbol for i in settings.watchlist if i.kind == "stock"][:30]


# ---------------- INSUFFICIENT_HISTORY: raw versus adjusted ----------------
def history_report(settings: Settings, store: Store, stocks: list[str], first: date, last: date) -> dict:
    """For each stock, the decision sessions in first..last on which the trend block's history (the
    bars after the last price break) is too short raw, and how many of them the corporate-action
    adjustment resolves (the break is explained by an applied split, bonus or demerger)."""
    sos = settings.stock_option_selling
    need = max(sos.sma_long, sos.trend_block.low_52w_sessions, sos.trend_block.rel_strength_lookback + 1)
    from .indicators import gap_breaks
    out = {}
    for sym in stocks:
        bars = store.history_bars("bars_1d", sym, "breeze")
        if not bars:
            continue
        acts = store.corporate_actions(sym, PRICE_ACTIONS)
        adj = AdjustedHistory(bars, acts, settings.history.max_overnight_gap_pct)
        raw_breaks = gap_breaks([b.open for b in bars], [b.close for b in bars], settings.history.max_overnight_gap_pct)
        short_raw = short_adj = 0
        for i, b in enumerate(bars):
            d = bar_date(b)
            if not (first <= d <= last):
                continue
            k = max([x for x in raw_breaks if x <= i], default=0)
            raw_len = i - k + 1
            adj_len = len(adj.window(i, 10 ** 6))
            short_raw += raw_len < need
            short_adj += adj_len < need
        applied = [f"{x['name']} {x['ex_date']}" for x in adj.factors if x["factor"]]
        not_applied = [f"{x['name']} {x['ex_date']}: {x['basis']}" for x in adj.factors if not x["factor"]]
        if short_raw or applied or not_applied:
            out[sym] = {"short_sessions_raw": short_raw, "short_sessions_adjusted": short_adj,
                        "resolved": short_raw - short_adj, "actions_applied": applied, "actions_not_applied": not_applied}
    return {"need_sessions": need, "period": [first.isoformat(), last.isoformat()], "stocks": out,
            "resolved_total": sum(v["resolved"] for v in out.values())}


# ---------------- tracked docs must not carry the repo's rows ----------------
# The licence forbids redistribution, and the docs are tracked in git (a GitHub remote). A doc may give
# factors, methods and aggregates; never a value of the repo's rows. A raw value may be kept only in the
# gitignored diagnostics (data/diagnostics/).
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_NUM = re.compile(r"(?<![\w.,/-])(-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|-?\d+\.\d+)(?![\d%]|\s*%)")
_SYM = re.compile(r"\b[A-Z][A-Z0-9&]{1,14}\b")
# "repo 138.76", "repo close 137.79", "against repo 482.875": a price-like value right after the word repo
# (the RBI repo RATE is a percentage and is not matched)
REPO_VALUE = re.compile(r"\brepo\b(?:'s)?(?:\s+(?:close[sd]?|price[sd]?|value[sd]?))?\s*[:=]?\s*\(?\s*"
                        r"(\d[\d,]*\.\d+)(?!\d|\s*%)", re.I)


def doc_candidates(text: str) -> list[tuple[int, str, list[date], list[float], set[str]]]:
    """(line number, line, its dates, its decimal numbers of at least 1 that are not percentages, its
    upper-case words) of every line that has both a date and such a number."""
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        dates = []
        for m in _DATE.finditer(line):
            try:
                dates.append(date.fromisoformat(m.group(1)))
            except ValueError:
                pass
        if not dates:
            continue
        rest = _DATE.sub(" ", line)
        nums = [float(m.group(1).replace(",", "")) for m in _NUM.finditer(rest)]
        nums = [x for x in nums if abs(x) >= 1]
        if nums:
            out.append((n, line, dates, nums, set(_SYM.findall(line))))
    return out


def external_values(settings: Settings, store: Store | None, wanted: dict[date, set[str]]) -> dict[date, set[float]]:
    """Every value of the external repo's rows on the given dates that this machine holds: the gitignored
    overlays in data/external/, the store's external tables (external bars, cross-check closes, positioning)
    and, for the stocks named next to a date, the clone's own daily files (if the clone is there)."""
    out: dict[date, set[float]] = {}

    def add(d: date, *vals) -> None:
        if d in wanted:
            out.setdefault(d, set()).update(float(v) for v in vals if isinstance(v, (int, float)))

    ext_dir = settings.path("data/external")
    for p in sorted(ext_dir.glob("*.csv")) if ext_dir.exists() else ():
        lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x and not x.startswith("#")]
        for row in csv.DictReader(lines):
            try:
                d = parse_date(row.get("date") or "")
            except ValueError:
                continue
            for v in row.values():
                try:
                    add(d, float(str(v).replace(",", "")))
                except ValueError:
                    pass
    if store is not None and wanted:
        keys = [d.isoformat() for d in wanted]
        marks = ",".join("?" * len(keys))
        with store._lock:
            bars = store._db.execute(f"SELECT date, open, high, low, close FROM external_bars_1d WHERE date IN ({marks})",
                                     keys).fetchall()
            cross = store._db.execute(f"SELECT date, external_close FROM price_crosscheck WHERE date IN ({marks})",
                                      keys).fetchall()
            pos = store._db.execute(f"SELECT date, data_json FROM positioning_daily WHERE date IN ({marks})",
                                    keys).fetchall()
        for d, *vals in bars + cross:
            add(date.fromisoformat(d), *vals)
        for d, js in pos:
            data = json.loads(js)
            add(date.fromisoformat(d), *(v for v in (data.values() if isinstance(data, dict) else ())))
    repo = settings.path(settings.external_data.repo_path)
    symbols = sorted({s for v in wanted.values() for s in v})
    if repo.exists():
        for sym in symbols:
            try:
                rows = read_stock_parquet(repo, sym)
            except Exception:                # an unreadable file is not a finding
                rows = None
            for r in rows or ():
                add(r["date"], r["open"], r["high"], r["low"], r["close"])
    return out


def doc_leaks(files: dict[str, str], values: Callable[[dict[date, set[str]]], dict[date, set[float]]]) -> list[str]:
    """Lines of tracked docs that carry a value of the external repo's rows: a number on a line next to a
    date on which the repo has that value (to the paisa), or a price-like value written right after the
    word "repo". `values(wanted)` gives the repo's values on the wanted dates (see external_values)."""
    found = []
    cands = {name: doc_candidates(text) for name, text in files.items()}
    wanted: dict[date, set[str]] = {}
    for rows in cands.values():
        for _, _, dates, _, syms in rows:
            for d in dates:
                wanted.setdefault(d, set()).update(syms)
    vals = values(wanted) if wanted else {}
    for name, text in files.items():
        for n, line in enumerate(text.splitlines(), start=1):
            if m := REPO_VALUE.search(line):
                found.append(f"{name}:{n}: value after 'repo': {m.group(1)}")
        for n, line, dates, nums, _ in cands[name]:
            hits = sorted({x for d in dates for x in nums if any(abs(x - v) < 0.005 for v in vals.get(d, ()))})
            if hits:
                found.append(f"{name}:{n}: value(s) of the repo's rows on {', '.join(d.isoformat() for d in dates)}: "
                             + ", ".join(f"{x:g}" for x in hits))
    return sorted(set(found), key=lambda x: (x.split(":")[0], int(x.split(":")[1])))


def tracked_docs(root: Path) -> dict[str, str]:
    """Every tracked text document of the project (git ls-files: *.md, *.txt, *.csv), by path."""
    names = subprocess.run(["git", "ls-files", "*.md", "*.txt", "*.csv"], cwd=root, capture_output=True, text=True,
                           check=True).stdout.split()
    return {n: (root / n).read_text(encoding="utf-8", errors="replace") for n in names if (root / n).exists()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"One-time import from {REPO_NAME} ({LICENCE_NOTE})")
    ap.add_argument("--history-report", action="store_true", help="only report which INSUFFICIENT_HISTORY cases the "
                                                                  "corporate-action adjustment resolves")
    ap.add_argument("--check-docs", action="store_true", help="only list lines of the tracked docs that carry a value "
                                                              "of the repo's rows (none may)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    store = Store(settings.path(settings.storage.db_path_live), live=True)
    if args.check_docs:
        leaks = doc_leaks(tracked_docs(settings.path(".")), lambda w: external_values(settings, store, w))
        print("\n".join(leaks) or "no repo values in the tracked docs")
        return 1 if leaks else 0
    out_dir = settings.path("data/diagnostics")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.history_report:
        rep = run_import(settings, store)
        (out_dir / "external_import.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        print(json.dumps({k: v for k, v in rep.items() if k != "crosscheck"}, indent=2, default=str))
        cc = rep["crosscheck"]
        print(f"cross-check: {cc['mismatches']} day(s) over {cc['limit_pct']}%: {cc['explained_by_adjustment']} explained "
              f"by a split, bonus or demerger the source adjusts for, {cc['possibly_explained']} possibly by a later "
              f"demerger or rights issue, {cc['unexplained']} unexplained")
        for s, v in cc["stocks"].items():
            print(f"  {s:12s} {v}")
    pri = _priority(settings)
    hr = history_report(settings, store, pri, settings.backtest.test_start.get("1day") or date(2019, 1, 22),
                        utcnow().astimezone(IST).date())
    (out_dir / "insufficient_history_report.json").write_text(json.dumps(hr, indent=2), encoding="utf-8")
    print(f"\nINSUFFICIENT_HISTORY (trend block needs {hr['need_sessions']} sessions): {hr['resolved_total']} "
          f"decision session(s) resolved by the adjustment")
    for s, v in hr["stocks"].items():
        print(f"  {s:12s} short raw {v['short_sessions_raw']:4d}  adjusted {v['short_sessions_adjusted']:4d}  "
              f"applied {v['actions_applied']}  not applied {v['actions_not_applied']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
