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
from ..regime import fii_bias
from ..store import Store
from ..strategies import MarketContext, build_strategies
from . import build_option_strategies
from .base import ExpiryInfo, OptionContext, OptionQuote, OptionStrategy, add_costs
from .greeks import iv_rank
from .payoff import UNHEDGED_SHORT, check_hedged
from .instruments import InstrumentBook
from .stock_profile import ClosedTrade, Snapshot, alert_kinds, snapshot
from .stock_selling import SpreadPosition, roll_choices
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
IV_EXPIRY_REACH_DAYS = 75     # stock IV history: an at-the-money call is used at most 70 days before its expiry
EXIT_CHECK_LABEL = {
    "daily": "target and stop checked on the legs' daily closes, filled at the next session's open",
    "intraday": "target and stop checked on the legs' 5-minute bars, filled at the next 5-minute bar's open "
                "(the daily close is the fallback when a day's 5-minute bars are not stored)",
}
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
    exit_reason: str              # TARGET | STOP | TIME | EXPIRY_EXIT | PERIOD_END
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
    exit_check: str = "daily"     # daily | intraday: how target and stop were checked for this trade
    exit_basis: str = ""          # what decided this exit: 5-minute bar | daily close | rule at the close


def intraday_spread_exit(leg_bars: list[list], signs: list[int], target: float, stop: float,
                         max_age: timedelta):
    """Target and stop of a spread on its legs' 5-minute bars for ONE session.

    `leg_bars`: each leg's bars of the session, oldest first. `signs`: +1 for a sold leg, -1 for a bought
    one, so sum(sign x price) is the cost to buy the spread back. At the close of every 5-minute bar the
    cost is compared with the target (at or below) and the stop (at or above). A leg that did not trade
    in that bar is priced at its last traded close if that is at most `max_age` old; older than that the
    cost is unknown and nothing is decided. A decision is filled at the open of the NEXT 5-minute bar
    (never the same bar), each leg at that bar's open or, if it did not trade in it, at its last close
    within `max_age`.

    Returns (reason, fill_time, [fill price per leg]) when filled in this session, (reason, None, None)
    when decided but not fillable before the close (the caller fills it at the next session's open),
    or None when neither level was reached."""
    by_start = [{b.start: b for b in bars} for bars in leg_bars]
    times = sorted({b.start for bars in leg_bars for b in bars})

    def price(i: int, at: datetime, what: str) -> float | None:
        b = by_start[i].get(at)
        if b is not None:
            return b.open if what == "open" else b.close
        older = [x for x in leg_bars[i] if x.start < at and at - x.start <= max_age]
        return older[-1].close if older else None

    decision = None
    for at in times:
        if decision is not None:
            fills = [price(i, at, "open") for i in range(len(leg_bars))]
            if all(f is not None for f in fills):
                return decision, at, fills
            continue
        closes = [price(i, at, "close") for i in range(len(leg_bars))]
        if any(c is None for c in closes):
            continue
        cost = sum(s * c for s, c in zip(signs, closes))
        if cost <= target:
            decision = "TARGET"
        elif cost >= stop:
            decision = "STOP"
    return (decision, None, None) if decision else None


class HistoryQuotes:
    """Quotes at the close of one past day, from the price source. Last traded prices only; open
    interest is known per contract but not across the chain, so the highest-OI rule cannot be applied."""
    oi_available = False

    def __init__(self, prices, underlying: str, d: date, spot: float, step: float | None):
        self.prices, self.underlying, self.d, self.spot, self.step = prices, underlying, d, spot, step

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


