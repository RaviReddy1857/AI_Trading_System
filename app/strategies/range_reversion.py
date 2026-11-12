"""Range mean-reversion (intraday, 5-minute bars, RANGE days only).

Bands = `band_bars`-bar average of this session's closes +/- `band_sd` standard deviations.
LONG when the previous bar closed below the lower band and the latest bar closes back inside;
SHORT mirrored at the upper band. Target = the average (middle band); stop beyond the extreme
of the last 3 bars plus `stop_buffer_pct`. Skipped if reward/risk is below `min_reward_risk`.
"""
from __future__ import annotations

import math

from ..config import RangeParams, hhmm
from .base import FIVE_MIN, MarketContext, Strategy, bar_close_time, fmt, ist_time


def _band(closes, n, k):
    window = closes[-n:]
    m = math.fsum(window) / n
    sd = math.sqrt(math.fsum((c - m) ** 2 for c in window) / n)
    return m, m - k * sd, m + k * sd


class RangeReversion(Strategy):
    id = "range_reversion"
    base_version = "1"
    horizon = "INTRADAY"
    interval = "5minute"
    allowed_regimes = frozenset({"RANGE"})
    params: RangeParams

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
        _, p_low, p_high = _band(closes[:-1], p.band_bars, p.band_sd) if len(closes) > p.band_bars else (0, 0, 0)
        if len(closes) <= p.band_bars or high <= low:
            return []
        last, prev = bars[-1], bars[-2]
        recent = bars[-3:]
        buf = p.stop_buffer_pct / 100
        valid = ctx.asof + FIVE_MIN
        band_text = f"{p.band_bars}-bar bands {fmt(low)} / {fmt(mid)} / {fmt(high)} ({p.band_sd:g} sd)"
        if prev.close < p_low and low < last.close < mid:
            entry, stop, target = last.close, min(b.low for b in recent) * (1 - buf), mid
            if (target - entry) >= p.min_reward_risk * (entry - stop) > 0:
                return [self.signal(ctx, "LONG", entry, stop, target, valid, [
                    f"regime {ctx.regime}", band_text,
                    f"previous close {fmt(prev.close)} was below the lower band {fmt(p_low)}; "
                    f"latest close {fmt(last.close)} is back inside",
                    f"target the average {fmt(mid)}, stop {fmt(stop)} below the 3-bar low"])]
        if prev.close > p_high and mid < last.close < high:
            entry, stop, target = last.close, max(b.high for b in recent) * (1 + buf), mid
            if (entry - target) >= p.min_reward_risk * (stop - entry) > 0:
                return [self.signal(ctx, "SHORT", entry, stop, target, valid, [
                    f"regime {ctx.regime}", band_text,
                    f"previous close {fmt(prev.close)} was above the upper band {fmt(p_high)}; "
                    f"latest close {fmt(last.close)} is back inside",
                    f"target the average {fmt(mid)}, stop {fmt(stop)} above the 3-bar high"])]
        return []
