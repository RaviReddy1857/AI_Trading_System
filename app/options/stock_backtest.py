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

from ..backtest import Engine, dev_holdout, load_calendar, stats
from ..config import Settings
from ..config import StockOptionSelling as StockOptionSellingParams
from ..history import bar_date
from ..models import utcnow
from ..quality import IST
from ..store import Store
from . import build_option_strategies
from .backtest import (OptionEngine, baseline_variants, prepare_history, print_report, run_option_backtest)
from .campaign import campaigns_from_trades
from .option_history import OptionHistory, OptionIntraday, budgeted_fetcher
from .reversal import build_reversal_strategies, reversal_outcomes
from .stock_selling import StockOptionSelling, build_stock_selling

log = logging.getLogger("stock_backtest")
STATUS_FILE = "data/option_download_status.json"
# What a "complete" (stock, year) in the status file covers. Raised when the backtest starts wanting
# contracts it did not want before (2: the roll exit policies); older marks are then checked again.
PLAN_VERSION = 3        # 3: the reversal candidates (bull call spread, put spread below the base low)
POLICY_LABEL = {"stop": "stop at the stop (no roll)", "roll_rules": "roll by the roll rules, else exit",
                "roll_always": "never stop; roll a losing spread every month, without limit"}


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


# ---------------- the effect of each rule and alert, against the baseline ----------------
ALERT_SETS = {
    "close beyond support / resistance": {"BELOW_SUPPORT", "ABOVE_RESISTANCE"},
    "new 52-week low / high": {"NEW_52W_LOW", "NEW_52W_HIGH"},
    "break of the 200-day average": {"BROKE_LONG_AVERAGE"},
    "FII streak begins": {"FII_STREAK"},
    "India VIX spike": {"VIX_SPIKE"},
    "regime turns VOLATILE or EVENT": {"REGIME_TURN"},
}


def variant(settings: Settings, off: tuple[str, ...] = (), baseline_rules: tuple[str, ...] = (),
            alert_exits=()) -> StockOptionSelling:
    """The strategy with some rules switched off, or with rules and alert exits added to its baseline."""
    raw = settings.stock_option_selling.model_dump()
    for rule in off:
        raw[rule]["enabled"] = False
    st = StockOptionSelling(StockOptionSellingParams.model_validate(raw))
    st.baseline_rules, st.alert_exits = frozenset(baseline_rules), frozenset(alert_exits)
    return st


def _row(label: str, trades: list, skipped: Counter, settings: Settings) -> dict:
    s = stats(trades, settings.account.capital_inr, settings.backtest.min_trades)
    blocked = {k.split(":", 1)[1]: v for k, v in skipped.items()
               if k.startswith("NO_SIGNAL:") and k.split(":", 1)[1] in ("DOWNTREND_BLOCK", "UPTREND_BLOCK", "STOCK_LOSS_LIMIT",
                                                                         "INSUFFICIENT_HISTORY", "CREDIT_TOO_SMALL")}
    return {"label": label, "trades": s["trades"], "win_rate_pct": s["win_rate_pct"], "net_pnl_inr": s["net_pnl_inr"],
            "per_trade_inr": round(s["net_pnl_inr"] / s["trades"], 2) if s["trades"] else None,
            "profit_factor": s["profit_factor"], "max_drawdown_inr": s["max_drawdown_inr"], "low_sample": s["low_sample"],
            "blocked": blocked, "alert_exits": sum(1 for t in trades if t.exit_reason.startswith("ALERT_")),
            "stops": sum(1 for t in trades if t.exit_reason == "STOP")}


def _bucket(trades: list, key) -> dict:
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(key(t.entry_conditions or {}), []).append(t)
    return groups


