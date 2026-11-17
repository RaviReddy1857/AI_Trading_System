"""Regression tests for the independent review of phases 1-2 (findings 1-5).
Each test fails on the code before the fix and passes after it."""
import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.calendar import MarketCalendar
from app.config import Regime as RegimeParams
from app.config import Settings
from tests.fixture_settings import load_settings
from app.feeds.breeze_feed import tick_from_breeze
from app.history import INTRADAY, parse_rows
from app.hub import MarketHub
from app.indicators import breadth, daily_returns
from app.main import create_app, write_request_problem
from app.manual_data import ManualData
from app.models import Bar, Tick
from app.quality import BAD_PRICE, check_tick
from app.regime import DailyBar, RegimeService, classify
from app.store import SimDataInLiveStore, Store
from tests.phase2_fixtures import SESSION, T0, live_ticks, make_inputs, settings_with_tmp_data, store_history

INF, NAN = float("inf"), float("nan")
P = RegimeParams()


# ---------------- 1. non-finite prices ----------------
@pytest.mark.parametrize("kw", [{"last": INF}, {"last": -INF}, {"last": NAN},
                                {"last": 100.0, "bid": INF}, {"last": 100.0, "ask": INF},
                                {"last": 100.0, "bid": NAN, "ask": 100.1},
                                {"last": 100.0, "bid": INF, "ask": INF}])
@pytest.mark.usefixtures("fixed_clock")
def test_non_finite_tick_is_quarantined(kw):
    last = kw.pop("last")
    assert check_tick(Tick("X", last, **kw), None, 5) == [BAD_PRICE]


def test_non_finite_tick_never_reaches_prices(tmp_path):
    hub = MarketHub(settings_with_tmp_data(tmp_path), Store(":memory:"), "breeze")
    hub.on_tick(Tick("RELIANCE", INF, received_at=T0))
    row = next(r for r in hub.snapshot(T0) if r["symbol"] == "RELIANCE")
    assert row["last"] is None and row["status"] == "NO_DATA"
    assert hub.stats()["quarantined_ticks"] == 1


def test_non_finite_extras_from_feed_are_ignored(tmp_path):
    hub = MarketHub(settings_with_tmp_data(tmp_path), Store(":memory:"), "breeze")
    # An infinite previous close is now quarantined outright (second review, finding 3).
    hub.on_tick(Tick("NIFTY", 23500.0, prev_close=INF, received_at=T0))
    row = next(r for r in hub.snapshot(T0) if r["symbol"] == "NIFTY")
    assert row["last"] is None and row["change_pct"] is None   # never inf, which breaks JSON
    # An infinite day high is ignored; the range falls back to the ticks seen.
    hub.on_tick(Tick("NIFTY", 23500.0, prev_close=23400.0, received_at=T0, day_high=INF, day_low=23400.0))
    q = hub.live_quote("NIFTY", T0)
    assert q["range_source"] != "feed" and math.isfinite(q["day_high"])
    t = tick_from_breeze({"last": "inf", "close": "nan"}, "NIFTY", "index")
    assert t is None


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_non_finite_history_row_is_rejected(field):
    row = {"datetime": "2026-11-10 09:15:00", "open": 10, "high": 11, "low": 9, "close": 10}
    row[field] = "inf" if field != "low" else "-inf"
    bars, bad = parse_rows([row], "X", "stock", INTRADAY)
    assert bars == [] and len(bad) == 1
    bars, bad = parse_rows([{**row, field: 10, "volume": "inf"}], "X", "stock", INTRADAY)
    assert bars[0].volume is None                          # unknown, not infinite


# ---------------- 2. an invalid file never empties the data ----------------
def manual_on(tmp_path, store):
    s = settings_with_tmp_data(tmp_path)
    cal = MarketCalendar()
    m = ManualData(s, store, cal)
    m.load_all()
    return m, cal


def test_invalid_holidays_file_keeps_last_good_calendar(tmp_path):
    store = Store(":memory:")                              # same DB across "restarts"
    (tmp_path / "holidays.csv").write_text("date,note\n2026-11-11,test holiday\n")
    manual_on(tmp_path, store)
    (tmp_path / "holidays.csv").write_text("date,note\n2026-11-11,test holiday\nnot-a-date,oops\n")
    m, cal = manual_on(tmp_path, store)
    assert not cal.is_trading_day(date(2026, 11, 11))      # still a holiday
    assert m.errors["holidays"]


