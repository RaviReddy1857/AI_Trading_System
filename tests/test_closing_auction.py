"""The closing auction session (CAS) from 3 Aug 2026 and the dated session calendar (app/session.py).

From 3 Aug 2026: F&O stocks trade continuously until 15:15, then a closing auction 15:15-15:35 sets their official
close; index values are indicative during it; options trade until 15:40; non-F&O stocks until 15:30. Before: every
segment 09:15-15:30. Auction prices are never a signal, a paper fill, an exit trigger or a quality event; option exit
calls keep working until 15:40; cash intraday stops entries and exits by the (unconfirmed) 15:00 cutoff; the chain
capture runs 14:50-15:15 with each chain's own time stamp; the after-close job and the session close wait for
Breeze's daily bar (the official close) and label any 5-minute fallback APPROXIMATE; backtests never enter or exit on
an F&O stock bar inside the auction, and nothing before 3 Aug 2026 changes."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import session as S
from app.calendar import MarketCalendar
from app.config import SessionCfg, SessionPeriod, Settings, load_settings
from app.hub import MarketHub
from app.models import Bar, Tick
from app.quality import IST, is_market_hours
from app.session import AUCTION, CASH_FNO, CASH_OTHER, CLOSED, DERIVATIVES, INDEX, OPEN, SessionCalendar
from app.store import Store
from tests.test_step1 import sim_settings

pytestmark = pytest.mark.usefixtures("fixed_clock")

PRE = date(2026, 7, 31)          # Friday, the last session before the closing auction
POST = date(2026, 11, 16)        # Monday, the fixtures' day (closing auction in force)


def at(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime.combine(d, time(h, m, s), IST).astimezone(timezone.utc)


def _raw(**session):
    raw = sim_settings().model_dump()
    raw["session"].update(session)
    return raw


@pytest.fixture(autouse=True)
def _restore_session_calendar():
    """Tests that configure another calendar put the config.yaml one back."""
    yield
    S.configure(load_settings().session)


# ---------------- 1. the dated calendar per segment ----------------
def test_before_3_aug_every_segment_is_0915_to_1530_without_an_auction():
    cal = SessionCalendar()
    for seg in (CASH_FNO, CASH_OTHER, INDEX, DERIVATIVES):
        h = cal.hours(seg, PRE)
        assert (h.open, h.continuous_end, h.close, h.has_auction) == (time(9, 15), time(15, 30), time(15, 30), False)
        assert cal.phase(seg, at(PRE, 15, 20)) == OPEN and cal.phase(seg, at(PRE, 15, 30)) == OPEN   # inclusive
        assert cal.phase(seg, at(PRE, 15, 30, 1)) == CLOSED and cal.phase(seg, at(PRE, 9, 14, 59)) == CLOSED
    assert cal.latest_close(PRE) == time(15, 30) and cal.cash_close(PRE) == time(15, 30)
    assert cal.hours(CASH_FNO, date(2014, 10, 27)).close == time(15, 30)     # before the first period: the first


def test_from_3_aug_fno_stocks_auction_1515_to_1535_options_to_1540_other_stocks_to_1530():
    cal = SessionCalendar()
    d = date(2026, 8, 3)
    assert cal.phase(CASH_FNO, at(d, 15, 14, 59)) == OPEN
    assert cal.phase(CASH_FNO, at(d, 15, 15)) == AUCTION and cal.phase(CASH_FNO, at(d, 15, 35)) == AUCTION
    assert cal.phase(CASH_FNO, at(d, 15, 35, 1)) == CLOSED
    assert cal.phase(INDEX, at(d, 15, 20)) == AUCTION and cal.phase(INDEX, at(d, 15, 36)) == CLOSED
    assert cal.phase(CASH_OTHER, at(d, 15, 20)) == OPEN and cal.phase(CASH_OTHER, at(d, 15, 30, 1)) == CLOSED
    assert cal.phase(DERIVATIVES, at(d, 15, 39)) == OPEN and cal.phase(DERIVATIVES, at(d, 15, 40, 1)) == CLOSED
    assert cal.latest_close(d) == time(15, 40) and cal.cash_close(d) == time(15, 35)
    assert cal.hours(CASH_FNO, d).describe() == "09:15-15:15 continuous, closing auction 15:15-15:35"
    assert cal.phase(CASH_FNO, at(date(2026, 8, 1), 11, 0)) == CLOSED                       # Saturday
    assert cal.phase(CASH_FNO, at(POST, 11, 0), {POST}) == CLOSED                           # a holiday
    assert cal.hours(CASH_FNO, date(2026, 7, 31)).close == time(15, 30)                     # the day before


def test_config_yaml_holds_the_same_calendar_and_load_settings_sets_it():
    s = load_settings()
    d = SessionCfg()                                          # config.yaml = the code's defaults (both dated)
    assert s.session.periods == d.periods and s.session.periods[-1].cash_intraday_cutoff == "15:00"
    assert s.session.periods[0].cash_intraday_cutoff is None                # before 3 Aug: the 15:15 exit applies
    assert S.calendar().cfg == s.session
    assert all(i.fno for i in s.watchlist if i.kind == "stock")     # every Nifty 50 stock is an F&O stock
    raw = _raw(periods=[{"from_date": "2000-01-01"},
                        {"from_date": "2026-08-03", "fno_continuous_end": "14:00", "fno_close": "15:35",
                         "index_continuous_end": "15:15", "index_close": "15:35", "derivatives_close": "15:40",
                         "cash_intraday_cutoff": "13:30"}])
    raw["stock_option_selling"]["after_close"]["chain_time"] = "13:00"
    raw["stock_option_selling"]["settlement"]["forced_exit_by"] = "13:00"
    raw["backtest"]["intraday_exit_time"] = "14:00"
    S.configure(Settings.model_validate(raw).session)
    assert S.hours(CASH_FNO, POST).continuous_end == time(14, 0)


@pytest.mark.parametrize("change, message", [
    ({"session": {"periods": [{"from_date": "2000-01-01", "cash_intraday_cutoff": "15:31"}]}},
     "must be after the open and not after continuous trading"),
    ({"session": {"periods": [{"from_date": "2026-08-03"}, {"from_date": "2000-01-01"}]}}, "in date order"),
    ({"session": {"periods": [{"from_date": "2000-01-01", "fno_continuous_end": "15:40", "fno_close": "15:35"}]}},
     "need open < fno_continuous_end <= fno_close"),
    ({"after_close": {"chain_time": "15:20"}}, "must be in continuous trading, before 15:15"),
    ({"after_close": {"start_time": "15:31"}}, "must be after every segment's close (15:40"),
    ({"alerts": {"end_of_day_time": "15:40"}}, "end_of_day_time 15:40 must be after every segment's close"),
    ({"backtest": {"intraday_exit_time": "15:20"}}, "after continuous trading of F&O stocks ends (15:15"),
    ({"settlement": {"forced_exit_by": "15:20"}}, "forced_exit_by 15:20 must be in continuous trading"),
])
def test_times_that_contradict_the_session_are_refused(change, message):
    raw = sim_settings().model_dump()
    for k, v in change.items():
        target = raw["stock_option_selling"][k] if k in ("after_close", "settlement") else raw[k]
        target.update(v)
    with pytest.raises(ValueError, match=__import__("re").escape(message)):
        Settings.model_validate(raw)


def test_market_hours_any_segment_or_one_segment_continuous():
    assert is_market_hours(at(POST, 15, 38)) and not is_market_hours(at(POST, 15, 40, 1))   # options to 15:40
    assert is_market_hours(at(PRE, 15, 30)) and not is_market_hours(at(PRE, 15, 31))        # unchanged before
    assert is_market_hours(at(POST, 15, 20), segment=INDEX)                                 # in the auction ...
    assert not is_market_hours(at(POST, 15, 20), segment=INDEX, continuous=True)           # ... not continuous
    assert is_market_hours(at(PRE, 15, 20), segment=INDEX, continuous=True)
    cal = MarketCalendar()
    assert cal.last_close_utc(POST) == at(POST, 15, 40) and cal.last_close_utc(PRE) == at(PRE, 15, 30)
    assert cal.target_session(at(POST, 15, 39)) == POST and cal.target_session(at(POST, 15, 41)) == date(2026, 11, 17)


# ---------------- 2. auction ticks are not trades ----------------
def _hub(settings=None, feed="breeze"):
    st = Store(":memory:")
    return MarketHub(settings or sim_settings(), st, feed, MarketCalendar()), st


def _tick(sym, when, last, bid=None, ask=None, vol=None):
    return Tick(sym, last, bid=bid, ask=ask, cum_volume=vol, source="breeze", received_at=when)


def test_fno_stock_ticks_in_the_auction_are_indications_never_a_quality_event_bar_or_fill():
    hub, st = _hub()
    hub.on_tick(_tick("RELIANCE", at(POST, 15, 14, 50), 321.0, 320.95, 321.05, 1000))
    hub.on_tick(_tick("NIFTY", at(POST, 15, 14, 50), 24000.0))
    # the 17-Nov pattern: last frozen, bid and ask crossed about 3% apart - an auction order book
    crossed = _tick("RELIANCE", at(POST, 15, 21, 21), 321.05, 330.40, 321.05, 900)
    hub.on_tick(crossed)
    hub.on_tick(_tick("NIFTY", at(POST, 15, 21, 21), 24123.0))
    now = at(POST, 15, 21, 30)
    assert st.quality_event_count_since(at(POST, 15, 0)) == 0               # no CROSSED_QUOTE, no VOLUME_WENT_BACK
    assert hub.stats()["auction_ticks"] == 2 and hub.stats()["quarantined_ticks"] == 0
    rows = {r["symbol"]: r for r in hub.snapshot(now)}
    assert rows["RELIANCE"]["status"] == "AUCTION" and rows["RELIANCE"]["last"] == 321.05 and rows["RELIANCE"]["bid"] == 330.40
    assert rows["RELIANCE"]["usable"] is False
    assert rows["NIFTY"]["status"] == "AUCTION" and rows["NIFTY"]["last"] == 24123.0 and not rows["NIFTY"]["usable"]
    assert hub.quote("RELIANCE", now)["status"] == "AUCTION" and hub.quote("RELIANCE", now)["last"] == 321.0   # last TRADE
    assert hub.live_quote("RELIANCE", now) is None
    assert hub.first_quote("RELIANCE", at(POST, 15, 15)) is None                # never a paper fill
    hub.close()
    bars = st.bars_1m_between("RELIANCE", at(POST, 15, 0), at(POST, 15, 40))
    assert [b.start for b in bars] == [at(POST, 15, 14)] and bars[0].close == 321.0        # no auction bar


def test_before_3_aug_the_same_tick_is_a_trade_quote_and_still_quarantined():
    hub, st = _hub()
    hub.on_tick(_tick("RELIANCE", at(PRE, 15, 21, 21), 321.05, 330.40, 321.05))
    assert st.quality_event_count_since(at(PRE, 15, 0)) == 1 and hub.stats()["auction_ticks"] == 0
    hub.on_tick(_tick("RELIANCE", at(PRE, 15, 22), 321.0, 320.9, 321.1))
    assert {r["symbol"]: r for r in hub.snapshot(at(PRE, 15, 22, 5))}["RELIANCE"]["status"] == "LIVE"


def test_a_non_fno_stock_trades_on_to_1530():
    raw = sim_settings().model_dump()
    for i in raw["watchlist"]:
        if i["symbol"] == "RELIANCE":
            i["fno"] = False
    hub, st = _hub(Settings.model_validate(raw))
    hub.on_tick(_tick("RELIANCE", at(POST, 15, 25), 321.0, 320.9, 321.1))
    assert {r["symbol"]: r for r in hub.snapshot(at(POST, 15, 25, 5))}["RELIANCE"]["status"] == "LIVE"
    hub.on_tick(_tick("RELIANCE", at(POST, 15, 26), 321.05, 330.40, 321.05))
    assert st.quality_event_count_since(at(POST, 15, 0)) == 1                # a crossed TRADE quote: quarantined


def test_sim_feed_has_no_auction_like_it_has_no_market_hours():
    hub, st = _hub(feed="sim")
    hub.on_tick(_tick("RELIANCE", at(POST, 15, 21), 321.0, 320.9, 321.1))
    assert {r["symbol"]: r for r in hub.snapshot(at(POST, 15, 21, 5))}["RELIANCE"]["status"] == "LIVE"


def test_the_dashboard_shows_auction():
    from app.page import PAGE_HTML
    assert ".AUCTION{" in PAGE_HTML and "closing auction: an auction indication" in PAGE_HTML


# ---------------- kill switch and exit calls in the auction ----------------
def test_kill_switch_in_the_auction_says_outside_market_hours_only_so_exit_calls_carry_on():
    from app.risk.engine import RiskService
    from app.risk.limits import kill_switch
    from tests.test_phase5_api import FakeHub
    s = sim_settings()
    svc = RiskService(s, Store(":memory:"), FakeHub({"NIFTY": "AUCTION", "RELIANCE": "AUCTION"}), MarketCalendar())
    m = svc.market_state("RELIANCE", at(POST, 15, 20))
    assert m.market_open is False                                            # no new signal in the auction
    ks = kill_switch("NIFTY", m.index_status, m.quarantined_recent, m.market_open, s.risk.kill_switch)
    assert ks == ["outside market hours (continuous trading; the closing auction is outside them)"]
    assert [r for r in ks if "market hours" not in r] == []                  # what exit calls see: nothing
    m2 = svc.market_state("RELIANCE", at(POST, 15, 10))
    assert m2.market_open is True
    assert "NIFTY price is AUCTION, not LIVE" in kill_switch("NIFTY", "AUCTION", 0, True, s.risk.kill_switch)


# ---------------- 3. cash intraday cutoff, holding exits ----------------
def _pos(trade_type, horizon, symbol="RELIANCE", legs=None, strategy_id=None, entry=POST):
    from app.journal import Position
    return Position("p1", "MANUAL", None, trade_type, "CASH_INDEX", symbol, "LONG", legs, 10, 100.0,
                    at(entry, 10, 0), strategy_id=strategy_id, horizon=horizon)


def test_intraday_time_exit_is_the_unconfirmed_1500_cutoff():
    from app.positions import time_exit_at
    s = sim_settings()
    due, why = time_exit_at(_pos("INTRADAY_STOCKS", "INTRADAY"), s, MarketCalendar())
    assert due == at(POST, 15, 0)
    assert why == "intraday: out by 15:00 IST (ICICI's own intraday square-off time after the closing auction is not confirmed)"
    assert s.session.cash_intraday_cutoff_confirmed is False


def test_swing_holding_exit_is_the_last_continuous_bar_of_the_stock_or_the_options():
    from app.journal import Leg
    from app.positions import time_exit_at
    s = sim_settings()
    due, _ = time_exit_at(_pos("SWING_STOCKS", "SWING", strategy_id="swing_pullback_v2"), s, MarketCalendar())
    assert due.astimezone(IST).time() == time(15, 10)                       # F&O stock: the 15:10 bar from 3 Aug
    due, _ = time_exit_at(_pos("SWING_STOCKS", "SWING", strategy_id="swing_pullback_v2", entry=date(2026, 7, 10)),
                          s, MarketCalendar())
    assert due.astimezone(IST).time() == time(15, 25)                       # before 3 Aug: unchanged
    legs = (Leg("BUY", "CE", 1000.0, date(2027, 1, 28), 1, 500, 10.0),)
    due, _ = time_exit_at(_pos("STOCK_OPTION_BUYING", "SWING", legs=legs, strategy_id="long_option"), s, MarketCalendar())
    assert due.astimezone(IST).time() in (time(15, 35), time(15, 15))       # options: the 15:35 bar (or the expiry rule)


def test_no_cash_intraday_signal_from_the_cutoff_and_none_at_all_in_the_auction():
    from tests.test_phase6_engine import BreakoutFixture, FixedRegime, make, seed, tick
    for bar_end, expect in ((at(POST, 14, 55), True), (at(POST, 15, 0), False), (at(POST, 15, 20), False)):
        c, eng = make()
        with c:
            px = seed(c.app, until=bar_end)
            tick(c.app, bar_end - timedelta(seconds=1), px)
            out = eng.on_bar(bar_end, bar_end + timedelta(seconds=3))
            made = any(x["strategy_id"] == "orb_v2" for x in out["stored"])
            assert made is expect, bar_end.astimezone(IST)


def test_the_loop_processes_bars_to_1540_for_option_exits_and_to_1530_before_3_aug():
    from tests.test_phase6_engine import make
    c, eng = make()
    with c:
        eng.last_bar = at(POST, 15, 35)
        assert eng.step(at(POST, 15, 40, 2)) is not None                    # the 15:40 bar: exits and paper
        eng.last_bar = at(POST, 15, 40)
        assert eng.step(at(POST, 15, 45, 2)) is None
    c, eng = make()
    with c:
        eng.last_bar = at(PRE, 15, 30)
        assert eng.step(at(PRE, 15, 35, 2)) is None


# ---------------- 4. chain capture 14:50-15:15, each chain stamped with its own fetch time ----------------
class SlowClock:
    """Breeze's real speed: each chain call takes `per_call` seconds."""

    def __init__(self, start, per_call=7.5):
        self.t, self.per_call = start, timedelta(seconds=per_call)

    def __call__(self):
        return self.t


