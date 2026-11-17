"""Loads and validates config.yaml. All risk limits are percentages of capital."""
from __future__ import annotations

from datetime import date as _date, time as _dtime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
# Your own overrides, never committed (.gitignore): any keys of config.yaml, merged over it when the file exists.
LOCAL_CONFIG = ROOT / "config.local.yaml"
# Set to "1" to read config.yaml only (the test suite does: tests/conftest.py), whatever config.local.yaml says.
NO_LOCAL_ENV = "TRADING_NO_LOCAL_CONFIG"


class StrictModel(BaseModel):
    """Base for every config section: NaN and +/-infinity are rejected in all numeric
    fields, so no limit can silently become infinite or unknown."""
    model_config = ConfigDict(allow_inf_nan=False)


class Account(StrictModel):
    # Upper bound Rs 100 crore: far above this single-user account, but catches typos (extra zeros).
    capital_inr: float = Field(gt=0, le=1_000_000_000)
    mode: Literal["paper", "live_manual"] = "paper"


class KillSwitch(StrictModel):
    """Phase 5 kill switch: all signals stop on a quarantined-tick spike (also on a stale index feed and
    outside market hours, which need no setting)."""
    quarantine_window_minutes: int = Field(5, ge=1, le=120)
    quarantine_max_events: int = Field(20, ge=1, le=100_000)   # this many or more in the window = spike


class Risk(StrictModel):
    max_loss_per_trade_pct: float = Field(gt=0, le=5)
    max_loss_per_option_pct: float = Field(gt=0, le=10)
    daily_loss_stop_pct: float = Field(gt=0, le=10)
    monthly_loss_pause_pct: float = Field(gt=0, le=30)
    uncommitted_cash_pct: float = Field(ge=0, le=100)
    max_open_selling_spreads: int = Field(ge=0, le=10)
    drawdown_halve_pct: float = Field(gt=0, le=50)
    drawdown_pause_pct: float = Field(gt=0, le=50)
    # phase 5: sector and correlation limits (counts and ratios, no rupee amounts)
    max_trades_per_sector: int = Field(2, ge=1, le=20)           # open trades per sector, the proposed one included
    correlation_window: int = Field(60, ge=20, le=252)           # sessions of daily returns, completed sessions only
    correlation_threshold: float = Field(0.8, gt=0, lt=1)        # ABOVE this = one position (risk counted together)
    correlation_min_overlap: int = Field(50, ge=10, le=252)      # common returns needed, else CORRELATION_UNKNOWN
    kill_switch: KillSwitch = KillSwitch()

    @model_validator(mode="after")
    def ordered(self) -> "Risk":
        if self.drawdown_halve_pct >= self.drawdown_pause_pct:
            raise ValueError("risk.drawdown_halve_pct must be below risk.drawdown_pause_pct")
        if self.correlation_min_overlap > self.correlation_window:
            raise ValueError("risk.correlation_min_overlap cannot exceed risk.correlation_window")
        return self


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


# BOARD_MEETING and CORPORATE_ACTION (with a symbol) keep a stock off the phase 9 shortlist until they pass.
# COVERAGE: a row saying that a stock's announced events were checked at a source, on a date, through a date.
EVENT_TYPES = ("EXPIRY", "RBI_POLICY", "BUDGET", "RESULTS", "MARKET_EVENT", "BOARD_MEETING", "CORPORATE_ACTION",
               "COVERAGE")


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
    # rows imported from the external repo (licence: personal use only, no redistribution): gitignored overlays,
    # merged on load; the user's own files win on the same date. Blank: external/<name> next to the user's file
    events_external_csv: str = ""
    fii_dii_external_csv: str = ""

    def overlay(self, kind: str) -> str:
        own, set_ = {"events": (self.events_csv, self.events_external_csv),
                     "fii_dii": (self.fii_dii_csv, self.fii_dii_external_csv)}[kind]
        if set_:
            return set_
        p = own.replace("\\", "/")
        folder, _, name = p.rpartition("/")
        return f"{folder + '/' if folder else ''}external/{name.removesuffix('.csv')}_external.csv"


class ExternalData(StrictModel):
    """One-time import from the external GitHub repo aswinv90/indian-market-historical-data (a local clone
    outside app/; only its data files are read, none of its scripts is run)."""
    repo_path: str = "../external_data/indian-market-historical-data"
    history_since: _date = _date(2019, 1, 22)           # daily OHLCV and RBI dates from here on
    corporate_actions_since: _date = _date(2019, 1, 1)
    positioning_since: _date = _date(2024, 1, 1)
    crosscheck_mismatch_pct: float = Field(0.5, gt=0, le=50)   # daily close vs Breeze: report above this


class DataQuality(StrictModel):
    """Pre-registered 2026-11-15 (app/data_quality.py): a stock-year is excluded from the Phase 9 backtests when
    its unexplained Breeze-vs-repo close mismatches exceed max_unexplained_pct of its sessions, or its Breeze
    closes jump more than max_one_day_jump_pct in one day without a split, bonus or demerger, unless it is
    listed in resolved_stock_years ("SYMBOL:YEAR", the resolution recorded in docs/STATUS.md)."""
    max_unexplained_pct: float = Field(5.0, gt=0, le=100)
    max_one_day_jump_pct: float = Field(20.0, gt=0, le=1000)
    resolved_stock_years: list[str] = []


class DataStatusLimits(StrictModel):
    """When a data source counts as STALE on the Data status page."""
    fii_dii_max_trading_days: int = Field(1, ge=0, le=30)
    coverage_max_age_days: int = Field(7, ge=1, le=60)
    corporate_actions_max_age_days: int = Field(7, ge=1, le=60)
    rbi_decision_grace_days: int = Field(3, ge=0, le=30)      # a scheduled meeting without a decision row after this
    inbox_dir: str = "data/inbox"
    # progress of the stock option history download (written by app.options.stock_backtest, read by this page)
    download_status_file: str = "data/option_download_status.json"
    # a board-meetings export gives coverage only if it can be the whole list: at least this many companies,
    # and meetings within this many days of both ends of the declared range
    board_meetings_min_symbols: int = Field(100, ge=10, le=5000)
    board_meetings_edge_days: int = Field(7, ge=0, le=60)


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


