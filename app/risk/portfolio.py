"""Phase 5 account and portfolio state: open positions (paper and manual), capital committed, margin
blocked, cash free, the uncommitted-cash rule, the selling-spread count and the capital-fit label.

Nothing here is assumed. A position whose risk, committed capital or open P&L is not known makes the
portfolio unknown (PORTFOLIO_UNKNOWN), and unknown positions (None) block every signal. Until the
phase 6 journal records positions, the live portfolio is unknown.

The correlated-group limits of stock option spreads (phase 9) stay in `app.options.portfolio`; this
module does not duplicate them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

PORTFOLIO_UNKNOWN = "PORTFOLIO_UNKNOWN"
RISK_BUDGET_USED = "RISK_BUDGET_USED"
CAPITAL_UNKNOWN = "CAPITAL_UNKNOWN"
FITS, TIGHT, NEEDS_MORE_CAPITAL = "FITS", "TIGHT", "NEEDS_MORE_CAPITAL"

# position kinds
CASH, OPTION_BUY, INDEX_SELLING_SPREAD, STOCK_SPREAD, OTHER = (
    "CASH", "OPTION_BUY", "INDEX_SELLING_SPREAD", "STOCK_SPREAD", "OTHER")


def known(x) -> bool:
    """A real, finite number (None, NaN and infinity are unknown)."""
    try:
        return x is not None and not isinstance(x, bool) and math.isfinite(x)
    except TypeError:
        return False


def paise(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class OpenPosition:
    """One open position, paper or manual. Rupee fields None = not known."""
    position_id: str
    symbol: str
    kind: str                           # CASH | OPTION_BUY | INDEX_SELLING_SPREAD | STOCK_SPREAD | OTHER
    risk_inr: float | None              # loss at the stop, or the max loss, costs included
    committed_inr: float | None         # cash in the position, or the margin blocked
    open_pnl_today_inr: float | None    # change of the open P&L since the previous close (since entry if opened today)
    open_pnl_month_inr: float | None    # change of the open P&L since the month began (since entry if opened this month)
    origin: str = "PAPER"               # PAPER | MANUAL

    @property
    def selling_spread(self) -> bool:
        return self.kind == INDEX_SELLING_SPREAD


@dataclass(frozen=True)
class AccountState:
    """What the risk engine knows about the account at one moment. None = not known (blocks)."""
    capital_inr: float | None
    positions: tuple[OpenPosition, ...] | None
    realised_today_inr: float | None
    realised_month_inr: float | None
    equity_curve: tuple[tuple[date, float], ...] | None     # account equity at each close, deposits included
    flows: tuple[tuple[date, float], ...] = field(default_factory=tuple)   # capital_log: deposit +, withdrawal -
    history_verified: bool = False      # closed and manual-trade history verified (phase 9 portfolio limits)


def position_problems(positions) -> list[str]:
    """Why the open positions are not known well enough to check a limit (empty = known)."""
    if positions is None:
        return ["open positions are not known (the phase 6 journal records them)"]
    out = []
    for p in positions:
        for name in ("risk_inr", "committed_inr", "open_pnl_today_inr", "open_pnl_month_inr"):
            if not known(getattr(p, name)):
                out.append(f"position {p.position_id} ({p.symbol}): {name} not known")
    return out


def committed_inr(positions) -> Decimal:
    """Capital committed: cash in positions plus margin blocked (positions must be known)."""
    return sum((paise(p.committed_inr) for p in positions), Decimal(0))


def capital_fit(capital: float, committed, new_inr, uncommitted_pct: float) -> tuple[str, float, dict]:
    """(label, shortfall in rupees, facts). FITS: committed + new within capital x (1 - uncommitted_pct);
    TIGHT: within capital but not within that; NEEDS_MORE_CAPITAL: above capital (shortfall = the excess).
    Exactly at a boundary is inside it."""
    cap = paise(capital)
    after = paise(committed) + paise(new_inr)
    usable = paise(cap * (Decimal(100) - Decimal(str(uncommitted_pct))) / Decimal(100))
    facts = {"committed_inr": float(paise(committed)), "new_inr": float(paise(new_inr)), "after_inr": float(after),
             "usable_inr": float(usable), "capital_inr": float(cap), "cash_free_inr": float(cap - paise(committed))}
    if after <= usable:
        return FITS, 0.0, facts
    if after <= cap:
        return TIGHT, 0.0, facts
    return NEEDS_MORE_CAPITAL, float(after - cap), facts


def pool_check(capital: float, positions, new_inr, uncommitted_pct: float) -> tuple[str | None, str, dict]:
    """The uncommitted-cash rule: (RISK_BUDGET_USED or None, why, facts)."""
    label, shortfall, facts = capital_fit(capital, committed_inr(positions), new_inr, uncommitted_pct)
    facts["capital_fit"] = label
    if label == FITS:
        return None, "within the capital pool", facts
    return (RISK_BUDGET_USED, f"capital committed would be Rs {facts['after_inr']:,.2f}, above Rs "
                              f"{facts['usable_inr']:,.2f} ({100 - uncommitted_pct:g}% of capital; "
                              f"{uncommitted_pct:g}% stays uncommitted)", facts)


def selling_spread_check(positions, is_selling: bool, max_open: int) -> tuple[str | None, str]:
    """max_open_selling_spreads (index option selling), the proposed one included."""
    if not is_selling:
        return None, "not a selling spread"
    n = sum(1 for p in positions if p.selling_spread)
    if n + 1 > max_open:
        return RISK_BUDGET_USED, f"{n} selling spread(s) open; one more is above max_open_selling_spreads {max_open}"
    return None, f"{n + 1} of {max_open} selling spreads"