def _capture(tmp_path, start, per_call, stocks=("RELIANCE", "TCS")):
    from tests.test_after_close import FakeChainSource, _job, raw
    job, s, store, j, src = _job(tmp_path, rows=[raw(1000.0, 5.0, 5.2)])
    clk = SlowClock(start, per_call)
    fetch = src.fetch

    def slow_fetch(inst, spec, right):
        clk.t += clk.per_call
        return fetch(inst, spec, right)
    src.fetch = slow_fetch
    return job, s, store, j, src, clk


def test_chain_capture_is_due_1450_to_1515_and_the_shortlist_after_1540(tmp_path):
    from tests.test_after_close import _job
    job, *_ = _job(tmp_path)
    assert job.due(at(POST, 14, 49)) is None and job.due(at(POST, 14, 50)) == "chains"
    assert job.due(at(POST, 15, 14)) == "chains" and job.due(at(POST, 15, 15)) is None
    assert job.due(at(POST, 15, 40)) is None and job.due(at(POST, 15, 41)) == "finish"


def test_each_chain_has_its_own_fetch_time_and_the_capture_stops_at_1515_the_rest_missed(tmp_path, monkeypatch):
    import app.options.after_close as ac
    job, s, store, j, src, clk = _capture(tmp_path, at(POST, 15, 14, 30), per_call=10)
    monkeypatch.setattr(ac, "utcnow", clk)
    st = job.run(clk())
    ch = st["steps"]["chains"]
    # 15:14:30: chain 1 (CE to 15:14:40, PE to 15:14:50), chain 2 from 15:14:50 (CE to 15:15:00): its PE is not
    # asked at 15:15, and the other two chains are missed
    rows = store._db.execute("SELECT underlying, expiry, right, as_of_utc FROM option_chain ORDER BY as_of_utc").fetchall()
    per_chain = {}
    for u, e, r, a in rows:
        per_chain.setdefault((u, e), set()).add(a)
    assert all(len(v) == 1 for v in per_chain.values())                     # one snapshot per expiry chain
    times = sorted(next(iter(v)) for v in per_chain.values())
    assert len(set(times)) == len(times) >= 2                               # each chain its own time ...
    assert all(datetime.fromisoformat(x) < at(POST, 15, 15) for x in times)  # ... all before the stop
    assert ch["missed"] and ch["ok"] is False and ch["stop_utc"] == at(POST, 15, 15).isoformat()
    assert len(src.calls) < 8                                               # nothing fetched at or after 15:15
    from app.options.after_close import summary_lines
    done = job.run(at(POST, 15, 41))
    text = "\n".join(summary_lines(s, j, POST, at(POST, 15, 45)))
    assert "expiry chains MISSED (not fetched by 15:15)" in text


