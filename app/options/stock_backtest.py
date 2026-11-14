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
from .backtest import (OPEN_AT_CUTOFF, OptionEngine, baseline_variants, option_stats, prepare_history, print_report, run_option_backtest)
from .campaign import campaigns_from_trades
from .portfolio import PortfolioGate, universe_groups
from .option_history import OptionHistory, OptionIntraday, budgeted_fetcher
from .reversal import build_reversal_strategies, reversal_outcomes
from .stock_selling import StockOptionSelling, build_stock_selling, phase9_settings

log = logging.getLogger("stock_backtest")
STATUS_FILE = "data/option_download_status.json"
# What a "complete" (stock, year) in the status file covers. Raised when the backtest starts wanting
# contracts it did not want before (2: the roll exit policies); older marks are then checked again.
PLAN_VERSION = 6        # 6: M1 rules (pivots, sizing, portfolio limits, open at cutoff): other strikes are wanted
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
    "market turns weak": {"MARKET_WEAK"},
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
    s = option_stats(trades, settings.account.capital_inr, settings.backtest.min_trades)
    blocked = {k.split(":", 1)[1]: v for k, v in skipped.items()
               if k.startswith("NO_SIGNAL:") and k.split(":", 1)[1] in ("DOWNTREND_BLOCK", "UPTREND_BLOCK", "STOCK_LOSS_LIMIT",
                                                                         "INSUFFICIENT_HISTORY", "CREDIT_TOO_SMALL", "MARKET_WEAK",
                                                                         "MARKET_UNKNOWN", "CORRELATED_LIMIT",
                                                                         "CORRELATION_UNKNOWN", "NO_CONFIRMED_LEVEL",
                                                                         "ZERO_SIZE")}
    return {"label": label, "trades": s["trades"], "win_rate_pct": s["win_rate_pct"], "net_pnl_inr": s["net_pnl_inr"],
            "per_trade_inr": round(s["net_pnl_inr"] / s["trades"], 2) if s["trades"] else None,
            "profit_factor": s["profit_factor"], "max_drawdown_inr": s["max_drawdown_inr"], "low_sample": s["low_sample"],
            "blocked": blocked, "alert_exits": sum(1 for t in trades if t.exit_reason.startswith("ALERT_")),
            "stops": sum(1 for t in trades if t.exit_reason == "STOP"),
            "open_at_cutoff": s["open_at_cutoff"]["positions"], "net_incl_open_inr": s["net_incl_open_inr"]}


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

    gated = settings.stock_option_selling.portfolio.enabled

    def run(st: StockOptionSelling, baseline: bool, portfolio: bool | None = None) -> tuple[list, Counter]:
        """The strategy rows run under the portfolio limits (as the main result); baseline rows do not,
        unless `portfolio` asks for it."""
        use = (not baseline) if portfolio is None else portfolio
        out, sk = [], Counter()
        streams = [(sym, v) for sym in syms for v in (baseline_variants(st) if baseline else ["strategy"])]
        if use and gated:
            return eng.run_portfolio(st, streams, dev, sk, PortfolioGate(settings, eng), mode), sk
        for sym, v in streams:
            out += eng.run_stream(st, sym, dev, v, sk, exit_check=mode)
        return out, sk

    no_min = variant(settings)
    no_min.params = no_min.params.model_copy(update={"min_credit_to_max_loss": 0.0})
    rows = [_row("strategy: all rules", trades, skipped, settings),
            _row("strategy without the minimum net credit", *run(no_min, False), settings),
            _row("strategy without the trend block", *run(variant(settings, off=("trend_block",)), False), settings),
            _row("strategy without the campaign limit", *run(variant(settings, off=("campaign_limit",)), False), settings),
            _row("strategy without the market block", *run(variant(settings, off=("market_block",)), False), settings),
            _row("strategy without the portfolio limits", *run(variant(settings), False, portfolio=False), settings),
            _row("baseline: no rules", base_trades, base_skipped, settings),
            _row("baseline + trend block", *run(variant(settings, baseline_rules=("trend_block",)), True), settings),
            _row("baseline + campaign limit", *run(variant(settings, baseline_rules=("campaign_limit",)), True), settings),
            _row("baseline + minimum net credit", *run(variant(settings, baseline_rules=("min_credit",)), True), settings),
            _row("baseline + market block", *run(variant(settings, baseline_rules=("market_block",)), True), settings),
            _row("baseline + portfolio limits", *run(variant(settings), True, portfolio=True), settings)]
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
        "India VIX on the entry signal day": lambda c: {True: "spike", False: "no spike"}.get(c.get("vix_spike"), "unknown"),
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


