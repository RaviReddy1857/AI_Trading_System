"""Phase 3b: cost estimate, v2 strategies (orb_v2, range_reversion_v2, swing v2 market filter),
retired vwap_pullback, fixed holdout start, regime ignoring Breeze's 15:15/15:20 bars,
holidays loaded for backtests, history floor on the first download."""
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from app.backtest import Engine, load_calendar, run_backtest, split_sessions
from app.calendar import MarketCalendar
from app.config import Settings
from app.costs import CostModel
from app.history import daily_start
from app.models import Bar
from app.quality import IST
from app.regime import PointInTimeRegime
from app.store import Store
from app.strategies import CLASSES, MarketContext, build_strategies, intraday_round_trip_pct
from app.strategies.base import LookAheadError
from app.strategies.orb_v2 import time_of_day_volume
from tests.fixture_settings import load_settings
from tests.test_phase3 import (DAY, FixedRegime, crafted_breakout_context, crafted_orb_v2_context, five, ist,
                               rising_index, settings)

UTC = timezone.utc


# ---------------- cost estimate ----------------
def test_round_trip_cost_is_charges_plus_slippage_both_sides():
    s = settings()
    pct = intraday_round_trip_pct(s)
    value = s.account.capital_inr * s.backtest.position_cap_pct / 100
    charges = CostModel(s.costs).equity("intraday_equity", value, value).total
    assert pct == pytest.approx(float(charges / Decimal(str(value)) * 100) + 2 * s.costs.slippage_pct.intraday_equity)
    assert 0.18 < pct < 0.19          # about 0.187% at current rates: 5x = 0.94%, 2x = 0.37%


def test_cost_aware_strategies_need_a_cost_estimate():
    s = settings()
    with pytest.raises(ValueError, match="round_trip_cost_pct"):
        CLASSES["orb_v2"](s.strategies.orb_v2)


def test_cost_estimate_is_part_of_the_v2_version():
    s = settings()
    a = CLASSES["orb_v2"](s.strategies.orb_v2, 0.187)
    b = CLASSES["orb_v2"](s.strategies.orb_v2, 0.25)
    assert a.version != b.version and a.version.startswith("2/")


# ---------------- registry ----------------
def test_vwap_pullback_is_retired_but_runnable_by_name():
    s = settings()
    default = [x.id for x in build_strategies(s)]
    assert "vwap_pullback" not in default
    assert {"orb", "range_reversion", "swing_pullback", "swing_breakout",
            "orb_v2", "range_reversion_v2", "swing_pullback_v2", "swing_breakout_v2"} <= set(default)
    assert build_strategies(s, ["vwap_pullback"])[0].id == "vwap_pullback"


def test_unknown_inactive_id_is_rejected():
    raw = settings().model_dump()
    raw["strategies"]["inactive"] = ["nope"]
    with pytest.raises(ValueError, match="unknown ids"):
        Settings.model_validate(raw)


def test_v1_versions_are_unchanged_by_phase_3b():
    """v1 is kept for comparison: its ids and versions are the ones the phase 3 reports carry."""
    real = __import__("app.config", fromlist=["load_settings"]).load_settings()
    got = {x.id: x.version for x in build_strategies(real, ["orb", "vwap_pullback", "range_reversion",
                                                            "swing_pullback", "swing_breakout"])}
    assert all(v.startswith("1/") and len(v) == 10 for v in got.values())
    # same params -> same hash as before the cost estimate existed (no cost in the v1 hash)
    from app.strategies.orb import OpeningRangeBreakout
    assert OpeningRangeBreakout(real.strategies.orb).version == got["orb"]


# ---------------- orb_v2 ----------------
def orb2(cost=0.187, **over):
    s = settings(strategies={"orb_v2": over}) if over else settings()
    return CLASSES["orb_v2"](s.strategies.orb_v2, cost)


def test_orb_v2_long_on_volume_above_time_of_day_average():
    st, ctx = orb2(), crafted_orb_v2_context()
    sigs = st.evaluate(ctx)
    assert len(sigs) == 1
    x = sigs[0]
    assert x.direction == "LONG" and x.entry == 1011 and x.stop == 1000 and x.target == 1011 + 2 * 11
    assert any("time-of-day" in w or "bar's" in w for w in x.why_now)


