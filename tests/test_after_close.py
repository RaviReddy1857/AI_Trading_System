"""The stock option shortlist, run by the app every trading day (app/options/after_close.py): the priority stocks'
chains from 14:50 while bids and asks are live (to 15:15, the end of continuous trading from 3 Aug 2026) (paced within the per-minute limit; a strike without a valid bid/ask
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
CHAINS_AT = datetime(2026, 11, 16, 14, 50, tzinfo=IST)           # the chain capture, continuous trading
AT = datetime(2026, 11, 16, 15, 41, tzinfo=IST)                  # after every close (options 15:40): history, shortlist
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

    def update(self, now, symbols=None):
        self.symbols = symbols
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
def test_chains_from_1450_then_history_and_shortlist_after_the_close_every_call_counted(tmp_path):
    job, s, store, j, src = _job(tmp_path)
    assert job.run((CHAINS_AT - timedelta(minutes=1)).astimezone(timezone.utc)) is None      # 14:49: nothing yet
    st = job.run(CHAINS_AT.astimezone(timezone.utc))
    # 2 priority stocks x 2 monthly expiries x 2 sides, from 14:50, before continuous trading ends
    assert len(src.calls) == 8 and {c[0] for c in src.calls} == set(STOCKS) and {c[1] for c in src.calls} == {EXP, EXP_NEXT}
    assert st["chains_status"] == DONE and "status" not in st and store.requests_used(DAY) == 8
    assert datetime.fromisoformat(st["steps"]["chains"]["at_utc"]).astimezone(IST).strftime("%H:%M") == "14:50"
    assert job.run((CHAINS_AT + timedelta(minutes=5)).astimezone(timezone.utc)) is None       # once a day
    assert "NOT FINISHED by 14:55 (step: history and shortlist" in summary_lines(s, j, DAY, CHAINS_AT + timedelta(minutes=5))[0]
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
    assert "No candidates" not in text and "waiting for today's bars" in text     # no session bar: it waits


def test_due_chains_from_1450_until_1515_and_finish_from_1541_on_trading_days(tmp_path):
    job, s, store, j, _ = _job(tmp_path)
    utc = lambda h, m, d=16: datetime(2026, 11, d, h, m, tzinfo=IST).astimezone(timezone.utc)   # noqa: E731
    assert job.due(utc(14, 49)) is None and job.due(utc(14, 50)) == "chains" and job.due(utc(15, 14)) == "chains"
    assert job.due(utc(15, 15)) is None                                      # continuous trading ended: never after
    assert job.due(utc(15, 40)) is None and job.due(utc(15, 41)) == "finish"  # after every close (options 15:40)
    assert job.due(datetime(2026, 11, 21, 14, 50, tzinfo=IST).astimezone(timezone.utc)) is None   # Saturday
    assert job.due(utc(14, 50, 24)) is None and job.due(utc(15, 41, 24)) is None                  # holiday
    j.set_state(STATE_KEY.format(day=DAY), json.dumps({"chains_status": RUNNING, "steps": {}}), AT)
    assert job.due(utc(14, 52)) == "chains"                                  # left RUNNING by a stopped app
    job.run(utc(14, 52))
    assert job.due(utc(14, 55)) is None
    job.run(utc(15, 41))
    assert job.due(utc(16, 30)) is None                                      # done today


def test_chains_missed_before_the_close_are_recorded_and_never_captured_after_it(tmp_path):
    job, s, store, j, src = _job(tmp_path)
    st = job.run(datetime(2026, 11, 16, 15, 41, tzinfo=IST).astimezone(timezone.utc))   # the app started at 15:41
    assert src.calls == [] and st["steps"]["chains"]["error"].startswith("MISSED: the app was not running")
    text = "\n".join(summary_lines(s, j, DAY, datetime(2026, 11, 16, 15, 45, tzinfo=IST)))
    assert "chains FAILED: MISSED" in text and "No candidates" not in text


def test_a_strike_without_a_valid_bid_ask_at_capture_is_stored_as_unpriced_and_reported(tmp_path):
    rows = [raw(1000, 9.5, 10.5), raw(1010, 0, 0), raw(1020, None, 7.0), raw(1030, 8.0, 6.0)]   # ok, none, none, crossed
    job, s, store, j, src = _job(tmp_path, rows=rows)
    st = job.run(CHAINS_AT.astimezone(timezone.utc))
    ch = st["steps"]["chains"]
    assert ch["no_bid_ask"] == 3 * 8 and ch["strikes"] == 4 * 8            # 8 calls, each 1 priced + 3 unpriced
    got = {r["strike"]: r for r in store.latest_option_chain("sim", "RELIANCE", EXP) if r["right"] == "PE"}
    assert (got[1000.0]["bid"], got[1000.0]["ask"]) == (9.5, 10.5)
    assert all(got[k]["bid"] is None and got[k]["ask"] is None for k in (1010.0, 1020.0, 1030.0))
    assert got[1010.0]["last"] == 5.0                                        # the last trade kept, never used as a price
    # zero or missing bid/ask: no quality event (counted above only); the crossed strike is still logged
    assert sum(1 for e in store.recent_quality_events(100) if e["reason"] == "BAD_OPTION_QUOTE") == 8
    job.run(AT.astimezone(timezone.utc))
    text = "\n".join(summary_lines(s, j, DAY, datetime(2026, 11, 16, 15, 45, tzinfo=IST)))
    assert "chains at 14:50: 24 of 32 strikes had no valid bid/ask (stored as unpriced)" in text
    assert "No candidates" not in text


def test_keep_unquoted_false_still_drops_unpriced_strikes(tmp_path):
    s = _settings(tmp_path)
    store = Store(":memory:")
    InstrumentBook(store, "sim").save([ContractSpec("RELIANCE", EXP, LOT, "monthly", (1000.0, 1010.0),
                                                    AT.astimezone(timezone.utc))])
    loader = ChainLoader(s, store, FakeChainSource([raw(1000, 9.5, 10.5), raw(1010, 0, 0)]), InstrumentBook(store, "sim"))
    out = loader.load(["RELIANCE"], now=CHAINS_AT.astimezone(timezone.utc), keep_unquoted=False)
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
    assert "NOT FINISHED by 15:41 (step: history)" in summary_lines(s, j, DAY, AT)[0]
    _state(j, steps={"chains": {"ok": True}, "history": {"ok": True},
                     "shortlist": {"ok": False, "error": "no daily bars stored for the index"}})
    assert summary_lines(s, j, DAY, AT)[0].startswith("Stock option shortlist: FAILED - no daily bars")


def test_the_shortlist_is_its_own_message_when_the_job_completes_not_in_the_1545_summary(tmp_path):
    """User, 2026-11-17: the 15:45 summary keeps signals, paper P&L and positions; the shortlist is sent on its own
    when the after-close job completes (or at 17:30 when it has not), with the same wording rules."""
    from app.options.after_close import shortlist_message
    from app.schedule import eod_summary
    job, s, store, j, _ = _job(tmp_path)
    job.run(CHAINS_AT.astimezone(timezone.utc))
    assert shortlist_message(s, j, DAY, AT.astimezone(timezone.utc)) is None        # chains only: not finished
    job.run(AT.astimezone(timezone.utc))
    eod = eod_summary(s, j, datetime(2026, 11, 16, 15, 45, tzinfo=IST).astimezone(timezone.utc))
    assert "Stock option shortlist (session" not in eod and "its own message when it completes" in eod
    text = shortlist_message(s, j, DAY, (AT + timedelta(minutes=1)).astimezone(timezone.utc))
    assert text.startswith("STOCK OPTION SHORTLIST Mon 16-Nov-2026" + chr(10))
    assert "Stock option shortlist (session 2026-11-16, 2 priority stocks, 11 Breeze calls):" in text
    assert "NO RESULT" in text and "data missing: EVENT_COVERAGE_UNKNOWN 2, NO_CHAIN 2" in text, text
    assert "No candidates" not in text


class _Book:
    def __init__(self):
        self.sent = {}

    def get(self, key):
        return self.sent.get(key)

    def enqueue(self, key, kind, text, now, expires=None):
        self.sent[key] = (kind, text)
        return True


def _notifier(s, j):
    return type("N", (), {"s": s, "j": j, "book": _Book(), "cal": MarketCalendar(), "store": None, "regime": None})()


def test_the_shortlist_message_is_sent_once_on_completion(tmp_path):
    from app.schedule import scheduled
    job, s, store, j, _ = _job(tmp_path)
    n = _notifier(s, j)
    job.run(CHAINS_AT.astimezone(timezone.utc))
    scheduled(n, datetime(2026, 11, 16, 15, 30, tzinfo=IST).astimezone(timezone.utc))
    assert "shortlist:2026-11-16" not in n.book.sent
    job.run(AT.astimezone(timezone.utc))
    scheduled(n, datetime(2026, 11, 16, 15, 42, tzinfo=IST).astimezone(timezone.utc))
    kind, text = n.book.sent["shortlist:2026-11-16"]
    assert kind == "SHORTLIST" and "Stock option shortlist (session 2026-11-16" in text
    assert "eod:2026-11-16" not in n.book.sent                              # the 15:45 summary is separate


def test_a_shortlist_not_finished_by_1730_is_sent_then_saying_so(tmp_path):
    from app.options.after_close import shortlist_message
    from app.schedule import scheduled
    job, s, store, j, _ = _job(tmp_path)
    n = _notifier(s, j)
    j.set_state(STATE_KEY.format(day=DAY), json.dumps({"status": WAITING, "chains_status": DONE, "steps": {},
                                                       "next_retry_utc": "2026-11-16T11:50:00+00:00",
                                                       "bar_attempts": 6, "session_bars": {"missing": {"NIFTY": "x"}}}), AT)
    scheduled(n, datetime(2026, 11, 16, 17, 29, tzinfo=IST).astimezone(timezone.utc))
    assert "shortlist:2026-11-16" not in n.book.sent
    scheduled(n, datetime(2026, 11, 16, 17, 30, tzinfo=IST).astimezone(timezone.utc))
    assert "NOT FINISHED by 17:30" in n.book.sent["shortlist:2026-11-16"][1]
    # never started: NOT RUN at 17:30; a step still RUNNING at 17:30 is waited for 30 minutes
    j2 = Journal(Store(":memory:"), s)
    assert "NOT RUN" in shortlist_message(s, j2, DAY, datetime(2026, 11, 16, 17, 30, tzinfo=IST).astimezone(timezone.utc))
    j2.set_state(STATE_KEY.format(day=DAY), json.dumps({"status": RUNNING, "step": "shortlist"}), AT)
    assert shortlist_message(s, j2, DAY, datetime(2026, 11, 16, 17, 45, tzinfo=IST).astimezone(timezone.utc)) is None
    assert "NOT FINISHED" in shortlist_message(s, j2, DAY, datetime(2026, 11, 16, 18, 0, tzinfo=IST).astimezone(timezone.utc))


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
    from app.config import AfterCloseShortlist, Settings
    assert (AfterCloseShortlist().chain_time, AfterCloseShortlist().start_time) == ("14:50", "15:41")
    for bad in ({"start_time": "15:00"}, {"start_time": "15:40"}, {"chain_time": "15:15"}, {"chain_time": "09:00"}):
        raw = sim_settings().model_dump()
        raw["stock_option_selling"]["after_close"].update(bad)
        with pytest.raises(ValidationError):                  # checked against the session calendar (Settings)
            Settings.model_validate(raw)


# ---------------- 17-Nov-2026: the shortlist runs for the session the chains were captured in ----------------
from app.options.after_close import WAITING, SessionBarsStore, session_bar_from_5m  # noqa: E402
from tests import clock  # noqa: E402

TODAY = date(2026, 11, 17)                                       # Tuesday
T_CHAINS = datetime(2026, 11, 17, 14, 50, tzinfo=IST)
T_FINISH = datetime(2026, 11, 17, 15, 41, tzinfo=IST)
OPEN_UTC = datetime(2026, 11, 17, 9, 15, tzinfo=IST).astimezone(timezone.utc)


def five_minute_bars(sym, day=TODAY, n=75, base=1000.0):
    start = datetime.combine(day, datetime.min.time().replace(hour=9, minute=15), IST).astimezone(timezone.utc)
    return [Bar(sym, start + timedelta(minutes=5 * k), base + k, base + k + 2, base + k - 1, base + k + 1, 100.0)
            for k in range(n)]


class TodayHistory:
    """Breeze has not published today's daily bar: daily history up to 16-Nov only; today's 5-minute bars as given
    (complete, partial or none). `publish_daily_on`: the attempt on which today's daily bar arrives."""

    def __init__(self, store, max_requests, on_request, five_min: int = 75, publish_daily_on: int | None = None):
        self.store, self.on_request, self.five_min, self.publish = store, on_request, five_min, publish_daily_on
        TodayHistory.attempts = getattr(TodayHistory, "attempts", 0) + 1
        TodayHistory.last = self

    def update(self, now, symbols=None):
        self.symbols = symbols
        self.on_request()
        idx = [Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None) for d in weekdays_back(DAY, 5)]
        self.store.save_history_bars("bars_1d", idx + stock_bars("RELIANCE", end=DAY) + stock_bars("TCS", end=DAY), "sim")
        if self.five_min:
            for sym, base in (("NIFTY", 24000.0), ("RELIANCE", 1100.0), ("TCS", 1300.0)):
                self.store.save_history_bars("bars_5m", five_minute_bars(sym, n=self.five_min, base=base), "sim")
        if self.publish is not None and TodayHistory.attempts >= self.publish:
            self.store.save_history_bars("bars_1d", [Bar(x, daily_start(TODAY), 1, 2, 0.5, 1.5, 10.0)
                                                     for x in ("NIFTY", "RELIANCE", "TCS")], "sim")
        return {"NIFTY:1day": {"saved": 5}}


