"""The kill switch counts only problems in live price ticks of stocks and indices (app.quality.TICK_REASONS):
crossed quotes, price jumps, volume going back, bad prices, volume or previous close. Option chain strikes with a zero
or missing bid/ask are stored as unpriced and counted in the chain summary only (no quality event); history bars with
bad values are logged and rejected, not counted. The 17-Nov-2026 15:20 case: 21 NIFTY chain rows + 3 volume ticks +
1 JIOFIN bar = 25 rows, of which only 3 are ticks."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.calendar import MarketCalendar
from app.history import HistoryLoader
from app.models import Tick
from app.options.chain import ChainLoader
from app.options.instruments import ContractSpec, InstrumentBook
from app.quality import IST, TICK_REASONS
from app.risk.engine import RiskService
from app.store import Store
from tests.test_after_close import EXP, EXP_NEXT, LOT, STOCKS, FakeChainSource, _settings, raw
from tests.test_phase5_api import Cal, FakeHub
from tests.test_step1 import sim_settings

pytestmark = pytest.mark.usefixtures("fixed_clock")

NOW = datetime(2026, 11, 17, 15, 20, 1, tzinfo=IST).astimezone(timezone.utc)      # the 17-Nov kill switch moment


def _svc(store):
    return RiskService(sim_settings(), store, FakeHub({"NIFTY": "LIVE", "RELIANCE": "LIVE"}), Cal())


def test_tick_reasons_are_the_live_tick_checks_only():
    assert TICK_REASONS == {"BAD_PRICE", "CROSSED_QUOTE", "PRICE_JUMP", "VOLUME_WENT_BACK", "BAD_VOLUME",
                            "BAD_PREV_CLOSE"}
    assert not {"BAD_OPTION_QUOTE", "BAD_BAR"} & TICK_REASONS


def test_the_17_nov_window_counts_3_ticks_not_25_rows_and_the_switch_stays_off():
    st = Store(":memory:")
    for k in range(21):                                   # the NIFTY option chain refresh (logged before this change)
        st.save_quality_event(NOW - timedelta(minutes=4), "NIFTY", "BAD_OPTION_QUOTE", 0.05, 0.0, 0.05,
                              f"breeze:chain:2026-11-17:{23950 + 50 * k}PE:missing bid/ask")
    for sym in ("BAJAJFINSV", "GRASIM", "MAXHEALTH"):
        st.save_quality_event(NOW - timedelta(minutes=4), sym, "VOLUME_WENT_BACK", 1.0, 1.0, 1.1, "breeze")
    st.save_quality_event(NOW - timedelta(seconds=7), "JIOFIN", "BAD_BAR", 226.0, None, None,
                          "breeze:5minute:negative volume")
    assert st.quality_event_count_since(NOW - timedelta(minutes=5)) == 25
    svc = _svc(st)
    assert svc.market_state("RELIANCE", NOW).quarantined_recent == 3
    # 15:20:01 is inside the closing auction since 3 Aug 2026: the switch is on for that alone, not for a spike
    assert svc.status(NOW)["kill_switch"]["reasons"] == [
        "outside market hours (continuous trading; the closing auction is outside them)"]


def test_crossed_quotes_in_live_ticks_still_trip_it():
    st = Store(":memory:")
    for k in range(20):
        st.save_quality_event(NOW - timedelta(seconds=30 + k), "NTPC", "CROSSED_QUOTE", 321.05, 330.4, 311.2, "breeze")
    ks = _svc(st).status(NOW)["kill_switch"]
    assert ks["on"] is True and any("20 ticks quarantined" in r for r in ks["reasons"])


def test_the_hub_quarantines_a_crossed_tick_as_a_counted_tick_event():
    from app.hub import MarketHub
    st = Store(":memory:")
    hub = MarketHub(sim_settings(), st, "breeze", MarketCalendar())
    # in continuous trading (15:10): a crossed quote is a quarantined trade quote; at 15:20 it would be an auction
    # indication (tests/test_closing_auction.py)
    hub.on_tick(Tick("NTPC", 321.05, bid=330.4, ask=311.2, received_at=NOW - timedelta(minutes=10), source="breeze"))
    assert st.recent_quality_events(5)[0]["reason"] == "CROSSED_QUOTE"
    assert st.quality_event_count_since(NOW - timedelta(minutes=15), TICK_REASONS) == 1


def test_the_1520_capture_with_thousands_of_unpriced_strikes_does_not_trip_it(tmp_path):
    s = _settings(tmp_path)
    st = Store(":memory:")
    strikes = tuple(float(k) for k in range(800, 1501, 10))
    InstrumentBook(st, "sim").save([ContractSpec(sym, e, LOT, "monthly", strikes, NOW) for sym in STOCKS
                                    for e in (EXP, EXP_NEXT)])
    rows = [raw(1000, 9.5, 10.5)] + [raw(k, 0, 0) for k in range(801, 1401, 2)] + [raw(1490, None, 7.0)]
    out = ChainLoader(s, st, FakeChainSource(rows), InstrumentBook(st, "sim")).load(STOCKS, now=NOW)
    assert sum(v["no_bid_ask"] for v in out.values()) == 8 * 301                 # 2,408 unpriced, counted here only
    assert st.recent_quality_events(10) == []                                  # no quality event for them
    unpriced = [r for r in st.latest_option_chain("sim", "RELIANCE", EXP) if r["bid"] is None]
    assert len(unpriced) == 2 * 301                                            # stored as unpriced (both sides)
    assert not any("quarantined" in r for r in _svc(st).status(NOW + timedelta(seconds=30))["kill_switch"]["reasons"])


def test_a_crossed_chain_strike_is_stored_unpriced_logged_but_never_counted(tmp_path):
    s = _settings(tmp_path)
    st = Store(":memory:")
    InstrumentBook(st, "sim").save([ContractSpec("RELIANCE", EXP, LOT, "monthly", (1000.0,), NOW)])
    ChainLoader(s, st, FakeChainSource([raw(1000, 8.0, 6.0)] * 30), InstrumentBook(st, "sim")).load(["RELIANCE"], now=NOW)
    ev = st.recent_quality_events(100)
    assert ev and all(e["reason"] == "BAD_OPTION_QUOTE" for e in ev)
    assert st.quality_event_count_since(NOW - timedelta(minutes=5), TICK_REASONS) == 0


def test_the_index_refresh_also_stores_unpriced_strikes_without_quality_events(tmp_path):
    s = _settings(tmp_path)
    st = Store(":memory:")
    InstrumentBook(st, "sim").save([ContractSpec("RELIANCE", EXP, LOT, "monthly", (1000.0, 1010.0), NOW)])
    out = ChainLoader(s, st, FakeChainSource([raw(1000, 9.5, 10.5), raw(1010, 0, 0.05)]),
                      InstrumentBook(st, "sim")).load(["RELIANCE"], now=NOW)        # the default load (index refresh)
    assert next(iter(out.values()))["no_bid_ask"] == 2 and st.recent_quality_events(10) == []
    got = {(r["strike"], r["right"]): r["bid"] for r in st.latest_option_chain("sim", "RELIANCE", EXP)}
    assert got[(1010.0, "PE")] is None and got[(1000.0, "PE")] == 9.5


def test_a_bad_history_bar_is_logged_and_rejected_not_counted(tmp_path):
    class Src:
        name, remote = "breeze", False

        def fetch(self, inst, interval, a, b):
            day = datetime(2026, 11, 17, 9, 15, tzinfo=IST)
            if inst.symbol != "RELIANCE" or interval != "5minute" or not (a <= day < b):
                return []                                  # only the chunk holding 17-Nov has bars
            return [{"datetime": (day + timedelta(minutes=5 * k)).strftime("%Y-%m-%d %H:%M:%S"), "open": 100,
                     "high": 101, "low": 99, "close": 100, "volume": -5 if k == 1 else 10} for k in range(3)]
    s = _settings(tmp_path)
    st = Store(":memory:")
    HistoryLoader(s, st, Src(), MarketCalendar()).update(NOW, symbols={"RELIANCE"})
    ev = [e for e in st.recent_quality_events(20) if e["symbol"] == "RELIANCE"]
    assert [e["reason"] for e in ev] == ["BAD_BAR"]                            # logged
    starts = [b.start.astimezone(IST).strftime("%H:%M") for b in st.history_bars("bars_5m", "RELIANCE", "breeze")]
    assert "09:20" not in starts and "09:15" in starts                         # rejected, the others kept
    assert st.quality_event_count_since(NOW - timedelta(hours=8), TICK_REASONS) == 0   # not counted
    assert date(2026, 11, 17) == NOW.astimezone(IST).date()
