"""Phase 9 M5 review fixes (Codex review of 968d410..267f09b, 2026-11-16).

1. Outcome windows are counted in MARKET sessions (the holiday calendar), need a price on every
   session in the window and no unadjusted price break inside it; otherwise the window is excluded,
   by reason - for the confirmations and both baselines alike.
2. every_session includes the confirmation dates (they are left out of washed_out_not_confirmed only).
3. Scenario triggers read FII rows and VIX bars inside [floor, as-of date] only.
4. For every group and horizon: eligible starts, completed cases and exclusions by reason, adding up.
Each test fails on the code of 267f09b.
"""
from datetime import date, timedelta
from types import SimpleNamespace

from app.calendar import MarketCalendar
from app.history import daily_start
from app.indicators import gap_breaks
from app.models import Bar
from app.options.reversal import (MISSING_SESSION, PAST_PERIOD, PRICE_BREAK, ReversalDay, ReversalScanner,
                                  market_sessions, reversal_outcomes)
from app.options.shortlist import scenario_triggers
from app.store import Store
from tests.test_phase9_core import settings, weekdays_back
from tests.test_phase9_m4 import vix_store

CFG = settings()


def bars_of(days, closes, sym="X"):
    return [Bar(sym, daily_start(d), c, c + 1, c - 1, c, 1e6) for d, c in zip(days, closes)]


def fake(days, closes, breaks=(), washed=(), confirmed=None, base_low=None):
    states = [ReversalDay(washed_out=i in washed or i == confirmed, confirmed=i == confirmed,
                          base_low=base_low if i == confirmed else None) for i in range(len(days))]
    return SimpleNamespace(days=states, dates=list(days), bars=bars_of(days, closes), breaks=tuple(breaks),
                           cfg=SimpleNamespace(high_lookback_sessions=3))


def every(out, h="2"):
    return out["windows"]["every_session"][h]


# ---------------- 1. price breaks, missing sessions, the holiday calendar ----------------
def test_ACCEPTANCE_a_window_across_an_unadjusted_2_for_1_split_is_excluded():
    """Bars 0-4 at 200, a 2-for-1 split before bar 5 (opens at 100, unadjusted), bars 5-9 at 100.
    Horizon 2: windows starting at 3 and 4 end after the split; before the fix they read -50%."""
    days = weekdays_back(date(2026, 3, 13), 10)
    closes = [200] * 5 + [100] * 5
    bars = bars_of(days, closes)
    breaks = gap_breaks([b.open for b in bars], [b.close for b in bars], CFG.history.max_overnight_gap_pct)
    assert breaks == [5]
    sc = ReversalScanner(bars, bars_of(days, [24000] * 10, "NIFTY"), CFG.stock_option_selling.reversal, 200, breaks)
    assert sc.breaks == (5,)                                     # the scanner keeps its breaks for the study
    out = reversal_outcomes({"X": sc}, days, [2], 2)
    r = out["baselines"]["every_session"]["outcomes"][0]
    assert r["cases"] == 6 and r["worst_pct"] == 0.0             # starts 0-2 and 5-7; no -50% "move"
    assert every(out) == {"eligible_starts": 10, "completed": 6, "excluded": {PRICE_BREAK: 2, PAST_PERIOD: 2}}
    # the new-low window (the longest horizon) obeys the same rule
    assert every(out, "new_low_within_2")["excluded"] == {PRICE_BREAK: 2, PAST_PERIOD: 2}