def test_orb_v2_short_mirrored_on_trending_down():
    ctx = crafted_orb_v2_context()
    bars = list(ctx.intraday)
    bars[-1] = replace(bars[-1], open=1001, high=1002, low=988, close=989, volume=2000.0)
    sigs = orb2().evaluate(replace(ctx, intraday=tuple(bars), regime="TRENDING_DOWN"))
    assert sigs and sigs[0].direction == "SHORT" and sigs[0].stop == 1010
    assert orb2().evaluate(replace(ctx, intraday=tuple(bars), regime="TRENDING_UP")) == []


def test_orb_v2_skips_a_stop_tighter_than_5x_round_trip_cost():
    st = orb2(cost=0.25)                      # 5 x 0.25 = 1.25% > the 1.09% stop
    assert st.evaluate(crafted_orb_v2_context()) == []
    assert st.rejections["stop_below_cost_floor"] == 1
    assert orb2(cost=0.20).evaluate(crafted_orb_v2_context())   # 1.00% floor: passes


def test_orb_v2_needs_volume_above_1_5x_and_20_prior_sessions():
    ctx = crafted_orb_v2_context()
    bars = list(ctx.intraday)
    bars[-1] = replace(bars[-1], volume=1500.0)         # exactly 1.5x: not above
    assert orb2().evaluate(replace(ctx, intraday=tuple(bars))) == []
    assert orb2().evaluate(replace(ctx, prior_sessions=ctx.prior_sessions[1:])) == []   # 19 sessions


def test_orb_v2_only_the_first_qualifying_breakout_of_the_day():
    ctx = crafted_orb_v2_context()
    bars = list(ctx.intraday)
    extra = replace(bars[-1], start=bars[-1].start + timedelta(minutes=5), close=1013)
    later = replace(ctx, intraday=tuple(bars + [extra]), asof=extra.start + timedelta(minutes=5))
    assert orb2().evaluate(later) == []


def test_orb_v2_uses_a_30_minute_range():
    ctx = crafted_orb_v2_context()
    bars = list(ctx.intraday)
    bars[5] = replace(bars[5], high=1015)                # 09:40 bar is inside the 30-minute range
    assert orb2().evaluate(replace(ctx, intraday=tuple(bars))) == []    # 1011 is not above 1015


def test_orb_v2_prior_session_after_today_is_look_ahead():
    ctx = crafted_orb_v2_context()
    future = tuple(five("TCS", [(1000, 1002, 998, 1001, 1000.0)] * 3, d=DAY + timedelta(days=1)))
    with pytest.raises(LookAheadError):
        orb2().evaluate(replace(ctx, prior_sessions=ctx.prior_sessions[1:] + (future,)))


def test_time_of_day_volume_average():
    days = [tuple(five("X", [(1, 1, 1, 1, v)] * 3, d=date(2026, 10, d))) for d, v in ((5, 100.0), (6, 300.0))]
    assert time_of_day_volume(days, time(9, 20), 2, 2) == 200.0
    assert time_of_day_volume(days, time(9, 20), 3, 2) is None        # fewer sessions than the lookback
    assert time_of_day_volume(days, time(10, 0), 2, 1) is None        # no bar at that time


# ---------------- range_reversion_v2 ----------------
def rr_ctx(prev, last, regime="RANGE"):
    closes = [1000 + (0.5 if i % 2 else -0.5) for i in range(19)] + [prev, last]
    bars = five("TCS", [(c, c + 1, c - 1, c, 1000.0) for c in closes])
    return MarketContext("TCS", "stock", bars[-1].start + timedelta(minutes=5), regime, intraday=tuple(bars))


def rr2(cost=0.187):
    return CLASSES["range_reversion_v2"](settings().strategies.range_reversion_v2, cost)


def test_range_v2_widens_a_tight_stop_to_5x_cost_and_targets_the_average():
    st = rr2()
    sigs = st.evaluate(rr_ctx(990, 995))
    assert len(sigs) == 1
    x = sigs[0]
    assert x.direction == "LONG" and x.entry == 995
    assert x.stop == round(995 * (1 - 5 * 0.187 / 100), 2)                 # widened (3-bar stop was ~0.70%)
    assert x.target == pytest.approx(999.25, abs=0.01)                      # the band average
    assert st.rejections["stop_widened_to_cost_floor (kept, not removed)"] == 1


def test_range_v2_keeps_a_wider_structural_stop():
    sigs = rr2().evaluate(rr_ctx(970, 985))
    assert sigs and sigs[0].stop == round(969 * 0.999, 2)                   # 3-bar low less 0.10%, 1.7% away