def _today_job(tmp_path, **hist):
    TodayHistory.attempts = 0
    s = _settings(tmp_path)
    store = Store(":memory:")
    j = Journal(store, s)
    strikes = tuple(float(k) for k in range(800, 1501, 10))
    InstrumentBook(store, "sim").save([ContractSpec(sym, e, LOT, "monthly", strikes, T_CHAINS.astimezone(timezone.utc))
                                       for sym in STOCKS for e in (EXP, EXP_NEXT)])
    src = FakeChainSource([raw(1000, 9.5, 10.5), raw(1100, 19.5, 20.5), raw(1200, 39.0, 41.0)])
    chains = ChainLoader(s, store, src, InstrumentBook(store, "sim"))
    job = AfterCloseJob(s, store, j, MarketCalendar(), chains, lambda m, cb: TodayHistory(store, m, cb, **hist), "sim")
    return job, s, store, j


def _run(job, at):
    with clock.travel(at.astimezone(timezone.utc)):
        return job.run(at.astimezone(timezone.utc))


def test_today_without_the_daily_bar_it_waits_then_builds_an_approximate_close_and_the_session_is_today(tmp_path):
    job, s, store, j = _today_job(tmp_path, five_min=75)
    _run(job, T_CHAINS)
    st = _run(job, T_FINISH)
    assert st["status"] == WAITING and st["session_bars"]["from_5m"] == []      # waits for the official close
    st = _run(job, datetime(2026, 11, 17, 17, 20, tzinfo=IST))              # the last attempt before 17:30
    assert st["status"] == DONE and st["steps"]["shortlist"]["session_date"] == "2026-11-17"     # never 16-Nov
    assert st["session_bars"]["from_5m"] == ["NIFTY", "RELIANCE", "TCS"] and st["session_bars"]["missing"] == {}
    assert "NO_CHAIN" not in st["steps"]["shortlist"]["reasons"]            # today's chains are used
    assert store.shortlist()["session_date"] == "2026-11-17"
    assert store.history_bars("bars_1d", "NIFTY", "sim", since=OPEN_UTC) == []   # nothing written to the daily table
    text = "\n".join(summary_lines(s, j, TODAY, datetime(2026, 11, 17, 15, 45, tzinfo=IST)))
    assert "(session 2026-11-17," in text
    assert ("APPROXIMATE close for 3 (no daily bar by 17:30: the last continuous trade from stored 5-minute bars, "
            "not the official closing-auction price): NIFTY, RELIANCE, TCS") in text


