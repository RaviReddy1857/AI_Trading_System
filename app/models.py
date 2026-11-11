from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Tick:
    symbol: str
    last: float
    bid: float | None = None       # None = not provided (indices have no bid/ask)
    ask: float | None = None
    cum_volume: float | None = None  # total traded quantity for the day
    prev_close: float | None = None
    source: str = "sim"
    received_at: datetime = field(default_factory=utcnow)


@dataclass
class Bar:
    symbol: str
    start: datetime      # minute start, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ticks: int = 0
