"""Strategy library (phase 3, revised in phase 3b). Each strategy lives in its own file with thresholds in
config.yaml. v1 strategies are kept unchanged for comparison; v2 versions have their own ids."""
from __future__ import annotations

from ..config import Settings
from .base import MarketContext, Strategy
from .orb import OpeningRangeBreakout
from .orb_v2 import OpeningRangeBreakoutV2
from .range_reversion import RangeReversion
from .range_reversion_v2 import RangeReversionV2
from .swing_breakout import SwingBreakout
from .swing_breakout_v2 import SwingBreakoutV2
from .swing_pullback import SwingPullback
from .swing_pullback_v2 import SwingPullbackV2
from .vwap_pullback import VwapPullback

CLASSES = {c.id: c for c in (OpeningRangeBreakout, VwapPullback, RangeReversion, SwingPullback, SwingBreakout,
                             OpeningRangeBreakoutV2, RangeReversionV2, SwingPullbackV2, SwingBreakoutV2)}


def intraday_round_trip_pct(settings: Settings) -> float:
    """Estimated intraday round-trip cost (charges + slippage, % of price) for a position at the
    position cap, from config.yaml costs. Raises MissingCostRates if a needed rate is empty."""
    from ..costs import CostModel
    value = settings.account.capital_inr * settings.backtest.position_cap_pct / 100
    return CostModel(settings.costs).round_trip_pct("intraday_equity", value)


def build_strategies(settings: Settings, ids: list[str] | None = None) -> list[Strategy]:
    """The named strategies, or every strategy not listed in strategies.inactive."""
    cfg = settings.strategies
    unknown = [i for i in (ids or []) if i not in CLASSES]
    if unknown:
        raise ValueError(f"unknown strategy ids {unknown}; known {sorted(CLASSES)}")
    chosen = ids or [i for i in CLASSES if i not in cfg.inactive]
    cost = None
    out = []
    for i in chosen:
        cls = CLASSES[i]
        if cls.uses_cost_estimate:
            cost = cost if cost is not None else intraday_round_trip_pct(settings)
            out.append(cls(getattr(cfg, i), cost))
        else:
            out.append(cls(getattr(cfg, i)))
    for st in out:
        status = cfg.status_of(st.id)
        st.alerts_enabled, st.mode, st.status_label = status.alerts, status.mode, status.label
    return out


__all__ = ["CLASSES", "MarketContext", "Strategy", "build_strategies", "intraday_round_trip_pct"]
