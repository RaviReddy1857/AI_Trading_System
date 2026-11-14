"""Phase 9 M2: the 0-100 candidate score, its factors, and the score bands of the backtest."""
import math
from collections import Counter
from datetime import date, timedelta

import pytest

from app.options.option_history import OptionHistory
from app.options.score import (FACTORS, fall_percentile, iv_falling_points, liquidity_points, range_profile_points,
                               rel_strength_points, score, strike_distance_points)
from app.options.stock_backtest import band_of, daily_bands, factor_bin, scan_candidates, score_reports
from app.options.stock_selling import build_stock_selling
from tests.test_phase9_core import (BT_FIRST, ctx_for, engine_for, ist_date, run_bt, settings, stock_bars, stock_world)

EQUAL = {k: 1.0 for k in FACTORS}


def test_each_factor_maps_to_points_as_documented():
    # IV falling: 100 at or below 90% of the 20-session peak, 0 at the peak, linear between
    assert iv_falling_points(18.0, [20.0] * 20, 0.9) == 100.0
    assert iv_falling_points(20.0, [20.0] * 20, 0.9) == 0.0
    assert iv_falling_points(19.0, [20.0] * 20, 0.9) == pytest.approx(50.0)
    assert iv_falling_points(22.0, [20.0] * 20, 0.9) == 0.0         # above the old peak: rising, no points
    assert iv_falling_points(None, [20.0], 0.9) is None and iv_falling_points(18.0, [], 0.9) is None
    # relative strength: 50 + 5 per point ahead (puts), the mirror for calls; index and sector peers averaged
    assert rel_strength_points(8.0, 2.0, (), "PE") == pytest.approx(80.0)
    assert rel_strength_points(8.0, 2.0, (), "CE") == pytest.approx(20.0)
    assert rel_strength_points(8.0, 2.0, (4.0, 6.0, 10.0), "PE") == pytest.approx(50 + 5 * ((6 + 2) / 2))
    assert rel_strength_points(30.0, 0.0, (), "PE") == 100.0 and rel_strength_points(None, 1.0, (), "PE") is None
    # strike distance: 50 points per yardstick at 1.5x or more of it, linearly from 0
    assert strike_distance_points(6.0, 4.0, 4.0) == pytest.approx(100.0)
    assert strike_distance_points(3.0, 4.0, 6.0) == pytest.approx(50 * (0.75 / 1.5) + 50 * (0.5 / 1.5))
    assert strike_distance_points(3.0, 4.0, -1.0) == pytest.approx(25.0 + 50.0)   # never fell over 21 sessions: full half
    assert strike_distance_points(3.0, None, 6.0) is None and strike_distance_points(3.0, 4.0, None) is None
    # range profile: 0% breached 100 points, 50% or more 0
    assert range_profile_points(0.0) == 100.0 and range_profile_points(25.0) == 50.0 and range_profile_points(60.0) == 0.0
    assert range_profile_points(None) is None
    # liquidity: log scale, the minimum 0 points, 10x the minimum 100
    assert liquidity_points(200, 200) == 0.0 and liquidity_points(2000, 200) == pytest.approx(100.0)
    assert liquidity_points(632.46, 200) == pytest.approx(50.0, abs=0.01) and liquidity_points(None, 200) is None


def test_score_is_the_weighted_mean_with_contributions_and_unknown_is_never_zero():
    pts = {"iv_rank": 60, "iv_falling": 30, "rel_strength": 90, "strike_distance": 75, "range_profile": 45, "liquidity": 0}
    s = score(pts, EQUAL)
    assert s["total"] == pytest.approx(50.0) and s["complete"] is True and s["missing"] == []
    assert s["contributions"]["rel_strength"] == pytest.approx(15.0) and sum(s["contributions"].values()) == pytest.approx(50.0)
    w = {**EQUAL, "iv_rank": 4.0}
    assert score(pts, w)["total"] == pytest.approx((60 * 4 + 30 + 90 + 75 + 45 + 0) / 9, abs=0.01)
    s = score({**pts, "iv_falling": None}, EQUAL)
    assert s["total"] is None and s["complete"] is False and s["missing"] == ["iv_falling"]
    assert s["contributions"]["iv_falling"] is None and s["factors"]["iv_falling"] is None
    assert score({**pts, "iv_falling": None}, {**EQUAL, "iv_falling": 0.0})["total"] is not None   # a weight of 0 is not needed


def test_fall_percentile_uses_only_completed_windows():
    closes = [100.0] * 30 + [100 - i for i in range(30)] + [70.0] * 30
    p = fall_percentile(closes, 21, 90, "PE")
    assert p is not None and p > 15                                  # the 30-point slide shows up as large falls
    assert fall_percentile(closes[:40], 21, 90, "PE") is None        # fewer than 30 windows: unknown
    rising = [100 + i for i in range(80)]
    assert fall_percentile(rising, 21, 90, "PE") < 0 and fall_percentile(rising, 21, 90, "CE") > 0


