"""Swing volume breakout, version 2 (phase 3b; daily bars, long only, delivery).

v1's rules (see swing_breakout.py) plus a market filter: NIFTY's close on the signal day must be
above its `index_sma_days`-day average. No signal without enough index history.
"""
from __future__ import annotations

from ..config import SwingBreakoutV2Params
from .base import MarketContext
from .swing_breakout import SwingBreakout
from .swing_pullback_v2 import index_above_average


class SwingBreakoutV2(SwingBreakout):
    id = "swing_breakout_v2"
    base_version = "2"
    needs_index_daily = True
    params: SwingBreakoutV2Params

    def index_ok(self, index_daily) -> bool:
        return index_above_average(index_daily, self.params.index_sma_days)[0]

    def _evaluate(self, ctx: MarketContext):
        out = super()._evaluate(ctx)
        if not out:
            return []
        passes, why = index_above_average(ctx.index_daily, self.params.index_sma_days)
        if not passes:
            self.rejections["index_below_average"] += 1
            return []
        s = out[0]
        return [self.signal(ctx, s.direction, s.entry, s.stop, s.target, s.valid_until,
                            [*s.why_now, f"market filter: {why}"])]
