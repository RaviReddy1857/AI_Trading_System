"""A synthetic washed-out stock for the reversal tests: a plateau near 1000, a fall in two legs with a
bounce between them (so the fall leaves a breakdown level), a wide base with one failed bounce (the
last swing high), a quiet base, and a breakout on volume."""
from datetime import date

from app.history import daily_start
from app.models import Bar
from tests.test_phase9_core import weekdays_back


def washed_out_stock(end=date(2026, 11, 13), breakout=True, after=0, sym="RELIANCE", index_growth=0.0, quiet_volume=False):
    """`index_growth`: NIFTY's gain per session (0.01 = 1% a session, compounding)."""
    px, rng, vol = [], [], []

    def add(values, r, v):
        for x in values:
            px.append(x), rng.append(r), vol.append(v)

    def line(a, b, n):
        return [a + (b - a) * (k + 1) / n for k in range(n)]

    add([1000.0 + (k % 3) for k in range(300)], 0.02, 1e6)        # plateau: the 52-week high
    add(line(1000, 800, 60), 0.02, 1.2e6)                          # first leg down
    add(line(800, 870, 10), 0.02, 1.0e6)                           # bounce: 800 becomes a breakdown level
    add(line(870, 540, 60), 0.02, 1.2e6)                           # second leg down to the low
    add(line(540, 600, 12), 0.02, 9e5)                             # failed bounce: 600 is the last swing high
    add(line(600, 562, 12), 0.02, 9e5)
    add([562.0 + (k % 2) for k in range(16)], 0.02, 8e5)           # wide base
    marks = {"quiet_from": len(px)}
    add([562.0 + (k % 2) for k in range(22)], 0.005, 7e5)          # quiet base: ranges shrink
    marks["breakout"] = len(px)
    if breakout:
        add([580.0, 600.0, 620.0], 0.008, 7e5 if quiet_volume else 3e6)   # third close is above the pivot high 612
        marks["confirmed"] = len(px) - 1
        add([620.0 + 2 * (k + 1) for k in range(after)], 0.008, 1e6)
    days = weekdays_back(end, len(px))
    bars = [Bar(sym, daily_start(d), p, p * (1 + r), p * (1 - r), p, v) for d, p, r, v in zip(days, px, rng, vol)]
    level = [24000 * (1 + index_growth) ** k for k in range(len(days))]
    idx = [Bar("NIFTY", daily_start(d), x, x * 1.002, x * 0.998, x, None) for d, x in zip(days, level)]
    return bars, idx, marks