def test_range_v2_skips_a_target_closer_than_2x_cost():
    st = rr2()
    assert st.evaluate(rr_ctx(992, 996)) == []                              # average 0.34% away < 0.374%
    assert st.rejections["target_below_cost_floor"] == 1


def test_range_v2_short_mirrored_and_range_only():
    closes_ctx = rr_ctx(1010, 1005)
    sigs = rr2().evaluate(closes_ctx)
    assert sigs and sigs[0].direction == "SHORT" and sigs[0].stop >= 1005 * (1 + 5 * 0.187 / 100) - 0.01
    assert rr2().evaluate(replace(closes_ctx, regime="TRENDING_UP")) == []


# ---------------- swing v2 market filter ----------------
def falling_index(until, n=60):
    rising = rising_index(until, n)
    return tuple(replace(b, close=30000 - 10 * i) for i, b in enumerate(rising))


@pytest.mark.parametrize("sid", ["swing_breakout_v2"])
def test_swing_v2_needs_nifty_above_its_50_day_average(sid):
    ctx = crafted_breakout_context()
    d = ctx.daily[-1].start.astimezone(IST).date()
    st = build_strategies(settings(), [sid])[0]
    up = st.evaluate(replace(ctx, index_daily=rising_index(d)))
    assert up and any("market filter" in w for w in up[0].why_now)
    assert st.evaluate(replace(ctx, index_daily=falling_index(d))) == []
    assert st.rejections["index_below_average"] == 1
    assert st.evaluate(replace(ctx, index_daily=())) == []                 # no index history: no signal
    v1 = build_strategies(settings(), ["swing_breakout"])[0]
    assert v1.evaluate(replace(ctx, index_daily=falling_index(d)))         # v1 has no filter


def test_swing_v2_index_bar_after_signal_day_is_look_ahead():
    ctx = crafted_breakout_context()
    d = ctx.daily[-1].start.astimezone(IST).date()
    st = build_strategies(settings(), ["swing_breakout_v2"])[0]
    with pytest.raises(LookAheadError):
        st.evaluate(replace(ctx, index_daily=rising_index(d + timedelta(days=7))))


# ---------------- engine supplies the new context ----------------
def test_engine_supplies_prior_sessions_and_index_daily():
    s = settings()
    store = Store(":memory:")
    days = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    for d in days:
        store.save_history_bars("bars_5m", five("TCS", [(1, 1, 1, 1, 1.0)] * 2, d=d), "sim")
    store.save_history_bars("bars_1d", [Bar("NIFTY", daily_start(d), 1, 1, 1, 1, None) for d in days], "sim")
    eng = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime())
    prior = eng.prior_sessions("TCS", date(2026, 11, 5), 20)
    assert [x[0].start.astimezone(IST).date() for x in prior] == days[:2]
    assert len(eng.prior_sessions("TCS", date(2026, 11, 5), 1)) == 1
    assert [b.start.astimezone(IST).date() for b in eng.index_daily(date(2026, 11, 4))] == days[:2]


def test_engine_gives_orb_v2_the_previous_sessions():
    seen = []

    class Spy(CLASSES["orb_v2"]):
        def _evaluate(self, ctx):
            seen.append(len(ctx.prior_sessions))
            return []

    s = settings()
    store = Store(":memory:")
    cal = MarketCalendar()
    d, days = date(2026, 10, 1), []
    while len(days) < 25:
        if cal.is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    for d in days:
        store.save_history_bars("bars_5m", five("TCS", [(1, 1, 1, 1, 1.0)] * 72, d=d), "sim")   # full sessions
    eng = Engine(s, store, "sim", cal, regime=FixedRegime())
    eng.run(Spy(s.strategies.orb_v2, 0.187), days, [("TCS", "stock")])
    assert seen and min(seen) == 0 and max(seen) == 20


# ---------------- fixed holdout ----------------
def test_split_with_fixed_holdout_start():
    days = [date(2026, 9, 28) + timedelta(days=i) for i in range(10)]
    dev, hold = split_sessions(days, 20, date(2026, 10, 5))
    assert dev[-1] < date(2026, 10, 5) <= hold[0] and len(dev) + len(hold) == 10
    assert split_sessions(days, 20) == (days[:8], days[8:])