def _clock(v: str) -> str:
    """Any IST clock time 'HH:MM' (phase 6: the pre-market plan and the end-of-day summary are outside the session)."""
    try:
        _dtime.fromisoformat(v)
    except ValueError:
        raise ValueError(f"'{v}' is not a time like 09:00") from None
    return v


CLOCK = Annotated[str, AfterValidator(_clock)]


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


def _whole_5m_bars(v: int) -> int:
    """An opening range is a whole number of 5-minute bars; 17 minutes would silently become 15."""
    if v % 5:
        raise ValueError(f"range_minutes must be a multiple of 5 (whole 5-minute bars), got {v}")
    return v


class OrbParams(StrictModel):
    range_minutes: Annotated[int, AfterValidator(_whole_5m_bars)] = Field(15, ge=5, le=60)
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
    range_minutes: Annotated[int, AfterValidator(_whole_5m_bars)] = Field(30, ge=5, le=90)
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


# ---------------- phase 4: option strategies ----------------
class LongOptionParams(StrictModel):
    """Directional option buying: an intraday or swing signal on the underlying triggers buying a
    near-the-money call (LONG trigger) or put (SHORT trigger). Stops and targets are % of the premium."""
    trigger_strategies: list[str] = ["swing_pullback_v2", "orb_v2"]   # underlying strategies whose signals trigger a buy
    max_iv_rank: float = Field(60.0, ge=0, le=100)            # buy only when IV rank is low or moderate
    min_days_to_expiry: int = Field(7, ge=1, le=60)           # nearest expiry at least this many calendar days away
    stop_pct_of_premium: float = Field(40.0, gt=0, lt=100)    # stop = premium x (1 - this%)
    target_pct_of_premium: float = Field(80.0, gt=0, le=1000)  # target = premium x (1 + this%)
    max_hold_days: int = Field(10, ge=1, le=60)               # sessions (swing triggers); intraday triggers exit the same day
    stock_exit_days_before_expiry: int = Field(7, ge=5, le=30)   # stock options: out before expiry week (physical settlement)
    index_exit_days_before_expiry: int = Field(0, ge=0, le=30)


class CreditSpreadParams(StrictModel):
    """Hedged credit spread on an index, monthly expiry: bull put spread in TRENDING_UP, bear call
    spread in TRENDING_DOWN. Every sold option is paired with a bought option of the same expiry."""
    min_iv_rank: float = Field(50.0, ge=0, le=100)            # sell only when IV rank is high
    max_short_delta: float = Field(0.20, gt=0, lt=0.5)        # |delta| of the sold strike at most this
    hedge_width_pct: float = Field(1.0, gt=0, le=10)          # bought strike this % of spot further out (rounded to a strike)
    # phase 5 decision (user, 2026-11-16): when one lot at that width risks more than the option risk budget
    # (risk.max_loss_per_option_pct of capital), try narrower widths down to this many points and take the widest
    # that sizes to at least one lot; none fits = NO TRADE (ZERO_SIZE). The risk limit itself is never raised.
    min_hedge_width_points: float = Field(100.0, gt=0, le=5000)
    entry_min_dte: int = Field(20, ge=1, le=90)               # calendar days to expiry at the signal
    entry_max_dte: int = Field(35, ge=1, le=120)
    min_credit_pct_of_width: float = Field(8.0, ge=0, le=100)  # skip if the credit is too thin for the risk
    profit_target_pct_of_credit: float = Field(50.0, gt=0, lt=100)   # close when this share of the credit is kept
    stop_multiple_of_credit: float = Field(2.0, gt=1, le=10)  # close when buying back costs this x the credit
    exit_days_before_expiry: int = Field(1, ge=0, le=30)      # out at the open of the first session this close to expiry

    @model_validator(mode="after")
    def window(self) -> "CreditSpreadParams":
        if self.entry_min_dte > self.entry_max_dte:
            raise ValueError("entry_min_dte cannot exceed entry_max_dte")
        if self.exit_days_before_expiry >= self.entry_min_dte:
            raise ValueError("exit_days_before_expiry must be below entry_min_dte")
        return self


class IronCondorParams(CreditSpreadParams):
    """Iron condor on NIFTY weekly options: a bull put spread and a bear call spread together.
    RANGE regime only (so never on an EVENT day)."""
    max_short_delta: float = Field(0.15, gt=0, lt=0.5)
    hedge_width_pct: float = Field(0.5, gt=0, le=10)
    entry_min_dte: int = Field(3, ge=1, le=90)
    entry_max_dte: int = Field(7, ge=1, le=120)
    exit_days_before_expiry: int = Field(0, ge=0, le=30)


class StrategyStatus(StrictModel):
    """Whether a strategy may produce alerts (phase 6) and in which mode, with a plain-language label
    from its backtest evidence. Strategies without an entry get no alerts and are paper-only."""
    alerts: bool = False
    mode: Literal["paper_only", "live"] = "paper_only"
    label: str = "not yet evaluated"


class Strategies(StrictModel):
    inactive: list[str] = []          # strategy ids kept in code but not run by default in backtests (retired)
    status: dict[str, StrategyStatus] = {}   # alerts / paper-only / evidence label per strategy id
    orb: OrbParams = OrbParams()
    vwap_pullback: VwapParams = VwapParams()
    range_reversion: RangeParams = RangeParams()
    swing_pullback: SwingPullbackParams = SwingPullbackParams()
    swing_breakout: SwingBreakoutParams = SwingBreakoutParams()
    orb_v2: OrbV2Params = OrbV2Params()
    range_reversion_v2: RangeV2Params = RangeV2Params()
    swing_pullback_v2: SwingPullbackV2Params = SwingPullbackV2Params()
    swing_breakout_v2: SwingBreakoutV2Params = SwingBreakoutV2Params()
    long_option: LongOptionParams = LongOptionParams()          # phase 4
    credit_spread: CreditSpreadParams = CreditSpreadParams()
    iron_condor: IronCondorParams = IronCondorParams()

    @model_validator(mode="after")
    def known_inactive(self) -> "Strategies":
        known = set(type(self).model_fields) - {"inactive", "status"}
        bad = [x for x in self.inactive if x not in known]
        if bad:
            raise ValueError(f"strategies.inactive has unknown ids {bad}")
        phase9 = {"stock_option_selling", "reversal_put_spread", "reversal_bull_call_spread"}   # own config section
        bad = [x for x in self.status if x not in known | phase9]
        if bad:
            raise ValueError(f"strategies.status has unknown ids {bad}")
        return self

    def status_of(self, sid: str) -> StrategyStatus:
        return self.status.get(sid, StrategyStatus())

    def alert_ids(self) -> list[str]:
        """Strategy ids allowed to produce alerts (phase 6 reads this)."""
        return sorted(s for s, st in self.status.items() if st.alerts)


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


