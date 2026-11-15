"""Data-quality corrections and stock-year verdicts (pre-registered and amended 2026-11-15), price data only."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.corporate_actions import AdjustedHistory
from app.data_quality import (ENTRY_MARGIN_DAYS, RULE, corrections, excluded_sessions, repo_series, run, verdicts)
from app.history import bar_date, daily_start
from app.models import Bar
from app.store import Store
from tests.test_step1 import sim_settings

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)
SINCE = date(2019, 1, 22)


def days_between(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def cfg(**dq):
    raw = sim_settings().model_dump()
    raw["data_quality"] = {**raw["data_quality"], **dq}
    raw["backtest"] = {**raw["backtest"], "test_start": {"1day": SINCE}}
    return Settings.model_validate(raw)


def act(ex, t, factor=None, sym="X"):
    return {"symbol": sym, "ex_date": ex.isoformat(), "action_type": t, "purpose": t, "record_date": None, "announced_on": None,
            "ratio": None, "price_factor": factor, "factor_basis": "t", "source": "t", "source_ref": "t",
            "imported_utc": NOW.isoformat()}


def series(days, price_of):
    return [Bar("X", daily_start(d), price_of(i, d), price_of(i, d), price_of(i, d), price_of(i, d), 1.0)
            for i, d in enumerate(days)]


def repo_rows(days, price_of):
    return [{"date": d, "open": price_of(i, d) * 1.001, "high": price_of(i, d) * 1.01, "low": price_of(i, d) * 0.99,
             "close": price_of(i, d)} for i, d in enumerate(days)]


# ---------------- 1. exact corrections (the BSE pattern) ----------------
def test_ACCEPTANCE_a_breeze_stretch_not_adjusted_for_a_later_action_is_corrected_by_the_exact_factor():
    days = days_between(date(2019, 1, 1), date(2022, 12, 30))
    boundary, ex1, ex2 = date(2021, 8, 2), date(2022, 3, 21), date(2022, 11, 1)
    # the true price is 100 (in post-everything terms 100/9); Breeze: before the boundary raw for both bonuses (x9),
    # from the boundary already adjusted for the first (x3), raw for the second until its ex-date
    breeze = series(days, lambda i, d: 900.0 if d < boundary else (300.0 if d < ex2 else 100.0))
    repo = repo_rows(days, lambda i, d: 100.0)                      # the repo: adjusted for both
    acts = [act(ex1, "BONUS", 1 / 3), act(ex2, "BONUS", 1 / 3)]
    fx = {date.fromisoformat(d): m for d, m, _ in corrections(breeze, repo_series(repo), acts, 15.0)}
    assert set(fx) == {d for d in days if d < boundary} and all(m == pytest.approx(1 / 3) for m in fx.values())
    adj = AdjustedHistory(breeze, acts, 15.0, fx)
    # the second bonus is applied (Breeze shows its gap); the first is not (Breeze already reflects it)
    assert [x["factor"] is not None for x in adj.factors] == [False, True]
    w = adj.window(len(days) - 1, 10 ** 6)
    assert len(w) == len(days) and {round(b.close, 6) for b in w} == {100.0}     # one continuous series, no break
    assert breeze[0].close == 900.0                                             # the raw bars are not changed


def test_a_repo_filler_day_inside_a_corrected_stretch_gets_the_same_factor_and_fillers_are_left_out():
    days = days_between(date(2019, 1, 1), date(2019, 12, 31))
    ex = date(2019, 10, 1)
    breeze = series(days, lambda i, d: 1000.0 if d < date(2019, 6, 3) else 100.0)
    rows = repo_rows(days, lambda i, d: 100.0)
    k = 40
    rows[k] = {"date": days[k], "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}     # a filler bar
    repo = repo_series(rows)
    assert days[k] not in repo and days[k + 1] in repo
    fx = {date.fromisoformat(d): m for d, m, _ in corrections(breeze, repo, [act(ex, "SPLIT", 0.1)], 15.0)}
    assert fx[days[k]] == pytest.approx(0.1) and len(fx) == sum(1 for d in days if d < date(2019, 6, 3))


# ---------------- 2. jumps: confirmed by the repo = real ----------------
def test_ACCEPTANCE_a_jump_the_repo_confirms_keeps_the_year_an_unconfirmed_one_excludes_it():
    s = cfg()
    days = days_between(date(2020, 1, 1), date(2020, 12, 31))
    crash = days[50]
    price = lambda i, d: 100.0 if d < crash else 75.0                # -25% in one day
    breeze = series(days, price)
    same = repo_series(repo_rows(days, lambda i, d: 100.0 if d < crash else 77.0))          # -23%: within 5 points
    v = verdicts(s, breeze, same, [], {}, SINCE)[2020]
    assert v["jumps"][0]["confirmed"] and not v["reasons"] and "real moves" in v["explanation"]
    other = repo_series(repo_rows(days, lambda i, d: 100.0 if d < crash else 99.0))        # the repo did not move
    v = verdicts(s, breeze, other, [], {}, SINCE)[2020]
    assert not v["jumps"][0]["confirmed"] and "does not confirm" in v["reasons"][0]
    opposite = repo_series(repo_rows(days, lambda i, d: 100.0 if d < crash else 125.0))
    assert verdicts(s, breeze, opposite, [], {}, SINCE)[2020]["reasons"]
    assert verdicts(s, breeze, None, [], {}, SINCE)[2020]["reasons"]          # nothing to confirm with: excluded


# ---------------- 3. the smooth-ratio test ----------------
def test_ACCEPTANCE_a_ratio_that_steps_only_on_ex_dates_is_kept_an_erratic_one_is_excluded_volatile_days_are_noise():
    s = cfg()
    days = days_between(date(2022, 1, 3), date(2022, 12, 30))
    div = days[100]
    breeze = series(days, lambda i, d: 100.0 + i * 0.1)
    # the repo adjusts for a dividend: its closes before the ex-date are 2% lower (a constant ratio stepping once)
    repo = repo_series(repo_rows(days, lambda i, d: (100.0 + i * 0.1) * (0.98 if d < div else 1.0)))
    v = verdicts(s, breeze, repo, [act(div, "DIVIDEND")], {}, SINCE)[2022]
    assert v["unexplained"] > 0 and not v["reasons"] and "repo's own" in v["explanation"]
    # the same without the dividend on record: the step is on a day with no action -> erratic
    assert "erratic" in verdicts(s, breeze, repo, [], {}, SINCE)[2022]["reasons"][0]
    # a 3% ratio step on a day the stock moved 9%: noise on a volatile day, kept
    k = 150
    vol_b = series(days, lambda i, d: 100.0 * (1.09 if i >= k else 1.0))
    vol_r = repo_series(repo_rows(days, lambda i, d: 99.0 * (1.12 if i >= k else 1.0) if i >= 140 else 100.0 * (1.12 if i >= k else 1.0)))
    out = verdicts(s, vol_b, vol_r, [], {}, SINCE)[2022]
    assert out["noise_days"] and out["noise_days"][0]["date"] == days[k].isoformat()
    # small closing differences (under 2% a day) never exclude
    wobble = repo_series(repo_rows(days, lambda i, d: (100.0 + i * 0.1) * (1.004 if i % 2 else 0.997)))
    assert not verdicts(s, breeze, wobble, [], {}, SINCE)[2022]["reasons"]


def test_verdicts_are_stored_resolved_years_kept_and_entries_blocked_before_an_excluded_year():
    s = cfg(resolved_stock_years=["Y:2023"])
    store = Store(":memory:")
    days = days_between(date(2023, 1, 2), date(2024, 12, 31))
    bars = [Bar(sym, daily_start(d), p, p, p, p, 1.0) for sym in ("X", "Y")
            for i, d in enumerate(days) for p in [125.0 if i == 60 else 100.0]]
    store.save_history_bars("bars_1d", bars, "breeze")
    rows = run(s, store, ["X", "Y"], None, NOW)                     # no repo: the jump cannot be confirmed
    by = {(r["symbol"], r["year"]): r for r in rows}
    assert by[("X", 2023)]["excluded"] and not by[("X", 2024)]["excluded"]
    assert not by[("Y", 2023)]["excluded"] and by[("Y", 2023)]["detail"]["resolved"]
    assert all(r["rule"] == RULE for r in rows)
    bad = excluded_sessions(rows, days)["X"]
    assert set(d for d in days if d.year == 2023) <= bad and date(2024, 6, 3) not in bad
    late = excluded_sessions([{"symbol": "Z", "year": 2024, "excluded": True}], days)["Z"]
    assert date(2023, 12, 29) in late and date(2023, 12, 29) - timedelta(days=ENTRY_MARGIN_DAYS + 5) not in late


def test_ACCEPTANCE_the_exclusion_blocks_entries_for_the_strategy_and_the_baseline_alike():
    from app.options.option_history import OptionHistory
    from app.options.stock_backtest import variant
    from tests.test_phase9_core import engine_for, stock_world
    w = stock_world()
    c, store, base, _ = w
    eng = engine_for(c, store, base, OptionHistory(store, "sim"))
    eng.set_expiry_floor(date(2025, 3, 1))
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    st = variant(c)
    free = {v: eng.run_stream(st, "RELIANCE", dev, v, Counter()) for v in ("strategy", "baseline:PE")}
    assert free["strategy"] and free["baseline:PE"]
    eng.no_entry = {"RELIANCE": set(dev)}
    sk, log = Counter(), []
    for v in ("strategy", "baseline:PE"):
        assert eng.run_stream(st, "RELIANCE", dev, v, sk, log) == []
    assert sk["NO_SIGNAL:EXCLUDED_STOCK_YEAR"] >= 1 and any(r["reason"] == "EXCLUDED_STOCK_YEAR" for r in log)


# ---------------- the fast version-7 recheck ----------------
def test_ACCEPTANCE_plan_7_drops_only_the_marks_whose_daily_history_changed():
    from app.options.option_history import OptionHistory
    from app.options.stock_backtest import changed_stock_years, migrate_to_7
    from tests.test_phase9_core import engine_for, stock_world
    w = stock_world()
    c, store, base, _ = w
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    eng = engine_for(c, store, base, OptionHistory(store, "sim"))
    status = {"plan": 6, "complete": {"RELIANCE": [2025], "TCS": [2025]}}
    assert changed_stock_years(eng, status["complete"], dev) == {}          # nothing stored that changes the history
    # a correction of one bar inside the tested months changes RELIANCE 2025's windows
    d0 = dev[10]
    store.replace_price_corrections("RELIANCE", [(d0.isoformat(), 0.5, "test")], RULE, NOW)
    eng2 = engine_for(c, store, base, OptionHistory(store, "sim"))
    msgs = []
    changed = migrate_to_7(eng2, status, dev, msgs.append)
    assert changed == {"RELIANCE": [2025]} and status["complete"] == {"TCS": [2025]} and status["plan"] == 7
    assert status["plan7_rechecked"] == {"RELIANCE": [2025]} and "1 stock-year mark" in msgs[0]
