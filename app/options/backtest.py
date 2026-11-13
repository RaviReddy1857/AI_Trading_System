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
  list); sizing against capital is phase 5. One open position per underlying per stream, and one
  entry per expiry for spreads.
- Sessions before backtest.test_start (2019-01-22) are never tested; the holdout is not touched.

No-filter baseline: the same strategy code with its entry rules off (no regime, IV-rank, credit or
trigger rule), entered on a fixed schedule: the first session inside the entry window of every expiry
(spreads and condors), or the first session of every month (option buying). Same strike rule, width,
expiry choice, exits, costs and price source. A credit spread has two baselines (bull put, bear call).

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
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from ..backtest import Engine, dev_holdout, load_calendar, stats, windows
from ..config import Settings
from ..costs import CostBreakdown, money
from ..history import bar_date, daily_start
from ..models import Bar, Signal, utcnow
from ..quality import IST
from ..store import Store
from ..strategies import MarketContext, build_strategies
from . import build_option_strategies
from .base import ExpiryInfo, OptionContext, OptionQuote, OptionStrategy, add_costs
from .greeks import iv_rank
from .instruments import InstrumentBook
from .option_history import (BreezeOptionFetcher, ModelPrices, OptionHistory, atm_candidates, atm_iv_series,
                             discover_expiries, simulated_expiries)

log = logging.getLogger("option_backtest")

PRICE_SOURCE_LABEL = {
    "option_history": "stored Breeze option prices (traded prices, daily bars; no bid/ask in history)",
    "model": "payoff simulation (Black-Scholes model prices, NOT market prices)",
}
DATA_EXCLUSION_KEYS = ("DROPPED_MISSING_OPTION_BAR", "DROPPED_MISSING_DAILY_BAR_DURING_TRADE",
                       "DROPPED_DATA_BREAK_DURING_TRADE", "NO_OPTION_BAR_AT_ENTRY")


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


