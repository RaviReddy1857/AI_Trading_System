"""Opening-range breakout (intraday, 5-minute bars).

Opening range = the first `range_minutes` of the session (default 09:15-09:30).
LONG on TRENDING_UP days when a bar closes above the range high by `breakout_buffer_pct`
for the first time; SHORT on TRENDING_DOWN days, mirrored. Stop at the other side of the
range, target at `reward_risk` x risk. Skipped when the range is wider than `max_range_pct`
of price or the breakout bar's volume is below `min_volume_mult` x the range's average volume.
"""
from __future__ import annotations

from ..config import OrbParams, hhmm
from .base import (FIVE_MIN, MarketContext, Strategy, bar_close_time, fmt, ist_time, ok,
                   session_starts_at_open)


class OpeningRangeBreakout(Strategy):
    id = "orb"
    base_version = "1"
    horizon = "INTRADAY"
    interval = "5minute"
    allowed_regimes = frozenset({"TRENDING_UP", "TRENDING_DOWN"})
    params: OrbParams

    def _evaluate(self, ctx: MarketContext):
        p = self.params
        bars = ctx.intraday
        n = p.range_minutes // 5
        if len(bars) <= n or not session_starts_at_open(bars):
            return []
        last = bars[-1]
        if ist_time(bar_close_time(last)) > hhmm(p.last_signal_time):
            return []
        rng = bars[:n]
        hi, lo = max(b.high for b in rng), min(b.low for b in rng)
        mid = (hi + lo) / 2
        if not (ok(hi) and ok(lo)) or (hi - lo) / mid * 100 > p.max_range_pct:
            return []
        up_level, down_level = hi * (1 + p.breakout_buffer_pct / 100), lo * (1 - p.breakout_buffer_pct / 100)
        between = bars[n:-1]
        vols = [b.volume for b in rng]
        if any(v is None for v in vols) or last.volume is None:
            return []
        avg_vol = sum(vols) / n
        if avg_vol <= 0 or last.volume < p.min_volume_mult * avg_vol:
            return []
        why_vol = f"breakout bar volume {last.volume:,.0f} >= {p.min_volume_mult:g}x range average {avg_vol:,.0f}"
        rng_text = f"opening range {fmt(lo)}-{fmt(hi)} ({(hi - lo) / mid * 100:.2f}% of price)"
        valid = ctx.asof + FIVE_MIN
        if ctx.regime == "TRENDING_UP" and last.close > up_level and all(b.close <= up_level for b in between):
            entry, stop = last.close, lo
            return [self.signal(ctx, "LONG", entry, stop, entry + p.reward_risk * (entry - stop), valid, [
                f"regime {ctx.regime}", rng_text,
                f"first close above the range: {fmt(last.close)} > {fmt(up_level)}", why_vol,
                f"stop at range low {fmt(lo)}, target {p.reward_risk:g}x risk"])]
        if ctx.regime == "TRENDING_DOWN" and last.close < down_level and all(b.close >= down_level for b in between):
            entry, stop = last.close, hi
            return [self.signal(ctx, "SHORT", entry, stop, entry - p.reward_risk * (stop - entry), valid, [
                f"regime {ctx.regime}", rng_text,
                f"first close below the range: {fmt(last.close)} < {fmt(down_level)}", why_vol,
                f"stop at range high {fmt(hi)}, target {p.reward_risk:g}x risk"])]
        return []
