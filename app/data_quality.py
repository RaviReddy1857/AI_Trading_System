"""Data-quality corrections and stock-year exclusion for the Phase 9 backtests.

Pre-registered 2026-11-15 and AMENDED the same day (docs/STATUS.md), before any 2023-2024 option data
was downloaded. Price data only (Breeze daily bars, the external repo's daily bars, corporate actions);
no option result is read. The same verdicts apply to the strategy, every variant and the baseline.

1. Exact corrections. A Breeze bar whose close, against the repo's, is off by exactly the factor of a
   later split or bonus that the Breeze history otherwise already reflects (the action shows no gap in
   Breeze) is a Breeze error that can be corrected exactly: the bar is multiplied by that factor in the
   adjusted series (table price_corrections; the raw store is not changed). Tolerance 2%.
2. One-day jumps. A move of the (corrected) Breeze close over 20% on a session with no split, bonus or
   demerger is a real market move when the repo confirms it (same date, same direction, within 5
   percentage points); otherwise it excludes the stock-year.
3. Unexplained mismatches. For a stock-year with any session whose Breeze close (corrected) differs from
   the repo's by more than 0.5% after the later applied corporate-action factors: the ratio Breeze /
   repo is tested day to day. A step over 2% on a day with no corporate action or dividend (none with an
   ex-date since the previous session) is ERRATIC, unless the stock moved more than 8% that day in either
   source (closing-price definition noise on a volatile day). Any erratic day excludes the stock-year; a
   ratio that only steps on ex-dates is the repo's own adjustment and the stock-year is kept.
Repo filler bars (open = high = low = close = the previous close) are repo errors and are left out of
every comparison. A stock-year listed in data_quality.resolved_stock_years is kept (resolution recorded in
STATUS.md). Exclusion: no entry in the stock-year or in the 45 calendar days before it.

Run:  python -m app.data_quality        (needs the external repo clone; stores corrections and verdicts)
"""
from __future__ import annotations

import argparse
import itertools
import json
from collections import defaultdict
from datetime import date, timedelta

from .corporate_actions import PRICE_ACTIONS, AdjustedHistory
from .history import bar_date

RULE = "dq-2026-11-15-amended"
ENTRY_MARGIN_DAYS = 45
CORRECTION_TOL = 0.02
MATCH_TOL_PCT = 0.5
CONFIRM_PP = 5.0
STEP_PCT = 2.0
VOLATILE_PCT = 8.0


def repo_series(rows: list[dict] | None) -> dict[date, float]:
    """The repo's closes without its filler bars (O = H = L = C = the previous close)."""
    rows = sorted(rows or [], key=lambda r: r["date"])
    out, prev = {}, None
    for r in rows:
        filler = prev is not None and r["open"] == r["high"] == r["low"] == r["close"] and r["close"] == prev
        if not filler:
            out[r["date"]] = r["close"]
        prev = r["close"]
    return out


def _later_product(factors: list[tuple[date, float]], d: date) -> float:
    f = 1.0
    for ex, x in factors:
        if ex > d:
            f *= x
    return f


def corrections(bars, repo: dict[date, float], actions: list[dict], max_gap_pct: float) -> list[tuple[str, float, str]]:
    """(date, multiplier, reason) of the Breeze bars off by exactly a later pre-adjusted action's factor."""
    raw = AdjustedHistory(bars, [a for a in actions if a["action_type"] in PRICE_ACTIONS], max_gap_pct)
    applied = [(x["ex_date"], x["factor"]) for x in raw.factors if x["factor"]]
    pre = []
    for x in raw.factors:
        if x["factor"] or "not seen in the prices" not in x["basis"]:
            continue
        fs = [a.get("price_factor") for a in x["actions"]]
        if all(fs):
            f = 1.0
            for v in fs:
                f *= v
            pre.append((x["ex_date"], f, x["name"]))
    if not pre:
        return []
    out = []
    status: dict[int, float | None] = {}          # bar index -> multiplier (1.0 = fine), None = no repo bar to judge
    for i, b in enumerate(bars):
        d = bar_date(b)
        if d not in repo or b.close <= 0:
            status[i] = None
            continue
        status[i] = 1.0
        a = _later_product(applied, d)
        r = repo[d] / b.close
        if abs(r / a - 1) <= CORRECTION_TOL:
            continue
        later = [p for p in pre if p[0] > d]
        hit = None
        for k in range(1, len(later) + 1):
            for combo in itertools.combinations(later, k):
                u = 1.0
                for _, f, _ in combo:
                    u *= f
                if abs(r / (a * u) - 1) <= CORRECTION_TOL:
                    hit = (u, combo)
                    break
            if hit:
                break
        if hit:
            names = ", ".join(f"{n} {ex.isoformat()} (x{f:.4f})" for ex, f, n in hit[1])
            status[i] = hit[0]
            out.append((d.isoformat(), hit[0], f"Breeze bar not adjusted for {names}, which the rest of the Breeze "
                                                f"history reflects; corrected by the exact factor"))
    # a bar with no repo bar to judge it (a repo filler or gap day) between two bars corrected by the same factor
    # belongs to the same unadjusted stretch: the same exact factor
    judged = [i for i, m in sorted(status.items()) if m is not None]
    for a, b in zip(judged, judged[1:]):
        if status[a] == status[b] != 1.0 and b - a > 1:
            for i in range(a + 1, b):
                if status.get(i) is None:
                    out.append((bar_date(bars[i]).isoformat(), status[a], "no repo bar that day; between bars corrected "
                                                                           "by the same exact factor"))
    return sorted(out)


