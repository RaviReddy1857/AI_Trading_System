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


# BOARD_MEETING and CORPORATE_ACTION (with a symbol) keep a stock off the phase 9 shortlist until they pass.
EVENT_TYPES = ("EXPIRY", "RBI_POLICY", "BUDGET", "RESULTS", "MARKET_EVENT", "BOARD_MEETING", "CORPORATE_ACTION")


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
class ShortlistWeights(StrictModel):
    """Ranking weights of the daily shortlist (each score is 0..1; the weights need not sum to 1)."""
    iv_rank: float = Field(0.35, ge=0, le=1)
    trend_clarity: float = Field(0.25, ge=0, le=1)
    distance: float = Field(0.20, ge=0, le=1)          # room between the price and its support / resistance
    liquidity: float = Field(0.20, ge=0, le=1)


class StockSellingBacktest(StrictModel):
    """Backtest and option-history download of stock option selling."""
    priority_stocks: int = Field(30, ge=1, le=250)      # the N most liquid F&O stocks are downloaded and tested
    liquidity_lookback_days: int = Field(60, ge=5, le=500)   # liquidity = median daily traded value over these sessions
    # How target and stop are checked while a spread is open: on the legs' 5-minute bars ("intraday") or on
    # the daily closes with a fill at the next session's open ("daily"). The first one is the main result;
    # every report shows the strategy and its baseline under each.
    exit_checks: list[Literal["intraday", "daily"]] = ["intraday", "daily"]
    # 5-minute check: a leg's last traded price may be this old at most (stock options do not trade every bar).
    intraday_leg_max_age_minutes: int = Field(30, ge=5, le=375)
    exit_on_strike_breach: bool = True                  # close (next open) when the stock closes beyond the sold strike
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
    """Falling-stock block (DOWNTREND_BLOCK): no put selling in a stock that is below its long
    average, made a 52-week low recently, or trails NIFTY badly. Mirrored for call selling in a
    strong uptrend."""
    enabled: bool = True
    long_sma_days: int = Field(200, ge=20, le=400)
    extreme_lookback_sessions: int = Field(252, ge=60, le=400)     # "52-week" low / high
    extreme_recent_sessions: int = Field(20, ge=1, le=120)         # ... made within this many sessions
    relative_return_sessions: int = Field(63, ge=10, le=260)       # about 3 months
    max_underperformance_pct: float = Field(10.0, gt=0, le=100)    # stock return minus NIFTY return, in points


class CampaignLimit(StrictModel):
    """Per-stock campaign limit (STOCK_LOSS_LIMIT): after repeated stops or a large loss in one stock,
    no new signal in that stock for a cooling-off period. Closed manual positions count too."""
    enabled: bool = True
    window_days: int = Field(90, ge=7, le=730)
    max_stopped_trades: int = Field(2, ge=1, le=20)
    max_cumulative_loss_pct: float = Field(5.0, gt=0, le=100)      # net loss in the window, % of capital
    cooling_off_days: int = Field(45, ge=1, le=365)


class RollRules(StrictModel):
    """When a threatened spread may be rolled instead of closed. Anything else is "exit"."""
    max_rolls: int = Field(1, ge=0, le=12)                         # rolls allowed in one campaign
    min_net_credit_pct_of_width: float = Field(10.0, ge=0, le=100)  # new credit after hedge and charges, % of the new width
    stuck_time_value_pct: float = Field(10.0, ge=0, le=100)        # sold option in the money with time value below this % of its price
    pre_expiry_window_days: int = Field(10, ge=0, le=60)           # this close to expiry a roll earns little: exit
    # A roll may go into the next monthly expiry even when that is further away than a new entry may be.
    max_days_to_expiry: int = Field(65, ge=8, le=120)


