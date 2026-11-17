"""Option strategy backtests on daily bars, each beside its no-filter baseline.

Price source (one per run; every trade records it):
- "option_history": stored Breeze option bars (real traded prices; history has no bid/ask). Used for
  the breeze source. A contract that is not stored is UNKNOWN: with --download it is fetched once,
  otherwise the trade is skipped and counted.
- "model": Black-Scholes prices from the underlying's bars (sim source only). The report is then
  labelled "payoff simulation".

Rules (the phase 3 engine's rules, applied to options):
- A signal comes from a day's CLOSE; the entry is the NEXT session's open of every leg, with adverse
  slippage on each leg. No entry if any leg has no bar that day, if the next session is not the next
  market session, or if the fill is at or beyond the signal's stop or target.
- Spreads are judged on the closing prices of their legs (cost to buy the spread back). The exit
  decision (target, stop, time before expiry) is taken at a close and filled at the NEXT session's
  open of every leg. A position is never held to settlement.
- A bought option uses its own daily high/low: stop first if a day touches both; a gap through the
  stop fills at the open. Time and expiry exits are at the close.
- A trade is dropped, never given an invented price, if a leg (or the underlying) has no bar for a
  session while it is open (data_exclusions).
- Costs: option brokerage per lot per executed leg, STT, exchange, SEBI, stamp duty and GST from
  config.yaml (app/costs.py), plus options slippage per side.
- Every trade is ONE lot at the lot size in today's instrument list (past lot sizes are not in the
  list); sizing against capital is phase 5. The strategy holds one open position per underlying and
  enters once per expiry for spreads.
- A signal whose sold option has no bought hedge is never entered (UNHEDGED_SHORT), whatever built it.
- Sessions before backtest.test_start (2019-01-22) are never tested; the holdout is not touched.

No-filter baseline: the same strategy code with its entry rules off (no regime, IV-rank, credit or
trigger rule), entered on a FIXED SCHEDULE known in advance: for spreads and condors, one attempt per
expiry on the first market session on which that expiry is inside the entry window; for option buying,
one attempt on the first market session of every month. There is exactly one attempt: if the scheduled
day has no valid strikes or prices, or the entry cannot be filled at the next session's open, that
expiry (or month) is recorded as skipped and never retried on a later day. Baseline positions of
different expiries may overlap (each is its own one-lot trade). Same strike rule, width, expiry choice,
exits, costs and price source. A credit spread has two baselines (bull put, bear call).

Not checked in backtests (history cannot know them): the margin entry and the bid/ask eligibility
rules (a bid and an ask on every traded strike, bid-ask width). Every report is therefore labelled
"excludes margin and bid/ask eligibility checks"; both stay required for live signals.

Run:  python -m app.options.backtest [--strategy credit_spread ...] [--underlyings NIFTY]
                                     [--source breeze|sim] [--download]
"""
from __future__ import annotations

import argparse
import bisect
import heapq
import json
import logging
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from ..backtest import Engine, dev_holdout, load_calendar, stats, windows
from ..config import Settings
from ..costs import CostBreakdown, money
from ..history import bar_date, daily_start
from ..indicators import gap_breaks
from ..models import Bar, Signal, utcnow
from ..quality import IST
from ..session import CASH_FNO, DERIVATIVES, close_utc, hours as session_hours
from ..regime import fii_bias
from ..store import Store
from ..strategies import MarketContext, build_strategies
from . import build_option_strategies
from .base import ExpiryInfo, OptionContext, OptionQuote, OptionStrategy, add_costs, index_option_budget
from .greeks import iv_rank
from .payoff import UNHEDGED_SHORT, check_hedged
from .instruments import InstrumentBook
from .stock_profile import (MARKET_UNKNOWN, MARKET_WEAK, ClosedTrade, Snapshot, alert_kinds, breadth_pct, market_weak,
                            vix_spike_series,
                            snapshot)
from .stock_selling import SpreadPosition, roll_choices, roll_money, trading_days_to
from .option_history import (ModelPrices, OptionHistory, OptionIntraday, atm_candidates, budgeted_fetcher,
                             discover_expiries, simulated_expiries, stock_iv_series)

log = logging.getLogger("option_backtest")

PRICE_SOURCE_LABEL = {
    "option_history": "stored Breeze option prices (traded prices, daily bars; no bid/ask in history)",
    "model": "payoff simulation (Black-Scholes model prices, NOT market prices)",
}
DATA_EXCLUSION_KEYS = ("DROPPED_MISSING_OPTION_BAR", "DROPPED_MISSING_DAILY_BAR_DURING_TRADE",
                       "DROPPED_DATA_BREAK_DURING_TRADE", "NO_OPTION_BAR_AT_ENTRY")
ELIGIBILITY_LABEL = "excludes margin and bid/ask eligibility checks"
# Checks a live signal must pass that history cannot answer. They are NOT counted as passed: the results
# simply do not include them, and every report says so.
BYPASSED_CHECKS = [
    "margin: past margins are not known (a live signal needs a current margin entry)",
    "bid/ask: option history has traded prices only (a live signal needs a bid and an ask within the width limit on every leg)",
    "highest-OI strike: history has open interest per contract, not across the chain",
    "events before expiry and event coverage: not available historically (announcements known at each past decision "
    "time are not stored); live and research candidates need a fresh COVERAGE row in events.csv",
    "lot sizes: today's lot size is used for every past date (past lot sizes are not in the instrument list)",
]
IV_EXPIRY_REACH_DAYS = 75     # stock IV history: an at-the-money call is used at most 70 days before its expiry
EXIT_CHECK_LABEL = {
    "daily": "target and stop checked on the legs' daily closes, filled at the next session's open",
    "intraday": "synchronized 5-minute bars of every leg: entered at the first synchronized bar's open of the session "
                "after the signal, target and stop checked on synchronized bar closes, every fill at the next "
                "synchronized bar's open; a campaign with a session lacking such a bar is data-incomplete and left out",
}
OPEN_AT_CUTOFF = "OPEN_AT_CUTOFF"
FORCED_EXPIRY_EXIT = "FORCED_EXPIRY_EXIT"     # phase 9: out at the first session inside the forced-exit window
# Order of the steps that fall on one instant (OptionEngine.run_portfolio): decisions on what is known at
# that instant, then new entry signals, then fills at the open of the bar that starts there.
DECIDE, SIGNAL, FILL = 0, 1, 2
# phase 9 strategies sized in whole lots by max_risk_per_position_pct: re-sized at the actual entry fill
RISK_SIZED_STRATEGIES = ("stock_option_selling", "reversal_put_spread", "reversal_bull_call_spread")


def option_stats(trades: list, capital: float, min_trades: int) -> dict:
    """Backtest statistics with positions still open at the cutoff kept apart (phase 9). Closed trades
    give the counts, win rate, averages, profit factor and the realised net; open positions are marked
    at the last close after estimated liquidation costs and enter only the equity figures
    (net_incl_open_inr, max_drawdown_incl_open_inr). Without open positions it equals stats()."""
    closed = [t for t in trades if t.exit_reason != OPEN_AT_CUTOFF]
    still = [t for t in trades if t.exit_reason == OPEN_AT_CUTOFF]
    s = stats(closed, capital, min_trades)
    mtm = mtm_equity(trades)              # drawdown of the daily mark-to-market portfolio equity
    s["equity_basis"] = {"drawdown": "daily mark-to-market portfolio equity (closed trades plus open positions "
                                     "marked at each session's close after estimated closing charges)",
                         "sessions": mtm["sessions"], "stale_position_days": mtm["stale_position_days"],
                         "max_mark_age_minutes": mtm["max_mark_age_minutes"],
                         "five_minute_runs": "marked at the last synchronized 5-minute bar of each session; a "
                                             "session without one carries the previous mark (stale, with its age)"}
    if not still:
        s.update({"net_incl_open_inr": s["net_pnl_inr"], "max_drawdown_incl_open_inr": mtm["max_drawdown_inr"],
                  "open_at_cutoff": {"positions": 0}})
        return s
    eq = stats(trades, capital, min_trades)
    s.update({"net_incl_open_inr": eq["net_pnl_inr"], "max_drawdown_incl_open_inr": mtm["max_drawdown_inr"],
              "open_at_cutoff": {
                  "positions": len(still),
                  "unrealised_inr": round(sum(t.net_pnl for t in still), 2),
                  "estimated_liquidation_costs_inr": round(sum(t.liquidation_costs_inr or 0.0 for t in still), 2),
                  "mark_time": max(t.mark_time or t.exit_time for t in still),
                  # how old the price behind each mark is at the end of the cutoff session (minutes) ...
                  "mark_age_minutes": sorted(mark_age_minutes(t) for t in still),
                  # ... kept apart from how long each position had been held at the cutoff (days)
                  "holding_days": sorted((datetime.fromisoformat(t.mark_time or t.exit_time)
                                          - datetime.fromisoformat(t.entry_time)).days for t in still),
                  "note": "marked at the last tested close after estimated liquidation costs; excluded from the trade "
                          "count, win rate and profit factor; included in the equity and drawdown figures"}})
    return s