def verdicts(settings, bars, repo: dict[date, float] | None, actions: list[dict], fixes: dict[date, float],
             since: date) -> dict[int, dict]:
    """{year: {sessions, unexplained, jumps, erratic_days, noise_days, reasons, explanation}} for one stock."""
    gap = settings.history.max_overnight_gap_pct
    adj = AdjustedHistory(bars, [a for a in actions if a["action_type"] in PRICE_ACTIONS], gap, fixes)
    cb = [(bar_date(b), b.close) for b in adj.bars]
    applied = [(x["ex_date"], x["factor"]) for x in adj.factors if x["factor"]]
    price_ex = {date.fromisoformat(a["ex_date"]) for a in actions if a["action_type"] in PRICE_ACTIONS}
    any_ex = {date.fromisoformat(a["ex_date"]) for a in actions}
    # a mismatch before a later demerger or rights issue is "possibly explained" (the repo's own adjustment) and is
    # not counted as unexplained (as pre-registered)
    maybe_ex = [date.fromisoformat(a["ex_date"]) for a in actions if a["action_type"] in ("DEMERGER", "RIGHTS")]
    cfg = settings.data_quality
    out = defaultdict(lambda: {"sessions": 0, "unexplained": 0, "jumps": [], "erratic_days": [], "noise_days": [],
                               "corrected_bars": 0})
    prev = None
    for k, (d, c) in enumerate(cb):
        if d < since:
            prev = (d, c)
            continue
        y = out[d.year]
        y["sessions"] += 1
        if prev and prev[1] > 0:
            mb = (c / prev[1] - 1) * 100
            if abs(mb) > cfg.max_one_day_jump_pct and not any(prev[0] < x <= d for x in price_ex):
                mr = (repo[d] / repo[prev[0]] - 1) * 100 if repo and d in repo and prev[0] in repo else None
                confirmed = mr is not None and (mr > 0) == (mb > 0) and abs(mr - mb) <= CONFIRM_PP
                y["jumps"].append({"date": d.isoformat(), "breeze_move_pct": round(mb, 1),
                                   "repo_move_pct": None if mr is None else round(mr, 1), "confirmed": confirmed})
        if (repo and d in repo and c > 0 and abs(repo[d] / c / _later_product(applied, d) - 1) * 100 > MATCH_TOL_PCT
                and not any(x > d for x in maybe_ex)):
            y["unexplained"] += 1
        prev = (d, c)
    # the smooth-ratio test, only for stock-years with unexplained mismatches
    if repo:
        last = None
        for d, c in cb:
            if d < since or c <= 0:
                last = None
                continue
            if d not in repo:                   # a repo filler bar or no repo bar: compared across it
                continue
            if last is not None and out[d.year]["unexplained"]:
                pd, pc, pr = last
                if not any(pd < x <= d for x in any_ex):
                    step = ((c / repo[d]) / (pc / pr) - 1) * 100
                    if abs(step) > STEP_PCT:
                        mb, mr = (c / pc - 1) * 100, (repo[d] / pr - 1) * 100
                        row = {"date": d.isoformat(), "ratio_step_pct": round(step, 2), "breeze_move_pct": round(mb, 1),
                               "repo_move_pct": round(mr, 1)}
                        (out[d.year]["noise_days"] if max(abs(mb), abs(mr)) > VOLATILE_PCT
                         else out[d.year]["erratic_days"]).append(row)
            last = (d, c, repo[d])
    for d0 in fixes:                        # every corrected bar of the year (also one just before the test start)
        if d0.year in out:
            out[d0.year]["corrected_bars"] += 1
            out[d0.year].setdefault("corrected_dates", []).append(d0.isoformat())
    for year, y in out.items():
        reasons = []
        bad_jumps = [j for j in y["jumps"] if not j["confirmed"]]
        if bad_jumps:
            reasons.append(f"one-day move(s) over {cfg.max_one_day_jump_pct:g}% with no corporate action that the repo does "
                           f"not confirm: " + ", ".join(f"{j['date']} {j['breeze_move_pct']:+.1f}% (repo "
                                                       f"{'-' if j['repo_move_pct'] is None else format(j['repo_move_pct'], '+.1f') + '%'})"
                                                       for j in bad_jumps))
        if y["erratic_days"]:
            reasons.append(f"Breeze/repo ratio erratic on {len(y['erratic_days'])} day(s) with no corporate action or "
                           f"dividend: " + ", ".join(f"{e['date']} {e['ratio_step_pct']:+.2f}%" for e in y["erratic_days"][:6]))
        y["reasons"] = reasons
        if not reasons:
            y["explanation"] = ("no unexplained mismatch" if not y["unexplained"] else
                                f"{y['unexplained']} unexplained mismatch day(s), but the ratio only steps on ex-dates or on "
                                f"volatile days: the repo's own (dividend or other) adjustment, not a data error")
            if y["corrected_bars"]:
                y["explanation"] += f"; {y['corrected_bars']} Breeze bar(s) corrected exactly"
            if any(j["confirmed"] for j in y["jumps"]):
                y["explanation"] += "; one-day move(s) over 20% confirmed by the repo (real moves)"
    return dict(out)


