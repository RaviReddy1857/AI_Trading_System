"""Loads and validates config.yaml. All risk limits are percentages of capital."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent


class Account(BaseModel):
    capital_inr: float = Field(gt=0)
    mode: Literal["paper", "live_manual"] = "paper"


class Risk(BaseModel):
    max_loss_per_trade_pct: float = Field(gt=0, le=5)
    max_loss_per_option_pct: float = Field(gt=0, le=10)
    daily_loss_stop_pct: float = Field(gt=0, le=10)
    monthly_loss_pause_pct: float = Field(gt=0, le=30)
    uncommitted_cash_pct: float = Field(ge=0, le=100)
    max_open_selling_spreads: int = Field(ge=0, le=10)
    drawdown_halve_pct: float = Field(gt=0, le=50)
    drawdown_pause_pct: float = Field(gt=0, le=50)


class Feed(BaseModel):
    mode: Literal["sim", "breeze"] = "sim"
    stale_after_seconds: int = Field(gt=0)
    max_jump_pct: float = Field(gt=0)


class Instrument(BaseModel):
    symbol: str
    breeze_code: str
    kind: Literal["index", "stock"]


class Server(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000

    @field_validator("host")
    @classmethod
    def local_only(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost"):
            raise ValueError("server.host must be 127.0.0.1 or localhost (local-only app)")
        return v


class Storage(BaseModel):
    db_path: str = "data/market.db"


class Settings(BaseModel):
    account: Account
    risk: Risk
    feed: Feed
    watchlist: list[Instrument]
    server: Server = Server()
    storage: Storage = Storage()

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
