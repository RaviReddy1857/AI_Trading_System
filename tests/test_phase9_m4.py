"""Phase 9 M4 gaps (2026-11-16): range and speed profile, scenario history.

1. Every trigger date that is not a scenario case is counted, by reason, next to the cases (section 8.7:
   "exclusions counted"); the breach history counts the start days whose holding period has not ended.
2. The breach history a decision reads (the score's range_profile factor and the signal's
   breach_history_pct) uses no bar before the statistics floor (2019-01-22), at any decision date.
3. India VIX spikes are computed from VIX bars on or after the floor only: older Breeze VIX bars are
   wrong-scale (issue 30), and a spike measured against them is not a spike.
Each test fails on the code before these changes.
"""
import math
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.backtest import Engine
from app.calendar import MarketCalendar
from app.history import daily_start
from app.models import Bar
from app.options.score import candidate_score
from app.options.shortlist import scenario_triggers
from app.options.stock_profile import breach_frequency, scenario_history, stats_closes, vix_spike_dates
from app.store import Store
from tests.test_phase3 import FixedRegime
from tests.test_phase9_core import UTC, ist_date, settings, weekdays_back
from tests.two_stock_world import engine, two_world

CFG = settings()
PR = CFG.stock_option_selling.profile
END = date(2026, 11, 13)


def series(closes, end=END, sym="RELIANCE"):
    return [Bar(sym, daily_start(d), c, c + 5, c - 5, c, 1e6) for d, c in zip(weekdays_back(end, len(closes)), closes)]


# ---------------- 1. exclusions counted ----------------
def test_ACCEPTANCE_every_trigger_that_is_not_a_case_is_counted_by_reason():
    """400 sessions, +1 a session. Floor = session 4. Triggers, by hand:
    session 3 (before the floor); a date 20 days after the last bar (no stored session within 5 days);
    the Saturday before session 10 and session 10 itself (both start at session 10: counted once); the
    last session (its 30 days have not passed); sessions 30 and 50. 7 trigger dates: 3 cases (the
    Saturday, 30, 50), 4 not counted."""
    bars = series([1000.0 + i for i in range(400)])
    dates = [ist_date(b.start.isoformat()) for b in bars]
    saturday = max(d for d in (dates[10] - timedelta(days=k) for k in range(1, 7)) if d.weekday() == 5)
    assert saturday not in dates and dates[10] - saturday <= timedelta(days=5) and dates[9] < saturday
    trig = {"mixed": {"dates": [dates[3], dates[-1] + timedelta(days=20), dates[10], saturday, dates[-1], dates[30], dates[50]]}}
    row = scenario_history(bars, trig, PR, floor=dates[4])[0]
    # the Saturday maps to session 10, the first session on or after it; session 10 itself is then the duplicate
    assert row["cases"] == 3 and row["triggers"] == 7
    assert row["excluded"] == {"before the data floor": 1, "no stored session within 5 days": 1,
                               "same starting session as an earlier trigger": 1, "horizon not passed yet": 1}
    assert sum(row["excluded"].values()) + row["cases"] == row["triggers"]
    assert "4 of 7 trigger dates not counted" in row["note"] and "horizon not passed yet: 1" in row["note"]
    assert row["status"] == "LOW SAMPLE" and row["label"] == "history, not a forecast"
    # nothing excluded: nothing said
    clean = scenario_history(bars, {"x": {"dates": [dates[30]]}}, PR, floor=dates[4])[0]
    assert clean["excluded"] == {} and "not counted" not in clean["note"]
    # no case at all: the note still says why each trigger was left out
    none = scenario_history(bars, {"late": {"dates": [dates[-2], dates[-1]]}}, PR)[0]
    assert none["cases"] == 0 and none["excluded"] == {"horizon not passed yet": 2} and "2 of 2 trigger dates" in none["note"]
    # no data file: no trigger to count
    assert scenario_history(bars, {"n": {"dates": None, "missing": "no data"}}, PR)[0]["excluded"] == {}


def test_the_breach_history_counts_the_start_days_whose_holding_period_has_not_ended():
    closes = [100.0 + i for i in range(30)]
    b = breach_frequency(closes, 5.0, "PE", 20)
    assert b["cases"] == 10 and b["not_yet_complete"] == 20 and b["cases"] + b["not_yet_complete"] == len(closes)