def policy_runs(settings: Settings, eng: OptionEngine, syms, sessions: list[date], exit_check: str = "daily",
                skipped: dict | None = None) -> dict:
    """{"strategy" | "baseline": {policy: trades}} for one stock or a list of stocks under one execution
    mode. The "stop" policy is run first and its entry signals are recorded; every other policy enters
    exactly those signals. The strategy's stocks run side by side under the portfolio limits (rolls are
    checked against them too); the baseline runs without them. `skipped`: {group: {policy: Counter}}."""
    syms = [syms] if isinstance(syms, str) else list(syms)
    policies = settings.stock_option_selling.backtest.exit_policies
    gated = settings.stock_option_selling.portfolio.enabled
    ref = variant(settings)
    out = {"strategy": {p: [] for p in policies}, "baseline": {p: [] for p in policies}}

    def counter(group: str, policy: str) -> Counter:
        return Counter() if skipped is None else skipped.setdefault(group, {}).setdefault(policy, Counter())

    def strategy_run(st, policy, record=None, replay=None):
        streams = [(s, "strategy") for s in syms]
        if gated:
            return eng.run_portfolio(st, streams, sessions, counter("strategy", policy), PortfolioGate(settings, eng),
                                     exit_check, record=record, replay=replay)
        trades = []
        for key in streams:
            rec = None if record is None else record.setdefault(key, [])
            trades += eng.run_stream(st, key[0], sessions, key[1], counter("strategy", policy), exit_check=exit_check,
                                     record=rec, replay=None if replay is None else replay.get(key, {}))
        return trades

    recorded: dict = {}
    out["strategy"]["stop"] = strategy_run(ref, "stop", record=recorded)
    for sym in syms:
        for name in baseline_variants(ref):
            rec: list = []
            out["baseline"]["stop"] += eng.run_stream(ref, sym, sessions, name, counter("baseline", "stop"),
                                                      exit_check=exit_check, record=rec)
            recorded[(sym, name)] = rec

    def replay_of(key) -> dict:
        r: dict = {}
        for d, sig in recorded.get(key, []):
            r.setdefault(d, []).append(sig)
        return r

    for policy in policies[1:]:
        st = variant(settings)
        st.exit_policy = policy
        out["strategy"][policy] = strategy_run(st, policy, replay={k: replay_of(k) for k in recorded if k[1] == "strategy"})
        for sym in syms:
            for name in baseline_variants(st):
                out["baseline"][policy] += eng.run_stream(st, sym, sessions, name, counter("baseline", policy),
                                                          exit_check=exit_check, replay=replay_of((sym, name)))
    return out


POLICY_NOTE = {
    "daily": "Decisions are taken on daily closes and filled at the next session's open; a roll closes the old spread "
             "and opens the new one at the same open.",
    "intraday": "Synchronized 5-minute bars of every leg: entry at the first synchronized bar of the session after the "
                "signal, stop and target on synchronized bar closes, every fill at the next synchronized bar's open; a "
                "roll fills at the first bar in which all four legs traded. A campaign with a session lacking a "
                "synchronized bar is data-incomplete and left out of every figure; the counts are shown.",
}