def test_config_freezes_the_holdout_starts():
    s = __import__("app.config", fromlist=["load_settings"]).load_settings()
    assert s.backtest.holdout_start == {"5minute": date(2026, 10, 5), "1day": date(2026, 6, 8)}


def test_run_backtest_uses_the_fixed_holdout(tmp_path):
    s = settings(backtest={"holdout_start": {"5minute": "2026-11-06"}, "reports_dir": str(tmp_path)})
    store = Store(":memory:")
    for d in (date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)):
        store.save_history_bars("bars_5m", five("NIFTY", [(1, 1, 1, 1)] * 3, d=d), "sim")
    eng = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime())
    rep = run_backtest(s, store, "sim", build_strategies(s, ["orb"])[0], engine=eng, save=False)["report"]
    assert rep["period"]["to"] == "2026-11-05" and rep["holdout"]["from"] == "2026-11-06"
    assert rep["holdout"]["evaluated"] is False and "filtered_signals" in rep


# ---------------- regime ignores 15:15 / 15:20 bars ----------------
def test_point_in_time_regime_skips_unreliable_late_bars():
    s = settings()
    store = Store(":memory:")
    rows = [(100, 101, 99, 100)] * 72 + [(100, 140, 60, 130), (100, 101, 99, 100.5)]   # 15:15 crazy, 15:20 ...
    bars = five("NIFTY", rows)
    bars = [b for b in bars if b.start.astimezone(IST).time() <= time(15, 25)]
    store.save_history_bars("bars_5m", bars, "sim")
    r = PointInTimeRegime(s, store, MarketCalendar(), "sim")
    kept = r._f("NIFTY", DAY)
    assert {b.start.astimezone(IST).time() for b in kept}.isdisjoint({time(15, 15), time(15, 20)})
    assert len(store.history_bars("bars_5m", "NIFTY", "sim")) == len(bars)          # still stored
    price, hi, lo = r._so_far("NIFTY", DAY, ist(15, 25))
    assert price == 100 and hi == 101 and lo == 99                                    # 15:15 bar not seen


# ---------------- holidays for backtests ----------------
def test_backtest_reads_holidays_from_the_csv_without_writing(tmp_path):
    (tmp_path / "holidays.csv").write_text("Sr. No,Date,Day,Description\n1,2026-11-10,Tuesday,Diwali\n",
                                           encoding="utf-8")
    s = settings(calendar={"holidays_csv": str(tmp_path / "holidays.csv")})
    store = Store(":memory:")
    cal = load_calendar(s, store)
    assert not cal.is_trading_day(date(2026, 11, 10))
    assert store.holidays() == {}                                   # nothing written (the app may hold the lock)


def test_backtest_falls_back_to_stored_holidays_when_the_csv_is_bad(tmp_path):
    (tmp_path / "holidays.csv").write_text("date,note\nnot-a-date,x\n", encoding="utf-8")
    s = settings(calendar={"holidays_csv": str(tmp_path / "holidays.csv")})
    store = Store(":memory:")
    store.replace_holidays([("2026-11-24", "Gurpurb")])
    cal = load_calendar(s, store)
    assert not cal.is_trading_day(date(2026, 11, 24)) and cal.is_trading_day(date(2026, 11, 10))


