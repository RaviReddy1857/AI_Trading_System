"""Phase 9 additions: falling-stock block, per-stock campaign limit, range and speed profile, scenario
history, open-position alerts, and the effect of each in the backtest against the baseline."""
import math
from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from app.config import StockOptionSelling as SosParams
from app.history import daily_start
from app.models import Bar, unhedged_short_legs
from app.options.base import ExpiryInfo
from app.options.option_history import OptionHistory
from app.options.shortlist import scenario_triggers, stock_view
from app.options.stock_backtest import ALERT_SETS, rule_effects, variant
from app.options.stock_profile import (DOWNTREND_BLOCK, HISTORY_LABEL, INSUFFICIENT_HISTORY, STOCK_LOSS_LIMIT,
                                       UPTREND_BLOCK, ClosedTrade, Snapshot, alert_kinds, alternating, breach_frequency, pivots, support_resistance,
                                       fii_streak_starts, range_speed_profile, scenario_history, snapshot, state_now,
                                       stock_loss_limit, trend_block, vix_spike_dates)
from app.options.stock_selling import SpreadPosition, build_stock_selling, position_alerts
from app.quality import IST
from app.store import Store
from tests.test_phase9_core import (settings, EXP, EXP_NEXT, LOT, NOW, chain, ctx_for, engine_for, index_bars, ist_date, run_bt,
                                    stock_bars, stock_world, weekdays_back)

CFG = settings().stock_option_selling
TB, CL, PR = CFG.trend_block, CFG.campaign_limit, CFG.profile
END = date(2026, 11, 13)


def series(closes, sym="RELIANCE", end=END, spread=5.0):
    return [Bar(sym, daily_start(d), c, c + spread, c - spread, c, 1e6) for d, c in zip(weekdays_back(end, len(closes)), closes)]


@pytest.fixture(scope="module")
def s():
    return settings()


# ---------------- 1. falling-stock block ----------------
def recovering():
    """A long fall, then a 60-session bounce: short-term up trend, but still under the 200-day average."""
    return series([1500 - 500 * i / 259 for i in range(260)] + [1000 + 1.5 * i for i in range(1, 61)])


def test_trend_block_blocks_puts_below_the_200_day_average_at_a_52_week_low_or_trailing_the_index():
    flat_idx = lambda bars: index_bars(bars)
    up = stock_bars()
    assert trend_block(up, flat_idx(up), "PE", TB) == (None, [])
    rec = recovering()
    code, why = trend_block(rec, flat_idx(rec), "PE", TB)
    assert code == DOWNTREND_BLOCK and len(why) == 1 and "below the 200-day average" in why[0]
    # a 52-week low five sessions ago, with the close back above the long average
    dip = series([1000.0] * 300 + [900, 1010, 1012, 1014, 1016])
    code, why = trend_block(dip, flat_idx(dip), "PE", TB)
    assert code == DOWNTREND_BLOCK and len(why) == 1 and "52-week low" in why[0]
    old_dip = series([1000.0] * 250 + [900] + [1010.0] * 60)        # the low is 60 sessions old: not recent
    assert trend_block(old_dip, flat_idx(old_dip), "PE", TB)[0] is None
    # flat stock while the index gained 15% in 63 sessions: trails by more than 10 points
    flat = series([900.0 if i == 100 else 1000.0 + (i % 2) for i in range(320)])     # its 52-week low is long past
    idx = index_bars(flat, 20000.0, 0.0)[:-64] + tuple(
        Bar("NIFTY", b.start, 0, 0, 0, 20000.0 * (1 + 0.15 * (i + 1) / 64), None) for i, b in enumerate(flat[-64:]))
    code, why = trend_block(flat, idx, "PE", TB)
    assert code == DOWNTREND_BLOCK and len(why) == 1 and "trails the index" in why[0]
    lag = index_bars(flat, 20000.0, 0.0)[:-64] + tuple(
        Bar("NIFTY", b.start, 0, 0, 0, 20000.0 * (1 + 0.08 * (i + 1) / 64), None) for i, b in enumerate(flat[-64:]))
    assert trend_block(flat, lag, "PE", TB)[0] is None              # 8 points behind: inside the margin


def test_trend_block_mirror_blocks_calls_in_a_strong_uptrend_and_unknown_never_passes():
    up, down = stock_bars(), stock_bars(start=1200, drift=-1.0)
    code, why = trend_block(up, index_bars(up), "CE", TB)
    assert code == UPTREND_BLOCK and any("above the 200-day average" in w for w in why) and any("52-week high" in w for w in why)
    assert trend_block(down, index_bars(down), "CE", TB) == (None, [])
    assert trend_block(down, index_bars(down), "PE", TB)[0] == DOWNTREND_BLOCK
    assert trend_block(up[-200:], index_bars(up[-200:]), "PE", TB)[0] == INSUFFICIENT_HISTORY
    assert trend_block(up, index_bars(up)[:-1], "PE", TB)[0] == INSUFFICIENT_HISTORY     # no index close for today
    assert trend_block(up, (), "PE", TB)[0] == INSUFFICIENT_HISTORY