def policy_comparison(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date],
                      exit_checks: tuple[str, ...] | None = None) -> dict:
    """The exit policies on the same entries, counted per campaign (an original trade and its rolls),
    under each execution mode."""
    policies = settings.stock_option_selling.backtest.exit_policies
    modes = tuple(exit_checks or settings.stock_option_selling.backtest.exit_checks)
    min_n = settings.backtest.min_trades

    def row(policy: str, trades: list, sk: Counter) -> dict:
        all_camps = campaigns_from_trades(trades)
        camps = [c for c in all_camps if c["last_exit"] != OPEN_AT_CUTOFF]         # closed campaigns
        still = [c for c in all_camps if c["last_exit"] == OPEN_AT_CUTOFF]
        nets = [c["realised_pnl_inr"] for c in camps]
        wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
        s = option_stats(trades, settings.account.capital_inr, min_n)
        n = len(camps)
        return {"policy": policy, "label": POLICY_LABEL[policy], "campaigns": n, "positions": len(trades),
                "rolls": sum(c["rolls"] for c in camps), "campaigns_rolled": sum(1 for c in camps if c["rolls"]),
                "max_rolls_in_a_campaign": max((c["rolls"] for c in camps), default=0),
                "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
                "avg_win_inr": round(sum(wins) / len(wins), 2) if wins else None,
                "avg_loss_inr": round(sum(losses) / len(losses), 2) if losses else None,
                "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
                "net_pnl_inr": round(sum(nets), 2), "per_campaign_inr": round(sum(nets) / n, 2) if n else None,
                "open_campaigns": len(still),
                "open_realised_inr": round(sum(t.net_pnl for t in trades if t.exit_reason != OPEN_AT_CUTOFF
                                               and (t.campaign_id or t.signal_id) in {c["campaign_id"] for c in still}), 2),
                "open_unrealised_inr": round(sum(t.net_pnl for t in trades if t.exit_reason == OPEN_AT_CUTOFF), 2),
                "net_incl_open_inr": s["net_incl_open_inr"], "max_drawdown_incl_open_inr": s["max_drawdown_incl_open_inr"],
                "worst_campaign_inr": min(nets) if nets else None, "max_drawdown_inr": s["max_drawdown_inr"],
                "costs_inr": s["costs_inr"], "hedge_cost_inr": round(sum(c["hedge_cost_inr"] for c in camps), 2),
                "rolled_when_recovery_was_not_possible": sum(1 for c in camps if c["rolled_when_recovery_was_not_possible"]),
                # which rule said "exit" each time a roll was considered and not made
                "roll_refusals": dict(sorted(Counter(t.roll_refused for t in trades if t.roll_refused).items())),
                "data_incomplete_campaigns": sk.get("DATA_INCOMPLETE_CAMPAIGNS", 0),
                "sessions_without_synchronized_bar": sk.get("DATA_INCOMPLETE:NO_SYNCHRONIZED_BAR_IN_SESSION", 0),
                "sessions_with_5_minute_bars_not_stored": sk.get("DATA_INCOMPLETE:5_MINUTE_BARS_NOT_STORED", 0),
                "entries_without_synchronized_bar": sk.get("NO_SYNCHRONIZED_BAR_AT_ENTRY", 0)
                + sk.get("5_MINUTE_BARS_NOT_STORED_AT_ENTRY", 0),
                "low_sample": n < min_n}

    out = {}
    for mode in modes:
        total = {"strategy": {p: [] for p in policies}, "baseline": {p: [] for p in policies}}
        sk: dict = {}
        got = policy_runs(settings, eng, syms, dev, mode, sk)
        for group in total:
            for p in policies:
                total[group][p] += got[group][p]
        out[mode] = {"exit_check": mode,
                     "note": "Every policy enters exactly the same signals (recorded from the stop policy). "
                             + POLICY_NOTE[mode] + " One lot each. Results are per campaign: an original trade and all "
                             "its rolls. Roll every month is research only and never reaches a live signal.",
                     "strategy": [row(p, total["strategy"][p], sk.get("strategy", {}).get(p, Counter())) for p in policies],
                     "baseline": [row(p, total["baseline"][p], sk.get("baseline", {}).get(p, Counter())) for p in policies]}
    return {"exit_policy_comparison": out}


SENSITIVITY_SHARE_PCT = 30.0     # above this share of data-incomplete campaigns a sensitivity run is added (user, 2026-11-14)
SENSITIVITY_LABEL = ("SENSITIVITY, not the primary result: a session of an open spread without a synchronized 5-minute bar "
                     "is judged on the legs' daily closes instead of excluding the campaign")


def incomplete_sensitivity(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date], by_mode: dict) -> dict:
    """Number and share of campaigns the synchronized run left out as data-incomplete. When the share is
    above 30% for the strategy or the baseline, the same streams are run once more with the missing
    sessions judged at the daily close, as a separately labelled sensitivity result."""
    if "intraday" not in by_mode:
        return {}
    trades, base_trades, sk, bsk, _ = by_mode["intraday"]
    out = {"strategy": {"kept": len(trades), "excluded": sk.get("DATA_INCOMPLETE_CAMPAIGNS", 0)},
           "baseline": {"kept": len(base_trades), "excluded": bsk.get("DATA_INCOMPLETE_CAMPAIGNS", 0)}}
    for g in out.values():
        n = g["kept"] + g["excluded"]
        g["excluded_share_pct"] = round(g["excluded"] / n * 100, 1) if n else None
    out["threshold_pct"] = SENSITIVITY_SHARE_PCT
    out["sensitivity"] = None
    if not any((g["excluded_share_pct"] or 0) > SENSITIVITY_SHARE_PCT for g in (out["strategy"], out["baseline"])):
        return {"data_incomplete_summary": out}
    st = variant(settings)
    eng.sync_missing_at_daily_close = True
    try:
        s_tr, b_tr, s_sk, b_sk = [], [], Counter(), Counter()
        for sym in syms:
            s_tr += eng.run_stream(st, sym, dev, "strategy", s_sk, exit_check="intraday")
            for v in baseline_variants(st):
                b_tr += eng.run_stream(st, sym, dev, v, b_sk, exit_check="intraday")
    finally:
        eng.sync_missing_at_daily_close = False
    cap, n = settings.account.capital_inr, settings.backtest.min_trades
    out["sensitivity"] = {
        "label": SENSITIVITY_LABEL, "strategy": stats(s_tr, cap, n), "baseline": stats(b_tr, cap, n),
        "still_excluded": {"strategy": s_sk.get("DATA_INCOMPLETE_CAMPAIGNS", 0), "baseline": b_sk.get("DATA_INCOMPLETE_CAMPAIGNS", 0)},
        "exits_decided_on_a_daily_close": sum(1 for t in s_tr + b_tr if "sensitivity" in t.exit_basis)}
    return {"data_incomplete_summary": out}