def test_ACCEPTANCE_a_window_with_a_missing_session_is_excluded_not_stretched():
    """The index trades every weekday; the stock has no bar on session 4. Before the fix the window
    from session 3 ran over bars 3 -> 5 (three sessions, not two) and the stock's close on session
    6 was read as two sessions after session 4's neighbour."""
    days = weekdays_back(date(2026, 3, 13), 10)
    closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
    keep = [i for i in range(10) if i != 4]
    sc = fake([days[i] for i in keep], [closes[i] for i in keep], washed=set(range(9)), confirmed=1, base_low=50)
    out = reversal_outcomes({"X": sc}, days, [2], 2)
    # starts: sessions 0,1,2,3,5,6,7,8,9 (9 bars). 2 and 3 cover session 4: excluded; 8 and 9 run past the end.
    assert every(out) == {"eligible_starts": 9, "completed": 5, "excluded": {MISSING_SESSION: 2, PAST_PERIOD: 2}}
    moves = sorted(round(x, 2) for x in [(102 / 100 - 1) * 100, (103 / 101 - 1) * 100, (107 / 105 - 1) * 100,
                                          (108 / 106 - 1) * 100, (109 / 107 - 1) * 100])
    assert out["baselines"]["every_session"]["outcomes"][0]["best_pct"] == moves[-1]
    # the confirmation (session 1) is measured the same way: 101 -> 103
    assert out["outcomes"][0]["cases"] == 1 and out["outcomes"][0]["median_pct"] == round((103 / 101 - 1) * 100, 2)
    assert out["windows"]["confirmations"]["2"] == {"eligible_starts": 1, "completed": 1, "excluded": {}}
    w = out["windows"]["washed_out_not_confirmed"]["2"]       # sessions 0,2,3,5,6,7,8,9 (1 is the confirmation)
    assert w == {"eligible_starts": 8, "completed": 4, "excluded": {MISSING_SESSION: 2, PAST_PERIOD: 2}}


def test_horizons_are_counted_in_market_sessions_from_the_holiday_calendar():
    """2026 is covered by the calendar. A holiday (no index bar, no stock bar) is not a session, so a
    window across it is whole; a calendar trading day the index and the stock both lack is a missing
    session, and the window is excluded."""
    days = weekdays_back(date(2026, 3, 13), 10)
    holiday, lost = days[4], days[7]
    cal = MarketCalendar({holiday: "a holiday", date(2026, 12, 25): "Christmas"})
    have = [d for d in days if d not in (holiday, lost)]
    sc = fake(have, [100 + i for i in range(len(have))])
    mkt, uncovered = market_sessions(have, cal)
    assert lost in mkt and holiday not in mkt and uncovered == []
    out = reversal_outcomes({"X": sc}, have, [2], 2, calendar=cal)
    # starts 0,1,2,3,5,6,8,9 (sessions without the holiday): 5 and 6 cover the lost day; 8 and 9 run past the end
    assert every(out) == {"eligible_starts": 8, "completed": 4, "excluded": {MISSING_SESSION: 2, PAST_PERIOD: 2}}
    # without a calendar the index's sessions are the market sessions: the lost day goes unnoticed
    assert every(reversal_outcomes({"X": sc}, have, [2], 2))["completed"] == 6
    # a year the calendar holds no holidays for is reported, and the index's sessions are used there
    old = weekdays_back(date(2025, 3, 14), 10)
    assert market_sessions(old, cal) == (old, [2025])
    note = reversal_outcomes({"X": fake(old, [100] * 10)}, old, [2], 2, calendar=cal)["market_sessions"]
    assert note["calendar_not_covering"] == [2025] and "2025" in note["note"]


# ---------------- 2. every_session includes the confirmations ----------------
def test_ACCEPTANCE_every_session_includes_the_confirmation_dates():
    days = weekdays_back(date(2026, 3, 13), 6)
    sc = fake(days, [100, 100, 100, 120, 120, 120], washed={0, 1, 3}, confirmed=1, base_low=90)
    out = reversal_outcomes({"X": sc}, days, [2], 2)
    assert out["confirmations"] == 1
    assert every(out)["eligible_starts"] == 6 and every(out)["completed"] == 4        # 0, 1, 2, 3
    assert out["windows"]["washed_out_not_confirmed"]["2"]["eligible_starts"] == 2    # 0, 3: not the confirmation
    assert out["baselines"]["every_session"]["outcomes"][0]["cases"] == 4


