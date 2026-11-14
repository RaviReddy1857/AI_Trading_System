"""Phase 9 position sizing: whole lots within the common risk budget per position.

lots = the largest whole number n with max loss(n lots) <= budget, where max loss includes every
charge for n lots and budget = max_risk_per_position_pct of capital. Rounded down, never up. Zero
lots means no actionable signal (ZERO_SIZE). A roll's replacement is never larger than the
position it replaces (`max_lots`), so no loss is ever chased with a bigger position. Margin is not
part of sizing: affordability is advisory (capital-fit label), the risk budget is a hard limit.
"""
from __future__ import annotations

import math
from decimal import Decimal

from .base import round_trip_costs
from .payoff import OptionLeg, credit_spread, debit_spread

ZERO_SIZE = "ZERO_SIZE"


def _spread(kind: str, legs_one: list[OptionLeg], n: int, lot_size: int, costs):
    legs = [OptionLeg(x.action, x.right, x.strike, x.expiry, n, x.price, x.lot_size) for x in legs_one]
    c = round_trip_costs(costs, legs).total
    sold = next(x for x in legs if x.action == "SELL")
    bought = next(x for x in legs if x.action == "BUY")
    net = round(sold.price, 2) - round(bought.price, 2)
    if kind == "credit":
        pay = credit_spread(sold.right, sold.strike, bought.strike, net, lot_size, n, c)
    else:
        pay = debit_spread(bought.right, bought.strike, sold.strike, -net, lot_size, n, c)
    return legs, pay


def size_spread(kind: str, legs_one: list[OptionLeg], lot_size: int, costs, budget_inr: float,
                max_lots: int | None = None):
    """(lots, legs for that many lots, payoff) of a credit or debit spread, or (0, None, payoff for one
    lot) when even one lot's cost-inclusive max loss is above the budget. `legs_one`: the legs for one
    lot. `max_lots`: a cap (a roll never grows the position)."""
    legs1, pay1 = _spread(kind, legs_one, 1, lot_size, costs)
    one = float(pay1.max_loss_inr)
    if one <= 0 or one > budget_inr:
        return 0, None, pay1
    n = max(1, math.floor(Decimal(str(budget_inr)) / Decimal(str(one))))
    if max_lots is not None:
        n = min(n, max_lots)
    while n > 1:
        legs, pay = _spread(kind, legs_one, n, lot_size, costs)
        if float(pay.max_loss_inr) <= budget_inr:
            return n, legs, pay
        n -= 1
    return 1, legs1, pay1