def mark_age_minutes(t) -> int:
    """Minutes between the price an open-at-cutoff mark uses and the end of that session (the options' close)."""
    at = datetime.fromisoformat(t.mark_time or t.exit_time).astimezone(IST)
    end = datetime.combine(at.date(), session_hours(DERIVATIVES, at.date()).close, IST)
    return max(0, int((end - at).total_seconds() // 60))


def session_end(d: date) -> datetime:
    """The end of option session d, in UTC: the options' close (app/session.py: 15:30 IST before 3 Aug 2026, 15:40
    after)."""
    return close_utc(DERIVATIVES, d)


def _read_mark(m) -> tuple[date, float, datetime]:
    """(session, value, time of its prices) of a stored mark; a mark without a price time is of that
    session's close."""
    d = m[0] if isinstance(m[0], date) else date.fromisoformat(m[0])
    at = m[2] if len(m) > 2 else session_end(d)
    return d, m[1], at if isinstance(at, datetime) else datetime.fromisoformat(at)


def mtm_equity(trades: list) -> dict:
    """The portfolio's daily mark-to-market equity: on each session, the net P&L of every trade closed by
    then plus every open position's mark of that session (its last mark when that day has none, or a mark
    carried from an earlier session: counted as stale). Each open position-day's mark age is the time from
    its prices to the end of the session (15:30 IST); the largest is reported. Drawdown = the largest fall
    from a running peak (starting at 0) of this series."""
    exits, marks = {}, []
    for t in trades:
        x = datetime.fromisoformat(t.exit_time).astimezone(IST).date()
        exits.setdefault(x, []).append(t.net_pnl)
        m = sorted((_read_mark(v) for v in (t.marks or [])), key=lambda v: v[0])
        marks.append((datetime.fromisoformat(t.entry_time).astimezone(IST).date(), x, m))
    days = sorted(set(exits) | {a for _, _, m in marks for a, _, _ in m})
    equity = peak = dd = 0.0
    realised, stale, series, max_age = 0.0, 0, [], 0
    for day in days:
        realised += sum(exits.get(day, ()))
        open_value = 0.0
        for start, end, m in marks:
            if not (start <= day < end):
                continue
            k = bisect.bisect_right([a for a, _, _ in m], day)
            if k == 0:
                stale += 1                                   # no mark yet for an open position
                continue
            _, value, at = m[k - 1]
            if at.astimezone(IST).date() != day:
                stale += 1                                   # carried from an earlier session
            max_age = max(max_age, int((session_end(day) - at).total_seconds() // 60))
            open_value += value
        equity = realised + open_value
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        series.append((day, round(equity, 2)))
    return {"max_drawdown_inr": round(dd, 2), "sessions": len(series), "stale_position_days": stale,
            "max_mark_age_minutes": max_age}


DATA_INCOMPLETE_KEYS = ("DATA_INCOMPLETE_CAMPAIGNS", "DATA_INCOMPLETE:5_MINUTE_BARS_NOT_STORED",
                        "DATA_INCOMPLETE:NO_SYNCHRONIZED_BAR_IN_SESSION", "DATA_INCOMPLETE:MISSING_LEG_BAR")
ELIGIBILITY_NOTE = ("Backtest results exclude the margin and bid/ask eligibility checks: past margins are not known and "
                    "option history has traded prices only (no bid/ask). A live signal still needs a current margin "
                    "entry and a bid and an ask within the width limit on every traded strike.")


@dataclass
class OptionTrade:
    strategy_id: str
    strategy_version: str
    variant: str                  # strategy | baseline | baseline:PE | baseline:CE
    symbol: str
    structure: str
    expiry: str
    legs: list[dict]              # action, right, strike, entry_price, exit_price (after slippage)
    lot_size: int
    lots: int
    signal_id: str
    signal_time: str
    regime: str
    entry_time: str
    entry_net: float              # per share: credit received (+) or premium paid (-), after slippage
    exit_time: str
    exit_net: float               # per share: paid to close (-) or received (+), after slippage
    exit_reason: str              # TARGET | STOP | TIME | FORCED_EXPIRY_EXIT | EXPIRY_EXIT | PERIOD_END | OPEN_AT_CUTOFF (a mark)
    gross_pnl: float
    costs: dict
    net_pnl: float
    price_source: str             # option_history | model
    max_loss_inr: float | None = None
    iv_rank: float | None = None
    why_now: list[str] = field(default_factory=list)
    entry_conditions: dict = field(default_factory=dict)   # phase 9: what was known at the signal (state, IV rank, ...)
    campaign_id: str = ""         # phase 9: the original signal's id; a roll keeps its original trade's id
    roll_no: int = 0              # 0 = the original trade, 1 = its first roll, ...
    max_profit_inr: float | None = None
    roll_refused: str = ""        # exit policy with rolls: the rule that refused a roll when this spread was closed
    # OPEN_AT_CUTOFF only: estimated charges to close the position at the mark (the net P&L already deducts them)
    liquidation_costs_inr: float | None = None
    exit_check: str = "daily"     # daily | intraday: how target and stop were checked for this trade
    exit_basis: str = ""          # what decided this exit: 5-minute bar | daily close | rule at the close
    # daily mark-to-market while open: [session, net P&L if closed at that session's leg closes, after all charges]
    marks: list = field(default_factory=list)
    mark_time: str | None = None  # OPEN_AT_CUTOFF only: time of the prices the mark uses


def synchronized_bars(leg_bars: list[list]) -> list[tuple[datetime, list]]:
    """The 5-minute bars of ONE session in which EVERY leg traded: [(bar start, [that bar of each leg])],
    oldest first. A spread is only ever priced from such a bar: the legs' prices are of the same five
    minutes, and no leg is carried forward from an earlier trade."""
    by_start = [{b.start: b for b in bars} for bars in leg_bars]
    if not by_start:
        return []
    common = sorted(set.intersection(*(set(x) for x in by_start)))
    return [(s, [x[s] for x in by_start]) for s in common]


def sync_decision(sync: list[tuple[datetime, list]], signs: list[int], target: float, stop: float,
                  use_stop: bool = True) -> tuple[str, int, float] | None:
    """Target and stop of a credit spread on its synchronized bars. `signs`: +1 for a sold leg, -1 for a
    bought one, so sum(sign x close) is the cost to buy the spread back at a bar's close. Returns
    (TARGET | STOP, index of the deciding bar, cost) for the first bar whose close reaches a level, or
    None. The fill is never on the deciding bar: the caller fills at the open of the NEXT synchronized
    bar (bar index + 1, or the first one of a later session)."""
    for k, (_, bars) in enumerate(sync):
        cost = sum(s * b.close for s, b in zip(signs, bars))
        if cost <= target:
            return "TARGET", k, cost
        if use_stop and cost >= stop:
            return "STOP", k, cost
    return None


class QuoteOverride:
    """Quotes with the last traded price of some contracts replaced (the open spread's legs at a
    5-minute bar's close), for a roll decision taken inside a session."""

    def __init__(self, quotes, prices: dict):
        self._q, self._p = quotes, prices
        self.oi_available = quotes.oi_available

    def strikes(self, expiry):
        return self._q.strikes(expiry)

    def atm_candidates(self, expiry):
        return self._q.atm_candidates(expiry)

    def quote(self, expiry, strike, right):
        px = self._p.get((expiry, float(strike), right))
        return OptionQuote(strike, right, None, None, px, None, None, None) if px is not None else self._q.quote(expiry, strike, right)

    def highest_oi_strike(self, expiry, right):
        return self._q.highest_oi_strike(expiry, right)


class HistoryQuotes:
    """Quotes at the close of one past day, from the price source. Last traded prices only; open
    interest is known per contract but not across the chain, so the highest-OI rule cannot be applied.

    An option's daily bar (close, volume, open interest) is final only at the options' close of its day (15:30
    before 3 Aug 2026, 15:40 after: app/session.py). With `asof` (the decision time), reading a bar of day `d` before
    that raises LookAheadError: a decision at the stocks' 15:35 close could otherwise see trades of 15:35-15:40."""
    oi_available = False

    def __init__(self, prices, underlying: str, d: date, spot: float, step: float | None,
                 asof: datetime | None = None):
        self.prices, self.underlying, self.d, self.spot, self.step = prices, underlying, d, spot, step
        self.asof = asof

    def strikes(self, expiry: date) -> list[float]:
        step = self.step or _auto_step(self.spot)
        lo, hi = self.spot * 0.75, self.spot * 1.25
        k = (int(lo / step) + 1) * step
        out = []
        while k <= hi:
            out.append(round(k, 2))
            k += step
        return out

    def atm_candidates(self, expiry: date) -> list[float]:
        return atm_candidates(self.spot, self.step)

    def quote(self, expiry: date, strike: float, right: str) -> OptionQuote | None:
        final_at = close_utc(DERIVATIVES, self.d)
        if self.asof is not None and self.asof < final_at:
            from ..strategies.base import LookAheadError
            raise LookAheadError(f"option daily bar of {self.d} is final at {final_at.astimezone(IST):%H:%M} IST, after "
                                 f"the decision time {self.asof.astimezone(IST):%H:%M}")
        b = self.prices.bar(self.underlying, expiry, strike, right, self.d)
        return None if b is None else OptionQuote(strike, right, None, None, b.close, b.oi, None, b.volume)

    def highest_oi_strike(self, expiry: date, right: str) -> float | None:
        return None


def _auto_step(spot: float) -> float:
    from .instruments import nice_step
    return nice_step(spot)


@dataclass
class _Position:
    sig: Signal
    legs: list            # OptionLeg
    fills: list[Decimal]  # entry fill per leg, after slippage
    entry_d: date
    entry_i: int
    variant: str
    pending_exit: str | None = None
    pending_basis: str = ""
    campaign_id: str = ""
    roll_no: int = 0
    realised: float = 0.0         # net P&L of the campaign's earlier positions
    pending_roll: Signal | None = None   # the spread to open when this one is closed (exit policies with rolls)
    last_bar: datetime | None = None     # synchronized execution: start of the bar this position was opened on
    roll_refused: str = ""
    entry_bar: datetime | None = None    # synchronized execution: the actual first synchronized entry bar (its start)
    max_loss_inr: float | None = None    # recomputed from the actual entry fills (None: the signal's estimate)
    marks: list = field(default_factory=list)   # [(session, mark-to-market net P&L, time of its prices)] while open


class OptionEngine:
    def __init__(self, settings: Settings, store: Store, source: str, prices, base: Engine | None = None,
                 lot_sizes: dict[str, int] | None = None, intraday: OptionIntraday | None = None):
        self.s, self.store, self.source, self.prices = settings, store, source, prices
        self.intraday = intraday         # 5-minute option bars (exit_check "intraday"); None = not available
        # SENSITIVITY ONLY (never the primary result): a session of an open spread without a synchronized bar
        # is judged on the legs' daily closes instead of making the campaign data-incomplete.
        self.sync_missing_at_daily_close = False
        self.base = base or Engine(settings, store, source, calendar=load_calendar(settings, store))
        self.costs = self.base.costs
        self.o = settings.options
        self.rate = self.o.risk_free_rate_pct / 100.0
        self.kinds = {i.symbol: i.kind for i in settings.watchlist}
        book = InstrumentBook(store, source, max_age_days=36500)      # any stored list: backtests are not live decisions
        self._lots = lot_sizes if lot_sizes is not None else {u: book.nearest_lot_size(u) for u in book.underlyings()}
        ref = self.base.daily(self.o.iv_reference_symbol)
        self._ref_iv = {bar_date(b): b.close for b in ref}
        self._iv: dict[str, tuple[list[date], list[float]]] = {}
        self._bars_by_date: dict[str, dict[date, Bar]] = {}
        self._market: dict[date, dict] = {}
        self._breadth: dict[date, float | None] = {}
        self._close_map: dict[str, dict] = {}
        self._score_in: dict[tuple, dict] = {}
        self._vix_spike: dict | None = None
        self._stock_closes: dict | None = None
        self._reversal: dict[str, object] = {}
        self._adjusted: dict[str, object] = {}
        self.adjust_for_actions = True          # False: raw daily history only (to measure what the adjustment changes)
        self.no_entry: dict[str, set[date]] = {}   # stock -> sessions without entries (excluded stock-years)
        self._trigger = None
        self.expiry_floor: date | None = None    # ignore expiries before this date (set_expiry_floor)
        # Last session a run tests. A stock's IV history then leaves out the at-the-money calls of
        # expiries more than IV_EXPIRY_REACH_DAYS after it: they cannot matter, and they would be
        # option prices of the holdout period.
        self.last_session: date | None = None

    def adjusted_history(self, sym: str):
        """The stock's point-in-time adjusted daily history (corporate actions in the store), or None when
        the store has no price action for it (then the raw bars after the last break are used)."""
        if self.adjust_for_actions is False or self.kinds.get(sym) != "stock":
            return None
        if sym not in self._adjusted:
            from ..corporate_actions import PRICE_ACTIONS, AdjustedHistory
            acts = self.store.corporate_actions(sym, PRICE_ACTIONS)
            fixes = self.store.price_corrections(sym)          # exact corrections of Breeze errors (app/data_quality.py)
            self._adjusted[sym] = (AdjustedHistory(self.base.daily(sym), acts, self.s.history.max_overnight_gap_pct, fixes)
                                   if acts or fixes else None)
        return self._adjusted[sym]

    def daily_window(self, sym: str, i: int, start: int, max_len: int = 1300) -> tuple:
        """The daily bars a decision at bar i sees (see adjusted_history)."""
        adj = self.adjusted_history(sym)
        if adj is not None:
            return adj.window(i, max_len)
        bars = self.base.daily(sym)
        return tuple(bars[max(start, i - max_len + 1):i + 1])

    def set_expiry_floor(self, floor: date | None) -> None:
        """Limit the expiries the engine looks at (used while downloading history block by block, so a
        block does not ask for the at-the-money contracts of every older month). Clears the IV cache."""
        self.expiry_floor = floor
        self._iv.clear()

    # ---- inputs ----
    def lot_size(self, sym: str) -> int | None:
        return self._lots.get(sym)

    def expiries(self, sym: str) -> list[tuple[date, str]]:
        """Stored expiries. Stock monthly contracts expire with the index monthly contract, so a stock
        uses the index's monthly dates (a wrong date simply has no data and the trade is skipped)."""
        if self.kinds.get(sym) == "stock":
            out = [(e, k) for e, k in self.prices.expiries(self.s.regime.index_symbol) if k == "monthly"]
        else:
            out = self.prices.expiries(sym)
        return [(e, k) for e, k in out if self.expiry_floor is None or e >= self.expiry_floor]

    def iv_series(self, sym: str) -> tuple[list[date], list[float]]:
        """Implied-volatility measure by date: India VIX for an index; for a stock, the implied
        volatility of the stored at-the-money calls struck about 35 and 15 days before each monthly
        expiry. Point in time: a call is used only from the day its strike was chosen, so the value
        for a day never depends on a later close (option_history.stock_iv_series)."""
        if sym not in self._iv:
            if self.kinds.get(sym) == "index":
                series = self._ref_iv
            elif self.prices.label == "model":
                series = self._ref_iv
            else:
                closes = {bar_date(b): b.close for b in self.base.daily(sym)}
                cfg = self.s.option_underlying(sym)
                reach = None if self.last_session is None else self.last_session + timedelta(days=IV_EXPIRY_REACH_DAYS)
                series = stock_iv_series(self.prices, sym,
                                         [e for e, _ in self.expiries(sym) if reach is None or e <= reach], closes,
                                         cfg.backtest_strike_step if cfg else None, self.rate,
                                         (cfg.dividend_yield_pct if cfg else 0) / 100.0)
            ds = sorted(series)
            self._iv[sym] = (ds, [series[d] for d in ds])
        return self._iv[sym]

    def iv_at(self, sym: str, d: date) -> tuple[float | None, float | None]:
        """(IV measure on day d, its IV rank against the previous lookback sessions)."""
        ds, vs = self.iv_series(sym)
        i = bisect.bisect_left(ds, d)
        if i >= len(ds) or ds[i] != d:
            return None, None
        hist = vs[max(0, i - self.o.iv_rank_lookback_days):i]
        return vs[i], iv_rank(vs[i], hist, self.o.iv_rank_min_history_days)

    def market_at(self, d: date) -> dict:
        """Market facts known after the close of day d: the FII bias for the next session (from the
        flows up to d; None when the figures are not in the data file) and India VIX's change on d."""
        if d not in self._market:
            nxt = self.base.next_session(d) or d + timedelta(days=1)
            streak = self.s.bias.streak_days
            bias, _ = fii_bias(self.store.fii_dii_before(nxt, streak + 3), nxt, self.base.calendar, streak)
            dates = self.base.session_dates()
            i = bisect.bisect_left(dates, d)
            prev = self._ref_iv.get(dates[i - 1]) if 0 < i < len(dates) and dates[i] == d else None
            now = self._ref_iv.get(d)
            if self._vix_spike is None:
                floor = self.s.backtest.test_start.get("1day")   # older Breeze VIX bars are wrong-scale (issue 30)
                self._vix_spike = vix_spike_series([b for b in self.base.daily(self.o.iv_reference_symbol)
                                                    if floor is None or bar_date(b) >= floor],
                                                   self.s.stock_option_selling.profile)
            self._market[d] = {"fii_bias": None if bias == "UNKNOWN" else bias,
                               "vix_change_pct": round((now / prev - 1) * 100, 2) if now and prev else None,
                               "vix_spike": self._vix_spike.get(d)}
        return self._market[d]

    def _close_on(self, sym: str, d: date) -> float | None:
        if sym not in self._close_map:
            self._close_map[sym] = {bar_date(b): b.close for b in self.base.daily(sym)}
        return self._close_map[sym].get(d)

    def score_inputs(self, sym: str, d: date) -> dict:
        """What the score needs that the strategy cannot compute from the stock's own bars, known at the
        close of day d: the IV window before d, and the rel_strength_lookback returns of the stock, the
        index and the stock's sector peers. The statistics floor (backtest.test_start) is passed too."""
        key = (sym, d)
        if key not in self._score_in:
            sos = self.s.stock_option_selling
            ds, vs = self.iv_series(sym)
            i = bisect.bisect_left(ds, d)
            window = vs[max(0, i - sos.score.iv_peak_sessions):i]
            sessions = self.base.session_dates()
            k = bisect.bisect_left(sessions, d)
            n = sos.trend_block.rel_strength_lookback
            ret = lambda s: (None if k < n or not self._close_on(s, sessions[k - n]) or self._close_on(s, d) is None
                             else (self._close_on(s, d) / self._close_on(s, sessions[k - n]) - 1) * 100)
            sector = next((x.sector for x in self.s.watchlist if x.symbol == sym), None)
            peers = [x.symbol for x in self.s.watchlist if x.kind == "stock" and x.symbol != sym and sector and x.sector == sector]
            floor = self.s.backtest.test_start.get("1day")
            from .score import history_fall_pct
            fall = history_fall_pct(self.base.daily(sym), d, floor, self.base.breaks(sym), sos.score) \
                if self.kinds.get(sym) == "stock" else {"PE": None, "CE": None}
            self._score_in[key] = {"hist_fall_pct": fall, "iv_window": tuple(window), "stock_ret_pct": ret(sym),
                                   "index_ret_pct": ret(self.s.regime.index_symbol),
                                   "peer_rets_pct": tuple(r for r in (ret(x) for x in peers) if r is not None),
                                   "stats_floor": daily_start(floor) if floor else None}
        return self._score_in[key]

    def breadth_at(self, d: date) -> float | None:
        """Market breadth known at the close of day d: share of the watchlist stocks above their
        sma_mid average, from each stock's bars up to and including d (cached)."""
        if d not in self._breadth:
            cfg = self.s.stock_option_selling.market_block
            sma_mid = self.s.stock_option_selling.sma_mid
            if self._stock_closes is None:
                self._stock_closes = {}
                for inst in self.s.watchlist:
                    if inst.kind == "stock":
                        bars = self.base.daily(inst.symbol)
                        self._stock_closes[inst.symbol] = ([bar_date(b) for b in bars], [b.close for b in bars])
            rows = []
            for dates, closes in self._stock_closes.values():
                k = bisect.bisect_right(dates, d)
                if k and dates[k - 1] == d:                      # the stock traded that day
                    rows.append(closes[max(0, k - sma_mid):k])
            self._breadth[d] = breadth_pct(rows, sma_mid, cfg.min_coverage_pct, len(self._stock_closes))
        return self._breadth[d]

    def reversal_scanner(self, sym: str):
        """The stock's reversal states for every day of its history (point in time; cached)."""
        if sym not in self._reversal:
            from .reversal import ReversalScanner
            sos = self.s.stock_option_selling
            floor = self.s.backtest.test_start.get("1day")       # post-2019 data only: older Breeze bars are unreliable
            bars = [b for b in self.base.daily(sym) if floor is None or bar_date(b) >= floor]
            breaks = gap_breaks([b.open for b in bars], [b.close for b in bars], self.s.history.max_overnight_gap_pct)
            self._reversal[sym] = ReversalScanner(bars, self.base.daily(self.s.regime.index_symbol), sos.reversal,
                                                  sos.sma_long, breaks, sos.swing_pivot_bars)
        return self._reversal[sym]

    def snapshot_at(self, sym: str, bars: tuple, d: date) -> Snapshot | None:
        sos = self.s.stock_option_selling
        m = self.market_at(d)
        code, _ = market_weak(self.base.index_daily(d)[-400:], self.breadth_at(d), sos.market_block,
                              sos.sma_long, sos.sma_mid)
        return snapshot(bars, sos.pivot_lookback_sessions, sos.trend_block, m["fii_bias"], m["vix_spike"],
                        self.base.regime.at_close(d).regime,
                        market_weak=None if code == MARKET_UNKNOWN else code == MARKET_WEAK, pivot_bars=sos.swing_pivot_bars,
                        sma_long=sos.sma_long)

    def trigger_strategy(self):
        if self._trigger is None:
            self._trigger = build_strategies(self.s, [self.o.backtest.trigger])[0]
        return self._trigger

    def context(self, strategy: OptionStrategy, sym: str, d: date, spot: float, variant: str,
                trigger: Signal | None = None, daily: tuple = (), closed_trades: tuple = ()) -> OptionContext:
        kind = self.kinds.get(sym, "stock")
        cfg = self.s.option_underlying(sym)
        iv, rank = self.iv_at(sym, d)
        lot = self.lot_size(sym)
        exps = tuple(ExpiryInfo(e, k, lot) for e, k in self.expiries(sym) if e >= d)[:8]
        # Decided at the options' close (15:30 before 3 Aug 2026; 15:40 after): the underlying's official close
        # (15:35) and the options' daily close, volume and open interest (final at 15:40) are all known then; the
        # fills are at the next session's open, as before (review fix, 2026-11-17).
        asof = close_utc(DERIVATIVES, d)
        return OptionContext(
            underlying=sym, kind=kind, asof=asof, regime=self.base.regime.at_close(d).regime, spot=spot,
            expiries=exps, quotes=HistoryQuotes(self.prices, sym, d, spot, cfg.backtest_strike_step if cfg else None,
                                                asof=asof),
            costs=self.costs, capital_inr=self.s.account.capital_inr, rate=self.rate,
            dividend=(cfg.dividend_yield_pct if cfg else 0.0) / 100.0, iv_reference_pct=iv, iv_rank=rank,
            trigger=trigger, allowed_kinds=tuple(cfg.expiries) if cfg else ("monthly",), margin=None,
            require_margin=False, historical=True, max_spread_pct=self.o.chain.max_spread_pct,
            min_oi_lots=self.o.chain.min_open_interest_lots, search_steps=self.o.backtest.strike_search_steps,
            no_filter=variant != "strategy", baseline_right=variant.split(":")[1] if ":" in variant else None,
            daily=daily, index_daily=self.base.index_daily(d)[-400:] if daily else (),
            closed_trades=closed_trades,
            option_risk_budget_inr=index_option_budget(self.s) if kind == "index" else None,
            market={**self.market_at(d), "reversal": self.reversal_scanner(sym).info_on(d),
                    "breadth_pct": self.breadth_at(d), **self.score_inputs(sym, d)} if daily else {})

    # ---- fills ----
    def _leg_bars(self, sym: str, legs, d: date):
        out = []
        for leg in legs:
            b = self.prices.bar(sym, leg.expiry, leg.strike, leg.right, d)
            if b is None:
                return None
            out.append(b)
        return out

    def _enter(self, sig: Signal, sym: str, d: date, i: int, variant: str, skipped: Counter) -> _Position | None:
        if check_hedged(sig.legs or ()):
            skipped[UNHEDGED_SHORT] += 1          # never entered, whatever produced the signal
            return None
        if daily_start(d) >= sig.valid_until:
            skipped["NO_NEXT_BAR"] += 1
            return None
        bars = self._leg_bars(sym, sig.legs, d)
        if bars is None:
            skipped["NO_OPTION_BAR_AT_ENTRY"] += 1
            return None
        fills = self._try_fill(sig, [b.open for b in bars], skipped)
        return None if fills is None else self._sized(_Position(sig, list(sig.legs), fills, d, i, variant), skipped)

    def _sized(self, pos: _Position, skipped: Counter) -> _Position | None:
        """Phase 9 (signals sized by the risk budget): after the actual slipped entry fills, recompute the
        cost-inclusive maximum loss; above max_risk_per_position_pct of capital the position is cut in
        whole lots, and with zero lots left it is not entered (ZERO_SIZE). Never more lots than signalled.
        The recomputed maximum loss is what the position and the portfolio book carry."""
        sig = pos.sig
        if sig.strategy_id not in RISK_SIZED_STRATEGIES or len(pos.legs) != 2:
            return pos
        from .payoff import OptionLeg
        from .sizing import ZERO_SIZE, size_spread
        lots = pos.legs[0].lots
        one = [OptionLeg(leg.action, leg.right, leg.strike, leg.expiry, 1, float(f), leg.lot_size)
               for leg, f in zip(pos.legs, pos.fills)]
        kind = "credit" if sig.direction == "SPREAD" else "debit"
        budget = self.s.account.capital_inr * self.s.stock_option_selling.max_risk_per_position_pct / 100.0
        n, legs_n, pay = size_spread(kind, one, pos.legs[0].lot_size, self.costs, budget, max_lots=lots)
        if n == 0:
            skipped[ZERO_SIZE + "_AT_FILL"] += 1
            return None
        if n < lots:
            skipped["RESIZED_AT_FILL"] += 1
            pos.legs = [replace(leg, lots=n) for leg in pos.legs]
        pos.max_loss_inr = float(pay.max_loss_inr)
        return pos

    def _enter_sync(self, sig: Signal, sym: str, d: date, i: int, variant: str, skipped: Counter) -> _Position | None:
        """Synchronized execution: enter at the open of the session's FIRST 5-minute bar in which every
        leg traded. No such bar, or the bars not stored: no entry (never a guess, never retried)."""
        if check_hedged(sig.legs or ()):
            skipped[UNHEDGED_SHORT] += 1
            return None
        if daily_start(d) >= sig.valid_until:
            skipped["NO_NEXT_BAR"] += 1
            return None
        sync = self._sync_bars(sym, sig.legs, d)
        if sync is None:
            skipped["5_MINUTE_BARS_NOT_STORED_AT_ENTRY"] += 1
            return None
        if not sync:
            skipped["NO_SYNCHRONIZED_BAR_AT_ENTRY"] += 1
            return None
        start, bars = sync[0]
        fills = self._try_fill(sig, [b.open for b in bars], skipped)
        if fills is None:
            return None
        pos = _Position(sig, list(sig.legs), fills, d, i, variant)
        pos.last_bar = pos.entry_bar = start
        return self._sized(pos, skipped)

    def _try_fill(self, sig: Signal, opens: list[float], skipped: Counter) -> list[Decimal] | None:
        """Entry fills (after slippage) at the given opening prices, or None when the price has already
        passed the signal's stop or target."""
        fills = [self.costs.fill_price("options", o, leg.action) for leg, o in zip(sig.legs, opens)]
        raw = sum((Decimal(str(o)) * (1 if leg.action == "SELL" else -1) for leg, o in zip(sig.legs, opens)), Decimal(0))
        net = sum((f * (1 if leg.action == "SELL" else -1) for leg, f in zip(sig.legs, fills)), Decimal(0))
        stop, target = Decimal(str(sig.stop)), Decimal(str(sig.target))
        if sig.direction == "SPREAD":
            # entry = credit received; target/stop = cost to buy back. Tradeable only between them.
            for price, why in ((raw, "GAPPED_PAST_STOP_OR_TARGET"), (net, "FILL_BEYOND_STOP_OR_TARGET")):
                if not (target < price < stop):
                    skipped[why] += 1
                    return None
            if net >= Decimal(str(sig.details["width"])):
                skipped["FILL_BEYOND_STOP_OR_TARGET"] += 1
                return None
        else:
            for price, why in ((-raw, "GAPPED_PAST_STOP_OR_TARGET"), (-net, "FILL_BEYOND_STOP_OR_TARGET")):
                if not (stop < price < target):
                    skipped[why] += 1
                    return None
        return fills

    def _close(self, pos: _Position, sym: str, d: date, raw_exit: list[float], at_open: bool, reason: str,
               strategy: OptionStrategy, exit_check: str = "daily", basis: str = "",
               exit_dt: datetime | None = None) -> OptionTrade:
        gross = Decimal(0)
        total = CostBreakdown()
        legs_out, exit_net = [], Decimal(0)
        for leg, fill, raw in zip(pos.legs, pos.fills, raw_exit):
            side = "BUY" if leg.action == "SELL" else "SELL"
            out = self.costs.fill_price("options", raw, side)
            q = Decimal(leg.lots * leg.lot_size)
            if leg.action == "SELL":
                sell_v, buy_v = money(fill * q), money(out * q)
                exit_net -= out
            else:
                buy_v, sell_v = money(fill * q), money(out * q)
                exit_net += out
            gross += sell_v - buy_v
            add_costs(total, self.costs.options(leg.lots, buy_v, sell_v))
            legs_out.append({"action": leg.action, "right": leg.right, "strike": leg.strike,
                             "expiry": leg.expiry.isoformat(), "lots": leg.lots, "lot_size": leg.lot_size,
                             "entry_price": float(fill), "exit_price": float(out)})
        entry_net = sum((f * (1 if leg.action == "SELL" else -1) for leg, f in zip(pos.legs, pos.fills)), Decimal(0))
        sig = pos.sig
        liq = None
        if reason == OPEN_AT_CUTOFF:                 # what closing it at the mark would cost
            buy_v = sum((Decimal(str(x["exit_price"])) * x["lots"] * x["lot_size"] for x in legs_out if x["action"] == "SELL"),
                        Decimal(0))
            sell_v = sum((Decimal(str(x["exit_price"])) * x["lots"] * x["lot_size"] for x in legs_out if x["action"] == "BUY"),
                         Decimal(0))
            liq = float(self.costs.options(pos.legs[0].lots, money(buy_v), money(sell_v)).total)
        t_exit = exit_dt or (datetime.combine(d, time(9, 15), IST).astimezone(timezone.utc) if at_open else session_end(d))
        return OptionTrade(
            strategy_id=strategy.id, strategy_version=strategy.version, variant=pos.variant, symbol=sym,
            structure=sig.details["structure"], expiry=pos.legs[0].expiry.isoformat(), legs=legs_out,
            lot_size=pos.legs[0].lot_size, lots=pos.legs[0].lots, signal_id=sig.signal_id,
            signal_time=sig.created_at.isoformat(), regime=sig.regime,
            entry_time=(pos.entry_bar or daily_start(pos.entry_d)).isoformat(), entry_net=float(entry_net),
            exit_time=t_exit.isoformat(), exit_net=float(exit_net), exit_reason=reason,
            gross_pnl=float(gross), costs=total.as_dict(), net_pnl=float(gross - total.total),
            price_source=self.prices.label,
            max_loss_inr=pos.max_loss_inr if pos.max_loss_inr is not None else sig.details.get("max_loss_inr"),
            marks=[[m[0].isoformat(), m[1], m[2].isoformat()] for m in pos.marks],
            mark_time=(exit_dt or session_end(d)).isoformat()
            if reason == OPEN_AT_CUTOFF else None,
            iv_rank=sig.details.get("iv_rank"), why_now=list(sig.why_now),
            entry_conditions=dict(sig.details.get("entry_conditions") or {}),
            campaign_id=pos.campaign_id or sig.signal_id, roll_no=pos.roll_no,
            max_profit_inr=sig.details.get("max_profit_inr"), exit_check=exit_check, exit_basis=basis,
            liquidation_costs_inr=liq)

    def _mark(self, pos: _Position, sym: str, d: date, strategy: OptionStrategy, sync: bool = False) -> None:
        """The open position's net P&L if it were closed at the session's mark (exit slippage and every
        charge included), for the daily mark-to-market equity: (session, value, time of the prices used).

        Five-minute (synchronized) runs: the closes of the LAST synchronized 5-minute bar of d, the same
        prices the run trades on - never the legs' independent daily closes, which may be of different
        moments. No synchronized bar that day: the previous mark is carried with its own price time, so
        its age is recorded (none yet: no mark). Daily runs: the legs' daily closes; a leg without a daily
        bar gives no mark that day (the equity carries the last one and counts it as stale)."""
        if sync:
            bars = self._sync_bars(sym, pos.legs, d)
            if bars:
                start, last = bars[-1]
                at = start + timedelta(minutes=5)
                t = self._close(pos, sym, d, [b.close for b in last], False, "MARK", strategy, exit_dt=at)
                pos.marks.append((d, round(t.net_pnl, 2), at))
            elif pos.marks:
                pos.marks.append((d, pos.marks[-1][1], pos.marks[-1][2]))
            return
        bars = self._leg_bars(sym, pos.legs, d)
        if bars is None:
            return
        t = self._close(pos, sym, d, [b.close for b in bars], False, "MARK", strategy)
        pos.marks.append((d, round(t.net_pnl, 2), session_end(d)))

    def _exit_days(self, strategy: OptionStrategy, sym: str) -> int:
        p = strategy.params
        if hasattr(p, "exit_days_before_expiry"):
            return p.exit_days_before_expiry
        return p.stock_exit_days_before_expiry if self.kinds.get(sym) == "stock" else p.index_exit_days_before_expiry

    def _late_exit(self, strategy: OptionStrategy, sym: str, nxt: date, expiry: date) -> str | None:
        """The exit a credit spread is given at a close because of the calendar alone, filled at the next
        session `nxt`: TIME when nxt is within the time exit (calendar days before expiry), else
        FORCED_EXPIRY_EXIT when nxt is inside the forced-exit window (phase 9:
        `forced_exit_trading_days_before_expiry` trading days, counted with the run's holiday calendar) -
        no position is ever inside that window. `strategy.time_exit = False` switches the time exit off
        (tests and comparisons); the forced exit cannot be switched off."""
        if getattr(strategy, "time_exit", True) and (expiry - nxt).days <= self._exit_days(strategy, sym):
            return "TIME"
        forced = getattr(strategy.params, "forced_exit_trading_days_before_expiry", None)
        if forced is not None and trading_days_to(nxt, expiry, self.base.calendar.is_trading_day) <= forced:
            return FORCED_EXPIRY_EXIT
        return None

    def _sync_bars(self, sym: str, legs, d: date):
        """The synchronized 5-minute bars of session d for these legs, or None when any leg's 5-minute
        bars are unknown (not stored, or the session is outside the stored window)."""
        if self.intraday is None:
            return None
        per = []
        for leg in legs:
            bars = self.intraday.session(sym, leg.expiry, leg.strike, leg.right, d)
            if bars is None:
                return None
            per.append(bars)
        return synchronized_bars(per)

    def _leg_gaps(self, sym: str, legs, d: date) -> list[datetime]:
        """Bar starts of session d at which SOME leg traded but not every leg: the spread's value at
        that moment is not known. Empty when the bars are not stored (that case is _sync_bars' None)."""
        if self.intraday is None:
            return []
        starts = []
        for leg in legs:
            bars = self.intraday.session(sym, leg.expiry, leg.strike, leg.right, d)
            if bars is None:
                return []
            starts.append({b.start for b in bars})
        if not starts:
            return []
        return sorted(set.union(*starts) - set.intersection(*starts))

    def _sync_exit(self, pos: _Position, sym: str, d: date, i: int, sync: list, k: int, reason: str,
                   strategy: OptionStrategy):
        """Close at the open of synchronized bar k (a generator: it yields the moment of the fill BEFORE
        filling, see iter_stream). With a roll pending, the old spread is closed and the replacement
        opened at the first bar from there on in which all four legs traded; without one in this session
        the roll is not made and the spread is simply closed."""
        start, bars = sync[k]
        rolled = None
        if pos.pending_roll is not None:               # where the roll would fill: read from the bars alone
            new = pos.pending_roll
            four = self._sync_bars(sym, list(pos.legs) + list(new.legs), d)
            for s4, b4 in (four or []):
                if s4 < start:
                    continue
                fills = self._try_fill(new, [x.open for x in b4[len(pos.legs):]], Counter())
                if fills is not None:
                    rolled = self._sized(_Position(new, list(new.legs), fills, d, i, pos.variant), Counter())
                    if rolled is not None:            # re-sized at its actual fill; zero lots: no roll
                        rolled.last_bar = rolled.entry_bar = s4
                        start, bars = s4, b4[:len(pos.legs)]
                break
        yield (start, FILL)
        done = self._close(pos, sym, d, [b.open for b in bars], False, "ROLLED" if rolled is not None else reason,
                           strategy, "intraday", pos.pending_basis, exit_dt=start)
        if rolled is not None:
            rolled.campaign_id, rolled.roll_no = done.campaign_id, pos.roll_no + 1
            rolled.realised = pos.realised + done.net_pnl
        else:
            done.roll_refused = "ROLL_NOT_FILLED" if pos.pending_roll is not None else pos.roll_refused
        return ("trade", done, rolled)

    def _sync_entry(self, sig: Signal, sym: str, d: date, i: int, variant: str, skipped: Counter, row: dict | None):
        """A pending entry of session d on synchronized bars (a generator): it yields the moment of the
        fill - the first bar in which every leg traded, or the session's open when there is none and the
        entry is not made - and returns the position or None."""
        sync = self._sync_bars(sym, sig.legs, d) if not check_hedged(sig.legs or ()) else None
        yield (sync[0][0] if sync else daily_start(d), FILL)
        before = Counter(skipped)
        pos = self._enter_sync(sig, sym, d, i, variant, skipped)
        if row is not None:
            why = sorted((skipped - before).keys())
            row.update(result="entered" if pos is not None else "skipped",
                       reason=None if pos is not None else (why[0] if why else "NOT_ENTERED"))
        return pos

    def _sync_roll_decision(self, pos: _Position, sym: str, d: date, start: datetime, bars: list, cost: float,
                            strategy: OptionStrategy, ctx_prev) -> None:
        """A stop reached at a synchronized bar's close under the roll policy. The replacement is chosen
        and judged by the entry rules on the last COMPLETED session (the newest daily data known inside
        the session); the roll net credit is priced from this bar's closes of all four legs. A
        replacement leg without a bar here leaves the roll unpriced: the answer is then exit."""
        ctx = ctx_prev()
        if ctx is None:
            pos.roll_refused = "ROLL_PRICE_UNKNOWN"
            return
        short, long_ = pos.legs[0], pos.legs[1]
        signs = [1 if leg.action == "SELL" else -1 for leg in pos.legs]
        credit = float(sum((f * s for f, s in zip(pos.fills, signs)), Decimal(0)))
        held = SpreadPosition(sym, short.right, short.strike, long_.strike, short.expiry, credit, short.lot_size, short.lots)
        over = {(leg.expiry, float(leg.strike), leg.right): b.close for leg, b in zip(pos.legs, bars)}
        choice = roll_choices(strategy, replace(ctx, quotes=QuoteOverride(ctx.quotes, over)), held, cost, pos.roll_no,
                              pos.realised, today=d)
        new = choice["roll_signal"]
        if new is None:
            pos.roll_refused = choice["refused_by"] or ""
            return
        now = []
        for leg in new.legs:
            sess = self.intraday.session(sym, leg.expiry, leg.strike, leg.right, d) if self.intraday is not None else None
            bar = next((b for b in (sess or []) if b.start == start), None)
            if bar is None:
                pos.roll_refused = "ROLL_PRICE_UNKNOWN"
                return
            now.append(bar.close)
        fp = lambda px, side: float(self.costs.fill_price("options", px, side))
        m = roll_money(self.costs, held, fp(bars[0].close, "BUY"), fp(bars[1].close, "SELL"), new,
                       fp(now[0], "SELL"), fp(now[1], "BUY"))
        limit = strategy.params.roll.min_roll_net_credit_pct_of_width
        if m["roll_net_credit_pct_of_width"] is None or not m["roll_net_credit_pct_of_width"] > limit:
            pos.roll_refused = "ROLL_NET_CREDIT_TOO_SMALL"
            return
        refused = self._roll_gate(sym, d, new, pos, intraday=True)
        if refused:
            pos.roll_refused = refused
            return
        pos.pending_roll = new

    def _manage_sync(self, pos: _Position, sym: str, d: date, i: int, last_allowed: date, strategy: OptionStrategy,
                     skipped: Counter, spot: float | None, alerts: tuple, ctx_now, ctx_prev):
        """One session of an open credit spread on synchronized 5-minute bars, as a generator: it yields
        (moment, phase) BEFORE each step that reads or changes the portfolio (a decision at a bar's
        close, a fill at a bar's open, the rules at the session's close), so that iter_stream and
        run_portfolio can run every stock's steps in time order. What it reads before a yield is this
        position's own bars only. Returns
        ('trade', OptionTrade, replacement position or None) | ('hold',) | ('incomplete',).
        'incomplete': the session's 5-minute bars are not stored for a leg, or the session has no bar
        in which every leg traded; the campaign is then data-incomplete and left out of the results."""
        sig, expiry = pos.sig, pos.legs[0].expiry
        t_open, t_close, five = daily_start(d), session_end(d), timedelta(minutes=5)
        sync = self._sync_bars(sym, pos.legs, d)
        if sync is None:
            yield (t_open, FILL)
            skipped["DATA_INCOMPLETE:5_MINUTE_BARS_NOT_STORED"] += 1
            return ("incomplete",)
        gaps = self._leg_gaps(sym, pos.legs, d)
        opened_here = pos.entry_d == d and pos.last_bar is not None
        if opened_here:
            # the entry bar's own close is judged too (filled at its open); only earlier bars are left out
            sync = [x for x in sync if x[0] >= pos.last_bar]
            gaps = [g for g in gaps if g > pos.last_bar]
        signs = [1 if leg.action == "SELL" else -1 for leg in pos.legs]
        policy = getattr(strategy, "exit_policy", "stop")
        never_stop = policy == "roll_always"
        missing_leg = False
        if sync and gaps:
            # A leg without a bar while the position is open: the spread's value at that moment is unknown.
            # What the bars BEFORE the first such moment decide still stands (an exit filled before it);
            # otherwise the campaign is data-incomplete (review fix, 2026-11-15).
            usable = [x for x in sync if x[0] < gaps[0]]
            if pos.pending_exit and usable:
                return (yield from self._sync_exit(pos, sym, d, i, usable, 0, pos.pending_exit, strategy))
            hit = None if pos.pending_exit else sync_decision(usable, signs, sig.target, sig.stop, use_stop=not never_stop)
            if hit is not None and hit[1] + 1 < len(usable):
                reason, k, cost = hit
                yield (usable[k][0] + five, DECIDE)
                pos.pending_exit, pos.pending_basis = reason, "synchronized 5-minute bar"
                if reason == "STOP" and policy == "roll_rules":
                    self._sync_roll_decision(pos, sym, d, usable[k][0], usable[k][1], cost, strategy, ctx_prev)
                return (yield from self._sync_exit(pos, sym, d, i, usable, k + 1, reason, strategy))
            yield (gaps[0], FILL)
            skipped["DATA_INCOMPLETE:MISSING_LEG_BAR"] += 1
            if not self.sync_missing_at_daily_close:
                return ("incomplete",)
            missing_leg = True                               # sensitivity run: judged at the daily closes below
        if (not sync and not opened_here) or missing_leg:
            if not missing_leg:
                yield (t_open, FILL)
                skipped["DATA_INCOMPLETE:NO_SYNCHRONIZED_BAR_IN_SESSION"] += 1
            if not self.sync_missing_at_daily_close:
                return ("incomplete",)
            # sensitivity run: the session is judged at the legs' daily closes; an exit decided there is still
            # filled at the next session's first synchronized bar. No daily bar for a leg: incomplete after all.
            yield (t_close, DECIDE)
            daily = self._leg_bars(sym, pos.legs, d)
            nxt = self.base.next_session(d)
            if daily is None or pos.pending_exit or d >= last_allowed or nxt is None or nxt > expiry:
                return ("incomplete",) if daily is None or d >= last_allowed or nxt is None or nxt > expiry else ("hold",)
            cost = sum((1 if leg.action == "SELL" else -1) * b.close for leg, b in zip(pos.legs, daily))
            never = getattr(strategy, "exit_policy", "stop") == "roll_always"
            if cost <= sig.target:
                pos.pending_exit, pos.pending_basis = "TARGET", "daily close (sensitivity: no synchronized bar that session)"
            elif not never and cost >= sig.stop:
                pos.pending_exit, pos.pending_basis = "STOP", "daily close (sensitivity: no synchronized bar that session)"
            elif self._late_exit(strategy, sym, nxt, expiry):
                pos.pending_exit, pos.pending_basis = self._late_exit(strategy, sym, nxt, expiry), "rule at the close"
            return ("hold",)
        if pos.pending_exit and sync:                      # decided earlier: the first synchronized bar's open
            return (yield from self._sync_exit(pos, sym, d, i, sync, 0, pos.pending_exit, strategy))
        hit = sync_decision(sync, signs, sig.target, sig.stop, use_stop=not never_stop)
        if hit is not None:
            reason, k, cost = hit
            yield (sync[k][0] + five, DECIDE)              # decided at this bar's close, on what is known then
            pos.pending_exit, pos.pending_basis = reason, "synchronized 5-minute bar"
            if reason == "STOP" and policy == "roll_rules":
                self._sync_roll_decision(pos, sym, d, sync[k][0], sync[k][1], cost, strategy, ctx_prev)
            if k + 1 < len(sync):
                return (yield from self._sync_exit(pos, sym, d, i, sync, k + 1, reason, strategy))
            return ("hold",)                               # filled at the first synchronized bar of the next session
        # ---- rules decided at the session's close (filled at the next session's first synchronized bar) ----
        yield (t_close, DECIDE)
        nxt = self.base.next_session(d)
        if not sync:
            return ("hold",)                               # opened on the session's last synchronized bar
        last_start, last = sync[-1]
        mark = [b.close for b in last]
        at_close = last_start + five
        basis = "rule at the close"
        if d >= expiry or nxt is None or nxt > expiry:
            return ("trade", self._close(pos, sym, d, mark, False, "EXPIRY_EXIT", strategy, "intraday", basis,
                                         exit_dt=at_close), None)
        if d >= last_allowed:
            return ("trade", self._close(pos, sym, d, mark, False, self._cutoff_reason(strategy), strategy, "intraday", basis,
                                         exit_dt=at_close), None)
        cost = sum(s * c for s, c in zip(signs, mark))
        credit = float(sum((f * s for f, s in zip(pos.fills, signs)), Decimal(0)))
        side = 0 if pos.legs[0].right == "PE" else 1
        if (not never_stop and getattr(strategy, "threat_exit", False) and spot is not None
                and (spot < pos.legs[0].strike if side == 0 else spot > pos.legs[0].strike)):
            pos.pending_exit, pos.pending_basis = "STRIKE_BREACHED", basis
        elif alerts and any(a in strategy.alert_exits for a in alerts[side]):
            pos.pending_exit = "ALERT_" + next(a for a in alerts[side] if a in strategy.alert_exits)
            pos.pending_basis = "alert at the close"
        elif self._late_exit(strategy, sym, nxt, expiry):
            pos.pending_exit, pos.pending_basis = self._late_exit(strategy, sym, nxt, expiry), basis
        wants_roll = (policy == "roll_rules" and pos.pending_exit == "STRIKE_BREACHED") or \
                     (never_stop and pos.pending_exit == "TIME" and cost > credit)
        if wants_roll:
            short, long_ = pos.legs[0], pos.legs[1]
            held = SpreadPosition(sym, short.right, short.strike, long_.strike, expiry, credit, short.lot_size, short.lots)
            choice = roll_choices(strategy, ctx_now(), held, cost, pos.roll_no, pos.realised, unlimited=never_stop)
            pos.pending_roll, pos.roll_refused = choice["roll_signal"], choice["refused_by"] or ""
            if pos.pending_roll is not None:
                refused = self._roll_gate(sym, d, pos.pending_roll, pos)
                if refused:
                    pos.pending_roll, pos.roll_refused = None, refused
        return ("hold",)

    def _manage(self, pos: _Position, sym: str, d: date, i: int, last_allowed: date, strategy: OptionStrategy,
                skipped: Counter, exit_check: str = "daily", spot: float | None = None, alerts: tuple = (),
                roll_ctx=None):
        """Position at the close of day d. Returns ('trade', OptionTrade) | ('drop',) | ('hold',)."""
        bars = self._leg_bars(sym, pos.legs, d)
        if bars is None:
            if d >= last_allowed and getattr(strategy, "report_open_at_cutoff", False):
                skipped["OPEN_AT_CUTOFF_VALUATION_UNKNOWN"] += 1     # open at the cutoff, no price to mark it
            else:
                skipped["DROPPED_MISSING_OPTION_BAR"] += 1
            return ("drop",)
        sig, expiry = pos.sig, pos.legs[0].expiry
        nxt = self.base.next_session(d)
        exit_days = self._exit_days(strategy, sym)
        closes = [b.close for b in bars]
        if sig.direction == "SPREAD":
            if d >= expiry or nxt is None or nxt > expiry:
                return ("trade", self._close(pos, sym, d, closes, False, "EXPIRY_EXIT", strategy, exit_check,
                                             "rule at the close"))
            signs = [1 if leg.action == "SELL" else -1 for leg in pos.legs]
            if d >= last_allowed:
                return ("trade", self._close(pos, sym, d, closes, False, self._cutoff_reason(strategy), strategy, exit_check,
                                             "rule at the close"))
            value = sum(b.close * s for s, b in zip(signs, bars))
            policy = getattr(strategy, "exit_policy", "stop")
            never_stop = policy == "roll_always"                # "roll every month": no stop, no breach exit
            if value <= sig.target:
                pos.pending_exit, pos.pending_basis = "TARGET", "daily close"
            elif not never_stop and value >= sig.stop:
                pos.pending_exit, pos.pending_basis = "STOP", "daily close"
            elif (not never_stop and getattr(strategy, "threat_exit", False) and spot is not None
                  and (spot < pos.legs[0].strike if pos.legs[0].right == "PE" else spot > pos.legs[0].strike)):
                pos.pending_exit, pos.pending_basis = "STRIKE_BREACHED", "rule at the close"
            elif alerts and any(a in strategy.alert_exits for a in alerts[0 if pos.legs[0].right == "PE" else 1]):
                kind = next(a for a in alerts[0 if pos.legs[0].right == "PE" else 1] if a in strategy.alert_exits)
                pos.pending_exit, pos.pending_basis = "ALERT_" + kind, "alert at the close"
            elif self._late_exit(strategy, sym, nxt, expiry):
                pos.pending_exit, pos.pending_basis = self._late_exit(strategy, sym, nxt, expiry), "rule at the close"
            credit = float(sum((f * s for f, s in zip(pos.fills, signs)), Decimal(0)))
            wants_roll = (policy == "roll_rules" and pos.pending_exit in ("STOP", "STRIKE_BREACHED")) or \
                         (policy == "roll_always" and pos.pending_exit == "TIME" and value > credit)
            if wants_roll and roll_ctx is not None:
                short, long_ = pos.legs[0], pos.legs[1]
                held = SpreadPosition(sym, short.right, short.strike, long_.strike, expiry, credit, short.lot_size, short.lots)
                choice = roll_choices(strategy, roll_ctx(), held, value, pos.roll_no, pos.realised, unlimited=never_stop)
                pos.pending_roll, pos.roll_refused = choice["roll_signal"], choice["refused_by"] or ""
                if pos.pending_roll is not None:
                    refused = self._roll_gate(sym, d, pos.pending_roll, pos)
                    if refused:
                        pos.pending_roll, pos.roll_refused = None, refused
            return ("hold",)
        if len(pos.legs) > 1:
            # A debit spread (bull call spread): judged on the closing value of its legs (bought minus
            # sold), like a credit spread; the exit is filled at the next session's open.
            basis = "rule at the close"
            if d >= expiry or nxt is None or nxt > expiry:
                return ("trade", self._close(pos, sym, d, closes, False, "EXPIRY_EXIT", strategy, exit_check, basis))
            if d >= last_allowed:
                return ("trade", self._close(pos, sym, d, closes, False, self._cutoff_reason(strategy), strategy, exit_check, basis))
            worth = sum(b.close * (1 if leg.action == "BUY" else -1) for leg, b in zip(pos.legs, bars))
            if worth >= sig.target:
                pos.pending_exit, pos.pending_basis = "TARGET", "daily close"
            elif worth <= sig.stop:
                pos.pending_exit, pos.pending_basis = "STOP", "daily close"
            elif (expiry - nxt).days <= exit_days:
                pos.pending_exit, pos.pending_basis = "TIME", basis
            return ("hold",)
        b = bars[0]
        hit = Engine._hit("LONG", Bar(sym, daily_start(d), b.open, b.high, b.low, b.close), sig.stop, sig.target,
                          i == pos.entry_i)
        if hit:
            return ("trade", self._close(pos, sym, d, [hit[1]], False, hit[0], strategy))
        if nxt is None or (expiry - nxt).days < exit_days or d >= expiry:
            return ("trade", self._close(pos, sym, d, closes, False, "EXPIRY_EXIT", strategy))
        if i - pos.entry_i + 1 >= strategy.params.max_hold_days:
            return ("trade", self._close(pos, sym, d, closes, False, "TIME", strategy))
        if d >= last_allowed:
            return ("trade", self._close(pos, sym, d, closes, False, self._cutoff_reason(strategy), strategy))
        return ("hold",)

    # ---- the baseline's fixed schedule ----
    def baseline_schedule(self, strategy: OptionStrategy, sym: str) -> dict[date, list]:
        """scheduled session -> what is attempted on it, fixed in advance from the calendar alone.
        Spreads and condors: each expiry of an allowed kind, on the first market session on which its
        days to expiry are inside the entry window. Option buying: None (the nearest valid expiry) on
        the first market session of each month."""
        dates = self.base.session_dates()
        out: dict[date, list] = defaultdict(list)
        if strategy.id == "long_option":
            seen: set[tuple[int, int]] = set()
            for d in dates:
                if (d.year, d.month) not in seen:
                    seen.add((d.year, d.month))
                    out[d].append(None)
            return dict(out)
        p = strategy.params
        kinds = getattr(strategy, "expiry_kinds", ("monthly",))
        for e, kind in self.expiries(sym):
            if kind not in kinds:
                continue
            i = bisect.bisect_left(dates, e - timedelta(days=p.entry_max_dte))
            if i < len(dates) and (e - dates[i]).days >= p.entry_min_dte:
                out[dates[i]].append((e, kind))
        return dict(out)

    # ---- one stream: one strategy (or one of its baselines) on one underlying ----
    # ---- portfolio gate (phase 9): set by run_portfolio while streams run side by side ----
    _gate = None                         # gate(sym, day, signal, replacing position or None) -> refusing code or None
    _book = None                         # stream key -> the signals open or pending in that stream

    @staticmethod
    def _cutoff_reason(strategy) -> str:
        """Phase 9 strategies report a position still open at the end of the tested period as open (a mark);
        the phase 4 strategies keep closing it there (PERIOD_END)."""
        return OPEN_AT_CUTOFF if getattr(strategy, "report_open_at_cutoff", False) else "PERIOD_END"

    def _roll_gate(self, sym: str, d: date, new: Signal, pos: _Position, intraday: bool = False) -> str | None:
        """`intraday`: the roll is decided inside session d, before its close: the gate may then use
        completed sessions BEFORE d only (d's daily close has not happened)."""
        if self._gate is None:
            return None
        return self._gate(sym, d, new, pos, True) if intraday else self._gate(sym, d, new, pos)

    def run_stream(self, strategy: OptionStrategy, sym: str, sessions: list[date], variant: str,
                   skipped: Counter, schedule_log: list | None = None, exit_check: str = "daily",
                   record: list | None = None, replay: dict | None = None) -> list[OptionTrade]:
        """`record`: every entry signal is appended to it as (signal day, signal). `replay`:
        {signal day: [signals]} - these signals are entered instead of evaluating the strategy, so
        different exit policies can be compared on exactly the same entries."""
        g = self.iter_stream(strategy, sym, sessions, variant, skipped, schedule_log, exit_check, record, replay)
        while True:
            try:
                next(g)
            except StopIteration as stop:
                return stop.value

    def run_portfolio(self, strategy: OptionStrategy, streams: list[tuple[str, str]], sessions: list[date],
                      skipped: Counter, gate, exit_check: str = "daily", record: dict | None = None,
                      replay: dict | None = None, trace: list | None = None) -> list[OptionTrade]:
        """Several streams ((symbol, variant) pairs) run side by side as ONE queue of events in time
        order, so that the portfolio gate sees every stream's open and pending positions exactly as
        they stand at the moment of each decision - never another stock's later fills, exits, rolls
        or signals of the same session.

        Every stream announces the moment of its next step (iter_stream); the earliest step of all
        streams runs next. Steps at the SAME instant run in this documented order:
          1. DECIDE - decisions on a 5-minute bar's close and the management of open positions at the
             session's close (exits at the close, roll decisions);
          2. SIGNAL - new entry signals at the session's close;
          3. FILL   - fills at a bar's open (entries, exits, a roll's two spreads) and drops.
        A decision at a bar's close uses what is known at that instant, so it comes before a fill at
        the open of the bar that starts then. Within one of these steps, truly simultaneous events of
        different streams run in the order the streams are given (the caller passes the stocks most
        liquid first): when two stocks signal at the same close and only one fits the limits, the
        first stream gets it. Nothing else depends on the order of `streams`.

        `gate(sym, day, signal, replacing[, intraday])` returns the refusing code or None (a new entry
        is refused before it is taken; a roll's replacement likewise; `intraday` is passed as True for
        a roll decided inside the session). `record` / `replay`: per stream key, as in run_stream.
        `trace`: every step is appended to it as (moment, phase, stream key), in the order run."""
        import heapq
        if getattr(gate, "universe", ()) is None:          # the correlated groups: built over every tested stock
            gate.universe = sorted({s for s, _ in streams})
        self._gate, self._book = gate, {}
        gens, out = {}, []
        try:
            heap = []

            def advance(n: int, key) -> None:
                try:
                    at, phase = next(gens[key])
                except StopIteration as stop:
                    out.extend(stop.value)
                else:
                    heapq.heappush(heap, (at, phase, n, key))

            for n, key in enumerate(streams):
                gens[key] = self.iter_stream(strategy, key[0], sessions, key[1], skipped, None, exit_check,
                                             None if record is None else record.setdefault(key, []),
                                             None if replay is None else replay.get(key, {}), key)
                advance(n, key)
            while heap:
                at, phase, n, key = heapq.heappop(heap)
                if trace is not None:
                    trace.append((at, phase, key))
                advance(n, key)
        finally:
            self._gate, self._book = None, None
        out.sort(key=lambda t: (t.exit_time, t.symbol, t.variant))
        return out

    def book_holdings(self, exclude_key=None, exclude_sig: Signal | None = None) -> list:
        """The positions open or pending in every stream (portfolio.Holding), each on its own."""
        from .portfolio import Holding
        out = []
        for key, rows in (self._book or {}).items():
            for s, risk in rows:
                if exclude_sig is not None and s is exclude_sig:
                    continue
                out.append(Holding(s.symbol, risk))
        return out

    def iter_stream(self, strategy: OptionStrategy, sym: str, sessions: list[date], variant: str,
                    skipped: Counter, schedule_log: list | None = None, exit_check: str = "daily",
                    record: list | None = None, replay: dict | None = None, key=None):
        """run_stream as a generator. Before each step it yields (moment, phase): the time (UTC) of what it
        is about to do and DECIDE | SIGNAL | FILL, so that run_portfolio can run every stream's steps in
        time order (see run_portfolio for the order at one instant). Every step reads the portfolio as
        published by the steps before it. Returns the trades. Run alone or side by side."""
        key = key or (sym, variant)
        kind = self.kinds.get(sym, "stock")
        bars = self.base.daily(sym)
        # data-quality exclusion (app/data_quality.py): no entry in an excluded stock-year, for every variant alike
        sessions_set = set(sessions)
        allowed = sessions_set - self.no_entry.get(sym, set())
        last_allowed = max(sessions) if sessions else None
        brk = set(self.base.breaks(sym)) if kind == "stock" else set()
        is_long = strategy.id == "long_option"
        baseline = variant != "strategy"
        trig = self.trigger_strategy() if is_long and not baseline else None
        schedule = self.baseline_schedule(strategy, sym) if baseline else {}
        attempted: set[date] = set()
        log_rows = schedule_log if schedule_log is not None else []
        trades: list[OptionTrade] = []
        positions: list[_Position] = []
        pending: list[tuple[Signal, dict | None]] = []      # (signal, its schedule row for a baseline attempt)
        # Synchronized execution: accepted entries of this session still waiting for their first synchronized
        # bar. Each keeps its risk reservation in the portfolio book until it fills (the position then carries
        # the maximum loss recomputed at the fill) or is not made (the reservation is released).
        entering: list[Signal] = []
        used_expiries: set[date] = set()
        start = 0
        needs_daily = getattr(strategy, "needs_daily", False)

        alert_exits = getattr(strategy, "alert_exits", frozenset())
        # Synchronized 5-minute execution applies to credit spreads; other structures stay on daily bars.
        sync = exit_check == "intraday" and getattr(strategy, "structure", "") == "CREDIT_SPREAD"
        incomplete: set[str] = set()
        prev_snap = None

        def recent(i: int) -> tuple:
            # the strategy's daily history: adjusted for splits, bonuses and demergers known that day (raw
            # prices otherwise); a break an applied action explains no longer cuts it (app/corporate_actions.py)
            if not needs_daily:
                return ()
            adj = self.adjusted_history(sym)
            return adj.window(i, 1300) if adj is not None else tuple(bars[max(start, i - 1299):i + 1])

        def closed() -> tuple:
            if not needs_daily:
                return ()
            return tuple(ClosedTrade(datetime.fromisoformat(t.exit_time).astimezone(IST).date(), t.exit_reason == "STOP",
                                     t.net_pnl) for t in trades)

        def scheduled(d: date, item) -> dict:
            row = {"underlying": sym, "variant": variant, "scheduled_day": d.isoformat(),
                   "expiry": item[0].isoformat() if item else None, "month": d.strftime("%Y-%m"),
                   "result": None, "reason": None}
            log_rows.append(row)
            return row

        def est(s: Signal) -> float:
            return float(s.details.get("max_loss_inr") or 0.0)

        def publish() -> None:
            # open positions carry their maximum loss recomputed at the actual fill; an accepted entry that has
            # not filled yet (pending, or waiting for its first synchronized bar) reserves its estimate
            if self._book is not None:
                self._book[key] = [(p.sig, p.max_loss_inr if p.max_loss_inr is not None else est(p.sig)) for p in positions] \
                    + [(p.pending_roll, est(p.pending_roll)) for p in positions if p.pending_roll] \
                    + [(s, est(s)) for s, _ in pending] + [(s, est(s)) for s in entering]

        for i, bar in enumerate(bars):
            d = bar_date(bar)
            if last_allowed is None or d > last_allowed:
                break
            t_open, t_close = daily_start(d), session_end(d)
            publish()
            yield (t_open, FILL)
            gap = i > 0 and self.base.sessions_between(bar_date(bars[i - 1]), d) > 0
            if i in brk:
                start = i
                skipped["DROPPED_DATA_BREAK_DURING_TRADE"] += len(positions)
                positions = []
                for _, row in pending:
                    if row is not None:
                        row.update(result="skipped", reason="DATA_BREAK_AT_ENTRY")
                pending = []
            if positions and gap:
                skipped["DROPPED_MISSING_DAILY_BAR_DURING_TRADE"] += len(positions)
                positions = []
            if gap:                                           # the signal's next bar is not the next session
                for _, row in pending:
                    skipped["NEXT_BAR_NOT_NEXT_SESSION"] += 1
                    if row is not None:
                        row.update(result="skipped", reason="NEXT_BAR_NOT_NEXT_SESSION")
                pending = []
            # what the session's close shows (used only by the rules decided at the close)
            alerts = ()
            if alert_exits:
                snap = self.snapshot_at(sym, recent(i), d)
                if positions or pending:
                    alerts = tuple(tuple(a["kind"] for a in alert_kinds(prev_snap, snap, right))
                                   for right in ("PE", "CE"))
                prev_snap = snap
            ctx_now = lambda: self.context(strategy, sym, d, bar.close, variant, None, recent(i), closed())
            ctx_prev = lambda: (self.context(strategy, sym, bar_date(bars[i - 1]), bars[i - 1].close, variant, None,
                                             recent(i - 1), closed()) if i > start else None)
            if not sync:
                # ---- exits decided at the previous close, filled at this open ----
                keep: list[_Position] = []
                for pos in positions:
                    if not pos.pending_exit:
                        keep.append(pos)
                        continue
                    legs_bars = self._leg_bars(sym, pos.legs, d)
                    if legs_bars is None:
                        skipped["DROPPED_MISSING_OPTION_BAR"] += 1
                    else:
                        rolled = None
                        if pos.pending_roll is not None:          # close this spread and open the next at the same open
                            rolled = self._enter(pos.pending_roll, sym, d, i, variant, Counter())
                        done = self._close(pos, sym, d, [b.open for b in legs_bars], True,
                                           "ROLLED" if rolled is not None else pos.pending_exit, strategy, exit_check,
                                           pos.pending_basis)
                        if rolled is None and pos.pending_roll is not None:
                            done.roll_refused = "ROLL_NOT_FILLED"        # the replacement could not be entered at this open
                        elif rolled is None:
                            done.roll_refused = pos.roll_refused
                        trades.append(done)
                        if rolled is not None:
                            rolled.campaign_id, rolled.roll_no = done.campaign_id, pos.roll_no + 1
                            rolled.realised = pos.realised + done.net_pnl
                            keep.append(rolled)
                positions = keep
                # ---- entries from the previous close's signals, at this open ----
                for sig, row in pending:
                    before = Counter(skipped)
                    pos = self._enter(sig, sym, d, i, variant, skipped)
                    if pos is not None:
                        positions.append(pos)
                    if row is not None:
                        why = sorted((skipped - before).keys())
                        row.update(result="entered" if pos is not None else "skipped",
                                   reason=None if pos is not None else (why[0] if why else "NOT_ENTERED"))
                pending = []
                # ---- every open position at this close ----
                if positions:
                    publish()
                    yield (t_close, DECIDE)
                keep = []
                for pos in positions:
                    r = self._manage(pos, sym, d, i, last_allowed, strategy, skipped, exit_check, bar.close, alerts, ctx_now)
                    if r[0] == "trade":
                        trades.append(r[1])
                    elif r[0] == "hold":
                        keep.append(pos)
                positions = keep
            else:
                # ---- synchronized 5-minute execution: this stream's steps of the session, in time order ----
                # Each pending entry and each open position is a generator that announces the moment of its
                # next step; the earliest runs next (ties: decisions before fills, then the order started).
                # A step is run only after its moment has been yielded, so run_portfolio can run the other
                # stocks' earlier steps first.
                starting = [(self._sync_entry(sig, sym, d, i, variant, skipped, row), sig) for sig, row in pending]
                entering = [sig for sig, _ in pending]         # reserved until each fills or is not made
                starting += [(self._manage_sync(pos, sym, d, i, last_allowed, strategy, skipped, bar.close, alerts,
                                                ctx_now, ctx_prev), pos) for pos in positions]
                pending = []
                waiting: list = []
                order = 0
                while starting or waiting:
                    if starting:
                        gen, pos = starting.pop(0)             # runs to its first announced moment: reads its own bars only
                    else:
                        moment, _, gen, pos = heapq.heappop(waiting)
                        publish()
                        yield moment
                    try:
                        moment = next(gen)
                    except StopIteration as stop:
                        r = stop.value
                    else:
                        order += 1
                        heapq.heappush(waiting, (moment, order, gen, pos))
                        continue
                    if isinstance(pos, Signal):                # a pending entry: filled, or not made
                        entering = [s for s in entering if s is not pos]   # the position's own risk now, or released
                        if r is not None:
                            positions.append(r)
                            starting.append((self._manage_sync(r, sym, d, i, last_allowed, strategy, skipped, bar.close,
                                                               alerts, ctx_now, ctx_prev), r))
                    elif r[0] == "trade":
                        trades.append(r[1])
                        positions = [p for p in positions if p is not pos]
                        if r[2] is not None:                   # a replacement opened inside the session: manage its rest
                            positions.append(r[2])
                            starting.append((self._manage_sync(r[2], sym, d, i, last_allowed, strategy, skipped, bar.close,
                                                               alerts, ctx_now, ctx_prev), r[2]))
                    elif r[0] == "incomplete":
                        incomplete.add(pos.campaign_id or pos.sig.signal_id)
                        positions = [p for p in positions if p is not pos]
            for pos in positions:                                 # daily mark-to-market of what stays open
                self._mark(pos, sym, d, strategy, sync)
            publish()                                             # today's fills and exits, before any later decision
            if d not in allowed or d >= last_allowed:
                continue
            yield (t_close, SIGNAL)                               # new signals: after every stock's positions at this close
            if replay is not None:                               # the same entries as a recorded run
                pending += [(sig, None) for sig in replay.get(d, ())]
                continue
            # ---- baseline: the fixed schedule, one attempt each, never retried ----
            if baseline:
                for item in schedule.get(d, ()):
                    attempted.add(d)
                    row = scheduled(d, item)
                    ctx = self.context(strategy, sym, d, bar.close, variant, None, recent(i), closed())
                    if item is not None:
                        ctx = replace(ctx, expiries=tuple(x for x in ctx.expiries if x.expiry == item[0])
                                      or (ExpiryInfo(item[0], item[1], self.lot_size(sym)),))
                    out = strategy.evaluate(ctx)
                    if not out:
                        reasons = [r.split(":")[0] for r in strategy.last_no_trade] or ["NO_SIGNAL"]
                        for reason in reasons:
                            skipped["NO_SIGNAL:" + reason] += 1
                        row.update(result="skipped", reason=reasons[0])
                        continue
                    if self._gate is not None:
                        refused = self._gate(sym, d, out[0], None)
                        if refused:
                            skipped["NO_SIGNAL:" + refused] += 1
                            row.update(result="skipped", reason=refused)
                            continue
                    pending.append((out[0], row))
                    publish()
                    if record is not None:
                        record.append((d, out[0]))
                continue
            # ---- strategy: a new signal from the close of day d, only when flat ----
            if positions or pending:
                continue
            trigger = None
            if is_long:
                # The trigger strategy is a cash-equity rule set; an index is passed as kind "stock"
                # because here its signal is only a trigger for an option, never a cash trade.
                mctx = MarketContext(sym, "stock", close_utc(CASH_FNO, d),
                                     self.base.regime.at_close(d).regime, daily=tuple(bars[start:i + 1]),
                                     index_daily=self.base.index_daily(d) if trig.needs_index_daily else ())
                got = trig.evaluate(mctx)
                if not got:
                    continue
                trigger = got[0]
            ctx = self.context(strategy, sym, d, bar.close, variant, trigger, recent(i), closed())
            if not is_long:
                if not strategy.entry_checks(ctx):
                    continue
                strategy.last_no_trade = []
                exp = strategy.pick(ctx)
                if exp is None:
                    for reason in strategy.last_no_trade:        # an expiry outside the window is not a skipped signal
                        if not reason.startswith("NO_EXPIRY"):
                            skipped["NO_SIGNAL:" + reason.split(":")[0]] += 1
                    continue
                if exp.expiry in used_expiries:
                    continue
            out = strategy.evaluate(ctx)
            if not out:
                for reason in strategy.last_no_trade:
                    skipped["NO_SIGNAL:" + reason.split(":")[0]] += 1
                continue
            if self._gate is not None:
                refused = self._gate(sym, d, out[0], None)
                if refused:
                    skipped["NO_SIGNAL:" + refused] += 1
                    continue
            pending.append((out[0], None))
            publish()
            if record is not None:
                record.append((d, out[0]))
            if not is_long:
                used_expiries.add(out[0].legs[0].expiry)
        # a scheduled day inside the tested period on which the underlying has no bar: skipped, not retried
        blocked = self.no_entry.get(sym, set())
        for d, items in sorted(schedule.items()):
            if d in blocked and d in sessions_set and last_allowed is not None and d < last_allowed:
                for item in items:
                    scheduled(d, item).update(result="skipped", reason="EXCLUDED_STOCK_YEAR")
                    skipped["NO_SIGNAL:EXCLUDED_STOCK_YEAR"] += 1
            if d in allowed and last_allowed is not None and d < last_allowed and d not in attempted:
                for item in items:
                    scheduled(d, item).update(result="skipped", reason="NO_UNDERLYING_BAR_ON_SCHEDULED_DAY")
                    skipped["NO_SIGNAL:NO_UNDERLYING_BAR_ON_SCHEDULED_DAY"] += 1
        for _, row in pending:                                # signalled on the last bar: no next session to fill
            if row is not None:
                row.update(result="skipped", reason="NO_NEXT_BAR")
        if incomplete:                                        # data-incomplete campaigns are left out entirely
            skipped["DATA_INCOMPLETE_CAMPAIGNS"] += len(incomplete)
            trades = [t for t in trades if (t.campaign_id or t.signal_id) not in incomplete]
        if self._book is not None:
            self._book[key] = []
        return trades


# ---------------- report ----------------
def _by(trades: list[OptionTrade], key) -> dict[str, list[OptionTrade]]:
    out: dict[str, list[OptionTrade]] = defaultdict(list)
    for t in trades:
        out[key(t)].append(t)
    return dict(sorted(out.items()))


def _share(excluded: int, kept: int) -> float | None:
    """Excluded campaigns as a % of all campaigns that were entered (kept + excluded)."""
    return round(excluded / (excluded + kept) * 100, 1) if excluded + kept else None


def compare(strategy_stats: dict, baseline_stats: dict) -> dict:
    """Strategy against its no-filter baseline, both after costs, per trade (they trade different numbers of times)."""
    def avg(s):
        return round(s["net_pnl_inr"] / s["trades"], 2) if s["trades"] else None
    a, b = avg(strategy_stats), avg(baseline_stats)
    if not strategy_stats["trades"]:
        verdict = "no trades: no evidence"
    elif not baseline_stats["trades"]:
        verdict = "baseline has no trades: cannot compare"
    elif strategy_stats["net_pnl_inr"] <= 0 or a <= b:
        verdict = "no edge vs baseline"
    else:
        verdict = "beats baseline after costs"
    if strategy_stats["trades"] and strategy_stats["low_sample"]:
        verdict += " (LOW SAMPLE)"
    return {"strategy_avg_net_per_trade_inr": a, "baseline_avg_net_per_trade_inr": b,
            "strategy_net_inr": strategy_stats["net_pnl_inr"], "baseline_net_inr": baseline_stats["net_pnl_inr"],
            "strategy_profit_factor": strategy_stats["profit_factor"],
            "baseline_profit_factor": baseline_stats["profit_factor"], "verdict": verdict}


def baseline_variants(strategy: OptionStrategy) -> list[str]:
    if strategy.id == "reversal_put_spread":
        return ["baseline:PE"]
    return ["baseline:PE", "baseline:CE"] if strategy.id in ("credit_spread", "stock_option_selling") else ["baseline"]


def run_option_backtest(settings: Settings, store: Store, source: str, strategy: OptionStrategy, prices,
                        underlyings: list[str] | None = None, engine: OptionEngine | None = None,
                        save: bool = True, now: datetime | None = None,
                        exit_checks: tuple[str, ...] = ("daily",), first_session: date | None = None,
                        coverage_note: str = "", extra=None, gate_factory=None) -> dict:
    """`exit_checks`: how target and stop are checked while a spread is open. The first mode is the
    main result; the report also shows the strategy and its baseline under every other mode listed."""
    eng = engine or OptionEngine(settings, store, source, prices)
    eng.costs.require("options")
    now = now or utcnow()
    cfg, bt = settings.backtest, settings.options.backtest
    known = {i.symbol for i in settings.watchlist}
    syms = [u for u in (underlyings or bt.underlyings.get(strategy.id, [])) if u in known]
    sessions = eng.base.session_dates()
    dev, hold = dev_holdout(settings, sessions, "1day")
    first = max([x for x in (bt.start.get(strategy.id), first_session) if x is not None], default=None)
    if first is not None:
        dev = [d for d in dev if d >= first]
    cap = settings.account.capital_inr
    if dev and eng.last_session != dev[-1]:
        eng.last_session = dev[-1]
        eng._iv.clear()
    strategy.rejections.clear()
    notes_lot = {sym: eng.lot_size(sym) for sym in syms}
    by_mode: dict[str, tuple] = {}
    portfolio_stats: dict[str, dict] = {}
    for mode in exit_checks:
        m_trades: list[OptionTrade] = []
        m_base: list[OptionTrade] = []
        m_skipped, m_base_skipped = Counter(), Counter()
        m_log: list[dict] = []
        for sym in syms:
            if gate_factory is None:
                m_trades += eng.run_stream(strategy, sym, dev, "strategy", m_skipped, exit_check=mode)
            for v in baseline_variants(strategy):
                m_base += eng.run_stream(strategy, sym, dev, v, m_base_skipped, m_log, exit_check=mode)
        if gate_factory is not None:                     # the strategy's stocks side by side, under the portfolio limits
            gate = gate_factory(eng)
            m_trades = eng.run_portfolio(strategy, [(s, "strategy") for s in syms], dev, m_skipped, gate, mode)
            portfolio_stats[mode] = gate.summary()
        for ts in (m_trades, m_base):
            ts.sort(key=lambda t: (t.exit_time, t.symbol, t.variant))
        by_mode[mode] = (m_trades, m_base, m_skipped, m_base_skipped, m_log)
    trades, base_trades, skipped, base_skipped, schedule_log = by_mode[exit_checks[0]]
    overall, base_overall = option_stats(trades, cap, cfg.min_trades), option_stats(base_trades, cap, cfg.min_trades)
    both_ways = {}
    for mode, (mt, mb, ms, mbs, _) in by_mode.items():
        so, bo = option_stats(mt, cap, cfg.min_trades), option_stats(mb, cap, cfg.min_trades)
        both_ways[mode] = {
            "label": EXIT_CHECK_LABEL[mode], "strategy": so, "baseline": bo,
            "baseline_by_variant": {k: option_stats(v, cap, cfg.min_trades) for k, v in _by(mb, lambda t: t.variant).items()},
            "strategy_by_exit": {k: len(v) for k, v in _by(mt, lambda t: t.exit_reason).items()},
            "baseline_by_exit": {k: len(v) for k, v in _by(mb, lambda t: t.exit_reason).items()},
            "baseline_by_exit_basis": {k: len(v) for k, v in _by(mb, lambda t: t.exit_basis or "-").items()},
            # synchronized execution: campaigns left out because a session had no bar in which every leg traded
            # (or its 5-minute bars are not stored), and the sessions that caused it
            "data_incomplete": {"strategy": {k: ms.get(k, 0) for k in DATA_INCOMPLETE_KEYS},
                                "baseline": {k: mbs.get(k, 0) for k in DATA_INCOMPLETE_KEYS},
                                "strategy_share_pct": _share(ms.get("DATA_INCOMPLETE_CAMPAIGNS", 0), len(mt)),
                                "baseline_share_pct": _share(mbs.get("DATA_INCOMPLETE_CAMPAIGNS", 0), len(mb))},
            "comparison": compare(so, bo)}
    wf = []
    for w in windows(dev, cfg.window_sessions * 3, cfg.step_sessions * 3):
        ws = set(w)
        tw = [t for t in trades if datetime.fromisoformat(t.entry_time).astimezone(IST).date() in ws]
        if w:
            wf.append({"from": w[0].isoformat(), "to": w[-1].isoformat(), "sessions": len(w), **option_stats(tw, cap, cfg.min_trades)})
    label = eng.prices.label
    rep = {
        "kind": "option", "strategy_id": strategy.id, "strategy_version": strategy.version,
        "params": strategy.params.model_dump(), "allowed_regimes": sorted(strategy.allowed_regimes),
        "horizon": strategy.horizon, "cost_label": eng.costs.label, "costs": eng.costs.info(),
        "data_source": source,
        "data_note": "SIMULATED DATA - results mean nothing about the real market" if source == "sim" else "",
        "price_source": label, "price_source_label": PRICE_SOURCE_LABEL[label],
        "payoff_simulation": label == "model",
        "eligibility_label": ELIGIBILITY_LABEL, "eligibility_note": ELIGIBILITY_NOTE,
        "bypassed_checks": BYPASSED_CHECKS,
        "valuation_incomplete": {"strategy": skipped.get("OPEN_AT_CUTOFF_VALUATION_UNKNOWN", 0),
                                 "baseline": base_skipped.get("OPEN_AT_CUTOFF_VALUATION_UNKNOWN", 0)},
        "portfolio_limits": portfolio_stats or None,
        "exit_check": exit_checks[0], "exit_check_label": EXIT_CHECK_LABEL[exit_checks[0]],
        "exit_check_comparison": both_ways,
        "symbols": len(syms), "underlyings": syms, "lot_sizes": notes_lot,
        "period": {"from": dev[0].isoformat() if dev else None, "to": dev[-1].isoformat() if dev else None,
                   "sessions": len(dev)},
        "capital_inr": cap,
        "overall": overall,
        "by_underlying": {k: option_stats(v, cap, cfg.min_trades) for k, v in _by(trades, lambda t: t.symbol).items()},
        "by_regime": {k: option_stats(v, cap, cfg.min_trades) for k, v in _by(trades, lambda t: t.regime).items()},
        "by_exit": {k: len(v) for k, v in _by(trades, lambda t: t.exit_reason).items()},
        "baseline": {
            "description": "the same structure (same strike rule, width, expiry choice, exits, costs and price "
                           "source) entered on a fixed schedule with no entry rules",
            "schedule": {
                "rule": ("one attempt on the first market session of each month" if strategy.id == "long_option" else
                         "one attempt per expiry, on the first market session on which the expiry is inside the entry "
                         "window") + "; a scheduled attempt that cannot be made or filled is skipped and never retried",
                "attempts": len(schedule_log),
                "entered": sum(1 for r in schedule_log if r["result"] == "entered"),
                "skipped": [r for r in schedule_log if r["result"] != "entered"],
            },
            "overall": base_overall,
            "by_variant": {k: option_stats(v, cap, cfg.min_trades) for k, v in _by(base_trades, lambda t: t.variant).items()},
            "by_underlying": {k: option_stats(v, cap, cfg.min_trades) for k, v in _by(base_trades, lambda t: t.symbol).items()},
            "by_exit": {k: len(v) for k, v in _by(base_trades, lambda t: t.exit_reason).items()},
            "skipped_signals": {k: v for k, v in base_skipped.items() if k not in DATA_EXCLUSION_KEYS},
            "data_exclusions": {k: base_skipped.get(k, 0) for k in DATA_EXCLUSION_KEYS},
        },
        "comparison": compare(overall, base_overall),
        "walk_forward": {"rules": "fixed (no parameters fitted); rolling test windows over the development period",
                         "window_sessions": cfg.window_sessions * 3, "step_sessions": cfg.step_sessions * 3,
                         "windows": wf, "profitable_windows": sum(1 for w in wf if w["net_pnl_inr"] > 0),
                         "windows_with_trades": sum(1 for w in wf if w["trades"] > 0)},
        "holdout": {"from": hold[0].isoformat() if hold else None, "to": hold[-1].isoformat() if hold else None,
                    "sessions": len(hold), "evaluated": False,
                    "result": "untouched - option backtests do not open the holdout",
                    "skipped_signals": None, "data_exclusions": None},
        "skipped_signals": {k: v for k, v in skipped.items() if k not in DATA_EXCLUSION_KEYS},
        "data_exclusions": {k: skipped.get(k, 0) for k in DATA_EXCLUSION_KEYS},
        "filtered_signals": {},
        "strategy_status": {"alerts": strategy.alerts_enabled, "mode": strategy.mode, "label": strategy.status_label},
        "download": {},
        "notes": [
            f"Price source: {PRICE_SOURCE_LABEL[label]}.",
            "Signals come from a day's close; entries fill at the next session's open of every leg plus slippage.",
            "Spread exits: " + EXIT_CHECK_LABEL[exit_checks[0]] + "; no position is held to settlement.",
            "Every trade is 1 lot at the lot size in today's instrument list (past lot sizes are not in the list); "
            "position sizing against capital is phase 5.",
            "The highest-OI strike rule is not applied in backtests: history has open interest per contract, not across the chain.",
            ELIGIBILITY_NOTE,
            "Baseline: one fixed scheduled attempt per expiry (or month); a skipped attempt is never retried later.",
            "A trade is dropped if a leg or the underlying has no bar for a session while it is open (data_exclusions).",
        ] + ([coverage_note] if coverage_note else []),
    }
    if extra is not None:                    # further tables computed on the same engine, sessions and stocks
        rep.update(extra(eng, syms, dev, by_mode))
    # after every table: whatever any of them wanted and did not find makes the report INCOMPLETE
    download = dict(eng.prices.download_stats())
    if eng.intraday is not None:
        download["intraday"] = eng.intraday.download_stats()
        download["contracts_wanted_but_not_stored"] = (download.get("contracts_wanted_but_not_stored", 0)
                                                       + download["intraday"]["contracts_wanted_but_not_stored"])
    rep["download"] = download
    run_id = f"{strategy.id}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    rep["run_id"], rep["created_utc"] = run_id, now.isoformat()
    all_trades = [asdict(t) for mode in exit_checks for part in by_mode[mode][:2] for t in part]
    if save:
        store.save_backtest(run_id, now, strategy.id, strategy.version, rep["params"], rep, all_trades)
        out = settings.path(settings.backtest.reports_dir)
        out.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"report": rep, "trades": all_trades}, indent=2, default=str)
        (out / f"{run_id}.json").write_text(payload, encoding="utf-8")
        (out / f"{strategy.id}_latest.json").write_text(payload, encoding="utf-8")
    return {"report": rep, "trades": all_trades}


# ---------------- price sources ----------------
def model_prices(settings: Settings, engine: Engine) -> ModelPrices:
    """Black-Scholes prices for the sim source (payoff simulation), with a FAKE expiry calendar."""
    dates = engine.session_dates()
    ref = {bar_date(b): b.close for b in engine.daily(settings.options.iv_reference_symbol)}
    cal = engine.calendar

    def expiries(sym: str):
        if not dates:
            return []
        cfg = settings.option_underlying(sym)
        return simulated_expiries(dates[0], dates[-1] + timedelta(days=70), cal.is_trading_day,
                                  bool(cfg and "weekly" in cfg.expiries))

    def div(sym: str) -> float:
        cfg = settings.option_underlying(sym)
        return (cfg.dividend_yield_pct if cfg else 0.0) / 100.0

    return ModelPrices(engine.daily, ref, settings.options.risk_free_rate_pct / 100.0, expiries, div)


def prepare_history(settings: Settings, store: Store, engine: Engine, hist: OptionHistory,
                    strategies: list[OptionStrategy], underlyings: list[str] | None) -> list[dict]:
    """Before a breeze backtest with --download: find the expiry dates the strategies need."""
    bt = settings.options.backtest
    sessions = engine.session_dates()
    dev, _ = dev_holdout(settings, sessions, "1day")
    if not dev:
        return []
    idx = settings.regime.index_symbol
    need: dict[tuple[str, str], date] = {}
    for st in strategies:
        start = max(dev[0], bt.start.get(st.id, dev[0]))
        for sym in (underlyings or bt.underlyings.get(st.id, [])):
            kind = next((i.kind for i in settings.watchlist if i.symbol == sym), None)
            if kind is None:
                continue
            key = (idx, "monthly") if kind == "stock" else (sym, "weekly" if st.id == "iron_condor" else "monthly")
            need[key] = min(need.get(key, start), start)
    out = []
    for (sym, kind), start in sorted(need.items(), key=lambda kv: kv[0][1]):     # monthly before weekly
        bars = engine.daily(sym)
        dates = [bar_date(b) for b in bars]

        def spot_before(d: date, bars=bars, dates=dates):
            i = bisect.bisect_left(dates, d - timedelta(days=4)) - 1
            return bars[i].close if i >= 0 and (d - dates[i]).days <= 14 else None

        step = settings.option_underlying(sym).backtest_strike_step
        end = dev[-1] + timedelta(days=45 if kind == "monthly" else 10)
        end = min(end, utcnow().astimezone(IST).date() - timedelta(days=1))
        out.append(discover_expiries(hist, sym, kind, start, end, spot_before, step))
    return out


def print_report(r: dict, source: str) -> None:
    o, b, c = r["overall"], r["baseline"]["overall"], r["comparison"]
    print(f"\n{r['strategy_id']} {r['strategy_version']}  [{r['cost_label']}]  source={source}")
    print(f"  PRICE SOURCE: {r['price_source_label']}")
    print(f"  NOTE: {r['eligibility_label']}")
    print(f"  EXITS: {r['exit_check_label']}")
    print(f"  underlyings {r['underlyings']}  lot sizes {r['lot_sizes']}")
    print(f"  period {r['period']['from']} .. {r['period']['to']} ({r['period']['sessions']} sessions); holdout untouched")
    for name, x in (("strategy", o), ("baseline", b)):
        print(f"  {name:9s} trades {x['trades']:4d}  win {x['win_rate_pct']}%  net Rs {x['net_pnl_inr']:>12,.2f}  "
              f"costs Rs {x['costs_inr']:>10,.2f}  max DD Rs {x['max_drawdown_inr']:>10,.2f}  PF {x['profit_factor']}  "
              f"{x['sample_note']}")
    for v, x in r["baseline"]["by_variant"].items():
        print(f"    {v:12s} trades {x['trades']:4d}  win {x['win_rate_pct']}%  net Rs {x['net_pnl_inr']:>12,.2f}  PF {x['profit_factor']}")
    print(f"  per trade after costs: strategy Rs {c['strategy_avg_net_per_trade_inr']}, baseline Rs "
          f"{c['baseline_avg_net_per_trade_inr']}  ->  {c['verdict'].upper()}")
    if len(r["exit_check_comparison"]) > 1:
        print("  both ways (how target and stop are checked):")
        for mode, x in r["exit_check_comparison"].items():
            for name in ("strategy", "baseline"):
                s = x[name]
                per = round(s["net_pnl_inr"] / s["trades"], 2) if s["trades"] else None
                print(f"    {mode:8s} {name:9s} trades {s['trades']:4d}  win {s['win_rate_pct']}%  net Rs "
                      f"{s['net_pnl_inr']:>12,.2f}  PF {s['profit_factor']}  per trade Rs {per}")
            print(f"    {mode:8s} baseline exits {x['baseline_by_exit']}  decided on {x['baseline_by_exit_basis']}")
    print(f"  strategy skipped {r['skipped_signals']}  exclusions {r['data_exclusions']}")
    print(f"  baseline skipped {r['baseline']['skipped_signals']}  exclusions {r['baseline']['data_exclusions']}")
    print(f"  exits: strategy {r['by_exit']}  baseline {r['baseline']['by_exit']}")
    sch = r["baseline"]["schedule"]
    print(f"  baseline schedule: {sch['attempts']} attempts, {sch['entered']} entered, {len(sch['skipped'])} skipped "
          f"{dict(Counter(x['reason'] for x in sch['skipped']))}")
    if r["download"].get("contracts_wanted_but_not_stored"):
        print(f"  INCOMPLETE: {r['download']['contracts_wanted_but_not_stored']} contracts wanted but not stored")
    print(f"  saved {r['run_id']}")


def main(argv: list[str] | None = None) -> int:
    from ..config import ROOT, load_settings
    ap = argparse.ArgumentParser(description="Option strategy backtests with a no-filter baseline")
    ap.add_argument("--strategy", nargs="*", help="option strategy ids (default: all)")
    ap.add_argument("--underlyings", nargs="*", help="underlyings (default: options.backtest.underlyings)")
    ap.add_argument("--source", choices=["breeze", "sim"], default="breeze")
    ap.add_argument("--download", action="store_true",
                    help="breeze: fetch missing option contracts and expiry dates from Breeze (read-only, cached)")
    ap.add_argument("--max-requests", type=int, help="with --download: request limit for this run")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    live = args.source == "breeze"
    store = Store(settings.path(settings.storage.db_path_live if live else settings.storage.db_path_sim), live=live)
    base = Engine(settings, store, args.source, calendar=load_calendar(settings, store))
    strategies = build_option_strategies(settings, args.strategy)
    if live:
        fetcher = None
        if args.download:
            from dotenv import load_dotenv

            from ..feeds.breeze_feed import BreezeFeed
            from .instruments import refresh_from_broker
            load_dotenv(ROOT / ".env")
            n = refresh_from_broker(store, settings.watchlist)
            print(f"instrument list refreshed: {n} contract expiries stored")
            fetcher = budgeted_fetcher(
                settings, store, BreezeFeed(settings.watchlist),
                args.max_requests if args.max_requests is not None else settings.options.backtest.max_requests_per_run)
            print(f"request budget: {fetcher.max_requests} requests may be sent in this run "
                  f"(daily budget {settings.options.backtest.daily_request_budget})")
        prices = OptionHistory(store, "breeze", fetcher)
        if args.download:
            for r in prepare_history(settings, store, base, prices, strategies, args.underlyings):
                print(f"expiries {r['underlying']} {r['kind']}: found {r['found']} new, not found {r['not_found']}"
                      + (f", stopped: {r['stopped']}" if r["stopped"] else ""))
    else:
        prices = model_prices(settings, base)
        book = InstrumentBook(store, "sim", max_age_days=36500)
        if not book.underlyings():
            from .instruments import sim_specs
            last = {i.symbol: (base.daily(i.symbol)[-1].close if base.daily(i.symbol) else None) for i in settings.watchlist}
            today = utcnow().astimezone(IST).date()
            book.save(sim_specs({k: v for k, v in last.items() if v}, today, base.calendar, utcnow(),
                                {s for s, c in settings.options.underlyings.items() if "weekly" in c.expiries}))
    eng = OptionEngine(settings, store, args.source, prices, base)
    for st in strategies:
        res = run_option_backtest(settings, store, args.source, st, prices, args.underlyings, engine=eng)
        print_report(res["report"], args.source)
    print(f"\ndownload: {prices.download_stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
