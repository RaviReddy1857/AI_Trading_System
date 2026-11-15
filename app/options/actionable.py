"""Phase 9: research candidates versus actionable signals.

A research candidate comes from the last valid session snapshot (daily bars and the option chain stored
for that session) and is labelled with its timestamps. It is never acted on as it stands.

An actionable signal additionally needs, at the moment it is shown:
- market hours, and a LIVE underlying price (DATA_STALE otherwise);
- a fresh option chain for the expiry (no older than options.chain.max_age_seconds) with a bid and an
  ask on every leg (DATA_STALE otherwise);
- a current margin entry for the complete proposed spread (MARGIN_UNKNOWN);
- the open positions known and within the portfolio limits (PORTFOLIO_UNKNOWN, CORRELATED_LIMIT,
  CORRELATION_UNKNOWN);
- and every other hard block passing at that moment (the strategy's own evaluation of the live context).
The portfolio is known only with the open positions AND a verified closed and manual-trade history;
until the phase 6 journal exists it is unknown (PORTFOLIO_UNKNOWN), so nothing is actionable yet.
"""
from __future__ import annotations

from datetime import datetime

from ..quality import is_market_hours
from .portfolio import portfolio_check

RESEARCH_LABEL = "research candidate"


def actionable_check(settings, signal, ctx, now: datetime, chain_as_of: dict, holdings, sectors: dict, corr,
                     holidays=(), history_verified: bool = False, universe=None) -> tuple[bool, list[str]]:
    """(actionable, reasons it is not). `signal`: the strategy's signal for `ctx` (None = no complete
    setup). `chain_as_of`: expiry -> time of the stored chain snapshot. `holdings`: the open positions
    (portfolio.Holding), or None when they are not known. `history_verified`: the closed and manual-trade
    history is verified (live: never before the phase 6 journal exists, so PORTFOLIO_UNKNOWN).
    `universe`: the stocks the correlated groups are built from (default: the watchlist stocks, the
    groups the page shows). `corr(a, b)`: correlation known now."""
    why: list[str] = []
    if signal is None:
        return False, ["no complete setup passes the rules"]
    if ctx.historical:
        why.append("DATA_STALE: a historical context is never actionable")
    if not is_market_hours(now, holidays):
        why.append("DATA_STALE: outside market hours - a research snapshot, not a live price")
    if ctx.status != "LIVE":
        why.append(f"DATA_STALE: the underlying's price status is {ctx.status}")
    max_age = settings.options.chain.max_age_seconds
    for leg in signal.legs:
        as_of = chain_as_of.get(leg.expiry)
        if as_of is None or (now - as_of).total_seconds() > max_age:
            why.append(f"DATA_STALE: option chain for {leg.expiry:%d-%b-%Y} is "
                       + ("not stored" if as_of is None else f"{(now - as_of).total_seconds() / 60:.0f} minutes old")
                       + f" (at most {max_age // 60} minutes)")
            break
    for leg in signal.legs:
        q = ctx.quotes.quote(leg.expiry, leg.strike, leg.right)
        if q is None or not q.has_bid_ask:
            why.append(f"DATA_STALE: no bid and ask for {leg.strike:g} {leg.right}")
    if (signal.details or {}).get("margin_inr") is None:
        why.append("MARGIN_UNKNOWN: no current margin entry for the complete spread")
    risk = float((signal.details or {}).get("max_loss_inr") or 0.0)
    if universe is None:
        universe = [i.symbol for i in settings.watchlist if i.kind == "stock"]
    code, text, _ = portfolio_check(signal.symbol, risk, holdings, sectors, corr,
                                    settings.stock_option_selling.portfolio, ctx.capital_inr,
                                    history_verified=history_verified, universe=universe)
    if code:
        why.append(f"{code}: {text}")
    return not why, why