EXPECTED_PER_MONTH = (3, 8)        # a reporting expectation (section 8.8), never a tuning target


def scan_candidates(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date]) -> list[dict]:
    """Every setup that passes every entry block, on every day, for every stock, whether or not a
    position is open: {stock, date, expiry, side, score, factors}. The per-stock campaign limit and the
    portfolio limits depend on positions and results, so they are left out. No outcome is read."""
    scan = variant(settings, off=("campaign_limit",))
    allowed = set(dev)
    out = []
    for sym in syms:
        bars = eng.base.daily(sym)
        brk = set(eng.base.breaks(sym))
        start = 0
        for i, bar in enumerate(bars):
            if i in brk:
                start = i
            d = bar_date(bar)
            if d not in allowed:
                continue
            ctx = eng.context(scan, sym, d, bar.close, "strategy", None, tuple(bars[max(start, i - 1299):i + 1]), ())
            for sig in scan.evaluate(ctx):
                sc = sig.details.get("score") or {}
                out.append({"stock": sym, "date": d, "expiry": sig.details["expiry"], "side": sig.details["side"],
                            "score": sc.get("total"), "factors": sc.get("factors", {})})
    return out


def monthly_candidates(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date], trades: list,
                       scanned: list[dict] | None = None) -> dict:
    """Per month: the candidates that passed every block and the trades taken (section 8.8). A
    candidate is a setup (stock, expiry, side) that passed every entry block on some day of the month,
    counted once, in the month it first passed (scan_candidates). The trades taken are the main result's
    (all blocks, the portfolio limits included). Months with fewer than 3 candidates or trades are
    reported as a shortage; no rule is loosened for it."""
    rows = scanned if scanned is not None else scan_candidates(settings, eng, syms, dev)
    first_seen: dict[tuple, str] = {}
    for r in rows:
        first_seen.setdefault((r["stock"], r["expiry"], r["side"]), r["date"].strftime("%Y-%m"))
    months = sorted({d.strftime("%Y-%m") for d in dev})
    taken = Counter(datetime.fromisoformat(t.entry_time).astimezone(IST).strftime("%Y-%m") for t in trades
                    if t.variant == "strategy" and t.roll_no == 0)
    cands = Counter(first_seen.values())
    stocks = {m: sorted({k[0] for k, v in first_seen.items() if v == m}) for m in months}
    lo, hi = EXPECTED_PER_MONTH
    out_rows = [{"month": m, "candidates": cands.get(m, 0), "stocks": stocks[m], "trades_taken": taken.get(m, 0),
                 "shortage": cands.get(m, 0) < lo or taken.get(m, 0) < lo} for m in months]
    return {"monthly_candidates": {
        "expectation": f"{lo}-{hi} a month is a reporting expectation, never a tuning target",
        "not_in_candidate_count": ["per-stock campaign limit (depends on results)", "portfolio limits (depend on open positions)"],
        "months": out_rows, "months_short": sum(1 for r in out_rows if r["shortage"]),
        "total_candidates": sum(cands.values()), "total_trades_taken": sum(taken.values())}}


SMALL_CROSS_SECTION = 3


def band_of(position: int, n: int) -> str:
    """Thirds of the day's ranked candidates (0 = the best): positions with 3 x position < n are the
    top third, 3 x position >= 2 x n the bottom third, the rest the middle. Fewer than 3 candidates
    that day: "small cross-section"."""
    if n < SMALL_CROSS_SECTION:
        return "small cross-section"
    if 3 * position < n:
        return "top"
    if 3 * position >= 2 * n:
        return "bottom"
    return "middle"


def daily_bands(scanned: list[dict]) -> dict[tuple, str]:
    """(stock, date, expiry, side) -> score band among that day's candidates. Ranked by score, highest
    first; ties by stock name, then expiry and side. A candidate whose score is incomplete is not ranked:
    "score incomplete"."""
    by_day: dict[date, list] = {}
    for r in scanned:
        by_day.setdefault(r["date"], []).append(r)
    out = {}
    for d, rows in by_day.items():
        scored = sorted((r for r in rows if r["score"] is not None),
                        key=lambda r: (-r["score"], r["stock"], r["expiry"], r["side"]))
        for k, r in enumerate(scored):
            out[(r["stock"], d, r["expiry"], r["side"])] = band_of(k, len(scored))
        for r in rows:
            if r["score"] is None:
                out[(r["stock"], d, r["expiry"], r["side"])] = "score incomplete"
    return out


