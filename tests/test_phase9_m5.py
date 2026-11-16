"""Phase 9 M5 gap (2026-11-16): the reversal outcome study against matching baselines.

The 10/20/40-session outcomes and the new-low share of every confirmed reversal are set against
baselines on the same stocks and the same tested sessions, measured exactly the same way:
- washed_out_not_confirmed (the matching baseline): every session the stock was washed out and no
  reversal was confirmed;
- every_session (a reference for the period's drift).
The definition was written before any result. The test fails on the code before the change (no
baselines in the output).
"""
from datetime import date
from types import SimpleNamespace

import pytest

from app.history import daily_start
from app.models import Bar
from app.options.reversal import BASELINES, ReversalDay, reversal_outcomes
from tests.test_phase9_core import weekdays_back

CLOSES = [100, 100, 100, 110, 105, 99, 105, 105, 105, 105]
LOWS = [99, 99, 99, 109, 104, 98, 104, 104, 104, 104]
WASHED = {0, 1, 3, 4}
CONFIRMED = 2


def fake_scanner():
    days = weekdays_back(date(2026, 3, 13), len(CLOSES))
    bars = [Bar("X", daily_start(d), c, c + 1, lo, c, 1e6) for d, c, lo in zip(days, CLOSES, LOWS)]
    states = [ReversalDay(washed_out=i in WASHED or i == CONFIRMED, confirmed=i == CONFIRMED,
                          base_low=98.5 if i == CONFIRMED else None) for i in range(len(CLOSES))]
    return SimpleNamespace(days=states, dates=days, bars=bars, cfg=SimpleNamespace(high_lookback_sessions=3)), days


def test_ACCEPTANCE_reversal_outcomes_against_matching_baselines_by_hand():
    """Horizon 2 sessions; a case needs i + 2 inside the period (sessions 0-7 start one).
    Confirmation, session 2: 100 -> 105 (session 4) = +5%; later lows 109, 104 stay above its base low 98.5.
    Matching baseline (washed out, not confirmed: sessions 0, 1, 3, 4):
      0: 100 -> 100 = 0%    1: 100 -> 110 = +10%    3: 110 -> 99 = -10%    4: 105 -> 105 = 0%
      median 0%, up in 1 of 4 = 25%.
      New low against that day's 3-session low: 0 (ref 99; later 99, 99: no), 1 (99; 99, 109: no),
      3 (99; 104, 98: yes), 4 (99; 98, 104: yes) = 2 of 4 = 50%.
    Every session (all but the confirmation, 0-7): adds 5: 99 -> 105 = +6.06%, 6: 0%, 7: 0%;
      median 0% of 7, new low 2 of 7 = 28.6%.
    Confirmation minus matching: median +5 points, up-share +75 points, new-low share -50 points;
    one confirmation is LOW SAMPLE (min 2), so the comparison is LOW SAMPLE."""
    sc, days = fake_scanner()
    out = reversal_outcomes({"X": sc}, days, [2], 2)
    assert out["confirmations"] == 1 and out["outcomes"][0]["median_pct"] == 5.0
    assert out["new_low_afterwards"]["share_pct"] == 0.0
    m = out["baselines"]["washed_out_not_confirmed"]
    assert m["what"] == BASELINES["washed_out_not_confirmed"] and "matching baseline" in m["what"]
    r = m["outcomes"][0]
    assert (r["cases"], r["median_pct"], r["up_pct_of_cases"], r["worst_pct"], r["best_pct"]) == (4, 0.0, 25.0, -10.0, 10.0)
    assert r["low_sample"] is False
    assert m["new_low_afterwards"] == {"within_sessions": 2, "cases": 4, "share_pct": 50.0, "low_sample": False}
    e = out["baselines"]["every_session"]
    assert e["outcomes"][0]["cases"] == 7 and e["outcomes"][0]["median_pct"] == 0.0
    assert e["outcomes"][0]["best_pct"] == 10.0                                    # session 1: 100 -> 110
    assert e["new_low_afterwards"]["cases"] == 7 and e["new_low_afterwards"]["share_pct"] == 28.6
    vs = out["versus_matching_baseline"]
    assert vs["baseline"] == "washed_out_not_confirmed"
    assert vs["outcomes"] == [{"sessions": 2, "median_minus_baseline_pct": 5.0, "up_share_minus_baseline_pts": 75.0,
                               "cases": 1, "baseline_cases": 4, "low_sample": True}]
    assert vs["new_low_share_minus_baseline_pts"] == -50.0 and vs["new_low_low_sample"] is True
    assert out["label"] == "history, not a forecast"


def test_the_baselines_use_the_same_sessions_and_never_a_horizon_past_the_period():
    sc, days = fake_scanner()
    # the period ends at session 5: only starts 0-3 have a whole horizon; the confirmation (2) still counts
    out = reversal_outcomes({"X": sc}, days[:6], [2], 2)
    m = out["baselines"]["washed_out_not_confirmed"]["outcomes"][0]
    assert m["cases"] == 3 and out["baselines"]["every_session"]["outcomes"][0]["cases"] == 3       # 0, 1, 3
    # a period without the confirmation: no confirmation, baselines still measured, the difference unknown
    later = reversal_outcomes({"X": sc}, days[3:], [2], 2)
    assert later["confirmations"] == 0 and later["baselines"]["washed_out_not_confirmed"]["outcomes"][0]["cases"] == 2
    assert later["versus_matching_baseline"]["outcomes"][0]["median_minus_baseline_pct"] is None
    # a confirmation day is never in a baseline; a day that is not washed out is never in the matching one
    sc.days[CONFIRMED] = ReversalDay(washed_out=True, confirmed=False)
    again = reversal_outcomes({"X": sc}, days, [2], 2)
    assert again["baselines"]["washed_out_not_confirmed"]["outcomes"][0]["cases"] == 5
    assert again["baselines"]["every_session"]["outcomes"][0]["cases"] == 8


def test_the_report_page_and_the_printout_show_the_baselines(capsys):
    from app.options.stock_backtest import print_reversal
    from app.page import BACKTEST_HTML
    sc, days = fake_scanner()
    print_reversal(reversal_outcomes({"X": sc}, days, [2], 2))
    text = capsys.readouterr().out
    assert "matching baseline" in text and "confirmations minus the matching baseline" in text
    assert "revBase" in BACKTEST_HTML and "Against matching baselines" in BACKTEST_HTML
