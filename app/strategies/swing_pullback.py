"""Swing trend pullback (daily bars, long only, delivery).

TRENDING_UP market; the stock closes above its `slow_sma`-day average, the fast average is
above the slow one, the day's low came within `pullback_pct` of the fast average, and the
day closed above the fast average as an up day -> LONG at the next session's open.
Stop `stop_atr_mult` x ATR below the close; target `reward_risk` x risk; time exit after
`max_hold_days` sessions (applied by the backtest).
"""
from __future__ import annotations

from ..config import SwingPullbackParams
from ..indicators import atr, last, sma
from .base import MarketContext, Strategy, fmt, next_weekday_open_window, ok
from ..history import bar_date


class SwingPullback(Strategy):
    id = "swing_pullback"
    base_version = "1"
    horizon = "SWING"
    interval = "1day"
    allowed_regimes = frozenset({"TRENDING_UP"})
    params: SwingPullbackParams

    def _evaluate(self, ctx: MarketContext):
        p = self.params
        d = ctx.daily
        need = max(p.slow_sma, p.atr_days + 1)
        if len(d) < need:
            return []
        closes = [b.close for b in d]
        fast, slow = last(sma(closes, p.fast_sma)), last(sma(closes, p.slow_sma))
        a = last(atr([b.high for b in d], [b.low for b in d], closes, p.atr_days))
        if not (ok(fast) and ok(slow) and ok(a)):
            return []
        today = d[-1]
        if not (today.close > slow and fast > slow and today.low <= fast * (1 + p.pullback_pct / 100)
                and today.close > fast and today.close > today.open):
            return []
        entry = today.close
        stop = entry - p.stop_atr_mult * a
        if stop <= 0:
            return []
        target = entry + p.reward_risk * (entry - stop)
        return [self.signal(ctx, "LONG", entry, stop, target, next_weekday_open_window(bar_date(today)), [
            f"regime {ctx.regime}",
            f"close {fmt(entry)} above the {p.slow_sma}-day average {fmt(slow)}; "
            f"{p.fast_sma}-day {fmt(fast)} above {p.slow_sma}-day",
            f"low {fmt(today.low)} pulled back to within {p.pullback_pct:g}% of the {p.fast_sma}-day average "
            f"and closed above it as an up day",
            f"stop {p.stop_atr_mult:g}x ATR({p.atr_days}) {fmt(a)} below, target {p.reward_risk:g}x risk, "
            f"time exit after {p.max_hold_days} sessions"])]
