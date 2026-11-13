"""Directional option buying (index and stock options).

Trigger: a signal from one of `trigger_strategies` on the underlying itself (intraday or swing).
LONG trigger -> buy a call, SHORT trigger -> buy a put. Rules:
- buy only when IV rank is low or moderate (at or below `max_iv_rank`); unknown IV rank = no signal
- expiry: the nearest allowed expiry at least `min_days_to_expiry` away; a stock option must also
  leave room before expiry week (stock options settle physically)
- strike: the listed strike nearest the underlying's price that is liquid (bid/ask width and open interest)
- stop and target are percentages of the premium; max loss = premium paid + costs
Stock options are BUY-ONLY in this phase, and every stock option signal carries the warning to be
out before expiry week.

The no-filter baseline (backtests) buys the same at-the-money call on a fixed schedule with no
trigger and no IV rule.
"""
from __future__ import annotations

from datetime import timedelta

from ..config import LongOptionParams
from ..strategies.base import fmt
from .base import OptionContext, OptionStrategy, leg_view, liquidity_problem, metrics, pick_expiry, round_trip_costs
from .greeks import days_to_expiry
from .payoff import OptionLeg, long_option


class LongOption(OptionStrategy):
    id = "long_option"
    base_version = "1"
    horizon = "SWING"
    # No regime rule of its own: the trigger strategy already has one.
    allowed_regimes = frozenset({"TRENDING_UP", "TRENDING_DOWN", "RANGE", "VOLATILE"})
    params: LongOptionParams

    def exit_before(self, ctx: OptionContext) -> int:
        p = self.params
        return p.stock_exit_days_before_expiry if ctx.kind == "stock" else p.index_exit_days_before_expiry

    def _evaluate(self, ctx: OptionContext):
        p = self.params
        if ctx.no_filter:
            right, why = "CE", ["no-filter baseline: at-the-money call bought on a fixed schedule"]
        else:
            t = ctx.trigger
            if t is None:
                return self.no("NO_TRIGGER", "no signal from a trigger strategy on the underlying")
            if t.strategy_id not in p.trigger_strategies or t.symbol != ctx.underlying or t.direction not in ("LONG", "SHORT"):
                return self.no("NO_TRIGGER", f"signal {t.strategy_id} on {t.symbol} is not a configured trigger")
            if ctx.regime == "EVENT" or ctx.regime == "UNKNOWN":
                return self.no("OUTSIDE_REGIME", f"regime {ctx.regime}")
            if ctx.iv_rank is None:
                return self.no("IV_RANK_UNKNOWN", "not enough implied-volatility history")
            if ctx.iv_rank > p.max_iv_rank:
                return self.no("IV_RANK_TOO_HIGH", f"IV rank {ctx.iv_rank:.0f} above {p.max_iv_rank:g}: options are expensive")
            right = "CE" if t.direction == "LONG" else "PE"
            why = [f"trigger: {t.strategy_id} {t.direction} on {ctx.underlying} ({'; '.join(t.why_now[:2])})",
                   f"IV rank {ctx.iv_rank:.0f} at or below {p.max_iv_rank:g} (options not expensive)"]
        exit_days = self.exit_before(ctx)
        min_dte = max(p.min_days_to_expiry, exit_days + 3) if ctx.kind == "stock" else p.min_days_to_expiry
        exp = pick_expiry(ctx, min_dte, None, ctx.allowed_kinds)
        if exp is None:
            return self.no("NO_EXPIRY", f"no {'/'.join(ctx.allowed_kinds)} expiry at least {min_dte} days away")
        if not exp.lot_size:
            return self.no("LOT_SIZE_UNKNOWN", f"{ctx.underlying} {exp.expiry}: lot size not in the instrument list")
        chosen, problems, illiquid = None, [], False
        for k in ctx.quotes.atm_candidates(exp.expiry):
            m = metrics(ctx, exp.expiry, k, right)
            if m is None:
                problems.append(f"{k:g}{right}: no usable price" + ("" if ctx.historical else " (bid/ask unknown)"))
                continue
            lp = liquidity_problem(ctx, m[0], exp.lot_size)
            if lp:
                illiquid = True
                problems.append(lp)
                continue
            chosen = (k, *m)
            break
        if chosen is None:
            return self.no("ILLIQUID" if illiquid else "DATA_MISSING",
                           "; ".join(problems) or "no strike near the money")
        strike, q, iv, delta = chosen
        premium = q.buy_price(ctx.historical)
        stop = premium * (1 - p.stop_pct_of_premium / 100)
        target = premium * (1 + p.target_pct_of_premium / 100)
        if round(stop, 2) <= 0 or round(stop, 2) >= round(premium, 2):
            return self.no("PREMIUM_TOO_SMALL", f"premium {premium:.2f} leaves no room for a stop")
        leg = OptionLeg("BUY", right, strike, exp.expiry, 1, round(premium, 2), exp.lot_size)
        costs = round_trip_costs(ctx.costs, [leg])
        pay = long_option(right, strike, leg.price, exp.lot_size, 1, costs.total)
        dte = days_to_expiry(ctx.asof, exp.expiry)
        warnings = []
        exit_by = exp.expiry - timedelta(days=exit_days)
        if ctx.kind == "stock":
            warnings.append(f"Stock option: physically settled. Exit by {exit_by.isoformat()} "
                            f"(before expiry week); do not hold into expiry.")
        details = {
            **pay.as_dict(ctx.capital_inr), "expiry": exp.expiry.isoformat(), "expiry_kind": exp.kind,
            "days_to_expiry": dte, "lot_size": exp.lot_size, "lots": 1, "premium": leg.price,
            "total_cost_inr": float(pay.max_loss_inr), "margin_inr": float(-pay.net_premium_inr),
            "margin_note": "option buying: cash needed is the premium, no margin is blocked",
            "iv_rank": ctx.iv_rank, "price_basis": "last traded (history)" if ctx.historical else "ask",
            "legs": [leg_view(leg, iv, delta, q)], "warnings": warnings, "exit_by": exit_by.isoformat(),
            "exit_rules": {"stop_premium": round(stop, 2), "target_premium": round(target, 2),
                           "max_hold_days": p.max_hold_days},
        }
        why += [f"buy {ctx.underlying} {exp.expiry:%d-%b-%Y} {strike:g} {right} at {fmt(leg.price)}: "
                f"nearest liquid strike to {fmt(ctx.spot)}, {dte} days to expiry, lot {exp.lot_size}",
                f"max loss = premium + costs = Rs {fmt(float(pay.max_loss_inr))} per lot; "
                f"stop at {fmt(stop)} ({p.stop_pct_of_premium:g}% of premium), target {fmt(target)}"]
        horizon = ctx.trigger.horizon if ctx.trigger is not None else "SWING"
        return [self.option_signal(ctx, "LONG", horizon, leg.price, stop, target, why + warnings, [leg], details)]