def run(settings, store, stocks: list[str], repo_rows, now) -> list[dict]:
    """Compute and store the corrections and the verdicts for these stocks. `repo_rows(sym)` -> the repo's
    daily rows (or None). Returns the verdict rows."""
    since = settings.backtest.test_start.get("1day") or date(2019, 1, 22)
    resolved = {x.strip().upper() for x in settings.data_quality.resolved_stock_years}
    rows = []
    for sym in stocks:
        bars = store.history_bars("bars_1d", sym, "breeze")
        if not bars:
            continue
        repo = repo_series(repo_rows(sym)) if repo_rows else None
        acts = store.corporate_actions(sym)
        fixes = corrections(bars, repo, acts, settings.history.max_overnight_gap_pct) if repo else []
        store.replace_price_corrections(sym, fixes, RULE, now)
        v = verdicts(settings, bars, repo, acts, {date.fromisoformat(d): m for d, m, _ in fixes}, since)
        for year, y in sorted(v.items()):
            key = f"{sym}:{year}"
            y["resolved"] = key in resolved
            y["repo_compared"] = repo is not None
            rows.append((sym, year, bool(y["reasons"]) and key not in resolved, json.dumps(y, default=str)))
    store.replace_quality_years(rows, RULE, now)
    return store.quality_years()


def excluded_sessions(years: list[dict], sessions: list[date]) -> dict[str, set[date]]:
    """Per stock, the sessions with no entry: an excluded year and the ENTRY_MARGIN_DAYS before it."""
    bad = defaultdict(set)
    for r in years:
        if r["excluded"]:
            bad[r["symbol"]].add(r["year"])
    return {s: {d for d in sessions if d.year in ys or (d + timedelta(days=ENTRY_MARGIN_DAYS)).year in ys}
            for s, ys in bad.items()}


def main(argv: list[str] | None = None) -> int:
    from .config import load_settings
    from .external_data import read_stock_parquet
    from .models import utcnow
    from .store import Store
    ap = argparse.ArgumentParser(description="Data-quality corrections and stock-year verdicts (price data only)")
    ap.add_argument("--stocks", nargs="*", help="default: every watchlist stock")
    args = ap.parse_args(argv)
    s = load_settings()
    store = Store(s.path(s.storage.db_path_live), live=True)
    repo = s.path(s.external_data.repo_path)
    stocks = args.stocks or [i.symbol for i in s.watchlist if i.kind == "stock"]
    rows = run(s, store, stocks, (lambda sym: read_stock_parquet(repo, sym)) if (repo / "LICENSE").exists() else None, utcnow())
    since_year = (s.backtest.test_start.get("1day") or date(2019, 1, 22)).year
    for r in rows:
        if r["year"] >= since_year and (r["excluded"] or r["detail"].get("corrected_bars")):
            print(f"{r['symbol']:11s} {r['year']}  {'EXCLUDED' if r['excluded'] else 'kept    '}  "
                  + ("; ".join(r["detail"]["reasons"]) if r["excluded"] else r["detail"].get("explanation", ""))[:220])
    ex = [r for r in rows if r["excluded"] and r["year"] >= since_year]
    print(f"\n{len(ex)} stock-year(s) excluded from {since_year}: " + ", ".join(f"{r['symbol']} {r['year']}" for r in ex))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
