"""Phase 9: the daily shortlist for stock option selling, built after the close.

For every watchlist stock with option contracts in the instrument list it applies the rules of
app/options/stock_selling.py to the stock's stored daily bars and its stored option chain of that
session, scores the valid setups (IV rank, trend clarity, room to support/resistance, liquidity)
and stores the ranked list in the `shortlist` table. The page /stock-option-selling and
GET /api/stock-option-selling/shortlist only read that table.

A stock is left off the list, with the reason shown, when anything it needs is missing or stale:
no daily bar for the session, no option chain stored that day (a failed chain request is never
guessed), no lot size, an illiquid strike, or results / a board meeting / a corporate action before
expiry. A shortlist row is a candidate, not a signal: signal cards and alerts come after phase 6.

Universe: the watchlist stocks (the Nifty 50). The broker's other F&O stocks have no price history
or live prices in this system yet, so they cannot be assessed.

Run after the close:  python -m app.options.shortlist            # loads the stock chains (paced), then builds
                      python -m app.options.shortlist --stored   # builds from the chains already stored today
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta, timezone

from ..config import Settings
from ..costs import CostModel
from ..history import bar_date, daily_start
from ..indicators import after_last_break, gap_breaks
from ..manual_data import Event
from ..models import utcnow
from ..quality import IST
from ..store import Store
from .analytics import OptionDesk, atm_iv_pct
from .base import ChainQuotes, ExpiryInfo, OptionContext
from .chain import stored_quotes, wanted_specs
from .greeks import days_to_expiry, years_to_expiry
from .instruments import InstrumentBook
from .margin import MarginBook
from .reversal import ReversalScanner, build_reversal_strategies
from .actionable import RESEARCH_LABEL, actionable_check
from .option_history import OptionHistory, stock_iv_series
from .portfolio import Correlations, universe_groups
from .stock_profile import HISTORY_LABEL, breadth_pct, fii_streak_starts, range_speed_profile, scenario_history, vix_spike_dates
from .stock_selling import StockOptionSelling, build_shortlist, build_stock_selling, phase9_settings

log = logging.getLogger("shortlist")


def universe(settings: Settings, book: InstrumentBook, today: date, now: datetime) -> list[str]:
    """Watchlist stocks with unexpired option contracts in a fresh instrument list."""
    return [i.symbol for i in settings.watchlist if i.kind == "stock" and book.specs(i.symbol, today, now)]


def session_date(settings: Settings, store: Store, source: str) -> date | None:
    """The newest session the index has a daily bar for."""
    bars = store.history_bars("bars_1d", settings.regime.index_symbol, source, limit=1)
    return bar_date(bars[-1]) if bars else None


def live_daily(settings: Settings, store: Store, bars: list, sym: str) -> tuple:
    """The daily history a research candidate sees: adjusted for the splits, bonuses and demergers known
    at the last bar (app/corporate_actions.py), raw prices after the last break when none is stored."""
    from ..corporate_actions import PRICE_ACTIONS, AdjustedHistory
    acts = store.corporate_actions(sym, PRICE_ACTIONS)
    fixes = store.price_corrections(sym)
    if not acts and not fixes:
        return tuple(after_last_break(bars, settings.history.max_overnight_gap_pct))
    return AdjustedHistory(bars, acts, settings.history.max_overnight_gap_pct, fixes).window(len(bars) - 1, len(bars))


def coverage_reason(settings: Settings, store: Store, book: InstrumentBook, sym: str, session: date,
                    now: datetime) -> str | None:
    """EVENT_COVERAGE_UNKNOWN with why, when the stock's event coverage does not run to its next monthly expiry (the
    stock option selling rule, `stock_selling.event_coverage`); None when covered or the expiry is not known."""
    from .stock_selling import EVENT_COVERAGE_UNKNOWN, event_coverage
    nxt = next((x.expiry for x in book.specs(sym, session, now) if x.kind == "monthly"), None)
    if nxt is None:
        return None
    rows = [Event(e["date"], e["type"], e["note"] or "", e["symbol"] or "") for e in store.all_events()
            if e["type"] == "COVERAGE" and e["symbol"] == sym]
    ok, text = event_coverage(rows, sym, session, nxt, settings.stock_option_selling.event_coverage_max_age_days)
    return None if ok else f"{EVENT_COVERAGE_UNKNOWN}: {text}"


def stock_context(settings: Settings, store: Store, source: str, book: InstrumentBook, desk: OptionDesk,
                  costs: CostModel, margins: MarginBook, sym: str, session: date, now: datetime,
                  regime: str = "UNKNOWN") -> tuple[OptionContext | None, str | None]:
    """(context for one stock at the close of `session`, None) or (None, why it cannot be assessed)."""
    o = settings.options
    cfg = settings.option_underlying(sym)
    bars = store.history_bars("bars_1d", sym, source, limit=400)
    if not bars or bar_date(bars[-1]) != session:
        return None, f"DATA_STALE: no daily bar for {session:%d-%b-%Y} (run python -m app.history)"
    spot = bars[-1].close
    asof = datetime.combine(session, time(15, 30), IST).astimezone(timezone.utc)
    rate, div = o.risk_free_rate_pct / 100.0, (cfg.dividend_yield_pct if cfg else 0.0) / 100.0
    chains, expiries, atm_now = {}, [], None
    chain_as_of = {}
    for spec in wanted_specs(book, settings, sym, session, now):
        quotes, as_of = stored_quotes(store, source, sym, spec.expiry)
        if not quotes or as_of is None or as_of.astimezone(IST).date() != session:
            continue                                   # no chain from this session: unknown, never guessed
        chains[spec.expiry] = quotes
        chain_as_of[spec.expiry] = as_of
        expiries.append(ExpiryInfo(spec.expiry, spec.kind, spec.lot_size))
        if atm_now is None and days_to_expiry(asof, spec.expiry) >= 5:
            atm_now = atm_iv_pct(quotes, spot, years_to_expiry(asof, spec.expiry), rate, div)
    if not chains:
        return None, (f"NO_CHAIN: no option chain stored for {session:%d-%b-%Y} (the request failed or was not made); "
                      f"the stock is not eligible today")
    iv = desk.iv_measure(sym, session, atm_now)
    sos = settings.stock_option_selling
    events = tuple(Event(e["date"], e["type"], e["note"] or "", e["symbol"] or "")
                   for e in store.events_between(session - timedelta(days=sos.event_coverage_max_age_days), max(chains)))
    return OptionContext(
        underlying=sym, kind="stock", asof=asof, regime=regime, spot=spot, expiries=tuple(expiries),
        quotes=ChainQuotes(spot, chains), costs=costs, capital_inr=settings.account.capital_inr, rate=rate,
        dividend=div, status="LIVE", iv_reference_pct=iv["value_pct"], iv_rank=iv["rank"],
        allowed_kinds=("monthly",), margin=lambda structure, width: margins.lookup(sym, structure, width, session),
        require_margin=True, historical=False, max_spread_pct=sos.max_bid_ask_pct_of_mid,
        min_oi_lots=sos.min_open_interest_lots, search_steps=o.backtest.strike_search_steps, events=events,
        daily=live_daily(settings, store, bars, sym),
        index_daily=tuple(store.history_bars("bars_1d", settings.regime.index_symbol, source, limit=400)),
        market={"reversal": reversal_scanner(settings, store, source, sym).info_on(session),
                "breadth_pct": breadth_on(settings, store, source, session), "chain_as_of": chain_as_of,
                "daily_bar_date": bar_date(bars[-1]), **score_inputs_from_store(settings, store, source, sym, session)}), None


# What to fill in when a scenario has no input data. Nothing is ever invented for a missing file.
DATA_NEEDED = {
    "after an FII selling streak": (
        "no data: data/fii_dii.csv has no rows. Fill it with one row per session: date,fii_net_cr,dii_net_cr "
        "(FII/FPI and DII net cash-market figures in Rs crore, selling negative)"),
    "after an RBI rate decision": (
        "no data: data/events.csv has no RBI_POLICY rows. Add one row per past decision day: "
        "date,type,note,symbol with type RBI_POLICY and symbol left blank"),
    "after its own results": (
        "no data: data/events.csv has no RESULTS rows for this stock. Add one row per past results day: "
        "date,type,note,symbol with type RESULTS and the stock's symbol"),
}


def scenario_triggers(settings: Settings, store: Store, source: str, sym: str, floor: date, session: date) -> dict:
    """Trigger dates of each scenario from the stored data files, or None (with what to fill in)
    when a file has nothing for it. Dates are only read, never made up."""
    p = settings.stock_option_selling.profile
    # every input is restricted to [floor, session] before any streak or spike is computed: a streak on the
    # floor date never counts selling days before it, and nothing after the as-of date is ever read
    inside = lambda d: floor <= d <= session
    sessions = [d for d in (bar_date(b) for b in store.history_bars("bars_1d", settings.regime.index_symbol, source))
                if inside(d)]
    fii = [(r["date"], r["fii_net_cr"]) for r in store.fii_dii_all() if inside(r["date"])]
    events = store.events_between(floor, session)
    rbi = [e["date"] for e in events if e["type"] == "RBI_POLICY"]
    results = [e["date"] for e in events if e["type"] == "RESULTS" and e["symbol"] == sym]
    # the percentile looks back a year, but never before the floor: older Breeze VIX bars are wrong-scale
    # (issue 30). In the first year after the floor the percentile part is unknown, so no day is a KNOWN
    # non-spike day and no episode can start there (vix_spike_dates needs one before the first spike day)
    vix = [b for b in store.history_bars("bars_1d", settings.options.iv_reference_symbol, source)
           if inside(bar_date(b))]
    return {
        "after an FII selling streak": {
            "dates": fii_streak_starts(fii, settings.bias.streak_days, True, sessions) if fii else None,
            "missing": DATA_NEEDED["after an FII selling streak"]},
        f"after an India VIX spike (up {p.vix_spike_rise_pct:g}% over {p.vix_spike_sessions} sessions or above its "
        f"{p.vix_spike_percentile:g}th percentile)": {
            "dates": vix_spike_dates(vix, p, floor) if len(vix) > 1 else None,
            "missing": "no data: no India VIX daily history stored (run python -m app.history)"},
        "after an RBI rate decision": {"dates": rbi or None, "missing": DATA_NEEDED["after an RBI rate decision"]},
        "after its own results": {"dates": results or None, "missing": DATA_NEEDED["after its own results"]},
    }


def stock_view(settings: Settings, store: Store, source: str, sym: str, now: datetime | None = None) -> dict:
    """Range and speed profile and scenario history of one stock, from stored daily bars on or after
    backtest.test_start (2019-01-22) and after its last unadjusted price break, up to the newest
    stored session. With a fresh option chain it also shows the move implied by option prices and,
    when the rules reach a sold strike, how often a strike that far away was passed before."""
    now = now or utcnow()
    settings = phase9_settings(settings)                   # research pages use the Phase 9 capital
    sos = settings.stock_option_selling
    floor = settings.backtest.test_start.get("1day")
    session = session_date(settings, store, source)
    stored = store.history_bars("bars_1d", sym, source)
    bars = after_last_break(stored, settings.history.max_overnight_gap_pct)
    cut = len(bars) < len(stored)
    bars = [b for b in bars if floor is None or bar_date(b) >= floor]
    out = {"stock": sym, "label": HISTORY_LABEL, "session_date": session.isoformat() if session else None,
           "profile": None, "scenarios": [], "setup": None, "notes": []}
    if not bars or session is None:
        out["notes"].append("no daily bars stored for this stock (run python -m app.history)")
        return out
    if cut and (floor is None or bar_date(bars[0]) > floor):
        out["notes"].append(f"history starts {bar_date(bars[0]):%d-%b-%Y}: an unadjusted price break (split, bonus or "
                            f"demerger) before it makes older prices unusable, so the windows are shorter than asked")
    right = distance = iv = None
    try:
        costs = CostModel(settings.costs)
        costs.require("options")
        book = InstrumentBook(store, source)
        desk = OptionDesk(settings, store, source, lambda s: None, lambda: "UNKNOWN", lambda: now)
        margins = MarginBook.from_store(store, settings.options.margin.max_age_days)
        ctx, why = stock_context(settings, store, source, book, desk, costs, margins, sym, session, now)
    except Exception as e:      # noqa: BLE001 - missing cost rates and the like: the history is still shown
        ctx, why = None, f"{type(e).__name__}: {e}"
    if ctx is None:
        out["notes"].append(f"no option-price figures: {why}")
    else:
        a = build_stock_selling(settings).assess(ctx)
        right, distance, iv = a.side, a.distance_pct, ctx.iv_reference_pct
        out["setup"] = {"status": a.status, "side": a.side, "expiry": a.expiry.isoformat() if a.expiry else None,
                        "short_strike": a.short_strike, "distance_pct": a.distance_pct, "reasons": a.reasons}
    dte = None
    if ctx is not None:
        exp_d = date.fromisoformat(out["setup"]["expiry"]) if out["setup"] and out["setup"]["expiry"] else \
            next((e.expiry for e in ctx.expiries if (e.expiry - session).days >= sos.entry_dte_min), None)
        dte = (exp_d - session).days if exp_d else None
    out["profile"] = range_speed_profile(bars, sos.profile, floor, right, distance, iv, sos.swing_pivot_bars, dte)
    out["scenarios"] = scenario_history(bars, scenario_triggers(settings, store, source, sym, floor or bar_date(bars[0]),
                                                                bar_date(bars[-1])), sos.profile, floor)
    return out


def store_correlations(settings: Settings, store: Store, source: str, window: int | None = None,
                       min_overlap: int | None = None) -> Correlations:
    """Daily-return correlations from the stored daily bars (completed sessions only). `window` and
    `min_overlap` default to the phase 9 portfolio settings (phase 5 passes its own)."""
    cfg = settings.stock_option_selling.portfolio
    sessions = [bar_date(b) for b in store.history_bars("bars_1d", settings.regime.index_symbol, source)]
    cache: dict = {}

    def daily(sym):
        if sym not in cache:
            cache[sym] = store.history_bars("bars_1d", sym, source)
        return cache[sym]

    def breaks(sym):
        bars = daily(sym)
        return gap_breaks([b.open for b in bars], [b.close for b in bars], settings.history.max_overnight_gap_pct)

    return Correlations(daily, sessions, breaks, window or cfg.correlation_window,
                        min_overlap or cfg.correlation_min_overlap)


def correlation_groups(settings: Settings, store: Store, source: str) -> dict:
    """The correlated groups of the watchlist stocks at the newest stored session (same sector, or
    daily-return correlation >= correlation_threshold over correlation_window sessions), for the page."""
    session = session_date(settings, store, source)
    if session is None:
        return {"date": None, "groups": [], "sizes": [], "problems": [], "problem_count": 0}
    corr = store_correlations(settings, store, source)
    stocks = [i.symbol for i in settings.watchlist if i.kind == "stock"]
    sectors = {i.symbol: (i.sector or None) for i in settings.watchlist}
    return {"date": session.isoformat(), **universe_groups(stocks, sectors, lambda a, b: corr.at(session, a, b),
                                                           settings.stock_option_selling.portfolio.correlation_threshold)}


def score_inputs_from_store(settings: Settings, store: Store, source: str, sym: str, session: date) -> dict:
    """The score's market inputs at the close of `session` from stored data (OptionEngine.score_inputs
    computes the same in backtests): the stock's at-the-money IV of the sessions before, and the
    rel_strength_lookback returns of the stock, NIFTY and the sector peers."""
    sos = settings.stock_option_selling
    cfg = settings.option_underlying(sym)
    closes = {bar_date(b): b.close for b in store.history_bars("bars_1d", sym, source, limit=400)}
    hist_src = OptionHistory(store, source)
    monthly = [e for e, k in hist_src.expiries(settings.regime.index_symbol) if k == "monthly"]
    series = stock_iv_series(hist_src, sym, monthly, closes, cfg.backtest_strike_step if cfg else None,
                             settings.options.risk_free_rate_pct / 100, (cfg.dividend_yield_pct if cfg else 0) / 100)
    window = [v for d, v in sorted(series.items()) if d < session][-sos.score.iv_peak_sessions:]
    sessions = [bar_date(b) for b in store.history_bars("bars_1d", settings.regime.index_symbol, source, limit=400)]
    n = sos.trend_block.rel_strength_lookback

    def ret(s: str) -> float | None:
        if session not in sessions or sessions.index(session) < n:
            return None
        c = {bar_date(b): b.close for b in store.history_bars("bars_1d", s, source, limit=400)}
        d0 = sessions[sessions.index(session) - n]
        return None if not c.get(d0) or c.get(session) is None else (c[session] / c[d0] - 1) * 100

    sector = next((x.sector for x in settings.watchlist if x.symbol == sym), None)
    peers = [x.symbol for x in settings.watchlist if x.kind == "stock" and x.symbol != sym and sector and x.sector == sector]
    floor = settings.backtest.test_start.get("1day")
    from .score import history_fall_pct
    every = store.history_bars("bars_1d", sym, source)          # all of it: the fall percentile reads every window since the floor
    fall = history_fall_pct(every, session, floor, gap_breaks([b.open for b in every], [b.close for b in every],
                                                              settings.history.max_overnight_gap_pct), sos.score)
    return {"hist_fall_pct": fall, "iv_window": tuple(window), "stock_ret_pct": ret(sym),
            "index_ret_pct": ret(settings.regime.index_symbol),
            "peer_rets_pct": tuple(r for r in (ret(x) for x in peers) if r is not None),
            "stats_floor": daily_start(floor) if floor else None}


_BREADTH: dict = {}


def breadth_on(settings: Settings, store: Store, source: str, session: date) -> float | None:
    """Market breadth at the close of `session` from the stored daily bars (stocks without a bar that
    day are left out; too few usable stocks means unknown)."""
    key = (id(store), source, session)
    if key not in _BREADTH:
        cfg = settings.stock_option_selling.market_block
        stocks = [i.symbol for i in settings.watchlist if i.kind == "stock"]
        rows = []
        for sym in stocks:
            bars = store.history_bars("bars_1d", sym, source, limit=settings.stock_option_selling.sma_mid)
            if bars and bar_date(bars[-1]) == session:
                rows.append([b.close for b in bars])
        _BREADTH.clear()
        _BREADTH[key] = breadth_pct(rows, settings.stock_option_selling.sma_mid, cfg.min_coverage_pct, len(stocks))
    return _BREADTH[key]


def reversal_scanner(settings: Settings, store: Store, source: str, sym: str) -> ReversalScanner:
    """The stock's reversal states from its stored daily bars on or after backtest.test_start."""
    floor = settings.backtest.test_start.get("1day")
    bars = [b for b in store.history_bars("bars_1d", sym, source) if floor is None or bar_date(b) >= floor]
    breaks = gap_breaks([b.open for b in bars], [b.close for b in bars], settings.history.max_overnight_gap_pct)
    sos = settings.stock_option_selling
    return ReversalScanner(bars, store.history_bars("bars_1d", settings.regime.index_symbol, source), sos.reversal,
                           sos.sma_long, breaks, sos.swing_pivot_bars)


