"""Strategy interface (architecture.md section 6).

A strategy is a pure function of its MarketContext: same context, same signals. It reads no
clock, no network and no database. It sees only completed bars up to `ctx.asof`.

Base rules every strategy gets for free (Strategy.evaluate):
- price status other than LIVE  -> no signal (missing or stale data means NO TRADE)
- regime not in allowed_regimes -> no signal
- index symbols                 -> no signal (cash-equity strategies; index trades are options, phase 4)
- a signal whose data is newer than ctx.asof is a bug and raises
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from pydantic import BaseModel

from ..models import Bar, Signal
from ..quality import IST, MARKET_OPEN

FIVE_MIN = timedelta(minutes=5)
_NS = uuid.UUID("6f1c2a40-3b1e-4c2a-9d7e-1a2b3c4d5e6f")


@dataclass(frozen=True)
class MarketContext:
    """Everything a strategy may look at for one symbol at one moment."""
    symbol: str
    kind: str                       # stock | index
    asof: datetime                  # UTC: close time of the newest bar included
    regime: str
    intraday: tuple[Bar, ...] = ()  # this session's completed 5-minute bars, oldest first
    daily: tuple[Bar, ...] = ()     # completed daily bars, oldest first (swing: last one is the signal day)
    status: str = "LIVE"            # price status from the quality gate; only LIVE is usable


class LookAheadError(RuntimeError):
    pass


class Strategy(ABC):
    id: str = ""
    base_version: str = "1"
    horizon: str = "INTRADAY"            # INTRADAY | SWING
    interval: str = "5minute"            # bars the strategy runs on: 5minute | 1day
    allowed_regimes: frozenset[str] = frozenset()

    def __init__(self, params: BaseModel):
        self.params = params
        blob = json.dumps(params.model_dump(), sort_keys=True).encode()
        # The version changes whenever a threshold changes, like the regime rules version.
        self.version = f"{self.base_version}/{hashlib.sha256(blob).hexdigest()[:8]}"

    def evaluate(self, ctx: MarketContext) -> list[Signal]:
        if ctx.status != "LIVE" or ctx.regime not in self.allowed_regimes or ctx.kind != "stock":
            return []
        bars = ctx.intraday if self.interval == "5minute" else ctx.daily
        if not bars:
            return []
        newest = bars[-1].start + (FIVE_MIN if self.interval == "5minute" else timedelta(0))
        if self.interval == "5minute" and newest > ctx.asof:
            raise LookAheadError(f"{self.id}: bar ending {newest} is after asof {ctx.asof}")
        out = self._evaluate(ctx)
        for s in out:
            if s.data_as_of > ctx.asof:
                raise LookAheadError(f"{self.id}: signal uses data after {ctx.asof}")
        return out

    @abstractmethod
    def _evaluate(self, ctx: MarketContext) -> list[Signal]: ...

    # ---- helpers ----
    def signal(self, ctx: MarketContext, direction: str, entry: float, stop: float, target: float,
               valid_until: datetime, why: Sequence[str]) -> Signal:
        key = f"{self.id}|{self.version}|{ctx.symbol}|{direction}|{ctx.asof.isoformat()}"
        return Signal(
            signal_id=str(uuid.uuid5(_NS, key)), created_at=ctx.asof,
            strategy_id=self.id, strategy_version=self.version, symbol=ctx.symbol,
            asset_class="CASH_EQUITY", direction=direction, horizon=self.horizon,
            entry=round(entry, 2), stop=round(stop, 2), target=round(target, 2),
            valid_until=valid_until, regime=ctx.regime, why_now=tuple(why), data_as_of=ctx.asof,
        )


# ---------------- shared pure helpers ----------------
def ok(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def ist_time(dt: datetime) -> time:
    return dt.astimezone(IST).time()


def bar_close_time(bar: Bar) -> datetime:
    return bar.start + FIVE_MIN


def session_starts_at_open(bars: Sequence[Bar]) -> bool:
    return bool(bars) and ist_time(bars[0].start) == MARKET_OPEN


def vwap_series(bars: Sequence[Bar]) -> list[float | None]:
    """Session VWAP after each bar, from typical price x volume. None while volume is unknown or zero."""
    out: list[float | None] = []
    pv = vol = 0.0
    for b in bars:
        if b.volume is None or not math.isfinite(b.volume) or b.volume < 0:
            return [None] * len(bars)
        pv += (b.high + b.low + b.close) / 3 * b.volume
        vol += b.volume
        out.append(pv / vol if vol > 0 else None)
    return out


def next_weekday_open_window(d: date, minutes: int = 15) -> datetime:
    """UTC end of the first `minutes` of the next weekday session (holidays make the signal expire)."""
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return (datetime.combine(n, MARKET_OPEN, IST) + timedelta(minutes=minutes)).astimezone(timezone.utc)


def fmt(x: float) -> str:
    return f"{x:,.2f}"