def test_at_breezes_real_speed_120_calls_from_1450_finish_before_1515(tmp_path):
    """7-9 replies a minute (17-Nov): 120 calls at 8 a minute = 15 minutes: 14:50 -> about 15:05."""
    calls = 120
    assert at(POST, 14, 50) + timedelta(minutes=calls / 7) < at(POST, 15, 15)     # even at 7 a minute (17.1 min)


# ---------------- 5. the official close: wait for Breeze's daily bar ----------------
def _five_bars(store, sym, d, end=time(15, 30), px=1000.0):
    t, out = at(d, 9, 15), []
    while t < at(d, end.hour, end.minute):
        out.append(Bar(sym, t, px, px + 1, px - 1, px, 100))
        t += timedelta(minutes=5)
        px += 1
    store.save_history_bars("bars_5m", out, "sim")
    return out


def test_the_5_minute_fallback_uses_continuous_bars_only_and_is_labelled_approximate():
    from app.options.after_close import session_bar_from_5m
    st = Store(":memory:")
    _five_bars(st, "RELIANCE", POST)                                        # 75 bars incl. the auction ones
    bar, how = session_bar_from_5m(st, "sim", "RELIANCE", POST, CASH_FNO)
    assert bar is not None and how.startswith("APPROXIMATE: built from 72 stored 5-minute bars to 15:15")
    assert bar.close == 1000.0 + 71                                         # the 15:10 bar, not the 15:25 auction bar
    _five_bars(st, "TCS", PRE)
    bar, how = session_bar_from_5m(st, "sim", "TCS", PRE, CASH_FNO)
    assert bar.close == 1000.0 + 74 and "75 stored 5-minute bars to 15:30" in how          # before 3 Aug: 75


