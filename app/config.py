"""Loads and validates config.yaml. All risk limits are percentages of capital."""
from __future__ import annotations

from datetime import date as _date, time as _dtime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Stored 5-minute bars starting at these IST times are unreliable in Breeze history (phase 3 diagnosis)
    # and are ignored by the point-in-time regime. They stay stored.
    ignore_5m_bar_starts: list[str] = ["15:15", "15:20"]

    @field_validator("ignore_5m_bar_starts")
    @classmethod
    def valid_times(cls, v: list[str]) -> list[str]:
        for x in v:
            _hhmm(x)
        return v

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
    daily_days: int = Field(400, ge=60, le=6000)       # calendar days of daily bars to keep (Breeze's own limit applies)
    intraday_days: int = Field(190, ge=1, le=1000)     # calendar days of 5-minute bars (>= 6 months for backtests)
    sim_seed_on_start: bool = True                     # sim mode: generate fake history
    # Breeze only (Breeze allows about 100 calls a minute): pause between history requests,
    # and how often to ask again when a reply comes back empty.
    request_pause_seconds: float = Field(0.7, ge=0, le=10)
    empty_retries: int = Field(2, ge=0, le=5)
    retry_pause_seconds: float = Field(3.0, ge=0, le=60)
    request_timeout_seconds: float = Field(60.0, gt=0, le=600)   # a request with no reply by then fails that symbol
    # Stock daily history is not adjusted for splits/bonuses/demergers: an overnight gap above this
    # is treated as a break, and no average, ATR, breakout or breadth value may span it.
    max_overnight_gap_pct: float = Field(25.0, gt=0, le=100)


class Calendar(StrictModel):
    events_csv: str = "data/events.csv"
    holidays_csv: str = "data/holidays.csv"
    fii_dii_csv: str = "data/fii_dii.csv"


# ---------------- phase 3: costs, strategies, backtest ----------------
def _hhmm(v: str) -> str:
    """An IST clock time 'HH:MM' inside the session 09:15-15:30."""
    try:
        t = _dtime.fromisoformat(v)
    except ValueError:
        raise ValueError(f"'{v}' is not a time like 14:30") from None
    if not (_dtime(9, 15) <= t <= _dtime(15, 30)):
        raise ValueError(f"{v} is outside market hours 09:15-15:30")
    return v


HHMM = Annotated[str, AfterValidator(_hhmm)]


def hhmm(v: str) -> _dtime:
    return _dtime.fromisoformat(v)


class Rate(StrictModel):
    """One published rate. value None = not sourced yet; any backtest needing it fails, naming it."""
    value: float | None = Field(None, ge=0)
    source: str = ""
    effective: str = ""


class EquityRates(StrictModel):
    brokerage_pct: Rate          # % of each leg's turnover
    stt_buy_pct: Rate
    stt_sell_pct: Rate
    exchange_txn_pct: Rate       # NSE transaction charge, both legs
    sebi_fee_pct: Rate           # both legs
    stamp_duty_buy_pct: Rate     # buy leg only
    ipft_pct: Rate               # NSE investor protection fund trust charge, both legs
    dp_charge_per_sell_inr: Rate | None = None   # delivery only: depository charge per scrip sold (before GST)


class OptionRates(StrictModel):
    brokerage_per_lot_inr: Rate  # per lot, each executed leg
    stt_sell_premium_pct: Rate
    exchange_txn_premium_pct: Rate
    sebi_fee_pct: Rate
    stamp_duty_buy_pct: Rate
    ipft_pct: Rate


class Slippage(StrictModel):
    """Adverse slippage per side, % of price. A setting, not a published rate."""
    intraday_equity: float = Field(0.05, ge=0, le=5)
    delivery_equity: float = Field(0.05, ge=0, le=5)
    options: float = Field(0.5, ge=0, le=20)


GST_BASES = ("brokerage", "exchange_txn", "sebi_fee", "ipft", "dp_charge")


class Costs(StrictModel):
    label: str = "standard rates, not yet verified against a contract note"
    verified_against_contract_note: bool = False
    plan: str = ""
    compiled_on: str = ""
    gst_pct: Rate
    gst_on: list[str] = list(GST_BASES)
    intraday_equity: EquityRates
    delivery_equity: EquityRates
    options: OptionRates
    slippage_pct: Slippage = Slippage()

    @field_validator("gst_on")
    @classmethod
    def known_bases(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in GST_BASES]
        if bad:
            raise ValueError(f"costs.gst_on has unknown items {bad}; allowed {list(GST_BASES)}")
        return v


class OrbParams(StrictModel):
    range_minutes: int = Field(15, ge=5, le=60)
    breakout_buffer_pct: float = Field(0.05, ge=0, le=2)
    max_range_pct: float = Field(1.5, gt=0, le=10)       # skip days with a very wide opening range
    min_volume_mult: float = Field(1.2, ge=0, le=10)     # breakout bar volume vs opening-range average
    reward_risk: float = Field(2.0, gt=0, le=10)
    last_signal_time: HHMM = "11:30"


class VwapParams(StrictModel):
    touch_pct: float = Field(0.15, ge=0, le=2)           # low within this % of VWAP counts as a touch
    min_bars_on_side: int = Field(6, ge=1, le=60)         # bars that closed on the trend side of VWAP before the pullback
    stop_buffer_pct: float = Field(0.10, ge=0, le=2)
    reward_risk: float = Field(1.5, gt=0, le=10)
    first_signal_time: HHMM = "09:45"
    last_signal_time: HHMM = "14:30"


