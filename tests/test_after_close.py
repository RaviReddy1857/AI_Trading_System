"""The stock option shortlist, run by the app every trading day (app/options/after_close.py): the priority stocks'
chains at 15:20 while bids and asks are live (paced within the per-minute limit; a strike without a valid bid/ask
recorded as such), then after the close the bar history and the shortlist - every Breeze call counted in the day's
budget and stopped at what is left - and its result in the 15:45 summary, never "no candidates" when a step failed or
data is missing."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.calendar import MarketCalendar
from app.history import HistoryLoader, RequestBudgetUsed, daily_start
from app.journal import Journal
from app.models import Bar
from app.options.after_close import (DONE, RUNNING, STATE_KEY, AfterCloseJob, expected_calls, priority_stocks,
                                     summary_lines)
from app.options.chain import ChainLoader, RateLimit
from app.options.instruments import ContractSpec, InstrumentBook
from app.quality import IST
from app.store import Store
from tests.test_phase9_core import EXP, EXP_NEXT, LOT, shortlist_contexts, stock_bars, weekdays_back
from tests.test_step1 import sim_settings

pytestmark = pytest.mark.usefixtures("fixed_clock")

DAY = date(2026, 11, 16)                                         # Monday, a trading day
CHAINS_AT = datetime(2026, 11, 16, 15, 20, tzinfo=IST)           # the chain capture, market open
AT = datetime(2026, 11, 16, 15, 31, tzinfo=IST)                  # after the close: history and shortlist
STOCKS = ["RELIANCE", "TCS"]


def _settings(tmp_path, stocks=STOCKS):
    status = tmp_path / "download_status.json"
    status.write_text(json.dumps({"stocks": stocks}), encoding="utf-8")
    s = sim_settings()
    return s.model_copy(update={"data_status": s.data_status.model_copy(update={"download_status_file": str(status)})})


class FakeChainSource:
    name, remote = "sim", False

    def __init__(self, rows=None):
        self.calls, self.rows = [], rows or []

    def fetch(self, inst, spec, right):
        self.calls.append((inst.symbol, spec.expiry, right))
        return list(self.rows)                                     # default: nothing quoted, the stock has no chain


def raw(strike, bid, ask, ltp=5.0):
    return {"strike_price": strike, "best_bid_price": bid, "best_offer_price": ask, "ltp": ltp, "open_interest": 1000,
            "chnge_oi": 0, "total_quantity_traded": 10, "spot_price": 1000}


class FakeHistory:
    """A HistoryLoader stand-in: sends `calls` requests through the budget hooks and stores the session's bars."""

    def __init__(self, store, max_requests, on_request, calls=3):
        self.store, self.max_requests, self.on_request, self.calls = store, max_requests, on_request, calls
        FakeHistory.made = self

    def update(self, now):
        sent = 0
        for _ in range(self.calls):
            if sent >= self.max_requests:
                return {"NIFTY:day": {"error": "RequestBudgetUsed: the day's Breeze request budget is used"}}
            sent += 1
            self.on_request()
        idx = [Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None) for d in weekdays_back(DAY, 5)]
        self.store.save_history_bars("bars_1d", idx + stock_bars("RELIANCE", end=DAY) + stock_bars("TCS", end=DAY), "sim")
        return {"NIFTY:day": {"saved": 5}}


def _job(tmp_path, store=None, history_calls=3, rows=None):
    s = _settings(tmp_path)
    store = store or Store(":memory:")
    j = Journal(store, s)
    strikes = tuple(float(k) for k in range(800, 1501, 10))
    InstrumentBook(store, "sim").save([ContractSpec(sym, e, LOT, "monthly", strikes, AT.astimezone(timezone.utc))
                                       for sym in STOCKS for e in (EXP, EXP_NEXT)])
    src = FakeChainSource(rows)
    chains = ChainLoader(s, store, src, InstrumentBook(store, "sim"))
    job = AfterCloseJob(s, store, j, MarketCalendar({date(2026, 11, 24): "Prakash Gurpurb"}), chains,
                        lambda m, cb: FakeHistory(store, m, cb, history_calls), "sim")
    return job, s, store, j, src


