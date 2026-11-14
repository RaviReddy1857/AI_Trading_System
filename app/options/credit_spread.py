"""Hedged credit spread on an index (NIFTY, BANKNIFTY), monthly expiry.

TRENDING_UP -> bull put spread (sell a put below the market, buy a put further below).
TRENDING_DOWN -> bear call spread (sell a call above, buy a call further above). Rules:
- index options only: selling stock options returns NO_TRADE in this phase (phase 9 adds it)
- sell only when IV rank is high (at or above `min_iv_rank`); unknown IV rank = no signal
- expiry: the monthly contract with `entry_min_dte`..`entry_max_dte` calendar days left
- sold strike: nearest the money with |delta| <= `max_short_delta` (delta from the strike's own
  price) and, when chain-wide open interest is known, beyond the highest-OI strike on that side
- bought strike: same expiry, about `hedge_width_pct` of spot further out. Never a sold option alone
- credit must be at least `min_credit_pct_of_width` of the strike width
- margin for the spread must be known (entered by the user), else MARGIN_UNKNOWN
Exits (applied by the backtest; phase 6 turns them into exit calls): keep
`profit_target_pct_of_credit` of the credit, stop when buying back costs `stop_multiple_of_credit`
x the credit, and out `exit_days_before_expiry` days before expiry at the latest.

Signal prices are per share of the SPREAD: entry = net credit, target and stop = cost to buy it back.
"""
from __future__ import annotations

from ..config import CreditSpreadParams
from ..strategies.base import fmt
from .base import (OptionContext, OptionStrategy, leg_view, liquidity_problem, pick_expiry, pick_hedge_strike,
                   pick_short_strike, round_trip_costs)
from .greeks import days_to_expiry
from .margin import MARGIN_UNKNOWN
from .payoff import InvalidStructure, OptionLeg, credit_spread

SIDE_BY_REGIME = {"TRENDING_UP": "PE", "TRENDING_DOWN": "CE"}
SIDE_NAME = {"PE": "bull put spread", "CE": "bear call spread"}


def build_side(strategy: OptionStrategy, ctx: OptionContext, exp, right: str, p: CreditSpreadParams):
    """One vertical credit spread: (legs, views, credit, width, notes), or None after recording why not."""
    got = pick_short_strike(ctx, exp.expiry, right, p.max_short_delta)
    if got[0] is None:
        strategy.no("NO_STRIKE", got[1])
        return None
    ks, qs, iv_s, d_s, notes = got
    hedge = pick_hedge_strike(ctx, exp.expiry, right, ks, p.hedge_width_pct)
    if hedge is None:
        strategy.no("UNHEDGED_SHORT", f"no usable hedge strike beyond {ks:g}{right}: a sold option is never left alone")
        return None
    kl, ql = hedge
    for q in (qs, ql):
        lp = liquidity_problem(ctx, q, exp.lot_size)
        if lp:
            strategy.no("ILLIQUID", lp)
            return None
    sell, buy = qs.sell_price(ctx.historical), ql.buy_price(ctx.historical)
    if sell is None or buy is None:
        strategy.no("DATA_MISSING", f"no usable price for {ks:g}/{kl:g} {right}")
        return None
    credit, width = sell - buy, abs(ks - kl)
    if credit <= 0 or credit >= width:
        strategy.no("NO_CREDIT", f"{ks:g}/{kl:g} {right}: net credit {credit:.2f} on width {width:g}")
        return None
    legs = [OptionLeg("SELL", right, ks, exp.expiry, 1, round(sell, 2), exp.lot_size),
            OptionLeg("BUY", right, kl, exp.expiry, 1, round(buy, 2), exp.lot_size)]
    from .base import metrics
    ml = metrics(ctx, exp.expiry, kl, right)
    views = [leg_view(legs[0], iv_s, d_s, qs), leg_view(legs[1], ml[1] if ml else None, ml[2] if ml else None, ql)]
    return legs, views, round(sell, 2) - round(buy, 2), width, notes


