"""Pure indicator functions. No I/O, no clock reads, no randomness.

Series functions return a list the same length as the input, with None where
there is not yet enough data. Unknown inputs stay unknown: they are never
treated as zero."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


def _ok(x) -> bool:
    """A usable value: a finite number (None, NaN and inf are unknown)."""
    return isinstance(x, (int, float)) and math.isfinite(x)


def _check_period(n: int) -> None:
    if not isinstance(n, int) or n < 1:
        raise ValueError("period must be a positive integer")


def sma(values: Sequence[float], n: int) -> list[float | None]:
    """Simple moving average of the last n values."""
    _check_period(n)
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = math.fsum(values[i - n + 1:i + 1]) / n
    return out


def ema(values: Sequence[float], n: int) -> list[float | None]:
    """Exponential moving average, alpha = 2/(n+1), seeded with the SMA of the first n values."""
    _check_period(n)
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    prev = math.fsum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    """TR[0] = high-low; afterwards max(high-low, |high-prev close|, |low-prev close|)."""
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must be the same length")
    out = []
    for i in range(len(highs)):
        hl = highs[i] - lows[i]
        if i == 0:
            out.append(hl)
        else:
            pc = closes[i - 1]
            out.append(max(hl, abs(highs[i] - pc), abs(lows[i] - pc)))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int) -> list[float | None]:
    """Average true range with Wilder smoothing. First value (index n-1) is the
    plain mean of the first n true ranges."""
    _check_period(n)
    tr = true_range(highs, lows, closes)
    out: list[float | None] = [None] * len(tr)
    if len(tr) < n:
        return out
    prev = math.fsum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def daily_returns(closes: Sequence[float]) -> list[float | None]:
    """Percent change from the previous close. First value is None."""
    out: list[float | None] = [None]
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        ok = _ok(prev) and prev != 0 and _ok(cur)
        out.append((cur - prev) / prev * 100.0 if ok else None)
    return out[:len(closes)]


def percentile(values: Sequence[float], p: float) -> float | None:
    """p-th percentile (0-100) with linear interpolation between closest ranks."""
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def last(values: Sequence[float | None]) -> float | None:
    return values[-1] if values else None


@dataclass(frozen=True)
class Breadth:
    pct_above: float | None      # share of counted stocks above their average, 0-100
    above: list[str]
    below: list[str]
    missing: list[str]           # no usable price or not enough history

    @property
    def counted(self) -> int:
        return len(self.above) + len(self.below)


def breadth(stocks: Mapping[str, tuple[Sequence[float], float | None]], n: int) -> Breadth:
    """Market breadth: share of stocks whose current price is above their n-day SMA.

    stocks maps symbol -> (completed daily closes, current price or None).
    A stock with no current price or fewer than n closes is listed as missing,
    never counted as above or below."""
    _check_period(n)
    above, below, missing = [], [], []
    for sym in sorted(stocks):
        closes, price = stocks[sym]
        usable = len(closes) >= n and all(_ok(c) for c in closes[-n:])
        avg = last(sma(list(closes), n)) if usable else None
        if not _ok(price) or not _ok(avg):
            missing.append(sym)
        elif price > avg:
            above.append(sym)
        else:
            below.append(sym)
    counted = len(above) + len(below)
    pct = (len(above) / counted * 100.0) if counted else None
    return Breadth(pct, above, below, missing)


def gap_breaks(opens: Sequence[float], closes: Sequence[float], max_gap_pct: float) -> list[int]:
    """Indexes i where bar i opened more than max_gap_pct away from bar i-1's close.

    Broker history is not adjusted for splits, bonuses or demergers, so such a jump usually
    means the prices before and after it are in different units. Calculations must not span it."""
    out = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], opens[i]
        if _ok(a) and _ok(b) and a > 0 and abs(b / a - 1) * 100 > max_gap_pct:
            out.append(i)
    return out


def after_last_break(bars: Sequence, max_gap_pct: float) -> list:
    """The bars after the last overnight gap above max_gap_pct (all bars if there is none).
    `bars` are objects with .open and .close, oldest first."""
    br = gap_breaks([b.open for b in bars], [b.close for b in bars], max_gap_pct)
    return list(bars[br[-1]:]) if br else list(bars)
