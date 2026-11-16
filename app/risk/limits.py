"""Phase 5 limits: sector, correlation, daily and monthly loss stops, drawdown, kill switch.

Every rupee figure is a percentage of capital, computed at call time, so changing account.capital_inr
rescales every limit. A limit is reached when the value equals it ("at the limit" = stopped, halved,
paused or blocked), except the correlation link, which needs a correlation strictly ABOVE the threshold
(the specification says "above 0.8"). Unknown inputs never pass a check.

Correlation groups reuse `app.options.portfolio` (return series, Pearson correlation, connected
components); this module only chooses the links: correlation above `risk.correlation_threshold`.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal

from ..options.portfolio import CORRELATED_LIMIT, CORRELATION_UNKNOWN, _components
from .portfolio import PORTFOLIO_UNKNOWN, known, paise

SECTOR_LIMIT = "SECTOR_LIMIT"
DATA_MISSING = "DATA_MISSING"
DAILY_STOP, MONTHLY_PAUSE = "DAILY_STOP", "MONTHLY_PAUSE"
DRAWDOWN_PAUSE = "DRAWDOWN_PAUSE"
KILL_SWITCH = "KILL_SWITCH"

__all__ = ["SECTOR_LIMIT", "DATA_MISSING", "DAILY_STOP", "MONTHLY_PAUSE", "DRAWDOWN_PAUSE", "KILL_SWITCH",
           "CORRELATED_LIMIT", "CORRELATION_UNKNOWN", "sector_check", "correlated_group", "loss_stops",
           "drawdown_state", "kill_switch"]


# ---------------- sector ----------------
def sector_check(symbol: str, positions, sectors: dict, max_per_sector: int) -> tuple[str | None, str]:
    """At most `max_per_sector` open trades per sector, the proposed one included. A sector that is not
    known (the proposed symbol's or a held one's) blocks: it cannot be counted."""
    sec = sectors.get(symbol)
    if not sec:
        return DATA_MISSING, f"sector of {symbol} not known (add it to the watchlist)"
    unknown = sorted({p.symbol for p in positions if not sectors.get(p.symbol)})
    if unknown:
        return DATA_MISSING, f"sector not known for held {', '.join(unknown)}"
    n = sum(1 for p in positions if sectors.get(p.symbol) == sec)
    if n + 1 > max_per_sector:
        return SECTOR_LIMIT, f"{n} open trade(s) in {sec}; one more is above max_trades_per_sector {max_per_sector}"
    return None, f"{n + 1} of {max_per_sector} trades in {sec}"


# ---------------- correlation ----------------
def correlated_group(symbol: str, positions, corr: Callable[[str, str], float | None],
                     threshold: float) -> tuple[str | None, str, set[str], Decimal]:
    """(CORRELATION_UNKNOWN or None, why, the group of `symbol`, the risk already held in that group).

    Two symbols are linked when their correlation is ABOVE `threshold`; a group is a connected component
    over the held symbols and the proposed one (the same symbol is always one group). A link that cannot
    be measured is unknown: it blocks only when it could put the proposed symbol into a group with a
    held position (directly or through other held symbols)."""
    syms = sorted({p.symbol for p in positions} | {symbol})
    own = {s: "\x00" + s for s in syms}          # no sector links here: sectors have their own limit

    def strict(a: str, b: str):
        c = corr(a, b)
        if not known(c):                         # None, NaN and infinity are unknown (NaN > 0.8 is False)
            return None
        return c if c > threshold else -1.0

    groups, problems = _components(syms, own, strict, threshold)
    comp = {s: k for k, g in enumerate(groups) for s in g}
    parent = list(range(len(groups)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for who, _ in problems:
        if len(who) == 2:
            parent[find(comp[who[0]])] = find(comp[who[1]])
    root = find(comp[symbol])
    unknown = [text for who, text in problems if find(comp[who[0]]) == root]
    group = next(g for g in groups if symbol in g)
    held = sum((paise(p.risk_inr) for p in positions if p.symbol in group), Decimal(0))
    if unknown:
        return CORRELATION_UNKNOWN, "; ".join(unknown[:3]), group, held
    return None, f"group {sorted(group)}", group, held


# ---------------- loss stops ----------------
def loss_stops(capital: float, realised_today, realised_month, positions, daily_pct: float,
               monthly_pct: float) -> tuple[list[tuple[str, str]], dict]:
    """([(code, why)], facts) on realised plus open P&L. DAILY_STOP when today's loss reaches
    daily_loss_stop_pct of capital; MONTHLY_PAUSE when the month's loss reaches monthly_loss_pause_pct.
    Any part not known -> PORTFOLIO_UNKNOWN (never treated as zero)."""
    if not (known(capital) and capital > 0):
        return [(PORTFOLIO_UNKNOWN, f"capital {capital!r} not known: the loss stops cannot be computed")], {}
    if positions is None or not known(realised_today) or not known(realised_month) or not all(
            known(p.open_pnl_today_inr) and known(p.open_pnl_month_inr) for p in positions):
        return [(PORTFOLIO_UNKNOWN, "realised or open P&L not known: the loss stops cannot be checked")], {}
    day = paise(realised_today) + sum((paise(p.open_pnl_today_inr) for p in positions), Decimal(0))
    month = paise(realised_month) + sum((paise(p.open_pnl_month_inr) for p in positions), Decimal(0))
    d_lim = paise(Decimal(str(capital)) * Decimal(str(daily_pct)) / 100)
    m_lim = paise(Decimal(str(capital)) * Decimal(str(monthly_pct)) / 100)
    facts = {"pnl_today_inr": float(day), "pnl_month_inr": float(month), "daily_stop_inr": float(d_lim),
             "monthly_pause_inr": float(m_lim)}
    out = []
    if -day >= d_lim:
        out.append((DAILY_STOP, f"today's loss Rs {-day:,.2f} (realised + open) reached the daily stop Rs {d_lim:,.2f} "
                                f"({daily_pct:g}% of capital)"))
    if -month >= m_lim:
        out.append((MONTHLY_PAUSE, f"this month's loss Rs {-month:,.2f} reached the monthly pause Rs {m_lim:,.2f} "
                                   f"({monthly_pct:g}% of capital)"))
    return out, facts


# ---------------- drawdown ----------------
def drawdown_state(equity_curve, flows: Sequence[tuple[date, float]], halve_pct: float,
                   pause_pct: float) -> tuple[Decimal | None, float, str | None, dict]:
    """(drawdown % or None, size factor, DRAWDOWN_PAUSE / PORTFOLIO_UNKNOWN or None, facts).

    Deposits and withdrawals are excluded with a deposit-neutral equity index: index_0 = 1 and, for
    each later close t, index_t = index_(t-1) x (equity_t - net flows dated after the previous close up
    to t) / equity_(t-1). A deposit therefore never counts as profit and never hides a drawdown. The
    drawdown is (peak index - index now) / peak index x 100. At or above drawdown_halve_pct every size is
    halved (factor 0.5); at or above drawdown_pause_pct new signals are paused."""
    if not equity_curve:
        return None, 1.0, PORTFOLIO_UNKNOWN, {"why": "equity history not known"}
    pts = sorted(equity_curve)
    if not all(known(e) and e > 0 for _, e in pts) or len({d for d, _ in pts}) != len(pts):
        return None, 1.0, PORTFOLIO_UNKNOWN, {"why": "equity history has an unknown, non-positive or repeated value"}
    if not all(known(a) for _, a in flows):
        return None, 1.0, PORTFOLIO_UNKNOWN, {"why": "a deposit or withdrawal amount is not known"}
    idx = peak = Decimal(1)
    prev_d, prev_e = pts[0][0], Decimal(str(pts[0][1]))
    for d, e in pts[1:]:
        flow = sum((Decimal(str(a)) for fd, a in flows if prev_d < fd <= d), Decimal(0))
        idx = idx * (Decimal(str(e)) - flow) / prev_e
        peak = max(peak, idx)
        prev_d, prev_e = d, Decimal(str(e))
    if idx <= 0:
        return None, 1.0, PORTFOLIO_UNKNOWN, {"why": "equity after flows is not positive"}
    dd = (peak - idx) / peak * 100
    net_flows = sum((Decimal(str(a)) for fd, a in flows if pts[0][0] < fd <= pts[-1][0]), Decimal(0))
    facts = {"drawdown_pct": float(round(dd, 4)), "index_now": float(round(idx, 6)), "index_peak": float(round(peak, 6)),
             "profit_excluding_deposits_inr": float(paise(Decimal(str(pts[-1][1])) - Decimal(str(pts[0][1])) - net_flows)),
             "halve_at_pct": halve_pct, "pause_at_pct": pause_pct}
    factor = 0.5 if dd >= Decimal(str(halve_pct)) else 1.0
    code = DRAWDOWN_PAUSE if dd >= Decimal(str(pause_pct)) else None
    return dd, factor, code, facts


# ---------------- kill switch ----------------
def kill_switch(index_symbol: str, index_status: str | None, quarantined_recent, market_open,
                cfg) -> list[str]:
    """Reasons the kill switch is on (empty = off): the index price is not LIVE, the clock is outside
    market hours, or at least `quarantine_max_events` ticks were quarantined in the last
    `quarantine_window_minutes`. An input that is not known turns it on."""
    out = []
    if market_open is None:
        out.append("market hours not known")
    elif not market_open:
        out.append("outside market hours")
    if index_status != "LIVE":
        out.append(f"{index_symbol} price is {index_status or 'not known'}, not LIVE")
    if not known(quarantined_recent):
        out.append("quarantined-tick count not known")
    elif quarantined_recent >= cfg.quarantine_max_events:
        out.append(f"{int(quarantined_recent)} ticks quarantined in the last {cfg.quarantine_window_minutes} minutes "
                   f"(spike at {cfg.quarantine_max_events})")
    return out
