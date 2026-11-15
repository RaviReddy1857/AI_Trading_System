"""Corporate actions and the adjusted view of daily history.

Splits, bonuses and demergers change a stock's price without changing what a holder owns, so daily
history before the ex-date is not comparable with the prices after it. For the trend, 52-week low,
range and profile calculations the history before an action is multiplied by the action's price
factor, so it reads in today's terms. Rules:

- Raw prices are never changed in the store; the adjusted view is built on the fly.
- Option strikes and option prices are never adjusted, and the spot used to pick strikes is the raw price.
- Point in time: at a decision on day d only actions known by then are used. The data has no
  announcement date, so an action counts from its ex-date (from its announcement date when one is known).
- A factor is applied only when the raw daily bars show the gap it explains (the ex-date open against
  the previous close within `GAP_TOLERANCE` of the factor). An action the prices do not show is
  reported and not applied - adjusting data that is already adjusted would create a false break.
- Demergers have no ratio in the data: the factor is the ex-date open divided by the previous close
  (the price the market put on what remained), recorded as "estimated from the ex-date opening gap".
- Rights issues and dividends are stored for the record; they are not used to adjust prices.
- A price break that an applied action explains no longer cuts the history; any other break still does.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import replace
from datetime import date, timedelta

from .history import bar_date
from .indicators import gap_breaks

PRICE_ACTIONS = ("SPLIT", "BONUS", "DEMERGER")
RECORD_ONLY = ("RIGHTS", "DIVIDEND")
GAP_TOLERANCE = 0.25            # the observed opening gap must be within 25% of the factor
NOT_EQUITY = re.compile(r"ncrps|preference|debenture|ncd\b|warrant", re.I)


def split_factor(ratio: str) -> float | None:
    """'10:2' = face value 10 split into 2: each share becomes 5, prices x 2/10."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*", ratio or "")
    if not m:
        return None
    old, new = float(m.group(1)), float(m.group(2))
    return new / old if old > 0 and 0 < new < old else None


def bonus_factor(ratio: str, purpose: str = "") -> float | None:
    """'1:2' = 1 new share for every 2 held: prices x 2/3. A bonus of preference shares or debentures
    does not change the equity price: None."""
    if NOT_EQUITY.search(purpose or ""):
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*", ratio or "")
    if not m:
        return None
    new, held = float(m.group(1)), float(m.group(2))
    return held / (new + held) if new > 0 and held > 0 else None


def stated_factor(action_type: str, ratio: str, purpose: str = "") -> tuple[float | None, str]:
    """(price factor, basis) from the action's own ratio; demergers: (None, 'from the ex-date opening gap')."""
    if action_type == "SPLIT":
        f = split_factor(ratio)
        return f, "split ratio" if f else "split ratio unreadable"
    if action_type == "BONUS":
        f = bonus_factor(ratio, purpose)
        return f, ("bonus ratio" if f else ("not an equity bonus" if NOT_EQUITY.search(purpose or "") else "bonus ratio unreadable"))
    if action_type == "DEMERGER":
        return None, "estimated from the ex-date opening gap"
    return None, "record only (not a price adjustment)"


def known_on(action: dict, d: date) -> bool:
    """Whether the action is known at a decision on day d: from its announcement date when known, else its ex-date."""
    when = action.get("announced_on") or action["ex_date"]
    return date.fromisoformat(when) <= d and date.fromisoformat(action["ex_date"]) <= d


def applied_factors(bars, actions: list[dict]) -> list[dict]:
    """The price actions that the raw bars confirm, each {ex_date, index, factor, basis, action}, oldest
    first; `index` is the first bar on or after the ex-date. Unconfirmed ones come back with factor None
    and the reason, for the report."""
    dates = [bar_date(b) for b in bars]
    out = []
    groups: dict[str, list[dict]] = {}
    for a in (x for x in actions if x["action_type"] in PRICE_ACTIONS):
        groups.setdefault(a["ex_date"], []).append(a)
    # actions on the same ex-date (a bonus and a split together) are one price change: checked and applied as one
    for ex_s in sorted(groups):
        acts = sorted(groups[ex_s], key=lambda x: (x["action_type"], x["purpose"]))
        ex = date.fromisoformat(ex_s)
        k = bisect.bisect_left(dates, ex)
        name = " + ".join(f"{x['action_type']}" for x in acts)
        row = {"ex_date": ex, "index": k, "factor": None, "basis": "", "action": acts[0], "actions": acts, "name": name}
        if k == 0 or k >= len(bars) or bars[k - 1].close <= 0 or dates[k] - dates[k - 1] > timedelta(days=7):
            row["basis"] = "no daily bars around the ex-date"
            out.append(row)
            continue
        gap = bars[k].open / bars[k - 1].close
        if any(x["action_type"] == "DEMERGER" for x in acts):
            if not (0.05 < gap < 0.98):
                row["basis"] = f"demerger gap {gap:.3f} not seen in the prices: not applied"
                out.append(row)
                continue
            row["factor"], row["basis"] = gap, "estimated from the ex-date opening gap"
            out.append(row)
            continue
        fs = [x.get("price_factor") for x in acts]
        if any(f is None for f in fs):
            row["basis"] = "; ".join(x.get("factor_basis") or "no factor" for x in acts if x.get("price_factor") is None)
            out.append(row)
            continue
        f = 1.0
        for x in fs:
            f *= x
        if abs(gap / f - 1) > GAP_TOLERANCE:
            row["basis"] = f"factor {f:.4f} not seen in the prices (opening gap {gap:.4f}): not applied"
            out.append(row)
            continue
        row["factor"], row["basis"] = f, " x ".join(x.get("factor_basis") or "" for x in acts)
        out.append(row)
    return out