# ---------------- history: request timeout, tail chunk, floor on first download ----------------
def test_hung_request_times_out_instead_of_stalling():
    import threading
    from app.history import BreezeHistorySource, call_with_timeout
    release = threading.Event()

    class HungFeed:
        def historical(self, *a):
            release.wait(5)
            return []

    src = BreezeHistorySource(HungFeed(), timeout_seconds=0.2)
    inst = load_settings().watchlist[0]
    with pytest.raises(TimeoutError):
        src.fetch(inst, "1day", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
    release.set()
    assert call_with_timeout(lambda: 5, 1.0) == 5
    with pytest.raises(ValueError):
        call_with_timeout(lambda: (_ for _ in ()).throw(ValueError("x")), 1.0)


def test_a_timeout_fails_only_that_symbol():
    from app.history import HistoryLoader, SimHistorySource

    class Src(SimHistorySource):
        remote = False

        def fetch(self, inst, interval, start, end):
            if inst.symbol == "TCS":
                raise TimeoutError("no reply within 60 s")
            return super().fetch(inst, interval, start, end)

    raw = load_settings().model_dump()
    raw["history"]["daily_days"] = 100
    raw["history"]["intraday_days"] = 3
    s = Settings.model_validate(raw)
    out = HistoryLoader(s, Store(":memory:"), Src(), sleep=lambda x: None).update(
        datetime(2026, 11, 12, 12, 0, tzinfo=UTC))
    assert "error" in out["TCS:1day"] and "error" not in out["INFY:1day"]


def test_sub_day_tail_is_folded_into_the_last_chunk_and_floor_set_on_first_download():
    from app.history import DAILY, HistoryLoader, SimHistorySource

    class Rec(SimHistorySource):
        name = "breeze"

        def __init__(self):
            super().__init__()
            self.calls = []

        def fetch(self, inst, interval, start, end):
            self.calls.append((inst.symbol, interval, start, end))
            rows = super().fetch(inst, interval, start, end)
            return [r for r in rows if r["datetime"] >= "2025-01-01"]     # nothing before 2025

    raw = load_settings().model_dump()
    raw["history"]["daily_days"] = 1801           # 2 x 900 days + 1 day: the last chunk would be a sliver
    raw["history"]["intraday_days"] = 1
    raw["watchlist"] = [i for i in raw["watchlist"] if i["kind"] == "index" or i["symbol"] == "TCS"]
    s = Settings.model_validate(raw)
    store, src = Store(":memory:"), Rec()
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    HistoryLoader(s, store, src, sleep=lambda x: None).update(now, {"TCS"})
    daily = [c for c in src.calls if c[1] == DAILY]
    assert len(daily) == 2 and all(c[3] - c[2] >= timedelta(days=1) for c in daily)
    assert store.history_floor("bars_1d", "TCS", "breeze") is not None
    src.calls.clear()
    HistoryLoader(s, store, src, sleep=lambda x: None).update(now, {"TCS"})
    assert not [c for c in src.calls if c[1] == DAILY and c[2] < datetime(2025, 1, 1, tzinfo=UTC)]


def test_test_start_excludes_earlier_sessions_from_development():
    from app.backtest import dev_holdout
    s = settings(backtest={"test_start": {"1day": "2026-10-01"}, "holdout_start": {"1day": "2026-11-05"}})
    days = [date(2026, 9, 28) + timedelta(days=i) for i in range(45)]
    dev, hold = dev_holdout(s, days, "1day")
    assert dev[0] == date(2026, 10, 1) and dev[-1] < date(2026, 11, 5) == hold[0]
    assert dev_holdout(s, days, "5minute")[0][0] == days[0]          # other interval unaffected
    real = __import__("app.config", fromlist=["load_settings"]).load_settings()
    assert real.backtest.test_start == {"1day": date(2019, 1, 22)}


# ---------------- strategy status: alerts off, paper only, evidence label ----------------
def test_every_phase3_cash_strategy_is_paper_only_without_alerts():
    real = __import__("app.config", fromlist=["load_settings"]).load_settings()
    assert real.strategies.alert_ids() == []
    for sid in CLASSES:
        st = real.strategies.status_of(sid)
        assert sid in real.strategies.status, f"{sid} has no status entry"
        assert st.alerts is False and st.mode == "paper_only"
        assert st.label in ("negative in backtests", "no edge vs random")
    built = build_strategies(real, list(CLASSES))
    assert all(x.alerts_enabled is False and x.mode == "paper_only" and x.status_label for x in built)


def test_strategy_status_defaults_and_validation():
    s = settings(strategies={"status": {}})
    assert s.strategies.status_of("orb").alerts is False and s.strategies.status_of("orb").mode == "paper_only"
    s2 = settings(strategies={"status": {"orb": {"alerts": True, "mode": "live", "label": "x"}}})
    assert s2.strategies.alert_ids() == ["orb"]
    raw = settings().model_dump()
    raw["strategies"]["status"] = {"nope": {}}
    with pytest.raises(ValueError, match="unknown ids"):
        Settings.model_validate(raw)


def test_report_carries_the_strategy_status():
    s = settings(strategies={"status": {"orb": {"label": "negative in backtests"}}})
    store = Store(":memory:")
    for d in (date(2026, 11, 3), date(2026, 11, 4)):
        store.save_history_bars("bars_5m", five("NIFTY", [(1, 1, 1, 1)] * 3, d=d), "sim")
    eng = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime())
    rep = run_backtest(s, store, "sim", build_strategies(s, ["orb"])[0], engine=eng, save=False)["report"]
    assert rep["strategy_status"] == {"alerts": False, "mode": "paper_only", "label": "negative in backtests"}
