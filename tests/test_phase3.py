"""Phase 3: Signal contract, cost model, strategies, backtest engine, point-in-time regime,
walk-forward split and reports. Acceptance checks are marked ACCEPTANCE."""
import json
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from app.backtest import (Engine, Trade, build_report, position_size, run_backtest, split_sessions, stats,
                          windows)
from app.calendar import MarketCalendar
from app.config import Costs, Settings
from app.costs import CostModel, MissingCostRates
from app.history import DAILY, INTRADAY, HistoryLoader, SimHistorySource, daily_start, seed_sim_history
from app.models import Bar, InvalidSignal, Signal
from app.quality import IST
from app.regime import REGIMES, PointInTimeRegime, RegimeResult
from app.store import Store
from app.strategies import CLASSES, MarketContext, Strategy, build_strategies
from app.strategies.base import LookAheadError
from tests.fixture_settings import load_settings

DAY = date(2026, 11, 10)                         # a Tuesday
UTC = timezone.utc


def ist(h, m, d=DAY):
    return datetime.combine(d, time(h, m), IST).astimezone(UTC)


def five(sym, ohlcv, d=DAY, start=(9, 15)):
    """5-minute bars from 09:15 IST: list of (open, high, low, close[, volume])."""
    t0 = ist(*start, d=d)
    out = []
    for i, row in enumerate(ohlcv):
        o, h, l, c, *v = row
        out.append(Bar(sym, t0 + timedelta(minutes=5 * i), o, h, l, c, v[0] if v else 1000.0))
    return out


# Explicit rates for the reconciliation test, so editing config.yaml cannot change the expected paise.
TEST_COSTS = {
    "label": "standard rates, not yet verified against a contract note", "plan": "test", "compiled_on": "2026-11-12",
    "gst_pct": {"value": 18},
    "gst_on": ["brokerage", "exchange_txn", "sebi_fee", "ipft", "dp_charge"],
    "intraday_equity": {"brokerage_pct": {"value": 0.022}, "stt_buy_pct": {"value": 0}, "stt_sell_pct": {"value": 0.025},
                        "exchange_txn_pct": {"value": 0.00307}, "sebi_fee_pct": {"value": 0.0001},
                        "stamp_duty_buy_pct": {"value": 0.003}, "ipft_pct": {"value": 0.0000001}},
    "delivery_equity": {"brokerage_pct": {"value": 0.22}, "stt_buy_pct": {"value": 0.1}, "stt_sell_pct": {"value": 0.1},
                        "exchange_txn_pct": {"value": 0.00307}, "sebi_fee_pct": {"value": 0.0001},
                        "stamp_duty_buy_pct": {"value": 0.015}, "ipft_pct": {"value": 0.0000001},
                        "dp_charge_per_sell_inr": {"value": 20}},
    "options": {"brokerage_per_lot_inr": {"value": 49}, "stt_sell_premium_pct": {"value": 0.15},
                "exchange_txn_premium_pct": {"value": 0.03553}, "sebi_fee_pct": {"value": 0.0001},
                "stamp_duty_buy_pct": {"value": 0.003}, "ipft_pct": {"value": 0.0000001}},
    "slippage_pct": {"intraday_equity": 0.05, "delivery_equity": 0.05, "options": 0.5},
}


def settings(**over) -> Settings:
    raw = load_settings().model_dump()
    raw["costs"] = json.loads(json.dumps(TEST_COSTS))
    raw["feed"]["mode"] = "sim"
    for k, v in over.items():
        raw[k] = {**raw.get(k, {}), **v} if isinstance(v, dict) else v
    return Settings.model_validate(raw)


class FixedRegime:
    """Stand-in for PointInTimeRegime in engine tests."""
    def __init__(self, regime="TRENDING_UP"):
        self.regime = regime
        self.calls = []

    def at_bar(self, t):
        self.calls.append(t)
        return RegimeResult(self.regime, [], "test")

    def at_close(self, d):
        self.calls.append(d)
        return RegimeResult(self.regime, [], "test")


class Stub(Strategy):
    """Signals once, on the bar that closes at `fire_at` (IST), with given stop/target."""
    id = "stub"
    horizon = "INTRADAY"
    interval = "5minute"
    allowed_regimes = frozenset(REGIMES)

    def __init__(self, fire_at, stop, target, direction="LONG"):
        from app.config import OrbParams
        super().__init__(OrbParams())
        self.fire_at, self.stop, self.target, self.direction = fire_at, stop, target, direction
        self.seen = []

    def _evaluate(self, ctx):
        self.seen.append((ctx.asof, ctx.intraday[-1].start))
        if ctx.asof != self.fire_at:
            return []
        last = ctx.intraday[-1]
        return [self.signal(ctx, self.direction, last.close, self.stop, self.target,
                            ctx.asof + timedelta(minutes=5), ["test"])]