def rule_effects(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date], by_mode: dict) -> dict:
    """What each entry rule and each open-position alert does to the result, on the same stocks,
    sessions, prices and exit check as the main result: the strategy with one rule removed, and the
    no-filter baseline with one rule (or one alert used as an exit at the next open) added. Also the
    baseline's trades split by what was known at entry."""
    mode = next(iter(by_mode))
    trades, base_trades, skipped, base_skipped, _ = by_mode[mode]

    def run(st: StockOptionSelling, baseline: bool) -> tuple[list, Counter]:
        out, sk = [], Counter()
        for sym in syms:
            for v in (baseline_variants(st) if baseline else ["strategy"]):
                out += eng.run_stream(st, sym, dev, v, sk, exit_check=mode)
        return out, sk

    no_min = variant(settings)
    no_min.params = no_min.params.model_copy(update={"min_net_credit_pct_of_max_loss": 0.0})
    rows = [_row("strategy: all rules", trades, skipped, settings),
            _row("strategy without the minimum net credit", *run(no_min, False), settings),
            _row("strategy without the trend block", *run(variant(settings, off=("trend_block",)), False), settings),
            _row("strategy without the campaign limit", *run(variant(settings, off=("campaign_limit",)), False), settings),
            _row("baseline: no rules", base_trades, base_skipped, settings),
            _row("baseline + trend block", *run(variant(settings, baseline_rules=("trend_block",)), True), settings),
            _row("baseline + campaign limit", *run(variant(settings, baseline_rules=("campaign_limit",)), True), settings),
            _row("baseline + minimum net credit", *run(variant(settings, baseline_rules=("min_credit",)), True), settings)]
    for name, kinds in ALERT_SETS.items():
        rows.append(_row(f"baseline + exit on alert: {name}", *run(variant(settings, alert_exits=kinds), True), settings))
    every = set().union(*ALERT_SETS.values())
    rows.append(_row("baseline + exit on any alert", *run(variant(settings, alert_exits=every), True), settings))

    sos = settings.stock_option_selling

    def breach_bucket(c: dict) -> str:
        v = c.get("breach_history_pct")
        return "unknown" if v is None else ("under 10%" if v < 10 else ("10-25%" if v <= 25 else "over 25%"))

    splits = {
        "stock state at entry": lambda c: c.get("state") or "unknown",
        "past breach rate of a strike this far away": breach_bucket,
        "IV rank at entry": lambda c: "unknown" if c.get("iv_rank") is None
        else (f"at or above {sos.min_iv_rank:g}" if c["iv_rank"] >= sos.min_iv_rank else f"below {sos.min_iv_rank:g}"),
        "FII bias at entry": lambda c: c.get("fii_bias") or "no data",
        "India VIX on the entry signal day": lambda c: "unknown" if c.get("vix_change_pct") is None
        else ("spike" if c["vix_change_pct"] >= sos.profile.vix_spike_pct else "no spike"),
    }
    by_cond = {}
    for title, key in splits.items():
        by_cond[title] = []
        for name, ts in sorted(_bucket(base_trades, key).items()):
            r = _row(name, ts, Counter(), settings)
            r["closed_beyond_sold_strike_or_stopped_pct"] = round(
                sum(1 for t in ts if t.exit_reason in ("STOP", "STRIKE_BREACHED")) / len(ts) * 100, 1)
            by_cond[title].append(r)
    return {"rule_effects": {"exit_check": mode, "rows": rows,
                             "note": "Each row uses the same stocks, sessions, prices, costs and exit check. An alert "
                                     "exit closes the spread at the next session's open after the alert."},
            "baseline_by_entry_condition": by_cond}


def policy_runs(settings: Settings, eng: OptionEngine, sym: str, sessions: list[date]) -> dict:
    """{"strategy" | "baseline": {policy: trades}} for one stock. The "stop" policy is run first and its
    entry signals are recorded; every other policy enters exactly those signals (daily exit check)."""
    policies = settings.stock_option_selling.backtest.exit_policies
    ref = variant(settings)
    out = {"strategy": {p: [] for p in policies}, "baseline": {p: [] for p in policies}}
    recorded = {}
    for name in ["strategy"] + baseline_variants(ref):
        rec: list = []
        group = "strategy" if name == "strategy" else "baseline"
        out[group]["stop"] += eng.run_stream(ref, sym, sessions, name, Counter(), exit_check="daily", record=rec)
        recorded[name] = rec
    for policy in policies[1:]:
        st = variant(settings)
        st.exit_policy = policy
        for name, rec in recorded.items():
            replay: dict = {}
            for d, sig in rec:
                replay.setdefault(d, []).append(sig)
            group = "strategy" if name == "strategy" else "baseline"
            out[group][policy] += eng.run_stream(st, sym, sessions, name, Counter(), exit_check="daily", replay=replay)
    return out