# ---------------- 2. the decision-time breach history starts at the floor ----------------
def test_ACCEPTANCE_a_decision_in_2020_reads_no_breach_history_before_2019_01_22():
    """A decision on 2020-06-30 sees 600 daily bars, about 230 of them before 2019-01-22. The bars
    before the floor are wrong-scale (a tenth of the price, as Breeze's older history can be): read as
    history they look like a 90% fall and every strike 'was passed'. Only bars from the floor count."""
    floor = date(2019, 1, 22)
    days = weekdays_back(date(2020, 6, 30), 600)
    bars = [Bar("X", daily_start(d), px, px + 5, px - 5, px, 1e6)
            for d, px in ((d, (100.0 if d < floor else 1000.0 + 3 * math.sin(i / 7))) for i, d in enumerate(days))]
    stats_floor = daily_start(floor)
    got = stats_closes(bars, stats_floor)
    assert len(got) == sum(1 for d in days if d >= floor) and min(got) > 900          # nothing before the floor
    assert stats_closes(bars, None) == [b.close for b in bars[-504:]]                # no floor known: the last 504
    assert len(stats_closes(series([1000.0] * 800), daily_start(date(2019, 1, 22)))) == 504
    # the score's range_profile factor: with the floor the 4% put strike was never passed (100 points)
    sos = CFG.stock_option_selling
    ctx = SimpleNamespace(daily=tuple(bars), spot=1000.0, iv_reference_pct=None, iv_rank=50.0,
                          market={"stats_floor": stats_floor, "hist_fall_pct": {"PE": 5.0, "CE": 5.0}})
    pts = candidate_score(ctx, "PE", 960.0, 500.0, 30, sos.score, sos)["factors"]["range_profile"]
    assert pts == 100.0
    # the same decision with the wrong-scale years counted: the jump from 100 to 1000 is read as no
    # breach for a put, but a call 4% above is 'passed' on every early start day
    raw = breach_frequency([b.close for b in bars[-504:]], 4.0, "CE", sos.profile.holding_sessions)["breached_pct"]
    assert raw > 0 and breach_frequency(got, 4.0, "CE", sos.profile.holding_sessions)["breached_pct"] == 0.0
    call = candidate_score(ctx, "CE", 1040.0, 500.0, 30, sos.score, sos)["factors"]["range_profile"]
    assert call == 100.0


# ---------------- 3. India VIX spikes from post-floor VIX only ----------------
def vix_store(floor: date):
    """INDIAVIX at a tenth of a percent before the floor (wrong scale), about 15 from the floor on, with
    one real spike 300 sessions after the floor (15 -> 19.5, +30% in 5 sessions)."""
    store = Store(":memory:")
    days = weekdays_back(floor + timedelta(days=600), 900)
    after = [d for d in days if d >= floor]
    spike_day = after[300]
    rows = []
    for i, d in enumerate(days):
        if d < floor:
            v = 0.15 + 0.001 * (i % 2)
        else:
            k = after.index(d)
            v = 19.5 if 300 <= k < 303 else 15.0 + 0.1 * (k % 2)
        rows.append(Bar("INDIAVIX", daily_start(d), v, v, v, v, None))
        rows.append(Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None))
        rows.append(Bar("RELIANCE", daily_start(d), 1000, 1005, 995, 1000 + (i % 3), 1e6))
    store.save_history_bars("bars_1d", rows, "sim")
    return store, days, spike_day


def test_ACCEPTANCE_vix_spike_triggers_come_from_vix_bars_on_or_after_the_floor_only():
    floor = date(2019, 1, 22)
    store, days, spike_day = vix_store(floor)
    s = settings()
    name = next(k for k in scenario_triggers(s, store, "sim", "RELIANCE", floor, days[-1]) if "VIX" in k)
    got = scenario_triggers(s, store, "sim", "RELIANCE", floor, days[-1])[name]["dates"]
    # the jump from the wrong-scale 0.15 to 15 on the floor day is not a spike; the real one is
    assert got == [spike_day]
    # before the change the whole stored history was read: the floor's first session looked like a +10,000% rise
    every = store.history_bars("bars_1d", "INDIAVIX", "sim")
    first = next(d for d in days if d >= floor)
    assert vix_spike_dates(every, PR, floor)[0] == first and first not in got


def test_the_backtest_engine_reads_vix_spikes_from_the_floor_on_too():
    cfg, store, base = two_world()
    floor = cfg.backtest.test_start["1day"]
    old = [b for b in store.history_bars("bars_1d", "INDIAVIX", "sim") if ist_date(b.start.isoformat()) < floor]
    store.save_history_bars("bars_1d", [Bar("INDIAVIX", b.start, b.open / 100, b.high / 100, b.low / 100, b.close / 100, None)
                                        for b in old], "sim")                              # wrong-scale before the floor
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    eng = engine(cfg, store, base)
    first = next(d for d in base.session_dates() if d >= floor)
    # the floor's first session would be a +9,900% "rise" against the wrong-scale bars; with the floor it
    # is not known (no five sessions and no year of VIX history yet), and unknown raises no alert
    assert eng.market_at(first)["vix_spike"] is None


def test_the_page_shows_the_trigger_dates_not_counted_and_the_incomplete_start_days():
    from app.page import SHORTLIST_HTML
    assert "Trigger dates not counted" in SHORTLIST_HTML and "x.excluded" in SHORTLIST_HTML
    assert "not_yet_complete" in SHORTLIST_HTML
