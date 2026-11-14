"""Phase 9 (core): monthly hedged credit spreads on F&O stocks.

Up trend -> bull put spread (sell a put below support, buy a put further below).
Down trend -> bear call spread (sell a call above resistance, buy a call further above).

Entry rules (StockOptionSelling.assess; every number is computed here, none by an LLM):
- stocks only; the stock must have a clear trend by the swing trend rule (close above the slow average
  and the fast average above the slow one = up; the mirror image = down). No clear trend: no signal.
- IV rank known and at or above `min_iv_rank`.
- no results, board meeting or corporate action for the stock between today and expiry.
- expiry: the monthly contract `entry_min_days_to_expiry`..`entry_max_days_to_expiry` calendar days away
  (default 20-35), so never in expiry week.
- sold strike: the first strike beyond the nearest support (puts) or resistance (calls) whose |delta|,
  from its own price, is at or below `max_short_delta`; when open interest is known across the chain
  (live) it must also lie beyond the highest-OI strike on that side. None: no signal, with the reason.
- bought strike: same expiry and right, `hedge_strikes_out` strikes further out (2, then 3): the
  nearest one that is liquid and keeps one lot's max loss (width x lot - credit + costs) within
  `max_loss_per_stock_spread_pct` of capital. None: no signal. A sold option is never left alone.
- both strikes pass the liquidity filters (open interest in lots; live also the bid-ask width).
- the credit must be worth more than the round-trip costs (max profit above zero).
- margin for the spread must be known for a live signal (MARGIN_UNKNOWN otherwise).
- falling-stock block (`trend_block`): no put selling when the stock is below its 200-day average, made
  a 52-week low in the last 20 sessions, or trails NIFTY over 3 months by more than the set margin
  (DOWNTREND_BLOCK); mirrored for call selling in a strong uptrend (UPTREND_BLOCK).
- per-stock campaign limit (`campaign_limit`): after 2 stopped trades or a set net loss in one stock
  within 90 days, no new signal in it for the cooling-off period (STOCK_LOSS_LIMIT). The context's
  `closed_trades` carries signals and manual positions alike.

Support = the lowest low and resistance = the highest high of the last
`support_resistance_lookback_days` sessions (today included).

Exits (exit_calls; the backtest applies the same rules): keep `profit_target_pct_of_credit` of the
credit; stop when buying the spread back costs `stop_multiple_of_credit` x the credit; threat when the
stock closes beyond the sold strike or the trend flips; forced exit `exit_days_before_expiry` days
before expiry (physical settlement). `roll_choices` prices "exit" against "roll" for a threatened
spread; a roll obeys every entry rule and is never unhedged.

The no-filter baseline (backtests) keeps the structure rules (support/resistance, delta, hedge, max
loss, liquidity, entry window) and drops the entry rules (trend, IV rank, events).

Not built yet (after phase 6): signal cards with the capital-fit label, alerts, manual positions.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from ..config import StockOptionSelling as StockOptionSellingParams
from ..indicators import atr, last, sma
from ..models import Signal
from ..quality import IST
from ..strategies.base import fmt, ok
from .base import (ExpiryInfo, OptionContext, OptionQuote, leg_view, liquidity_problem, metrics, pick_short_strike,
                   round_trip_costs)
from .credit_spread import SIDE_NAME, CreditSpread
from .margin import MARGIN_UNKNOWN
from .payoff import InvalidStructure, OptionLeg, credit_spread
from .stock_profile import (STOCK_LOSS_LIMIT, Snapshot, alert_kinds, breach_frequency, state_now, stock_loss_limit,
                            trend_block)

SIDE_BY_TREND = {"UP": "PE", "DOWN": "CE"}
EXCLUDING_EVENTS = ("RESULTS", "BOARD_MEETING", "CORPORATE_ACTION")
MAX_HEDGE_PROBES = 4            # strikes without a price that may be passed over while counting "strikes out"


# ---------------- pure rules ----------------
def trend_of(daily, fast: int, slow: int, atr_days: int) -> tuple[str | None, float | None, str]:
    """(direction UP | DOWN | None, clarity 0..1 or None, text). The swing trend rule on daily bars.
    Clarity = distance between the fast and slow averages in ATRs, capped at 2 ATRs = 1.0."""
    if len(daily) < max(slow, atr_days + 1):
        return None, None, f"trend unknown: {len(daily)} daily bars, needs {max(slow, atr_days + 1)}"
    closes = [b.close for b in daily]
    f, s = last(sma(closes, fast)), last(sma(closes, slow))
    a = last(atr([b.high for b in daily], [b.low for b in daily], closes, atr_days))
    if not (ok(f) and ok(s) and ok(a)) or a <= 0:
        return None, None, "trend unknown: averages unavailable"
    c = closes[-1]
    clarity = round(min(1.0, abs(f - s) / (2 * a)), 3)
    text = f"close {fmt(c)}, {fast}-day average {fmt(f)}, {slow}-day average {fmt(s)}"
    if c > s and f > s:
        return "UP", clarity, "up trend: " + text
    if c < s and f < s:
        return "DOWN", clarity, "down trend: " + text
    return None, clarity, "no clear trend: " + text


def support_resistance(daily, lookback: int) -> tuple[float | None, float | None]:
    """(support, resistance): the lowest low and the highest high of the last `lookback` sessions."""
    if len(daily) < lookback:
        return None, None
    recent = daily[-lookback:]
    return min(b.low for b in recent), max(b.high for b in recent)


def events_before_expiry(events, symbol: str, today: date, expiry: date) -> list:
    """The stock's own results, board meetings and corporate actions from today to expiry."""
    return [e for e in events or () if getattr(e, "symbol", "") == symbol and today <= e.date <= expiry
            and e.type in EXCLUDING_EVENTS]