def engine_with(bars, s=None, regime="TRENDING_UP"):
    s = s or settings()
    store = Store(":memory:")
    store.save_history_bars("bars_5m", bars, "sim")
    store.save_history_bars("bars_5m", five("NIFTY", [(b.open, b.high, b.low, b.close) for b in bars]), "sim")
    return Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime(regime)), s


# ---------------- Signal contract ----------------
def sig(**kw):
    base = dict(signal_id="x", created_at=ist(10, 0), strategy_id="s", strategy_version="1", symbol="TCS",
                asset_class="CASH_EQUITY", direction="LONG", horizon="INTRADAY", entry=100.0, stop=98.0,
                target=104.0, valid_until=ist(10, 5), regime="TRENDING_UP", why_now=("r",), data_as_of=ist(10, 0))
    return Signal(**{**base, **kw})


def test_signal_schema_accepts_valid_and_serialises():
    d = sig().to_dict()
    assert d["direction"] == "LONG" and d["entry"] == 100.0 and d["valid_until"].endswith("+00:00")
    assert set(d) >= {"signal_id", "created_at", "strategy_id", "strategy_version", "symbol", "asset_class",
                      "direction", "horizon", "entry", "stop", "target", "valid_until", "regime", "why_now",
                      "data_as_of", "entry_zone", "legs"}


@pytest.mark.parametrize("kw, msg", [
    ({"stop": 101.0}, "stop < entry < target"),
    ({"direction": "SHORT"}, "target < entry < stop"),
    ({"entry": float("nan")}, "finite"),
    ({"direction": "SIDEWAYS"}, "direction"),
    ({"valid_until": ist(9, 55)}, "valid_until"),
    ({"why_now": ()}, "why_now"),
    ({"data_as_of": ist(10, 5)}, "look-ahead"),
    ({"created_at": datetime(2026, 11, 10, 10, 0)}, "timezone-aware"),
])
def test_signal_schema_rejects_bad_values(kw, msg):
    with pytest.raises(InvalidSignal, match=msg):
        sig(**kw)


# ---------------- costs ----------------
def test_ACCEPTANCE_fixture_trade_reconciles_to_the_paise():
    """Hand calculation (Prime 999 intraday rates, 0.05% slippage):
    signal bar closes 1000.00; next bar opens 1000.00 -> buy fill 1000.50; target 1020.00 -> sell fill 1019.49.
    qty = min(floor(2000 / (1000.50 - 990)) = 190, floor(50000 / 1000.50) = 49) = 49.
    buy value 49,024.50; sell value 49,955.01; gross 930.51.
    brokerage 10.79 + 10.99 = 21.78; STT 12.49; NSE 1.51 + 1.53 = 3.04; SEBI 0.05 + 0.05 = 0.10; IPFT 0.00;
    stamp 1.47; GST 18% x (21.78 + 3.04 + 0.10 + 0.00) = 4.49; total 43.37; net 887.14."""
    bars = five("TCS", [(999, 1001, 998, 1000), (1000, 1005, 999, 1004), (1004, 1021, 1003, 1018), (1018, 1019, 1010, 1012)])
    eng, s = engine_with(bars)
    trades, _ = eng.run(Stub(ist(9, 20), stop=990.0, target=1020.0), [DAY], [("TCS", "stock")])
    assert len(trades) == 1
    t = trades[0]
    assert (t.entry_price, t.qty, t.exit_price, t.exit_reason) == (1000.50, 49, 1019.49, "TARGET")
    assert t.gross_pnl == 930.51
    assert t.costs == {"brokerage": 21.78, "stt": 12.49, "exchange_txn": 3.04, "sebi_fee": 0.10, "ipft": 0.0,
                       "stamp_duty": 1.47, "dp_charge": 0.0, "gst": 4.49, "total": 43.37}
    assert t.net_pnl == 887.14


def test_delivery_costs_include_dp_charge_and_both_side_stt():
    c = CostModel(Costs.model_validate(TEST_COSTS)).equity("delivery_equity", 10000, 11000)
    assert c.stt == Decimal("21.00") and c.dp_charge == Decimal("20.00") and c.stamp_duty == Decimal("1.50")
    assert c.brokerage == Decimal("46.20")            # 22.00 + 24.20
    assert c.exchange_txn == Decimal("0.65")          # 0.307 -> 0.31 and 0.3377 -> 0.34
    assert c.gst == Decimal("12.04")                  # 18% x (46.20 + 0.65 + 0.02 + 0.00 + 20.00) = 12.0366


def test_option_costs():
    c = CostModel(Costs.model_validate(TEST_COSTS)).options(2, 10000, 12000)
    assert c.brokerage == Decimal("196.00") and c.stt == Decimal("18.00") and c.stamp_duty == Decimal("0.30")


