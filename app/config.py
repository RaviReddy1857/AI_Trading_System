"""Loads and validates config.yaml. All risk limits are percentages of capital."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent


class StrictModel(BaseModel):
    """Base for every config section: NaN and +/-infinity are rejected in all numeric
    fields, so no limit can silently become infinite or unknown."""
    model_config = ConfigDict(allow_inf_nan=False)


class Account(StrictModel):
    # Upper bound Rs 100 crore: far above this single-user account, but catches typos (extra zeros).
    capital_inr: float = Field(gt=0, le=1_000_000_000)
    mode: Literal["paper", "live_manual"] = "paper"


class Risk(StrictModel):
    max_loss_per_trade_pct: float = Field(gt=0, le=5)
    max_loss_per_option_pct: float = Field(gt=0, le=10)
    daily_loss_stop_pct: float = Field(gt=0, le=10)
    monthly_loss_pause_pct: float = Field(gt=0, le=30)
    uncommitted_cash_pct: float = Field(ge=0, le=100)
    max_open_selling_spreads: int = Field(ge=0, le=10)
    drawdown_halve_pct: float = Field(gt=0, le=50)
    drawdown_pause_pct: float = Field(gt=0, le=50)


class Feed(StrictModel):
    mode: Literal["sim", "breeze"] = "sim"
    stale_after_seconds: int = Field(gt=0)
    max_jump_pct: float = Field(gt=0)
    # Breeze live feed: pause between symbol subscriptions so ~50 requests are not fired at once.
    subscribe_pause_seconds: float = Field(0.25, ge=0, le=5)


class Instrument(StrictModel):
    symbol: str = Field(min_length=1)
    breeze_code: str = Field(min_length=1)
    kind: Literal["index", "stock"]
    sector: str | None = None        # stocks: the Industry column of the Nifty 50 file


class Server(StrictModel):
    host: str = "127.0.0.1"
    port: int = 8000

    @field_validator("host")
    @classmethod
    def local_only(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost"):
            raise ValueError("server.host must be 127.0.0.1 or localhost (local-only app)")
        return v


class Storage(StrictModel):
    """One SQLite file per feed mode, so simulated data can never mix with live data."""
    db_path_sim: str = "data/market_sim.db"
    db_path_live: str = "data/market_live.db"

    @model_validator(mode="after")
    def different_files(self) -> "Storage":
        if Path(self.db_path_sim).resolve() == Path(self.db_path_live).resolve():
            raise ValueError("storage.db_path_sim and storage.db_path_live must be different files")
        return self


EVENT_TYPES = ("EXPIRY", "RBI_POLICY", "BUDGET", "RESULTS", "MARKET_EVENT")


class Regime(StrictModel):
    """Thresholds for the market-regime rules (app/regime.py)."""
    index_symbol: str = "NIFTY"
    vix_symbol: str = "INDIAVIX"
    sma_fast_days: int = Field(20, ge=2, le=200)
    sma_slow_days: int = Field(50, ge=3, le=400)
    atr_days: int = Field(14, ge=2, le=100)
    volatile_range_atr_mult: float = Field(1.5, gt=0, le=10)
    vix_lookback_days: int = Field(252, ge=20, le=1000)
    vix_percentile: float = Field(80, gt=0, lt=100)
    vix_min_history_days: int = Field(200, ge=20, le=1000)
    breadth_sma_days: int = Field(20, ge=2, le=200)
    breadth_up_pct: float = Field(60, ge=0, le=100)
    breadth_down_pct: float = Field(40, ge=0, le=100)
    breadth_min_coverage_pct: float = Field(80, gt=0, le=100)
    event_types: list[str] = ["EXPIRY", "RBI_POLICY", "BUDGET", "MARKET_EVENT"]
    recompute_seconds: int = Field(60, ge=5, le=3600)

    @model_validator(mode="after")
    def consistent(self) -> "Regime":
        if self.sma_fast_days >= self.sma_slow_days:
            raise ValueError("regime.sma_fast_days must be below sma_slow_days")
        if self.breadth_down_pct >= self.breadth_up_pct:
            raise ValueError("regime.breadth_down_pct must be below breadth_up_pct")
        if self.vix_min_history_days > self.vix_lookback_days:
            raise ValueError("regime.vix_min_history_days cannot exceed vix_lookback_days")
        bad = [t for t in self.event_types if t not in EVENT_TYPES]
        if bad:
            raise ValueError(f"regime.event_types has unknown types {bad}; allowed {list(EVENT_TYPES)}")
        return self


class Bias(StrictModel):
    """FII/DII next-day bias: N straight days of FII net buying/selling."""
    streak_days: int = Field(3, ge=1, le=10)


class History(StrictModel):
    daily_days: int = Field(400, ge=60, le=2000)       # calendar days of daily bars to keep
    intraday_days: int = Field(10, ge=1, le=60)        # calendar days of 5-minute bars
    sim_seed_on_start: bool = True                     # sim mode: generate fake history
    # Breeze only (Breeze allows about 100 calls a minute): pause between history requests,
    # and how often to ask again when a reply comes back empty.
    request_pause_seconds: float = Field(0.7, ge=0, le=10)
    empty_retries: int = Field(2, ge=0, le=5)
    retry_pause_seconds: float = Field(3.0, ge=0, le=60)


class Calendar(StrictModel):
    events_csv: str = "data/events.csv"
    holidays_csv: str = "data/holidays.csv"
    fii_dii_csv: str = "data/fii_dii.csv"


class Settings(StrictModel):
    account: Account
    risk: Risk
    feed: Feed
    watchlist: list[Instrument]
    server: Server = Server()
    storage: Storage = Storage()
    regime: Regime = Regime()
    bias: Bias = Bias()
    history: History = History()
    calendar: Calendar = Calendar()

    @model_validator(mode="after")
    def regime_symbols_in_watchlist(self) -> "Settings":
        for attr in ("symbol", "breeze_code"):
            seen: set[str] = set()
            dupes = sorted({v for v in (getattr(i, attr) for i in self.watchlist) if v in seen or seen.add(v)})
            if dupes:
                raise ValueError(f"watchlist has duplicate {attr} values: {dupes}")
        symbols = {i.symbol for i in self.watchlist}
        for name in ("index_symbol", "vix_symbol"):
            sym = getattr(self.regime, name)
            if sym not in symbols:
                raise ValueError(f"regime.{name} '{sym}' is not in the watchlist")
        return self

    @property
    def is_live_feed(self) -> bool:
        return self.feed.mode == "breeze"

    def db_path(self) -> Path:
        """The SQLite file for the configured feed mode."""
        return self.path(self.storage.db_path_live if self.is_live_feed else self.storage.db_path_sim)

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p

    def rupees(self, pct: float) -> float:
        """Convert a percentage limit into rupees at the current capital."""
        return round(self.account.capital_inr * pct / 100.0, 2)

    def limits_in_rupees(self) -> dict[str, float]:
        r = self.risk
        return {
            "max_loss_per_trade": self.rupees(r.max_loss_per_trade_pct),
            "max_loss_per_option": self.rupees(r.max_loss_per_option_pct),
            "daily_loss_stop": self.rupees(r.daily_loss_stop_pct),
            "monthly_loss_pause": self.rupees(r.monthly_loss_pause_pct),
            "uncommitted_cash": self.rupees(r.uncommitted_cash_pct),
        }


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        return Settings.model_validate(yaml.safe_load(f))
