"""Swing volume breakout (daily bars, long only, delivery).

TRENDING_UP market; the stock closes above the highest high of the previous `lookback_days`
sessions on volume of at least `volume_mult` x their average -> LONG at the next session's
open. Stop `stop_atr_mult` x ATR below the close; target `reward_risk` x risk; time exit after
`max_hold_days` sessions (applied by the backtest).
"""
from __future__ import annotations

from ..config import SwingBreakoutParams
from ..history import bar_date
from ..indicators import atr, last
from .base import MarketContext, Strategy, fmt, next_weekday_open_window, ok


class SwingBreakout(Strategy):
    id = "swing_breakout"
    base_version = "1"
    horizon = "SWING"
    interval = "1day"
    allowed_regimes = frozenset({"TRENDING_UP"})
    params: SwingBreakoutParams

    def _evaluate(self, ctx: MarketContext):
        p = self.params
        d = ctx.daily
        if len(d) < max(p.lookback_days, p.atr_days) + 1:
            return []
        today, prior = d[-1], d[-(p.lookback_days + 1):-1]
        vols = [b.volume for b in prior]
        if today.volume is None or any(v is None for v in vols):
            return []
        avg_vol = sum(vols) / len(vols)
        top = max(b.high for b in prior)
        a = last(atr([b.high for b in d], [b.low for b in d], [b.close for b in d], p.atr_days))
        if not (ok(avg_vol) and ok(a)) or not (today.close > top and today.volume >= p.volume_mult * avg_vol):
            return []
        entry = today.close
        stop = entry - p.stop_atr_mult * a
        if stop <= 0:
            return []
        target = entry + p.reward_risk * (entry - stop)
        return [self.signal(ctx, "LONG", entry, stop, target, next_weekday_open_window(bar_date(today)), [
            f"regime {ctx.regime}",
            f"close {fmt(entry)} above the {p.lookback_days}-day high {fmt(top)}",
            f"volume {today.volume:,.0f} is {today.volume / avg_vol:.1f}x the {p.lookback_days}-day average "
            f"{avg_vol:,.0f} (needs {p.volume_mult:g}x)",
            f"stop {p.stop_atr_mult:g}x ATR({p.atr_days}) {fmt(a)} below, target {p.reward_risk:g}x risk, "
            f"time exit after {p.max_hold_days} sessions"])]