def test_after_close_waits_for_the_daily_bar_and_builds_an_approximate_close_only_at_the_deadline(tmp_path, monkeypatch):
    from tests.test_after_close import FakeHistory, _job
    job, s, store, j, src = _job(tmp_path, history_calls=1)
    monkeypatch.setattr(FakeHistory, "update", lambda self, now, symbols=None: {})   # today's bars not published
    for sym in ("NIFTY", "RELIANCE", "TCS"):
        _five_bars(store, sym, POST)                                        # every 5-minute bar is there
    job.run(at(POST, 14, 50))
    st = job.run(at(POST, 15, 41))
    assert st["status"] == "WAITING" and st["session_bars"]["from_5m"] == []          # it waits for the daily bar
    assert "official close" in st["session_bars"]["missing"]["NIFTY"]
    st = job.run(at(POST, 17, 26))                                          # not the last try: 17:30 is
    assert st["status"] == "WAITING" and st["next_retry_utc"] == at(POST, 17, 30).isoformat()
    st = job.run(at(POST, 17, 30))                                          # the last try, at the deadline
    assert st["status"] == "DONE" and st["session_bars"]["approximate"] == ["NIFTY", "RELIANCE", "TCS"]
    from app.options.after_close import summary_lines
    text = "\n".join(summary_lines(s, j, POST, at(POST, 17, 30)))
    assert "APPROXIMATE close for 3 (no daily bar by 17:30: the last continuous trade" in text


