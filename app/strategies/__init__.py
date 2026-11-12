"""Strategy library (phase 3). Each strategy lives in its own file with thresholds in config.yaml."""
from __future__ import annotations

from ..config import Settings
from .base import MarketContext, Strategy
from .orb import OpeningRangeBreakout
from .range_reversion import RangeReversion
from .swing_breakout import SwingBreakout
from .swing_pullback import SwingPullback
from .vwap_pullback import VwapPullback

CLASSES = {c.id: c for c in (OpeningRangeBreakout, VwapPullback, RangeReversion, SwingPullback, SwingBreakout)}


def build_strategies(settings: Settings, ids: list[str] | None = None) -> list[Strategy]:
    cfg = settings.strategies
    params = {"orb": cfg.orb, "vwap_pullback": cfg.vwap_pullback, "range_reversion": cfg.range_reversion,
              "swing_pullback": cfg.swing_pullback, "swing_breakout": cfg.swing_breakout}
    unknown = [i for i in (ids or []) if i not in CLASSES]
    if unknown:
        raise ValueError(f"unknown strategy ids {unknown}; known {sorted(CLASSES)}")
    return [CLASSES[i](params[i]) for i in (ids or list(CLASSES))]


__all__ = ["CLASSES", "MarketContext", "Strategy", "build_strategies"]