def test_today_with_incomplete_bars_it_waits_and_retries_then_uses_the_published_daily_bar(tmp_path):
    job, s, store, j = _today_job(tmp_path, five_min=11, publish_daily_on=2)   # 09:15-10:05 only, as on 17-Nov
    _run(job, T_CHAINS)
    st = _run(job, T_FINISH)
    assert st["status"] == WAITING and st["next_retry_utc"].startswith("2026-11-17T10:26")   # 15:56 IST
    assert "no daily bar (official close) for 17-Nov yet" in st["session_bars"]["missing"]["NIFTY"]
    assert store.shortlist()["rows"] == []                                   # not built on 16-Nov meanwhile
    head = summary_lines(s, j, TODAY, datetime(2026, 11, 17, 15, 45, tzinfo=IST))
    assert head[0].startswith("Stock option shortlist: NOT FINISHED by 15:45: waiting for today's bars (attempt 1, "
                              "next try 15:56, until 17:30)") and "NIFTY (no daily bar (official close) for 17-Nov yet" in head[1]
    assert job.due(datetime(2026, 11, 17, 15, 40, tzinfo=IST).astimezone(timezone.utc)) is None
    st = _run(job, datetime(2026, 11, 17, 15, 56, tzinfo=IST))
    assert TodayHistory.last.symbols == {"NIFTY", "RELIANCE", "TCS"}         # a retry asks only for the missing
    assert st["status"] == DONE and st["steps"]["shortlist"]["session_date"] == "2026-11-17"
    text = "\n".join(summary_lines(s, j, TODAY, datetime(2026, 11, 17, 16, 0, tzinfo=IST)))
    assert "session close: Breeze daily bars (official close) for 3 (after 2 attempts)" in text