def adjusted(bars, factors: list[dict]):
    """The bars with every bar before each applied ex-date multiplied by that action's factor (prices)
    and divided by it (volume). New Bar objects; the input is not changed."""
    use = [x for x in factors if x["factor"]]
    if not use:
        return list(bars)
    mult = [1.0] * len(bars)
    for x in use:
        for k in range(x["index"]):
            mult[k] *= x["factor"]
    out = []
    for b, m in zip(bars, mult):
        if m == 1.0:
            out.append(b)
        else:
            out.append(replace(b, open=b.open * m, high=b.high * m, low=b.low * m, close=b.close * m,
                               volume=None if b.volume is None else b.volume / m))
    return out


class AdjustedHistory:
    """Point-in-time adjusted daily history of one stock (cached per set of known actions)."""

    def __init__(self, bars, actions: list[dict], max_gap_pct: float, corrections: dict | None = None):
        """`corrections`: {date: multiplier} - Breeze bars off by exactly a corporate-action factor
        (app/data_quality.py), corrected before anything else; the raw store is not changed."""
        self.corrections = dict(corrections or {})
        self.raw = list(bars)
        self.bars = [replace(b, open=b.open * m, high=b.high * m, low=b.low * m, close=b.close * m,
                             volume=None if b.volume is None else b.volume / m)
                     if (m := self.corrections.get(bar_date(b), 1.0)) != 1.0 else b for b in self.raw]
        self.dates = [bar_date(b) for b in self.bars]
        self.factors = applied_factors(self.bars, actions)
        self.raw_breaks = gap_breaks([b.open for b in self.bars], [b.close for b in self.bars], max_gap_pct)
        self.raw_breaks_raw = gap_breaks([b.open for b in self.raw], [b.close for b in self.raw], max_gap_pct)
        self._cache: dict[int, tuple[list, list[int]]] = {}

    def _known(self, d: date) -> list[dict]:
        return [x for x in self.factors if x["factor"] and all(known_on(a, d) for a in x["actions"])]

    def _view(self, d: date) -> tuple[list, list[int]]:
        known = self._known(d)
        n = len(known)
        if n not in self._cache:
            explained = {x["index"] for x in known}
            self._cache[n] = (adjusted(self.bars, known), [k for k in self.raw_breaks if k not in explained])
        return self._cache[n]

    def window(self, i: int, max_len: int) -> tuple:
        """The daily bars ending at index i, adjusted for the actions known on that day, from the last
        break they do not explain, at most `max_len` bars."""
        d = self.dates[i]
        bars, breaks = self._view(d)
        k = bisect.bisect_right(breaks, i)
        start = breaks[k - 1] if k else 0
        return tuple(bars[max(start, i - max_len + 1):i + 1])

    def differs_from_raw(self, i: int, max_len: int) -> bool:
        """Whether the window a decision at bar i sees differs from the raw bars after the last raw
        break (the history before any adjustment or correction)."""
        k = bisect.bisect_right(self.raw_breaks_raw, i)
        raw_start = self.raw_breaks_raw[k - 1] if k else 0
        w = self.window(i, max_len)
        raw = self.raw[max(raw_start, i - max_len + 1):i + 1]
        return len(w) != len(raw) or any(a is not b for a, b in zip(w, raw))

    def last_unexplained_break(self, i: int) -> int:
        _, breaks = self._view(self.dates[i])
        k = bisect.bisect_right(breaks, i)
        return breaks[k - 1] if k else 0