def test_strategy_gives_no_put_signal_in_a_falling_stock_and_the_block_can_be_switched_off(s):
    bars = recovering()
    strat = build_stock_selling(s)
    assert strat.evaluate(ctx_for(s, bars)) == []
    assert any(r.startswith(DOWNTREND_BLOCK) and "200-day average" in r for r in strat.last_no_trade)
    off = build_stock_selling(settings(stock_option_selling={"trend_block": {**TB.model_dump(), "enabled": False},
                                                             "min_credit_to_max_loss": 0}))
    sig = off.evaluate(ctx_for(s, bars))
    assert sig and sig[0].details["side"] == "PE" and off.version != strat.version
    # the no-filter baseline ignores the block unless the comparison asks for it
    base = dict(no_filter=True, baseline_right="PE")
    assert strat.evaluate(ctx_for(s, bars, **base))
    with_block = variant(s, baseline_rules=("trend_block",))
    assert with_block.evaluate(ctx_for(s, bars, **base)) == []
    assert any(r.startswith(DOWNTREND_BLOCK) for r in with_block.last_no_trade)
    # too little history for the block: no signal, never a guess
    short = stock_bars(n=150)
    assert strat.evaluate(ctx_for(s, short)) == [] and any(r.startswith(INSUFFICIENT_HISTORY) for r in strat.last_no_trade)


# ---------------- 2. per-stock campaign limit ----------------
def ct(day, stopped, pnl, origin="SIGNAL"):
    return ClosedTrade(day, stopped, pnl, origin)


def test_campaign_limit_after_two_stops_or_a_cumulative_loss_within_90_days():
    cap = 200000.0
    d = date(2026, 6, 1)
    assert stock_loss_limit([], d, CL, cap) is None
    assert stock_loss_limit([ct(date(2026, 5, 1), True, -3000)], d, CL, cap) is None             # one stop is not enough
    two = [ct(date(2026, 4, 1), True, -3000), ct(date(2026, 5, 20), True, -3000)]
    until, why = stock_loss_limit(two, d, CL, cap)
    assert until == date(2026, 5, 20) + timedelta(days=45) and "2 stopped trades" in why
    assert stock_loss_limit(two, date(2026, 7, 3), CL, cap) is not None                           # day 44 of the cooling-off
    assert stock_loss_limit(two, date(2026, 7, 4), CL, cap) is None                               # day 45: it has passed
    far = [ct(date(2026, 2, 1), True, -3000), ct(date(2026, 5, 20), True, -3000)]                # 108 days apart
    assert stock_loss_limit(far, d, CL, cap) is None
    # a net loss of 5% of capital (Rs 10,000) in the window, without two stops
    loss = [ct(date(2026, 4, 10), False, -6000), ct(date(2026, 5, 10), False, 1500), ct(date(2026, 5, 25), False, -5500)]
    until, why = stock_loss_limit(loss, d, CL, cap)
    assert until == date(2026, 5, 25) + timedelta(days=45) and "net loss Rs 10,000.00" in why
    assert stock_loss_limit(loss[:2] + [ct(date(2026, 5, 25), False, -5400)], d, CL, cap) is None  # Rs 9,900: under the limit
    assert stock_loss_limit(loss, d, CL, 400000.0) is None                                        # the limit is a % of capital
    # a position entered by hand counts like any other, and a trade that closes later is not known yet
    manual = [ct(date(2026, 4, 1), True, -3000, "MANUAL"), ct(date(2026, 5, 20), True, -3000)]
    assert stock_loss_limit(manual, d, CL, cap) is not None
    assert stock_loss_limit(two, date(2026, 5, 19), CL, cap) is None


def test_strategy_gives_no_signal_in_a_stock_that_is_cooling_off(s):
    bars = stock_bars()
    strat = build_stock_selling(s)
    stops = (ct(date(2026, 9, 20), True, -4000, "MANUAL"), ct(date(2026, 10, 30), True, -4000))
    assert strat.evaluate(ctx_for(s, bars))
    assert strat.evaluate(ctx_for(s, bars, closed_trades=stops)) == []
    assert any(r.startswith(STOCK_LOSS_LIMIT) and "14-Dec-2026" in r for r in strat.last_no_trade)
    assert strat.evaluate(ctx_for(s, bars, closed_trades=stops[:1]))
    off = build_stock_selling(settings(stock_option_selling={"campaign_limit": {**CL.model_dump(), "enabled": False}}))
    assert off.evaluate(ctx_for(s, bars, closed_trades=stops))
    for bad in ({"campaign_limit": {"stock_cooloff_days": 0}}, {"trend_block": {"low_52w_sessions": 5}},
                {"profile": {"windows_years": []}}, {"profile": {"trending_efficiency": 1.5}}):
        with pytest.raises(Exception):
            settings(stock_option_selling=bad)


# ---------------- 3. range and speed profile ----------------
def test_swings_state_and_breach_frequency_by_hand():
    # 2-bar pivots on (low, high) bars: a low below the 2 lows before it and at or below the 2 after it
    hl = [(10, 12), (9, 11), (7, 9), (8, 10), (8, 14), (11, 15), (10, 13), (12, 16), (13, 18), (14, 19), (12, 15), (11, 14)]
    bars = [Bar("X", daily_start(d), lo, hi, lo, lo, 1.0) for d, (lo, hi) in zip(weekdays_back(END, len(hl)), hl)]
    got = pivots(bars, 2)
    assert ("L", 2, 4, 7) in got
    assert [x for x in got if x[0] == "H"] == [("H", 9, 11, 19)]          # above the 2 highs before and >= the 2 after
    assert all(x[2] == x[1] + 2 for x in got)                             # confirmed 2 sessions after the pivot
    eq = [Bar("X", daily_start(d), 5, 6, lo, 5, 1.0) for d, lo in zip(weekdays_back(END, 7), [5, 4, 3, 3, 4, 5, 6])]
    assert [x[1] for x in pivots(eq, 2) if x[0] == "L"] == [2]           # of two equal lows only the first counts
    assert pivots(bars[:4], 2) == []
    assert alternating([("L", 1, 3, 5), ("L", 4, 6, 4), ("H", 6, 8, 9)]) == [("L", 4, 6, 4), ("H", 6, 8, 9)]
    assert state_now([100 + i for i in range(30)], 20, 0.3) == ("TRENDING_UP", 1.0)
    assert state_now([100 - i for i in range(30)], 20, 0.3) == ("TRENDING_DOWN", 1.0)
    assert state_now([100 + (i % 2) for i in range(31)], 20, 0.3) == ("RANGING", 0.0)
    assert state_now([100, 101], 20, 0.3) == (None, None)
    # a put strike 5% below: starts at 100 (95 level; lows ahead 96, 94 -> breached), 96 (91.2; 94, 97 -> no), 94 (89.3; no)
    assert breach_frequency([100, 96, 94, 97, 99], 5.0, "PE", 2) == {"cases": 3, "breached": 1, "breached_pct": 33.3}
    assert breach_frequency([100, 104, 106, 103, 99], 5.0, "CE", 2) == {"cases": 3, "breached": 1, "breached_pct": 33.3}
    assert breach_frequency([100, 99], 5.0, "PE", 5) == {"cases": 0, "breached": 0, "breached_pct": None}