class OptionUnderlying(StrictModel):
    """One underlying whose options the system follows."""
    expiries: list[Literal["weekly", "monthly"]] = ["monthly"]   # which expiries get a chain and signals
    # Strike spacing assumed for EXPIRED contracts in backtests (the instrument list only has current
    # contracts). Live strikes always come from the instrument list. None = try round steps near the price.
    backtest_strike_step: float | None = Field(None, gt=0)
    dividend_yield_pct: float = Field(0.0, ge=0, le=20)


class OptionChainCfg(StrictModel):
    refresh_seconds: int = Field(300, ge=30, le=3600)      # breeze mode: reload index chains this often in market hours
    max_age_seconds: int = Field(900, ge=30, le=86400)     # a stored chain older than this is stale: no signal
    max_spread_pct: float = Field(10.0, gt=0, le=100)      # a traded strike's bid-ask width, % of mid, at most this
    min_open_interest_lots: int = Field(100, ge=0)         # a traded strike's open interest, in lots, at least this
    request_pause_seconds: float = Field(0.7, ge=0, le=10)
    # every chain call to Breeze in any 60 seconds (the live index refresh and the 15:20 stock capture together)
    max_calls_per_minute: int = Field(60, ge=1, le=600)


class OptionMargin(StrictModel):
    csv: str = "data/margins.csv"                          # margins you copy from ICICI's margin calculator
    max_age_days: int = Field(7, ge=1, le=90)              # an older entry counts as unknown


class OptionBacktest(StrictModel):
    """Option backtests (app/options/backtest.py). Trades are 1 lot each; sizing is phase 5."""
    underlyings: dict[str, list[str]] = {                  # strategy id -> underlyings tested
        "credit_spread": ["NIFTY", "BANKNIFTY"], "iron_condor": ["NIFTY"],
        "long_option": ["NIFTY", "BANKNIFTY", "RELIANCE", "HDFCBANK", "INFY", "SBIN"]}
    start: dict[str, _date] = {}                           # strategy id -> first session tested (default backtest.test_start)
    trigger: str = "swing_pullback_v2"                     # long_option: the daily-bar trigger that is backtested
    max_requests_per_run: int = Field(1500, ge=0, le=5000)  # --download: stop asking Breeze after this many requests
    # Option-history requests to Breeze per calendar day (IST), counted in the store across runs. Every
    # option-history download stops when the day's count reaches it and resumes on a later day.
    daily_request_budget: int = Field(3500, ge=0, le=20000)
    strike_search_steps: int = Field(8, ge=1, le=30)       # how far the short-strike search may walk


class Options(StrictModel):
    """Phase 4. Implied volatility and delta are computed by code (Black-Scholes on the option's mid price)."""
    risk_free_rate_pct: float = Field(6.5, ge=0, le=20)
    iv_reference_symbol: str = "INDIAVIX"                  # index IV measure for IV rank (stocks: stored ATM IV history)
    iv_rank_lookback_days: int = Field(252, ge=20, le=1000)
    iv_rank_min_history_days: int = Field(200, ge=20, le=1000)
    underlyings: dict[str, OptionUnderlying] = {}          # index underlyings; watchlist stocks use `stocks`
    stocks: OptionUnderlying = OptionUnderlying()
    chain: OptionChainCfg = OptionChainCfg()
    margin: OptionMargin = OptionMargin()
    backtest: OptionBacktest = OptionBacktest()

    @model_validator(mode="after")
    def consistent(self) -> "Options":
        if self.iv_rank_min_history_days > self.iv_rank_lookback_days:
            raise ValueError("options.iv_rank_min_history_days cannot exceed iv_rank_lookback_days")
        return self


# ---------------- phase 9: stock option selling ----------------
class ScoreWeights(StrictModel):
    """Weights of the six score factors (app/options/score.py); equal by default."""
    iv_rank: float = Field(1.0, ge=0, le=10)
    iv_falling: float = Field(1.0, ge=0, le=10)
    rel_strength: float = Field(1.0, ge=0, le=10)
    strike_distance: float = Field(1.0, ge=0, le=10)
    range_profile: float = Field(1.0, ge=0, le=10)
    liquidity: float = Field(1.0, ge=0, le=10)


class ScoreConfig(StrictModel):
    """The 0-100 candidate score (section 4). A ranking aid, never a probability."""
    weights: ScoreWeights = ScoreWeights()
    iv_peak_sessions: int = Field(20, ge=2, le=120)                # iv_falling: IV against its peak of these sessions
    iv_falling_ratio: float = Field(0.9, gt=0, lt=1)               # at or below 90% of the peak = falling (full points)
    hist_fall_sessions: int = Field(21, ge=2, le=120)              # hist_fall_percentile: 21-session falls ...
    hist_fall_percentile: float = Field(90.0, gt=0, lt=100)        # ... their 90th percentile, since 2019-01-22


