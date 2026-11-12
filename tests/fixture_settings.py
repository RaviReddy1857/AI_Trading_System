"""Settings for tests: the real config.yaml (limits, thresholds) with a small, fixed
watchlist, so no test depends on the full Nifty 50 list in config.yaml."""
from __future__ import annotations

from pathlib import Path

from app.config import ROOT, Settings
from app.config import load_settings as _load_real

FIXTURE_WATCHLIST = [
    {"symbol": "NIFTY", "breeze_code": "NIFTY", "kind": "index"},
    {"symbol": "BANKNIFTY", "breeze_code": "CNXBAN", "kind": "index"},
    {"symbol": "RELIANCE", "breeze_code": "RELIND", "kind": "stock", "sector": "Oil Gas & Consumable Fuels"},
    {"symbol": "HDFCBANK", "breeze_code": "HDFBAN", "kind": "stock", "sector": "Financial Services"},
    {"symbol": "ICICIBANK", "breeze_code": "ICIBAN", "kind": "stock", "sector": "Financial Services"},
    {"symbol": "INFY", "breeze_code": "INFTEC", "kind": "stock", "sector": "Information Technology"},
    {"symbol": "TCS", "breeze_code": "TCS", "kind": "stock", "sector": "Information Technology"},
    {"symbol": "INDIAVIX", "breeze_code": "INDVIX", "kind": "index"},
]

__all__ = ["ROOT", "FIXTURE_WATCHLIST", "load_settings"]


def load_settings(path: str | Path | None = None) -> Settings:
    """Like app.config.load_settings, but with FIXTURE_WATCHLIST."""
    raw = _load_real(path).model_dump()
    raw["watchlist"] = [dict(i) for i in FIXTURE_WATCHLIST]
    return Settings.model_validate(raw)
