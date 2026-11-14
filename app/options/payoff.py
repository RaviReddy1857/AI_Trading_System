"""Payoff arithmetic for the three option structures the system suggests: a long option, a
vertical credit spread and an iron condor. Pure functions on Decimal, rupees rounded to the paise.

Every structure is defined-risk: each sold option is paired with a bought option of the same
expiry and right further out of the money. `check_hedged` rejects anything else (UNHEDGED_SHORT).

Figures are per position of `lots` x `lot_size` shares:
- max_loss_inr and max_profit_inr are at expiry and INCLUDE `costs_inr` (the estimated charges to
  open and close). max_loss_inr is a positive number of rupees lost.
- breakevens are underlying prices at expiry, including costs: only prices at which the P&L really
  is zero. A credit structure whose costs are at least its credit cannot break even (empty). An iron
  condor with unequal wings whose credit after costs exceeds the narrower width has one breakeven.
Margin is not computed here (see app/options/margin.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..costs import money
from ..models import UNHEDGED_SHORT, unhedged_short_legs


class InvalidStructure(ValueError):
    pass


@dataclass(frozen=True)
class OptionLeg:
    """One option leg of a signal (architecture.md: action, right, strike, expiry, lots, price)."""
    action: str          # BUY | SELL
    right: str           # CE | PE
    strike: float
    expiry: date
    lots: int
    price: float         # premium per share used for the estimate
    lot_size: int

    def to_dict(self) -> dict:
        return {"action": self.action, "right": self.right, "strike": self.strike,
                "expiry": self.expiry.isoformat(), "lots": self.lots, "price": self.price,
                "lot_size": self.lot_size}


@dataclass(frozen=True)
class Payoff:
    structure: str                 # LONG_OPTION | CREDIT_SPREAD | IRON_CONDOR
    net_premium_inr: Decimal       # paid (negative) or received (positive) at entry, before costs
    costs_inr: Decimal
    max_loss_inr: Decimal          # positive rupees, costs included
    max_profit_inr: Decimal | None  # None = unlimited (long call)
    breakevens: tuple[Decimal, ...]
    width: Decimal | None = None   # strike distance of the widest spread, per share

    def as_dict(self, capital_inr: float | None = None) -> dict:
        out = {"structure": self.structure, "net_premium_inr": float(self.net_premium_inr),
               "costs_inr": float(self.costs_inr), "max_loss_inr": float(self.max_loss_inr),
               "max_profit_inr": None if self.max_profit_inr is None else float(self.max_profit_inr),
               "max_profit_note": "unlimited" if self.max_profit_inr is None else "",
               "breakevens": [float(b) for b in self.breakevens],
               "width": None if self.width is None else float(self.width)}
        if capital_inr:
            out["max_loss_pct_of_capital"] = round(float(self.max_loss_inr) / capital_inr * 100, 2)
        return out


def _d(x) -> Decimal:
    return Decimal(str(x))


def _positive(name: str, *xs) -> None:
    for x in xs:
        v = _d(x)
        if not v.is_finite() or v <= 0:
            raise InvalidStructure(f"{name} must be finite and above zero (got {x})")


def _qty(lots: int, lot_size: int) -> Decimal:
    if not (isinstance(lots, int) and isinstance(lot_size, int)) or lots < 1 or lot_size < 1:
        raise InvalidStructure(f"lots and lot size must be whole numbers of at least 1 (got {lots}, {lot_size})")
    return Decimal(lots * lot_size)


def _costs(costs) -> Decimal:
    c = _d(costs)
    if not c.is_finite() or c < 0:
        raise InvalidStructure(f"costs must be zero or more (got {costs})")
    return money(c)


def intrinsic(right: str, strike, spot) -> Decimal:
    k, s = _d(strike), _d(spot)
    return max(Decimal(0), s - k) if right == "CE" else max(Decimal(0), k - s)


def long_option(right: str, strike, premium, lot_size: int, lots: int = 1, costs=0) -> Payoff:
    """Bought call or put. Max loss = premium paid + costs. A call's profit is unlimited; a put's
    is the strike less the premium (the underlying cannot go below zero)."""
    if right not in ("CE", "PE"):
        raise InvalidStructure(f"right must be CE or PE (got {right})")
    _positive("strike and premium", strike, premium)
    q, c = _qty(lots, lot_size), _costs(costs)
    paid = money(_d(premium) * q)
    per_share_cost = c / q
    if right == "CE":
        return Payoff("LONG_OPTION", -paid, c, paid + c, None, (_d(strike) + _d(premium) + per_share_cost,))
    best = money(_d(strike) * q) - paid - c
    return Payoff("LONG_OPTION", -paid, c, paid + c, max(best, Decimal(0)),
                  (_d(strike) - _d(premium) - per_share_cost,))


def credit_spread(right: str, short_strike, long_strike, credit, lot_size: int, lots: int = 1, costs=0) -> Payoff:
    """Vertical credit spread: sell `short_strike`, buy `long_strike` further out of the money
    (puts: bought strike below the sold one; calls: above). `credit` is the net premium received
    per share. Max profit = credit - costs; max loss = (width - credit) + costs."""
    if right not in ("CE", "PE"):
        raise InvalidStructure(f"right must be CE or PE (got {right})")
    _positive("strikes and credit", short_strike, long_strike, credit)
    ks, kl, cr = _d(short_strike), _d(long_strike), _d(credit)
    if right == "PE" and not kl < ks:
        raise InvalidStructure("bull put spread: the bought put must be below the sold put")
    if right == "CE" and not kl > ks:
        raise InvalidStructure("bear call spread: the bought call must be above the sold call")
    width = abs(ks - kl)
    if cr >= width:
        raise InvalidStructure(f"credit {cr} is not below the strike width {width}: check the prices")
    q, c = _qty(lots, lot_size), _costs(costs)
    received = money(cr * q)
    per_share_cost = c / q
    be = ks - cr + per_share_cost if right == "PE" else ks + cr - per_share_cost
    return Payoff("CREDIT_SPREAD", received, c, money(width * q) - received + c, received - c,
                  (be,) if received > c else (), width)


def debit_spread(right: str, long_strike, short_strike, debit, lot_size: int, lots: int = 1, costs=0) -> Payoff:
    """Vertical debit spread: buy `long_strike` nearer the money, sell `short_strike` further out
    (bull call spread: bought call below the sold call; bear put spread: bought put above the sold
    put). `debit` is the net premium paid per share. Max loss = debit + costs; max profit =
    (width - debit) - costs."""
    if right not in ("CE", "PE"):
        raise InvalidStructure(f"right must be CE or PE (got {right})")
    _positive("strikes and debit", long_strike, short_strike, debit)
    kl, ks, db = _d(long_strike), _d(short_strike), _d(debit)
    if right == "CE" and not kl < ks:
        raise InvalidStructure("bull call spread: the bought call must be below the sold call")
    if right == "PE" and not kl > ks:
        raise InvalidStructure("bear put spread: the bought put must be above the sold put")
    width = abs(ks - kl)
    if db >= width:
        raise InvalidStructure(f"debit {db} is not below the strike width {width}: check the prices")
    q, c = _qty(lots, lot_size), _costs(costs)
    paid = money(db * q)
    per_share_cost = c / q
    best = money(width * q) - paid - c
    be = kl + db + per_share_cost if right == "CE" else kl - db - per_share_cost
    return Payoff("DEBIT_SPREAD", -paid, c, paid + c, best, (be,) if best > 0 else (), width)


def iron_condor(put_short, put_long, call_short, call_long, credit, lot_size: int, lots: int = 1,
                costs=0) -> Payoff:
    """Bull put spread plus bear call spread, same expiry. `credit` is the total net premium per
    share. Only one side can lose at expiry, so max loss = (wider width - credit) + costs."""
    _positive("strikes and credit", put_short, put_long, call_short, call_long, credit)
    ps, pl, cs, cl, cr = (_d(x) for x in (put_short, put_long, call_short, call_long, credit))
    if not (pl < ps < cs < cl):
        raise InvalidStructure("iron condor needs bought put < sold put < sold call < bought call")
    width = max(ps - pl, cl - cs)
    if cr >= width:
        raise InvalidStructure(f"credit {cr} is not below the strike width {width}: check the prices")
    q, c = _qty(lots, lot_size), _costs(costs)
    received = money(cr * q)
    per_share_cost = c / q
    # A breakeven is real only if it lies on that side's sloping part, between the sold and the bought
    # strike. With unequal wings the credit (after costs) can exceed the narrower width: that side then
    # never loses and has no breakeven.
    bes = []
    if received > c:
        low, high = ps - cr + per_share_cost, cs + cr - per_share_cost
        if low >= pl:
            bes.append(low)
        if high <= cl:
            bes.append(high)
    return Payoff("IRON_CONDOR", received, c, money(width * q) - received + c, received - c, tuple(bes), width)


def pnl_at_expiry(legs, spot, costs=0) -> Decimal:
    """Rupee P&L of a set of legs if the underlying settles at `spot`, after `costs`."""
    total = Decimal(0)
    for leg in legs:
        q = _qty(leg.lots, leg.lot_size)
        value = intrinsic(leg.right, leg.strike, spot)
        sign = Decimal(1) if leg.action == "BUY" else Decimal(-1)
        total += sign * (value - _d(leg.price)) * q
    return money(total) - _costs(costs)


def check_hedged(legs) -> list[str]:
    """[] if every sold option is covered by a bought option of the same expiry and right that is
    further out of the money with at least as many shares; otherwise ["UNHEDGED_SHORT"].
    Each bought option covers one sold option only. The rule itself lives in app/models.py, where
    the Signal model, the backtest entry and Store.save_backtest() enforce the same check."""
    return [UNHEDGED_SHORT] if unhedged_short_legs(legs) else []