class StockSellingBacktest(StrictModel):
    """Backtest and option-history download of stock option selling."""
    priority_stocks: int = Field(30, ge=1, le=250)      # the N most liquid F&O stocks are downloaded and tested
    liquidity_lookback_days: int = Field(60, ge=5, le=500)   # liquidity = median daily traded value over these sessions
    # Execution: "intraday" = synchronized 5-minute bars of every leg (entry, stop, target and every fill; no stale
    # price, no daily fallback; a campaign with a missing bar is data-incomplete and left out) or "daily" = daily
    # closes with fills at the next session's open. The first one is the main result; reports show both.
    exit_checks: list[Literal["intraday", "daily"]] = ["intraday", "daily"]
    exit_on_strike_breach: bool = True                  # close (next open) when the stock closes beyond the sold strike
    # Capital the Phase 9 backtests are run at (the user's stock-option capital). Every limit stays a % of it.
    capital_inr: float = Field(1000000.0, gt=0)
    # Download order: every priority stock is completed for this year and later (newest first) before any earlier
    # year is asked for (user, 2026-11-14).
    download_first_years_from: int = Field(2023, ge=2019, le=2100)
    # Exit policies compared on the SAME entries (daily exit check): "stop" = close at the stop; "roll_rules" =
    # roll a threatened spread when the roll rules allow it, else close; "roll_always" = never stop, roll a
    # losing spread into the next month at the forced exit, without limit.
    exit_policies: list[Literal["stop", "roll_rules", "roll_always"]] = ["stop", "roll_rules", "roll_always"]

    @model_validator(mode="after")
    def distinct(self) -> "StockSellingBacktest":
        if not self.exit_checks or len(set(self.exit_checks)) != len(self.exit_checks):
            raise ValueError("stock_option_selling.backtest.exit_checks must list each mode once")
        if not self.exit_policies or self.exit_policies[0] != "stop" or len(set(self.exit_policies)) != len(self.exit_policies):
            raise ValueError("stock_option_selling.backtest.exit_policies must start with 'stop' and list each policy once")
        return self


class TrendBlock(StrictModel):
    """Falling-stock block (DOWNTREND_BLOCK): no put selling in a stock that closed below sma_long, made a
    52-week low (lowest close of `low_52w_sessions`) within `new_low_lookback`, or whose
    `rel_strength_lookback` return trails NIFTY by more than `rel_strength_max_lag_pct` points. Mirrored for
    call selling in a strong uptrend (UPTREND_BLOCK)."""
    enabled: bool = True
    new_low_lookback: int = Field(20, ge=1, le=120)
    low_52w_sessions: int = Field(252, ge=60, le=400)
    rel_strength_lookback: int = Field(63, ge=10, le=260)
    rel_strength_max_lag_pct: float = Field(5.0, gt=0, le=100)


class MarketBlock(StrictModel):
    """MARKET_WEAK: no put-selling signal when NIFTY closed below its sma_long or fewer than
    `market_breadth_min_pct` of the watchlist stocks closed above their sma_mid."""
    enabled: bool = True
    market_breadth_min_pct: float = Field(40.0, ge=0, le=100)
    min_coverage_pct: float = Field(80.0, gt=0, le=100)            # share of stocks with usable data, else breadth is unknown


class PortfolioLimits(StrictModel):
    """CORRELATED_LIMIT / CORRELATION_UNKNOWN / PORTFOLIO_UNKNOWN (app/options/portfolio.py)."""
    enabled: bool = True
    max_open_spreads: int = Field(6, ge=1, le=100)
    max_total_risk_pct: float = Field(10.0, gt=0, le=100)          # of capital, every open position counted
    correlation_window: int = Field(60, ge=10, le=500)             # sessions of daily returns
    correlation_threshold: float = Field(0.7, gt=0, le=1)
    correlation_min_overlap: int = Field(50, ge=5, le=500)         # common observations needed
    group_max_positions: int = Field(2, ge=1, le=100)
    group_max_risk_pct: float = Field(5.0, gt=0, le=100)

    @model_validator(mode="after")
    def overlap(self) -> "PortfolioLimits":
        if self.correlation_min_overlap > self.correlation_window:
            raise ValueError("portfolio.correlation_min_overlap cannot exceed correlation_window")
        return self


class CampaignLimit(StrictModel):
    """Per-stock campaign limit (STOCK_LOSS_LIMIT): after `stock_loss_max_stops` stopped trades or a net loss of
    `stock_loss_max_pct_capital` in one stock within `stock_loss_window_days`, no new signal in that stock for
    `stock_cooloff_days`. Closed manual positions count too, once recorded."""
    enabled: bool = True
    stock_loss_window_days: int = Field(90, ge=7, le=730)
    stock_loss_max_stops: int = Field(2, ge=1, le=20)
    stock_loss_max_pct_capital: float = Field(1.5, gt=0, le=100)
    stock_cooloff_days: int = Field(30, ge=1, le=365)


class RollRules(StrictModel):
    """When a threatened spread may be rolled instead of closed. Anything else is "exit"."""
    max_rolls: int = Field(1, ge=0, le=12)                         # rolls allowed in one campaign
    # roll net credit (new credit - debit to close the old spread - all roll charges) / replacement width, above this %
    min_roll_net_credit_pct_of_width: float = Field(10.0, ge=0, le=100)
    stuck_time_value_pct: float = Field(20.0, ge=0, le=100)        # sold option in the money with time value below this % of its price


class BullCallParams(StrictModel):
    """The bull call spread candidate of a confirmed reversal: buy the call nearest the money, sell the
    call `width_strikes` strikes higher, same expiry. One lot; the loss is limited to the debit."""
    width_strikes: int = Field(2, ge=1, le=10)
    entry_min_dte: int = Field(20, ge=8, le=90)
    entry_max_dte: int = Field(35, ge=8, le=120)              # the same 20-35 days as the put spread (user, 2026-11-14)
    target_pct_of_max_profit: float = Field(60.0, gt=0, le=100)   # sell when the spread is worth debit + this % of the max profit
    stop_pct_of_debit: float = Field(50.0, gt=0, lt=100)          # sell when it has lost this % of the debit
    exit_days_before_expiry: int = Field(7, ge=7, le=30)          # stock options settle physically

    @model_validator(mode="after")
    def window(self) -> "BullCallParams":
        if self.entry_min_dte > self.entry_max_dte or self.exit_days_before_expiry >= self.entry_min_dte:
            raise ValueError("reversal.bull_call: need exit_days_before_expiry < entry_min_dte <= entry_max_dte")
        return self