def test_every_signal_carries_its_score_from_decision_time_inputs():
    cfg = settings()
    st = build_stock_selling(cfg)
    sig = st.evaluate(ctx_for(cfg, stock_bars()))[0]
    sc = sig.details["score"]
    assert set(sc["factors"]) == set(FACTORS) and sc["complete"] is True and 0 <= sc["total"] <= 100
    assert sig.details["entry_conditions"]["score"] == sc["total"]
    assert sc["factors"]["iv_rank"] == 80.0                          # ctx_for's IV rank
    # changing an input that is not known at the decision does nothing: the score reads ctx only
    from dataclasses import replace
    c = ctx_for(cfg, stock_bars())
    lower = st.evaluate(replace(c, market={**c.market, "iv_window": (c.iv_reference_pct,) * 20}))[0].details["score"]
    assert lower["factors"]["iv_falling"] == 0.0 and lower["total"] < sc["total"]   # IV at its peak scores nothing
    no_iv = st.evaluate(replace(c, market={**c.market, "iv_window": ()}))[0].details["score"]
    assert no_iv["total"] is None and no_iv["missing"] == ["iv_falling"]


def test_ACCEPTANCE_bands_are_thirds_of_each_days_candidates_with_ties_and_small_days_documented():
    assert [band_of(k, 9) for k in range(9)] == ["top"] * 3 + ["middle"] * 3 + ["bottom"] * 3
    assert [band_of(k, 4) for k in range(4)] == ["top", "top", "middle", "bottom"]
    assert [band_of(k, 3) for k in range(3)] == ["top", "middle", "bottom"]
    assert band_of(0, 2) == band_of(1, 2) == band_of(0, 1) == "small cross-section"
    d1, d2 = date(2025, 7, 1), date(2025, 7, 2)
    rows = [{"stock": s, "date": d1, "expiry": "2025-07-29", "side": "PE", "score": sc}
            for s, sc in (("B", 70.0), ("A", 70.0), ("C", 50.0), ("D", None))]
    rows += [{"stock": "A", "date": d2, "expiry": "2025-07-29", "side": "PE", "score": 10.0}]
    b = daily_bands(rows)
    assert b[("A", d1, "2025-07-29", "PE")] == "top" and b[("B", d1, "2025-07-29", "PE")] == "middle"   # tie: by name
    assert b[("C", d1, "2025-07-29", "PE")] == "bottom" and b[("D", d1, "2025-07-29", "PE")] == "score incomplete"
    assert b[("A", d2, "2025-07-29", "PE")] == "small cross-section"                  # a different day ranks on its own
    assert factor_bin(None) == "unknown" and factor_bin(10) == "low (under 33.3)" and factor_bin(50) == "middle"
    assert factor_bin(70) == "high (66.7 or more)"


@pytest.fixture(scope="module")
def world():
    return stock_world()


def test_ACCEPTANCE_backtest_reports_score_bands_and_each_factor_against_the_baseline(world):
    cfg, store, base, _ = world
    eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
    eng.set_expiry_floor(BT_FIRST)
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    eng.last_session = dev[-1]
    res = run_bt(world, exit_checks=("daily",))
    from app.options.backtest import OptionTrade
    trades = [OptionTrade(**t) for t in res["trades"]]
    base_trades = [t for t in trades if t.variant != "strategy"]
    out = score_reports(cfg, eng, ["RELIANCE"], dev, {"daily": ([t for t in trades if t.variant == "strategy"],
                                                                 base_trades, Counter(), Counter(), [])})
    sb = out["score_bands"]
    assert sb["exit_check"] == "daily" and "ties" in sb["rule"] and sb["unmatched"] == 0
    assert sum(r["trades"] for r in sb["bands"]) >= 1
    assert {r["band"] for r in sb["bands"]} <= {"top", "middle", "bottom", "small cross-section", "score incomplete"}
    assert all(r["low_sample"] for r in sb["bands"])                 # a fixture of one stock: every band is small
    # one stock: never 3 candidates on a day, so a scored trade is in a small cross-section, never a third
    assert {r["band"] for r in sb["bands"]} <= {"small cross-section", "score incomplete"}
    fac = out["baseline_by_score_factor"]["factors"]
    assert set(fac) == {"total", *FACTORS}
    for f, rows in fac.items():
        assert sum(r["trades"] + r["open_at_cutoff"] for r in rows) == len(base_trades), f   # closed plus open at the cutoff
    assert {r["bin"] for r in fac["iv_rank"]} <= {"low (under 33.3)", "middle", "high (66.7 or more)", "unknown"}
    # the candidates scan reads no outcome: the same rows whatever happened after
    scanned = scan_candidates(cfg, eng, ["RELIANCE"], dev)
    assert scanned and all(r["score"] is None or 0 <= r["score"] <= 100 for r in scanned)
    assert scan_candidates(cfg, eng, ["RELIANCE"], dev) == scanned


def test_shortlist_ranks_by_score_and_puts_incomplete_scores_last():
    from dataclasses import replace
    from app.options.stock_selling import build_shortlist
    from tests.test_phase9_core import shortlist_contexts
    cfg = settings()
    ctxs = shortlist_contexts(cfg)
    ctxs[1] = replace(ctxs[1], market={**ctxs[1].market, "iv_window": ()})    # TCS: IV history unknown
    rows = build_shortlist(build_stock_selling(cfg), ctxs)
    top = [r for r in rows if r["status"] == "SHORTLISTED"]
    assert [r["stock"] for r in top] == ["RELIANCE", "TCS"] and top[1]["score"] is None
    assert top[1]["score_missing"] == ["iv_falling"]