# ---------------- the job ----------------
def test_chains_at_1520_then_history_and_shortlist_after_the_close_every_call_counted(tmp_path):
    job, s, store, j, src = _job(tmp_path)
    assert job.run((CHAINS_AT - timedelta(minutes=1)).astimezone(timezone.utc)) is None      # 15:19: nothing yet
    st = job.run(CHAINS_AT.astimezone(timezone.utc))
    # 2 priority stocks x 2 monthly expiries x 2 sides, at 15:20, before the close
    assert len(src.calls) == 8 and {c[0] for c in src.calls} == set(STOCKS) and {c[1] for c in src.calls} == {EXP, EXP_NEXT}
    assert st["chains_status"] == DONE and "status" not in st and store.requests_used(DAY) == 8
    assert datetime.fromisoformat(st["steps"]["chains"]["at_utc"]).astimezone(IST).strftime("%H:%M") == "15:20"
    assert job.run((CHAINS_AT + timedelta(minutes=5)).astimezone(timezone.utc)) is None       # once a day
    assert "NOT FINISHED by 15:25 (step: history and shortlist" in summary_lines(s, j, DAY, CHAINS_AT + timedelta(minutes=5))[0]
    st = job.run(AT.astimezone(timezone.utc))
    assert len(src.calls) == 8                                               # no chain call after the close
    assert st["status"] == DONE and st["requests"] == 8 + 3 and store.requests_used(DAY) == 11
    assert st["steps"]["chains"]["ok"] and st["steps"]["history"]["ok"] and st["steps"]["shortlist"]["ok"]
    assert store.shortlist()["session_date"] == DAY.isoformat()            # stored for the session
    assert st["steps"]["shortlist"]["data_gaps"]["NO_CHAIN"] == 2           # nothing was quoted
    assert json.loads(j.get_state(STATE_KEY.format(day=DAY)))["status"] == DONE


def test_the_job_stops_at_what_is_left_of_the_days_budget(tmp_path):
    job, s, store, j, src = _job(tmp_path)
    store.add_requests(DAY, 3495)                                            # 3,500 a day: 5 left
    job.run(CHAINS_AT.astimezone(timezone.utc))
    st = job.run(AT.astimezone(timezone.utc))
    assert len(src.calls) == 4 and store.requests_used(DAY) == 3500         # 2 chains (4 calls); 1 left, not enough
    failed = st["steps"]["chains"]["failed"]
    assert len(failed) == 2 and all(v.startswith("BUDGET") for v in failed.values())
    assert FakeHistory.made.max_requests == 1 and not st["steps"]["history"]["ok"]
    text = "\n".join(summary_lines(s, j, DAY, AT))
    assert "chains failed for 2" in text and "BUDGET" in text and "history failed" in text
    assert "No candidates" not in text


def test_due_chains_from_1520_until_the_close_and_finish_from_1531_on_trading_days(tmp_path):
    job, s, store, j, _ = _job(tmp_path)
    utc = lambda h, m, d=16: datetime(2026, 11, d, h, m, tzinfo=IST).astimezone(timezone.utc)   # noqa: E731
    assert job.due(utc(15, 19)) is None and job.due(utc(15, 20)) == "chains" and job.due(utc(15, 29)) == "chains"
    assert job.due(utc(15, 30)) is None                                      # the close: never captured after it
    assert job.due(utc(15, 31)) == "finish"
    assert job.due(datetime(2026, 11, 21, 15, 20, tzinfo=IST).astimezone(timezone.utc)) is None   # Saturday
    assert job.due(utc(15, 20, 24)) is None and job.due(utc(15, 31, 24)) is None                  # holiday
    j.set_state(STATE_KEY.format(day=DAY), json.dumps({"chains_status": RUNNING, "steps": {}}), AT)
    assert job.due(utc(15, 22)) == "chains"                                  # left RUNNING by a stopped app
    job.run(utc(15, 22))
    assert job.due(utc(15, 25)) is None
    job.run(utc(15, 31))
    assert job.due(utc(16, 30)) is None                                      # done today