class Reversal(StrictModel):
    """Reversal detector for washed-out stocks. Watch list and candidates only: nothing here alerts
    until `alerts_approved` is set by the user."""
    enabled: bool = True
    alerts_approved: bool = False                                  # no reversal alert is produced while this is false
    washed_out_drawdown_pct: float = Field(25.0, gt=0, lt=100)     # this far below the 52-week high ...
    min_sessions_below_long: int = Field(63, ge=1, le=400)         # ... with at least this many sessions in a row below sma_long
    high_lookback_sessions: int = Field(252, ge=60, le=400)        # ... and below the trend block's long average
    base_no_new_low_sessions: int = Field(15, ge=3, le=120)        # base: the low is at least this many sessions old ...
    range_sessions: int = Field(10, ge=3, le=60)                   # ... and the average daily range of the last N sessions
    range_compare_sessions: int = Field(20, ge=3, le=120)          # base_range_contraction: ATR(range_sessions) below
    range_shrink_ratio: float = Field(0.8, gt=0, le=1)             # range_shrink_ratio x ATR(range_compare_sessions)
    base_valid_sessions: int = Field(10, ge=1, le=60)              # a confirmation must come this soon after a base-forming day
    confirm_sma_days: int = Field(20, ge=5, le=100)
    volume_sma_days: int = Field(20, ge=5, le=100)
    relative_return_sessions: int = Field(10, ge=2, le=60)         # return ahead of NIFTY over these sessions
    lift_sessions: int = Field(20, ge=1, le=120)                   # the falling-stock block stays lifted this long (put spreads below the base low only)
    rearm_sessions: int = Field(40, ge=1, le=250)                  # no second confirmation of the same stock sooner than this
    outcome_sessions: list[int] = [10, 20, 40]
    bull_call: BullCallParams = BullCallParams()

    @model_validator(mode="after")
    def horizons(self) -> "Reversal":
        if not self.outcome_sessions or any(not (1 <= n <= 250) for n in self.outcome_sessions):
            raise ValueError("stock_option_selling.reversal.outcome_sessions must list sessions between 1 and 250")
        return self


class StockProfile(StrictModel):
    """Range and speed profile, scenario history and the levels the open-position alerts use."""
    windows_years: list[int] = [2, 5]
    state_lookback_sessions: int = Field(20, ge=5, le=120)
    trending_efficiency: float = Field(0.30, gt=0, lt=1)           # net move / path length at or above this = trending
    holding_sessions: int = Field(20, ge=1, le=120)                # a spread is held about this long
    scenario_horizon_days: int = Field(30, ge=1, le=180)           # calendar days
    scenario_min_cases: int = Field(10, ge=1, le=1000)             # fewer cases: LOW SAMPLE
    # vix_spike: India VIX up at least vix_spike_rise_pct over vix_spike_sessions sessions, or closing above its
    # vix_spike_percentile over the previous vix_percentile_lookback sessions.
    vix_spike_rise_pct: float = Field(20.0, gt=0, le=500)
    vix_spike_sessions: int = Field(5, ge=1, le=60)
    vix_spike_percentile: float = Field(80.0, gt=0, lt=100)
    vix_percentile_lookback: int = Field(252, ge=20, le=1000)

    @model_validator(mode="after")
    def windows(self) -> "StockProfile":
        if not self.windows_years or any(not (1 <= y <= 20) for y in self.windows_years):
            raise ValueError("stock_option_selling.profile.windows_years must list years between 1 and 20")
        return self


# ---------------- phase 9 M3: physical settlement of stock options ----------------
class DeliveryMargin(StrictModel):
    """The broker's expiry-week delivery margin on near-month in-the-money (ITM) and close-to-the-money (CTM)
    stock options: a share of (VaR + ELM + adhoc margin), rising each trading day to expiry. Empty schedule =
    margin unknown (never zero)."""
    confirmed: bool = False
    source: str = ""
    near_month_only: bool = True
    ctm_within_pct_of_spot: float | None = Field(None, gt=0, le=50)   # CTM: strikes within this % of spot
    base: str = ""                                                   # what the % is of, as published
    # trading days before expiry (0 = expiry day) -> % of the base; e.g. {4: 10, 3: 25, 2: 45, 1: 70, 0: 100}
    schedule_pct: dict[int, float] = {}
    collected_daily_at: HHMM | None = None                           # on these days, collected at this IST time ...
    collected_daily_on_days: list[int] = []
    collected_hourly_on_days: list[int] = []                         # ... and hourly on these

    @model_validator(mode="after")
    def check(self) -> "DeliveryMargin":
        s = self.schedule_pct
        if s:
            days = sorted(s, reverse=True)
            if days != list(range(days[0], -1, -1)):
                raise ValueError("delivery_margin.schedule_pct: one entry per trading day down to 0 (expiry day)")
            vals = [s[d] for d in days]
            if any(not 0 < v <= 100 for v in vals) or vals != sorted(vals):
                raise ValueError("delivery_margin.schedule_pct: percentages in (0, 100], rising towards expiry")
        for d in self.collected_daily_on_days + self.collected_hourly_on_days:
            if d not in s:
                raise ValueError(f"delivery_margin: collection day {d} is not in schedule_pct")
        if set(self.collected_daily_on_days) & set(self.collected_hourly_on_days):
            raise ValueError("delivery_margin: a day is either daily or hourly collection, not both")
        if self.collected_daily_on_days and self.collected_daily_at is None:
            raise ValueError("delivery_margin: collected_daily_on_days needs collected_daily_at")
        if self.confirmed and (not s or self.ctm_within_pct_of_spot is None or not self.source):
            raise ValueError("delivery_margin: confirmed needs schedule_pct, ctm_within_pct_of_spot and source")
        return self