def test_range_speed_profile_windows_floor_and_implied_move():
    days = weekdays_back(END, 1600)
    bars = [Bar("RELIANCE", daily_start(d), 1000.0, 1000.0 + 50, 1000.0 - 50, 1000.0 + 20 * math.sin(i / 9), 1e6)
            for i, d in enumerate(days)]
    floor = date(2022, 1, 1)
    p = range_speed_profile(bars, PR, floor, "PE", 4.0, 30.0, dte_days=30)
    assert p["label"] == HISTORY_LABEL == "history, not a forecast" and p["as_of"] == END.isoformat()
    w2, w5 = p["windows"]["2"], p["windows"]["5"]
    assert w2["from"] >= (END - timedelta(days=731)).isoformat() and w5["from"] >= floor.isoformat()   # nothing before the floor
    assert w5["bars"] > w2["bars"] and w2["months"] in (22, 23, 24)
    assert w2["monthly_range_median_pct"] == pytest.approx(10.0, abs=0.01)       # high-low 100 on an open of 1000, every month
    # expected_move (section 2): spot x IV x sqrt(DTE / 365)
    assert p["implied_move_pct"] == pytest.approx(30.0 * math.sqrt(30 / 365), abs=0.01)
    assert p["expected_move_inr"] == pytest.approx(bars[-1].close * 0.30 * math.sqrt(30 / 365), abs=0.01)
    assert "not a guaranteed range" in p["implied_move_note"]
    from app.options.stock_profile import expected_move
    assert expected_move(1000, 25.0, 25) == pytest.approx(1000 * 0.25 * math.sqrt(25 / 365))
    assert expected_move(None, 25.0, 25) is None and expected_move(1000, None, 25) is None and expected_move(1000, 25.0, 0) is None
    assert w2["breach"]["distance_pct"] == 4.0 and w2["breach"]["side"] == "PE" and w2["breach"]["cases"] == w2["bars"] - 20
    assert w2["breach"]["breached"] == 0 and w2["upswings"] == 0                # a 2% wave never moves 4% or swings 8%
    assert 0 < w2["typical_move_pct"] < 4
    # unknown inputs stay unknown
    q = range_speed_profile(bars, PR, floor)
    assert q["implied_move_pct"] is None and q["windows"]["2"]["breach"] is None and "no expected move" in q["implied_move_note"]
    assert range_speed_profile([], PR)["windows"]["2"]["monthly_range_median_pct"] is None
    # a swinging stock: 20% waves give swings with their length in sessions
    wave = series([1000 * (1 + 0.2 * math.sin(i / 20)) for i in range(700)])
    sw = range_speed_profile(wave, PR)["windows"]["2"]
    assert sw["upswings"] >= 3 and 50 <= sw["avg_sessions_low_to_high"] <= 70 and sw["avg_upswing_pct"] > 30
    assert sw["avg_sessions_high_to_low"] is not None and sw["avg_downswing_pct"] < -25


# ---------------- 4. scenario history ----------------
def test_scenario_history_counts_only_completed_cases_and_marks_low_sample_and_no_data():
    bars = series([1000.0 + i for i in range(400)])                  # +1 a session
    dates = [ist_date(b.start.isoformat()) for b in bars]
    trig = {"twelve": {"dates": dates[10:250:20]}, "three": {"dates": dates[5:8]},
            "nothing": {"dates": None, "missing": "no data: fill data/x.csv"},
            "too recent": {"dates": [dates[-5]]}, "before floor": {"dates": [dates[3]]}}
    out = {r["scenario"]: r for r in scenario_history(bars, trig, PR, floor=dates[4])}
    assert all(r["label"] == "history, not a forecast" and r["horizon_days"] == 30 for r in out.values())
    t = out["twelve"]
    assert t["cases"] == 12 and t["status"] == "ok" and t["worst_pct"] > 0 and t["down_pct_of_cases"] == 0.0
    # by hand: trigger at index 10 (close 1010); 30 calendar days later is 22 sessions on: close 1032 -> +2.18%
    j = next(i for i, d in enumerate(dates) if d >= dates[10] + timedelta(days=30))
    assert t["best_pct"] == pytest.approx((bars[j].close / bars[10].close - 1) * 100, abs=0.01)
    assert out["three"]["cases"] == 3 and out["three"]["status"] == "LOW SAMPLE" and "fewer than 10" in out["three"]["note"]
    assert out["nothing"]["status"] == "no data" and out["nothing"]["cases"] == 0 and out["nothing"]["note"] == "no data: fill data/x.csv"
    assert "median_pct" not in out["nothing"]
    assert out["too recent"]["cases"] == 0 and out["too recent"]["status"] == "no data"        # its 30 days have not passed
    assert out["before floor"]["cases"] == 0
    # nothing uses later data: the cases known 100 sessions earlier are the same numbers later on
    early = {r["scenario"]: r for r in scenario_history(bars[:300], {"twelve": {"dates": dates[10:250:20]}}, PR)}
    later = {r["scenario"]: r for r in scenario_history(bars, {"twelve": {"dates": dates[10:250:20]}}, PR)}
    assert early["twelve"]["worst_pct"] == later["twelve"]["worst_pct"] and early["twelve"]["cases"] <= later["twelve"]["cases"]