class BeyondLevel:
    """A view of the quotes that only offers strikes beyond a price level (below it for puts, above it
    for calls), so the shared short-strike rule searches from the support or resistance outward."""

    def __init__(self, quotes, right: str, level: float):
        self._q, self._right, self._level = quotes, right, level
        self.oi_available = quotes.oi_available

    def strikes(self, expiry: date) -> list[float]:
        ks = self._q.strikes(expiry)
        return [k for k in ks if (k < self._level if self._right == "PE" else k > self._level)]

    def atm_candidates(self, expiry: date):
        return self._q.atm_candidates(expiry)

    def quote(self, expiry: date, strike: float, right: str):
        return self._q.quote(expiry, strike, right)

    def highest_oi_strike(self, expiry: date, right: str):
        return self._q.highest_oi_strike(expiry, right)


@dataclass
class Assessment:
    """Everything the rules found for one stock on one day (shown on the shortlist)."""
    stock: str
    status: str = "EXCLUDED"            # CANDIDATE (a valid setup) | EXCLUDED
    side: str | None = None             # PE (bull put) | CE (bear call)
    expiry: date | None = None
    scores: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    signal: Signal | None = None
    short_strike: float | None = None   # the sold strike the rules chose, if they got that far
    distance_pct: float | None = None   # its distance from the price, % of the price