def test_today_past_the_deadline_without_the_index_bar_it_fails_and_never_uses_an_older_session(tmp_path):
    job, s, store, j = _today_job(tmp_path, five_min=11)
    _run(job, T_CHAINS)
    st = _run(job, datetime(2026, 11, 17, 17, 20, tzinfo=IST))              # next try 17:35 > 17:30: last attempt
    assert st["status"] == DONE and not st["steps"]["shortlist"]["ok"]
    assert "no bar for NIFTY on 17-Nov-2026" in st["steps"]["shortlist"]["error"]
    assert "not run on an older session" in st["steps"]["shortlist"]["error"]
    assert store.shortlist()["rows"] == []
    text = "\n".join(summary_lines(s, j, TODAY, datetime(2026, 11, 17, 17, 30, tzinfo=IST)))
    assert text.startswith("Stock option shortlist: FAILED - no bar for NIFTY") and "No candidates" not in text


def test_run_shortlist_for_a_given_session_refuses_an_older_one(tmp_path):
    from app.options.shortlist import run_shortlist
    s = _settings(tmp_path)
    store = Store(":memory:")
    store.save_history_bars("bars_1d", [Bar("NIFTY", daily_start(d), 1, 2, 0.5, 1.5, None)
                                        for d in weekdays_back(DAY, 5)], "sim")
    out = run_shortlist(s, store, "sim", now=T_FINISH.astimezone(timezone.utc), session=TODAY, save=False)
    assert out["rows"] == [] and "no daily bar for NIFTY on 17-Nov-2026 (newest 16-Nov-2026" in out["error"]