class OptionEngine:
    def __init__(self, settings: Settings, store: Store, source: str, prices, base: Engine | None = None,
                 lot_sizes: dict[str, int] | None = None):
        self.s, self.store, self.source, self.prices = settings, store, source, prices
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
        self._trigger = None

    # ---- inputs ----
    def lot_size(self, sym: str) -> int | None:
        return self._lots.get(sym)

    def expiries(self, sym: str) -> list[tuple[date, str]]:
        """Stored expiries. Stock monthly contracts expire with the index monthly contract, so a stock
        uses the index's monthly dates (a wrong date simply has no data and the trade is skipped)."""
        if self.kinds.get(sym) == "stock":
            return [(e, k) for e, k in self.prices.expiries(self.s.regime.index_symbol) if k == "monthly"]
        return self.prices.expiries(sym)

    def iv_series(self, sym: str) -> tuple[list[date], list[float]]:
        """Implied-volatility measure by date: India VIX for an index; for a stock, the implied
        volatility of the stored at-the-money calls struck about 35 and 15 days before each monthly
        expiry (a fixed set, so the series does not depend on what else is stored)."""
        if sym not in self._iv:
            if self.kinds.get(sym) == "index":
                series = self._ref_iv
            elif self.prices.label == "model":
                series = self._ref_iv
            else:
                bars = self.base.daily(sym)
                closes = {bar_date(b): b.close for b in bars}
                dates = sorted(closes)
                cfg = self.s.option_underlying(sym)
                step = cfg.backtest_strike_step if cfg else None
                wanted = {}
                for e, _ in self.expiries(sym):
                    for back in (35, 15):
                        i = bisect.bisect_right(dates, e - timedelta(days=back)) - 1
                        if i < 0 or (e - timedelta(days=back) - dates[i]).days > 7:
                            continue
                        for k in atm_candidates(closes[dates[i]], step):
                            c = self.prices.contract(sym, e, k, "CE")
                            if c is None:
                                break
                            if c:
                                wanted[(e, k, "CE")] = c
                                break
                series = atm_iv_series(wanted, closes, self.rate, (cfg.dividend_yield_pct if cfg else 0) / 100.0)
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

    def trigger_strategy(self):
        if self._trigger is None:
            self._trigger = build_strategies(self.s, [self.o.backtest.trigger])[0]
        return self._trigger

    def context(self, strategy: OptionStrategy, sym: str, d: date, spot: float, variant: str,
                trigger: Signal | None = None) -> OptionContext:
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
            no_filter=variant != "strategy", baseline_right=variant.split(":")[1] if ":" in variant else None)

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
               strategy: OptionStrategy) -> OptionTrade:
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
                             "entry_price": float(fill), "exit_price": float(out)})
        entry_net = sum((f * (1 if leg.action == "SELL" else -1) for leg, f in zip(pos.legs, pos.fills)), Decimal(0))
        sig = pos.sig
        t_exit = datetime.combine(d, time(9, 15) if at_open else time(15, 30), IST).astimezone(timezone.utc)
        return OptionTrade(
            strategy_id=strategy.id, strategy_version=strategy.version, variant=pos.variant, symbol=sym,
            structure=sig.details["structure"], expiry=pos.legs[0].expiry.isoformat(), legs=legs_out,
            lot_size=pos.legs[0].lot_size, lots=pos.legs[0].lots, signal_id=sig.signal_id,
            signal_time=sig.created_at.isoformat(), regime=sig.regime,
            entry_time=daily_start(pos.entry_d).isoformat(), entry_net=float(entry_net),
            exit_time=t_exit.isoformat(), exit_net=float(exit_net), exit_reason=reason,
            gross_pnl=float(gross), costs=total.as_dict(), net_pnl=float(gross - total.total),
            price_source=self.prices.label, max_loss_inr=sig.details.get("max_loss_inr"),
            iv_rank=sig.details.get("iv_rank"), why_now=list(sig.why_now))

    def _exit_days(self, strategy: OptionStrategy, sym: str) -> int:
        p = strategy.params
        if hasattr(p, "exit_days_before_expiry"):
            return p.exit_days_before_expiry
        return p.stock_exit_days_before_expiry if self.kinds.get(sym) == "stock" else p.index_exit_days_before_expiry

    def _manage(self, pos: _Position, sym: str, d: date, i: int, last_allowed: date, strategy: OptionStrategy,
                skipped: Counter):
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
                return ("trade", self._close(pos, sym, d, closes, False, "EXPIRY_EXIT", strategy))
            if d >= last_allowed:
                return ("trade", self._close(pos, sym, d, closes, False, "PERIOD_END", strategy))
            value = sum(b.close * (1 if leg.action == "SELL" else -1) for leg, b in zip(pos.legs, bars))
            if value <= sig.target:
                pos.pending_exit = "TARGET"
            elif value >= sig.stop:
                pos.pending_exit = "STOP"
            elif (expiry - nxt).days <= exit_days:
                pos.pending_exit = "TIME"
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

    # ---- one stream: one strategy (or one of its baselines) on one underlying ----
    def run_stream(self, strategy: OptionStrategy, sym: str, sessions: list[date], variant: str,
                   skipped: Counter) -> list[OptionTrade]:
        kind = self.kinds.get(sym, "stock")
        bars = self.base.daily(sym)
        allowed = set(sessions)
        last_allowed = max(sessions) if sessions else None
        brk = set(self.base.breaks(sym)) if kind == "stock" else set()
        is_long = strategy.id == "long_option"
        trig = self.trigger_strategy() if is_long and variant == "strategy" else None
        trades: list[OptionTrade] = []
        pos: _Position | None = None
        pending: Signal | None = None
        used_expiries: set[date] = set()
        used_months: set[tuple[int, int]] = set()
        start = 0
        for i, bar in enumerate(bars):
            d = bar_date(bar)
            if last_allowed is None or d > last_allowed:
                break
            gap = i > 0 and self.base.sessions_between(bar_date(bars[i - 1]), d) > 0
            if i in brk:
                start = i
                if pos is not None:
                    skipped["DROPPED_DATA_BREAK_DURING_TRADE"] += 1
                    pos = None
                pending = None
            if pos is not None and gap:
                skipped["DROPPED_MISSING_DAILY_BAR_DURING_TRADE"] += 1
                pos = None
            if pending is not None:
                sig, pending = pending, None
                if gap:
                    skipped["NEXT_BAR_NOT_NEXT_SESSION"] += 1
                else:
                    pos = self._enter(sig, sym, d, i, variant, skipped)
            elif pos is not None and pos.pending_exit:
                legs_bars = self._leg_bars(sym, pos.legs, d)
                if legs_bars is None:
                    skipped["DROPPED_MISSING_OPTION_BAR"] += 1
                else:
                    trades.append(self._close(pos, sym, d, [b.open for b in legs_bars], True, pos.pending_exit, strategy))
                pos = None
            if pos is not None:
                r = self._manage(pos, sym, d, i, last_allowed, strategy, skipped)
                if r[0] == "trade":
                    trades.append(r[1])
                    pos = None
                elif r[0] == "drop":
                    pos = None
            if pos is not None or d not in allowed or d >= last_allowed:
                continue
            # ---- a new signal from the close of day d ----
            trigger = None
            if is_long and variant == "strategy":
                # The trigger strategy is a cash-equity rule set; an index is passed as kind "stock"
                # because here its signal is only a trigger for an option, never a cash trade.
                mctx = MarketContext(sym, "stock", datetime.combine(d, time(15, 30), IST).astimezone(timezone.utc),
                                     self.base.regime.at_close(d).regime, daily=tuple(bars[start:i + 1]),
                                     index_daily=self.base.index_daily(d) if trig.needs_index_daily else ())
                got = trig.evaluate(mctx)
                if not got:
                    continue
                trigger = got[0]
            elif is_long:
                if (d.year, d.month) in used_months:
                    continue
            ctx = self.context(strategy, sym, d, bar.close, variant, trigger)
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
            if is_long and variant != "strategy":
                used_months.add((d.year, d.month))           # the fixed monthly date is used up, signal or not
            if not out:
                for reason in strategy.last_no_trade:
                    skipped["NO_SIGNAL:" + reason.split(":")[0]] += 1
                continue
            pending = out[0]
            if not is_long:
                used_expiries.add(pending.legs[0].expiry)
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
    return ["baseline:PE", "baseline:CE"] if strategy.id == "credit_spread" else ["baseline"]