def test_chains_missed_before_the_close_are_recorded_and_never_captured_after_it(tmp_path):
    job, s, store, j, src = _job(tmp_path)
    st = job.run(datetime(2026, 11, 16, 15, 40, tzinfo=IST).astimezone(timezone.utc))   # the app started at 15:40
    assert src.calls == [] and st["steps"]["chains"]["error"].startswith("MISSED: the app was not running")
    text = "\n".join(summary_lines(s, j, DAY, datetime(2026, 11, 16, 15, 45, tzinfo=IST)))
    assert "chains FAILED: MISSED" in text and "No candidates" not in text


def test_a_strike_without_a_valid_bid_ask_at_1520_is_stored_as_unpriced_and_reported(tmp_path):
    rows = [raw(1000, 9.5, 10.5), raw(1010, 0, 0), raw(1020, None, 7.0), raw(1030, 8.0, 6.0)]   # ok, none, none, crossed
    job, s, store, j, src = _job(tmp_path, rows=rows)
    st = job.run(CHAINS_AT.astimezone(timezone.utc))
    ch = st["steps"]["chains"]
    assert ch["no_bid_ask"] == 3 * 8 and ch["strikes"] == 4 * 8            # 8 calls, each 1 priced + 3 unpriced
    got = {r["strike"]: r for r in store.latest_option_chain("sim", "RELIANCE", EXP) if r["right"] == "PE"}
    assert (got[1000.0]["bid"], got[1000.0]["ask"]) == (9.5, 10.5)
    assert all(got[k]["bid"] is None and got[k]["ask"] is None for k in (1010.0, 1020.0, 1030.0))
    assert got[1010.0]["last"] == 5.0                                        # the last trade kept, never used as a price
    assert sum(1 for e in store.recent_quality_events(100) if e["reason"] == "BAD_OPTION_QUOTE") == 24   # still logged
    job.run(AT.astimezone(timezone.utc))
    text = "\n".join(summary_lines(s, j, DAY, datetime(2026, 11, 16, 15, 45, tzinfo=IST)))
    assert "chains at 15:20: 24 of 32 strikes had no valid bid/ask (stored as unpriced)" in text
    assert "No candidates" not in text


def test_the_live_index_refresh_keeps_dropping_unpriced_strikes(tmp_path):
    s = _settings(tmp_path)
    store = Store(":memory:")
    InstrumentBook(store, "sim").save([ContractSpec("RELIANCE", EXP, LOT, "monthly", (1000.0, 1010.0),
                                                    AT.astimezone(timezone.utc))])
    loader = ChainLoader(s, store, FakeChainSource([raw(1000, 9.5, 10.5), raw(1010, 0, 0)]), InstrumentBook(store, "sim"))
    out = loader.load(["RELIANCE"], now=CHAINS_AT.astimezone(timezone.utc))
    assert all("no_bid_ask" not in v for v in out.values())
    assert {r["strike"] for r in store.latest_option_chain("sim", "RELIANCE", EXP)} == {1000.0}


def test_chain_calls_keep_the_per_minute_limit():
    t = {"now": 0.0}
    slept = []

    def sleep(x):
        slept.append(x)
        t["now"] += x
    rl = RateLimit(60, sleep, lambda: t["now"])
    for _ in range(150):
        rl.wait()
        t["now"] += 0.1                                                      # each call takes 0.1 s
    # 150 calls 0.1 s apart at 60 a minute: the 61st and the 121st wait until the oldest call is a minute old
    assert len(slept) == 2 and all(53.9 < x <= 60.1 for x in slept)
    calls = []
    t["now"] = 1000.0
    rl2 = RateLimit(60, sleep, lambda: t["now"])
    for _ in range(200):
        rl2.wait()
        calls.append(t["now"])
        t["now"] += 0.05
    assert max(sum(1 for c in calls if a <= c < a + 60) for a in calls) <= 60   # never more than 60 in any minute


