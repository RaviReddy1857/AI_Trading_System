"""Event lists never say "none" without coverage: the pre-market plan and the signal cards' "why now" show
"EVENT COVERAGE MISSING: results dates unknown" when the week's results-date coverage (COVERAGE rows in events.csv,
retrieved within data_status.coverage_max_age_days, running to the week's Friday) is missing or incomplete."""
from __future__ import annotations

import pytest

from datetime import date, datetime, timezone

from app.cards import why_now
from app.manual_data import EVENT_COVERAGE_MISSING, week_coverage
from app.schedule import events_week_text, premarket_plan
from app.store import Store
from tests.phase6_fixtures import bull_put, cash_signal, new_journal
from tests.test_step1 import sim_settings

MON, TUE, FRI = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 20)
PRE = datetime(2026, 11, 17, 3, 31, tzinfo=timezone.utc)                    # Tue 09:01 IST


pytestmark = pytest.mark.usefixtures("fixed_clock")      # the app reads the clock: the fixtures' day


def cov(symbol, retrieved=MON, through=date(2026, 11, 30)):
    return (retrieved.isoformat(), "COVERAGE", symbol, f"source=NSE board meetings; covered_through={through.isoformat()}")


def stocks(s):
    return [i.symbol for i in s.watchlist if i.kind == "stock"]


def store_with(rows):
    st = Store(":memory:")
    st.replace_events(rows)
    return st


# ---------------- the pre-market plan ----------------
def test_plan_without_any_coverage_says_coverage_missing_not_none():
    s = sim_settings()
    j, _ = new_journal(s)
    plan = premarket_plan(s, store_with([]), j, None, PRE)
    line = next(x for x in plan.splitlines() if x.startswith("Events this week:"))
    assert line.startswith(f"Events this week: {EVENT_COVERAGE_MISSING}") and "none" not in line


def test_plan_with_incomplete_coverage_names_the_gaps_and_keeps_known_events():
    s = sim_settings()
    first, *rest = stocks(s)
    st = store_with([cov(x) for x in rest] + [("2026-11-19", "RBI_POLICY", "", "scheduled")])
    text = events_week_text(s, st, TUE, date(2026, 11, 22))
    assert text.startswith(EVENT_COVERAGE_MISSING) and f"1 of {len(rest) + 1} stock(s): {first}" in text
    assert text.endswith("known: 19-Nov RBI_POLICY")


def test_stale_or_short_coverage_counts_as_missing():
    s = sim_settings()
    old = store_with([cov(x, retrieved=date(2026, 11, 2)) for x in stocks(s)])          # 15 days before
    short = store_with([cov(x, through=date(2026, 11, 18)) for x in stocks(s)])          # ends before Friday
    for st in (old, short):
        assert events_week_text(s, st, TUE, date(2026, 11, 22)).startswith(EVENT_COVERAGE_MISSING)


def test_full_coverage_says_none_and_lists_events_without_coverage_rows():
    s = sim_settings()
    st = store_with([cov(x) for x in stocks(s)])
    assert events_week_text(s, st, TUE, date(2026, 11, 22)) == "none (results dates covered for every watchlist stock)"
    st = store_with([cov(x) for x in stocks(s)] + [("2026-11-18", "RESULTS", stocks(s)[0], "")])
    assert events_week_text(s, st, TUE, date(2026, 11, 22)) == f"18-Nov RESULTS {stocks(s)[0]}"
    plan = premarket_plan(s, st, new_journal(s)[0], None, PRE)
    assert EVENT_COVERAGE_MISSING not in plan and "COVERAGE" not in plan


# ---------------- signal cards ("why now") ----------------
def _cov(rows, symbols=("RELIANCE", "TCS"), extra=()):
    events = [{"date": date.fromisoformat(r[0]), "type": r[1], "symbol": r[2], "note": r[3]} for r in rows]
    return week_coverage(events, list(symbols), TUE, FRI, 7, extra=extra)


def _events_line(sig, coverage, events=()):
    return why_now(sig, "TRENDING_UP", "BULLISH", list(events), coverage)[-1]


def test_card_for_a_stock_follows_that_stocks_coverage():
    sig = cash_signal(symbol="RELIANCE")
    assert _events_line(sig, _cov([cov("RELIANCE")])) == "no event for this instrument or the market this week"
    line = _events_line(sig, _cov([cov("TCS")]), [{"date": "2026-11-19", "type": "RBI_POLICY", "symbol": ""}])
    assert line.startswith(EVENT_COVERAGE_MISSING) and "RELIANCE" in line and line.endswith("known: 2026-11-19 RBI_POLICY")


def test_card_for_an_index_needs_every_watchlist_stock_and_unknown_coverage_is_missing():
    sig = bull_put()                                                            # NIFTY, an index option
    assert _events_line(sig, _cov([cov("RELIANCE")])).startswith(EVENT_COVERAGE_MISSING)
    assert _events_line(sig, _cov([cov("RELIANCE"), cov("TCS")])) == "no event for this instrument or the market this week"
    assert _events_line(sig, None) == f"{EVENT_COVERAGE_MISSING} (coverage not checked)"


def test_stock_option_underlying_outside_the_watchlist_uses_its_own_coverage():
    sig = cash_signal(symbol="ABB")
    assert _events_line(sig, _cov([cov("ABB")], extra=["ABB"])) == "no event for this instrument or the market this week"
    assert _events_line(sig, _cov([], extra=["ABB"])).startswith(EVENT_COVERAGE_MISSING)


def test_engine_card_without_coverage_shows_it_missing(tmp_path):
    from datetime import timedelta
    from tests.test_phase6_engine import BAR_END, make, seed, tick
    c, eng = make(str(tmp_path / "cov.db"))
    with c:
        last = seed(c.app)
        tick(c.app, BAR_END - timedelta(seconds=1), last)
        out = eng.on_bar(BAR_END, BAR_END + timedelta(seconds=3))
        card = out["stored"][0]["card"]
        assert card["status"] == "APPROVED"
        assert any(x.startswith(EVENT_COVERAGE_MISSING) for x in card["why_now"]), card["why_now"]