class AutoCloseSource(StrictModel):
    """One published statement of when the broker closes open option positions itself on expiry day."""
    source: str
    long_options: HHMM | None = None
    short_options: HHMM | None = None
    futures: HHMM | None = None
    note: str = ""


class AutoClose(StrictModel):
    """The broker's own closing of positions on expiry day. Every statement is kept; alert deadlines use the
    EARLIEST time any statement gives for any leg the position holds (a spread's bought leg closed early
    leaves its sold leg unhedged)."""
    confirmed: bool = False
    sources: list[AutoCloseSource] = []

    def deadline(self, has_long: bool, has_short: bool) -> _dtime | None:
        """Earliest stated time for the position's option legs; None when no statement gives one."""
        times = [hhmm(t) for s in self.sources
                 for t, held in ((s.long_options, has_long), (s.short_options, has_short)) if held and t]
        return min(times, default=None)


class FreshPositionBlock(StrictModel):
    """Whether the broker blocks new positions in the last days before expiry. Empty rule = not known."""
    confirmed: bool = False
    rule: str = ""
    source: str = ""


class SettlementObligation(StrictModel):
    """What settlement needs: a short put assigned = buy strike x lot size per lot in cash; a short call
    assigned = deliver the shares from demat, by the deadline below on expiry day."""
    confirmed: bool = False                        # the obligation itself
    source: str = ""
    call_shares_in_demat_by: HHMM | None = None    # IST, expiry day
    call_shares_deadline_confirmed: bool = False


class BrokerSettlement(StrictModel):
    """ICICI Direct's physical-settlement rules, entered by the user from ICICI; each rule carries its own
    `confirmed` flag. Unconfirmed rules are shown as "ICICI rule not confirmed"."""
    delivery_margin: DeliveryMargin = DeliveryMargin()
    auto_close: AutoClose = AutoClose()
    fresh_position_block: FreshPositionBlock = FreshPositionBlock()
    obligation: SettlementObligation = SettlementObligation()


class Settlement(StrictModel):
    """Settlement-readiness alerts for short stock option legs (alerts and estimates only)."""
    alert_from_trading_days: int = Field(7, ge=1, le=20)      # alerts start this many trading days before expiry
    urgent_from_trading_days: int = Field(5, ge=0, le=20)     # URGENT from here for an ITM or near-the-money short leg
    near_money_pct: float = Field(5.0, gt=0, le=50)           # near the money: strike within this % of spot
    broker: BrokerSettlement = BrokerSettlement()

    @model_validator(mode="after")
    def check(self) -> "Settlement":
        if self.urgent_from_trading_days > self.alert_from_trading_days:
            raise ValueError("settlement.urgent_from_trading_days cannot exceed alert_from_trading_days")
        return self


class AfterCloseShortlist(StrictModel):
    """The stock option shortlist run by the app every trading day (app/options/after_close.py): the priority stocks'
    chains at `chain_time`, while bids and asks are live; then, after the close, the bar history and the shortlist; in
    the end-of-day summary."""
    enabled: bool = True
    chain_time: CLOCK = "15:20"                            # IST; in market hours, before the close (15:30)
    start_time: CLOCK = "15:31"                            # IST; not before the close (15:30)
    # today's bars not there yet (daily bar not published, 5-minute bars incomplete): try again this often until then
    bars_retry_minutes: int = Field(15, ge=5, le=120)
    bars_deadline: CLOCK = "17:30"                         # IST; after start_time

    @field_validator("start_time")
    @classmethod
    def _after_close(cls, v: str) -> str:
        if hhmm(v) < _dtime(15, 30):
            raise ValueError("after_close.start_time must be at or after the close, 15:30")
        return v

    @model_validator(mode="after")
    def _deadline_after_start(self) -> "AfterCloseShortlist":
        if hhmm(self.bars_deadline) <= hhmm(self.start_time):
            raise ValueError("after_close.bars_deadline must be after start_time")
        return self

    @field_validator("chain_time")
    @classmethod
    def _in_market_hours(cls, v: str) -> str:
        if not _dtime(9, 15) <= hhmm(v) < _dtime(15, 30):
            raise ValueError("after_close.chain_time must be in market hours (09:15 to before 15:30): bids and asks "
                             "are live only then")
        return v


