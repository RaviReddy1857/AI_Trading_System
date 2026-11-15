"""Pre-registered data-quality exclusion of stock-years (app/data_quality.py)."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from app.config import Settings
from app.data_quality import ENTRY_MARGIN_DAYS, assess, excluded_list, excluded_sessions
from app.history import daily_start
from app.models import Bar
from app.store import Store
from tests.test_step1 import sim_settings

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


def days_of(year):
    d, out = date(year, 1, 1), []
    while d.year == year:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def cfg(**dq):
    raw = sim_settings().model_dump()
    raw["data_quality"] = {**raw["data_quality"], **dq}
    return Settings.model_validate(raw)


def world():
    store = Store(":memory:")
    bars = []
    for y in (2023, 2024, 2025):
        for k, d in enumerate(days_of(y)):
            p = 100.0
            if y == 2023 and k == 100:
                p = 125.0                      # a one-day +25% bar with no corporate action
            if y == 2024 and k >= 150:
                p = 50.0                       # a halving explained by a split on that day
            if y == 2025:
                p = 50.0
            bars.append(Bar("X", daily_start(d), p, p, p, p, 1.0))
    store.save_history_bars("bars_1d", bars, "breeze")
    split_day = days_of(2024)[150]
    store.save_corporate_actions([{"symbol": "X", "ex_date": split_day.isoformat(), "action_type": "SPLIT", "purpose": "split",
                                   "record_date": None, "announced_on": None, "ratio": "2:1", "price_factor": 0.5,
                                   "factor_basis": "split ratio", "source": "t", "source_ref": "t", "imported_utc": NOW.isoformat()}])
    # unexplained mismatches on 14 of 2025's sessions (over 5%), and one explained one
    rows = [("X", d.isoformat(), 50.0, 52.0, 4.0, None, "t", NOW.isoformat()) for d in days_of(2025)[:14]]
    rows.append(("X", days_of(2025)[20].isoformat(), 50.0, 25.0, -50.0, "SPLIT 2026-01-01", "t", NOW.isoformat()))
    store.save_crosscheck(rows)
    return store


def test_ACCEPTANCE_the_pre_registered_rule_excludes_jumps_without_an_action_and_too_many_unexplained_mismatches():
    store = world()
    a = assess(cfg(), store, ["X"])
    assert a[("X", 2023)]["excluded"] and "one-day jump" in a[("X", 2023)]["reasons"][0]
    assert len(a[("X", 2023)]["jumps"]) == 1                         # up 25% counts; back down exactly 20% is not over 20%
    assert not a[("X", 2024)]["excluded"]                            # the halving is the split's ex-date
    assert a[("X", 2025)]["excluded"] and a[("X", 2025)]["unexplained"] == 14     # the explained one is not counted
    assert a[("X", 2025)]["unexplained_pct"] > 5
    # exactly at the limits is not over them
    assert not assess(cfg(max_unexplained_pct=14 / 261 * 100 + 0.01), store, ["X"])[("X", 2025)]["excluded"]
    # resolved stock-years are kept (the resolution is recorded in docs/STATUS.md)
    r = assess(cfg(resolved_stock_years=["X:2023"]), store, ["X"])
    assert not r[("X", 2023)]["excluded"] and r[("X", 2023)]["resolved"] and r[("X", 2023)]["reasons"]
    assert [(x["stock"], x["year"]) for x in excluded_list(a)] == [("X", 2023), ("X", 2025)]


def test_no_entry_in_an_excluded_year_or_the_days_before_it():
    store = world()
    a = assess(cfg(), store, ["X"])
    sessions = days_of(2023) + days_of(2024) + days_of(2025)
    bad = excluded_sessions(a, sessions)["X"]
    assert set(days_of(2023)) <= bad and set(days_of(2025)) <= bad
    assert date(2024, 12, 31) in bad and date(2024, 12, 31) - timedelta(days=ENTRY_MARGIN_DAYS + 5) not in bad
    assert date(2024, 6, 3) not in bad


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