def factor_bin(points: float | None) -> str:
    if points is None:
        return "unknown"
    return "low (under 33.3)" if points < 100 / 3 else ("high (66.7 or more)" if points >= 200 / 3 else "middle")


def score_reports(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date], by_mode: dict) -> dict:
    """Score bands (section 8.5) and each score factor against the baseline (section 8.3), on the main
    execution. Bands: the strategy's trades WITHOUT the portfolio limits (each stock trades whenever it is
    flat), each placed in the top, middle or bottom third of the candidates available on its decision
    day. Factors: the no-filter baseline's trades split by each factor's points."""
    mode = next(iter(by_mode))
    base_trades = by_mode[mode][1]
    scanned = scan_candidates(settings, eng, syms, dev)
    bands = daily_bands(scanned)
    st = variant(settings)
    free: list = []
    for sym in syms:
        free += eng.run_stream(st, sym, dev, "strategy", Counter(), exit_check=mode)
    groups: dict[str, list] = {}
    unmatched = 0
    for tr in free:
        if tr.roll_no:
            continue
        d = datetime.fromisoformat(tr.signal_time).astimezone(IST).date()
        side = next(x["right"] for x in tr.legs if x["action"] == "SELL")
        band = bands.get((tr.symbol, d, tr.expiry, side))
        if band is None:
            unmatched += 1
            band = "not in the day's candidates"
        groups.setdefault(band, []).append(tr)
    cap, n = settings.account.capital_inr, settings.backtest.min_trades
    order = ["top", "middle", "bottom", "small cross-section", "score incomplete", "not in the day's candidates"]
    day_sizes = Counter(len([r for r in scanned if r["date"] == d]) for d in {r["date"] for r in scanned})
    out = {"score_bands": {
        "exit_check": mode,
        "rule": "each day's candidates ranked by score (ties: stock, expiry, side); top third = 3 x position < n, bottom "
                "third = 3 x position >= 2n, else middle; fewer than 3 candidates that day: small cross-section",
        "trades": "the strategy's trades without the portfolio limits (each stock whenever it is flat)",
        "candidate_days": len({r["date"] for r in scanned}),
        "candidates_per_day": {str(k): v for k, v in sorted(day_sizes.items())},
        "bands": [{"band": b, **_row(b, groups.get(b, []), Counter(), settings)} for b in order if b in groups],
        "unmatched": unmatched}}
    from .score import FACTORS
    fac = {}
    for f in ("total",) + FACTORS:
        split: dict[str, list] = {}
        for tr in base_trades:
            ec = tr.entry_conditions or {}
            pts = ec.get("score") if f == "total" else (ec.get("score_factors") or {}).get(f)
            split.setdefault(factor_bin(pts), []).append(tr)
        fac[f] = [{"bin": k, **_row(k, v, Counter(), settings)} for k, v in sorted(split.items())]
    out["baseline_by_score_factor"] = {"exit_check": mode, "note": "the no-filter baseline's trades by each factor's points "
                                       "at entry (fixed bins: under 33.3, 33.3-66.7, 66.7 and more)", "factors": fac}
    return out


