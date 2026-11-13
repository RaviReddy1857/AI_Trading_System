"""Phase 4: option chains, lot sizes and expiries from the instrument list, payoff arithmetic,
implied volatility and delta, option strategies and their backtests.

Index options (NIFTY, BANKNIFTY) may be bought, or sold as hedged spreads only. Stock options are
BUY-ONLY in this phase. Nothing here places an order."""
from __future__ import annotations

from ..config import Settings
from .base import OptionContext, OptionStrategy
from .credit_spread import CreditSpread
from .iron_condor import IronCondor
from .long_option import LongOption

OPTION_CLASSES = {c.id: c for c in (LongOption, CreditSpread, IronCondor)}


def build_option_strategies(settings: Settings, ids: list[str] | None = None) -> list[OptionStrategy]:
    unknown = [i for i in (ids or []) if i not in OPTION_CLASSES]
    if unknown:
        raise ValueError(f"unknown option strategy ids {unknown}; known {sorted(OPTION_CLASSES)}")
    out = []
    for i in ids or list(OPTION_CLASSES):
        st = OPTION_CLASSES[i](getattr(settings.strategies, i))
        status = settings.strategies.status_of(i)
        st.alerts_enabled, st.mode, st.status_label = status.alerts, status.mode, status.label
        out.append(st)
    return out


__all__ = ["OPTION_CLASSES", "OptionContext", "OptionStrategy", "build_option_strategies"]
