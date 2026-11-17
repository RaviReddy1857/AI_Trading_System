"""Review fixes, Codex review of b012f3f..b7b1bf7 (2026-11-17). Each test fails on the code before the fix.

1. HIGH: the option backtest decided at 15:35 (the stocks' close) but read option daily bars (close, volume, OI) that
   are final only at the options' 15:40 close. Now it decides at the options' close and a context stamped earlier
   cannot read a bar of that day (LookAheadError).
2. MEDIUM: the chain capture re-checks the 15:15 stop right before each call, after on_request().
3. MEDIUM: the after-close job's last retry runs at the 17:30 deadline itself; the approximate close only if the
   official daily bar is still missing then.
4. MEDIUM: the cash intraday cutoff is dated: before 3 Aug 2026 the configured 15:15 exit; from 3 Aug 15:00.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.calendar import MarketCalendar
from app.history import daily_start
from app.models import Bar
from app.quality import IST
from app.store import Store
from tests.test_step1 import sim_settings

pytestmark = pytest.mark.usefixtures("fixed_clock")


def at(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime.combine(d, time(h, m, s), IST).astimezone(timezone.utc)


# ---------------- 1. option backtest decision time ----------------
class _Prices:
    """An option daily bar whose final values (set at the 15:40 close) differ from what was seen at 15:35."""
    label = "fixture"

    def __init__(self):
        self.reads = []

    def bar(self, underlying, expiry, strike, right, d):
        self.reads.append(d)
        from app.options.option_history import OptionBar
        return OptionBar(d, 10, 12, 9, 11.0, 500.0, 7000.0)


def test_a_1535_decision_cannot_read_an_option_daily_bar_that_is_final_at_1540():
    from app.options.backtest import HistoryQuotes
    from app.strategies.base import LookAheadError
    d = date(2026, 8, 4)
    prices = _Prices()
    q = HistoryQuotes(prices, "RELIANCE", d, 1000.0, 10.0, asof=at(d, 15, 35))
    with pytest.raises(LookAheadError, match="final at 15:40"):
        q.quote(date(2026, 8, 27), 1000.0, "PE")
    assert prices.reads == []                                    # the bar's values never reached the decision
    ok = HistoryQuotes(prices, "RELIANCE", d, 1000.0, 10.0, asof=at(d, 15, 40))
    assert ok.quote(date(2026, 8, 27), 1000.0, "PE").last == 11.0
    pre = date(2026, 7, 31)                                      # before 3 Aug: final at 15:30, as before
    assert HistoryQuotes(prices, "RELIANCE", pre, 1000.0, 10.0, asof=at(pre, 15, 30)).quote(
        date(2026, 8, 27), 1000.0, "PE").last == 11.0


def test_the_option_backtest_decides_at_the_options_close_and_nothing_before_3_aug_moves():
    from app.options.backtest import OptionEngine
    s = sim_settings()
    eng = OptionEngine.__new__(OptionEngine)                      # only context(): its inputs stubbed
    eng.s, eng.o, eng.rate, eng.costs = s, s.options, 0.065, None
    eng.kinds, eng.prices = {"RELIANCE": "stock"}, _Prices()
    eng.iv_at = lambda sym, d: (None, None)
    eng.lot_size = lambda sym: 250
    eng.expiries = lambda sym: [(date(2026, 8, 27), "monthly")]
    eng.base = type("B", (), {"regime": type("R", (), {"at_close": lambda self, d: type("X", (), {"regime": "RANGE"})()})(),
                              "index_daily": lambda self, d: ()})()
    from app.options import build_option_strategies
    strat = build_option_strategies(s)[0]
    post = eng.context(strat, "RELIANCE", date(2026, 8, 4), 1000.0, "strategy")
    assert post.asof == at(date(2026, 8, 4), 15, 40) and post.quotes.asof == post.asof
    pre = eng.context(strat, "RELIANCE", date(2026, 6, 30), 1000.0, "strategy")
    assert pre.asof == at(date(2026, 6, 30), 15, 30)             # unchanged before 3 Aug (stored option data ends 30-Jun)


# ---------------- 2. the chain stop re-checked after on_request ----------------
def test_no_chain_call_once_the_clock_passes_1515_inside_on_request(tmp_path):
    from app.options.chain import ChainLoader
    from app.options.instruments import ContractSpec, InstrumentBook
    from tests.test_after_close import EXP, LOT, FakeChainSource, _settings, raw
    s = _settings(tmp_path)
    st = Store(":memory:")
    InstrumentBook(st, "sim").save([ContractSpec("RELIANCE", EXP, LOT, "monthly", (1000.0,), at(date(2026, 11, 16), 9, 0))])
    src = FakeChainSource([raw(1000.0, 5.0, 5.2)])
    clock = {"t": at(date(2026, 11, 16), 15, 14, 59)}

    def on_request():                                           # the budget write takes the clock past 15:15
        clock["t"] = at(date(2026, 11, 16), 15, 15, 1)
    out = ChainLoader(s, st, src, InstrumentBook(st, "sim")).load(
        ["RELIANCE"], max_requests=10, on_request=on_request, stop_at=at(date(2026, 11, 16), 15, 15),
        clock=lambda: clock["t"])
    assert src.calls == []                                      # no Breeze call after 15:15
    v = next(iter(out.values()))
    assert v["missed"] is True and "counted in the day's budget, not sent" in v["error"]


# ---------------- 3. the last retry at 17:30 itself ----------------
def test_an_official_daily_bar_arriving_at_1728_is_used_not_the_approximate_close(tmp_path):
    from app.options.after_close import DONE, WAITING
    from tests.test_after_close import T_CHAINS, T_FINISH, TODAY, _run, _today_job
    job, s, store, j = _today_job(tmp_path, five_min=75)        # every 5-minute bar there, no daily bar yet
    _run(job, T_CHAINS)
    assert _run(job, T_FINISH)["status"] == WAITING
    st = _run(job, datetime(2026, 11, 17, 17, 20, tzinfo=IST))
    assert st["status"] == WAITING and st["session_bars"]["from_5m"] == []      # before the fix: approximate here
    store.save_history_bars("bars_1d", [Bar(x, daily_start(TODAY), 1, 2, 0.5, 1.5, 10.0)       # 17:28: published
                                        for x in ("NIFTY", "RELIANCE", "TCS")], "sim")
    st = _run(job, datetime(2026, 11, 17, 17, 30, tzinfo=IST))
    assert st["status"] == DONE and st["session_bars"]["daily"] == 3 and st["session_bars"]["from_5m"] == []


# ---------------- 4. the dated intraday cutoff ----------------
def _intraday(day):
    from app.journal import Position
    return Position("p", "MANUAL", None, "INTRADAY_STOCKS", "CASH_INDEX", "RELIANCE", "LONG", None, 10, 100.0,
                    at(day, 10, 0), horizon="INTRADAY")


def test_intraday_time_exit_is_1515_on_31_jul_and_1500_on_4_aug():
    from app.positions import time_exit_at
    s = sim_settings()
    due, why = time_exit_at(_intraday(date(2026, 7, 31)), s, MarketCalendar())
    assert due == at(date(2026, 7, 31), 15, 15) and why == "intraday: out by 15:15 IST"
    due, why = time_exit_at(_intraday(date(2026, 8, 4)), s, MarketCalendar())
    assert due == at(date(2026, 8, 4), 15, 0) and "not confirmed" in why


@pytest.mark.parametrize("day, bar_end, expect", [
    (date(2026, 7, 31), (15, 5), True),        # before 3 Aug: entries until the 15:15 exit, as before
    (date(2026, 7, 31), (15, 15), False),
    (date(2026, 8, 4), (14, 55), True),
    (date(2026, 8, 4), (15, 5), False),        # from 3 Aug: none from 15:00
])
def test_cash_intraday_entries_stop_at_the_dated_cutoff(day, bar_end, expect):
    from tests.test_phase6_engine import make, seed, tick
    import tests.test_phase6_engine as pe
    open_t = at(day, 9, 15)
    end = at(day, *bar_end)
    c, eng = make()
    with c:
        old = pe.DAY_OPEN
        pe.DAY_OPEN = open_t
        try:
            px = seed(c.app, until=end)
        finally:
            pe.DAY_OPEN = old
        tick(c.app, end - timedelta(seconds=1), px)
        out = eng.on_bar(end, end + timedelta(seconds=3))
        assert any(x["strategy_id"] == "orb_v2" for x in out["stored"]) is expect