@pytest.mark.parametrize("fname, good, bad", [
    ("holidays.csv", "date,note\n2026-12-25,Christmas\n", "date,note\n2026-12-25,Christmas\nbad,x\n"),
    ("events.csv", "date,type,note\n2026-12-04,RBI_POLICY,x\n", "date,type,note\n2026-12-04,PARTY,x\n"),
    ("fii_dii.csv", "date,fii_net_cr,dii_net_cr\n2026-11-10,-5,1\n",
     "date,fii_net_cr,dii_net_cr\n2026-11-10,-5,1\n2026-11-09,abc,1\n"),
])
def test_invalid_file_makes_regime_unknown_naming_file_and_row(tmp_path, fname, good, bad):
    s = settings_with_tmp_data(tmp_path)
    store = Store(":memory:")
    (tmp_path / fname).write_text(bad)
    cal = MarketCalendar()
    m = ManualData(s, store, cal)
    m.load_all()
    hub = MarketHub(s, store, "breeze", cal)
    store_history(store, cal, SESSION)
    live_ticks(hub, T0, nifty=22700.0)
    svc = RegimeService(s, store, hub, cal, m)
    out = svc.evaluate(T0 + timedelta(seconds=5))
    assert out["regime"] == "UNKNOWN"
    hit = [r for r in out["reasons"] if fname in r]
    assert hit and ("line 2" in hit[0] or "line 3" in hit[0]), out["reasons"]
    # fixing the file and reloading clears the problem
    (tmp_path / fname).write_text(good)
    m.load_all()
    out = svc.evaluate(T0 + timedelta(seconds=6))
    assert not any(fname in r for r in out["reasons"]) and out["regime"] != "UNKNOWN", out["reasons"]


# ---------------- 3. separate SQLite file per feed mode ----------------
def test_db_file_chosen_from_feed_mode():
    raw = load_settings().model_dump()
    raw["feed"]["mode"] = "sim"
    assert Settings.model_validate(raw).db_path().name == "market_sim.db"
    raw["feed"]["mode"] = "breeze"
    assert Settings.model_validate(raw).db_path().name == "market_live.db"
    raw["storage"] = {"db_path_sim": "data/x.db", "db_path_live": "data/x.db"}
    with pytest.raises(ValueError):
        Settings.model_validate(raw)


def test_live_store_refuses_sim_rows():
    live = Store(":memory:", live=True)
    bar = Bar("NIFTY", T0, 1, 1, 1, 1)
    with pytest.raises(SimDataInLiveStore):
        live.save_bar(bar, "sim")
    with pytest.raises(SimDataInLiveStore):
        live.save_history_bars("bars_1d", [bar], "sim")
    with pytest.raises(SimDataInLiveStore):
        live.save_quality_event(T0, "NIFTY", "BAD_BAR", 1, None, None, "sim:5minute:bad")
    live.save_bar(bar, "breeze")                           # live rows are fine
    Store(":memory:").save_bar(bar, "sim")                 # the sim file accepts sim rows


def test_sim_feed_cannot_run_against_live_config(tmp_path):
    class SimNamed:
        name = "sim"
    s = settings_with_tmp_data(tmp_path, feed={"mode": "breeze"})
    with pytest.raises(ValueError, match="does not match"):
        create_app(s, feed=SimNamed(), db_path=":memory:")


# ---------------- 4. zero or missing ATR, and other divisions ----------------
def test_zero_atr_gives_unknown_not_an_error():
    flat = [DailyBar(SESSION - timedelta(days=80 - i), 20000, 20000, 20000, 20000) for i in range(60)]
    r = classify(make_inputs("up", index_history=flat, index_price=20000.0,
                             index_day_high=20000.0, index_day_low=20000.0), P)
    assert r.regime == "UNKNOWN" and any("ATR is zero or unknown" in x for x in r.reasons)