def test_ACCEPTANCE_empty_rates_fail_loudly_and_are_named():
    bad = json.loads(json.dumps(TEST_COSTS))
    bad["intraday_equity"]["stt_sell_pct"]["value"] = None
    bad["gst_pct"]["value"] = None
    s = settings(costs=bad)
    eng, _ = engine_with(five("TCS", [(1, 1, 1, 1)]), s)
    with pytest.raises(MissingCostRates, match=r"costs\.gst_pct.*costs\.intraday_equity\.stt_sell_pct"):
        eng.run(Stub(ist(9, 20), 1, 2), [DAY], [("TCS", "stock")])
    with pytest.raises(MissingCostRates, match="no 'costs' section"):
        CostModel(None)


def test_real_config_costs_are_labelled_and_options_ipft_left_empty():
    from app.config import load_settings as real
    c = real().costs
    assert c.label == "standard rates, not yet verified against a contract note"
    assert c.verified_against_contract_note is False
    m = CostModel(c)
    assert m.missing("intraday_equity") == [] and m.missing("delivery_equity") == []
    assert m.missing("options") == ["costs.options.ipft_pct"]
    for seg in ("intraday_equity", "delivery_equity", "options"):
        for name, rate in getattr(c, seg):
            if rate is not None and rate.value is not None:
                assert rate.source and rate.effective, f"{seg}.{name} has no source/effective date"


def test_position_size_respects_risk_and_cap():
    assert position_size(200000, 1.0, 25, 100.0, 99.0) == 500          # cap: 50,000 / 100
    assert position_size(200000, 1.0, 100, 100.0, 99.0) == 2000        # risk: 2,000 / 1
    assert position_size(200000, 1.0, 25, 100.0, 100.0) == 0


# ---------------- backtest engine ----------------
def test_ACCEPTANCE_signal_bar_close_is_never_the_fill():
    """The signal bar closes at 1000; the next bar opens at 1010. The fill must be 1010 + slippage."""
    bars = five("TCS", [(995, 1001, 994, 1000), (1010, 1012, 1008, 1011), (1011, 1013, 1009, 1012)])
    eng, _ = engine_with(bars)
    strat = Stub(ist(9, 20), stop=950.0, target=1100.0)
    trades, _ = eng.run(strat, [DAY], [("TCS", "stock")])
    t = trades[0]
    assert t.signal_close == 1000.0
    assert t.entry_price == 1010.51 and t.entry_price != t.signal_close     # 1010 x 1.0005
    assert datetime.fromisoformat(t.entry_time) == bars[1].start
    assert datetime.fromisoformat(t.entry_time) > datetime.fromisoformat(t.signal_time) - timedelta(seconds=1)
    # the strategy never saw a bar that had not closed yet
    assert all(start + timedelta(minutes=5) <= asof for asof, start in strat.seen)


def test_ACCEPTANCE_no_next_bar_means_no_trade():
    """A signal on the last available bar cannot be filled on that bar."""
    bars = five("TCS", [(995, 1001, 994, 1000), (1000, 1001, 999, 1000)])
    eng, _ = engine_with(bars)
    trades, skipped = eng.run(Stub(ist(9, 25), stop=950.0, target=1100.0), [DAY], [("TCS", "stock")])
    assert trades == [] and skipped["NO_NEXT_BAR"] == 1


def test_gap_in_bars_means_no_fill():
    bars = five("TCS", [(995, 1001, 994, 1000)]) + five("TCS", [(1000, 1001, 999, 1000)], start=(9, 30))
    eng, _ = engine_with(bars)
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], [("TCS", "stock")])
    assert trades == [] and skipped["NO_NEXT_BAR"] == 1


def test_strategy_context_cannot_contain_a_future_bar():
    s = build_strategies(settings(), ["orb"])[0]
    bars = tuple(five("TCS", [(1, 1, 1, 1)] * 5))
    with pytest.raises(LookAheadError):
        s.evaluate(MarketContext("TCS", "stock", bars[-1].start, "TRENDING_UP", intraday=bars))


def test_stop_assumed_first_when_bar_hits_both():
    bars = five("TCS", [(999, 1001, 998, 1000), (1000, 1001, 999, 1000), (1000, 1030, 980, 1000)])
    eng, _ = engine_with(bars)
    t = eng.run(Stub(ist(9, 20), stop=990.0, target=1020.0), [DAY], [("TCS", "stock")])[0][0]
    assert t.exit_reason == "STOP" and t.exit_price == 989.51              # 990 x 0.9995 = 989.505, half up


def test_gap_through_stop_fills_at_the_open():
    bars = five("TCS", [(999, 1001, 998, 1000), (1000, 1001, 999, 1000), (970, 975, 960, 965)])
    eng, _ = engine_with(bars)
    t = eng.run(Stub(ist(9, 20), stop=990.0, target=1020.0), [DAY], [("TCS", "stock")])[0][0]
    assert t.exit_reason == "STOP" and t.exit_price == 969.52              # 970 x 0.9995 (worse than the stop)


