"""Range mean-reversion, version 2 (phase 3b; intraday, 5-minute bars, RANGE days only).

Entry as v1: LONG when the previous bar closed below the lower band (`band_bars`-bar average
-/+ `band_sd` sd) and the latest bar closes back inside below the average; SHORT mirrored.
Stop = whichever is further from entry: the v1 stop (extreme of the last 3 bars plus
`stop_buffer_pct`) or `min_stop_cost_mult` x the estimated round-trip cost.
Target = the band average. Skipped when the target is closer than `min_target_cost_mult` x the
round-trip cost. v1's minimum reward/risk is not applied.
"""
from __future__ import annotations

from ..config import RangeV2Params, hhmm
from .base import FIVE_MIN, MarketContext, Strategy, bar_close_time, fmt, ist_time
from .range_reversion import _band


class RangeReversionV2(Strategy):
    id = "range_reversion_v2"
    base_version = "2"
    horizon = "INTRADAY"
    interval = "5minute"
    allowed_regimes = frozenset({"RANGE"})
    uses_cost_estimate = True
    params: RangeV2Params

    def _evaluate(self, ctx: MarketContext):
        p = self.params
        bars = ctx.intraday
        if len(bars) < p.band_bars + 1:
            return []
        t = ist_time(bar_close_time(bars[-1]))
        if not (hhmm(p.first_signal_time) <= t <= hhmm(p.last_signal_time)):
            return []
        closes = [b.close for b in bars]
        mid, low, high = _band(closes, p.band_bars, p.band_sd)
        _, p_low, p_high = _band(closes[:-1], p.band_bars, p.band_sd)
        if high <= low:
            return []
        last, prev = bars[-1], bars[-2]
        recent = bars[-3:]
        buf = p.stop_buffer_pct / 100
        cost = self.round_trip_cost_pct
        floor = p.min_stop_cost_mult * cost / 100
        band_text = f"{p.band_bars}-bar bands {fmt(low)} / {fmt(mid)} / {fmt(high)} ({p.band_sd:g} sd)"
        if prev.close < p_low and low < last.close < mid:
            side, entry = "LONG", last.close
            structural = min(b.low for b in recent) * (1 - buf)
            stop = min(structural, entry * (1 - floor))
            entry_text = (f"previous close {fmt(prev.close)} was below the lower band {fmt(p_low)}; "
                          f"latest close {fmt(entry)} is back inside")
        elif prev.close > p_high and mid < last.close < high:
            side, entry = "SHORT", last.close
            structural = max(b.high for b in recent) * (1 + buf)
            stop = max(structural, entry * (1 + floor))
            entry_text = (f"previous close {fmt(prev.close)} was above the upper band {fmt(p_high)}; "
                          f"latest close {fmt(entry)} is back inside")
        else:
            return []
        target_pct = abs(mid - entry) / entry * 100
        if target_pct < p.min_target_cost_mult * cost:
            self.rejections["target_below_cost_floor"] += 1
            return []
        widened = stop != structural
        if widened:
            self.rejections["stop_widened_to_cost_floor (kept, not removed)"] += 1
        stop_pct = abs(entry - stop) / entry * 100
        why = [f"regime {ctx.regime}", band_text, entry_text,
               f"target the average {fmt(mid)} ({target_pct:.2f}% away, >= {p.min_target_cost_mult:g}x round-trip "
               f"cost {cost:.3f}%)",
               f"stop {fmt(stop)} ({stop_pct:.2f}% away): "
               + (f"widened from the 3-bar extreme {fmt(structural)} to {p.min_stop_cost_mult:g}x round-trip cost"
                  if widened else "the 3-bar extreme, already beyond the cost floor")]
        return [self.signal(ctx, side, entry, stop, mid, ctx.asof + FIVE_MIN, why)]