class StockOptionSelling(CreditSpread):
    id = "stock_option_selling"
    base_version = "1"
    horizon = "MONTHLY"
    allowed_regimes = frozenset()        # the stock's own trend decides, not the market regime
    structure = "CREDIT_SPREAD"
    expiry_kinds = ("monthly",)
    needs_daily = True                   # the context must carry the stock's daily bars
    params: StockOptionSellingParams
    # Backtest comparisons only: entry rules ("trend_block", "campaign_limit") that the no-filter
    # baseline should apply as well, and alert kinds on which an open spread is closed at the next open.
    baseline_rules: frozenset = frozenset()
    alert_exits: frozenset = frozenset()

    def applies(self, rule: str, ctx: OptionContext) -> bool:
        return getattr(self.params, rule).enabled and (not ctx.no_filter or rule in self.baseline_rules)

    @property
    def threat_exit(self) -> bool:
        return self.params.backtest.exit_on_strike_breach

    # ---- entry rules ----
    def entry_checks(self, ctx: OptionContext) -> bool:
        p = self.params
        if ctx.kind != "stock":
            self.no("NOT_A_STOCK", "stock option selling runs on stocks; index spreads are the credit_spread strategy")
            return False
        if ctx.no_filter:
            return True
        if not p.enabled:
            self.no("DISABLED", "stock_option_selling.enabled is false")
            return False
        direction, _, text = trend_of(ctx.daily, p.trend_fast_sma, p.trend_slow_sma, p.atr_days)
        if direction is None:
            self.no("NO_TREND", text)
            return False
        if ctx.iv_rank is None:
            self.no("IV_RANK_UNKNOWN", "not enough implied-volatility history for this stock")
            return False
        if ctx.iv_rank < p.min_iv_rank:
            self.no("IV_RANK_TOO_LOW", f"IV rank {ctx.iv_rank:.0f} below {p.min_iv_rank:g}: premium is thin")
            return False
        return True

    def side(self, ctx: OptionContext) -> str | None:
        if ctx.no_filter:
            return ctx.baseline_right if ctx.baseline_right in ("PE", "CE") else None
        p = self.params
        return SIDE_BY_TREND.get(trend_of(ctx.daily, p.trend_fast_sma, p.trend_slow_sma, p.atr_days)[0])

    def pick_hedge(self, ctx: OptionContext, exp: ExpiryInfo, right: str, ks: float, qs: OptionQuote):
        """The bought strike: 2 strikes further out, then 3 (config), counting strikes that have a
        price; the nearest one that is liquid and keeps max loss within the limit.
        Returns (strike, quote, legs, credit, payoff, n_out) or None after recording why not."""
        p = self.params
        sell = qs.sell_price(ctx.historical)
        if sell is None:
            self.no("DATA_MISSING", f"no usable price for {ks:g} {right}")
            return None
        further = sorted((k for k in ctx.quotes.strikes(exp.expiry) if (k < ks if right == "PE" else k > ks)),
                         key=lambda k: abs(k - ks))
        limit = ctx.capital_inr * p.max_loss_per_stock_spread_pct / 100.0
        lo, hi = p.hedge_strikes_out
        n, why = 0, []
        for k in further[:hi + MAX_HEDGE_PROBES]:
            q = ctx.quotes.quote(exp.expiry, k, right)
            if q is None or q.buy_price(ctx.historical) is None:
                continue                                     # a strike without a price is not counted
            n += 1
            if n < lo:
                continue
            if n > hi:
                break
            lp = liquidity_problem(ctx, q, exp.lot_size)
            if lp:
                why.append(("ILLIQUID", lp))
                continue
            buy = q.buy_price(ctx.historical)
            credit = round(sell, 2) - round(buy, 2)
            width = abs(ks - k)
            if credit <= 0 or credit >= width:
                why.append(("NO_CREDIT", f"{ks:g}/{k:g} {right}: net credit {credit:.2f} on width {width:g}"))
                continue
            legs = [OptionLeg("SELL", right, ks, exp.expiry, 1, round(sell, 2), exp.lot_size),
                    OptionLeg("BUY", right, k, exp.expiry, 1, round(buy, 2), exp.lot_size)]
            try:
                pay = credit_spread(right, ks, k, credit, exp.lot_size, 1, round_trip_costs(ctx.costs, legs).total)
            except InvalidStructure as e:
                why.append(("NO_CREDIT", str(e)))
                continue
            if pay.max_profit_inr <= 0:
                why.append(("NO_CREDIT_AFTER_COSTS", f"{ks:g}/{k:g} {right}: credit {credit:.2f} x lot {exp.lot_size} "
                                                     f"does not cover the round-trip costs"))
                continue
            if float(pay.max_loss_inr) > limit:
                why.append(("MAX_LOSS_ABOVE_LIMIT",
                            f"{ks:g}/{k:g} {right}: max loss Rs {fmt(float(pay.max_loss_inr))} per lot is above "
                            f"{p.max_loss_per_stock_spread_pct:g}% of capital (Rs {fmt(limit)})"))
                continue
            return k, q, legs, credit, pay, n
        if why:
            self.no(*why[0])
        else:
            self.no("UNHEDGED_SHORT", f"no priced strike {lo}-{hi} strikes beyond {ks:g}{right}: "
                                      f"a sold option is never left alone")
        return None

    def assess(self, ctx: OptionContext) -> Assessment:
        """Apply every rule to one stock. `signal` is set only for a complete, valid, hedged setup."""
        p = self.params
        out = Assessment(ctx.underlying)
        direction, clarity, trend_text = trend_of(ctx.daily, p.trend_fast_sma, p.trend_slow_sma, p.atr_days)
        out.scores = {"iv_rank": None if ctx.iv_rank is None else round(ctx.iv_rank / 100.0, 3),
                      "trend_clarity": clarity, "distance": None, "liquidity": None}
        out.reasons.append(trend_text)
        out.reasons.append("IV rank unknown" if ctx.iv_rank is None else f"IV rank {ctx.iv_rank:.0f}")

        def stop() -> Assessment:
            out.reasons += [r for r in self.last_no_trade if r not in out.reasons]
            return out

        if not self.entry_checks(ctx):
            return stop()
        right = self.side(ctx)
        if right is None:
            self.no("NO_SIDE", "baseline needs a side")
            return stop()
        out.side = right
        today = ctx.asof.astimezone(IST).date()
        if self.applies("trend_block", ctx):
            code, why_block = trend_block(ctx.daily, ctx.index_daily, right, p.trend_block)
            if code:
                self.no(code, "; ".join(why_block))
                return stop()
        if self.applies("campaign_limit", ctx):
            limit = stock_loss_limit(ctx.closed_trades, today, p.campaign_limit, ctx.capital_inr)
            if limit:
                self.no(STOCK_LOSS_LIMIT, limit[1])
                return stop()
        exp = self.pick(ctx)
        if exp is None:
            return stop()
        out.expiry = exp.expiry
        if not ctx.no_filter:
            ev = events_before_expiry(ctx.events, ctx.underlying, today, exp.expiry)
            if ev:
                self.no("EVENT_BEFORE_EXPIRY", ", ".join(f"{e.type} on {e.date:%d-%b-%Y}" for e in ev[:3]))
                return stop()
        support, resistance = support_resistance(ctx.daily, p.support_resistance_lookback_days)
        level = support if right == "PE" else resistance
        if level is None:
            self.no("INSUFFICIENT_HISTORY", f"fewer than {p.support_resistance_lookback_days} daily bars for support/resistance")
            return stop()
        closes = [b.close for b in ctx.daily]
        a = last(atr([b.high for b in ctx.daily], [b.low for b in ctx.daily], closes, p.atr_days)) \
            if len(ctx.daily) > p.atr_days else None
        gap = (ctx.spot - level) if right == "PE" else (level - ctx.spot)
        if ok(a) and a > 0:
            out.scores["distance"] = round(max(0.0, min(1.0, gap / (5 * a))), 3)
        level_name = "support" if right == "PE" else "resistance"
        out.reasons.append(f"{level_name} {fmt(level)} ({p.support_resistance_lookback_days}-session "
                           f"{'low' if right == 'PE' else 'high'}), {fmt(gap)} from the price {fmt(ctx.spot)}")
        lctx = replace(ctx, max_spread_pct=p.max_bid_ask_spread_pct, min_oi_lots=p.min_open_interest)
        got = pick_short_strike(replace(lctx, quotes=BeyondLevel(ctx.quotes, right, level)), exp.expiry, right,
                                p.max_short_delta)
        if got[0] is None:
            self.no("NO_STRIKE", f"beyond {level_name} {fmt(level)}: {got[1]}")
            return stop()
        ks, qs, iv_s, d_s, notes = got
        out.short_strike, out.distance_pct = ks, round(abs(ctx.spot - ks) / ctx.spot * 100, 2)
        lp = liquidity_problem(lctx, qs, exp.lot_size)
        if lp:
            self.no("ILLIQUID", lp)
            return stop()
        if qs.oi is not None and p.min_open_interest:
            out.scores["liquidity"] = round(min(1.0, qs.oi / exp.lot_size / (5.0 * p.min_open_interest)), 3)
        hedge = self.pick_hedge(lctx, exp, right, ks, qs)
        if hedge is None:
            return stop()
        kl, ql, legs, credit, pay, n_out = hedge
        ml = metrics(ctx, exp.expiry, kl, right)
        views = [leg_view(legs[0], iv_s, d_s, qs), leg_view(legs[1], ml[1] if ml else None, ml[2] if ml else None, ql)]
        if ctx.no_filter:
            why = [f"no-filter baseline: {SIDE_NAME[right]} on a fixed schedule"]
        else:
            why = [f"{trend_text}: {SIDE_NAME[right]}",
                   f"IV rank {ctx.iv_rank:.0f} at or above {p.min_iv_rank:g} (premium is rich)"]
        why.append(f"sold strike {ks:g} is beyond {level_name} {fmt(level)}: delta {views[0]['delta']} "
                   f"(limit {p.max_short_delta:g}); " + "; ".join(notes))
        why.append(f"bought strike {kl:g}, {n_out} strikes further out; max loss within "
                   f"{p.max_loss_per_stock_spread_pct:g}% of capital")
        setup = (f"{SIDE_NAME[right]} {ks:g}/{kl:g} {exp.expiry:%d-%b-%Y}: credit {fmt(credit)}, "
                 f"max loss Rs {fmt(float(pay.max_loss_inr))} per lot")
        sig = self.finish(ctx, exp, legs, views, credit, abs(ks - kl), pay, why, SIDE_NAME[right])
        if not sig:
            if self.last_no_trade and self.last_no_trade[-1].startswith(MARGIN_UNKNOWN):
                out.status = "CANDIDATE"          # a valid setup; no signal until the margin is entered
                out.reasons.append(setup)
            return stop()
        s = sig[0]
        distance = abs(ctx.spot - ks) / ctx.spot * 100
        floor_closes = closes[-504:]                         # about two years, all on or before today
        s.details.update({"side": right, "trend": direction, "support": support, "resistance": resistance,
                          "hedge_strikes_out": n_out,
                          "entry_conditions": {
                              "state": state_now(closes, p.profile.state_lookback_sessions, p.profile.trending_efficiency)[0],
                              "short_strike_distance_pct": round(distance, 2),
                              "breach_history_pct": breach_frequency(floor_closes, distance, right,
                                                                     p.profile.holding_sessions)["breached_pct"],
                              "iv_rank": ctx.iv_rank, "fii_bias": ctx.market.get("fii_bias"),
                              "vix_change_pct": ctx.market.get("vix_change_pct")},
                          "warnings": ["Stock options settle physically: close before expiry week."]})
        out.signal, out.status = s, "CANDIDATE"
        out.reasons.append(setup)
        return out

    def _evaluate(self, ctx: OptionContext):
        a = self.assess(ctx)
        return [a.signal] if a.signal is not None else []