def test_the_chain_loader_paces_remote_calls_through_the_shared_limit(tmp_path):
    s = _settings(tmp_path)
    s = s.model_copy(update={"options": s.options.model_copy(update={"chain": s.options.chain.model_copy(
        update={"max_calls_per_minute": 3, "request_pause_seconds": 0.0})})})
    store = Store(":memory:")
    InstrumentBook(store, "sim").save([ContractSpec(sym, e, LOT, "monthly", (1000.0,), AT.astimezone(timezone.utc))
                                       for sym in STOCKS for e in (EXP, EXP_NEXT)])
    t = {"now": 0.0}
    slept = []

    def sleep(x):
        slept.append(x)
        t["now"] += x

    class Remote(FakeChainSource):
        remote = True
    loader = ChainLoader(s, store, Remote(), InstrumentBook(store, "sim"), sleep=sleep, clock=lambda: t["now"])
    loader.load(STOCKS, now=CHAINS_AT.astimezone(timezone.utc))
    waits = [x for x in slept if x > 0]                                      # (the pause between calls is 0 here)
    assert len(waits) == 2 and loader.rate.waited > 119                      # 8 calls at 3 a minute: two full waits


# ---------------- the summary ----------------
def _state(j, **st):
    j.set_state(STATE_KEY.format(day=DAY), json.dumps({"status": DONE, "requests": 226, **st}), AT)


def _ok_steps(shortlist):
    return {"chains": {"ok": True, "failed": {}}, "history": {"ok": True, "failed": {}},
            "shortlist": {"ok": True, "session_date": DAY.isoformat(), "stocks": 30, **shortlist}}


def test_summary_lists_candidates_with_side_strikes_and_score(tmp_path):
    s = _settings(tmp_path)
    j = Journal(Store(":memory:"), s)
    _state(j, steps=_ok_steps({"candidates": [
        {"stock": "TCS", "side": "PE", "expiry": "2026-12-29", "strikes": [3200.0, 3150.0], "score": 72.34, "rank": 1}],
        "reasons": {"NO_TREND": 20, "IV_RANK_TOO_LOW": 9}, "data_gaps": {}}))
    text = "\n".join(summary_lines(s, j, DAY, AT))
    assert "1. TCS bull put 3200/3150 exp 2026-12-29 score 72.3" in text and "226 Breeze calls" in text
    assert "Not on the list: NO_TREND 20, IV_RANK_TOO_LOW 9" in text


def test_summary_says_no_candidates_only_when_every_step_succeeded_and_no_data_is_missing(tmp_path):
    s = _settings(tmp_path)
    j = Journal(Store(":memory:"), s)
    _state(j, steps=_ok_steps({"candidates": [], "reasons": {"NO_TREND": 21, "IV_RANK_TOO_LOW": 9}, "data_gaps": {}}))
    assert "No candidates. Top reasons: NO_TREND 21, IV_RANK_TOO_LOW 9" in "\n".join(summary_lines(s, j, DAY, AT))
    _state(j, steps=_ok_steps({"candidates": [], "reasons": {"EVENT_COVERAGE_UNKNOWN": 30},
                               "data_gaps": {"EVENT_COVERAGE_UNKNOWN": 30}}))
    text = "\n".join(summary_lines(s, j, DAY, AT))
    assert "No candidates" not in text and "NO RESULT" in text and "data missing: EVENT_COVERAGE_UNKNOWN 30" in text
    steps = _ok_steps({"candidates": [], "reasons": {"NO_TREND": 30}, "data_gaps": {}})
    steps["chains"] = {"ok": False, "error": "ConnectionError: Breeze down"}
    _state(j, steps=steps)
    text = "\n".join(summary_lines(s, j, DAY, AT))
    assert "No candidates" not in text and "chains FAILED: ConnectionError: Breeze down" in text


