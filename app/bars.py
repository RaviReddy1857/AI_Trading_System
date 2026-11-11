"""Builds 1-minute bars from good ticks."""
from __future__ import annotations

from typing import Callable

from .models import Bar, Tick


class BarBuilder:
    def __init__(self, on_bar_closed: Callable[[Bar], None]):
        self._on_closed = on_bar_closed
        self._open: dict[str, Bar] = {}
        self._last_cum_vol: dict[str, float] = {}

    def add(self, tick: Tick) -> None:
        minute = tick.received_at.replace(second=0, microsecond=0)
        vol = 0.0
        if tick.cum_volume is not None:
            prev = self._last_cum_vol.get(tick.symbol)
            if prev is not None and tick.cum_volume >= prev:
                vol = tick.cum_volume - prev
            self._last_cum_vol[tick.symbol] = tick.cum_volume

        bar = self._open.get(tick.symbol)
        if bar is not None and bar.start != minute:
            self._on_closed(bar)
            bar = None
        if bar is None:
            bar = Bar(tick.symbol, minute, tick.last, tick.last, tick.last, tick.last)
            self._open[tick.symbol] = bar
        bar.high = max(bar.high, tick.last)
        bar.low = min(bar.low, tick.last)
        bar.close = tick.last
        bar.volume += vol
        bar.ticks += 1

    def current(self, symbol: str) -> Bar | None:
        return self._open.get(symbol)

    def flush(self) -> None:
        for bar in list(self._open.values()):
            self._on_closed(bar)
        self._open.clear()
