"""Opening-range breakout, version 2 (phase 3b; intraday, 5-minute bars).

Opening range = the first `range_minutes` of the session (default 09:15-09:45).
LONG on TRENDING_UP days, SHORT on TRENDING_DOWN days, on the first 5-minute close beyond the
range whose volume is above `min_volume_mult` x the average volume of the same time-of-day bar
over the previous `volume_lookback_sessions` sessions. No signal until those sessions exist.
Stop at the opposite side of the range; target `reward_risk` x risk.
Cost rule: skipped when the stop distance is below `min_stop_cost_mult` x the estimated
round-trip cost (charges + slippage, from config.yaml costs).

Differences from v1: 30-minute range; time-of-day volume average instead of the opening-range
average; no breakout buffer; no maximum range width; the cost rule.
"""
from __future__ import annotations

from ..config import OrbV2Params, hhmm
from ..models import Bar
from .base import (FIVE_MIN, MarketContext, Strategy, bar_close_time, fmt, ist_time, ok,
                   session_starts_at_open)


def time_of_day_volume(prior_sessions, t, lookback: int, min_sessions: int) -> float | None:
    """Average volume of the bar starting at IST time `t` over the last `lookback` sessions.
    None if fewer than `lookback` sessions are given or fewer than `min_sessions` have that bar."""
    sessions = prior_sessions[-lookback:]
    if len(sessions) < lookback:
        return None
    vols = []
    for s in sessions:
        b = next((x for x in s if ist_time(x.start) == t), None)
        if b is not None and b.volume is not None and b.volume >= 0:
            vols.append(b.volume)
    if len(vols) < min_sessions:
        return None
    return sum(vols) / len(vols)


class OpeningRangeBreakoutV2(Strategy):
    id = "orb_v2"
    base_version = "2"
    horizon = "INTRADAY"
    interval = "5minute"
    allowed_regimes = frozenset({"TRENDING_UP", "TRENDING_DOWN"})
    uses_cost_estimate = True
    params: OrbV2Params

    def __init__(self, params: OrbV2Params, round_trip_cost_pct: float | None = None):
        super().__init__(params, round_trip_cost_pct)
        self.prior_sessions_needed = params.volume_lookback_sessions

    def _qualifies(self, ctx: MarketContext, bar: Bar, hi: float, lo: float) -> str | None:
        """'LONG' / 'SHORT' if `bar` closes beyond the range in the regime's direction on enough volume."""
        p = self.params
        if ctx.regime == "TRENDING_UP" and bar.close > hi:
            side = "LONG"
        elif ctx.regime == "TRENDING_DOWN" and bar.close < lo:
            side = "SHORT"
        else:
            return None
        avg = time_of_day_volume(ctx.prior_sessions, ist_time(bar.start), p.volume_lookback_sessions,
                                 p.min_volume_sessions)
        if avg is None or avg <= 0 or bar.volume is None or not bar.volume > p.min_volume_mult * avg:
            return None
        return side

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
        if not (ok(hi) and ok(lo)) or hi <= lo:
            return []
        side = self._qualifies(ctx, last, hi, lo)
        if side is None:
            return []
        # only the first qualifying breakout bar of the day
        if any(self._qualifies(ctx, b, hi, lo) == side for b in bars[n:-1]):
            return []
        entry = last.close
        stop = lo if side == "LONG" else hi
        dist_pct = abs(entry - stop) / entry * 100
        floor = p.min_stop_cost_mult * self.round_trip_cost_pct
        if dist_pct < floor:
            self.rejections["stop_below_cost_floor"] += 1
            return []
        avg = time_of_day_volume(ctx.prior_sessions, ist_time(last.start), p.volume_lookback_sessions,
                                 p.min_volume_sessions)
        why = [f"regime {ctx.regime}",
               f"opening range {fmt(lo)}-{fmt(hi)} ({p.range_minutes} minutes)",
               f"volume {last.volume:,.0f} > {p.min_volume_mult:g}x the {ist_time(last.start):%H:%M} bar's "
               f"{p.volume_lookback_sessions}-session average {avg:,.0f}",
               f"stop {fmt(stop)} is {dist_pct:.2f}% away (>= {p.min_stop_cost_mult:g}x round-trip cost "
               f"{self.round_trip_cost_pct:.3f}% = {floor:.2f}%), target {p.reward_risk:g}x risk"]
        valid = ctx.asof + FIVE_MIN
        if side == "LONG":
            why.insert(2, f"first qualifying close above the range: {fmt(entry)} > {fmt(hi)}")
            return [self.signal(ctx, "LONG", entry, stop, entry + p.reward_risk * (entry - stop), valid, why)]
        why.insert(2, f"first qualifying close below the range: {fmt(entry)} < {fmt(lo)}")
        return [self.signal(ctx, "SHORT", entry, stop, entry - p.reward_risk * (stop - entry), valid, why)]