# ---------------- shortlist ----------------
def weighted_score(scores: dict, weights) -> float:
    """Weighted sum of the four 0..1 scores; an unknown score counts as zero."""
    return round(sum((scores.get(k) or 0.0) * getattr(weights, k)
                     for k in ("iv_rank", "trend_clarity", "distance", "liquidity")), 4)


def build_shortlist(strategy: StockOptionSelling, contexts: list[OptionContext]) -> list[dict]:
    """Assess every stock and rank the valid setups by weighted score. Returns rows for the `shortlist`
    table: the top `shortlist_size` candidates ranked 1..n, then every other stock with the reason it
    is not on the list (illiquid, event before expiry, no trend, ...)."""
    p = strategy.params
    rows = []
    for ctx in contexts:
        a = strategy.assess(ctx)
        rows.append({"stock": a.stock, "expiry": a.expiry.isoformat() if a.expiry else None, "side": a.side,
                     "status": a.status, "score": weighted_score(a.scores, p.weights) if a.status == "CANDIDATE" else None,
                     "scores": a.scores, "reasons": a.reasons, "rank": None})
    good = sorted((r for r in rows if r["status"] == "CANDIDATE"), key=lambda r: (-r["score"], r["stock"]))
    for n, r in enumerate(good, start=1):
        if n <= p.shortlist_size:
            r["rank"], r["status"] = n, "SHORTLISTED"
        else:
            r["reasons"] = r["reasons"] + [f"valid setup, but ranked {n} (the shortlist shows {p.shortlist_size})"]
    rest = sorted((r for r in rows if r["status"] != "SHORTLISTED"), key=lambda r: r["stock"])
    return [r for r in good if r["status"] == "SHORTLISTED"] + rest