def credit_ratio_distribution(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date]) -> dict:
    """Net credit after hedge and charges divided by cost-inclusive maximum loss, for the spreads that
    pass every other rule, at their decision time. NO OUTCOME IS LOOKED AT: each stock is scanned day
    by day; the first day a spread on an expiry passes the rules gives one observation (per stock,
    expiry and side). The minimum-credit rule itself is off, and so is the campaign limit, which
    depends on earlier results. Also given for the no-filter structure (baseline attempts), for reference."""
    scan = variant(settings, off=("campaign_limit",))
    scan.params = scan.params.model_copy(update={"min_credit_to_max_loss": 0.0})
    allowed = set(dev)

    def collect(st, variants) -> list[dict]:
        rows, seen = [], set()
        for sym in syms:
            bars = eng.base.daily(sym)
            brk = set(eng.base.breaks(sym))
            start = 0
            for name in variants:
                for i, bar in enumerate(bars):
                    if i in brk:
                        start = i
                    d = bar_date(bar)
                    if d not in allowed:
                        continue
                    if name != "strategy":
                        want = eng.baseline_schedule(st, sym).get(d)
                        if not want:
                            continue
                    ctx = eng.context(st, sym, d, bar.close, name, None, tuple(bars[max(start, i - 1299):i + 1]), ())
                    for sig in st.evaluate(ctx):
                        key = (sym, sig.details["expiry"], sig.details["side"], name)
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append({"stock": sym, "date": d.isoformat(), "expiry": sig.details["expiry"],
                                     "side": sig.details["side"], "ratio": sig.details["net_credit_pct_of_max_loss"] / 100.0})
        return rows

    def summary(rows: list[dict]) -> dict:
        xs = sorted(r["ratio"] for r in rows if r["ratio"] is not None)
        if not xs:
            return {"observations": 0, "median": None, "p25": None, "p75": None, "min": None, "max": None}
        from ..indicators import percentile
        out = {"observations": len(xs), "median": round(statistics.median(xs), 4), "p25": round(percentile(xs, 25), 4),
               "p75": round(percentile(xs, 75), 4), "min": round(xs[0], 4), "max": round(xs[-1], 4),
               "low_sample": len(xs) < settings.backtest.min_trades}
        for level in (0.10, 0.15, 0.20, 0.25):
            out[f"share_at_or_above_{level:.2f}_pct"] = round(sum(1 for x in xs if x >= level) / len(xs) * 100, 1)
        return out

    rules = collect(scan, ["strategy"])
    structure = collect(scan, baseline_variants(scan))
    return {"what": "net credit after hedge and charges / cost-inclusive maximum loss, one lot, at the decision time; "
                    "no outcome was looked at",
            "passing_every_other_rule": {**summary(rules),
                                         "puts": summary([r for r in rules if r["side"] == "PE"]),
                                         "calls": summary([r for r in rules if r["side"] == "CE"])},
            "no_filter_structure": summary(structure), "stocks": len(syms),
            "period": {"from": dev[0].isoformat() if dev else None, "to": dev[-1].isoformat() if dev else None},
            "rules_not_applied": ["minimum net credit (the rule being measured)", "per-stock campaign limit (depends on results)"],
            "observations": rules}


def groups_at_end(settings: Settings, eng: OptionEngine, syms: list[str], dev: list[date]) -> dict:
    """The correlated groups of the tested stocks at the last tested session (same sector, or daily-return
    correlation >= the threshold over the window before it)."""
    if not dev:
        return {}
    gate = PortfolioGate(settings, eng)
    d = dev[-1]
    return {"correlation_groups_at_end": {"date": d.isoformat(),
                                          **universe_groups(syms, gate.sectors, lambda a, b: gate.corr.at(d, a, b),
                                                            gate.cfg.correlation_threshold)}}


