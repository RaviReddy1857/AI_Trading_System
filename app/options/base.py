"""Shared pieces of the option strategies: the context a strategy may see, option quotes, and the
strike-selection rules. The same code runs live (quotes from the stored option chain, with bid/ask)
and in backtests (quotes from stored option history: last traded prices, no bid/ask).

Rules every option strategy gets (OptionStrategy.evaluate):
- price status other than LIVE, or no usable spot price  -> no signal (DATA_STALE / DATA_MISSING)
- no lot size for the expiry in the instrument list      -> no signal (LOT_SIZE_UNKNOWN)
- live: a strike without both a bid and an ask is unusable -> no signal on it
- a spread whose margin is not known                      -> no signal (MARGIN_UNKNOWN)
- any sold option without its bought hedge                -> no signal (UNHEDGED_SHORT)
The reasons of the last evaluation are kept in `strategy.last_no_trade` for display.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from ..costs import CostBreakdown, CostModel
from ..models import Signal
from ..quality import IST
from ..strategies.base import _NS, Strategy, next_weekday_open_window
from .greeks import bs_delta, days_to_expiry, iv_and_delta, years_to_expiry
from .payoff import OptionLeg, check_hedged

SIGNAL_VALID_MINUTES = 15


@dataclass(frozen=True)
class OptionQuote:
    strike: float
    right: str                    # CE | PE
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    oi: float | None = None       # open interest in shares
    oi_change: float | None = None
    volume: float | None = None

    @property
    def has_bid_ask(self) -> bool:
        return _pos(self.bid) and _pos(self.ask) and self.ask >= self.bid

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2 if self.has_bid_ask else None

    @property
    def spread_pct(self) -> float | None:
        m = self.mid
        return (self.ask - self.bid) / m * 100 if m else None

    def price(self, historical: bool) -> float | None:
        """Price for IV and delta: the bid/ask mid. In backtests (history has trades only): the last trade."""
        if historical:
            return self.last if _pos(self.last) else None
        return self.mid

    def sell_price(self, historical: bool) -> float | None:
        return (self.last if _pos(self.last) else None) if historical else (self.bid if self.has_bid_ask else None)

    def buy_price(self, historical: bool) -> float | None:
        return (self.last if _pos(self.last) else None) if historical else (self.ask if self.has_bid_ask else None)


def _pos(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


@dataclass(frozen=True)
class ExpiryInfo:
    expiry: date
    kind: str                     # weekly | monthly
    lot_size: int | None          # None = not in the instrument list (LOT_SIZE_UNKNOWN)


class Quotes(Protocol):
    oi_available: bool            # True when open interest is known across the whole chain

    def strikes(self, expiry: date) -> Sequence[float]: ...
    def atm_candidates(self, expiry: date) -> Sequence[float]: ...
    def quote(self, expiry: date, strike: float, right: str) -> OptionQuote | None: ...
    def highest_oi_strike(self, expiry: date, right: str) -> float | None: ...


class ChainQuotes:
    """Quotes from stored option-chain snapshots (live use). `chains`: expiry -> list of OptionQuote."""
    oi_available = True

    def __init__(self, spot: float, chains: dict[date, list[OptionQuote]]):
        self.spot = spot
        self._q = {e: {(q.strike, q.right): q for q in rows} for e, rows in chains.items()}

    def strikes(self, expiry: date) -> list[float]:
        return sorted({k for k, _ in self._q.get(expiry, {})})

    def atm_candidates(self, expiry: date) -> list[float]:
        return sorted(self.strikes(expiry), key=lambda k: abs(k - self.spot))[:3]

    def quote(self, expiry: date, strike: float, right: str) -> OptionQuote | None:
        return self._q.get(expiry, {}).get((strike, right))

    def highest_oi_strike(self, expiry: date, right: str) -> float | None:
        rows = [q for q in self._q.get(expiry, {}).values() if q.right == right and _pos(q.oi)]
        return max(rows, key=lambda q: (q.oi, -abs(q.strike - self.spot))).strike if rows else None


@dataclass(frozen=True)
class OptionContext:
    """Everything an option strategy may look at for one underlying at one moment."""
    underlying: str
    kind: str                         # index | stock
    asof: datetime                    # UTC
    regime: str
    spot: float | None
    expiries: tuple[ExpiryInfo, ...]  # unexpired contracts, nearest first
    quotes: Quotes
    costs: CostModel
    capital_inr: float
    rate: float                       # risk-free rate, decimal
    dividend: float = 0.0             # dividend yield, decimal
    status: str = "LIVE"
    iv_reference_pct: float | None = None   # index: India VIX; stock: stored ATM implied volatility
    iv_rank: float | None = None      # 0..100 against the past year, None = unknown
    trigger: Signal | None = None     # long option: the underlying's own signal
    allowed_kinds: tuple[str, ...] = ("monthly",)
    margin: Callable[[str, float], float | None] | None = None   # (structure, width) -> rupees per lot
    require_margin: bool = True       # False in backtests: past margins are not known
    historical: bool = False          # True in backtests: last traded prices, no bid/ask
    max_spread_pct: float = 10.0
    min_oi_lots: int = 0
    search_steps: int = 8
    # No-filter baseline (backtests): the same structure with no entry rules.
    no_filter: bool = False
    baseline_right: str | None = None  # credit spread baseline: which side to sell (PE | CE)
    events: tuple = field(default_factory=tuple)   # manual_data.Event rows (results, board meetings, corporate actions)
    daily: tuple = field(default_factory=tuple)    # the underlying's daily bars up to asof (phase 9: trend, support, resistance)

    def __post_init__(self):
        # Margin and bid/ask are required for every live signal. Only a backtest context (historical:
        # traded prices, past margins unknown) may switch the margin rule off.
        if not self.historical and not self.require_margin:
            raise ValueError("a live option context must require margin (require_margin=False is for backtests only)")


class OptionStrategy(Strategy):
    """An option strategy uses the phase 3 interface (id, version from its thresholds, status,
    evaluate -> list[Signal]) with an OptionContext instead of bars."""
    interval = "1day"

    def __init__(self, params):
        super().__init__(params)
        self.last_no_trade: list[str] = []

    def no(self, code: str, text: str = "") -> list:
        self.last_no_trade.append(f"{code}: {text}" if text else code)
        self.rejections[code] += 1
        return []

    def evaluate(self, ctx: OptionContext) -> list[Signal]:      # type: ignore[override]
        self.last_no_trade = []
        if ctx.status != "LIVE":
            return self.no("DATA_STALE", f"{ctx.underlying} price status is {ctx.status}")
        if not _pos(ctx.spot):
            return self.no("DATA_MISSING", f"no usable {ctx.underlying} price")
        out = self._evaluate(ctx)
        for s in out:
            problems = check_hedged(s.legs or ())
            if problems:
                return self.no(problems[0], "a sold option has no bought hedge of the same expiry")
        return out

    # ---- helpers ----
    def option_signal(self, ctx: OptionContext, direction: str, horizon: str, entry: float, stop: float,
                      target: float, why: Sequence[str], legs: Sequence[OptionLeg], details: dict) -> Signal:
        key = "|".join([self.id, self.version, ctx.underlying, direction, ctx.asof.isoformat()]
                       + [f"{leg.action}{leg.right}{leg.strike:g}{leg.expiry}" for leg in legs])
        if ctx.historical:
            valid = next_weekday_open_window(ctx.asof.astimezone(IST).date())
        else:
            valid = ctx.asof + timedelta(minutes=SIGNAL_VALID_MINUTES)
        return Signal(
            signal_id=str(uuid.uuid5(_NS, key)), created_at=ctx.asof, strategy_id=self.id,
            strategy_version=self.version, symbol=ctx.underlying,
            asset_class="INDEX_OPTION" if ctx.kind == "index" else "STOCK_OPTION",
            direction=direction, horizon=horizon, entry=round(entry, 2), stop=round(stop, 2),
            target=round(target, 2), valid_until=valid, regime=ctx.regime, why_now=tuple(why),
            data_as_of=ctx.asof, legs=tuple(legs), details=details)


# ---------------- selection rules (pure) ----------------
def pick_expiry(ctx: OptionContext, min_dte: int, max_dte: int | None, kinds: Sequence[str] | None = None) -> ExpiryInfo | None:
    """The nearest expiry with min_dte <= calendar days to expiry <= max_dte, of an allowed kind."""
    for e in ctx.expiries:
        dte = days_to_expiry(ctx.asof, e.expiry)
        if dte < min_dte or (kinds is not None and e.kind not in kinds):
            continue
        if max_dte is not None and dte > max_dte:
            return None
        return e
    return None


def metrics(ctx: OptionContext, expiry: date, strike: float, right: str):
    """(quote, implied volatility, delta) of a strike from its own price, or None if any is unknown."""
    q = ctx.quotes.quote(expiry, strike, right)
    if q is None:
        return None
    iv, delta = iv_and_delta(q.price(ctx.historical), ctx.spot, strike, years_to_expiry(ctx.asof, expiry),
                             ctx.rate, ctx.dividend, right)
    return (q, iv, delta) if delta is not None else None


def otm_strikes(ctx: OptionContext, expiry: date, right: str) -> list[float]:
    """Out-of-the-money strikes, nearest the money first."""
    ks = ctx.quotes.strikes(expiry)
    return sorted((k for k in ks if k < ctx.spot), reverse=True) if right == "PE" \
        else sorted(k for k in ks if k > ctx.spot)


def pick_short_strike(ctx: OptionContext, expiry: date, right: str, max_delta: float):
    """The sold strike: the out-of-the-money strike nearest the money whose |delta| (from its own
    price) is at or below max_delta; if open interest is known across the chain, it must also lie
    beyond the highest-OI strike on that side (support for puts, resistance for calls).
    Returns (strike, quote, iv, delta, notes) or (None, reason)."""
    ks = otm_strikes(ctx, expiry, right)
    if not ks:
        return None, "no out-of-the-money strikes"
    t = years_to_expiry(ctx.asof, expiry)
    g = 0
    if _pos(ctx.iv_reference_pct) and t:
        g = len(ks) - 1
        for i, k in enumerate(ks):
            d = bs_delta(ctx.spot, k, t, ctx.rate, ctx.dividend, ctx.iv_reference_pct / 100.0, right)
            if d is not None and abs(d) <= max_delta:
                g = i
                break
    found = None
    m = metrics(ctx, expiry, ks[g], right)
    if m is not None and abs(m[2]) <= max_delta:
        i, found = g, m
        for _ in range(ctx.search_steps):                  # walk toward the money while the rule still holds
            if i == 0:
                break
            inner = metrics(ctx, expiry, ks[i - 1], right)
            if inner is None or abs(inner[2]) > max_delta:
                break
            i, found = i - 1, inner
    else:
        i = g
        for _ in range(ctx.search_steps):                  # walk away from the money until it holds
            i += 1
            if i >= len(ks):
                break
            m = metrics(ctx, expiry, ks[i], right)
            if m is not None and abs(m[2]) <= max_delta:
                found = m
                break
    if found is None:
        return None, f"no {right} strike with |delta| <= {max_delta:g} and a usable price"
    notes = []
    if ctx.quotes.oi_available:
        wall = ctx.quotes.highest_oi_strike(expiry, right)
        if wall is None:
            return None, "open interest unknown"
        beyond = (lambda k: k < wall) if right == "PE" else (lambda k: k > wall)
        if not beyond(ks[i]):
            found = None
            for j in range(i + 1, len(ks)):
                if beyond(ks[j]):
                    m = metrics(ctx, expiry, ks[j], right)
                    if m is not None and abs(m[2]) <= max_delta:
                        i, found = j, m
                    break
            if found is None:
                return None, f"no usable {right} strike beyond the highest-OI strike {wall:g}"
        notes.append(f"beyond the highest-OI {right} strike {wall:g}")
    else:
        notes.append("highest-OI rule not applied (chain-wide open interest is not available for this date)")
    q, iv, delta = found
    return ks[i], q, iv, delta, notes


def pick_hedge_strike(ctx: OptionContext, expiry: date, right: str, short_strike: float, width_pct: float):
    """The bought strike: same expiry and right, further out of the money, the listed strike nearest
    `width_pct` of spot beyond the sold strike. Returns (strike, quote) or None."""
    want = ctx.spot * width_pct / 100.0
    target = short_strike - want if right == "PE" else short_strike + want
    further = [k for k in ctx.quotes.strikes(expiry) if (k < short_strike if right == "PE" else k > short_strike)]
    for k in sorted(further, key=lambda k: (abs(k - target), abs(k - short_strike)))[:3]:
        q = ctx.quotes.quote(expiry, k, right)
        if q is not None and q.buy_price(ctx.historical) is not None:
            return k, q
    return None


def liquidity_problem(ctx: OptionContext, q: OptionQuote, lot_size: int) -> str | None:
    """Why a strike is too illiquid to trade, or None."""
    if not ctx.historical:
        if not q.has_bid_ask:
            return f"{q.strike:g}{q.right}: no bid/ask"
        if q.spread_pct > ctx.max_spread_pct:
            return f"{q.strike:g}{q.right}: bid-ask width {q.spread_pct:.1f}% of mid, above {ctx.max_spread_pct:g}%"
    if ctx.min_oi_lots and q.oi is not None and q.oi < ctx.min_oi_lots * lot_size:
        return f"{q.strike:g}{q.right}: open interest {q.oi / lot_size:,.0f} lots, below {ctx.min_oi_lots}"
    if ctx.min_oi_lots and q.oi is None and not ctx.historical:
        return f"{q.strike:g}{q.right}: open interest unknown"
    return None


def round_trip_costs(costs: CostModel, legs: Sequence[OptionLeg]) -> CostBreakdown:
    """Estimated charges to open and later close every leg at its current price (one lot each side)."""
    total = CostBreakdown()
    for leg in legs:
        value = leg.price * leg.lots * leg.lot_size
        add_costs(total, costs.options(leg.lots, value, value))
    return total


def add_costs(total: CostBreakdown, c: CostBreakdown) -> CostBreakdown:
    for k in ("brokerage", "stt", "exchange_txn", "sebi_fee", "ipft", "stamp_duty", "dp_charge", "gst"):
        setattr(total, k, getattr(total, k) + getattr(c, k))
    return total


def leg_view(leg: OptionLeg, iv: float | None, delta: float | None, q: OptionQuote) -> dict:
    return {**leg.to_dict(), "iv_pct": None if iv is None else round(iv * 100, 2),
            "delta": None if delta is None else round(delta, 3), "bid": q.bid, "ask": q.ask, "last": q.last,
            "open_interest": q.oi}
