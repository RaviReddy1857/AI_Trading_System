from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.bars import BarBuilder
from app.config import load_settings, Settings
from app.feeds.breeze_feed import tick_from_breeze
from app.hub import MarketHub
from app.main import create_app
from app.models import Tick
from app.quality import (BAD_PRICE, CROSSED_QUOTE, PRICE_JUMP, VOLUME_WENT_BACK,
                         check_tick, freshness, is_market_hours)
from app.store import Store

T0 = datetime(2026, 11, 11, 5, 0, 0, tzinfo=timezone.utc)  # Wed 10:30 IST


def tk(last, **kw):
    kw.setdefault("received_at", T0)
    return Tick("RELIANCE", last, **kw)


# ---------- config ----------
def test_config_loads_and_limits_scale_with_capital():
    s = load_settings()
    assert s.account.capital_inr == 200000
    assert s.limits_in_rupees()["max_loss_per_trade"] == 2000
    assert s.limits_in_rupees()["max_loss_per_option"] == 5000
    s.account.capital_inr = 300000
    assert s.limits_in_rupees()["daily_loss_stop"] == 6000


def test_public_host_is_rejected():
    raw = load_settings().model_dump()
    raw["server"]["host"] = "0.0.0.0"
    with pytest.raises(ValueError):
        Settings.model_validate(raw)


# ---------- quality gate ----------
@pytest.mark.parametrize("bad", [0, -5, float("nan")])
def test_bad_price_quarantined(bad):
    assert check_tick(tk(bad), None, 5) == [BAD_PRICE]


def test_crossed_quote_quarantined():
    assert CROSSED_QUOTE in check_tick(tk(100, bid=101, ask=100.5), None, 5)


def test_missing_quote_is_not_an_error():
    assert check_tick(tk(100), None, 5) == []


def test_price_jump_quarantined_but_normal_move_passes():
    prev = tk(100)
    assert PRICE_JUMP in check_tick(tk(120), prev, 5)
    assert check_tick(tk(101), prev, 5) == []


def test_jump_check_self_heals_after_a_pause():
    later = tk(120, received_at=T0 + timedelta(seconds=121))
    assert check_tick(later, tk(100), 5) == []


def test_volume_going_backwards_quarantined():
    assert VOLUME_WENT_BACK in check_tick(tk(100, cum_volume=50), tk(100, cum_volume=90), 5)


def test_freshness_states():
    assert is_market_hours(T0)
    assert freshness(T0, T0 + timedelta(seconds=10), 30) == "LIVE"
    assert freshness(T0, T0 + timedelta(seconds=31), 30) == "STALE"
    assert freshness(None, T0, 30) == "NO_DATA"
    sunday = datetime(2026, 11, 15, 5, 0, tzinfo=timezone.utc)
    assert freshness(sunday, sunday, 30) == "CLOSED"
    evening = datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc)  # 16:30 IST
    assert freshness(evening, evening, 30) == "CLOSED"


# ---------- bars ----------
def test_bar_builder_ohlc_and_volume():
    closed = []
    b = BarBuilder(closed.append)
    for sec, price, vol in [(0, 100, 1000), (20, 103, 1100), (40, 99, 1250)]:
        b.add(tk(price, cum_volume=vol, received_at=T0 + timedelta(seconds=sec)))
    b.add(tk(101, cum_volume=1300, received_at=T0 + timedelta(seconds=65)))
    assert len(closed) == 1
    bar = closed[0]
    assert (bar.open, bar.high, bar.low, bar.close, bar.ticks) == (100, 103, 99, 99, 3)
    assert bar.volume == 250
    assert b.current("RELIANCE").volume == 50


# ---------- hub: bad ticks never reach prices or bars ----------
def test_hub_drops_bad_ticks_and_marks_stale():
    s = load_settings()
    store = Store(":memory:")
    hub = MarketHub(s, store, "breeze")
    hub.on_tick(tk(100, received_at=T0))
    hub.on_tick(tk(130, received_at=T0 + timedelta(seconds=1)))  # 30% jump
    row = next(r for r in hub.snapshot(T0 + timedelta(seconds=2)) if r["symbol"] == "RELIANCE")
    assert row["last"] == 100 and row["status"] == "LIVE" and row["usable"]
    assert hub.stats() == {"good_ticks": 1, "quarantined_ticks": 1}
    assert store.recent_quality_events()[0]["reason"] == PRICE_JUMP
    row = next(r for r in hub.snapshot(T0 + timedelta(seconds=90)) if r["symbol"] == "RELIANCE")
    assert row["status"] == "STALE" and not row["usable"]
    nifty = next(r for r in hub.snapshot(T0) if r["symbol"] == "NIFTY")
    assert nifty["last"] is None and nifty["status"] == "NO_DATA" and not nifty["usable"]


# ---------- Breeze message parsing ----------
def test_breeze_tick_parsing():
    raw = {"symbol": "4.1!2885", "last": 1290.5, "bPrice": 1290.4, "sPrice": 1290.6,
           "ttq": 123456, "close": 1280.0, "quotes": "Quotes Data"}
    t = tick_from_breeze(raw, "RELIANCE", "stock")
    assert (t.last, t.bid, t.ask, t.cum_volume, t.prev_close) == (1290.5, 1290.4, 1290.6, 123456, 1280.0)
    idx = tick_from_breeze({"last": "23500.1", "bPrice": 0, "sPrice": 0, "close": ""}, "NIFTY", "index")
    assert idx.last == 23500.1 and idx.bid is None and idx.ask is None and idx.prev_close is None
    assert tick_from_breeze({"last": ""}, "NIFTY", "index") is None


# ---------- web app ----------
class FakeFeed:
    name = "sim"

    def start(self, on_tick):
        on_tick(Tick("NIFTY", 23500.0, prev_close=23400.0))

    def stop(self):
        pass


def test_api_endpoints():
    with TestClient(create_app(feed=FakeFeed(), db_path=":memory:")) as c:
        assert c.get("/health").json()["ok"] is True
        rows = c.get("/api/prices").json()["rows"]
        nifty = next(r for r in rows if r["symbol"] == "NIFTY")
        assert nifty["last"] == 23500.0 and nifty["status"] == "LIVE"
        assert c.get("/api/config").json()["limits_inr"]["max_loss_per_trade"] == 2000
        assert "RESEARCH ONLY" in c.get("/").text
        assert c.get("/api/quality").json()["quarantined_ticks"] == 0