@pytest.mark.parametrize("change", [{"index_price": INF}, {"index_price": NAN}, {"vix_now": INF},
                                    {"index_day_high": NAN}, {"index_day_low": -INF}, {"index_price": 0.0}])
def test_non_finite_regime_inputs_give_unknown(change):
    assert classify(make_inputs("up", **change), P).regime == "UNKNOWN"


def test_day_high_below_low_gives_unknown():
    inp = make_inputs("up")
    r = classify(replace(inp, index_day_high=inp.index_price - 10, index_day_low=inp.index_price + 10), P)
    assert r.regime == "UNKNOWN" and any("below day low" in x for x in r.reasons)


def test_bad_values_in_history_give_unknown():
    inp = make_inputs("up")
    hist = list(inp.index_history)
    hist[-1] = replace(hist[-1], close=NAN)
    assert classify(replace(inp, index_history=hist), P).regime == "UNKNOWN"
    assert classify(replace(inp, vix_history=[*inp.vix_history[:-1], INF]), P).regime == "UNKNOWN"


def test_indicators_treat_non_finite_as_missing():
    closes = [100.0 + i for i in range(20)]
    b = breadth({"A": (closes, NAN), "B": (closes, 125.0), "C": ([*closes[:-1], INF], 125.0)}, 20)
    assert b.missing == ["A", "C"] and b.above == ["B"]
    assert daily_returns([0.0, 10.0, NAN, 5.0]) == [None, None, None, None]


# ---------------- 5. write routes refuse cross-site requests ----------------
def local_client(tmp_path):
    s = settings_with_tmp_data(tmp_path)

    class Quiet:
        name = "sim"
        def start(self, on_tick): pass
        def stop(self): pass
    app = create_app(s, feed=Quiet(), db_path=":memory:", background=False)
    return TestClient(app, base_url="http://127.0.0.1:8000")


CSV = {"text": "date,fii_net_cr,dii_net_cr\n2026-11-10,-5,1\n"}


@pytest.mark.usefixtures("fixed_clock")
def test_allowed_local_json_request(tmp_path):
    with local_client(tmp_path) as c:
        r = c.post("/data/import/fii_dii", json=CSV, headers={"Origin": "http://127.0.0.1:8000"})
        assert r.status_code == 200 and r.json()["ok"]
        r = c.post("/data/import/fii_dii", json=CSV, headers={"Host": "localhost:8000"})   # no Origin
        assert r.status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:9999", "null",
                                    "http://localhost.evil.example:8000"])
@pytest.mark.usefixtures("fixed_clock")
def test_foreign_origin_is_refused(tmp_path, origin):
    with local_client(tmp_path) as c:
        r = c.post("/data/import/fii_dii", json=CSV, headers={"Origin": origin})
        assert r.status_code == 403
    assert (tmp_path / "fii_dii.csv").read_text() == "date,fii_net_cr,dii_net_cr\n"   # nothing written


@pytest.mark.usefixtures("fixed_clock")
def test_form_encoded_post_is_refused(tmp_path):
    with local_client(tmp_path) as c:
        r = c.post("/data/import/fii_dii", data={"text": CSV["text"]})
        assert r.status_code == 415
        r = c.post("/data/import/fii_dii", content=CSV["text"], headers={"Content-Type": "text/plain"})
        assert r.status_code == 415
    assert (tmp_path / "fii_dii.csv").read_text() == "date,fii_net_cr,dii_net_cr\n"


@pytest.mark.usefixtures("fixed_clock")
def test_foreign_host_is_refused(tmp_path):
    with local_client(tmp_path) as c:
        r = c.post("/data/import/fii_dii", json=CSV, headers={"Host": "attacker.example:8000"})
        assert r.status_code == 403
        assert c.get("/api/regime").status_code == 200        # reads are unaffected


def test_write_check_unit():
    ok = {"host": "127.0.0.1:8000", "content-type": "application/json; charset=utf-8"}
    assert write_request_problem("POST", ok, 8000) is None
    assert write_request_problem("GET", {"host": "anything"}, 8000) is None
    assert write_request_problem("DELETE", {**ok, "host": "127.0.0.1:8001"}, 8000)[0] == 403
    assert write_request_problem("PUT", {**ok, "content-type": "multipart/form-data"}, 8000)[0] == 415