def test_fii_streaks_need_consecutive_sessions_and_vix_spikes_are_one_session_rises():
    days = weekdays_back(date(2026, 3, 13), 10)
    nets = [100, -50, -60, -70, -80, 20, -10, -20, 30, -5]
    rows = list(zip(days, nets))
    assert fii_streak_starts(rows, 3) == [days[3]]                   # completed once, on the third selling day
    assert fii_streak_starts(rows, 2) == [days[2], days[7]]
    assert fii_streak_starts(rows, 3, selling=False) == []
    gap = [r for r in rows if r[0] != days[2]]                       # one day missing from the file
    assert fii_streak_starts(gap, 3) == [days[4]]                    # read as given: three rows in a row
    assert fii_streak_starts(gap, 3, sessions=days) == []            # with the session calendar the gap breaks the run
    assert fii_streak_starts(rows, 3, sessions=days) == [days[3]]
    # vix_spike (section 2): up 20% over 5 sessions, or above the 80th percentile of the previous closes
    cfgv = PR.model_copy(update={"vix_percentile_lookback": 20})
    vdays = weekdays_back(date(2026, 3, 13), 40)
    level = [12.0 if i % 2 == 0 else 12.2 for i in range(30)] + [12.5, 13.0, 15.5, 16.0, 15.9, 15.0, 14.0, 13.0, 12.6, 12.4]
    vix = [Bar("INDIAVIX", daily_start(d), v, v, v, v, None) for d, v in zip(vdays, level)]
    from app.options.stock_profile import vix_spike_at, vix_spike_series
    closes = [b.close for b in vix]
    assert vix_spike_at(closes, 4, cfgv) is None                    # not enough history for either part
    assert vix_spike_at(closes, 19, cfgv) is None                   # the rise part is known (no), the percentile is not
    assert vix_spike_at(closes, 25, cfgv) is False                  # both known: neither
    assert vix_spike_at(closes, 32, cfgv) is True                   # 15.5 vs 12.25 five sessions before: +26%
    assert vix_spike_at(closes, 30, cfgv) is True                   # 12.5: above the 80th percentile (12.2) of the last 20
    assert vix_spike_at(closes, 29, cfgv) is False
    assert vix_spike_dates(vix, cfgv) == [vdays[30]]                # one episode: its first day only
    assert vix_spike_dates(vix, cfgv, floor=vdays[35]) == []
    assert set(vix_spike_series(vix, cfgv)) == set(vdays)