class BullCallParams(StrictModel):
    """The bull call spread candidate of a confirmed reversal: buy the call nearest the money, sell the
    call `width_strikes` strikes higher, same expiry. One lot; the loss is limited to the debit."""
    width_strikes: int = Field(2, ge=1, le=10)
    entry_min_dte: int = Field(20, ge=8, le=90)
    entry_max_dte: int = Field(65, ge=8, le=120)
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
    drawdown_pct: float = Field(30.0, gt=0, lt=100)                # washed out: this far below the 52-week high ...
    min_months: int = Field(3, ge=1, le=24)                        # ... on every session for this many months (21 sessions each) ...
    high_lookback_sessions: int = Field(252, ge=60, le=400)        # ... and below the trend block's long average
    base_no_new_low_sessions: int = Field(15, ge=3, le=120)        # base: the low is at least this many sessions old ...
    range_sessions: int = Field(10, ge=3, le=60)                   # ... and the average daily range of the last N sessions
    range_compare_sessions: int = Field(30, ge=3, le=120)          # is at most `range_shrink_ratio` of the average of
    range_shrink_ratio: float = Field(0.85, gt=0, le=1)            # the `range_compare_sessions` sessions before them
    base_valid_sessions: int = Field(10, ge=1, le=60)              # a confirmation must come this soon after a base-forming day
    confirm_sma_days: int = Field(20, ge=5, le=100)
    volume_sma_days: int = Field(20, ge=5, le=100)
    relative_return_sessions: int = Field(10, ge=2, le=60)         # return ahead of NIFTY over these sessions
    swing_pct: float = Field(5.0, gt=0, le=50)                     # zigzag size for "the last swing high" and breakdown levels
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
    swing_pct: float = Field(8.0, gt=0, le=50)                     # a swing is a move of at least this % (zigzag)
    state_lookback_sessions: int = Field(20, ge=5, le=120)
    trending_efficiency: float = Field(0.30, gt=0, lt=1)           # net move / path length at or above this = trending
    holding_sessions: int = Field(20, ge=1, le=120)                # a spread is held about this long
    scenario_horizon_days: int = Field(30, ge=1, le=180)           # calendar days
    scenario_min_cases: int = Field(10, ge=1, le=1000)             # fewer cases: LOW SAMPLE
    vix_spike_pct: float = Field(15.0, gt=0, le=200)               # India VIX close up this % in one session

    @model_validator(mode="after")
    def windows(self) -> "StockProfile":
        if not self.windows_years or any(not (1 <= y <= 20) for y in self.windows_years):
            raise ValueError("stock_option_selling.profile.windows_years must list years between 1 and 20")
        return self


class StockOptionSelling(StrictModel):
    """Phase 9: monthly hedged credit spreads on F&O stocks (bull put in an up trend, bear call in a down
    trend). Every sold option is paired with a bought option of the same expiry. Stock options settle
    physically, so no position is open in expiry week."""
    enabled: bool = True
    min_iv_rank: float = Field(50.0, ge=0, le=100)            # sell only when the stock's IV rank is at least this
    min_open_interest: int = Field(100, ge=0)                 # each traded strike: open interest in LOTS at least this
    max_bid_ask_spread_pct: float = Field(10.0, gt=0, le=100)  # each traded strike: bid-ask width, % of mid, at most this
    max_short_delta: float = Field(0.20, gt=0, lt=0.5)
    hedge_strikes_out: list[int] = [2, 3]                     # bought strike: this many strikes further out (min, max)
    max_loss_per_stock_spread_pct: float = Field(10.0, gt=0, le=100)   # one lot's max loss, % of capital, at most this
    # Net credit after the hedge and all charges must be at least this % of the max loss (CREDIT_TOO_SMALL). 0 = off.
    min_net_credit_pct_of_max_loss: float = Field(10.0, ge=0, le=100)
    entry_min_days_to_expiry: int = Field(20, ge=8, le=90)
    entry_max_days_to_expiry: int = Field(35, ge=8, le=120)
    profit_target_pct_of_credit: float = Field(50.0, gt=0, lt=100)
    stop_multiple_of_credit: float = Field(2.0, gt=1, le=10)
    exit_days_before_expiry: int = Field(7, ge=7, le=30)      # out this many days before expiry: never in expiry week
    trend_fast_sma: int = Field(20, ge=2, le=200)             # the swing trend rule: close and fast average vs slow average
    trend_slow_sma: int = Field(50, ge=3, le=400)
    atr_days: int = Field(14, ge=2, le=100)
    support_resistance_lookback_days: int = Field(20, ge=5, le=250)   # support = lowest low, resistance = highest high
    shortlist_size: int = Field(10, ge=1, le=100)
    weights: ShortlistWeights = ShortlistWeights()
    trend_block: TrendBlock = TrendBlock()
    campaign_limit: CampaignLimit = CampaignLimit()
    roll: RollRules = RollRules()
    reversal: Reversal = Reversal()
    profile: StockProfile = StockProfile()
    backtest: StockSellingBacktest = StockSellingBacktest()

    @model_validator(mode="after")
    def consistent(self) -> "StockOptionSelling":
        if self.entry_min_days_to_expiry > self.entry_max_days_to_expiry:
            raise ValueError("stock_option_selling.entry_min_days_to_expiry cannot exceed entry_max_days_to_expiry")
        if self.exit_days_before_expiry >= self.entry_min_days_to_expiry:
            raise ValueError("stock_option_selling.exit_days_before_expiry must be below entry_min_days_to_expiry")
        h = self.hedge_strikes_out
        if len(h) != 2 or not (1 <= h[0] <= h[1] <= 10):
            raise ValueError("stock_option_selling.hedge_strikes_out must be [min, max] with 1 <= min <= max <= 10")
        if self.trend_fast_sma >= self.trend_slow_sma:
            raise ValueError("stock_option_selling.trend_fast_sma must be below trend_slow_sma")
        return self

    # the names the option backtest engine reads on every spread strategy
    @property
    def entry_min_dte(self) -> int:
        return self.entry_min_days_to_expiry

    @property
    def entry_max_dte(self) -> int:
        return self.entry_max_days_to_expiry

    @property
    def min_credit_pct_of_width(self) -> float:
        return 0.0


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


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        return Settings.model_validate(yaml.safe_load(f))