WATCH_NOTE = "Watch list only, never a signal. Reversal candidates do not alert until you approve them."


def reversal_watch(settings: Settings, store: Store, source: str, now: datetime | None = None) -> dict:
    """Washed-out stocks, stocks forming a base and confirmed reversals at the newest stored session.
    For a confirmed reversal (while the base low holds, for `lift_sessions`) the two candidate trades
    are worked out from the option chain stored that session; without one the row says so. Read-only."""
    now = now or utcnow()
    settings = phase9_settings(settings)                   # research pages use the Phase 9 capital
    sos = settings.stock_option_selling
    session = session_date(settings, store, source)
    out = {"session_date": session.isoformat() if session else None, "note": WATCH_NOTE, "rows": [],
           "alerts_approved": sos.reversal.alerts_approved, "enabled": sos.reversal.enabled}
    if session is None or not sos.reversal.enabled:
        return out
    put_st, call_st = build_reversal_strategies(settings)
    made = None
    for inst in settings.watchlist:
        if inst.kind != "stock":
            continue
        sc = reversal_scanner(settings, store, source, inst.symbol)
        if not sc.bars or sc.dates[-1] != session:
            out.setdefault("stocks_without_a_bar_for_the_session", []).append(inst.symbol)
            continue                                     # no bar for the session: unknown, not listed
        info = sc.info(len(sc.bars) - 1)
        if info["state"] is None:
            continue
        row = {"stock": inst.symbol, "state": info["state"], "label": "watch", "close": sc.bars[-1].close,
               "drawdown_pct": info["drawdown_pct"], "high_52w": info["high_52w"], "base_low": info["base_low"],
               "confirmed_on": info["confirmed_on"], "reasons": info["reasons"], "stall_zones": info["stall_zones"],
               "candidates": []}
        if info["lift_active"]:
            if made is None:
                costs = CostModel(settings.costs)
                costs.require("options")
                made = (InstrumentBook(store, source), OptionDesk(settings, store, source, lambda s: None, lambda: "UNKNOWN",
                                                                   lambda: now),
                        costs, MarginBook.from_store(store, settings.options.margin.max_age_days))
            ctx, why = stock_context(settings, store, source, made[0], made[1], made[2], made[3], inst.symbol, session, now)
            if ctx is None:
                row["candidates"] = [{"candidate": "bull call spread", "status": "NO_TRADE", "reasons": [why]},
                                     {"candidate": "put credit spread below the base low", "status": "NO_TRADE", "reasons": [why]}]
            else:
                from dataclasses import replace
                live = replace(ctx, market={**ctx.market, "reversal": {**info, "confirmed_today": True}})
                for name, st in (("bull call spread", call_st), ("put credit spread below the base low", put_st)):
                    sigs = st.evaluate(live)
                    row["candidates"].append({"candidate": name, "status": "CANDIDATE" if sigs else "NO_TRADE",
                                              "label": st.status_label, "reasons": list(st.last_no_trade),
                                              "trade": sigs[0].to_dict() if sigs else None})
        out["rows"].append(row)
    stale = out.get("stocks_without_a_bar_for_the_session")
    if stale:
        out["note"] += (f" {len(stale)} stock(s) have no daily bar for {session:%d-%b-%Y} and are not assessed "
                        f"(run python -m app.history).")
    order = {"REVERSAL_CONFIRMED": 0, "BASE_FORMING": 1, "WASHED_OUT": 2}
    out["rows"].sort(key=lambda r: (order[r["state"]], r["stock"]))
    return out