class CreditSpread(OptionStrategy):
    id = "credit_spread"
    base_version = "1"
    horizon = "MONTHLY"
    allowed_regimes = frozenset(SIDE_BY_REGIME)
    structure = "CREDIT_SPREAD"
    expiry_kinds: tuple[str, ...] = ("monthly",)
    params: CreditSpreadParams

    def entry_checks(self, ctx: OptionContext) -> bool:
        """Entry rules shared with the iron condor. False after recording the reason."""
        p = self.params
        if ctx.kind != "index":
            self.no("STOCK_OPTION_SELLING_NOT_ALLOWED",
                    "stock options are buy-only in this phase (physical settlement); hedged stock spreads arrive in phase 9")
            return False
        if ctx.no_filter:
            return True
        if ctx.regime not in self.allowed_regimes:
            self.no("OUTSIDE_REGIME", f"regime {ctx.regime} not in {sorted(self.allowed_regimes)}")
            return False
        if ctx.iv_rank is None:
            self.no("IV_RANK_UNKNOWN", "not enough implied-volatility history")
            return False
        if ctx.iv_rank < p.min_iv_rank:
            self.no("IV_RANK_TOO_LOW", f"IV rank {ctx.iv_rank:.0f} below {p.min_iv_rank:g}: premium is thin")
            return False
        return True

    def pick(self, ctx: OptionContext):
        p = self.params
        exp = pick_expiry(ctx, p.entry_min_dte, p.entry_max_dte, self.expiry_kinds)
        if exp is None:
            self.no("NO_EXPIRY", f"no {'/'.join(self.expiry_kinds)} expiry {p.entry_min_dte}-{p.entry_max_dte} days away")
            return None
        if not exp.lot_size:
            self.no("LOT_SIZE_UNKNOWN", f"{ctx.underlying} {exp.expiry}: lot size not in the instrument list")
            return None
        return exp

    def finish(self, ctx: OptionContext, exp, legs, views, credit: float, width: float, pay, why: list[str],
               structure_name: str):
        p = self.params
        if not ctx.no_filter and credit < width * p.min_credit_pct_of_width / 100:
            return self.no("CREDIT_TOO_SMALL", f"credit {credit:.2f} is under {p.min_credit_pct_of_width:g}% of the "
                                               f"{width:g}-point width")
        lots = legs[0].lots
        per_lot = ctx.margin(self.structure, width) if ctx.margin is not None else None
        margin = None if per_lot is None else per_lot * lots
        if margin is None and ctx.require_margin:
            return self.no(MARGIN_UNKNOWN, f"enter the margin for one lot of {ctx.underlying} {self.structure} "
                                           f"width {width:g} from ICICI's margin calculator (Options page)")
        target = credit * (1 - p.profit_target_pct_of_credit / 100)
        stop = credit * p.stop_multiple_of_credit
        dte = days_to_expiry(ctx.asof, exp.expiry)
        details = {
            **pay.as_dict(ctx.capital_inr), "expiry": exp.expiry.isoformat(), "expiry_kind": exp.kind,
            "days_to_expiry": dte, "lot_size": exp.lot_size, "lots": lots, "premium": round(credit, 2),
            "net_credit": round(credit, 2), "total_cost_inr": None,
            "margin_inr": margin, "margin_per_lot_inr": per_lot,
            "margin_note": f"margin for {lots} lot(s) from your per-lot entry" if margin is not None
            else "margin not known for this date (backtest)",
            "iv_rank": ctx.iv_rank, "price_basis": "last traded (history)" if ctx.historical else "sell at bid, buy at ask",
            "legs": views, "warnings": [],
            "exit_rules": {"buy_back_at_or_below": round(target, 2), "stop_buy_back_at_or_above": round(stop, 2),
                           "exit_days_before_expiry": p.exit_days_before_expiry},
        }
        why = why + [
            f"{structure_name} on {ctx.underlying} {exp.expiry:%d-%b-%Y} ({dte} days): "
            + ", ".join(f"{leg.action.lower()} {leg.strike:g} {leg.right} at {fmt(leg.price)}" for leg in legs),
            f"net credit {fmt(credit)} per share x {lots} lot(s) of {exp.lot_size} = Rs {fmt(float(pay.net_premium_inr))}; "
            f"max loss Rs {fmt(float(pay.max_loss_inr))} ({details['max_loss_pct_of_capital']}% of capital); "
            f"breakeven {', '.join(fmt(float(b)) for b in pay.breakevens) or 'none: costs exceed the credit'}",
            f"exit: buy back at {fmt(target)} or lower ({p.profit_target_pct_of_credit:g}% of credit kept), "
            f"stop at {fmt(stop)} ({p.stop_multiple_of_credit:g}x credit), out {p.exit_days_before_expiry} day(s) before expiry"]
        return [self.option_signal(ctx, "SPREAD", "WEEKLY" if exp.kind == "weekly" else "MONTHLY",
                                   credit, stop, target, why, legs, details)]

    def _evaluate(self, ctx: OptionContext):
        p = self.params
        if not self.entry_checks(ctx):
            return []
        right = ctx.baseline_right if ctx.no_filter else SIDE_BY_REGIME[ctx.regime]
        if right not in ("PE", "CE"):
            return self.no("NO_SIDE", "baseline needs a side")
        exp = self.pick(ctx)
        if exp is None:
            return []
        side = build_side(self, ctx, exp, right, p)
        if side is None:
            return []
        legs, views, credit, width, notes = side
        try:
            pay = credit_spread(right, legs[0].strike, legs[1].strike, credit, exp.lot_size, 1,
                                round_trip_costs(ctx.costs, legs).total)
        except InvalidStructure as e:
            return self.no("NO_CREDIT", str(e))
        if ctx.no_filter:
            why = [f"no-filter baseline: {SIDE_NAME[right]} on a fixed schedule"]
        else:
            why = [f"regime {ctx.regime}: {SIDE_NAME[right]}",
                   f"IV rank {ctx.iv_rank:.0f} at or above {p.min_iv_rank:g} (premium is rich)"]
        why.append(f"sold strike {legs[0].strike:g}: delta {views[0]['delta']} (limit {p.max_short_delta:g}); "
                   + "; ".join(notes))
        return self.finish(ctx, exp, legs, views, credit, width, pay, why, SIDE_NAME[right])