def test_short_trade_profit_and_costs():
    bars = five("TCS", [(1001, 1002, 999, 1000), (1000, 1001, 999, 1000), (1000, 1000, 975, 978)])
    eng, _ = engine_with(bars)
    t = eng.run(Stub(ist(9, 20), stop=1010.0, target=980.0, direction="SHORT"), [DAY], [("TCS", "stock")])[0][0]
    assert t.direction == "SHORT" and t.exit_reason == "TARGET"
    assert t.entry_price == 999.50 and t.exit_price == 980.49             # sell lower, buy back higher
    assert t.gross_pnl == round(t.qty * (999.50 - 980.49), 2) and t.net_pnl < t.gross_pnl


def test_intraday_trades_close_by_1515():
    flat = [(1000, 1001, 999, 1000)] * 75                                   # 09:15 .. 15:25
    eng, _ = engine_with(five("TCS", flat))
    t = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], [("TCS", "stock")])[0][0]
    assert t.exit_reason == "EOD" and datetime.fromisoformat(t.exit_time) == ist(15, 15)


def test_no_entry_after_last_entry_time():
    flat = [(1000, 1001, 999, 1000)] * 75
    eng, _ = engine_with(five("TCS", flat))
    trades, skipped = eng.run(Stub(ist(14, 50), stop=950.0, target=1100.0), [DAY], [("TCS", "stock")])
    assert trades == [] and skipped["AFTER_LAST_ENTRY_TIME"] == 1


def test_swing_enters_next_day_open_and_exits_on_time():
    s = settings()
    store = Store(":memory:")
    cal = MarketCalendar()
    days = []
    d = date(2026, 10, 1)
    while len(days) < 20:
        if cal.is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    bars = [Bar("TCS", daily_start(x), 100 + i, 101 + i, 99 + i, 100.5 + i, 1e5) for i, x in enumerate(days)]
    store.save_history_bars("bars_1d", bars, "sim")
    store.save_history_bars("bars_1d", [replace(b, symbol="NIFTY") for b in bars], "sim")

    class SwingStub(Strategy):
        id, horizon, interval = "swingstub", "SWING", "1day"
        allowed_regimes = frozenset(REGIMES)

        def _evaluate(self, ctx):
            if ctx.daily[-1].start != bars[5].start:
                return []
            from app.strategies.base import next_weekday_open_window
            c = ctx.daily[-1].close
            return [self.signal(ctx, "LONG", c, c - 50, c + 500, next_weekday_open_window(days[5]), ["t"])]

    from app.config import SwingBreakoutParams
    eng = Engine(s, store, "sim", cal, regime=FixedRegime())
    t = eng.run_swing(SwingStub(SwingBreakoutParams()), "TCS", "stock", days, Counter(), max_hold=3)[0]
    assert datetime.fromisoformat(t.entry_time) == bars[6].start and t.entry_price == round(106 * 1.0005, 2)
    assert t.exit_reason == "TIME" and datetime.fromisoformat(t.exit_time).astimezone(IST).date() == days[8]
    assert t.segment == "delivery_equity" and t.costs["dp_charge"] == 20.0


# ---------------- strategies ----------------
@pytest.fixture(scope="module")
def sim_store():
    raw = load_settings().model_dump()
    raw["history"]["intraday_days"] = 60
    raw["history"]["daily_days"] = 600
    s = Settings.model_validate(raw)
    store = Store(":memory:")
    seed_sim_history(s, store, MarketCalendar(), now=datetime(2026, 11, 12, 3, 0, tzinfo=UTC))
    return s, store


def crafted_breakout_context():
    """Simulated volume never spikes 1.5x, so the volume breakout gets a hand-made context:
    30 quiet days, then a close above the 20-day high on 3x volume."""
    cal, d, bars = MarketCalendar(), date(2026, 9, 1), []
    while len(bars) < 31:
        if cal.is_trading_day(d):
            i = len(bars)
            bars.append(Bar("TCS", daily_start(d), 100 + 0.1 * i, 101 + 0.1 * i, 99 + 0.1 * i, 100 + 0.1 * i, 1000.0))
        d += timedelta(days=1)
    last = bars[-1]
    bars[-1] = replace(last, open=103.0, high=106.0, low=102.5, close=105.5, volume=3000.0)
    return MarketContext("TCS", "stock", bars[-1].start, "TRENDING_UP", daily=tuple(bars))