class StockOptionSelling(StrictModel):
    """Phase 9: monthly hedged credit spreads on F&O stocks (bull put in an up trend, bear call in a down
    trend). Every sold option is paired with a bought option of the same expiry. Stock options settle
    physically, so no position is open in expiry week. Names follow section 2 of the Phase 9
    specification; docs/implementation.md has the parameter table."""
    enabled: bool = True
    after_close: AfterCloseShortlist = AfterCloseShortlist()
    min_iv_rank: float = Field(50.0, ge=0, le=100)            # sell only when the stock's IV rank is at least this
    min_open_interest_lots: int = Field(200, ge=0)            # each traded strike: open interest in lots
    max_bid_ask_pct_of_mid: float = Field(5.0, gt=0, le=100)   # each traded strike, live: bid-ask width, % of mid
    short_delta_max: float = Field(0.20, gt=0, lt=0.5)
    hedge_strikes_out: list[int] = [2, 3]                     # bought strike: this many strikes further out (min, max)
    max_risk_per_position_pct: float = Field(2.5, gt=0, le=100)     # a position's cost-inclusive max loss, % of capital
    # Net credit after the hedge, charges and slippage / cost-inclusive max loss, at least this (CREDIT_TOO_SMALL). 0 = off.
    # 0.10, set by the user on 2026-11-14 from the distribution measured on stored data BEFORE any outcome was looked at.
    min_credit_to_max_loss: float = Field(0.10, ge=0, le=1)
    entry_dte_min: int = Field(20, ge=8, le=90)               # calendar days to expiry
    entry_dte_max: int = Field(35, ge=8, le=120)
    profit_target_pct_of_credit: float = Field(50.0, gt=0, lt=100)
    stop_spread_value_multiple: float = Field(2.0, gt=1, le=10)   # exit when the buy-back value reaches this x the entry credit
    time_exit_dte: int = Field(10, ge=7, le=30)               # out this many calendar days before expiry
    forced_exit_trading_days_before_expiry: int = Field(5, ge=1, le=15)   # no position inside this window, whatever else
    # EVENT_COVERAGE_UNKNOWN: a live or research candidate needs a COVERAGE row in events.csv for the stock, retrieved
    # at most this many days ago and covering the option's expiry.
    event_coverage_max_age_days: int = Field(7, ge=1, le=60)
    sma_short: int = Field(20, ge=2, le=200)                  # the swing trend rule: close and sma_short against sma_mid
    sma_mid: int = Field(50, ge=3, le=400)                    # also the breadth average
    sma_long: int = Field(200, ge=20, le=400)                 # falling-stock block, market block, washed-out list
    atr_days: int = Field(14, ge=2, le=100)
    # swing_pivot_bars: a pivot low (high) is a session whose low (high) is below (above) the 5 sessions before it and at
    # or below (above) the 5 after; it is usable only once those 5 later sessions have completed, never on its own day.
    swing_pivot_bars: int = Field(5, ge=1, le=20)
    # Support = the nearest confirmed pivot low below the close, resistance = the nearest confirmed pivot high above it,
    # among the pivots of the last `pivot_lookback_sessions` sessions (a new definition, recorded 2026-11-15).
    pivot_lookback_sessions: int = Field(252, ge=20, le=500)
    shortlist_size: int = Field(10, ge=1, le=100)
    score: ScoreConfig = ScoreConfig()
    trend_block: TrendBlock = TrendBlock()
    campaign_limit: CampaignLimit = CampaignLimit()
    market_block: MarketBlock = MarketBlock()
    portfolio: PortfolioLimits = PortfolioLimits()
    roll: RollRules = RollRules()
    reversal: Reversal = Reversal()
    profile: StockProfile = StockProfile()
    backtest: StockSellingBacktest = StockSellingBacktest()
    settlement: Settlement = Settlement()

    @model_validator(mode="after")
    def consistent(self) -> "StockOptionSelling":
        if self.settlement.urgent_from_trading_days < self.forced_exit_trading_days_before_expiry:
            raise ValueError("stock_option_selling.settlement.urgent_from_trading_days must be at least "
                             "forced_exit_trading_days_before_expiry (URGENT no later than the forced exit)")
        if self.entry_dte_min > self.entry_dte_max:
            raise ValueError("stock_option_selling.entry_dte_min cannot exceed entry_dte_max")
        if self.time_exit_dte >= self.entry_dte_min:
            raise ValueError("stock_option_selling.time_exit_dte must be below entry_dte_min")
        h = self.hedge_strikes_out
        if len(h) != 2 or not (1 <= h[0] <= h[1] <= 10):
            raise ValueError("stock_option_selling.hedge_strikes_out must be [min, max] with 1 <= min <= max <= 10")
        if not (self.sma_short < self.sma_mid < self.sma_long):
            raise ValueError("stock_option_selling needs sma_short < sma_mid < sma_long")
        return self

    # the names the shared spread code (CreditSpread.finish, the option backtest engine) reads
    @property
    def entry_min_dte(self) -> int:
        return self.entry_dte_min

    @property
    def entry_max_dte(self) -> int:
        return self.entry_dte_max

    @property
    def min_credit_pct_of_width(self) -> float:
        return 0.0

    @property
    def stop_multiple_of_credit(self) -> float:
        return self.stop_spread_value_multiple

    @property
    def exit_days_before_expiry(self) -> int:
        return self.time_exit_dte


# ---------------- phase 6: live signals, alerts, exit calls ----------------
class ScoringWeights(StrictModel):
    """Weights of the six confluence factors of the 0-100 signal score (a ranking aid, never a probability)."""
    timeframes: float = Field(1.0, ge=0, le=10)
    volume: float = Field(1.0, ge=0, le=10)
    relative_strength: float = Field(1.0, ge=0, le=10)
    regime_fit: float = Field(1.0, ge=0, le=10)
    fii_bias: float = Field(1.0, ge=0, le=10)
    room_to_level: float = Field(1.0, ge=0, le=10)


class Scoring(StrictModel):
    weights: ScoringWeights = ScoringWeights()
    sma_5m: int = Field(12, ge=2, le=75)             # 5-minute closes in the 5-minute trend average (an hour)
    sma_15m: int = Field(8, ge=2, le=50)             # 15-minute closes in the 15-minute trend average
    sma_daily: int = Field(20, ge=2, le=200)         # daily closes in the daily trend average
    sr_lookback_days: int = Field(20, ge=5, le=252)  # sessions of daily highs/lows for the nearest level
    min_known_factors: int = Field(3, ge=1, le=6)    # fewer known factors: no score (ranked last)


class SettlementRepeat(StrictModel):
    """Minutes between repeats of a settlement-readiness alert, by level (an overdue exit repeats every bar)."""
    NOTICE: int = Field(240, ge=5, le=1440)
    WARNING: int = Field(60, ge=5, le=1440)
    URGENT: int = Field(15, ge=1, le=1440)

    @model_validator(mode="after")
    def ordered(self) -> "SettlementRepeat":
        if not self.URGENT <= self.WARNING <= self.NOTICE:
            raise ValueError("alerts.settlement_repeat_minutes: URGENT <= WARNING <= NOTICE")
        return self


class Alerts(StrictModel):
    """Telegram and dashboard alerts. OFF until the user switches them on (telegram_enabled)."""
    telegram_enabled: bool = False                    # master switch: nothing goes to Telegram while false
    top_per_bar: int = Field(3, ge=1, le=20)          # signal alerts per bar: the highest scores only
    daily_signal_cap: int = Field(10, ge=0, le=100)   # signal alerts per day (exit calls are not capped)
    exit_call_repeat_minutes: int = Field(15, ge=1, le=240)
    settlement_repeat_minutes: SettlementRepeat = SettlementRepeat()
    retry_first_seconds: int = Field(30, ge=5, le=3600)    # a failed send is retried after this, doubling each time
    retry_max_seconds: int = Field(600, ge=5, le=86400)
    max_alert_age_minutes: int = Field(60, ge=5, le=1440)  # an alert without its own expiry is given up after this
    send_timeout_seconds: float = Field(10, gt=0, le=60)
    premarket_time: CLOCK = "09:00"
    end_of_day_time: CLOCK = "15:45"
    heartbeat_bar_gap_seconds: int = Field(120, ge=60, le=1800)   # no bar processed this long in market hours: alert
    sleep_gap_seconds: int = Field(300, ge=60, le=86400)          # wall clock this far past the last loop: asleep

    @model_validator(mode="after")
    def ordered(self) -> "Alerts":
        if self.retry_max_seconds < self.retry_first_seconds:
            raise ValueError("alerts.retry_max_seconds must be at least retry_first_seconds")
        return self


