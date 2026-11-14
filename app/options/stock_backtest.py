"""Phase 9: backtest of stock option selling, and the download of the option history it needs.

Universe: the watchlist stocks that have option contracts in the instrument list, ranked by liquidity
(median daily traded value, close x volume, over the last `liquidity_lookback_days` sessions of the
development period). The `priority_stocks` most liquid ones are downloaded and tested.

Backtest (app/options/backtest.py engine, same rules as the index spreads): signal at a day's close,
entry at the next session's open of both legs, one lot at today's lot size, costs and slippage on
every leg. Target and stop are checked on the legs' 5-minute bars ("intraday", filled at the next
5-minute bar) and, for comparison, on daily closes filled at the next session's open ("daily"); the
report shows strategy and baseline under both. Forced exit `exit_days_before_expiry` days before
expiry; optional exit when the stock closes beyond the sold strike. The holdout is never opened.

No-filter baseline: the same spread (same strike rules, hedge rule, exits) on each side, one attempt
per monthly expiry on the first session it is inside the entry window, with no trend, IV-rank or
event rule.

Download (--download): works within the daily request budget (options.backtest.daily_request_budget,
counted in the store per day across runs), newest year first and, inside a year, the most liquid
stock first. Everything already stored (or already asked for) is skipped, so a run on a later day
simply carries on. A Breeze error (session expired, rate limit, network) stops the download at once;
what was fetched stays stored, and the reason and the progress are written to the status file.

Run:  python -m app.options.stock_backtest                 # report from stored prices
      python -m app.options.stock_backtest --download      # fetch within today's budget, then report
      python -m app.options.stock_backtest --status        # liquidity ranking, budget and progress
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from ..backtest import Engine, dev_holdout, load_calendar
from ..config import Settings
from ..history import bar_date
from ..models import utcnow
from ..quality import IST
from ..store import Store
from . import build_option_strategies
from .backtest import (OptionEngine, baseline_variants, prepare_history, print_report, run_option_backtest)
from .option_history import OptionHistory, OptionIntraday, budgeted_fetcher
from .stock_selling import StockOptionSelling, build_stock_selling

log = logging.getLogger("stock_backtest")
STATUS_FILE = "data/option_download_status.json"


def liquid_stocks(settings: Settings, engine: OptionEngine, before: date | None = None) -> list[dict]:
    """Watchlist stocks with option contracts, most liquid first. Liquidity = the median of
    close x volume over the last `liquidity_lookback_days` daily bars before `before` (default: the
    holdout start, so the holdout is not looked at)."""
    n = settings.stock_option_selling.backtest.liquidity_lookback_days
    out = []
    for inst in settings.watchlist:
        if inst.kind != "stock" or not engine.lot_size(inst.symbol):
            continue
        bars = [b for b in engine.base.daily(inst.symbol) if before is None or bar_date(b) < before]
        values = [b.close * b.volume for b in bars[-n:] if b.volume]
        if len(values) < n:
            continue                                  # not enough history to rank: left out, not guessed
        out.append({"stock": inst.symbol, "median_traded_value_inr": round(statistics.median(values), 0),
                    "lot_size": engine.lot_size(inst.symbol)})
    out.sort(key=lambda r: (-r["median_traded_value_inr"], r["stock"]))
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    return out


def priority_stocks(settings: Settings, engine: OptionEngine) -> list[str]:
    hold = settings.backtest.holdout_start.get("1day")
    ranked = liquid_stocks(settings, engine, hold)
    return [r["stock"] for r in ranked[:settings.stock_option_selling.backtest.priority_stocks]]


def read_status(settings: Settings) -> dict:
    p = settings.path(STATUS_FILE)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"complete": {}, "runs": []}


def write_status(settings: Settings, status: dict) -> Path:
    p = settings.path(STATUS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)
    return p


def download_blocks(settings: Settings, engine: OptionEngine, strategy: StockOptionSelling, stocks: list[str],
                    dev: list[date], status: dict, progress=print) -> dict:
    """Fetch what the backtest needs, newest year first, most liquid stock first, until the request
    budget is used or Breeze returns an error. Marks a (stock, year) complete when its streams ran
    without wanting anything that is not stored. Returns a summary of this run."""
    hist: OptionHistory = engine.prices
    intr = engine.intraday
    years = sorted({d.year for d in dev}, reverse=True)
    done = {s: set(v) for s, v in status.get("complete", {}).items()}
    engine.last_session = dev[-1] if dev else None           # nothing of the holdout period is asked for
    stopped = None
    newly = 0

    def missing() -> int:
        return len(hist.not_stored) + (len(intr.not_stored) if intr is not None else 0)

    for year in years:
        block = [d for d in dev if d.year == year]
        engine.set_expiry_floor(date(year - 1, 1, 1))        # a year of earlier months for the IV rank
        for sym in stocks:
            if year in done.get(sym, set()):
                continue
            if hist.error is not None:
                stopped = f"Breeze error: {hist.error}"
                break
            if hist.fetcher is None or not hist.fetcher.budget_left:
                stopped = "request budget used"
                break
            before, used = missing(), hist.fetcher.requests
            for variant in ["strategy"] + baseline_variants(strategy):
                engine.run_stream(strategy, sym, block, variant, Counter(), exit_check="intraday")
            if missing() == before and hist.error is None:
                done.setdefault(sym, set()).add(year)
                newly += 1
            progress(f"  {year} {sym:12s} requests {hist.fetcher.requests - used:4d}  "
                     f"{'complete' if year in done.get(sym, set()) else 'INCOMPLETE'}  "
                     f"(run total {hist.fetcher.requests}, left {hist.fetcher.budget_left})")
        if stopped:
            break
    if stopped is None and hist.error is not None:
        stopped = f"Breeze error: {hist.error}"
    engine.set_expiry_floor(None)
    status["complete"] = {s: sorted(v) for s, v in sorted(done.items())}
    return {"stopped": stopped, "stock_years_completed_this_run": newly,
            "requests_this_run": hist.fetcher.requests if hist.fetcher else 0}


def coverage(status: dict, stocks: list[str], years: list[int]) -> tuple[list[int], list[str]]:
    """(years, stocks) a report can cover without missing contracts: the newest run of consecutive
    years for which at least one stock is complete, and the stocks complete for all of them."""
    done = {s: set(v) for s, v in status.get("complete", {}).items()}
    use: list[int] = []
    for y in sorted(years, reverse=True):
        if any(set(use + [y]) <= done.get(s, set()) for s in stocks):
            use.append(y)
        else:
            break
    return sorted(use), [s for s in stocks if use and set(use) <= done.get(s, set())]


def main(argv: list[str] | None = None) -> int:
    from ..config import ROOT, load_settings
    ap = argparse.ArgumentParser(description="Stock option selling: backtest with a no-filter baseline, and its download")
    ap.add_argument("--download", action="store_true", help="fetch missing option history from Breeze (read-only), "
                                                            "within the daily request budget")
    ap.add_argument("--max-requests", type=int, help="with --download: request limit for this run (default: what is "
                                                     "left of the daily budget)")
    ap.add_argument("--stocks", nargs="*", help="stocks to use instead of the most liquid ones")
    ap.add_argument("--skip-index", action="store_true", help="with --download: do not first fetch the contracts the "
                                                              "index (phase 4) backtests still want")
    ap.add_argument("--status", action="store_true", help="show the liquidity ranking, the request budget and progress")
    ap.add_argument("--all-data", action="store_true", help="report on every priority stock and the whole period, "
                                                            "also where contracts are missing (marked INCOMPLETE)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    store = Store(settings.path(settings.storage.db_path_live), live=True)
    base = Engine(settings, store, "breeze", calendar=load_calendar(settings, store))
    strategy = build_stock_selling(settings)
    bt = settings.stock_option_selling.backtest
    today = utcnow().astimezone(IST).date()
    budget = settings.options.backtest.daily_request_budget
    status = read_status(settings)

    fetcher = None
    if args.download:
        from dotenv import load_dotenv

        from ..feeds.breeze_feed import BreezeFeed
        from .instruments import refresh_from_broker
        load_dotenv(ROOT / ".env")
        try:
            print(f"instrument list refreshed: {refresh_from_broker(store, settings.watchlist)} contract expiries stored")
        except Exception as e:      # noqa: BLE001 - the public file could not be read; the stored list is used
            print(f"instrument list not refreshed ({type(e).__name__}: {e}); using the stored list")
        fetcher = budgeted_fetcher(settings, store, BreezeFeed(settings.watchlist), args.max_requests)
        print(f"request budget: {store.requests_used(today)} of {budget} used today; "
              f"{fetcher.max_requests} may be sent in this run")
    hist = OptionHistory(store, "breeze", fetcher)
    eng = OptionEngine(settings, store, "breeze", hist, base, intraday=OptionIntraday(hist))
    sessions = base.session_dates()
    dev, _ = dev_holdout(settings, sessions, "1day")
    ranked = liquid_stocks(settings, eng, settings.backtest.holdout_start.get("1day"))
    stocks = [s.upper() for s in args.stocks] if args.stocks else [r["stock"] for r in ranked[:bt.priority_stocks]]

    if args.status:
        print(f"request budget: {store.requests_used(today)} of {budget} used today ({today})")
        print(f"most liquid watchlist F&O stocks (median daily traded value, last {bt.liquidity_lookback_days} "
              f"development sessions); the first {bt.priority_stocks} are downloaded and tested:")
        for r in ranked:
            years = status.get("complete", {}).get(r["stock"], [])
            print(f"  {r['rank']:3d} {r['stock']:12s} Rs {r['median_traded_value_inr'] / 1e7:9,.1f} crore  lot {r['lot_size']:5d}"
                  f"  complete years: {years or '-'}")
        for run in status.get("runs", [])[-5:]:
            print(f"  run {run}")
        return 0

    if args.download:
        run = {"at": utcnow().isoformat(timespec="seconds")}
        if not args.skip_index:
            # issue 54: the contracts the index baselines want since the fixed-schedule change
            index_strategies = build_option_strategies(settings)
            for r in prepare_history(settings, store, base, hist, index_strategies, None):
                print(f"expiries {r['underlying']} {r['kind']}: found {r['found']} new, not found {r['not_found']}"
                      + (f", stopped: {r['stopped']}" if r["stopped"] else ""))
            index_eng = OptionEngine(settings, store, "breeze", hist, base)
            before = fetcher.requests
            for st in index_strategies:
                res = run_option_backtest(settings, store, "breeze", st, hist, None, engine=index_eng,
                                          save=hist.error is None)
                print_report(res["report"], "breeze")
            run["index_requests"] = fetcher.requests - before
            run["index_contracts_still_missing"] = len(hist.not_stored)
            hist.not_stored.clear()
        print(f"\nstock option history: {len(stocks)} stocks, newest year first")
        run.update(download_blocks(settings, eng, strategy, stocks, dev, status))
        run["requests_used_today"] = store.requests_used(today)
        status.setdefault("runs", []).append(run)
        status["updated"] = run["at"]
        status["stocks"] = stocks
        path = write_status(settings, status)
        print(f"\ndownload: {run}")
        print(f"progress written to {path}")
        if run["stopped"] and run["stopped"].startswith("Breeze error"):
            print("STOPPED on a Breeze error. Nothing is lost: run the same command again after a fresh login.")
        hist.fetcher = None                                   # the report below reads stored prices only
        hist.not_stored.clear()
        eng.intraday.not_stored.clear()
        eng.set_expiry_floor(None)

    years, covered = coverage(status, stocks, sorted({d.year for d in dev}))
    if args.all_data or not covered:
        use_stocks, first, note = stocks, None, ""
        if not covered:
            print("\nNo (stock, year) is completely downloaded yet: this report runs on whatever is stored and is INCOMPLETE.")
    else:
        use_stocks, first = covered, date(years[0], 1, 1)
        eng.set_expiry_floor(date(years[0] - 1, 1, 1))        # as downloaded: one earlier year for the IV rank
        note = (f"Coverage: {len(covered)} of {len(stocks)} priority stocks, {years[0]}-{years[-1]}: the stocks and years "
                f"whose option history is completely downloaded. The rest is not in this result.")
        print(f"\nreport covers {len(covered)} stocks, {years[0]}..{years[-1]} (completely downloaded); "
              f"use --all-data for everything stored")
    res = run_option_backtest(settings, store, "breeze", strategy, hist, use_stocks, engine=eng,
                              exit_checks=tuple(bt.exit_checks), first_session=first, coverage_note=note)
    print_report(res["report"], "breeze")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