def first_signal_context(strategy, s, store):
    """Deterministically search simulated data for a context where the strategy fires."""
    if strategy.id == "swing_breakout":
        return crafted_breakout_context()
    for inst in [i for i in s.watchlist if i.kind == "stock"]:
        if strategy.interval == "1day":
            d = store.history_bars("bars_1d", inst.symbol, "sim")
            for i in range(60, len(d)):
                for reg in sorted(strategy.allowed_regimes):
                    ctx = MarketContext(inst.symbol, "stock", d[i].start, reg, daily=tuple(d[:i + 1]))
                    if strategy.evaluate(ctx):
                        return ctx
        else:
            by = {}
            for b in store.history_bars("bars_5m", inst.symbol, "sim"):
                by.setdefault(b.start.astimezone(IST).date(), []).append(b)
            for day in sorted(by):
                bars = by[day]
                for i in range(len(bars)):
                    for reg in sorted(strategy.allowed_regimes):
                        ctx = MarketContext(inst.symbol, "stock", bars[i].start + timedelta(minutes=5), reg,
                                            intraday=tuple(bars[:i + 1]))
                        if strategy.evaluate(ctx):
                            return ctx
    return None


@pytest.mark.parametrize("sid", sorted(CLASSES))
def test_ACCEPTANCE_strategy_is_silent_outside_its_regimes(sid, sim_store):
    s, store = sim_store
    strat = build_strategies(settings(), [sid])[0]
    ctx = first_signal_context(strat, s, store)
    assert ctx is not None, f"{sid}: no firing context found in simulated data"
    sigs = strat.evaluate(ctx)
    assert sigs and all(x.strategy_id == sid and x.strategy_version == strat.version for x in sigs)
    for reg in REGIMES:
        if reg not in strat.allowed_regimes:
            assert strat.evaluate(replace(ctx, regime=reg)) == [], f"{sid} fired in {reg}"
    # not LIVE, or an index: no signal either
    assert strat.evaluate(replace(ctx, status="STALE")) == []
    assert strat.evaluate(replace(ctx, kind="index")) == []
    # deterministic: same context, same signals
    assert strat.evaluate(ctx) == sigs
    assert all(x.data_as_of <= ctx.asof for x in sigs)


def test_strategy_version_changes_with_thresholds():
    a = build_strategies(settings(), ["orb"])[0]
    b = build_strategies(settings(strategies={"orb": {"reward_risk": 3.0}}), ["orb"])[0]
    assert a.version != b.version and a.version.startswith("1/")


def test_orb_long_fixture():
    s = build_strategies(settings(), ["orb"])[0]
    rng = [(1000, 1004, 998, 1002, 1000), (1002, 1005, 1000, 1003, 1000), (1003, 1004, 1001, 1002, 1000)]
    brk = [(1002, 1010, 1002, 1009, 2000)]
    bars = tuple(five("TCS", rng + brk))
    ctx = MarketContext("TCS", "stock", bars[-1].start + timedelta(minutes=5), "TRENDING_UP", intraday=bars)
    out = s.evaluate(ctx)
    assert len(out) == 1 and out[0].direction == "LONG" and out[0].stop == 998.0
    assert out[0].target == round(1009 + 2 * (1009 - 998), 2)
    assert s.evaluate(replace(ctx, regime="TRENDING_DOWN")) == []         # no long on a down day
    # second breakout bar the same day: not the first close above, so silent
    bars2 = tuple(five("TCS", rng + brk + [(1009, 1012, 1008, 1011, 2500)]))
    assert s.evaluate(replace(ctx, asof=bars2[-1].start + timedelta(minutes=5), intraday=bars2)) == []


# ---------------- point-in-time regime ----------------
def test_ACCEPTANCE_backtest_regime_uses_only_past_data(sim_store):
    s, store = sim_store
    cal = MarketCalendar()
    days = sorted({b.start.astimezone(IST).date() for b in store.history_bars("bars_5m", "NIFTY", "sim")})
    d = days[-5]
    t = ist(11, 0, d=d)
    before = PointInTimeRegime(s, store, cal, "sim").at_bar(t)
    assert before.regime != "UNKNOWN", before.reasons
    # Rewrite everything AFTER t: later 5-minute bars of that day and every later daily bar.
    future = Store(":memory:")
    for table in ("bars_1d", "bars_5m"):
        for inst in s.watchlist:
            bars = store.history_bars(table, inst.symbol, "sim")
            moved = []
            for b in bars:
                later = (b.start + timedelta(minutes=5) > t) if table == "bars_5m" else (b.start >= daily_start(d))
                moved.append(replace(b, open=b.open * 3, high=b.high * 3.5, low=b.low * 0.2, close=b.close * 0.3)
                             if later else b)
            future.save_history_bars(table, moved, "sim")
    after = PointInTimeRegime(s, future, cal, "sim").at_bar(t)
    assert (after.regime, after.reasons, after.values) == (before.regime, before.reasons, before.values)
    # ...while changing a bar that ended before t does change the inputs (the check is not vacuous)
    past = Store(":memory:")
    for table in ("bars_1d", "bars_5m"):
        for inst in s.watchlist:
            bars = store.history_bars(table, inst.symbol, "sim")
            if inst.symbol == "NIFTY" and table == "bars_5m":
                bars = [replace(b, high=b.high * 1.05) if b.start.astimezone(IST).date() == d
                        and b.start + timedelta(minutes=5) <= t else b for b in bars]
            past.save_history_bars(table, bars, "sim")
    changed = PointInTimeRegime(s, past, cal, "sim").at_bar(t)
    assert changed.values != before.values


