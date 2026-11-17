from datetime import date, datetime, timedelta, timezone

from app.calendar import MarketCalendar
from tests.fixture_settings import load_settings
from app.history import (BAD_BAR, DAILY, INTRADAY, BreezeHistorySource, HistoryLoader,
                         SimHistorySource, bar_date, is_complete, parse_rows, seed_sim_history)
from app.models import Bar
from app.store import Store

T0 = datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc)   # Wed 10:30 IST


def test_parse_converts_ist_to_utc_and_keys_daily_bars():
    rows = [{"datetime": "2026-11-10 09:15:00", "open": "100", "high": "101", "low": "99",
             "close": "100.5", "volume": "1200"}]
    bars, bad = parse_rows(rows, "RELIANCE", "stock", INTRADAY)
    assert bad == [] and bars[0].start == datetime(2026, 11, 10, 3, 45, tzinfo=timezone.utc)
    assert bars[0].volume == 1200
    daily, _ = parse_rows([{**rows[0], "datetime": "2026-11-10 00:00:00"}], "RELIANCE", "stock", DAILY)
    assert daily[0].start == datetime(2026, 11, 10, 3, 45, tzinfo=timezone.utc)
    assert bar_date(daily[0]) == date(2026, 11, 10)


def test_parse_rejects_bad_rows_and_keeps_unknown_volume_unknown():
    ok = {"datetime": "2026-11-10 09:15:00", "open": 10, "high": 11, "low": 9, "close": 10}
    rows = [ok,
            {**ok, "datetime": "2026-11-10 09:20:00", "close": ""},
            {**ok, "datetime": "2026-11-10 09:25:00", "high": 9.5},          # high below open
            {**ok, "datetime": "2026-11-10 09:30:00", "low": 0},
            {**ok, "datetime": None}]
    bars, bad = parse_rows(rows, "X", "stock", INTRADAY)
    assert len(bars) == 1 and len(bad) == 4
    assert bars[0].volume is None                  # missing volume is not zero
    idx, _ = parse_rows([{**ok, "volume": 0}], "NIFTY", "index", INTRADAY)
    assert idx[0].volume is None


def test_incomplete_bars_are_not_complete():
    today = Bar("X", datetime(2026, 11, 11, 3, 45, tzinfo=timezone.utc), 1, 1, 1, 1)
    assert not is_complete(today, DAILY, T0)                               # 10:30 IST
    assert not is_complete(today, DAILY, datetime(2026, 11, 11, 10, 5, tzinfo=timezone.utc))   # 15:35: options trade
    assert is_complete(today, DAILY, datetime(2026, 11, 11, 10, 10, tzinfo=timezone.utc))      # 15:40: every close
    forming = Bar("X", T0 - timedelta(minutes=3), 1, 1, 1, 1)
    assert not is_complete(forming, INTRADAY, T0)
    assert is_complete(forming, INTRADAY, T0 + timedelta(minutes=2))


class RecordingSource:
    """Fake source wrapping the sim generator, recording every call; one symbol fails
    and one row per call is broken."""
    name = "breeze"

    def __init__(self, fail_symbol=None):
        self.sim = SimHistorySource()
        self.calls = []
        self.fail_symbol = fail_symbol

    def fetch(self, inst, interval, start, end):
        self.calls.append((inst.symbol, interval, start, end))
        if inst.symbol == self.fail_symbol:
            raise ConnectionError("network down")
        rows = self.sim.fetch(inst, interval, start, end)
        if rows:
            rows.append({**rows[0], "close": -1})   # a bad row
        return rows


def test_loader_stores_daily_and_5min_for_every_symbol_including_vix():
    s = load_settings()
    store = Store(":memory:")
    src = RecordingSource()
    summary = HistoryLoader(s, store, src).update(T0)
    counts = store.history_counts("breeze")
    assert set(counts) == {i.symbol for i in s.watchlist} and "INDIAVIX" in counts
    assert all(c["bars_1d"] > 250 and c["bars_5m"] > 0 for c in counts.values())
    assert all("error" not in v for v in summary.values())
    # no bar for today (2026-11-11) because the session is not finished
    last = store.history_bars("bars_1d", "NIFTY", "breeze", limit=1)[0]
    assert bar_date(last) == date(2026, 11, 10)
    # 5-minute requests are split into chunks of at most 10 days
    five = [c for c in src.calls if c[0] == "NIFTY" and c[1] == INTRADAY]
    assert all(c[3] - c[2] <= timedelta(days=10) for c in five)
    # bad rows were quarantined, not stored
    assert any(e["reason"] == BAD_BAR for e in store.recent_quality_events(100))


def test_loader_skips_holidays_and_continues_after_a_failure():
    s = load_settings()
    store = Store(":memory:")
    cal = MarketCalendar({date(2026, 11, 9): "test holiday"})
    summary = HistoryLoader(s, store, RecordingSource(fail_symbol="TCS"), cal).update(T0)
    assert "error" in summary["TCS:1day"] and "error" not in summary["INFY:1day"]
    days = {bar_date(b) for b in store.history_bars("bars_1d", "INFY", "breeze")}
    assert date(2026, 11, 9) not in days and date(2026, 11, 10) in days


def test_second_update_only_fetches_from_last_stored_day():
    s = load_settings()
    store = Store(":memory:")
    src = RecordingSource()
    loader = HistoryLoader(s, store, src)
    loader.update(T0)
    src.calls.clear()
    loader.update(T0 + timedelta(days=1))
    first_daily = [c for c in src.calls if c[0] == "NIFTY" and c[1] == DAILY][0]
    assert first_daily[2] >= datetime(2026, 11, 9, tzinfo=timezone.utc)


def test_sim_history_is_deterministic():
    inst = load_settings().watchlist[0]
    a = SimHistorySource().fetch(inst, DAILY, T0 - timedelta(days=30), T0)
    b = SimHistorySource().fetch(inst, DAILY, T0 - timedelta(days=90), T0)
    assert a == b[-len(a):]


def test_seed_sim_history_never_stores_today():
    s = load_settings()
    store = Store(":memory:")
    seed_sim_history(s, store, MarketCalendar(), now=T0)
    assert bar_date(store.history_bars("bars_1d", "NIFTY", "sim", limit=1)[0]) == date(2026, 11, 10)
    assert store.history_counts("breeze") == {}           # sim never mixes with real data


def test_breeze_source_sends_utc_iso_dates():
    class FakeFeed:
        def historical(self, inst, interval, from_iso, to_iso):
            self.args = (interval, from_iso, to_iso)
            return []
    feed = FakeFeed()
    BreezeHistorySource(feed).fetch(load_settings().watchlist[0], DAILY, T0 - timedelta(days=1), T0)
    assert feed.args == ("1day", "2026-11-10T05:00:00.000Z", "2026-11-11T05:00:00.000Z")