def test_session_close_marks_fno_stock_positions_at_the_official_close_or_says_approximate():
    from app.history import daily_start
    from tests.test_phase6_engine import make
    from tests.test_phase6_recovery import add_position
    c, eng = make()
    with c:
        add_position(c, stop=900.0)                                           # RELIANCE 10 shares at 1,060
        day = POST
        assert eng.close_session(day, at(day, 15, 42)) is None                # waits for Breeze's daily bar
        assert json.loads(eng.j.get_state(f"equity_close_waiting:{day}"))["waiting_for"] == ["RELIANCE"]
        eng.store.save_history_bars("bars_1d", [Bar("RELIANCE", daily_start(day), 1060, 1080, 1050, 1075.0, 1)], "sim")
        out = eng.close_session(day, at(day, 16, 0))
        assert out["CASH_INDEX"]["note"] == "close: official closing-auction close (Breeze daily bar) for RELIANCE"
        state = json.loads(eng.j.get_state(f"equity_closed:{day}"))
        assert "official" in state["note"]
    c, eng = make()
    with c:
        pid = add_position(c, stop=900.0)
        eng.j.save_mark(pid, at(POST, 15, 10), 12.5)                           # the last mark: a continuous trade
        out = eng.close_session(POST, at(POST, 17, 31))                       # past the deadline, no daily bar
        assert out["CASH_INDEX"]["note"].startswith("close APPROXIMATE: no Breeze daily bar (official closing-auction "
                                                    "close) by 17:30 for RELIANCE")