def test_point_in_time_regime_unknown_when_index_bar_missing(sim_store):
    s, store = sim_store
    pit = PointInTimeRegime(s, store, MarketCalendar(), "sim")
    assert pit.at_bar(ist(11, 0, d=date(2020, 1, 6))).regime == "UNKNOWN"


# ---------------- walk-forward and reports ----------------
def test_split_and_windows():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
    dev, hold = split_sessions(days, 20)
    assert len(hold) == 20 and dev[-1] < hold[0] and dev + hold == days
    w = windows(dev, 21, 21)
    assert [len(x) for x in w] == [21, 21, 21, 17] and sum(w, []) == dev


def mk_trade(net, regime="RANGE", day=1):
    t = ist(10, 0, d=date(2026, 9, day))
    return Trade("s", "1", "TCS", "LONG", "INTRADAY", "intraday_equity", "id", t.isoformat(), 1.0, regime,
                 t.isoformat(), 1.0, 1, 0.5, 2.0, t.isoformat(), 1.0, "TARGET", net + 1, {"total": 1.0}, net)


def test_ACCEPTANCE_reports_mark_low_sample():
    st = stats([mk_trade(10)] * 29, 200000, 30)
    assert st["low_sample"] and st["sample_note"].startswith("LOW SAMPLE: 29 trades")
    assert stats([mk_trade(10)] * 30, 200000, 30)["low_sample"] is False


def test_stats_arithmetic():
    st = stats([mk_trade(100, day=1), mk_trade(-40, day=2), mk_trade(-60, day=3), mk_trade(50, day=4)], 200000, 30)
    assert (st["trades"], st["wins"], st["losses"], st["win_rate_pct"]) == (4, 2, 2, 50.0)
    assert (st["avg_win_inr"], st["avg_loss_inr"], st["net_pnl_inr"]) == (75.0, -50.0, 50.0)
    assert st["max_drawdown_inr"] == 100.0 and st["profit_factor"] == 1.5 and st["costs_inr"] == 4.0


def test_report_by_regime_windows_and_cost_label():
    s = settings()
    strat = build_strategies(s, ["orb"])[0]
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]
    trades = [mk_trade(10, "RANGE", 1), mk_trade(-5, "TRENDING_UP", 2)]
    rep = build_report(strat, s, CostModel(s.costs), "breeze", days, [date(2026, 11, 1)], trades, Counter(),
                       None, None, 50)
    assert rep["cost_label"] == "standard rates, not yet verified against a contract note"
    assert set(rep["by_regime"]) == {"RANGE", "TRENDING_UP"} and rep["by_regime"]["RANGE"]["low_sample"]
    assert rep["walk_forward"]["windows"][0]["trades"] == 2 and rep["overall"]["low_sample"]
    assert rep["holdout"]["evaluated"] is False and "untouched" in rep["holdout"]["result"]
    assert rep["period"] == {"from": "2026-09-01", "to": "2026-09-30", "sessions": 30}


def test_run_backtest_keeps_holdout_untouched_and_saves(sim_store, tmp_path):
    s0, store = sim_store
    raw = s0.model_dump()
    raw["costs"] = TEST_COSTS
    raw["backtest"]["reports_dir"] = str(tmp_path)
    s = Settings.model_validate(raw)
    strat = build_strategies(s, ["range_reversion"])[0]
    eng = Engine(s, store, "sim", MarketCalendar())
    res = run_backtest(s, store, "sim", strat, engine=eng, now=datetime(2026, 11, 12, tzinfo=UTC))
    rep = res["report"]
    hold_start = date.fromisoformat(rep["holdout"]["from"])
    assert all(datetime.fromisoformat(t["exit_time"]).astimezone(IST).date() < hold_start for t in res["trades"])
    assert rep["holdout"]["evaluated"] is False and rep["data_source"] == "sim" and "SIMULATED" in rep["data_note"]
    assert (tmp_path / "range_reversion_latest.json").exists()
    assert store.backtest_run(rep["run_id"])["report"]["overall"] == rep["overall"]
    # the same run again gives the same trades (deterministic)
    again = run_backtest(s, store, "sim", strat, engine=Engine(s, store, "sim", MarketCalendar()), save=False)
    assert again["trades"] == res["trades"]
    with_hold = run_backtest(s, store, "sim", strat, engine=eng, include_holdout=True, save=False)
    assert with_hold["report"]["holdout"]["evaluated"] is True
    assert any(t.get("holdout") for t in with_hold["trades"]) or with_hold["report"]["holdout"]["result"]["trades"] == 0