def run_shortlist(settings: Settings, store: Store, source: str, now: datetime | None = None,
                  strategy: StockOptionSelling | None = None, save: bool = True) -> dict:
    """Assess the universe at the close of the newest stored session and store the ranked list."""
    now = now or utcnow()
    settings = phase9_settings(settings)                   # research pages use the Phase 9 capital
    session = session_date(settings, store, source)
    if session is None:
        return {"session_date": None, "rows": [], "error": "no daily bars stored for the index"}
    strategy = strategy or build_stock_selling(settings)
    book = InstrumentBook(store, source)
    costs = CostModel(settings.costs)
    costs.require("options")
    margins = MarginBook.from_store(store, settings.options.margin.max_age_days)
    desk = OptionDesk(settings, store, source, lambda s: None, lambda: "UNKNOWN", lambda: now)
    contexts, left_out = [], []
    stocks = universe(settings, book, session, now)
    for sym in stocks:
        ctx, why = stock_context(settings, store, source, book, desk, costs, margins, sym, session, now)
        if ctx is None:
            # every reason that is known without the chain: the data problem, and the event coverage
            whys = [why] + [x for x in (coverage_reason(settings, store, book, sym, session, now),) if x]
            left_out.append({"stock": sym, "expiry": None, "side": None, "rank": None, "status": "EXCLUDED",
                             "score": None, "scores": {}, "reasons": whys, "label": RESEARCH_LABEL,
                             "actionable": False, "not_actionable_because": whys,
                             "snapshot": {"session_date": session.isoformat()}})
        else:
            contexts.append(ctx)
    stored_corr = store_correlations(settings, store, source)
    sectors = {i.symbol: (i.sector or None) for i in settings.watchlist}

    def annotate(ctx, a) -> dict:
        """Every shortlist row is a research candidate from the session snapshot; whether it would be
        actionable is checked against the live rules (positions are not recorded yet: never, for now)."""
        as_of = ctx.market.get("chain_as_of") or {}
        ok, why = actionable_check(settings, a.signal, ctx, now, as_of, None, sectors,
                                   lambda x, y: stored_corr.at(session, x, y))
        return {"label": RESEARCH_LABEL, "actionable": ok, "not_actionable_because": why,
                "snapshot": {"session_date": session.isoformat(),
                             "daily_bar_date": ctx.market["daily_bar_date"].isoformat(),
                             "chain_as_of_utc": {e.isoformat(): x.isoformat() for e, x in sorted(as_of.items())}}}

    rows = build_shortlist(strategy, contexts, annotate) + left_out
    if not stocks:
        rows = [{"stock": "-", "expiry": None, "side": None, "rank": None, "status": "EXCLUDED", "score": None,
                 "scores": {}, "reasons": ["LOT_SIZE_UNKNOWN: the instrument list is missing or older than 5 days"]}]
    if save:
        store.save_shortlist(session, rows, now)
    return {"session_date": session.isoformat(), "rows": rows, "stocks": len(stocks)}


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from ..config import ROOT, load_settings
    ap = argparse.ArgumentParser(description="Daily shortlist for stock option selling (after the close)")
    ap.add_argument("--stored", action="store_true", help="do not ask Breeze: use the option chains already stored today")
    ap.add_argument("--stocks", nargs="*", help="load chains for these stocks only (default: every watchlist stock)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    source = "breeze" if settings.is_live_feed else "sim"
    store = Store(settings.db_path(), live=settings.is_live_feed)
    if not args.stored and settings.is_live_feed:
        from ..feeds.breeze_feed import BreezeFeed
        from .chain import BreezeChainSource, ChainLoader
        from .instruments import refresh_from_broker
        load_dotenv(ROOT / ".env")
        print(f"instrument list: {refresh_from_broker(store, settings.watchlist)} contract expiries stored")
        book = InstrumentBook(store, source)
        now = utcnow()
        want = [s.upper() for s in args.stocks] if args.stocks else universe(settings, book, now.astimezone(IST).date(), now)
        loader = ChainLoader(settings, store, BreezeChainSource(BreezeFeed(settings.watchlist)), book)
        failed = {k: v["error"] for k, v in loader.load(want).items() if "error" in v}
        print(f"option chains loaded for {len(want)} stocks; {len(failed)} failed (those stocks are not eligible today)")
        for k, v in list(failed.items())[:10]:
            print(f"  {k}: {v}")
    out = run_shortlist(settings, store, source)
    if out.get("error"):
        print(out["error"])
        return 1
    top = [r for r in out["rows"] if r["status"] == "SHORTLISTED"]
    print(f"shortlist for {out['session_date']}: {len(top)} candidate(s) of {out['stocks']} stocks")
    for r in top:
        print(f"  {r['rank']:2d} {r['stock']:12s} {'bull put' if r['side'] == 'PE' else 'bear call'}  {r['expiry']}  "
              f"score {r['score']:.2f}  {r['reasons'][-1]}")
    why = {}
    for r in out["rows"]:
        if r["status"] != "SHORTLISTED" and r["reasons"]:
            why.setdefault(r["reasons"][-1].split(":")[0], []).append(r["stock"])
    for k, v in sorted(why.items()):
        print(f"  not listed - {k}: {len(v)} ({', '.join(v[:8])}{' ...' if len(v) > 8 else ''})")
    print("open http://127.0.0.1:8000/stock-option-selling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
