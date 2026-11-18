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
SAME_SYMBOL_ALREADY_APPROVED = "SAME_SYMBOL_ALREADY_APPROVED"
EXPOSURE_UNKNOWN = "EXPOSURE_UNKNOWN"
BULLISH, BEARISH, NEUTRAL = "BULLISH", "BEARISH", "NEUTRAL"
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
    # the same-exposure rule (`same_symbol_check`): the exposure to the underlying (`exposure_of`: BULLISH, BEARISH,
    # NEUTRAL; None = could not be classified) and the IST session it was approved or opened in. session None = not
    # recorded: the rule does not count it (fixtures); the real book (`journal.pool_account`) always sets it.
    exposure: str | None = None
    session: date | None = None

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
        for name in ("risk_inr", "committed_inr"):           # a P&L may be negative; risk and capital in use may not
            v = getattr(p, name)
            if known(v) and v < 0:
                out.append(f"position {p.position_id} ({p.symbol}): {name} is negative ({v:,.2f}): invalid, never "
                           f"netted against other positions")
    return out


def committed_inr(positions) -> Decimal:
    """Capital committed: cash in positions plus margin blocked (positions must be known)."""
    return sum((paise(p.committed_inr) for p in positions), Decimal(0))


def capital_fit(capital: float, committed, new_inr, uncommitted_pct: float) -> tuple[str, float, dict]:
    """(label, shortfall in rupees, facts). FITS: committed + new within capital x (1 - uncommitted_pct);
    TIGHT: within capital but not within that; NEEDS_MORE_CAPITAL: above capital (shortfall = the excess).
    Exactly at a boundary is inside it. Raises ValueError for a capital that is not a positive finite number
    or a commitment that is negative or not finite (never summed)."""
    if not (known(capital) and capital > 0):
        raise ValueError(f"capital {capital!r} is not a positive finite number")
    for name, v in (("committed", committed), ("new_inr", new_inr)):
        if not (known(float(v)) and v >= 0):
            raise ValueError(f"{name} {v!r} is negative or not finite")
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
    # what makes up the commitment, one row per position or reservation (shown on the signal card)
    facts["items"] = [{"id": p.position_id, "symbol": p.symbol, "origin": p.origin,
                       "committed_inr": float(paise(p.committed_inr))} for p in positions]
    if label == FITS:
        return None, "within the capital pool", facts
    return (RISK_BUDGET_USED, f"capital committed would be Rs {facts['after_inr']:,.2f}, above Rs "
                              f"{facts['usable_inr']:,.2f} ({100 - uncommitted_pct:g}% of capital; "
                              f"{uncommitted_pct:g}% stays uncommitted)", facts)


def _leg(x):
    """(action, right, strike, lots) of a leg object or a stored leg dict; None when any is missing or invalid."""
    get = x.get if isinstance(x, dict) else (lambda k: getattr(x, k, None))
    a, r, k, n = get("action"), get("right"), get("strike"), get("lots")
    if a not in ("BUY", "SELL") or r not in ("CE", "PE") or not (known(k) and k > 0):
        return None
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return None
    return a, r, Decimal(str(k)), n


def exposure_of(direction: str | None, legs) -> str | None:
    """What the trade needs the underlying to do: BULLISH, BEARISH or NEUTRAL; None = cannot be classified.

    Cash: LONG = BULLISH, SHORT = BEARISH. Options: from the expiry payoff of the legs (premiums left out: they shift
    every point equally). First the open tails beyond the strikes: gaining on one side only (bought call, sold put)
    sets the side; exposed on both sides (strangles, straddles) is NEUTRAL. With both tails flat (spreads): a payoff
    peak or trough strictly inside the strikes (condors, butterflies, whatever their wing widths) is NEUTRAL; else the
    higher end sets it (bull put and bull call spreads BULLISH; bear call and bear put spreads BEARISH).
    `Position.view` starts from the sold legs, which calls a bull call (debit) spread BEARISH; this does not."""
    if not legs:
        return {"LONG": BULLISH, "SHORT": BEARISH}.get(direction)
    parsed = [_leg(x) for x in legs]
    if any(p is None for p in parsed):
        return None
    strikes = sorted({k for _, _, k, _ in parsed})
    lo, hi = strikes[0], strikes[-1]
    d = (hi - lo) or lo / 10

    def payoff(s: Decimal) -> Decimal:
        total = Decimal(0)
        for a, r, k, n in parsed:
            v = max(Decimal(0), s - k) if r == "CE" else max(Decimal(0), k - s)
            total += v * n if a == "BUY" else -v * n
        return total

    def sign(x: Decimal) -> int:
        return (x > 0) - (x < 0)

    gain_up = sign(payoff(hi + 2 * d) - payoff(hi + d))         # rising beyond every strike
    gain_down = sign(payoff(lo - 2 * d) - payoff(lo - d))       # falling beyond every strike
    if gain_up or gain_down:
        if gain_up == gain_down:
            return NEUTRAL                                      # gains (or loses) both ways
        score = gain_up - gain_down
        return BULLISH if score > 0 else BEARISH if score < 0 else NEUTRAL
    ends = (payoff(lo - d), payoff(hi + d))
    inside = [payoff(k) for k in strikes]
    if max(inside) > max(ends) or min(inside) < min(ends):
        return NEUTRAL                                          # a range bet: condor, butterfly
    return BULLISH if ends[1] > ends[0] else BEARISH if ends[1] < ends[0] else NEUTRAL


def same_symbol_check(symbol: str, exposure: str | None, positions, session) -> tuple[str | None, str]:
    """One approval per underlying, exposure and session: a signal whose underlying already has an approved signal
    awaiting my decision or an open position with the same exposure (`exposure_of`), approved or opened in the same
    IST session, is blocked (SAME_SYMBOL_ALREADY_APPROVED), whatever strategy or version issued either. Unknown never
    passes: a proposed exposure that cannot be classified, or a same-session holding on the underlying whose exposure
    is not known, blocks with EXPOSURE_UNKNOWN. `positions` is the live book at the moment of the check."""
    if exposure is None:
        return EXPOSURE_UNKNOWN, f"exposure of the {symbol} signal to its underlying cannot be classified"
    same = [p for p in positions if p.symbol == symbol and p.session is not None and p.session == session]
    unknown = [p for p in same if p.exposure not in (BULLISH, BEARISH, NEUTRAL)]
    if unknown:
        return (EXPOSURE_UNKNOWN, f"{symbol}: exposure of {', '.join(p.position_id for p in unknown[:3])} (this session) "
                                  "cannot be classified")
    hits = [p for p in same if p.exposure == exposure]
    if not hits:
        return None, f"no approved or open {exposure} {symbol} in this session"
    ids = ", ".join(p.position_id for p in hits[:3])
    return (SAME_SYMBOL_ALREADY_APPROVED, f"{symbol} {exposure} already has an approved signal or open position this "
                                          f"session ({ids}); one per underlying, exposure and session")


def selling_spread_check(positions, is_selling: bool, max_open: int) -> tuple[str | None, str]:
    """max_open_selling_spreads (index option selling), the proposed one included."""
    if not is_selling:
        return None, "not a selling spread"
    n = sum(1 for p in positions if p.selling_spread)
    if n + 1 > max_open:
        return RISK_BUDGET_USED, f"{n} selling spread(s) open; one more is above max_open_selling_spreads {max_open}"
    return None, f"{n + 1} of {max_open} selling spreads"