def test_backtest_routes_and_page(sim_store):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed
    s0, _ = sim_store
    store = Store(":memory:")
    s = settings()
    store.save_backtest("r1", datetime(2026, 11, 12, tzinfo=UTC), "orb", "1/x", {}, {
        "strategy_id": "orb", "overall": stats([], 1, 30), "period": {}, "cost_label": "standard rates"}, [])
    app = create_app(s, feed=FakeFeed(), db_path=":memory:", background=False)
    app.state.store.save_backtest("r1", datetime(2026, 11, 12, tzinfo=UTC), "orb", "1/x", {},
                                  {"strategy_id": "orb", "overall": stats([], 1, 30)}, [{"n": 1}])
    with TestClient(app) as c:
        assert c.get("/api/backtests").json()["runs"][0]["run_id"] == "r1"
        assert c.get("/api/backtests/r1").json()["trades"] == [{"n": 1}]
        assert c.get("/api/backtests/nope").status_code == 404
        page = c.get("/backtests")
        assert page.status_code == 200 and "LOW SAMPLE" in page.text and "next bar" in page.text


# ---------------- history: 6-month 5-minute backfill ----------------
class Rec:
    name, remote = "breeze", True

    def __init__(self, floor=None):
        self.sim, self.calls, self.floor = SimHistorySource(), [], floor

    def fetch(self, inst, interval, a, b):
        self.calls.append((inst.symbol, interval, a, b))
        rows = self.sim.fetch(inst, interval, a, b)
        if self.floor:
            rows = [r for r in rows if r["datetime"][:10] >= self.floor.isoformat()]
        return rows


AFTER = datetime(2026, 11, 11, 11, 0, tzinfo=UTC)


def one(sym="TCS", intraday_days=190):
    raw = load_settings().model_dump()
    raw["watchlist"] = [i for i in raw["watchlist"] if i["kind"] == "index" or i["symbol"] == sym]
    raw["history"]["intraday_days"] = intraday_days
    return Settings.model_validate(raw)


def test_backfill_fetches_only_older_chunks_then_remembers_floor():
    store = Store(":memory:")
    HistoryLoader(one(intraday_days=10), store, Rec(), sleep=lambda x: None).update(AFTER, {"TCS"})
    first_before = store.first_history_start("bars_5m", "TCS", "breeze")
    src = Rec(floor=date(2026, 7, 1))           # the source has nothing before 1 July
    HistoryLoader(one(), store, src, sleep=lambda x: None).update(AFTER, {"TCS"})
    five_calls = [c for c in src.calls if c[1] == INTRADAY]
    assert five_calls and all(c[3] <= first_before for c in five_calls)            # nothing re-fetched
    assert all(c[3] - c[2] <= timedelta(days=10) for c in five_calls)               # date-range chunks
    assert store.first_history_start("bars_5m", "TCS", "breeze").astimezone(IST).date() == date(2026, 7, 1)
    assert store.history_floor("bars_5m", "TCS", "breeze") is not None
    src2 = Rec(floor=date(2026, 7, 1))
    out = HistoryLoader(one(), store, src2, sleep=lambda x: None).update(AFTER, {"TCS"})
    assert src2.calls == [] and out["TCS:5minute"]["skipped"] == "already stored"


def test_coverage_report_lists_first_day_per_symbol():
    from app.history import coverage
    store = Store(":memory:")
    HistoryLoader(one(intraday_days=20), store, Rec(), sleep=lambda x: None).update(AFTER, {"TCS"})
    text = coverage(store, "breeze", ["TCS", "INFY"])
    assert "TCS" in text and "INFY         NONE" in text and "2026-10-2" in text


def test_preopen_and_postclose_bars_are_dropped_and_purged():
    """Breeze returns 09:00-09:10 pre-open and 15:30-15:35 post-close 5-minute bars (seen 2026-11-12)."""
    from app.history import parse_rows
    rows = [{"datetime": f"2026-11-10 {hm}:00", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1}
            for hm in ("09:00", "09:05", "09:10", "09:15", "15:25", "15:30", "15:35")]
    bars, bad = parse_rows(rows, "TCS", "stock", INTRADAY)
    assert [b.start.astimezone(IST).strftime("%H:%M") for b in bars] == ["09:15", "15:25"] and bad == []
    daily, _ = parse_rows(rows[:1], "TCS", "stock", DAILY)
    assert len(daily) == 1                                    # daily bars are not filtered by time
    store = Store(":memory:", live=True)
    odd = [Bar("TCS", ist(h, m), 10, 11, 9, 10, 1) for h, m in ((9, 5), (9, 15), (15, 25), (15, 30))]
    store.save_history_bars("bars_5m", odd, "breeze")
    assert store.delete_bars_outside_session("breeze") == 2
    left = store.history_bars("bars_5m", "TCS", "breeze")
    assert [b.start.astimezone(IST).strftime("%H:%M") for b in left] == ["09:15", "15:25"]