class Exits(StrictModel):
    """Exit-call rules for open positions (R = the initial risk per share: entry to the first stop)."""
    breakeven_after_r: float = Field(1.0, gt=0, le=10)    # move the stop to the entry after this gain in R
    trail_after_r: float = Field(1.5, gt=0, le=20)        # trail the stop from this gain in R
    trail_distance_r: float = Field(1.0, gt=0, le=10)     # the trailing stop stays this many R behind the best price
    partial_at_r: float = Field(1.0, gt=0, le=10)         # book part of the position at this gain in R (once)
    partial_pct: float = Field(50, gt=0, lt=100)          # % of the units to book (positions of 2 or more units)
    invalidation_buffer_pct: float = Field(0.3, ge=0, le=5)   # the underlying must close this far beyond the level

    @model_validator(mode="after")
    def ordered(self) -> "Exits":
        if self.trail_after_r < self.breakeven_after_r:
            raise ValueError("exits.trail_after_r must be at least breakeven_after_r")
        return self


class Execution(StrictModel):
    """Execution records and the slippage report."""
    strike_distance_bands_pct: list[float] = [1.0, 2.0, 5.0, 10.0]   # strike distance from spot, % of spot

    @field_validator("strike_distance_bands_pct")
    @classmethod
    def increasing(cls, v: list[float]) -> list[float]:
        if not v or any(b <= 0 for b in v) or v != sorted(set(v)):
            raise ValueError("execution.strike_distance_bands_pct must be positive and increasing")
        return v


class PaperBook(StrictModel):
    """The paper books: each strategy has its own paper book per pool, with that pool's capital and the real book's
    per-trade sizing, never counted against my real capital. A strategy's paper trades never stop another's; the
    only refusal is this per-strategy safety cap."""
    max_open_trades: int = Field(10, ge=1, le=500)            # open + pending paper trades per strategy and pool


class EngineCfg(StrictModel):
    """The live signal loop (paper mode). Bars are 5 minutes; nothing is ever sent to the broker."""
    enabled: bool = True
    poll_seconds: float = Field(2.0, ge=0.5, le=60)
    feed_reconnect_after_seconds: int = Field(90, ge=30, le=3600)   # index STALE this long in market hours: reconnect


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
    options: Options = Options()             # phase 4
    stock_option_selling: StockOptionSelling = StockOptionSelling()   # phase 9
    external_data: ExternalData = ExternalData()
    data_status: DataStatusLimits = DataStatusLimits()
    data_quality: DataQuality = DataQuality()
    scoring: Scoring = Scoring()             # phase 6
    alerts: Alerts = Alerts()
    exits: Exits = Exits()
    execution: Execution = Execution()
    engine: EngineCfg = EngineCfg()
    paper: PaperBook = PaperBook()

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
        o = self.options
        bad = sorted(set(o.underlyings) - symbols)
        if bad:
            raise ValueError(f"options.underlyings has symbols not in the watchlist: {bad}")
        if o.iv_reference_symbol not in symbols:
            raise ValueError(f"options.iv_reference_symbol '{o.iv_reference_symbol}' is not in the watchlist")
        known = set(Strategies.model_fields) - {"inactive", "status"}
        bad = sorted(set(self.strategies.long_option.trigger_strategies + [o.backtest.trigger]) - known)
        if bad:
            raise ValueError(f"unknown trigger strategy ids {bad}")
        return self

    def option_underlying(self, symbol: str) -> "OptionUnderlying | None":
        """Option settings for a watchlist symbol: its entry in options.underlyings, or options.stocks
        for a stock. None for an index without an entry (for example INDIAVIX)."""
        if symbol in self.options.underlyings:
            return self.options.underlyings[symbol]
        inst = next((i for i in self.watchlist if i.symbol == symbol), None)
        return self.options.stocks if inst is not None and inst.kind == "stock" else None

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


def _merge(base: dict, over: dict, where: str = "") -> dict:
    """`over` merged into `base`: nested sections key by key; any other value (lists too) replaced whole. A key
    that config.yaml does not have is refused (a typo in the local file would otherwise be silently ignored)."""
    out = dict(base)
    for k, v in over.items():
        if k not in out:
            raise ValueError(f"config.local.yaml: {where}{k} is not a key of config.yaml")
        out[k] = _merge(out[k], v, f"{where}{k}.") if isinstance(v, dict) and isinstance(out[k], dict) else v
    return out


def load_settings(path: str | Path | None = None, local_path: str | Path | None = None) -> Settings:
    """config.yaml (or `path`), validated. With the default file, `config.local.yaml` (or `local_path`) is merged
    over it when it exists - your machine's own switches, e.g. alerts.telegram_enabled - unless the environment
    variable TRADING_NO_LOCAL_CONFIG is "1" (the tests set it, so they read only the committed settings). An
    unknown key or a bad value in the local file fails validation like one in config.yaml."""
    import os
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    local = Path(local_path) if local_path else LOCAL_CONFIG
    if path is None and os.environ.get(NO_LOCAL_ENV) != "1" and local.exists():
        with open(local, "r", encoding="utf-8") as f:
            over = yaml.safe_load(f) or {}
        if not isinstance(over, dict):
            raise ValueError(f"{local.name} must hold config.yaml keys (a mapping), not {type(over).__name__}")
        raw = _merge(raw, over)
    return Settings.model_validate(raw)