def test_summary_when_the_job_did_not_run_has_not_finished_or_failed(tmp_path):
    s = _settings(tmp_path)
    j = Journal(Store(":memory:"), s)
    assert "NOT RUN" in summary_lines(s, j, DAY, AT)[0]
    j.set_state(STATE_KEY.format(day=DAY), json.dumps({"status": RUNNING, "step": "history"}), AT)
    assert "NOT FINISHED by 15:31 (step: history)" in summary_lines(s, j, DAY, AT)[0]
    _state(j, steps={"chains": {"ok": True}, "history": {"ok": True},
                     "shortlist": {"ok": False, "error": "no daily bars stored for the index"}})
    assert summary_lines(s, j, DAY, AT)[0].startswith("Stock option shortlist: FAILED - no daily bars")


def test_the_end_of_day_summary_includes_the_shortlist(tmp_path):
    from app.schedule import eod_summary
    job, s, store, j, _ = _job(tmp_path)
    job.run(CHAINS_AT.astimezone(timezone.utc))
    job.run(AT.astimezone(timezone.utc))
    text = eod_summary(s, j, datetime(2026, 11, 16, 15, 45, tzinfo=IST).astimezone(timezone.utc))
    assert "Stock option shortlist (session 2026-11-16, 2 priority stocks, 11 Breeze calls):" in text
    assert "NO RESULT" in text and "data missing: EVENT_COVERAGE_UNKNOWN 2, NO_CHAIN 2" in text, text
    assert "No candidates" not in text


# ---------------- the pieces ----------------
def test_shortlist_rows_carry_the_strikes_sold_first(tmp_path):
    from app.options.stock_selling import build_shortlist, build_stock_selling
    from tests.test_phase9_core import settings as p9
    s = p9()
    rows = build_shortlist(build_stock_selling(s), shortlist_contexts(s))
    top = [r for r in rows if r["status"] == "SHORTLISTED"]
    assert top and all(len(r["strikes"]) == 2 for r in top)
    for r in top:                                                   # bull put: the sold put above the bought one
        sold, bought = r["strikes"]
        assert (sold > bought) if r["side"] == "PE" else (sold < bought)


def test_history_loader_stops_at_its_request_limit_and_counts_each_call(tmp_path):
    class Src:
        name, remote = "sim", False

        def fetch(self, inst, interval, a, b):
            return []
    s = _settings(tmp_path)
    seen = []
    loader = HistoryLoader(s, Store(":memory:"), Src(), MarketCalendar(), max_requests=3, on_request=lambda: seen.append(1))
    out = loader.update(AT.astimezone(timezone.utc))
    assert len(seen) == 3                                       # never more than the limit, each call counted
    stopped = [k for k, v in out.items() if "RequestBudgetUsed" in v.get("error", "")]
    assert stopped and stopped[-1] == list(out)[-1]            # every symbol after the limit: nothing asked
    assert all(RequestBudgetUsed.__name__ in out[k]["error"] for k in list(out)[list(out).index(stopped[0]):])


def test_priority_stocks_and_the_daily_call_count(tmp_path):
    s = _settings(tmp_path, stocks=["TCS", "NOT_IN_WATCHLIST", "RELIANCE"])
    assert priority_stocks(s) == ["TCS", "RELIANCE"]
    n = expected_calls(s)
    assert n["chains"] == 2 * 2 * 2 and n["history"] == len(s.watchlist) * 2
    assert n["most"] == n["chains"] + n["history"] * (1 + s.history.empty_retries)


def test_start_time_after_the_close_and_chain_time_in_market_hours():
    from pydantic import ValidationError
    from app.config import AfterCloseShortlist
    assert AfterCloseShortlist(start_time="15:30").start_time == "15:30"
    assert AfterCloseShortlist().chain_time == "15:20"
    for bad in ({"start_time": "15:00"}, {"chain_time": "15:30"}, {"chain_time": "09:00"}):
        with pytest.raises(ValidationError):
            AfterCloseShortlist(**bad)