# ---------------- exits and adjustments ----------------
@dataclass(frozen=True)
class SpreadPosition:
    """An open stock credit spread, per share prices. `credit` is what was received at entry."""
    underlying: str
    right: str                    # PE (bull put) | CE (bear call)
    short_strike: float
    long_strike: float
    expiry: date
    credit: float
    lot_size: int
    lots: int = 1

    @property
    def shares(self) -> int:
        return self.lot_size * self.lots


def exit_levels(credit: float, p: StockOptionSellingParams) -> tuple[float, float]:
    """(buy back at or below, stop at or above), per share."""
    return credit * (1 - p.profit_target_pct_of_credit / 100.0), credit * p.stop_multiple_of_credit


def strike_breached(right: str, short_strike: float, stock_close: float) -> bool:
    return stock_close < short_strike if right == "PE" else stock_close > short_strike


def exit_calls(pos: SpreadPosition, p: StockOptionSellingParams, today: date, close_cost: float | None,
               stock_close: float | None, trend: str | None) -> list[dict]:
    """Every exit or adjustment call that applies to an open spread right now, most urgent first.
    `close_cost` = cost per share to buy the spread back (sold leg's price minus bought leg's price);
    None = unknown, which is itself reported, never treated as "no exit". The caller repeats the calls
    until the position is closed (phase 6)."""
    out = []
    target, stop = exit_levels(pos.credit, p)
    days = (pos.expiry - today).days

    def pnl(cost: float) -> float:
        return round((pos.credit - cost) * pos.shares, 2)

    if days <= p.exit_days_before_expiry:
        out.append({"kind": "FORCED_EXPIRY_EXIT", "action": "Exit now: physical settlement",
                    "reason": f"{days} day(s) to expiry; stock options settle physically, no position in expiry week"})
    if close_cost is None:
        out.append({"kind": "DATA_MISSING", "action": "Check the position by hand",
                    "reason": "no usable price for one of the legs: target and stop cannot be checked"})
    else:
        if close_cost >= stop:
            out.append({"kind": "STOP", "action": "Exit: stop reached",
                        "reason": f"cost to close {fmt(close_cost)} is at or above {fmt(stop)} "
                                  f"({p.stop_multiple_of_credit:g}x the credit {fmt(pos.credit)})",
                        "pnl_before_costs_inr": pnl(close_cost)})
        elif close_cost <= target:
            out.append({"kind": "TARGET", "action": "Exit: profit target reached",
                        "reason": f"cost to close {fmt(close_cost)} is at or below {fmt(target)} "
                                  f"({p.profit_target_pct_of_credit:g}% of the credit {fmt(pos.credit)} kept)",
                        "pnl_before_costs_inr": pnl(close_cost)})
    if stock_close is not None and strike_breached(pos.right, pos.short_strike, stock_close):
        out.append({"kind": "STRIKE_BREACHED", "action": "Threat: exit or roll",
                    "reason": f"{pos.underlying} closed at {fmt(stock_close)}, beyond the sold strike {pos.short_strike:g}"})
    against = "DOWN" if pos.right == "PE" else "UP"
    if trend == against:
        out.append({"kind": "TREND_FLIP", "action": "Threat: exit or roll",
                    "reason": f"the trend turned {against.lower()}, against this {SIDE_NAME[pos.right]}"})
    return out


