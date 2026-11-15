"""Phase 9 (core): monthly hedged credit spreads on F&O stocks.

Up trend -> bull put spread (sell a put below support, buy a put further below).
Down trend -> bear call spread (sell a call above resistance, buy a call further above).

Entry rules (StockOptionSelling.assess; every number is computed here, none by an LLM):
- stocks only; the stock must have a clear trend by the swing trend rule (close above the slow average
  and the fast average above the slow one = up; the mirror image = down). No clear trend: no signal.
- IV rank known and at or above `min_iv_rank`.
- no results, board meeting or corporate action for the stock between today and expiry.
- expiry: the monthly contract `entry_dte_min`..`entry_dte_max` calendar days away
  (default 20-35), so never in expiry week.
- sold strike: the first strike beyond the nearest support (puts) or resistance (calls) whose |delta|,
  from its own price, is at or below `max_short_delta`; when open interest is known across the chain
  (live) it must also lie beyond the highest-OI strike on that side. None: no signal, with the reason.
- bought strike: same expiry and right, `hedge_strikes_out` strikes further out (2, then 3): the
  nearest one that is liquid and keeps one lot's max loss (width x lot - credit + costs) within
  `max_risk_per_position_pct` of capital. None: no signal. A sold option is never left alone.
- both strikes pass the liquidity filters (open interest in lots; live also the bid-ask width).
- the credit must be worth more than the round-trip costs (max profit above zero).
- margin for the spread must be known for a live signal (MARGIN_UNKNOWN otherwise).
- falling-stock block (`trend_block`): no put selling when the stock is below its 200-day average, made
  a 52-week low in the last 20 sessions, or trails NIFTY over 3 months by more than the set margin
  (DOWNTREND_BLOCK); mirrored for call selling in a strong uptrend (UPTREND_BLOCK).
- market block (`market_block`): no put selling when NIFTY is below its 200-day average or fewer than 40% of
  the watchlist stocks are above their 50-session average (MARKET_WEAK); unknown market data blocks too
  (MARKET_UNKNOWN), for put and call selling alike. Call selling is not blocked by a weak market.
- per-stock campaign limit (`campaign_limit`): after 2 stopped trades or a set net loss in one stock
  within 90 days, no new signal in it for the cooling-off period (STOCK_LOSS_LIMIT). The context's
  `closed_trades` carries signals and manual positions alike.

Support = the nearest confirmed 5-bar pivot low below the close, resistance = the nearest confirmed
pivot high above it, among the pivots of the last `pivot_lookback_sessions` sessions; a pivot is used
only once its 5 later sessions have completed (stock_profile.pivots).

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
from datetime import date, timedelta

from ..config import StockOptionSelling as StockOptionSellingParams
from ..costs import CostBreakdown
from ..indicators import atr, last, sma
from ..models import Signal
from ..quality import IST
from ..strategies.base import fmt, ok
from .base import (ExpiryInfo, OptionContext, OptionQuote, add_costs, leg_view, liquidity_problem, metrics,
                   pick_short_strike,
                   round_trip_costs)
from .credit_spread import SIDE_NAME, CreditSpread
from .margin import MARGIN_UNKNOWN
from .campaign import after_roll
from .score import candidate_score
from .sizing import ZERO_SIZE, size_spread
from .payoff import InvalidStructure, OptionLeg, credit_spread
from .stock_profile import (DOWNTREND_BLOCK, MARKET_UNKNOWN, MARKET_WEAK, expected_move, support_resistance, STOCK_LOSS_LIMIT, Snapshot, market_weak, alert_kinds, breach_frequency, state_now, stock_loss_limit,
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


EVENT_COVERAGE_UNKNOWN = "EVENT_COVERAGE_UNKNOWN"


def event_coverage(events, symbol: str, today: date, expiry: date, max_age_days: int) -> tuple[bool, str]:
    """(covered, text). Covered = events.csv holds a COVERAGE row for the stock that was retrieved on or
    before today, at most `max_age_days` ago, and covers the option's expiry. The absence of an
    announced event is only meaningful with such a row; without it the event rule is unknown."""
    from ..manual_data import coverage_fields
    best = None
    for e in events or ():
        if getattr(e, "type", "") != "COVERAGE" or getattr(e, "symbol", "") != symbol:
            continue
        got = coverage_fields(e.note)
        if got is None or e.date > today:
            continue
        if best is None or (e.date, got[1]) > (best[0], best[2]):
            best = (e.date, got[0], got[1])
    if best is None:
        return False, (f"no event coverage row for {symbol} in events.csv (type COVERAGE: symbol, source, retrieved_at, "
                       f"covered_through)")
    retrieved, source, through = best
    if (today - retrieved).days > max_age_days:
        return False, (f"event coverage from {source} was retrieved {retrieved:%d-%b-%Y}, more than {max_age_days} days ago")
    if through < expiry:
        return False, f"event coverage from {source} runs to {through:%d-%b-%Y}, before the expiry {expiry:%d-%b-%Y}"
    return True, f"events checked at {source} on {retrieved:%d-%b-%Y}, covered through {through:%d-%b-%Y}"


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
    score: dict | None = None           # the 0-100 score (score.py) once a sold strike is chosen
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

    exit_policy = "stop"                 # backtest comparison: stop | roll_rules | roll_always
    below_base_low = False               # reversal candidate: the sold strike always sits below the base low
    report_open_at_cutoff = True         # backtests: a position open at the end of the period is reported open

    def applies(self, rule: str, ctx: OptionContext) -> bool:
        if ctx.rolling:
            return False                 # "any": the research-only unlimited roll has no entry rules
        return getattr(self.params, rule).enabled and (not ctx.no_filter or rule in self.baseline_rules)

    def min_credit_applies(self, ctx: OptionContext) -> bool:
        return (self.params.min_credit_to_max_loss > 0 and ctx.rolling != "any"
                and (not ctx.no_filter or "min_credit" in self.baseline_rules))

    @property
    def threat_exit(self) -> bool:
        return self.params.backtest.exit_on_strike_breach

    # ---- entry rules ----
    def entry_checks(self, ctx: OptionContext) -> bool:
        p = self.params
        if ctx.kind != "stock":
            self.no("NOT_A_STOCK", "stock option selling runs on stocks; index spreads are the credit_spread strategy")
            return False
        if ctx.no_filter or ctx.rolling == "any":
            return True
        if not p.enabled:
            self.no("DISABLED", "stock_option_selling.enabled is false")
            return False
        direction, _, text = trend_of(ctx.daily, p.sma_short, p.sma_mid, p.atr_days)
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
        if ctx.no_filter or ctx.rolling:
            return ctx.baseline_right if ctx.baseline_right in ("PE", "CE") else None
        p = self.params
        return SIDE_BY_TREND.get(trend_of(ctx.daily, p.sma_short, p.sma_mid, p.atr_days)[0])

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
        budget = ctx.capital_inr * p.max_risk_per_position_pct / 100.0     # max_risk_per_position_pct
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
            one = [OptionLeg("SELL", right, ks, exp.expiry, 1, round(sell, 2), exp.lot_size),
                   OptionLeg("BUY", right, k, exp.expiry, 1, round(buy, 2), exp.lot_size)]
            try:
                lots, legs, pay = size_spread("credit", one, exp.lot_size, ctx.costs, budget, ctx.max_lots)
            except InvalidStructure as e:
                why.append(("NO_CREDIT", str(e)))
                continue
            if pay.max_profit_inr <= 0:
                why.append(("NO_CREDIT_AFTER_COSTS", f"{ks:g}/{k:g} {right}: credit {credit:.2f} x lot {exp.lot_size} "
                                                     f"does not cover the round-trip costs"))
                continue
            if lots == 0:
                why.append((ZERO_SIZE, f"{ks:g}/{k:g} {right}: one lot's max loss Rs {fmt(float(pay.max_loss_inr))} is "
                                       f"above the position risk budget of {p.max_risk_per_position_pct:g}% of capital "
                                       f"(Rs {fmt(budget)}): 0 whole lots"))
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
        self.last_no_trade = []          # the reasons of THIS assessment only (roll decisions call assess directly)
        out = Assessment(ctx.underlying)
        direction, clarity, trend_text = trend_of(ctx.daily, p.sma_short, p.sma_mid, p.atr_days)
        out.scores = {}
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
        rev = ctx.market.get("reversal") or {}
        base_low = rev.get("base_low") if (rev.get("lift_active") or rev.get("confirmed_today")) and right == "PE" else None
        below = base_low if self.below_base_low and not ctx.no_filter else None
        if self.applies("trend_block", ctx):
            code, why_block = trend_block(ctx.daily, ctx.index_daily, right, p.trend_block, p.sma_long)
            if code == DOWNTREND_BLOCK and base_low is not None and p.reversal.enabled:
                below = base_low              # REVERSAL_CONFIRMED lifts the block, only for a put spread below the base low
                out.reasons.append(f"REVERSAL_CONFIRMED ({rev.get('confirmed_on')}): the falling-stock block is lifted "
                                   f"for a put spread sold below the base low {fmt(base_low)}")
            elif code:
                self.no(code, "; ".join(why_block))
                return stop()
        if self.below_base_low and not ctx.no_filter and below is None:
            self.no("NO_REVERSAL", "no confirmed reversal with a base low for this stock")
            return stop()
        if self.applies("market_block", ctx):
            # MARKET_WEAK: put selling only (the side a weak market hurts). MARKET_UNKNOWN: both sides -
            # an unknown market state is never read as "not weak" (review fix, 2026-11-15)
            code, why_mkt = market_weak(ctx.index_daily, ctx.market.get("breadth_pct"), p.market_block,
                                        p.sma_long, p.sma_mid)
            if code == MARKET_UNKNOWN or (code == MARKET_WEAK and right == "PE"):
                self.no(code, "; ".join(why_mkt))
                return stop()
        if self.applies("campaign_limit", ctx):
            limit = stock_loss_limit(ctx.closed_trades, today, p.campaign_limit, ctx.capital_inr)
            if limit:
                self.no(STOCK_LOSS_LIMIT, limit[1])
                return stop()
        if ctx.rolling == "any":          # the next month, whatever its days to expiry
            exp = next((e for e in ctx.expiries if e.lot_size), None)
            if exp is None:
                self.no("NO_EXPIRY", "no later expiry with a known lot size")
        else:
            exp = self.pick(ctx)
        if exp is None:
            return stop()
        out.expiry = exp.expiry
        coverage_missing = None
        if not ctx.no_filter and ctx.rolling != "any":
            ev = events_before_expiry(ctx.events, ctx.underlying, today, exp.expiry)
            if ev:
                self.no("EVENT_BEFORE_EXPIRY", ", ".join(f"{e.type} on {e.date:%d-%b-%Y}" for e in ev[:3]))
                return stop()
            if not ctx.historical:
                # Live and research: no announced event is only a pass with verified, fresh coverage through expiry.
                # (Backtests cannot know past coverage: the report lists the event check as not available historically.)
                covered, text = event_coverage(ctx.events, ctx.underlying, today, exp.expiry, p.event_coverage_max_age_days)
                if covered:
                    out.reasons.append(text)
                else:
                    coverage_missing = text
        support, resistance = support_resistance(ctx.daily, p.pivot_lookback_sessions, p.swing_pivot_bars)
        level = support if right == "PE" else resistance
        if level is not None and below is not None:
            level = min(level, below)
        elif below is not None:
            level = below                      # a reversal candidate: below the base low is the rule
        if level is None:
            if len(ctx.daily) < p.pivot_lookback_sessions:
                self.no("INSUFFICIENT_HISTORY", f"{len(ctx.daily)} daily bars, the confirmed pivots need "
                                                f"{p.pivot_lookback_sessions}")
            else:
                self.no("NO_CONFIRMED_LEVEL", f"no confirmed {'support (pivot low) below' if right == 'PE' else 'resistance (pivot high) above'} "
                                              f"the price in the last {p.pivot_lookback_sessions} sessions")
            return stop()
        closes = [b.close for b in ctx.daily]
        a = last(atr([b.high for b in ctx.daily], [b.low for b in ctx.daily], closes, p.atr_days)) \
            if len(ctx.daily) > p.atr_days else None
        gap = (ctx.spot - level) if right == "PE" else (level - ctx.spot)
        level_name = ("the base low" if below is not None and level == below else "support") if right == "PE" else "resistance"
        out.reasons.append(f"{level_name} {fmt(level)} (confirmed {p.swing_pivot_bars}-bar pivot "
                           f"{'low' if right == 'PE' else 'high'}), {fmt(gap)} from the price {fmt(ctx.spot)}")
        lctx = replace(ctx, max_spread_pct=p.max_bid_ask_pct_of_mid, min_oi_lots=p.min_open_interest_lots)
        got = pick_short_strike(replace(lctx, quotes=BeyondLevel(ctx.quotes, right, level)), exp.expiry, right,
                                p.short_delta_max)
        if got[0] is None:
            self.no("NO_STRIKE", f"beyond {level_name} {fmt(level)}: {got[1]}")
            return stop()
        ks, qs, iv_s, d_s, notes = got
        out.short_strike, out.distance_pct = ks, round(abs(ctx.spot - ks) / ctx.spot * 100, 2)
        lp = liquidity_problem(lctx, qs, exp.lot_size)
        if lp:
            self.no("ILLIQUID", lp)
            return stop()
        out.score = candidate_score(ctx, right, ks, None if qs.oi is None else qs.oi / exp.lot_size,
                                    (exp.expiry - today).days, p.score, p)
        out.scores = out.score["factors"]
        hedge = self.pick_hedge(lctx, exp, right, ks, qs)
        if hedge is None:
            return stop()
        kl, ql, legs, credit, pay, n_out = hedge
        net_inr, max_loss_inr = float(pay.max_profit_inr), float(pay.max_loss_inr)
        net_pct = round(net_inr / max_loss_inr * 100, 2) if max_loss_inr > 0 else None
        if self.min_credit_applies(ctx) and (net_pct is None or net_pct < p.min_credit_to_max_loss * 100):
            self.no("CREDIT_TOO_SMALL", f"{ks:g}/{kl:g} {right}: credit after the hedge and charges Rs {fmt(net_inr)} per lot "
                                        f"is {net_pct}% of the max loss Rs {fmt(max_loss_inr)}, under "
                                        f"{p.min_credit_to_max_loss * 100:g}% (min_credit_to_max_loss {p.min_credit_to_max_loss:g})")
            return stop()
        ml = metrics(ctx, exp.expiry, kl, right)
        views = [leg_view(legs[0], iv_s, d_s, qs), leg_view(legs[1], ml[1] if ml else None, ml[2] if ml else None, ql)]
        if ctx.rolling:
            why = [f"roll of an open {SIDE_NAME[right]}"]
        elif ctx.no_filter:
            why = [f"no-filter baseline: {SIDE_NAME[right]} on a fixed schedule"]
        elif self.below_base_low:
            why = ["REVERSAL_CONFIRMED: " + "; ".join(rev.get("reasons", [])[:3]),
                   f"{SIDE_NAME[right]} with the sold strike below the base low {fmt(below)}"]
        else:
            why = [f"{trend_text}: {SIDE_NAME[right]}",
                   f"IV rank {ctx.iv_rank:.0f} at or above {p.min_iv_rank:g} (premium is rich)"]
        why.append(f"sold strike {ks:g} is beyond {level_name} {fmt(level)}: delta {views[0]['delta']} "
                   f"(limit {p.short_delta_max:g}); " + "; ".join(notes))
        why.append(f"bought strike {kl:g}, {n_out} strikes further out; {legs[0].lots} lot(s): max loss "
                   f"Rs {fmt(float(pay.max_loss_inr))} within the {p.max_risk_per_position_pct:g}% risk budget")
        lots = legs[0].lots
        setup = (f"{SIDE_NAME[right]} {ks:g}/{kl:g} {exp.expiry:%d-%b-%Y}, {lots} lot(s) of {exp.lot_size}: credit "
                 f"{fmt(credit)} a share, max loss Rs {fmt(float(pay.max_loss_inr))} for the position")
        if coverage_missing is not None:
            # a research candidate stays visible with the reason; it is never an actionable signal
            self.no(EVENT_COVERAGE_UNKNOWN, coverage_missing)
            out.status = "CANDIDATE"
            out.reasons.append(setup)
            return stop()
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
                          "score": out.score,
                          "reversal": ({"base_low": below, "confirmed_on": rev.get("confirmed_on"),
                                        "stall_zones": rev.get("stall_zones", [])} if below is not None else None),
                          # what is left of the premium after paying for the hedge and every charge, one lot
                          "sold_premium_inr": round(legs[0].price * exp.lot_size * lots, 2),
                          "hedge_cost_inr": round(legs[1].price * exp.lot_size * lots, 2),
                          "net_credit_after_costs_inr": round(net_inr, 2),
                          "net_credit_after_costs_per_share": round(net_inr / (exp.lot_size * lots), 2),
                          "sizing": {"lots": lots, "risk_budget_inr": round(ctx.capital_inr * p.max_risk_per_position_pct / 100, 2),
                                     "max_loss_one_lot_inr": round(float(pay.max_loss_inr) / lots, 2), "rule": "whole lots within the risk budget, rounded down"},
                          "net_credit_pct_of_max_loss": net_pct,
                          "sizing_note": "whole lots within the risk budget by the normal rules: earlier losses in this "
                                         "stock never raise the size",
                          "entry_conditions": {
                              "state": state_now(closes, p.profile.state_lookback_sessions, p.profile.trending_efficiency)[0],
                              "short_strike_distance_pct": round(distance, 2),
                              "breach_history_pct": breach_frequency(floor_closes, distance, right,
                                                                     p.profile.holding_sessions)["breached_pct"],
                              "iv_rank": ctx.iv_rank, "fii_bias": ctx.market.get("fii_bias"),
                              "vix_spike": ctx.market.get("vix_spike"),
                              "score": out.score["total"] if out.score else None,
                              "score_factors": out.score["factors"] if out.score else {},
                              "expected_move_inr": (None if (em := expected_move(ctx.spot, ctx.iv_reference_pct,
                                                                                 (exp.expiry - today).days)) is None
                                                    else round(em, 2)),
                              "short_strike_beyond_expected_move": (None if em is None else abs(ctx.spot - ks) >= em)},
                          "warnings": ["Stock options settle physically: close before expiry week."]})
        out.signal, out.status = s, "CANDIDATE"
        out.reasons.append(setup)
        return out

    def _evaluate(self, ctx: OptionContext):
        a = self.assess(ctx)
        return [a.signal] if a.signal is not None else []


# ---------------- shortlist ----------------
def build_shortlist(strategy: StockOptionSelling, contexts: list[OptionContext], annotate=None) -> list[dict]:
    """Assess every stock and rank the valid setups by their 0-100 score (score.py), each factor's
    contribution shown. Returns rows for the `shortlist`
    table: the top `shortlist_size` candidates ranked 1..n, then every other stock with the reason it
    is not on the list (illiquid, event before expiry, no trend, ...)."""
    p = strategy.params
    rows = []
    for ctx in contexts:
        a = strategy.assess(ctx)
        sc = a.score or {}
        rows.append({"stock": a.stock, "expiry": a.expiry.isoformat() if a.expiry else None, "side": a.side,
                     "status": a.status, "score": sc.get("total") if a.status == "CANDIDATE" else None,
                     "scores": a.scores, "contributions": sc.get("contributions", {}),
                     "score_missing": sc.get("missing", []), "reasons": a.reasons, "rank": None,
                     **(annotate(ctx, a) if annotate is not None else {})})
    # fully scored candidates by score; a candidate whose score is incomplete after them, by name
    good = sorted((r for r in rows if r["status"] == "CANDIDATE"),
                  key=lambda r: (r["score"] is None, -(r["score"] or 0.0), r["stock"]))
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
    return credit * (1 - p.profit_target_pct_of_credit / 100.0), credit * p.stop_spread_value_multiple


def strike_breached(right: str, short_strike: float, stock_close: float) -> bool:
    return stock_close < short_strike if right == "PE" else stock_close > short_strike


def exit_calls(pos: SpreadPosition, p: StockOptionSellingParams, today: date, close_cost: float | None,
               stock_close: float | None, trend: str | None, costs=None,
               leg_px: tuple[float | None, float | None] | None = None) -> list[dict]:
    """Every exit or adjustment call that applies to an open spread right now, most urgent first.
    `close_cost` = cost per share to buy the spread back (sold leg's price minus bought leg's price);
    None = unknown, which is itself reported, never treated as "no exit". With `costs` and `leg_px`
    (the sold and bought legs' premiums) a stop or target also shows the closing charges, each leg on
    its own premium, and the P&L after them. The caller repeats the calls until the position is closed
    (phase 6)."""
    out = []
    target, stop = exit_levels(pos.credit, p)
    days = (pos.expiry - today).days
    charges = closing_charges(costs, pos, *leg_px) if costs is not None and leg_px is not None else None

    def pnl(cost: float) -> dict:
        before = round((pos.credit - cost) * pos.shares, 2)
        return {"pnl_before_costs_inr": before, "closing_charges_inr": None if charges is None else round(charges, 2),
                "pnl_after_closing_charges_inr": None if charges is None else round(before - charges, 2)}

    left = trading_days_to(today, pos.expiry)
    if left <= p.forced_exit_trading_days_before_expiry:
        out.append({"kind": "FORCED_EXPIRY_EXIT", "action": "Exit now: physical settlement",
                    "reason": f"{left} trading day(s) to expiry, inside the forced-exit window; stock options settle "
                              f"physically"})
    elif days <= p.time_exit_dte:
        out.append({"kind": "TIME_EXIT", "action": "Exit: time exit",
                    "reason": f"{days} day(s) to expiry, at or inside the {p.time_exit_dte}-day time exit"})
    if close_cost is None:
        out.append({"kind": "DATA_MISSING", "action": "Check the position by hand",
                    "reason": "no usable price for one of the legs: target and stop cannot be checked"})
    else:
        if close_cost >= stop:
            out.append({"kind": "STOP", "action": "Exit: stop reached",
                        "reason": f"cost to close {fmt(close_cost)} is at or above {fmt(stop)} "
                                  f"({p.stop_spread_value_multiple:g}x the credit {fmt(pos.credit)})",
                        **pnl(close_cost)})
        elif close_cost <= target:
            out.append({"kind": "TARGET", "action": "Exit: profit target reached",
                        "reason": f"cost to close {fmt(close_cost)} is at or below {fmt(target)} "
                                  f"({p.profit_target_pct_of_credit:g}% of the credit {fmt(pos.credit)} kept)",
                        **pnl(close_cost)})
    if stock_close is not None and strike_breached(pos.right, pos.short_strike, stock_close):
        out.append({"kind": "STRIKE_BREACHED", "action": "Threat: exit or roll",
                    "reason": f"{pos.underlying} closed at {fmt(stock_close)}, beyond the sold strike {pos.short_strike:g}"})
    against = "DOWN" if pos.right == "PE" else "UP"
    if trend == against:
        out.append({"kind": "TREND_FLIP", "action": "Threat: exit or roll",
                    "reason": f"the trend turned {against.lower()}, against this {SIDE_NAME[pos.right]}"})
    return out


def closing_charges(costs, pos: SpreadPosition, short_px: float | None, long_px: float | None) -> float | None:
    """Estimated charges to close the spread, each leg on its own actual premium: buying back the sold
    leg at `short_px` and selling the bought leg at `long_px` (per share). Never on the net spread
    value. None when a leg's premium is unknown."""
    if short_px is None or long_px is None:
        return None
    total = CostBreakdown()
    add_costs(total, costs.options(pos.lots, max(short_px, 0.0) * pos.shares, 0.0))
    add_costs(total, costs.options(pos.lots, 0.0, max(long_px, 0.0) * pos.shares))
    return float(total.total)


def leg_prices(ctx: OptionContext, pos: SpreadPosition) -> tuple[float | None, float | None]:
    """(sold leg, bought leg) premiums of the open spread from the context's quotes, None when unknown."""
    qs = ctx.quotes.quote(pos.expiry, pos.short_strike, pos.right)
    ql = ctx.quotes.quote(pos.expiry, pos.long_strike, pos.right)
    return (qs.price(ctx.historical) if qs is not None else None, ql.price(ctx.historical) if ql is not None else None)


def roll_money(costs, pos: SpreadPosition, old_short: float, old_long: float, replacement: Signal,
               new_short: float, new_long: float) -> dict:
    """Roll net credit in rupees from the four EXECUTION prices (slippage already in them):
        new spread credit - debit to close the old spread - all roll charges
    Charges cover buying back the old sold leg, selling the old bought leg, selling the new sold leg
    and buying the new bought leg."""
    sold = next(leg for leg in replacement.legs if leg.action == "SELL")
    bought = next(leg for leg in replacement.legs if leg.action == "BUY")
    new_shares = sold.lots * sold.lot_size
    charges = CostBreakdown()
    add_costs(charges, costs.options(pos.lots, old_short * pos.shares, old_long * pos.shares))
    add_costs(charges, costs.options(sold.lots, new_long * new_shares, new_short * new_shares))
    new_credit = (new_short - new_long) * new_shares
    close_debit = (old_short - old_long) * pos.shares
    width_inr = abs(sold.strike - bought.strike) * new_shares
    net = new_credit - close_debit - float(charges.total)
    return {"new_credit_inr": round(new_credit, 2), "close_debit_inr": round(close_debit, 2),
            "roll_charges_inr": float(charges.total), "roll_net_credit_inr": round(net, 2),
            "replacement_width_inr": round(width_inr, 2),
            "roll_net_credit_pct_of_width": round(net / width_inr * 100, 2) if width_inr > 0 else None}


def roll_net_credit(ctx: OptionContext, pos: SpreadPosition, replacement: Signal) -> dict | None:
    """roll_money with the prices of this moment's quotes: each of the four legs is priced once at
    its execution price with adverse slippage (live: sell at the bid, buy at the ask; history: the
    last traded price moved against us by the options slippage). None when a leg of the open spread
    has no usable price: unknown is never treated as a pass."""
    qs = ctx.quotes.quote(pos.expiry, pos.short_strike, pos.right)
    ql = ctx.quotes.quote(pos.expiry, pos.long_strike, pos.right)
    buy_back = qs.buy_price(ctx.historical) if qs is not None else None
    sell_hedge = ql.sell_price(ctx.historical) if ql is not None else None
    if buy_back is None or sell_hedge is None:
        return None

    def px(price: float, side: str) -> float:
        return float(ctx.costs.fill_price("options", price, side)) if ctx.historical else float(price)

    sold = next(leg for leg in replacement.legs if leg.action == "SELL")
    bought = next(leg for leg in replacement.legs if leg.action == "BUY")
    return roll_money(ctx.costs, pos, px(buy_back, "BUY"), px(sell_hedge, "SELL"), replacement,
                      px(sold.price, "SELL"), px(bought.price, "BUY"))


def trading_days_to(today: date, expiry: date, is_trading_day=None) -> int:
    """Trading days after `today` up to and including `expiry` (weekdays, less the holidays the
    caller's calendar knows)."""
    n, d = 0, today
    while d < expiry:
        d += timedelta(days=1)
        if d.weekday() < 5 and (is_trading_day is None or is_trading_day(d)):
            n += 1
    return n


def stuck_position(pos: SpreadPosition, short_price: float | None, spot: float | None, today: date, p,
                   is_trading_day=None) -> str | None:
    """Why rolling this spread would earn little, or None. Stuck = the sold option is in the money and
    its time value is below `roll.stuck_time_value_pct` of its price (a roll would mostly pay
    intrinsic value), or the position is inside the forced-exit window
    (`forced_exit_trading_days_before_expiry`). `p`: the stock_option_selling settings."""
    roll_cfg = p.roll
    left = trading_days_to(today, pos.expiry, is_trading_day)
    if left <= p.forced_exit_trading_days_before_expiry:
        return (f"{left} trading day(s) to expiry, inside the {p.forced_exit_trading_days_before_expiry}-trading-day "
                f"forced-exit window")
    if short_price is None or spot is None or short_price <= 0:
        return None
    intrinsic = max(0.0, pos.short_strike - spot) if pos.right == "PE" else max(0.0, spot - pos.short_strike)
    if intrinsic <= 0:
        return None
    tv_pct = max(0.0, short_price - intrinsic) / short_price * 100
    if tv_pct < roll_cfg.stuck_time_value_pct:
        return (f"the sold {pos.short_strike:g} {pos.right} is in the money and only {tv_pct:.1f}% of its price "
                f"{fmt(short_price)} is time value (under {roll_cfg.stuck_time_value_pct:g}%)")
    return None


STUCK_ACTION = "exit; rolling earns little"


def roll_choices(strategy: StockOptionSelling, ctx: OptionContext, pos: SpreadPosition, close_cost: float,
                 rolls_so_far: int = 0, realised_so_far_inr: float = 0.0, unlimited: bool = False,
                 today: date | None = None) -> dict:
    """Exit or roll for a threatened spread, with numbers. Exit: the cost to close now and the realised
    P&L (always shown, whatever is unknown about a roll). A roll is suggested only when all hold:
    - the position is not stuck (stuck_position);
    - fewer than `roll.max_rolls` rolls have been completed in this campaign;
    - the replacement spread has the same or a later expiry and passes EVERY entry rule as a new
      trade would (trend, IV rank, events, falling-stock block, campaign limit, strikes, hedge, max
      loss, liquidity, entry window, minimum credit, margin), on the same side;
    - its sold strike is strictly lower (put spread) or strictly higher (call spread) than the old one;
    - roll net credit = new spread credit - debit to close the old spread - all roll charges
      (roll_net_credit), and roll net credit / (replacement width x lot size x lots) is above
      `roll.min_net_credit_pct_of_width` / 100. A positive new premium alone does not qualify.
    Otherwise the action is EXIT and `roll_note` says why. The replacement is one lot: a roll never
    raises the size. `campaign_after_roll` shows the best the campaign could still end at; if that
    is a loss it says "recovery not possible from this position".
    `unlimited` (research-only backtest policy "roll every month"): the next month's spread by the
    structure rules alone, with none of the conditions above; it never reaches a live signal."""
    p = strategy.params
    today = today or ctx.asof.astimezone(IST).date()
    # closing charges on each leg's own premium (buy back the sold leg, sell the bought leg), never on
    # the net spread value; a leg without a price leaves the charges and the realised figure unknown
    exit_costs = closing_charges(ctx.costs, pos, *leg_prices(ctx, pos))
    realised = None if exit_costs is None else round((pos.credit - close_cost) * pos.shares - exit_costs, 2)
    out = {"action": "EXIT",
           "exit": {"cost_to_close_per_share": round(close_cost, 2), "cost_to_close_inr": round(close_cost * pos.shares, 2),
                    "closing_charges_inr": None if exit_costs is None else round(exit_costs, 2),
                    "realised_pnl_inr": realised,
                    "campaign_pnl_inr": None if realised is None else round(realised_so_far_inr + realised, 2),
                    "note": "after estimated closing charges on each leg's premium" if exit_costs is not None
                            else "closing charges unknown: a leg has no usable price"},
           "roll": None, "roll_note": "", "roll_signal": None, "refused_by": None}

    def exit_only(code: str, why: str) -> dict:
        out["roll_note"], out["refused_by"] = why, code
        return out

    if unlimited:
        mode, expiries = "any", [e for e in ctx.expiries if e.expiry > pos.expiry][:1]
    else:
        q = ctx.quotes.quote(pos.expiry, pos.short_strike, pos.right)
        stuck = stuck_position(pos, q.price(ctx.historical) if q is not None else None, ctx.spot, today, p)
        if stuck:
            return exit_only("STUCK_POSITION", f"{STUCK_ACTION}: {stuck}")
        if rolls_so_far >= p.roll.max_rolls:
            return exit_only("MAX_ROLLS", f"exit: this campaign has already been rolled {rolls_so_far} time(s) "
                                          f"(max_rolls {p.roll.max_rolls})")
        mode, expiries = "", [e for e in ctx.expiries if e.expiry >= pos.expiry]
    best, money, why_not, codes = None, None, [], []
    for exp in expiries:
        a = strategy.assess(replace(ctx, expiries=(exp,), rolling=mode, no_filter=False, max_lots=pos.lots,
                                    baseline_right=pos.right if unlimited else None))
        if a.signal is None:
            why_not += [r for r in strategy.last_no_trade if r not in why_not]
            codes += [r.split(":")[0] for r in strategy.last_no_trade]
            continue
        short, d = next(leg for leg in a.signal.legs if leg.action == "SELL"), a.signal.details
        if unlimited:
            best = a.signal
            break
        if short.right != pos.right:
            why_not.append(f"{exp.expiry:%d-%b-%Y}: the entry rules now give a {SIDE_NAME[short.right]}, not a "
                           f"{SIDE_NAME[pos.right]}")
            codes.append("OTHER_SIDE")
            continue
        moved = short.strike < pos.short_strike if pos.right == "PE" else short.strike > pos.short_strike
        if not moved:
            why_not.append(f"{exp.expiry:%d-%b-%Y}: the rules give the sold strike {short.strike:g}, not "
                           f"{'below' if pos.right == 'PE' else 'above'} the current {pos.short_strike:g}")
            codes.append("STRIKE_NOT_BETTER")
            continue
        m = roll_net_credit(ctx, pos, a.signal)
        if m is None:
            why_not.append("the open spread has no usable price for a leg: the roll cannot be priced")
            codes.append("ROLL_PRICE_UNKNOWN")
            continue
        if not (m["roll_net_credit_pct_of_width"] is not None
                and m["roll_net_credit_pct_of_width"] > p.roll.min_roll_net_credit_pct_of_width):
            why_not.append(f"{exp.expiry:%d-%b-%Y}: roll net credit Rs {fmt(m['roll_net_credit_inr'])} (new credit Rs "
                           f"{fmt(m['new_credit_inr'])} - close Rs {fmt(m['close_debit_inr'])} - charges Rs "
                           f"{fmt(m['roll_charges_inr'])}) is {m['roll_net_credit_pct_of_width']}% of the replacement "
                           f"width, not above {p.roll.min_roll_net_credit_pct_of_width:g}%")
            codes.append("ROLL_NET_CREDIT_TOO_SMALL")
            continue
        best, money = a.signal, m
        break
    if best is None:
        return exit_only(codes[0] if codes else "NO_EXPIRY",
                         "exit: no roll passes the rules" + (": " + "; ".join(why_not[:4]) if why_not else
                                                             ": no expiry on or after the current one to roll into"))
    d = best.details
    out.update({"action": "ROLL", "roll_signal": best, "roll": {
        "legs": [leg.to_dict() for leg in best.legs], "expiry": d["expiry"], "roll_no": rolls_so_far + 1,
        "new_credit_per_share": d["net_credit"], "hedge_cost_inr": d["hedge_cost_inr"],
        **(money or {}),
        "new_max_loss_inr": d["max_loss_inr"], "new_breakevens": d["breakevens"], "margin_inr": d["margin_inr"],
        "campaign_after_roll": (after_roll(realised_so_far_inr, realised, d["max_profit_inr"]) if realised is not None
                                else None)}})
    return out


def position_alerts(strategy: StockOptionSelling, ctx: OptionContext, pos: SpreadPosition, close_cost: float | None,
                    prev: Snapshot | None, now: Snapshot | None, rolls_so_far: int = 0,
                    realised_so_far_inr: float = 0.0) -> list[dict]:
    """Alerts for an open spread: what changed since the previous close (stock_profile.alert_kinds),
    each with the exit and roll choices in numbers. With no usable price for the legs the choices
    say so instead of showing numbers. Phase 6 sends these (Telegram) and repeats them."""
    changes = list(alert_kinds(prev, now, pos.right, strategy.params.reversal.alerts_approved))
    q = ctx.quotes.quote(pos.expiry, pos.short_strike, pos.right)
    stuck = stuck_position(pos, q.price(ctx.historical) if q is not None else None, ctx.spot,
                           ctx.asof.astimezone(IST).date(), strategy.params)
    if stuck:
        changes.append({"kind": "STUCK_POSITION", "what_changed": stuck})
    if not changes:
        return []
    if close_cost is None:
        choices = {"action": "EXIT", "exit": None, "roll": None,
                   "roll_note": "no usable price for one of the legs: exit and roll cannot be priced"}
    else:
        choices = roll_choices(strategy, ctx, pos, close_cost, rolls_so_far, realised_so_far_inr)
        choices.pop("roll_signal", None)
    return [{**c, "underlying": pos.underlying, "position": f"{SIDE_NAME[pos.right]} {pos.short_strike:g}/"
             f"{pos.long_strike:g} {pos.expiry:%d-%b-%Y}", **choices} for c in changes]


def phase9_settings(settings):
    """The settings every Phase 9 output uses (backtests and the research pages): the same file, with
    the capital set to the user's stock-option capital (stock_option_selling.backtest.capital_inr).
    Every limit stays a % of it."""
    cap = settings.stock_option_selling.backtest.capital_inr
    if settings.account.capital_inr == cap:
        return settings
    return settings.model_copy(update={"account": settings.account.model_copy(update={"capital_inr": cap})})


def build_stock_selling(settings) -> StockOptionSelling:
    """The strategy with its thresholds from config.yaml (`stock_option_selling`) and its status."""
    st = StockOptionSelling(settings.stock_option_selling)
    status = settings.strategies.status_of(st.id)
    st.alerts_enabled, st.mode, st.status_label = status.alerts, status.mode, status.label
    return st
