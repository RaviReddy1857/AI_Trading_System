"""Iron condor on NIFTY weekly options: a bull put spread and a bear call spread, same expiry.

RANGE regime only, so never on an EVENT (or VOLATILE, trending or UNKNOWN) day. Only for an
underlying whose config lists weekly expiries (NIFTY). The other rules are the credit spread's:
IV rank at or above `min_iv_rank`; both sold strikes at |delta| <= `max_short_delta` and beyond the
highest-OI strike when that is known; each sold option hedged `hedge_width_pct` further out; margin
known; exits at the profit target, the stop, or `exit_days_before_expiry` before expiry.

The expiry is the nearest contract `entry_min_dte`..`entry_max_dte` days away, weekly or monthly
(in the week of the monthly expiry there is no separate weekly contract).
"""
from __future__ import annotations

from ..config import IronCondorParams
from .base import OptionContext, round_trip_costs
from .credit_spread import CreditSpread, choose_width
from .payoff import InvalidStructure, iron_condor


class IronCondor(CreditSpread):
    id = "iron_condor"
    base_version = "1"
    horizon = "WEEKLY"
    allowed_regimes = frozenset({"RANGE"})
    structure = "IRON_CONDOR"
    expiry_kinds = ("weekly", "monthly")
    params: IronCondorParams

    def _evaluate(self, ctx: OptionContext):
        p = self.params
        if not self.entry_checks(ctx):
            return []
        if "weekly" not in ctx.allowed_kinds:
            return self.no("NO_WEEKLY_OPTIONS", f"{ctx.underlying} has no weekly contracts configured")
        exp = self.pick(ctx)
        if exp is None:
            return []
        sides = choose_width(self, ctx, exp, p, ["PE", "CE"])   # same width both sides; one lot within the limit
        if sides is None:
            return []
        put, call = sides
        legs = put[0] + call[0]
        views = put[1] + call[1]
        credit, width = put[2] + call[2], max(put[3], call[3])
        try:
            pay = iron_condor(put[0][0].strike, put[0][1].strike, call[0][0].strike, call[0][1].strike, credit,
                              exp.lot_size, 1, round_trip_costs(ctx.costs, legs).total)
        except InvalidStructure as e:
            return self.no("NO_CREDIT", str(e))
        if ctx.no_filter:
            why = ["no-filter baseline: iron condor on a fixed schedule"]
        else:
            why = [f"regime {ctx.regime}: range-bound market, no event day",
                   f"IV rank {ctx.iv_rank:.0f} at or above {p.min_iv_rank:g} (premium is rich)"]
        why.append(f"sold strikes {put[0][0].strike:g} PE (delta {views[0]['delta']}) and {call[0][0].strike:g} CE "
                   f"(delta {views[2]['delta']}), limit {p.max_short_delta:g}; " + "; ".join(dict.fromkeys(put[4] + call[4])))
        return self.finish(ctx, exp, legs, views, credit, width, pay, why, "iron condor")
