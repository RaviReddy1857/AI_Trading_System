"""Phase 9 portfolio limits: how many spreads, how much risk, and how much in one correlated group.

Every open position counts on its own toward `max_open_spreads` and `max_total_risk_pct` of capital.
Positions are also grouped: two stocks are linked when they are in the same sector or when the
correlation of their daily returns over the last `correlation_window` completed sessions is at least
`correlation_threshold` (with at least `correlation_min_overlap` common observations). A group is a
connected component of those links, so A-B and B-C put A, B and C in one group. Each group is capped
at `group_max_positions` positions and `group_max_risk_pct` of capital. The proposed position is
included when checking.

Codes (no actionable entry or roll when any applies):
- CORRELATED_LIMIT: a count, total-risk or group limit would be exceeded.
- CORRELATION_UNKNOWN: a stock's sector is not known, or two stocks of different sectors do not
  have enough common return history to measure their correlation. An unknown relationship is never
  put into an assumed group.
- PORTFOLIO_UNKNOWN: the open positions are not known (live, until positions are recorded in
  phase 6). Missing history is never treated as no exposure.
Only completed sessions are used: nothing after the decision day.
"""
from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from ..history import bar_date

CORRELATED_LIMIT, CORRELATION_UNKNOWN, PORTFOLIO_UNKNOWN = "CORRELATED_LIMIT", "CORRELATION_UNKNOWN", "PORTFOLIO_UNKNOWN"


@dataclass(frozen=True)
class Holding:
    symbol: str
    risk_inr: float            # the position's cost-inclusive maximum loss


def daily_returns(bars: Sequence, sessions: Sequence[date], breaks: Sequence[int] = ()) -> dict[date, float]:
    """Close-to-close returns by date, only where the stock traded on the previous market session too
    (a missing session is not bridged) and not across an unadjusted price break."""
    pos = {d: i for i, d in enumerate(sessions)}
    brk = set(breaks)
    out = {}
    for k in range(1, len(bars)):
        d, prev = bar_date(bars[k]), bar_date(bars[k - 1])
        if k in brk or d not in pos or prev not in pos or pos[d] != pos[prev] + 1 or bars[k - 1].close <= 0:
            continue
        out[d] = bars[k].close / bars[k - 1].close - 1
    return out


def correlation(a: dict[date, float], b: dict[date, float], window: Sequence[date], min_overlap: int) -> float | None:
    """Pearson correlation of two return series over the dates of `window` both have; None with fewer
    than `min_overlap` common observations or no variation."""
    xs = [(a[d], b[d]) for d in window if d in a and d in b]
    if len(xs) < min_overlap:
        return None
    n = len(xs)
    ma, mb = sum(x for x, _ in xs) / n, sum(y for _, y in xs) / n
    va = sum((x - ma) ** 2 for x, _ in xs)
    vb = sum((y - mb) ** 2 for _, y in xs)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in xs) / math.sqrt(va * vb)


class Correlations:
    """Correlations as known after the close of a day, from daily bars (cached per day and pair)."""

    def __init__(self, daily: Callable[[str], Sequence], sessions: Sequence[date], breaks: Callable[[str], Sequence[int]],
                 window: int, min_overlap: int):
        self._daily, self._breaks = daily, breaks
        self.sessions = list(sessions)
        self.window, self.min_overlap = window, min_overlap
        self._ret: dict[str, dict] = {}
        self._cache: dict[tuple, float | None] = {}

    def returns(self, sym: str) -> dict[date, float]:
        if sym not in self._ret:
            self._ret[sym] = daily_returns(self._daily(sym), self.sessions, self._breaks(sym))
        return self._ret[sym]

    def at(self, d: date, a: str, b: str) -> float | None:
        key = (d, *sorted((a, b)))
        if key not in self._cache:
            i = bisect.bisect_right(self.sessions, d)
            win = self.sessions[max(0, i - self.window):i]           # completed sessions up to and including d
            self._cache[key] = correlation(self.returns(a), self.returns(b), win, self.min_overlap)
        return self._cache[key]


def linked_groups(symbols: Sequence[str], sectors: dict[str, str | None], corr: Callable[[str, str], float | None],
                  threshold: float) -> tuple[list[set[str]], list[str]]:
    """(connected components, problems). A link: same known sector, or correlation >= threshold.
    Problems: an unknown sector, or a pair of different sectors without a measurable correlation."""
    syms = sorted(set(symbols))
    problems = [f"{s}: sector not known" for s in syms if not sectors.get(s)]
    parent = {s: s for s in syms}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            sa, sb = sectors.get(a), sectors.get(b)
            if sa and sb and sa == sb:
                parent[find(a)] = find(b)
                continue
            c = corr(a, b)
            if c is None:
                problems.append(f"{a}-{b}: correlation not measurable (too little common return history)")
            elif c >= threshold:
                parent[find(a)] = find(b)
    groups: dict[str, set[str]] = {}
    for s in syms:
        groups.setdefault(find(s), set()).add(s)
    return sorted(groups.values(), key=lambda g: (-len(g), sorted(g))), problems