def policy_comparison(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date]) -> dict:
    """The exit policies on the same entries, counted per campaign (an original trade and its rolls)."""
    policies = settings.stock_option_selling.backtest.exit_policies
    total = {"strategy": {p: [] for p in policies}, "baseline": {p: [] for p in policies}}
    for sym in syms:
        got = policy_runs(settings, eng, sym, dev)
        for group in total:
            for p in policies:
                total[group][p] += got[group][p]
    min_n = settings.backtest.min_trades

    def row(policy: str, trades: list) -> dict:
        camps = campaigns_from_trades(trades)
        nets = [c["realised_pnl_inr"] for c in camps]
        s = stats(trades, settings.account.capital_inr, min_n)
        n = len(camps)
        return {"policy": policy, "label": POLICY_LABEL[policy], "campaigns": n, "positions": len(trades),
                "rolls": sum(c["rolls"] for c in camps), "campaigns_rolled": sum(1 for c in camps if c["rolls"]),
                "max_rolls_in_a_campaign": max((c["rolls"] for c in camps), default=0),
                "win_rate_pct": round(sum(1 for x in nets if x > 0) / n * 100, 1) if n else None,
                "net_pnl_inr": s["net_pnl_inr"], "per_campaign_inr": round(s["net_pnl_inr"] / n, 2) if n else None,
                "worst_campaign_inr": min(nets) if nets else None, "max_drawdown_inr": s["max_drawdown_inr"],
                "costs_inr": s["costs_inr"], "hedge_cost_inr": round(sum(c["hedge_cost_inr"] for c in camps), 2),
                "rolled_when_recovery_was_not_possible": sum(1 for c in camps if c["rolled_when_recovery_was_not_possible"]),
                "low_sample": n < min_n}

    return {"exit_policy_comparison": {
        "exit_check": "daily",
        "note": "Every policy enters exactly the same signals (recorded from the stop policy). Decisions are taken on "
                "daily closes and filled at the next session's open; a roll closes the old spread and opens the new "
                "one at the same open, one lot each. Results are per campaign: an original trade and all its rolls.",
        "strategy": [row(p, total["strategy"][p]) for p in policies],
        "baseline": [row(p, total["baseline"][p]) for p in policies]}}


def print_effects(rep: dict) -> None:
    pc = rep.get("exit_policy_comparison")
    if pc:
        print("  exit policies on the same entries (daily exit check, per campaign):")
        for group in ("strategy", "baseline"):
            for r in pc[group]:
                print(f"    {group:9s} {r['policy']:12s} campaigns {r['campaigns']:4d}  rolls {r['rolls']:4d} (max "
                      f"{r['max_rolls_in_a_campaign']})  win {r['win_rate_pct']}%  net Rs {r['net_pnl_inr']:>12,.2f}  "
                      f"per campaign Rs {r['per_campaign_inr']}  worst Rs {r['worst_campaign_inr']}"
                      + ("  LOW SAMPLE" if r["low_sample"] else ""))
    fx = rep.get("rule_effects")
    if not fx:
        return
    print(f"  effect of each rule and alert ({fx['exit_check']} exit check):")
    for r in fx["rows"]:
        print(f"    {r['label']:52s} trades {r['trades']:4d}  win {r['win_rate_pct']}%  net Rs {r['net_pnl_inr']:>12,.2f}  "
              f"per trade Rs {r['per_trade_inr']}  PF {r['profit_factor']}"
              + (f"  blocked {r['blocked']}" if r["blocked"] else "") + (f"  alert exits {r['alert_exits']}" if r["alert_exits"] else ""))
    for title, rows in rep.get("baseline_by_entry_condition", {}).items():
        print(f"  baseline by {title}:")
        for r in rows:
            print(f"    {r['label']:28s} trades {r['trades']:4d}  win {r['win_rate_pct']}%  per trade Rs {r['per_trade_inr']}  "
                  f"stopped or strike passed {r['closed_beyond_sold_strike_or_stopped_pct']}%")


def print_reversal(out: dict) -> None:
    print(f"\nreversal detector ({out['label']}): {out['confirmations']} confirmed reversals in {out['stocks']} stocks")
    for r in out["outcomes"]:
        print(f"  {r['sessions']:3d} sessions later: cases {r['cases']:4d}  median {r['median_pct']}%  mean {r['mean_pct']}%  "
              f"up in {r['up_pct_of_cases']}%  worst {r['worst_pct']}%  best {r['best_pct']}%"
              + ("  LOW SAMPLE" if r["low_sample"] else ""))
    nl = out["new_low_afterwards"]
    print(f"  made a new low within {nl['within_sessions']} sessions: {nl['share_pct']}% of {nl['cases']} cases"
          + ("  LOW SAMPLE" if nl["low_sample"] else ""))