class OptionEngine:
    def __init__(self, settings: Settings, store: Store, source: str, prices, base: Engine | None = None,
                 lot_sizes: dict[str, int] | None = None, intraday: OptionIntraday | None = None):
        self.s, self.store, self.source, self.prices = settings, store, source, prices
        self.intraday = intraday         # 5-minute option bars (exit_check "intraday"); None = not available
        self.max_leg_age = timedelta(minutes=settings.stock_option_selling.backtest.intraday_leg_max_age_minutes)
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
        self._reversal: dict[str, object] = {}
        self._trigger = None
        self.expiry_floor: date | None = None    # ignore expiries before this date (set_expiry_floor)
        # Last session a run tests. A stock's IV history then leaves out the at-the-money calls of
        # expiries more than IV_EXPIRY_REACH_DAYS after it: they cannot matter, and they would be
        # option prices of the holdout period.
        self.last_session: date | None = None

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
            self._market[d] = {"fii_bias": None if bias == "UNKNOWN" else bias,
                               "vix_change_pct": round((now / prev - 1) * 100, 2) if now and prev else None}
        return self._market[d]

    def reversal_scanner(self, sym: str):
        """The stock's reversal states for every day of its history (point in time; cached)."""
        if sym not in self._reversal:
            from .reversal import ReversalScanner
            sos = self.s.stock_option_selling
            floor = self.s.backtest.test_start.get("1day")       # post-2019 data only: older Breeze bars are unreliable
            bars = [b for b in self.base.daily(sym) if floor is None or bar_date(b) >= floor]
            breaks = gap_breaks([b.open for b in bars], [b.close for b in bars], self.s.history.max_overnight_gap_pct)
            self._reversal[sym] = ReversalScanner(bars, self.base.daily(self.s.regime.index_symbol), sos.reversal,
                                                  sos.trend_block.long_sma_days, breaks)
        return self._reversal[sym]

    def snapshot_at(self, sym: str, bars: tuple, d: date) -> Snapshot | None:
        sos = self.s.stock_option_selling
        m = self.market_at(d)
        return snapshot(bars, sos.support_resistance_lookback_days, sos.trend_block, m["fii_bias"], m["vix_change_pct"],
                        self.base.regime.at_close(d).regime)

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
        asof = datetime.combine(d, time(15, 30), IST).astimezone(timezone.utc)
        return OptionContext(
            underlying=sym, kind=kind, asof=asof, regime=self.base.regime.at_close(d).regime, spot=spot,
            expiries=exps, quotes=HistoryQuotes(self.prices, sym, d, spot, cfg.backtest_strike_step if cfg else None),
            costs=self.costs, capital_inr=self.s.account.capital_inr, rate=self.rate,
            dividend=(cfg.dividend_yield_pct if cfg else 0.0) / 100.0, iv_reference_pct=iv, iv_rank=rank,
            trigger=trigger, allowed_kinds=tuple(cfg.expiries) if cfg else ("monthly",), margin=None,
            require_margin=False, historical=True, max_spread_pct=self.o.chain.max_spread_pct,
            min_oi_lots=self.o.chain.min_open_interest_lots, search_steps=self.o.backtest.strike_search_steps,
            no_filter=variant != "strategy", baseline_right=variant.split(":")[1] if ":" in variant else None,
            daily=daily, index_daily=self.base.index_daily(d)[-400:] if daily else (),
            closed_trades=closed_trades,
            market={**self.market_at(d), "reversal": self.reversal_scanner(sym).info_on(d)} if daily else {})

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
        fills = [self.costs.fill_price("options", b.open, leg.action) for leg, b in zip(sig.legs, bars)]
        raw = sum((Decimal(str(b.open)) * (1 if leg.action == "SELL" else -1) for leg, b in zip(sig.legs, bars)), Decimal(0))
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
        return _Position(sig, list(sig.legs), fills, d, i, variant)

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
        t_exit = exit_dt or datetime.combine(d, time(9, 15) if at_open else time(15, 30), IST).astimezone(timezone.utc)
        return OptionTrade(
            strategy_id=strategy.id, strategy_version=strategy.version, variant=pos.variant, symbol=sym,
            structure=sig.details["structure"], expiry=pos.legs[0].expiry.isoformat(), legs=legs_out,
            lot_size=pos.legs[0].lot_size, lots=pos.legs[0].lots, signal_id=sig.signal_id,
            signal_time=sig.created_at.isoformat(), regime=sig.regime,
            entry_time=daily_start(pos.entry_d).isoformat(), entry_net=float(entry_net),
            exit_time=t_exit.isoformat(), exit_net=float(exit_net), exit_reason=reason,
            gross_pnl=float(gross), costs=total.as_dict(), net_pnl=float(gross - total.total),
            price_source=self.prices.label, max_loss_inr=sig.details.get("max_loss_inr"),
            iv_rank=sig.details.get("iv_rank"), why_now=list(sig.why_now),
            entry_conditions=dict(sig.details.get("entry_conditions") or {}),
            campaign_id=pos.campaign_id or sig.signal_id, roll_no=pos.roll_no,
            max_profit_inr=sig.details.get("max_profit_inr"), exit_check=exit_check, exit_basis=basis)

    def _exit_days(self, strategy: OptionStrategy, sym: str) -> int:
        p = strategy.params
        if hasattr(p, "exit_days_before_expiry"):
            return p.exit_days_before_expiry
        return p.stock_exit_days_before_expiry if self.kinds.get(sym) == "stock" else p.index_exit_days_before_expiry

    def _leg_sessions(self, sym: str, legs, d: date):
        """Each leg's 5-minute bars of session d, or None if any leg's are unknown."""
        if self.intraday is None:
            return None
        out = []
        for leg in legs:
            bars = self.intraday.session(sym, leg.expiry, leg.strike, leg.right, d)
            if bars is None:
                return None
            out.append(bars)
        return out

    def _manage(self, pos: _Position, sym: str, d: date, i: int, last_allowed: date, strategy: OptionStrategy,
                skipped: Counter, exit_check: str = "daily", spot: float | None = None, alerts: tuple = (),
                roll_ctx=None):
        """Position at the close of day d. Returns ('trade', OptionTrade) | ('drop',) | ('hold',)."""
        bars = self._leg_bars(sym, pos.legs, d)
        if bars is None:
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
            if exit_check == "intraday":
                sess = self._leg_sessions(sym, pos.legs, d)
                if sess is None:
                    skipped["INTRADAY_BARS_UNKNOWN_DAY_CHECKED_ON_DAILY_CLOSE"] += 1
                else:
                    r = intraday_spread_exit(sess, signs, sig.target, sig.stop, self.max_leg_age)
                    if r is not None and r[1] is not None:
                        return ("trade", self._close(pos, sym, d, r[2], False, r[0], strategy, exit_check,
                                                     "5-minute bar", exit_dt=r[1] ))
                    if r is not None:
                        pos.pending_exit, pos.pending_basis = r[0], "5-minute bar, filled at the next session's open"
                        return ("hold",)
            if d >= last_allowed:
                return ("trade", self._close(pos, sym, d, closes, False, "PERIOD_END", strategy, exit_check,
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
            elif (expiry - nxt).days <= exit_days:
                pos.pending_exit, pos.pending_basis = "TIME", "rule at the close"
            credit = float(sum((f * s for f, s in zip(pos.fills, signs)), Decimal(0)))
            wants_roll = (policy == "roll_rules" and pos.pending_exit in ("STOP", "STRIKE_BREACHED")) or \
                         (policy == "roll_always" and pos.pending_exit == "TIME" and value > credit)
            if wants_roll and roll_ctx is not None:
                short, long_ = pos.legs[0], pos.legs[1]
                held = SpreadPosition(sym, short.right, short.strike, long_.strike, expiry, credit, short.lot_size, short.lots)
                pos.pending_roll = roll_choices(strategy, roll_ctx(), held, value, pos.roll_no, pos.realised,
                                                unlimited=never_stop)["roll_signal"]
            return ("hold",)
        if len(pos.legs) > 1:
            # A debit spread (bull call spread): judged on the closing value of its legs (bought minus
            # sold), like a credit spread; the exit is filled at the next session's open.
            basis = "rule at the close"
            if d >= expiry or nxt is None or nxt > expiry:
                return ("trade", self._close(pos, sym, d, closes, False, "EXPIRY_EXIT", strategy, exit_check, basis))
            if d >= last_allowed:
                return ("trade", self._close(pos, sym, d, closes, False, "PERIOD_END", strategy, exit_check, basis))
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
            return ("trade", self._close(pos, sym, d, closes, False, "PERIOD_END", strategy))
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
    def run_stream(self, strategy: OptionStrategy, sym: str, sessions: list[date], variant: str,
                   skipped: Counter, schedule_log: list | None = None, exit_check: str = "daily",
                   record: list | None = None, replay: dict | None = None) -> list[OptionTrade]:
        """`record`: every entry signal is appended to it as (signal day, signal). `replay`:
        {signal day: [signals]} - these signals are entered instead of evaluating the strategy, so
        different exit policies can be compared on exactly the same entries."""
        kind = self.kinds.get(sym, "stock")
        bars = self.base.daily(sym)
        allowed = set(sessions)
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
        used_expiries: set[date] = set()
        start = 0
        needs_daily = getattr(strategy, "needs_daily", False)

        alert_exits = getattr(strategy, "alert_exits", frozenset())
        spike = self.s.stock_option_selling.profile.vix_spike_pct
        prev_snap = None

        def recent(i: int) -> tuple:
            return tuple(bars[max(start, i - 1299):i + 1]) if needs_daily else ()

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

        for i, bar in enumerate(bars):
            d = bar_date(bar)
            if last_allowed is None or d > last_allowed:
                break
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
                    if pos.pending_roll is not None and not gap:     # close this spread and open the next at the same open
                        rolled = self._enter(pos.pending_roll, sym, d, i, variant, Counter())
                    done = self._close(pos, sym, d, [b.open for b in legs_bars], True,
                                       "ROLLED" if rolled is not None else pos.pending_exit, strategy, exit_check,
                                       pos.pending_basis)
                    trades.append(done)
                    if rolled is not None:
                        rolled.campaign_id, rolled.roll_no = done.campaign_id, pos.roll_no + 1
                        rolled.realised = pos.realised + done.net_pnl
                        keep.append(rolled)
            positions = keep
            # ---- entries from the previous close's signals, at this open ----
            for sig, row in pending:
                before = Counter(skipped)
                pos = None
                if gap:
                    skipped["NEXT_BAR_NOT_NEXT_SESSION"] += 1
                else:
                    pos = self._enter(sig, sym, d, i, variant, skipped)
                if pos is not None:
                    positions.append(pos)
                if row is not None:
                    why = sorted((skipped - before).keys())
                    row.update(result="entered" if pos is not None else "skipped",
                               reason=None if pos is not None else (why[0] if why else "NOT_ENTERED"))
            pending = []
            # ---- every open position at this close ----
            keep = []
            alerts = ()
            if alert_exits:
                snap = self.snapshot_at(sym, recent(i), d)
                if positions:
                    alerts = tuple(tuple(a["kind"] for a in alert_kinds(prev_snap, snap, right, spike))
                                   for right in ("PE", "CE"))
                prev_snap = snap
            for pos in positions:
                r = self._manage(pos, sym, d, i, last_allowed, strategy, skipped, exit_check, bar.close, alerts,
                                 lambda: self.context(strategy, sym, d, bar.close, variant, None, recent(i), closed()))
                if r[0] == "trade":
                    trades.append(r[1])
                elif r[0] == "hold":
                    keep.append(pos)
            positions = keep
            if d not in allowed or d >= last_allowed:
                continue
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
                    pending.append((out[0], row))
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
                mctx = MarketContext(sym, "stock", datetime.combine(d, time(15, 30), IST).astimezone(timezone.utc),
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
            pending.append((out[0], None))
            if record is not None:
                record.append((d, out[0]))
            if not is_long:
                used_expiries.add(out[0].legs[0].expiry)
        # a scheduled day inside the tested period on which the underlying has no bar: skipped, not retried
        for d, items in sorted(schedule.items()):
            if d in allowed and last_allowed is not None and d < last_allowed and d not in attempted:
                for item in items:
                    scheduled(d, item).update(result="skipped", reason="NO_UNDERLYING_BAR_ON_SCHEDULED_DAY")
                    skipped["NO_SIGNAL:NO_UNDERLYING_BAR_ON_SCHEDULED_DAY"] += 1
        for _, row in pending:                                # signalled on the last bar: no next session to fill
            if row is not None:
                row.update(result="skipped", reason="NO_NEXT_BAR")
        return trades


# ---------------- report ----------------
def _by(trades: list[OptionTrade], key) -> dict[str, list[OptionTrade]]:
    out: dict[str, list[OptionTrade]] = defaultdict(list)
    for t in trades:
        out[key(t)].append(t)
    return dict(sorted(out.items()))


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
                        coverage_note: str = "", extra=None) -> dict:
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
    for mode in exit_checks:
        m_trades: list[OptionTrade] = []
        m_base: list[OptionTrade] = []
        m_skipped, m_base_skipped = Counter(), Counter()
        m_log: list[dict] = []
        for sym in syms:
            m_trades += eng.run_stream(strategy, sym, dev, "strategy", m_skipped, exit_check=mode)
            for v in baseline_variants(strategy):
                m_base += eng.run_stream(strategy, sym, dev, v, m_base_skipped, m_log, exit_check=mode)
        for ts in (m_trades, m_base):
            ts.sort(key=lambda t: (t.exit_time, t.symbol, t.variant))
        by_mode[mode] = (m_trades, m_base, m_skipped, m_base_skipped, m_log)
    trades, base_trades, skipped, base_skipped, schedule_log = by_mode[exit_checks[0]]
    overall, base_overall = stats(trades, cap, cfg.min_trades), stats(base_trades, cap, cfg.min_trades)
    both_ways = {}
    for mode, (mt, mb, ms, mbs, _) in by_mode.items():
        so, bo = stats(mt, cap, cfg.min_trades), stats(mb, cap, cfg.min_trades)
        both_ways[mode] = {
            "label": EXIT_CHECK_LABEL[mode], "strategy": so, "baseline": bo,
            "baseline_by_variant": {k: stats(v, cap, cfg.min_trades) for k, v in _by(mb, lambda t: t.variant).items()},
            "strategy_by_exit": {k: len(v) for k, v in _by(mt, lambda t: t.exit_reason).items()},
            "baseline_by_exit": {k: len(v) for k, v in _by(mb, lambda t: t.exit_reason).items()},
            "baseline_by_exit_basis": {k: len(v) for k, v in _by(mb, lambda t: t.exit_basis or "-").items()},
            "days_checked_on_daily_close_instead": ms.get("INTRADAY_BARS_UNKNOWN_DAY_CHECKED_ON_DAILY_CLOSE", 0)
            + mbs.get("INTRADAY_BARS_UNKNOWN_DAY_CHECKED_ON_DAILY_CLOSE", 0),
            "comparison": compare(so, bo)}
    wf = []
    for w in windows(dev, cfg.window_sessions * 3, cfg.step_sessions * 3):
        ws = set(w)
        tw = [t for t in trades if datetime.fromisoformat(t.entry_time).astimezone(IST).date() in ws]
        if w:
            wf.append({"from": w[0].isoformat(), "to": w[-1].isoformat(), "sessions": len(w), **stats(tw, cap, cfg.min_trades)})
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
        "exit_check": exit_checks[0], "exit_check_label": EXIT_CHECK_LABEL[exit_checks[0]],
        "exit_check_comparison": both_ways,
        "symbols": len(syms), "underlyings": syms, "lot_sizes": notes_lot,
        "period": {"from": dev[0].isoformat() if dev else None, "to": dev[-1].isoformat() if dev else None,
                   "sessions": len(dev)},
        "capital_inr": cap,
        "overall": overall,
        "by_underlying": {k: stats(v, cap, cfg.min_trades) for k, v in _by(trades, lambda t: t.symbol).items()},
        "by_regime": {k: stats(v, cap, cfg.min_trades) for k, v in _by(trades, lambda t: t.regime).items()},
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
            "by_variant": {k: stats(v, cap, cfg.min_trades) for k, v in _by(base_trades, lambda t: t.variant).items()},
            "by_underlying": {k: stats(v, cap, cfg.min_trades) for k, v in _by(base_trades, lambda t: t.symbol).items()},
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