def portfolio_check(symbol: str, risk_inr: float, holdings: Sequence[Holding] | None, sectors: dict[str, str | None],
                    corr: Callable[[str, str], float | None], cfg, capital_inr: float) -> tuple[str | None, str, dict]:
    """(code or None, why, facts) for a proposed position next to the open ones (`holdings`, each
    position on its own; None = not known). The proposed position is included in every limit."""
    if holdings is None:
        return PORTFOLIO_UNKNOWN, "open positions are not known: exposure and losses cannot be checked", {}
    hs = list(holdings) + [Holding(symbol, risk_inr)]
    total = sum(h.risk_inr for h in hs)
    facts = {"open_positions_with_this": len(hs), "total_risk_inr": round(total, 2)}
    if len(hs) > cfg.max_open_spreads:
        return CORRELATED_LIMIT, f"{len(hs)} spreads would be open, above max_open_spreads {cfg.max_open_spreads}", facts
    limit = capital_inr * cfg.max_total_risk_pct / 100.0
    if total > limit:
        return (CORRELATED_LIMIT, f"total risk Rs {total:,.0f} would be above {cfg.max_total_risk_pct:g}% of capital "
                                  f"(Rs {limit:,.0f})", facts)
    groups, problems = linked_groups([h.symbol for h in hs], sectors, corr, cfg.correlation_threshold)
    if problems:
        return CORRELATION_UNKNOWN, "; ".join(problems[:3]), facts
    group = next(g for g in groups if symbol in g)
    in_group = [h for h in hs if h.symbol in group]
    g_risk = sum(h.risk_inr for h in in_group)
    facts.update({"group": sorted(group), "group_positions": len(in_group), "group_risk_inr": round(g_risk, 2)})
    if len(in_group) > cfg.group_max_positions:
        return (CORRELATED_LIMIT, f"{len(in_group)} positions in the correlated group {sorted(group)}, above "
                                  f"group_max_positions {cfg.group_max_positions}", facts)
    g_limit = capital_inr * cfg.group_max_risk_pct / 100.0
    if g_risk > g_limit:
        return (CORRELATED_LIMIT, f"risk Rs {g_risk:,.0f} in the correlated group {sorted(group)} would be above "
                                  f"{cfg.group_max_risk_pct:g}% of capital (Rs {g_limit:,.0f})", facts)
    return None, "within the portfolio limits", facts


class PortfolioGate:
    """The portfolio limits as a gate for OptionEngine.run_portfolio. Counts every check, every refusal
    and the size of the proposed position's group, for the report."""

    def __init__(self, settings, engine):
        self.cfg = settings.stock_option_selling.portfolio
        self.capital = settings.account.capital_inr
        self.eng = engine
        self.sectors = {i.symbol: (i.sector or None) for i in settings.watchlist}
        self.corr = Correlations(engine.base.daily, engine.base.session_dates(), engine.base.breaks,
                                 self.cfg.correlation_window, self.cfg.correlation_min_overlap)
        from collections import Counter
        self.checks, self.refused, self.group_sizes = 0, Counter(), Counter()

    def __call__(self, sym: str, d: date, sig, replacing) -> str | None:
        holdings = self.eng.book_holdings(exclude_sig=replacing.sig if replacing is not None else None)
        risk = float(sig.details.get("max_loss_inr") or 0.0)
        code, _, facts = portfolio_check(sym, risk, holdings, self.sectors, lambda a, b: self.corr.at(d, a, b),
                                         self.cfg, self.capital)
        self.checks += 1
        if "group_positions" in facts:
            self.group_sizes[facts["group_positions"]] += 1
        if code:
            self.refused[code] += 1
        return code

    def summary(self) -> dict:
        return {"checks": self.checks, "refused": dict(sorted(self.refused.items())),
                "group_size_of_proposed_positions": {str(k): v for k, v in sorted(self.group_sizes.items())}}


def universe_groups(symbols, sectors: dict, corr: Callable[[str, str], float | None], threshold: float) -> dict:
    """The correlated groups of a set of stocks (same sector or correlation >= threshold), for display."""
    groups, problems = linked_groups(symbols, sectors, corr, threshold)
    return {"groups": [sorted(g) for g in groups], "sizes": [len(g) for g in groups], "problems": problems[:20],
            "problem_count": len(problems)}