def test_daily_bars_are_final_only_after_the_last_close():
    from app.history import is_complete, daily_start
    b = Bar("RELIANCE", daily_start(POST), 1, 1, 1, 1, 1)
    assert not is_complete(b, "1day", at(POST, 15, 36)) and is_complete(b, "1day", at(POST, 15, 40))
    b = Bar("RELIANCE", daily_start(PRE), 1, 1, 1, 1, 1)
    assert is_complete(b, "1day", at(PRE, 15, 30))                           # before 3 Aug: unchanged


# ---------------- options: expiry and deadlines at the options' close ----------------
def test_options_expire_and_the_forced_exit_is_due_at_the_options_close():
    from app.options.greeks import years_to_expiry
    from app.options.backtest import session_end
    assert years_to_expiry(at(POST, 15, 39), POST) == pytest.approx(60 / (365 * 86400))
    assert years_to_expiry(at(PRE, 15, 29), PRE) == pytest.approx(60 / (365 * 86400))
    assert session_end(POST) == at(POST, 15, 40) and session_end(PRE) == at(PRE, 15, 30)


# ---------------- 6. backtests: no entry or exit on an F&O stock bar inside the auction ----------------
from app.strategies.base import Strategy  # noqa: E402


class EveryBar(Strategy):
    """A fixture strategy: LONG on every bar, wide stop and target (only the time exit closes it)."""
    id = "orb_v2"
    base_version = "fixture-every-bar"
    interval = "5minute"
    allowed_regimes = frozenset({"TRENDING_UP"})

    def __init__(self):
        from pydantic import BaseModel

        class P(BaseModel):
            k: int = 1
        super().__init__(P())

    def _evaluate(self, ctx):
        last = ctx.intraday[-1]
        return [self.signal(ctx, "LONG", last.close, last.close * 0.5, last.close * 2,
                            ctx.asof + timedelta(minutes=10), ["fixture"])]