def run_reversal_reports(settings: Settings, store: Store, eng: OptionEngine, option_stocks: list[str],
                         study_stocks: list[str], dev: list[date], first: date | None, note: str, save: bool = True) -> dict:
    """The reversal study: the outcome of every confirmed reversal (daily prices of `study_stocks`;
    no option prices needed), and both candidate trades against their no-filter baselines on
    `option_stocks` (the stocks whose option history is stored). Daily exit check."""
    sessions = [d for d in dev if first is None or d >= first]
    outcomes = reversal_outcomes({s: eng.reversal_scanner(s) for s in study_stocks}, dev,
                                 settings.stock_option_selling.reversal.outcome_sessions, settings.backtest.min_trades)
    reports = {}
    for st in build_reversal_strategies(settings):
        res = run_option_backtest(settings, store, eng.source, st, eng.prices, option_stocks, engine=eng, save=save,
                                  exit_checks=("daily",), first_session=first, coverage_note=note,
                                  extra=lambda e, syms, d, by_mode: {"reversal_outcomes": outcomes,
                                                                     "tested_sessions": len(sessions)})
        reports[st.id] = res["report"]
    return {"outcomes": outcomes, "reports": reports}


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
    if status.get("plan") != PLAN_VERSION:                   # the backtest wants more than the old marks covered
        status["complete"], status["plan"] = {}, PLAN_VERSION
    done = {s: set(v) for s, v in status.get("complete", {}).items()}
    engine.last_session = dev[-1] if dev else None           # nothing of the holdout period is asked for
    stopped = None
    newly = 0

    def missing() -> int:
        return len(hist.not_stored) + (len(intr.not_stored) if intr is not None else 0)

    for year in years:
        block = [d for d in dev if d.year >= year]           # to the end: a rolled campaign can run past the year
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
            loose = variant(settings, off=("trend_block", "campaign_limit"))
            loose.params = loose.params.model_copy(update={"min_net_credit_pct_of_max_loss": 0.0})
            for st, names in ((strategy, ["strategy"] + baseline_variants(strategy)), (loose, ["strategy"])):
                for name in names:                           # the rule-effect rows need no other contracts than these
                    engine.run_stream(st, sym, block, name, Counter(), exit_check="intraday")
            policy_runs(settings, engine, sym, block)        # the spreads the roll policies open
            for st in build_reversal_strategies(settings):   # the two reversal candidates and their baselines
                for name in ["strategy"] + baseline_variants(st):
                    engine.run_stream(st, sym, block, name, Counter(), exit_check="daily")
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
        if not covered and not args.all_data:
            print("\nNo (stock, year) is completely downloaded yet, so there is nothing to report. "
                  "Run with --download (or --all-data for an INCOMPLETE report on whatever is stored).")
            return 0
    else:
        use_stocks, first = covered, date(years[0], 1, 1)
        eng.set_expiry_floor(date(years[0] - 1, 1, 1))        # as downloaded: one earlier year for the IV rank
        note = (f"Coverage: {len(covered)} of {len(stocks)} priority stocks, {years[0]}-{years[-1]}: the stocks and years "
                f"whose option history is completely downloaded. The rest is not in this result.")
        print(f"\nreport covers {len(covered)} stocks, {years[0]}..{years[-1]} (completely downloaded); "
              f"use --all-data for everything stored")
    res = run_option_backtest(settings, store, "breeze", strategy, hist, use_stocks, engine=eng,
                              exit_checks=tuple(bt.exit_checks), first_session=first, coverage_note=note,
                              extra=lambda e, syms, d, by_mode: {**rule_effects(settings, e, syms, d, by_mode),
                                                                 **policy_comparison(settings, e, syms, d)})
    print_report(res["report"], "breeze")
    print_effects(res["report"])
    rev = run_reversal_reports(settings, store, eng, use_stocks, [r["stock"] for r in ranked], dev, first, note)
    print_reversal(rev["outcomes"])
    for rep in rev["reports"].values():
        print_report(rep, "breeze")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
