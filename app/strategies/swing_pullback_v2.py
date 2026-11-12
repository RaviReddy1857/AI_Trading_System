"""Swing trend pullback, version 2 (phase 3b; daily bars, long only, delivery).

v1's rules (see swing_pullback.py) plus a market filter: NIFTY's close on the signal day must be
above its `index_sma_days`-day average. No signal without enough index history.
"""
from __future__ import annotations

from ..config import SwingPullbackV2Params
from ..indicators import last, sma
from .base import MarketContext, fmt, ok
from .swing_pullback import SwingPullback


def index_above_average(index_daily, days: int) -> tuple[bool, str]:
    """(passes, reason) for 'index close above its `days`-day average' on the last bar given."""
    if len(index_daily) < days:
        return False, f"index history has {len(index_daily)} days, needs {days}"
    closes = [b.close for b in index_daily]
    avg = last(sma(closes, days))
    if not (ok(avg) and ok(closes[-1])):
        return False, "index average unavailable"
    return closes[-1] > avg, f"NIFTY close {fmt(closes[-1])} vs its {days}-day average {fmt(avg)}"


class SwingPullbackV2(SwingPullback):
    id = "swing_pullback_v2"
    base_version = "2"
    needs_index_daily = True
    params: SwingPullbackV2Params

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