class RangeParams(StrictModel):
    band_bars: int = Field(20, ge=5, le=100)
    band_sd: float = Field(2.0, gt=0, le=5)
    stop_buffer_pct: float = Field(0.10, ge=0, le=2)
    min_reward_risk: float = Field(1.0, gt=0, le=10)
    first_signal_time: HHMM = "09:45"
    last_signal_time: HHMM = "14:30"


class SwingPullbackParams(StrictModel):
    fast_sma: int = Field(20, ge=2, le=200)
    slow_sma: int = Field(50, ge=3, le=400)
    pullback_pct: float = Field(1.0, ge=0, le=10)         # low within this % of the fast average
    atr_days: int = Field(14, ge=2, le=100)
    stop_atr_mult: float = Field(1.5, gt=0, le=10)
    reward_risk: float = Field(2.0, gt=0, le=10)
    max_hold_days: int = Field(10, ge=1, le=60)


class SwingBreakoutParams(StrictModel):
    lookback_days: int = Field(20, ge=5, le=250)
    volume_mult: float = Field(1.5, gt=0, le=20)
    atr_days: int = Field(14, ge=2, le=100)
    stop_atr_mult: float = Field(1.5, gt=0, le=10)
    reward_risk: float = Field(2.0, gt=0, le=10)
    max_hold_days: int = Field(10, ge=1, le=60)


class OrbV2Params(StrictModel):
    """Phase 3b. Opening range of range_minutes; a close beyond it on volume above min_volume_mult x the
    same time-of-day bar's average over the previous volume_lookback_sessions sessions."""
    range_minutes: int = Field(30, ge=5, le=90)
    min_volume_mult: float = Field(1.5, gt=0, le=10)
    volume_lookback_sessions: int = Field(20, ge=5, le=60)
    min_volume_sessions: int = Field(15, ge=1, le=60)    # sessions that must have the same time-of-day bar
    reward_risk: float = Field(2.0, gt=0, le=10)
    last_signal_time: HHMM = "11:30"
    min_stop_cost_mult: float = Field(5.0, ge=0, le=50)   # stop distance >= this x round-trip cost

    @model_validator(mode="after")
    def enough(self) -> "OrbV2Params":
        if self.min_volume_sessions > self.volume_lookback_sessions:
            raise ValueError("orb_v2.min_volume_sessions cannot exceed volume_lookback_sessions")
        return self


class RangeV2Params(StrictModel):
    """Phase 3b. v1 entry; stop = further of the 3-bar extreme (+buffer) and min_stop_cost_mult x round-trip
    cost; target = band average; skipped if the target is closer than min_target_cost_mult x round-trip cost."""
    band_bars: int = Field(20, ge=5, le=100)
    band_sd: float = Field(2.0, gt=0, le=5)
    stop_buffer_pct: float = Field(0.10, ge=0, le=2)
    first_signal_time: HHMM = "09:45"
    last_signal_time: HHMM = "14:30"
    min_stop_cost_mult: float = Field(5.0, ge=0, le=50)
    min_target_cost_mult: float = Field(2.0, ge=0, le=50)


class SwingPullbackV2Params(SwingPullbackParams):
    index_sma_days: int = Field(50, ge=2, le=400)        # longs only while NIFTY closes above this average


class SwingBreakoutV2Params(SwingBreakoutParams):
    index_sma_days: int = Field(50, ge=2, le=400)


class Strategies(StrictModel):
    inactive: list[str] = []          # strategy ids kept in code but not run by default (retired)
    orb: OrbParams = OrbParams()
    vwap_pullback: VwapParams = VwapParams()
    range_reversion: RangeParams = RangeParams()
    swing_pullback: SwingPullbackParams = SwingPullbackParams()
    swing_breakout: SwingBreakoutParams = SwingBreakoutParams()
    orb_v2: OrbV2Params = OrbV2Params()
    range_reversion_v2: RangeV2Params = RangeV2Params()
    swing_pullback_v2: SwingPullbackV2Params = SwingPullbackV2Params()
    swing_breakout_v2: SwingBreakoutV2Params = SwingBreakoutV2Params()

    @model_validator(mode="after")
    def known_inactive(self) -> "Strategies":
        known = set(type(self).model_fields) - {"inactive"}
        bad = [x for x in self.inactive if x not in known]
        if bad:
            raise ValueError(f"strategies.inactive has unknown ids {bad}")
        return self


class Backtest(StrictModel):
    intraday_exit_time: HHMM = "15:15"        # intraday trades are closed by this IST time
    last_entry_time: HHMM = "14:45"           # no new intraday entries after this
    position_cap_pct: float = Field(25.0, gt=0, le=100)   # position value cap, % of capital
    holdout_pct: float = Field(20.0, ge=5, le=50)         # final untouched share of sessions
    window_sessions: int = Field(21, ge=5, le=250)        # walk-forward test window
    step_sessions: int = Field(21, ge=1, le=250)
    min_trades: int = Field(30, ge=1, le=1000)            # below this a result is LOW SAMPLE
    reports_dir: str = "data/backtests"
    # Fixed holdout start per bar interval (phase 3b). Sessions on or after it are the holdout; empty = the
    # last holdout_pct of sessions. Fixed so that adding older history never moves the holdout back over
    # data that strategies were already developed on.
    holdout_start: dict[Literal["5minute", "1day"], _date] = {}
    # First session a backtest may test, per bar interval (earlier bars still feed indicators). Phase 3b:
    # Breeze daily history before 2019-01-22 has days at the wrong price scale (see docs/STATUS.md).
    test_start: dict[Literal["5minute", "1day"], _date] = {}


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
    costs: Costs | None = None               # required by backtests (phase 3)
    strategies: Strategies = Strategies()
    backtest: Backtest = Backtest()

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