def test_session_bar_from_5_minute_bars_needs_the_full_continuous_session():
    store = Store(":memory:")
    bars = five_minute_bars("RELIANCE", base=1000.0)
    store.save_history_bars("bars_5m", bars, "sim")
    bar, how = session_bar_from_5m(store, "sim", "RELIANCE", TODAY)
    # an F&O stock from 3 Aug 2026: the 72 bars of continuous trading (09:15-15:10), never the auction bars
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (1000.0, 1073.0, 999.0, 1072.0, 7200.0)
    assert bar.start == OPEN_UTC and how.startswith("APPROXIMATE: built from 72 stored 5-minute bars to 15:15")
    store2 = Store(":memory:")
    store2.save_history_bars("bars_5m", bars[:71], "sim")                    # 15:10 missing
    assert session_bar_from_5m(store2, "sim", "RELIANCE", TODAY)[0] is None


def test_session_bars_store_adds_the_built_bar_only_where_the_daily_history_lacks_it():
    store = Store(":memory:")
    hist = [Bar("NIFTY", daily_start(d), 1, 2, 0.5, 1.5, None) for d in weekdays_back(DAY, 5)]
    store.save_history_bars("bars_1d", hist, "sim")
    built = Bar("NIFTY", daily_start(TODAY), 3, 4, 2, 3.5, None)
    view = SessionBarsStore(store, {"NIFTY": built})
    got = view.history_bars("bars_1d", "NIFTY", "sim", limit=3)
    assert len(got) == 3 and got[-1] == built
    assert view.history_bars("bars_1d", "NIFTY", "sim", before=OPEN_UTC)[-1].start < OPEN_UTC
    assert view.history_bars("bars_5m", "NIFTY", "sim") == []                # other tables: the store's
    store.save_history_bars("bars_1d", [Bar("NIFTY", daily_start(TODAY), 9, 9, 9, 9, None)], "sim")
    assert view.history_bars("bars_1d", "NIFTY", "sim", limit=1)[0].close == 9    # the stored daily bar wins
    assert view.requests_used(TODAY) == 0                                    # everything else passes through


def test_bars_deadline_must_be_after_the_start():
    from pydantic import ValidationError
    from app.config import AfterCloseShortlist
    with pytest.raises(ValidationError):
        AfterCloseShortlist(start_time="15:31", bars_deadline="15:31")