def run_option_backtest(settings: Settings, store: Store, source: str, strategy: OptionStrategy, prices,
                        underlyings: list[str] | None = None, engine: OptionEngine | None = None,
                        save: bool = True, now: datetime | None = None) -> dict:
    eng = engine or OptionEngine(settings, store, source, prices)
    eng.costs.require("options")
    now = now or utcnow()
    cfg, bt = settings.backtest, settings.options.backtest
    known = {i.symbol for i in settings.watchlist}
    syms = [u for u in (underlyings or bt.underlyings.get(strategy.id, [])) if u in known]
    sessions = eng.base.session_dates()
    dev, hold = dev_holdout(settings, sessions, "1day")
    first = bt.start.get(strategy.id)
    if first is not None:
        dev = [d for d in dev if d >= first]
    cap = settings.account.capital_inr
    strategy.rejections.clear()
    trades: list[OptionTrade] = []
    base_trades: list[OptionTrade] = []
    skipped, base_skipped = Counter(), Counter()
    notes_lot = {}
    for sym in syms:
        notes_lot[sym] = eng.lot_size(sym)
        trades += eng.run_stream(strategy, sym, dev, "strategy", skipped)
        for v in baseline_variants(strategy):
            base_trades += eng.run_stream(strategy, sym, dev, v, base_skipped)
    for ts in (trades, base_trades):
        ts.sort(key=lambda t: (t.exit_time, t.symbol, t.variant))
    overall, base_overall = stats(trades, cap, cfg.min_trades), stats(base_trades, cap, cfg.min_trades)
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
        "download": eng.prices.download_stats(),
        "notes": [
            f"Price source: {PRICE_SOURCE_LABEL[label]}.",
            "Signals come from a day's close; entries fill at the next session's open of every leg plus slippage.",
            "Spread exits are decided on closing prices and filled at the next session's open; no position is held to settlement.",
            "Every trade is 1 lot at the lot size in today's instrument list (past lot sizes are not in the list); "
            "position sizing against capital is phase 5.",
            "The highest-OI strike rule is not applied in backtests: history has open interest per contract, not across the chain.",
            "Margin is not checked in backtests (past margins are unknown); live signals need a margin entry.",
            "A trade is dropped if a leg or the underlying has no bar for a session while it is open (data_exclusions).",
        ],
    }
    run_id = f"{strategy.id}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    rep["run_id"], rep["created_utc"] = run_id, now.isoformat()
    all_trades = [asdict(t) for t in trades] + [asdict(t) for t in base_trades]
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
            h = settings.history
            fetcher = BreezeOptionFetcher(
                BreezeFeed(settings.watchlist), {i.symbol: i.breeze_code for i in settings.watchlist},
                h.request_pause_seconds, h.request_timeout_seconds, 1, h.retry_pause_seconds,
                args.max_requests if args.max_requests is not None else settings.options.backtest.max_requests_per_run)
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
        r = res["report"]
        o, b, c = r["overall"], r["baseline"]["overall"], r["comparison"]
        print(f"\n{st.id} {st.version}  [{r['cost_label']}]  source={args.source}")
        print(f"  PRICE SOURCE: {r['price_source_label']}")
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
        print(f"  strategy skipped {r['skipped_signals']}  exclusions {r['data_exclusions']}")
        print(f"  baseline skipped {r['baseline']['skipped_signals']}  exclusions {r['baseline']['data_exclusions']}")
        print(f"  exits: strategy {r['by_exit']}  baseline {r['baseline']['by_exit']}")
        print(f"  saved {r['run_id']}")
    print(f"\ndownload: {prices.download_stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