# ---------------- unadjusted corporate actions (data breaks) ----------------
def test_gap_breaks_and_after_last_break():
    from app.indicators import after_last_break, gap_breaks
    bars = [Bar("X", ist(9, 15, d=date(2026, 9, 1) + timedelta(days=i)), o, o, o, c, 1)
            for i, (o, c) in enumerate([(100, 101), (101, 102), (51, 52), (52, 53), (53, 54)])]
    assert gap_breaks([b.open for b in bars], [b.close for b in bars], 25) == [2]     # 102 -> 51 (-50%)
    assert gap_breaks([b.open for b in bars], [b.close for b in bars], 60) == []
    assert [b.close for b in after_last_break(bars, 25)] == [52, 53, 54]


def daily_series(sym, closes, start=date(2026, 1, 1)):
    cal, d, out = MarketCalendar(), start, []
    for c in closes:
        while not cal.is_trading_day(d):
            d += timedelta(days=1)
        o = c[0] if isinstance(c, tuple) else c
        cl = c[1] if isinstance(c, tuple) else c
        out.append(Bar(sym, daily_start(d), o, max(o, cl) + 1, min(o, cl) - 1, cl, 1e5))
        d += timedelta(days=1)
    return out


def test_breadth_ignores_history_across_a_break(sim_store):
    """A 1:1 bonus halves the price overnight. Without the rule the stock looks 'below its average'
    for 20 days; with it, the stock has too little clean history and is left out of breadth."""
    s, store = sim_store
    pit = PointInTimeRegime(s, store, MarketCalendar(), "sim")
    days = sorted({b.start.astimezone(IST).date() for b in store.history_bars("bars_5m", "NIFTY", "sim")})
    d = days[-3]
    base = pit.at_bar(ist(11, 0, d=d))
    halved = Store(":memory:")
    for table in ("bars_1d", "bars_5m"):
        for inst in s.watchlist:
            bars = store.history_bars(table, inst.symbol, "sim")
            if inst.symbol == "TCS":
                cut = daily_start(d) - timedelta(days=7)
                bars = [b if b.start >= cut else replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2,
                                                            close=b.close * 2) for b in bars]
            halved.save_history_bars(table, bars, "sim")
    after = PointInTimeRegime(s, halved, MarketCalendar(), "sim").at_bar(ist(11, 0, d=d))
    assert "TCS" in after.values["breadth_missing"] and "TCS" not in base.values["breadth_missing"]


def test_swing_trade_open_across_a_break_is_dropped():
    s = settings()
    store = Store(":memory:")
    closes = [100 + i for i in range(10)] + [(55, 56)] + [56 + i for i in range(1, 10)]
    bars = daily_series("TCS", closes)
    store.save_history_bars("bars_1d", bars, "sim")
    store.save_history_bars("bars_1d", [replace(b, symbol="NIFTY") for b in bars], "sim")
    days = [bar_date_(b) for b in bars]

    class Once(Strategy):
        id, horizon, interval = "once", "SWING", "1day"
        allowed_regimes = frozenset(REGIMES)

        def _evaluate(self, ctx):
            if ctx.daily[-1].start != bars[7].start:
                return []
            from app.strategies.base import next_weekday_open_window
            c = ctx.daily[-1].close
            return [self.signal(ctx, "LONG", c, c - 30, c + 100, next_weekday_open_window(days[7]), ["t"])]

    from app.config import SwingBreakoutParams
    eng = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime())
    skipped = Counter()
    trades = eng.run_swing(Once(SwingBreakoutParams()), "TCS", "stock", days, skipped, max_hold=10)
    assert trades == [] and skipped["DROPPED_DATA_BREAK_DURING_TRADE"] == 1      # no fake -45% stop-out
    assert eng.breaks("TCS") == [10] and eng.breaks("NIFTY") == [10]


def bar_date_(b):
    return b.start.astimezone(IST).date()


def test_exposure_counts_overlapping_trades():
    from app.backtest import exposure
    a, b, c = mk_trade(1, day=1), mk_trade(1, day=1), mk_trade(1, day=2)
    b = replace(b, entry_time=a.entry_time, exit_time=ist(11, 0, d=date(2026, 9, 1)).isoformat(), qty=10)
    # a opens and closes at 10:00 (exits are counted before entries at the same moment), b is open 10:00-11:00
    e = exposure([a, b, c], 100.0)
    assert e["max_open_trades"] == 1 and e["max_position_value_inr"] == 10.0
    long_a = replace(a, exit_time=ist(12, 0, d=date(2026, 9, 1)).isoformat())
    e = exposure([long_a, b, c], 100.0)
    assert e["max_open_trades"] == 2 and e["max_position_value_inr"] == 11.0 and e["max_position_value_pct_of_capital"] == 11.0