def roll_choices(strategy: StockOptionSelling, ctx: OptionContext, pos: SpreadPosition, close_cost: float) -> dict:
    """The two choices for a threatened spread, with numbers. Exit: cost to close now and the realised
    P&L. Roll: close this spread and open the same structure at the next valid strikes (this month if
    it is still inside the entry window, else the next month). A roll obeys every entry rule, so it is
    never unhedged; if no valid roll exists the answer says so."""
    exit_costs = float(round_trip_costs(ctx.costs, [
        OptionLeg("BUY", pos.right, pos.short_strike, pos.expiry, pos.lots, max(close_cost, 0.05), pos.lot_size)]).total) / 2
    realised = round((pos.credit - close_cost) * pos.shares - exit_costs, 2)
    out = {"exit": {"cost_to_close_per_share": round(close_cost, 2),
                    "cost_to_close_inr": round(close_cost * pos.shares, 2),
                    "realised_pnl_inr": realised, "note": "after estimated closing charges"},
           "roll": None, "roll_note": ""}
    best = None
    for exp in ctx.expiries:
        a = strategy.assess(replace(ctx, expiries=(exp,)))
        if a.signal is None or a.side != pos.right:
            continue
        legs = a.signal.legs
        if exp.expiry == pos.expiry and legs[0].strike == pos.short_strike and legs[1].strike == pos.long_strike:
            continue                                         # the same spread is not a roll
        best = a.signal
        break
    if best is None:
        out["roll_note"] = "no valid roll: " + ("; ".join(strategy.last_no_trade) or
                                                "no other strikes or month pass the entry rules")
        return out
    d = best.details
    out["roll"] = {"legs": [leg.to_dict() for leg in best.legs], "expiry": d["expiry"],
                   "new_credit_per_share": d["net_credit"],
                   "net_per_share": round(d["net_credit"] - close_cost, 2),
                   "net_inr": round((d["net_credit"] - close_cost) * pos.shares, 2),
                   "net_is": "credit" if d["net_credit"] >= close_cost else "debit",
                   "new_max_loss_inr": d["max_loss_inr"], "new_breakevens": d["breakevens"],
                   "margin_inr": d["margin_inr"]}
    return out


