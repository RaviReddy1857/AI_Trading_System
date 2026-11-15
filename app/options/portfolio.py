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

    def at(self, d: date, a: str, b: str, include_day: bool = True) -> float | None:
        """Correlation as known at the CLOSE of d (sessions up to and including d). `include_day=False`:
        as known INSIDE session d, before its close - completed sessions before d only."""
        key = (d, include_day, *sorted((a, b)))
        if key not in self._cache:
            i = bisect.bisect_right(self.sessions, d) if include_day else bisect.bisect_left(self.sessions, d)
            win = self.sessions[max(0, i - self.window):i]           # completed sessions only
            self._cache[key] = correlation(self.returns(a), self.returns(b), win, self.min_overlap)
        return self._cache[key]


def linked_groups(symbols: Sequence[str], sectors: dict[str, str | None], corr: Callable[[str, str], float | None],
                  threshold: float) -> tuple[list[set[str]], list[str]]:
    """(connected components, problems). A link: same known sector, or correlation >= threshold.
    Problems: an unknown sector, or a pair of different sectors without a measurable correlation."""
    groups, problems = _components(symbols, sectors, corr, threshold)
    return groups, [text for _, text in problems]


def _components(symbols: Sequence[str], sectors: dict[str, str | None], corr: Callable[[str, str], float | None],
                threshold: float) -> tuple[list[set[str]], list[tuple[tuple[str, ...], str]]]:
    """linked_groups with each problem as (the stocks it is about, text)."""
    syms = sorted(set(symbols))
    problems = [((s,), f"{s}: sector not known") for s in syms if not sectors.get(s)]
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
                problems.append(((a, b), f"{a}-{b}: correlation not measurable (too little common return history)"))
            elif c >= threshold:
                parent[find(a)] = find(b)
    groups: dict[str, set[str]] = {}
    for s in syms:
        groups.setdefault(find(s), set()).add(s)
    return sorted(groups.values(), key=lambda g: (-len(g), sorted(g))), problems


def _unknown_joins(groups: list[set[str]], problems: list[tuple[tuple[str, ...], str]], held: set[str]) -> list[str]:
    """The problems that could change a group holding positions (texts).

    A held (or the proposed) stock's own sector unknown always counts. Otherwise the known groups are
    joined along every POSSIBLE link - an unmeasurable correlation, or any link of a stock whose sector
    is unknown (it may share a sector with any stock) - through every stock of the universe, held or
    not. When two components that hold positions could end up in one group, every unknown link inside
    that possible group counts: A(held) -?- C(unheld) -?- B(held) is unknown, although neither link
    alone joins two held components. Unknown links that could only join empty components change nothing."""
    comp = {s: k for k, g in enumerate(groups) for s in g}
    occupied = {comp[s] for s in held}
    out = [text for who, text in problems if len(who) == 1 and who[0] in held]
    parent = list(range(len(groups)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    unknown_sector = [who[0] for who, _ in problems if len(who) == 1]
    for who, _ in problems:
        if len(who) == 2:
            parent[find(comp[who[0]])] = find(comp[who[1]])
    for s in unknown_sector:                                 # its sector may match any other stock's
        for k in range(len(groups)):
            parent[find(comp[s])] = find(k)
    roots: dict[int, int] = {}
    for k in occupied:
        roots[find(k)] = roots.get(find(k), 0) + 1
    joinable = {r for r, n in roots.items() if n > 1}
    for who, text in problems:
        if text not in out and find(comp[who[0]]) in joinable:
            out.append(text)
    return out


def portfolio_check(symbol: str, risk_inr: float, holdings: Sequence[Holding] | None, sectors: dict[str, str | None],
                    corr: Callable[[str, str], float | None], cfg, capital_inr: float,
                    history_verified: bool = False, universe: Sequence[str] = ()) -> tuple[str | None, str, dict]:
    """(code or None, why, facts) for a proposed position next to the open ones (`holdings`, each
    position on its own; None = not known). The proposed position is included in every limit.
    `history_verified`: the closed and manual-trade history is known and verified too (the per-stock
    loss limits read it); without it the portfolio is unknown, whatever the open holdings say. In a
    backtest the simulated history counts as verified; live it stays unknown until the phase 6 journal.
    `universe`: the stocks the correlated groups are built from (connected components over the whole
    universe, the same groups as displayed); the holdings and the proposed stock are always included."""
    if holdings is None:
        return PORTFOLIO_UNKNOWN, "open positions are not known: exposure and losses cannot be checked", {}
    if not history_verified:
        return (PORTFOLIO_UNKNOWN, "closed and manual-trade history is not verified: losses and exposure cannot be "
                                   "checked", {})
    hs = list(holdings) + [Holding(symbol, risk_inr)]
    total = sum(h.risk_inr for h in hs)
    facts = {"open_positions_with_this": len(hs), "total_risk_inr": round(total, 2)}
    if len(hs) > cfg.max_open_spreads:
        return CORRELATED_LIMIT, f"{len(hs)} spreads would be open, above max_open_spreads {cfg.max_open_spreads}", facts
    limit = capital_inr * cfg.max_total_risk_pct / 100.0
    if total > limit:
        return (CORRELATED_LIMIT, f"total risk Rs {total:,.0f} would be above {cfg.max_total_risk_pct:g}% of capital "
                                  f"(Rs {limit:,.0f})", facts)
    held = {h.symbol for h in hs}
    groups, problems = _components(sorted(set(universe) | held), sectors, corr, cfg.correlation_threshold)
    relevant = _unknown_joins(groups, problems, held)
    if relevant:
        return CORRELATION_UNKNOWN, "; ".join(relevant[:3]), facts
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

    # the simulated trades of a backtest are its whole closed history: verified by construction
    history_verified = True

    def __init__(self, settings, engine, universe=None):
        self.universe = list(universe) if universe is not None else None   # None: run_portfolio sets the streams' stocks
        self.cfg = settings.stock_option_selling.portfolio
        self.capital = settings.account.capital_inr
        self.eng = engine
        self.sectors = {i.symbol: (i.sector or None) for i in settings.watchlist}
        self.corr = Correlations(engine.base.daily, engine.base.session_dates(), engine.base.breaks,
                                 self.cfg.correlation_window, self.cfg.correlation_min_overlap)
        from collections import Counter
        self.checks, self.refused, self.group_sizes = 0, Counter(), Counter()

    def __call__(self, sym: str, d: date, sig, replacing, intraday: bool = False) -> str | None:
        """`intraday`: the decision is taken inside session d (a roll at a 5-minute bar's close), so the
        correlations use completed sessions before d only; d's daily close has not happened yet. A
        decision at d's close (a new signal, a roll decided by a rule at the close) may use d."""
        holdings = self.eng.book_holdings(exclude_sig=replacing.sig if replacing is not None else None)
        risk = float(sig.details.get("max_loss_inr") or 0.0)
        code, _, facts = portfolio_check(sym, risk, holdings, self.sectors,
                                         lambda a, b: self.corr.at(d, a, b, include_day=not intraday),
                                         self.cfg, self.capital, history_verified=self.history_verified,
                                         universe=self.universe or ())
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
    """The correlated groups of a set of stocks (same sector or correlation >= threshold), for display:
    the same connected components portfolio_check enforces with `universe=symbols`."""
    groups, problems = linked_groups(symbols, sectors, corr, threshold)
    return {"groups": [sorted(g) for g in groups], "sizes": [len(g) for g in groups], "problems": problems[:20],
            "problem_count": len(problems)}
