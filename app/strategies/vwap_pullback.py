"""VWAP pullback (intraday, 5-minute bars, with the trend).

TRENDING_UP: the previous `min_bars_on_side` bars all closed above session VWAP; the latest
bar dips to within `touch_pct` of VWAP, then closes above VWAP as an up bar -> LONG.
TRENDING_DOWN mirrored -> SHORT. Stop beyond the bar's extreme or VWAP (whichever is further)
plus `stop_buffer_pct`; target `reward_risk` x risk. Needs volume (no VWAP without it).
"""
from __future__ import annotations

from ..config import VwapParams, hhmm
from .base import FIVE_MIN, MarketContext, Strategy, bar_close_time, fmt, ist_time, vwap_series


class VwapPullback(Strategy):
    id = "vwap_pullback"
    base_version = "1"
    horizon = "INTRADAY"
    interval = "5minute"
    allowed_regimes = frozenset({"TRENDING_UP", "TRENDING_DOWN"})
    params: VwapParams

    def _evaluate(self, ctx: MarketContext):
        p = self.params
        bars = ctx.intraday
        if len(bars) < p.min_bars_on_side + 1:
            return []
        t = ist_time(bar_close_time(bars[-1]))
        if not (hhmm(p.first_signal_time) <= t <= hhmm(p.last_signal_time)):
            return []
        vw = vwap_series(bars)
        if any(v is None for v in vw[-(p.min_bars_on_side + 1):]):
            return []
        last, v = bars[-1], vw[-1]
        prior = list(zip(bars[-(p.min_bars_on_side + 1):-1], vw[-(p.min_bars_on_side + 1):-1]))
        valid = ctx.asof + FIVE_MIN
        touch = p.touch_pct / 100
        buf = p.stop_buffer_pct / 100
        if ctx.regime == "TRENDING_UP" and all(b.close > x for b, x in prior) \
                and last.low <= v * (1 + touch) and last.close > v and last.close > last.open:
            entry, stop = last.close, min(last.low, v) * (1 - buf)
            return [self.signal(ctx, "LONG", entry, stop, entry + p.reward_risk * (entry - stop), valid, [
                f"regime {ctx.regime}",
                f"{p.min_bars_on_side} bars closed above VWAP before the pullback",
                f"bar low {fmt(last.low)} touched VWAP {fmt(v)} (within {p.touch_pct:g}%) and closed above at {fmt(last.close)}",
                f"stop {fmt(stop)} below the low/VWAP, target {p.reward_risk:g}x risk"])]
        if ctx.regime == "TRENDING_DOWN" and all(b.close < x for b, x in prior) \
                and last.high >= v * (1 - touch) and last.close < v and last.close < last.open:
            entry, stop = last.close, max(last.high, v) * (1 + buf)
            return [self.signal(ctx, "SHORT", entry, stop, entry - p.reward_risk * (stop - entry), valid, [
                f"regime {ctx.regime}",
                f"{p.min_bars_on_side} bars closed below VWAP before the pullback",
                f"bar high {fmt(last.high)} touched VWAP {fmt(v)} (within {p.touch_pct:g}%) and closed below at {fmt(last.close)}",
                f"stop {fmt(stop)} above the high/VWAP, target {p.reward_risk:g}x risk"])]
        return []