def _bt_store(days):
    st = Store(":memory:")
    for d in days:
        for sym in ("NIFTY", "RELIANCE"):
            _five_bars(st, sym, d)
            st.save_history_bars("bars_1d", [Bar(sym, datetime.combine(d, time(9, 15), IST).astimezone(timezone.utc),
                                                 1000, 1100, 900, 1000, 1)], "sim")
    return st


def _run(settings, store, days):
    from collections import Counter
    from app.backtest import Engine
    from app.regime import PointInTimeRegime

    class AnyRegime:
        def at_bar(self, t):
            return type("R", (), {"regime": "TRENDING_UP"})()

        def at_close(self, d):
            return self.at_bar(d)
    eng = Engine(settings, store, "sim", MarketCalendar(), regime=AnyRegime())
    sk = Counter()
    return eng.run_intraday(EveryBar(), "RELIANCE", "stock", days, sk), sk


def _trade_times(trades):
    return [(t.entry_time, t.exit_time, t.exit_reason) for t in trades]


@pytest.mark.skipif(load_settings().costs is None, reason="needs cost rates")
def test_backtest_before_3_aug_is_identical_with_or_without_the_auction_calendar_and_after_it_never_trades_cas():
    s = sim_settings()
    days_pre, days_post = [date(2026, 7, 29), date(2026, 7, 30)], [date(2026, 8, 4), date(2026, 8, 5)]
    store = _bt_store(days_pre + days_post)
    with_cas, _ = _run(s, store, days_pre + days_post)
    S.configure(SessionCfg(periods=[SessionPeriod(from_date=date(2000, 1, 1))]))          # the old, single session
    without, _ = _run(s, store, days_pre + days_post)
    S.configure(s.session)
    pre = lambda ts: [x for x in _trade_times(ts) if x[0][:10] < "2026-08-03"]          # noqa: E731
    assert pre(with_cas) == pre(without) and pre(with_cas)                   # before 3 Aug: identical
    # with the defaults (exit at the close of the bar ending 15:15) the post-3-Aug trades are identical too
    assert _trade_times(with_cas) == _trade_times(without)
    for e, x, _ in _trade_times(with_cas):
        assert datetime.fromisoformat(x).astimezone(IST).time() <= time(15, 15)
        assert datetime.fromisoformat(e).astimezone(IST).time() < time(15, 15)


@pytest.mark.skipif(load_settings().costs is None, reason="needs cost rates")
def test_backtest_never_enters_or_exits_on_a_bar_outside_continuous_trading():
    """A calendar whose continuous trading ends at 14:00 on the post-3-Aug days: every trade exits by 14:00 and none
    enters on or after it; the pre-3-Aug days still exit at the configured 15:15."""
    s = sim_settings()
    days = [date(2026, 7, 30), date(2026, 8, 4)]
    store = _bt_store(days)
    S.configure(SessionCfg(periods=[SessionPeriod(from_date=date(2000, 1, 1)),
                                    SessionPeriod(from_date=date(2026, 8, 3), fno_continuous_end="14:00",
                                                  fno_close="15:35", index_continuous_end="15:15", index_close="15:35",
                                                  derivatives_close="15:40", cash_intraday_cutoff="13:30")]))
    trades, _ = _run(s, store, days)
    by_day = {}
    for e, x, why in _trade_times(trades):
        by_day.setdefault(e[:10], []).append((datetime.fromisoformat(e).astimezone(IST).time(),
                                               datetime.fromisoformat(x).astimezone(IST).time(), why))
    assert max(x for _, x, _ in by_day["2026-08-04"]) == time(14, 0)
    assert all(e < time(14, 0) for e, _, _ in by_day["2026-08-04"])
    assert max(x for _, x, _ in by_day["2026-07-30"]) == time(15, 15)
