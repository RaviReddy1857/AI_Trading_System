"""Phase 5 position sizing. Every figure is computed here by code; nothing is estimated elsewhere.

Cash (intraday and swing):
    quantity = floor(budget / (entry - stop)), budget = capital x risk.max_loss_per_trade_pct x size factor,
    capped so the position value stays within backtest.position_cap_pct of capital (x size factor).
    The formula and the cap are `app.backtest.position_size` (the phase 3 backtest uses the same one).
    The live risk is then made cost-inclusive: the largest quantity whose loss at the stop PLUS the
    estimated charges and slippage of the round trip stays within the budget. Rounded down, never up.

Options:
    lots = floor(budget / max loss of one lot), budget = capital x risk.max_loss_per_option_pct x size
    factor (stock option spreads of phase 9: stock_option_selling.max_risk_per_position_pct), where the
    max loss is cost-inclusive. The n-lot max loss is then recomputed with the charges for n lots and n
    is lowered until it fits. Two-leg spreads go through `app.options.sizing.size_spread` (phase 9's
    sizing); a single bought option and an iron condor use the same rule on `app.options.payoff`.
    Zero lots = ZERO_SIZE.

The size factor comes from the drawdown rule (1.0 or 0.5); it only ever lowers a size.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from decimal import Decimal

from ..backtest import position_size
from ..options.base import round_trip_costs
from ..options.payoff import InvalidStructure, OptionLeg, Payoff, iron_condor, long_option
from ..options.sizing import ZERO_SIZE, size_spread

__all__ = ["ZERO_SIZE", "cash_quantity", "option_lots", "lots_within"]


def _d(x) -> Decimal:
    return Decimal(str(x))


def cash_quantity(capital: float, risk_pct: float, cap_pct: float, entry: float, stop: float,
                  cost_inr: Callable[[int], Decimal] | None = None, size_factor: float = 1.0,
                  held_risk_inr: float = 0.0) -> tuple[int, dict]:
    """(quantity, facts). `cost_inr(q)`: estimated charges and slippage of the round trip for q shares
    (None = no costs, used by the hand-calculation tests). `held_risk_inr`: risk already held in
    positions that count as the same position (correlated group); it is taken off the budget.
    0 = ZERO_SIZE."""
    if not (0 < size_factor <= 1):
        raise ValueError("size_factor must be in (0, 1]: sizing may only be reduced")
    base = capital * size_factor
    by_formula = position_size(base, risk_pct, cap_pct, entry, stop)
    if held_risk_inr < 0:
        raise ValueError("held_risk_inr cannot be negative")
    budget = _d(capital) * _d(risk_pct) / 100 * _d(size_factor) - _d(held_risk_inr)
    per_share = abs(_d(entry) - _d(stop))
    facts = {"budget_inr": float(round(budget, 2)), "risk_per_share": float(per_share),
             "by_formula": by_formula, "cap_value_inr": round(base * cap_pct / 100, 2)}
    if by_formula <= 0 or budget <= 0:
        return 0, facts

    def fits(q: int) -> bool:                    # exact decimal check, so float rounding never adds a share
        return q * per_share + (_d(cost_inr(q)) if cost_inr is not None else 0) <= budget

    lo, hi = 0, by_formula                       # largest q in [0, by_formula] that fits (costs grow with q)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    facts["after_costs"] = lo
    return lo, facts


def lots_within(budget_inr: float, loss_for: Callable[[int], Decimal], max_lots: int | None = None) -> int:
    """The largest whole n with loss_for(n) <= budget: floor(budget / loss_for(1)), then lowered while the
    recomputed n-lot loss (charges for n lots) is above the budget. 0 when one lot does not fit."""
    one = _d(loss_for(1))
    budget = _d(budget_inr)
    if one <= 0 or one > budget:
        return 0
    n = int(math.floor(budget / one))
    if max_lots is not None:
        n = min(n, max_lots)
    while n > 1 and _d(loss_for(n)) > budget:
        n -= 1
    return n


def _scaled(legs: list[OptionLeg], n: int) -> list[OptionLeg]:
    return [OptionLeg(x.action, x.right, x.strike, x.expiry, n, x.price, x.lot_size) for x in legs]


def _long_payoff(leg: OptionLeg, n: int, costs) -> Payoff:
    legs = _scaled([leg], n)
    return long_option(leg.right, leg.strike, leg.price, leg.lot_size, n, round_trip_costs(costs, legs).total)


def _condor_payoff(legs1: list[OptionLeg], n: int, costs) -> Payoff:
    def one(action, right):
        return next(x for x in legs1 if x.action == action and x.right == right)
    ps, pl, cs, cl = one("SELL", "PE"), one("BUY", "PE"), one("SELL", "CE"), one("BUY", "CE")
    credit = round(ps.price, 2) + round(cs.price, 2) - round(pl.price, 2) - round(cl.price, 2)
    legs = _scaled(legs1, n)
    return iron_condor(ps.strike, pl.strike, cs.strike, cl.strike, credit, ps.lot_size, n,
                       round_trip_costs(costs, legs).total)


def option_lots(legs, costs, budget_inr: float, max_lots: int | None = None) -> tuple[int, Payoff | None, str]:
    """(lots, payoff for that many lots - or for one lot when 0 -, structure). `legs`: the signal's
    option legs (any lot count; sized again from one lot). Raises ValueError for a structure it does not
    know (the engine turns that into NO_TRADE)."""
    legs1 = _scaled([x for x in legs], 1)
    if not legs1 or len({x.lot_size for x in legs1}) != 1 or not all(x.lot_size and x.lot_size > 0 for x in legs1):
        raise ValueError("lot size unknown or different between legs")
    lot_size = legs1[0].lot_size
    actions = sorted(x.action for x in legs1)
    if len(legs1) == 1 and actions == ["BUY"]:
        n = lots_within(budget_inr, lambda k: _long_payoff(legs1[0], k, costs).max_loss_inr, max_lots)
        return n, _long_payoff(legs1[0], max(n, 1), costs), "LONG_OPTION"
    if len(legs1) == 2 and actions == ["BUY", "SELL"]:
        sold = next(x for x in legs1 if x.action == "SELL")
        bought = next(x for x in legs1 if x.action == "BUY")
        kind = "credit" if round(sold.price, 2) > round(bought.price, 2) else "debit"
        try:
            n, _, pay = size_spread(kind, legs1, lot_size, costs, budget_inr, max_lots=max_lots)
        except InvalidStructure as e:
            raise ValueError(str(e)) from e
        return n, pay, "CREDIT_SPREAD" if kind == "credit" else "DEBIT_SPREAD"
    if len(legs1) == 4 and actions == ["BUY", "BUY", "SELL", "SELL"]:
        try:
            n = lots_within(budget_inr, lambda k: _condor_payoff(legs1, k, costs).max_loss_inr, max_lots)
            return n, _condor_payoff(legs1, max(n, 1), costs), "IRON_CONDOR"
        except (InvalidStructure, StopIteration) as e:
            raise ValueError(f"not an iron condor: {e}") from e
    raise ValueError(f"unknown option structure with {len(legs1)} legs")