def profile_store(with_inputs: bool):
    store = Store(":memory:")
    days = weekdays_back(END, 700)
    bars = []
    for i, d in enumerate(days):
        px = 1000 + 0.3 * i + 10 * math.sin(i / 6)
        bars.append(Bar("RELIANCE", daily_start(d), px - 1, px + 6, px - 6, px, 1e6))
        bars.append(Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None))
        v = 14 * (1.2 if i % 25 == 12 else 1.0)
        bars.append(Bar("INDIAVIX", daily_start(d), v, v, v, v, None))
    store.save_history_bars("bars_1d", bars, "sim")
    if with_inputs:
        store.replace_fii_dii([(d.isoformat(), -100.0 if (i // 4) % 5 == 0 else 100.0, 0.0) for i, d in enumerate(days[:650])])
        store.replace_events([(days[i].isoformat(), "RBI_POLICY", "", "fixture") for i in range(20, 600, 40)]
                             + [(days[i].isoformat(), "RESULTS", "RELIANCE", "fixture") for i in range(30, 600, 60)]
                             + [(days[40].isoformat(), "RESULTS", "TCS", "fixture")])
    return store, days


def test_ACCEPTANCE_scenarios_without_input_data_say_no_data_and_name_the_file_to_fill(s):
    store, days = profile_store(with_inputs=False)
    v = stock_view(s, store, "sim", "RELIANCE", now=NOW)
    assert v["label"] == "history, not a forecast" and v["profile"]["state_now"] in ("TRENDING_UP", "TRENDING_DOWN", "RANGING")
    by = {r["scenario"]: r for r in v["scenarios"]}
    assert len(by) == 4
    fii, rbi, res = by["after an FII selling streak"], by["after an RBI rate decision"], by["after its own results"]
    for r in (fii, rbi, res):
        assert r["status"] == "no data" and r["cases"] == 0 and r["note"].startswith("no data") and "median_pct" not in r
    assert "data/fii_dii.csv" in fii["note"] and "date,fii_net_cr,dii_net_cr" in fii["note"]
    assert "data/events.csv" in rbi["note"] and "RBI_POLICY" in rbi["note"]
    assert "data/events.csv" in res["note"] and "RESULTS" in res["note"]
    vix = by["after an India VIX spike (up 20% over 5 sessions or above its 80th percentile)"]   # only stored prices
    assert vix["cases"] >= 10 and vix["status"] == "ok"
    assert v["profile"]["implied_move_pct"] is None and v["setup"] is None      # no option chain stored
    assert any("no option-price figures" in n for n in v["notes"])
    # nothing was written anywhere: the files' tables are still empty
    assert store.fii_dii_all() == [] and store.events_between(date(2000, 1, 1), date(2100, 1, 1)) == []


def test_scenarios_use_the_stored_fii_rbi_and_results_dates_when_they_exist(s):
    store, days = profile_store(with_inputs=True)
    floor = days[0]
    trig = scenario_triggers(s, store, "sim", "RELIANCE", floor, days[-1])
    assert len(trig["after an RBI rate decision"]["dates"]) == 15
    assert len(trig["after its own results"]["dates"]) == 10         # the TCS row is not RELIANCE's
    streaks = trig["after an FII selling streak"]["dates"]
    assert streaks and all(d in days for d in streaks)
    v = stock_view(s, store, "sim", "RELIANCE", now=NOW)
    by = {r["scenario"]: r for r in v["scenarios"]}
    assert by["after an RBI rate decision"]["cases"] == 15 and by["after an RBI rate decision"]["status"] == "ok"
    assert by["after its own results"]["cases"] == 10 and by["after an FII selling streak"]["cases"] >= 10
    assert all(r["label"] == "history, not a forecast" for r in v["scenarios"])


def test_profile_route_is_read_only_and_unknown_stock_is_404():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    cfg = sim_settings()
    app = create_app(cfg, feed=FakeFeed(), db_path=":memory:", background=False)
    stock = next(i.symbol for i in cfg.watchlist if i.kind == "stock")
    with TestClient(app) as c:
        r = c.get(f"/api/stock-option-selling/profile/{stock}")
        assert r.status_code == 200 and r.json()["label"] == "history, not a forecast" and r.json()["stock"] == stock
        assert c.get("/api/stock-option-selling/profile/NOSUCH").status_code == 404
        assert c.get("/api/stock-option-selling/profile/NIFTY").status_code == 404
        assert "history, not a forecast" in c.get("/stock-option-selling").text


# ---------------- 5. open-position alerts ----------------
def snap(day=date(2026, 11, 12), **kw):
    base = dict(close=1100.0, support=1080.0, resistance=1130.0, low_52w=900.0, high_52w=1200.0, sma_long=1050.0,
                fii_bias="NEUTRAL", vix_spike=False, regime="RANGE")
    base.update(kw)
    return Snapshot(day, **base)


def kinds(prev, now, right="PE"):
    return [a["kind"] for a in alert_kinds(prev, now, right)]


def test_ACCEPTANCE_each_open_position_alert_fires_once_when_its_condition_begins():
    prev = snap()
    today = date(2026, 11, 13)
    assert kinds(prev, snap(today)) == []
    assert kinds(prev, snap(today, close=1079.0)) == ["BELOW_SUPPORT"]             # below yesterday's support
    assert kinds(prev, snap(today, close=1080.0)) == []                            # at the support is not below it
    assert kinds(prev, snap(today, close=899.0)) == ["BELOW_SUPPORT", "NEW_52W_LOW", "BROKE_LONG_AVERAGE"]
    assert kinds(prev, snap(today, close=1049.0, support=1040.0)) == ["BELOW_SUPPORT", "BROKE_LONG_AVERAGE"]
    assert kinds(snap(support=1000.0), snap(today, close=1049.0, support=1000.0)) == ["BROKE_LONG_AVERAGE"]
    assert kinds(prev, snap(today, fii_bias="BEARISH")) == ["FII_STREAK"]
    assert kinds(prev, snap(today, vix_spike=True)) == ["VIX_SPIKE"]
    assert kinds(snap(vix_spike=True), snap(today, vix_spike=True)) == []          # a spike that goes on is not new
    assert kinds(snap(vix_spike=None), snap(today, vix_spike=True)) == []          # unknown before: not a known change
    assert kinds(prev, snap(today, regime="VOLATILE")) == ["REGIME_TURN"]
    assert kinds(prev, snap(today, regime="EVENT")) == ["REGIME_TURN"]
    assert kinds(prev, snap(today, regime="TRENDING_DOWN")) == []
    assert kinds(prev, snap(today, news_tags=("EVENT_RISK",))) == ["EVENT_RISK"]
    # a condition that was already true yesterday is not a change today
    assert kinds(snap(close=1070.0), snap(today, close=1060.0)) == []
    assert kinds(snap(fii_bias="BEARISH"), snap(today, fii_bias="BEARISH")) == []
    assert kinds(snap(regime="VOLATILE"), snap(today, regime="VOLATILE")) == []
    assert kinds(snap(news_tags=("EVENT_RISK",)), snap(today, news_tags=("EVENT_RISK",))) == []
    # unknown values raise nothing; no previous snapshot, no alert
    assert kinds(prev, snap(today, close=1000.0, support=None, sma_long=None, low_52w=None)) == ["BELOW_SUPPORT", "BROKE_LONG_AVERAGE"][:1]
    assert kinds(snap(support=None, sma_long=None, fii_bias=None, regime=None),
                 snap(today, close=800.0, low_52w=None, sma_long=None, fii_bias="BEARISH", regime="EVENT")) == []
    assert kinds(None, snap(today, close=800.0)) == [] and kinds(prev, None) == []
    # bear call spread: the mirror image
    assert kinds(prev, snap(today, close=1131.0), "CE") == ["ABOVE_RESISTANCE"]
    assert kinds(prev, snap(today, close=1201.0), "CE") == ["ABOVE_RESISTANCE", "NEW_52W_HIGH"]
    assert kinds(snap(close=1040.0), snap(today, close=1060.0), "CE") == ["BROKE_LONG_AVERAGE"]
    assert kinds(prev, snap(today, fii_bias="BULLISH"), "CE") == ["FII_STREAK"]
    assert kinds(prev, snap(today, fii_bias="BEARISH"), "CE") == []
    assert kinds(prev, snap(today, close=1079.0), "CE") == []
    text = alert_kinds(prev, snap(today, close=1079.0), "PE", 15.0)[0]["what_changed"]
    assert "1,079.00" in text and "1,080.00" in text and "1,100.00" in text        # what changed, in numbers


def test_snapshot_levels_are_from_the_bars_given():
    bars = stock_bars()
    sn = snapshot(bars, 20, TB, "NEUTRAL", 2.0, "RANGE")
    assert sn.close == bars[-1].close and sn.support == min(b.low for b in bars[-20:])
    assert sn.low_52w == min(b.low for b in bars[-253:-1]) and sn.high_52w == max(b.high for b in bars[-253:-1])
    assert sn.sma_long == pytest.approx(sum(b.close for b in bars[-200:]) / 200)
    short = snapshot(bars[-100:], 20, TB)
    assert short.sma_long is None and short.low_52w is None and short.support is not None
    assert snapshot([], 20, TB) is None


def test_ACCEPTANCE_an_alert_says_what_changed_and_shows_exit_and_roll_with_numbers():
    from tests.test_phase9_core import HELD_ROLL, LOT_R, big_settings, roll_ctx
    cfg = big_settings()
    strat = build_stock_selling(cfg)
    c = roll_ctx(cfg, stock_bars())
    prev, now = snap(), snap(date(2026, 11, 13), close=1079.0, vix_spike=True)
    alerts = position_alerts(strat, c, HELD_ROLL, 0.8, prev, now)
    assert [a["kind"] for a in alerts] == ["BELOW_SUPPORT", "VIX_SPIKE"]
    for a in alerts:
        assert a["underlying"] == "RELIANCE" and "bull put spread 1110/1100" in a["position"] and a["what_changed"]
        assert a["exit"]["cost_to_close_inr"] == 0.8 * LOT_R and a["action"] == "ROLL"
        assert a["roll"] is not None and unhedged_short_legs(a["roll"]["legs"]) == [] and a["roll"]["new_max_loss_inr"] > 0
        assert a["roll"]["roll_net_credit_inr"] > 0 and "roll_signal" not in a
    assert position_alerts(strat, c, HELD_ROLL, 0.8, prev, snap(date(2026, 11, 13))) == []     # nothing changed: no alert
    blind = position_alerts(strat, c, HELD_ROLL, None, prev, now)
    assert blind[0]["exit"] is None and "cannot be priced" in blind[0]["roll_note"]
    # roll inputs unknown (a leg of the open spread has no quote): the exit guidance is still there
    gone = position_alerts(strat, roll_ctx(cfg, stock_bars(), drop={(1100.0, "PE")}), HELD_ROLL, 0.8, prev, now)
    assert gone[0]["action"] == "EXIT" and gone[0]["exit"]["cost_to_close_inr"] == 0.8 * LOT_R and gone[0]["roll"] is None


# ---------------- in the backtest: each rule and alert against the baseline ----------------
@pytest.fixture(scope="module")
def world():
    """The fixture stock falls 60 on 20 May 2025 (so there are confirmed pivot highs above the price for
    call spreads), then jumps +16 on 1 July 2025 (a close above resistance while a call spread is open)
    and +70 on 1 August 2025 (a call spread is stopped)."""
    return stock_world(shocks={date(2025, 5, 20): -60.0, date(2025, 7, 1): 16.0, date(2025, 8, 1): 70.0})


def stream(world, st, name, mode="daily"):
    cfg, store, base, _ = world
    hist = OptionHistory(store, "sim")
    eng = engine_for(cfg, store, base, hist)
    eng.set_expiry_floor(date(2025, 3, 1))
    sessions = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    sk = Counter()
    return eng.run_stream(st, "RELIANCE", sessions, name, sk, exit_check=mode), sk


def test_backtest_trend_block_on_the_baseline_blocks_calls_in_the_rising_fixture_stock(world):
    cfg = world[0]
    plain, _ = stream(world, variant(cfg), "baseline:CE")
    blocked, sk = stream(world, variant(cfg, baseline_rules=("trend_block",)), "baseline:CE")
    assert plain and blocked == [] and sk["NO_SIGNAL:UPTREND_BLOCK"] >= len(plain)   # every scheduled call spread blocked
    puts, sk2 = stream(world, variant(cfg, baseline_rules=("trend_block",)), "baseline:PE")
    assert puts and sk2["NO_SIGNAL:DOWNTREND_BLOCK"] == 0             # the stock is rising: puts are not blocked


def test_backtest_campaign_limit_stops_new_entries_after_a_stopped_trade(world):
    cfg = world[0]
    one = settings(backtest=cfg.backtest.model_dump(), options=cfg.options.model_dump(),
                   stock_option_selling={"campaign_limit": {**CL.model_dump(), "stock_loss_max_stops": 1,
                                                                "stock_cooloff_days": 60}})
    plain, _ = stream(world, variant(one), "baseline:CE")
    limited, sk = stream(world, variant(one, baseline_rules=("campaign_limit",)), "baseline:CE")
    stops = [t for t in plain if t.exit_reason == "STOP"]
    assert stops, "the fixture needs a stopped call spread"
    assert sk["NO_SIGNAL:STOCK_LOSS_LIMIT"] >= 1 and len(limited) < len(plain)
    for a in limited:
        if a.exit_reason != "STOP":
            continue
        until = ist_date(a.exit_time) + timedelta(days=CL.stock_cooloff_days)
        assert not [b for b in limited if ist_date(a.exit_time) <= ist_date(b.signal_time) < until]


def test_backtest_alert_exit_closes_at_the_next_open_after_the_alert(world):
    cfg, _, base, _ = world
    plain, _ = stream(world, variant(cfg), "baseline:CE")
    kindset = ALERT_SETS["close beyond support / resistance"]
    alerted, _ = stream(world, variant(cfg, alert_exits=kindset), "baseline:CE")
    hits = [t for t in alerted if t.exit_reason == "ALERT_ABOVE_RESISTANCE"]
    assert hits, "the rising fixture stock closes above resistance while a call spread is open"
    bars = base.daily("RELIANCE")
    dates = [ist_date(b.start.isoformat()) for b in bars]
    for t in hits:
        exit_d = ist_date(t.exit_time)
        i = dates.index(exit_d) - 1                                   # the session whose close raised the alert
        resistance_before = support_resistance(bars[:i], 252, 5)[1]     # known at the previous close
        assert bars[i].close > resistance_before and bars[i - 1].close <= resistance_before
        assert datetime.fromisoformat(t.exit_time).astimezone(IST).hour == 9 and t.exit_basis == "alert at the close"
        assert dates[i] > ist_date(t.entry_time) or dates[i] == ist_date(t.entry_time)
    assert all(not t.exit_reason.startswith("ALERT_") for t in plain)
    puts, _ = stream(world, variant(cfg, alert_exits=kindset), "baseline:PE")
    assert all(t.exit_reason != "ALERT_ABOVE_RESISTANCE" for t in puts)      # that alert is not against a put spread


def test_ACCEPTANCE_report_shows_the_effect_of_each_rule_and_alert_against_the_baseline(world):
    cfg, store, base, _ = world
    res = run_bt(world)
    hist = OptionHistory(store, "sim")
    eng = engine_for(cfg, store, base, hist)
    eng.set_expiry_floor(date(2025, 3, 1))
    eng.last_session = date(2025, 9, 30)
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]

    def split(mode):
        sk = Counter()
        st = build_stock_selling(cfg)
        strat_t = eng.run_stream(st, "RELIANCE", dev, "strategy", sk, exit_check=mode)
        bsk = Counter()
        base_t = [t for v in ("baseline:PE", "baseline:CE") for t in eng.run_stream(st, "RELIANCE", dev, v, bsk, exit_check=mode)]
        return (strat_t, base_t, sk, bsk, [])

    out = rule_effects(cfg, eng, ["RELIANCE"], dev, {"intraday": split("intraday")})
    rows = {r["label"]: r for r in out["rule_effects"]["rows"]}
    assert list(rows)[:12] == ["strategy: all rules", "strategy without the minimum net credit",
                               "strategy without the trend block", "strategy without the campaign limit",
                               "strategy without the market block", "strategy without the portfolio limits",
                               "baseline: no rules", "baseline + trend block", "baseline + campaign limit",
                               "baseline + minimum net credit", "baseline + market block", "baseline + portfolio limits"]
    assert len(rows) == 12 + len(ALERT_SETS) + 1 and out["rule_effects"]["exit_check"] == "intraday"
    assert rows["strategy: all rules"]["net_pnl_inr"] == res["report"]["overall"]["net_pnl_inr"]
    assert rows["baseline: no rules"]["net_pnl_inr"] == res["report"]["baseline"]["overall"]["net_pnl_inr"]
    assert rows["baseline + trend block"]["trades"] < rows["baseline: no rules"]["trades"]
    assert rows["baseline + trend block"]["blocked"].get("UPTREND_BLOCK", 0) > 0
    assert rows["baseline + exit on alert: close beyond support / resistance"]["alert_exits"] > 0
    assert rows["baseline + exit on any alert"]["alert_exits"] >= rows["baseline + exit on alert: close beyond support / resistance"]["alert_exits"]
    assert rows["baseline + exit on alert: FII streak begins"]["alert_exits"] == 0       # no FII figures in the fixture: no alert
    cond = out["baseline_by_entry_condition"]
    assert set(cond) == {"stock state at entry", "past breach rate of a strike this far away", "IV rank at entry",
                         "FII bias at entry", "India VIX on the entry signal day"}
    for title, groups in cond.items():
        assert sum(g["trades"] for g in groups) == rows["baseline: no rules"]["trades"], title
    assert [g["label"] for g in cond["FII bias at entry"]] == ["no data"]
    # every backtest trade carries what was known at its signal
    t0 = next(t for t in res["trades"] if t["variant"] == "baseline:PE")
    ec = t0["entry_conditions"]
    assert ec["state"] in ("TRENDING_UP", "TRENDING_DOWN", "RANGING") and ec["short_strike_distance_pct"] > 0
    assert ec["breach_history_pct"] is not None and ec["fii_bias"] is None and ec["vix_spike"] in (True, False, None)
    assert "expected_move_inr" in ec and "short_strike_beyond_expected_move" in ec


# ---------------- MARKET_WEAK ----------------
def test_ACCEPTANCE_market_weak_blocks_put_selling_and_unknown_market_data_blocks_too(s):
    from app.options.stock_profile import MARKET_UNKNOWN, MARKET_WEAK, breadth_pct, market_weak
    MB = s.stock_option_selling.market_block
    bars = stock_bars()
    flat, falling = index_bars(bars), index_bars(bars, 24000.0, -10.0)
    assert market_weak(flat, 60.0, MB, 200) == (None, [])
    code, why = market_weak(falling, 60.0, MB, 200)                  # the index is below its 200-day average
    assert code == MARKET_WEAK and len(why) == 1 and "below its 200-day average" in why[0]
    code, why = market_weak(flat, 39.9, MB, 200)                     # breadth under 40%
    assert code == MARKET_WEAK and "39.9% of stocks" in why[0]
    assert market_weak(flat, 40.0, MB, 200)[0] is None               # at the minimum is not below it
    assert market_weak(flat, None, MB, 200)[0] == MARKET_UNKNOWN     # unknown breadth never passes
    assert market_weak(flat[:150], 60.0, MB, 200)[0] == MARKET_UNKNOWN
    # breadth by hand: 3 of 4 usable stocks above their 3-session average; a stock with too few closes is not usable
    rows = [[1, 2, 3], [3, 2, 4], [5, 5, 6], [9, 8, 7], [1]]
    assert breadth_pct(rows, 3, 80.0, 5) == 75.0 and breadth_pct(rows, 3, 90.0, 5) is None and breadth_pct([], 3, 80.0, 0) is None
    # strategy: a weak or unknown market gives no put signal; the block can be switched off; calls are not blocked
    strat = build_stock_selling(s)
    assert strat.evaluate(ctx_for(s, bars))
    for market, code in (({"breadth_pct": 30.0}, MARKET_WEAK), ({}, MARKET_UNKNOWN)):
        assert strat.evaluate(ctx_for(s, bars, market=market)) == [] and any(r.startswith(code) for r in strat.last_no_trade)
    assert strat.evaluate(ctx_for(s, bars, index_daily=falling)) == [] and any(r.startswith(MARKET_WEAK) for r in strat.last_no_trade)
    off = build_stock_selling(settings(stock_option_selling={"market_block": {**MB.model_dump(), "enabled": False}}))
    assert off.evaluate(ctx_for(s, bars, market={"breadth_pct": 30.0}))
    down = stock_bars(start=1200, drift=-1.0)
    calls = build_stock_selling(settings(stock_option_selling={"min_credit_to_max_loss": 0.05}))
    sig = calls.evaluate(ctx_for(s, down, chain(down[-1].close), market={"breadth_pct": 10.0}))
    assert sig and sig[0].details["side"] == "CE"                    # a known weak market: call selling goes ahead
    assert calls.evaluate(ctx_for(s, down, chain(down[-1].close), market={})) == []      # unknown: no call spread either
    assert any(r.startswith(MARKET_UNKNOWN) for r in calls.last_no_trade)
    # open-position guidance: MARKET_WEAK is reported for a put spread on the session it begins
    k = lambda prev, now, right="PE": [a["kind"] for a in alert_kinds(prev, now, right)]
    assert k(snap(market_weak=False), snap(date(2026, 11, 13), market_weak=True)) == ["MARKET_WEAK"]
    assert k(snap(market_weak=True), snap(date(2026, 11, 13), market_weak=True)) == []
    assert k(snap(market_weak=None), snap(date(2026, 11, 13), market_weak=True)) == []       # unknown before: not a known change
    assert k(snap(market_weak=False), snap(date(2026, 11, 13), market_weak=True), "CE") == []


def test_backtest_market_block_uses_breadth_known_at_each_close(world):
    cfg, store, base, _ = world
    eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
    d = date(2025, 7, 15)
    assert eng.breadth_at(d) == 100.0                                # the one fixture stock is above its 50-session average
    assert eng.breadth_at(date(2025, 7, 13)) is None                 # a Sunday: no stock traded, breadth unknown
    thin = settings(backtest=cfg.backtest.model_dump(), options=cfg.options.model_dump(),
                    stock_option_selling={"market_block": {"enabled": True, "market_breadth_min_pct": 40.0,
                                                           "min_coverage_pct": 80.0}})
    eng2 = engine_for(thin, store, base, OptionHistory(store, "sim"))
    assert eng2.breadth_at(d) is None                                # 1 of 5 watchlist stocks has data: under 80% coverage
    blocked, sk = [], Counter()
    eng2.set_expiry_floor(date(2025, 3, 1))
    sessions = [x for x in base.session_dates() if date(2025, 6, 2) <= x < date(2025, 10, 1)]
    blocked = eng2.run_stream(variant(thin, baseline_rules=("market_block",)), "RELIANCE", sessions, "baseline:PE", sk)
    assert blocked == [] and sk["NO_SIGNAL:MARKET_UNKNOWN"] >= 3     # unknown breadth blocks every scheduled put spread
    # review fix 2026-11-15: MARKET_UNKNOWN blocks call spreads too (this test used to assert they passed);
    # a KNOWN weak market still blocks put selling only (test above: a CE signal with breadth 10%)
    ck = Counter()
    calls = eng2.run_stream(variant(thin, baseline_rules=("market_block",)), "RELIANCE", sessions, "baseline:CE", ck)
    assert calls == [] and ck["NO_SIGNAL:MARKET_UNKNOWN"] >= 3
    eng.set_expiry_floor(date(2025, 3, 1))
    known = eng.run_stream(variant(cfg, baseline_rules=("market_block",)), "RELIANCE", sessions, "baseline:CE", Counter())
    assert known                                                     # breadth known: call spreads are not blocked


def test_ACCEPTANCE_support_is_a_confirmed_pivot_used_only_after_its_five_later_sessions():
    """A pivot low is not support on its own day nor in the four sessions after it: only from the fifth."""
    lows = [110, 109, 108, 107, 106, 100, 104, 105, 106, 107, 108, 109, 110]
    bars = [Bar("X", daily_start(d), lo + 2, lo + 4, lo, lo + 2, 1.0) for d, lo in zip(weekdays_back(END, len(lows)), lows)]
    for k in range(5, 10):                                           # the low is at index 5; bars up to k are known
        assert support_resistance(bars[:k + 1], 10, 5)[0] is None, k
    assert support_resistance(bars[:11], 10, 5)[0] == 100            # confirmed at index 10
    # the profile uses the same confirmed pivots for its swings
    wave = series([1000 * (1 + 0.2 * math.sin(i / 20)) for i in range(700)])
    w = range_speed_profile(wave, PR, pivot_bars=5)["windows"]["2"]
    assert w["upswings"] >= 3 and 50 <= w["avg_sessions_low_to_high"] <= 70
