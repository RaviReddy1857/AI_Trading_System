"""Phase 9: campaign tracking. A campaign is an original spread and every roll of it.

Each roll stays linked to its original trade (the campaign id is the original signal's id). For a
campaign the system keeps and shows, in rupees:
- cumulative realised P&L of the positions already closed (after charges)
- total hedge cost paid: premium paid for the bought options, less what they were sold back for
- total charges
- the best possible remaining outcome: the open position's maximum profit plus the P&L so far.
  If even that is a loss, the campaign says "recovery not possible from this position".

A campaign never changes the size of anything: a new signal after a closed campaign is built by the
normal rules at the normal size (there is no recovery sizing).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

NO_RECOVERY = "recovery not possible from this position"


@dataclass(frozen=True)
class CampaignPosition:
    """One spread of a campaign. Prices are per share; rupee fields are for the whole position."""
    roll_no: int                       # 0 = the original trade
    right: str
    short_strike: float
    long_strike: float
    expiry: date
    shares: int                        # lots x lot size
    sold_at: float                     # premium received for the sold option
    hedge_paid: float                  # premium paid for the bought option
    charges_inr: float                 # charges paid on this position so far (entry, plus exit once closed)
    max_profit_inr: float | None = None   # an open position's best case after its charges
    bought_back_at: float | None = None   # None = still open
    hedge_sold_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.bought_back_at is None

    @property
    def credit(self) -> float:
        return self.sold_at - self.hedge_paid

    @property
    def realised_inr(self) -> float | None:
        if self.is_open:
            return None
        back = self.bought_back_at - (self.hedge_sold_at or 0.0)
        return round((self.credit - back) * self.shares - self.charges_inr, 2)


def campaign_summary(campaign_id: str, stock: str, positions: Sequence[CampaignPosition]) -> dict:
    """The campaign's figures from its positions, oldest first. Unknown stays None, never zero."""
    ps = sorted(positions, key=lambda x: x.roll_no)
    closed = [x for x in ps if not x.is_open]
    still = [x for x in ps if x.is_open]
    realised = round(sum(x.realised_inr for x in closed), 2)
    hedge = round(sum((x.hedge_paid - (x.hedge_sold_at or 0.0)) * x.shares for x in ps), 2)
    charges = round(sum(x.charges_inr for x in ps), 2)
    best = None
    if not still:
        best = realised
    elif len(still) == 1 and still[0].max_profit_inr is not None:
        best = round(realised + still[0].max_profit_inr, 2)
    out = {"campaign_id": campaign_id, "stock": stock, "side": ps[0].right if ps else None,
           "positions": len(ps), "rolls": max((x.roll_no for x in ps), default=0), "open": bool(still),
           "realised_pnl_inr": realised, "hedge_cost_inr": hedge, "charges_inr": charges,
           "best_possible_outcome_inr": best,
           "recovery_possible": None if best is None else best >= 0,
           "message": "", "legs": [{
               "roll_no": x.roll_no, "right": x.right, "short_strike": x.short_strike, "long_strike": x.long_strike,
               "expiry": x.expiry.isoformat(), "shares": x.shares, "credit": round(x.credit, 2),
               "open": x.is_open, "realised_pnl_inr": x.realised_inr} for x in ps]}
    if still and best is not None and best < 0:
        out["message"] = NO_RECOVERY
    elif still and best is None:
        out["message"] = "best case unknown: the open position's maximum profit is not known"
    return out


def after_roll(realised_so_far_inr: float, close_pnl_inr: float, new_max_profit_inr: float) -> dict:
    """What a roll would leave: P&L realised once the current spread is closed, and the best the
    campaign can still end at with the new spread."""
    realised = round(realised_so_far_inr + close_pnl_inr, 2)
    best = round(realised + new_max_profit_inr, 2)
    return {"realised_pnl_inr": realised, "best_possible_outcome_inr": best, "recovery_possible": best >= 0,
            "message": "" if best >= 0 else NO_RECOVERY}


def campaigns_from_trades(trades: Sequence) -> list[dict]:
    """Campaign summaries of closed backtest trades (objects or dicts with campaign_id, roll_no, legs,
    costs, net_pnl, max_profit_inr). Every position is closed, so the best case is the result."""
    def get(t, k):
        return t[k] if isinstance(t, dict) else getattr(t, k)

    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(get(t, "campaign_id") or get(t, "signal_id"), []).append(t)
    out = []
    for cid, ts in groups.items():
        ts.sort(key=lambda t: get(t, "roll_no"))
        ps, seen_worst = [], False
        realised = 0.0
        for t in ts:
            legs = get(t, "legs")
            sell = next(x for x in legs if x["action"] == "SELL")
            buy = next(x for x in legs if x["action"] == "BUY")
            if get(t, "roll_no") > 0 and get(t, "max_profit_inr") is not None and realised + get(t, "max_profit_inr") < 0:
                seen_worst = True                    # at this roll even the best case was a loss
            realised += get(t, "net_pnl")
            ps.append(CampaignPosition(get(t, "roll_no"), sell["right"], sell["strike"], buy["strike"],
                                       date.fromisoformat(get(t, "expiry")), sell["lots"] * sell["lot_size"],
                                       sell["entry_price"], buy["entry_price"], get(t, "costs")["total"],
                                       get(t, "max_profit_inr"), sell["exit_price"], buy["exit_price"]))
        s = campaign_summary(cid, get(ts[0], "symbol"), ps)
        s["realised_pnl_inr"] = round(sum(get(t, "net_pnl") for t in ts), 2)      # the engine's own rounding, to the paise
        s["best_possible_outcome_inr"] = s["realised_pnl_inr"]
        s["rolled_when_recovery_was_not_possible"] = seen_worst
        s["variant"] = get(ts[0], "variant")
        s["last_exit"] = get(ts[-1], "exit_reason")
        out.append(s)
    return out