def position_alerts(strategy: StockOptionSelling, ctx: OptionContext, pos: SpreadPosition, close_cost: float | None,
                    prev: Snapshot | None, now: Snapshot | None) -> list[dict]:
    """Alerts for an open spread: what changed since the previous close (stock_profile.alert_kinds),
    each with the exit and roll choices in numbers. With no usable price for the legs the choices
    say so instead of showing numbers. Phase 6 sends these (Telegram) and repeats them."""
    changes = alert_kinds(prev, now, pos.right, strategy.params.profile.vix_spike_pct)
    if not changes:
        return []
    if close_cost is None:
        choices = {"exit": None, "roll": None, "roll_note": "no usable price for one of the legs: "
                                                            "exit and roll cannot be priced"}
    else:
        choices = roll_choices(strategy, ctx, pos, close_cost)
    return [{**c, "underlying": pos.underlying, "position": f"{SIDE_NAME[pos.right]} {pos.short_strike:g}/"
             f"{pos.long_strike:g} {pos.expiry:%d-%b-%Y}", **choices} for c in changes]


def build_stock_selling(settings) -> StockOptionSelling:
    """The strategy with its thresholds from config.yaml (`stock_option_selling`) and its status."""
    st = StockOptionSelling(settings.stock_option_selling)
    status = settings.strategies.status_of(st.id)
    st.alerts_enabled, st.mode, st.status_label = status.alerts, status.mode, status.label
    return st