def print_effects(rep: dict) -> None:
    for mode, x in (rep.get("portfolio_limits") or {}).items():
        print(f"  portfolio limits ({mode}): {x['checks']} entries checked, refused {x['refused']}, "
              f"group size of the proposed positions {x['group_size_of_proposed_positions']}")
    sb = rep.get("score_bands")
    if sb:
        print(f"  score bands ({sb['exit_check']} execution; {sb['trades']}): candidates per day {sb['candidates_per_day']}")
        for r in sb["bands"]:
            print(f"    {r['band']:28s} trades {r['trades']:4d}  win {r['win_rate_pct']}%  net Rs {r['net_pnl_inr']:>12,.2f}  "
                  f"per trade Rs {r['per_trade_inr']}  PF {r['profit_factor']}" + ("  LOW SAMPLE" if r["low_sample"] else ""))
    bf = rep.get("baseline_by_score_factor")
    if bf:
        print("  baseline by score factor at entry:")
        for f, rows in bf["factors"].items():
            print(f"    {f}: " + "; ".join(f"{r['bin']} {r['trades']} trades, per trade Rs {r['per_trade_inr']}" for r in rows))
    mc = rep.get("monthly_candidates")
    if mc:
        print(f"  monthly candidates and trades ({mc['expectation']}): {mc['total_candidates']} candidates, "
              f"{mc['total_trades_taken']} trades, {mc['months_short']} month(s) short")
        for r in mc["months"]:
            print(f"    {r['month']}  candidates {r['candidates']:3d}  trades {r['trades_taken']:3d}"
                  + ("  SHORT" if r["shortage"] else ""))
    cg = rep.get("correlation_groups_at_end")
    if cg:
        print(f"  correlated groups on {cg['date']}: sizes {cg['sizes']}; problems {cg['problem_count']}")
    di = rep.get("data_incomplete_summary")
    if di:
        for g in ("strategy", "baseline"):
            print(f"  data-incomplete ({g}): {di[g]['excluded']} of {di[g]['kept'] + di[g]['excluded']} campaigns left out "
                  f"({di[g]['excluded_share_pct']}%)")
        if di["sensitivity"]:
            sv = di["sensitivity"]
            print(f"  {sv['label']}:")
            for g in ("strategy", "baseline"):
                x = sv[g]
                print(f"    {g:9s} trades {x['trades']:4d}  win {x['win_rate_pct']}%  net Rs {x['net_pnl_inr']:>12,.2f}  PF {x['profit_factor']}")
    for mode, pc in (rep.get("exit_policy_comparison") or {}).items():
        print(f"  exit policies on the same entries ({mode} execution, per campaign):")
        for group in ("strategy", "baseline"):
            for r in pc[group]:
                print(f"    {group:9s} {r['policy']:12s} campaigns {r['campaigns']:4d}  rolls {r['rolls']:4d} (max "
                      f"{r['max_rolls_in_a_campaign']})  win {r['win_rate_pct']}%  net Rs {r['net_pnl_inr']:>12,.2f}  "
                      f"per campaign Rs {r['per_campaign_inr']}  worst Rs {r['worst_campaign_inr']}"
                      + (f"  roll refused by {r['roll_refusals']}" if r["roll_refusals"] else "")
                      + (f"  data-incomplete campaigns left out {r['data_incomplete_campaigns']}"
                         if r["data_incomplete_campaigns"] else "")
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


def _no_portfolio(settings: Settings) -> Settings:
    """For the download only: one stock at a time, without the portfolio limits (a superset of the
    contracts the gated runs can want)."""
    sos = settings.stock_option_selling
    return settings.model_copy(update={"stock_option_selling": sos.model_copy(
        update={"portfolio": sos.portfolio.model_copy(update={"enabled": False})})})


def waiting_for_priority_years(priority_years: list[int], stocks: list[str], done: dict) -> list[str]:
    """Newest first: the stocks (with price history) not yet complete for every priority year. An
    earlier year is downloaded only when this is empty."""
    return [s for s in stocks if not set(priority_years) <= set(done.get(s, ()))]


def download_blocks(settings: Settings, engine: OptionEngine, strategy: StockOptionSelling, stocks: list[str],
                    dev: list[date], status: dict, progress=print, offline: bool = False, checkpoint=None) -> dict:
    """Fetch what the backtest needs, newest year first, most liquid stock first, until the request
    budget is used or Breeze returns an error. Marks a (stock, year) complete when its streams ran
    without wanting anything that is not stored. Returns a summary of this run.
    `offline`: ask Breeze for nothing; only mark what is already completely stored (after a run that
    was interrupted before it could record its progress). `checkpoint(status)` is called after every
    (stock, year), so an interrupted run keeps its marks."""
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

    first_from = settings.stock_option_selling.backtest.download_first_years_from
    priority_years = [y for y in years if y >= first_from]
    for year in years:
        if year < first_from and not offline:
            waiting = waiting_for_priority_years(priority_years, [s for s in stocks if engine.base.daily(s)], done)
            if waiting:
                stopped = (f"waiting: {len(waiting)} stock(s) not yet complete for {min(priority_years)}-"
                           f"{max(priority_years)} ({', '.join(waiting[:5])}{' ...' if len(waiting) > 5 else ''})")
                break
        block = [d for d in dev if d.year >= year]           # to the end: a rolled campaign can run past the year
        engine.set_expiry_floor(date(year - 1, 1, 1))        # a year of earlier months for the IV rank
        for sym in stocks:
            if year in done.get(sym, set()):
                continue
            if not engine.base.daily(sym):
                progress(f"  {year} {sym:12s} no daily bars stored: skipped (run python -m app.history)")
                continue
            if hist.error is not None:
                stopped = f"Breeze error: {hist.error}"
                break
            if not offline and (hist.fetcher is None or not hist.fetcher.budget_left):
                stopped = "request budget used"
                break
            before, used = missing(), (hist.fetcher.requests if hist.fetcher else 0)
            no_min = variant(settings)
            no_min.params = no_min.params.model_copy(update={"min_credit_to_max_loss": 0.0})
            runs = [(strategy, ["strategy"] + baseline_variants(strategy)), (no_min, ["strategy"]),
                    (variant(settings, off=("trend_block",)), ["strategy"]),      # each rule-effect row enters its own trades
                    (variant(settings, off=("campaign_limit",)), ["strategy"]),
                    (variant(settings, off=("market_block",)), ["strategy"])]
            for st, names in runs:
                for name in names:
                    engine.run_stream(st, sym, block, name, Counter(), exit_check="intraday")
            for mode in settings.stock_option_selling.backtest.exit_checks:
                policy_runs(_no_portfolio(settings), engine, sym, block, mode)   # the spreads the roll policies open
            for st in build_reversal_strategies(settings):   # the two reversal candidates and their baselines
                for name in ["strategy"] + baseline_variants(st):
                    engine.run_stream(st, sym, block, name, Counter(), exit_check="daily")
            if missing() == before and hist.error is None:
                done.setdefault(sym, set()).add(year)
                newly += 1
            sent = hist.fetcher.requests if hist.fetcher else 0
            progress(f"  {year} {sym:12s} requests {sent - used:4d}  "
                     f"{'complete' if year in done.get(sym, set()) else 'INCOMPLETE'}  "
                     f"(run total {sent}, left {hist.fetcher.budget_left if hist.fetcher else 0})")
            status["complete"] = {s: sorted(v) for s, v in sorted(done.items())}
            if checkpoint is not None:
                checkpoint(status)
        if stopped:
            break
    if stopped is None and hist.error is not None:
        stopped = f"Breeze error: {hist.error}"
    if stopped is None and not offline and hist.fetcher is not None and not hist.fetcher.budget_left:
        stopped = "request budget used"
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
    ap.add_argument("--recheck", action="store_true", help="ask Breeze for nothing: mark the stocks and years whose option "
                                                           "history is already completely stored (after an interrupted run)")
    ap.add_argument("--years", nargs="*", type=int, help="with --recheck: only these years")
    ap.add_argument("--no-report", action="store_true", help="download or recheck only; do not run the reports")
    ap.add_argument("--credit-distribution", action="store_true",
                    help="only show the distribution of net credit / max loss of the spreads that pass every other rule "
                         "(no outcomes are looked at)")
    ap.add_argument("--all-data", action="store_true", help="report on every priority stock and the whole period, "
                                                            "also where contracts are missing (marked INCOMPLETE)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = phase9_settings(load_settings())
    print(f"capital for the Phase 9 backtests: Rs {settings.account.capital_inr:,.0f} "
          f"(stock_option_selling.backtest.capital_inr)")
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
        run.update(download_blocks(settings, eng, strategy, stocks, dev, status,
                                   checkpoint=lambda st: write_status(settings, st)))
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

    if args.recheck:
        want = [d for d in dev if not args.years or d.year in args.years]
        print(f"recheck (no requests): {len(stocks)} stocks")
        download_blocks(settings, eng, strategy, stocks, want, status, offline=True,
                        checkpoint=lambda st: write_status(settings, st))
        print(f"progress written to {write_status(settings, status)}")
        hist.not_stored.clear()
        eng.intraday.not_stored.clear()
        eng.set_expiry_floor(None)
    if args.no_report:
        return 0
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
    if args.credit_distribution:
        span = [d for d in dev if first is None or d >= first]
        eng.last_session = dev[-1]
        dist = credit_ratio_distribution(settings, eng, use_stocks, span)
        out_path = settings.path("data/diagnostics/credit_ratio_distribution.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(dist, indent=2), encoding="utf-8")
        print(f"\n{dist['what']}")
        print(f"  {dist['stocks']} stocks, {dist['period']['from']} .. {dist['period']['to']}; not applied: {dist['rules_not_applied']}")
        for name in ("passing_every_other_rule", "no_filter_structure"):
            x = dist[name]
            print(f"  {name:26s} n {x['observations']:4d}  median {x['median']}  25th {x['p25']}  75th {x['p75']}  "
                  f"min {x['min']}  max {x['max']}" + ("  LOW SAMPLE" if x.get("low_sample") else ""))
            for k in [k for k in x if k.startswith("share_at_or_above")]:
                print(f"      {k}: {x[k]}")
        for side in ("puts", "calls"):
            x = dist["passing_every_other_rule"][side]
            print(f"    {side:5s} n {x['observations']:4d}  median {x['median']}  25th {x['p25']}  75th {x['p75']}")
        print(f"  written to {out_path}")
        return 0
    res = run_option_backtest(settings, store, "breeze", strategy, hist, use_stocks, engine=eng,
                              exit_checks=tuple(bt.exit_checks), first_session=first, coverage_note=note,
                              gate_factory=(lambda e: PortfolioGate(settings, e)) if settings.stock_option_selling.portfolio.enabled else None,
                              extra=lambda e, syms, d, by_mode: {**rule_effects(settings, e, syms, d, by_mode),
                                                                 **policy_comparison(settings, e, syms, d),
                                                                 **incomplete_sensitivity(settings, e, syms, d, by_mode),
                                                                 **monthly_candidates(settings, e, syms, d, by_mode[next(iter(by_mode))][0]),
                                                                 **score_reports(settings, e, syms, d, by_mode),
                                                                 **groups_at_end(settings, e, syms, d)})
    print_report(res["report"], "breeze")
    print_effects(res["report"])
    rev = run_reversal_reports(settings, store, eng, use_stocks, [r["stock"] for r in ranked], dev, first, note)
    print_reversal(rev["outcomes"])
    for rep in rev["reports"].values():
        print_report(rep, "breeze")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