# ---------------- 4. eligible = completed + excluded, for every group and horizon ----------------
def test_every_group_and_horizon_reports_eligible_completed_and_excluded():
    days = weekdays_back(date(2026, 3, 13), 30)
    closes = [100 + (i % 7) for i in range(30)]
    keep = [i for i in range(30) if i not in (11, 20)]
    sc = fake([days[i] for i in keep], [closes[i] for i in keep], breaks=(15,), washed=set(range(0, 28, 2)),
              confirmed=5, base_low=90)
    out = reversal_outcomes({"X": sc}, days, [2, 5], 2)
    assert set(out["windows"]) == {"confirmations", "washed_out_not_confirmed", "every_session"}
    for g, by in out["windows"].items():
        assert set(by) == {"2", "5", "new_low_within_5"}
        for k, a in by.items():
            assert a["eligible_starts"] == a["completed"] + sum(a["excluded"].values())
            cases = (out["new_low_afterwards"] if g == "confirmations" else out["baselines"][g]["new_low_afterwards"]) \
                if k.startswith("new_low") else \
                next(r for r in (out["outcomes"] if g == "confirmations" else out["baselines"][g]["outcomes"])
                     if str(r["sessions"]) == k)
            assert a["completed"] == cases["cases"]
    assert out["windows"]["every_session"]["2"]["eligible_starts"] == 28
    assert set(out["windows"]["every_session"]["5"]["excluded"]) == {MISSING_SESSION, PRICE_BREAK, PAST_PERIOD}


def test_the_printout_shows_the_window_accounting(capsys):
    from app.options.stock_backtest import print_reversal
    days = weekdays_back(date(2026, 3, 13), 10)
    print_reversal(reversal_outcomes({"X": fake(days, [100] * 10, breaks=(5,))}, days, [2], 2))
    text = capsys.readouterr().out
    assert "eligible starts = completed + excluded" in text and PRICE_BREAK in text


# ---------------- 3. scenario triggers inside [floor, as-of date] ----------------
def fii_store(floor):
    store = Store(":memory:")
    days = weekdays_back(floor + timedelta(days=40), 60)
    store.save_history_bars("bars_1d", [Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None) for d in days], "sim")
    return store, days


def test_ACCEPTANCE_fii_selling_before_the_floor_makes_no_streak_on_the_floor_date():
    floor = date(2019, 1, 22)                                     # a Tuesday
    store, days = fii_store(floor)
    k = days.index(floor)
    # selling on the two sessions before the floor and on the floor: three in a row only with pre-floor rows
    store.replace_fii_dii([(d.isoformat(), -500.0, 100.0) for d in days[k - 2:k + 1]]
                          + [(d.isoformat(), 300.0, 100.0) for d in days[k + 1:k + 5]])
    assert CFG.bias.streak_days == 3
    got = scenario_triggers(CFG, store, "sim", "RELIANCE", floor, days[-1])["after an FII selling streak"]["dates"]
    assert floor not in (got or [])


def test_ACCEPTANCE_triggers_as_of_a_date_contain_nothing_later():
    floor = date(2019, 1, 22)
    store, days = fii_store(floor)
    asof = date(2019, 2, 1)
    later = [d for d in days if d > asof][:3]                     # a streak completed after the as-of date
    store.replace_fii_dii([(d.isoformat(), -500.0, 100.0) for d in later])
    t = scenario_triggers(CFG, store, "sim", "RELIANCE", floor, asof)
    assert not [d for d in (t["after an FII selling streak"]["dates"] or []) if d > asof]
    assert scenario_triggers(CFG, store, "sim", "RELIANCE", floor, days[-1])["after an FII selling streak"]["dates"] \
        == [later[-1]]                                            # the streak is seen once its date has come
    # India VIX: a spike after the as-of date is not a trigger as of that date
    vstore, vdays, spike_day = vix_store(floor)
    name = next(k for k in scenario_triggers(CFG, vstore, "sim", "RELIANCE", floor, vdays[-1]) if "VIX" in k)
    assert scenario_triggers(CFG, vstore, "sim", "RELIANCE", floor, vdays[-1])[name]["dates"] == [spike_day]
    before = vdays[vdays.index(spike_day) - 1]
    assert not [d for d in (scenario_triggers(CFG, vstore, "sim", "RELIANCE", floor, before)[name]["dates"] or [])
                if d > before]
    early = scenario_triggers(CFG, vstore, "sim", "RELIANCE", floor, asof)
    assert not [d for k, v in early.items() if v["dates"] for d in v["dates"] if d > asof]
