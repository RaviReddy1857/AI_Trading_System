"""Phase 6 M4, step 14 acceptance: each failure ends in a safe, visible state.
- restart mid-session: open positions monitored again within one bar, unconfirmed alerts sent, no duplicate or
  revived signal, "restarted at hh:mm, data from hh:mm";
- stale and dropped feed: no signal, banner, one heartbeat alert, the feed asked to reconnect, signals resume only on
  fresh LIVE prices;
- expired Breeze login: red state (dashboard and Data status), one alert, no signal; credentials never logged;
- partial and failed import: last good data in use, file rejected with a reason, Data status row not green;
- missed alert (re-sent until delivered) and duplicated alert (sent once): see also test_phase6_telegram.py;
- laptop sleep: simulated clock jump - prices STALE until fresh ticks, due exit calls re-sent, expired signals not
  sent, one "asleep from - to" alert."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Tick
from app.recovery import Watchdog
from tests.test_phase6_engine import BAR_END, BreakoutFixture, FixedRegime, make, seed, tick
from tests.test_step1 import sim_settings

NOW = BAR_END + timedelta(seconds=3)                                      # Monday 10:30:03 IST


def on(s=None):
    s = s or sim_settings()
    raw = s.model_dump()
    raw["alerts"]["telegram_enabled"] = True
    return type(s).model_validate(raw)


class Sender:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    def send(self, text):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("network down")
        self.sent.append(text)


def watchdog(app, feed=None):
    w = Watchdog(app.state.engine.s, app.state.hub, app.state.journal, app.state.alerts, app.state.engine.cal,
                 feed or object(), app.state.engine)
    w.start(NOW - timedelta(minutes=1))
    return w


def add_position(c, stop=1050.0):
    r = c.post("/journal/positions", json={"symbol": "RELIANCE", "trade_type": "SWING_STOCKS", "direction": "LONG",
                                           "quantity": 10, "entry_price": 1060.0, "entry_date": "2026-11-16",
                                           "stop": stop})
    return r.json()["position"]["position_id"]


# ---------------- restart mid-session ----------------
def test_restart_monitors_positions_within_one_bar_and_sends_unconfirmed_alerts(tmp_path):
    db = str(tmp_path / "r.db")
    c, eng = make(db)
    with c:
        pid = add_position(c, stop=1070.0)                                    # price below the stop: STOP due
        c.app.state.alerts.enqueue("exit:old:STOP:1", "EXIT_CALL", "unconfirmed before the restart", NOW,
                                   NOW + timedelta(hours=1))
    # restart (same database): the position is monitored on the first processed bar
    s = on()
    from tests.phase6_fixtures import app_client
    c2 = app_client(s, db_path=db)
    eng2 = c2.app.state.engine
    eng2.cash, eng2.swing, eng2.regime = [BreakoutFixture()], [], FixedRegime()
    eng2.desk = type("NoOptions", (), {"underlyings": lambda self: []})()
    with c2:
        last = seed(c2.app)
        tick(c2.app, BAR_END - timedelta(seconds=1), last)
        out = eng2.on_bar(BAR_END, NOW)
        assert any(d["position_id"] == pid and d["kind"] == "STOP" for d in out["due"])
        # the alert queued before the restart (Telegram was off then: logged OFF) is not sent now; one queued while
        # on is sent after the restart
        snd = Sender()
        c2.app.state.alerts.enqueue("exit:p:STOP:1", "EXIT_CALL", "queued", NOW, NOW + timedelta(hours=1))
        c2.app.state.alerts.dispatch(NOW + timedelta(seconds=5), snd)
        assert "queued\nAlert key: exit:p:STOP:1" in snd.sent and not any("unconfirmed before the restart" in t for t in snd.sent)
        w = watchdog(c2.app)
        tick(c2.app, NOW + timedelta(seconds=10), last)
        st = w.status(NOW + timedelta(seconds=20))
        assert st["restart_text"].startswith("restarted at 10:29, data from ")


# ---------------- stale and dropped feed ----------------
class Feed:
    def __init__(self):
        self.reconnects = 0
        self.login_state = {"state": "OK", "at": None, "message": ""}

    def reconnect(self):
        self.reconnects += 1


def test_stale_feed_no_signal_banner_one_heartbeat_and_reconnect():
    c, eng = make()
    with c:
        last = seed(c.app)
        app = c.app
        app.state.hub.on_tick(Tick("RELIANCE", last, bid=last - 0.1, ask=last + 0.1, source="sim",
                                   received_at=BAR_END - timedelta(minutes=10)))
        app.state.hub.on_tick(Tick("NIFTY", 23500.0, source="sim", received_at=BAR_END - timedelta(minutes=10)))
        out = eng.on_bar(BAR_END, NOW)                  # stale prices: the strategies see no LIVE price, no signal
        assert out["signals"] == 0 and out["approved"] == 0 and c.get("/api/paper").json()["open"] == []
        feed = Feed()
        w = watchdog(app, feed)
        first = w.check(NOW)                                 # stale for 10 minutes already: alert and reconnect now
        assert "HEARTBEAT_FEED" in first and "RECONNECT" in first and feed.reconnects == 1
        assert w.check(NOW + timedelta(seconds=15)) == []    # one alert per outage; reconnects are rate-limited
        assert "stale since 10:20" in w.status(NOW)["banners"][0]["text"]
        assert "RECONNECT" in w.check(NOW + timedelta(seconds=s_reconnect(app)))
        assert feed.reconnects == 2
        # fresh LIVE prices: the next bar's signals are decided normally again
        tick(app, BAR_END + timedelta(minutes=5) - timedelta(seconds=1), last + 3)
        assert w.check(BAR_END + timedelta(minutes=5)) == [] and w.status(BAR_END + timedelta(minutes=5))["stale_since_utc"] is None


def s_reconnect(app):
    return app.state.engine.s.engine.feed_reconnect_after_seconds + 1


def test_engine_heartbeat_when_a_bar_is_not_processed():
    c, eng = make()
    with c:
        tick(c.app, BAR_END + timedelta(minutes=2), 1000.0)
        eng.last_bar = BAR_END - timedelta(minutes=5)
        w = watchdog(c.app)
        assert "HEARTBEAT_ENGINE" not in w.check(BAR_END + timedelta(seconds=60))
        assert "HEARTBEAT_ENGINE" in w.check(BAR_END + timedelta(seconds=121))
        assert "HEARTBEAT_ENGINE" not in w.check(BAR_END + timedelta(seconds=150))


def test_stale_position_shows_paused_since():
    c, eng = make()
    with c:
        pid = add_position(c)
        seed(c.app)
        c.app.state.hub.on_tick(Tick("NIFTY", 23500.0, source="sim", received_at=NOW))
        eng.on_bar(BAR_END, NOW)                                               # RELIANCE has no tick: stale
        p = next(x for x in c.get("/api/positions").json()["open"] if x["position_id"] == pid)
        assert p["price_note"] and p["plan"]["stale_since_utc"] == NOW.isoformat() and p["exit_calls"] == []


# ---------------- expired Breeze login ----------------
def test_expired_login_red_one_alert_no_signal_and_no_credentials(monkeypatch, caplog):
    import logging
    from app.feeds.breeze_feed import BreezeFeed
    monkeypatch.setenv("BREEZE_SESSION_TOKEN", "SECRET-SESSION-12345")
    feed = BreezeFeed([])
    with caplog.at_level(logging.DEBUG):
        feed.note_reply({"Success": None, "Status": 500,
                         "Error": "Session key is expired. token SECRET-SESSION-12345"})
    assert feed.login_state["state"] == "EXPIRED"
    assert "SECRET-SESSION-12345" not in feed.login_state["message"] and "SECRET-SESSION-12345" not in caplog.text
    c, eng = make()
    with c:
        w = watchdog(c.app, feed)
        assert "LOGIN_EXPIRED" in w.check(NOW)
        assert "LOGIN_EXPIRED" not in w.check(NOW + timedelta(minutes=1))       # one alert a day
        assert w.status(NOW)["banners"][0]["colour"] == "red"
        from app.data_status import data_status
        ds = data_status(c.app.state.engine.s, c.app.state.store, eng.cal, "breeze", manual=None, now=NOW,
                         login_state=feed.login_state)
        row = next(r for r in ds["rows"] if r["source"] == "Breeze session")
        assert row["colour"] == "red" and "EXPIRED" in row["what_to_do"]
        assert "SECRET-SESSION-12345" not in str(ds)
        # no fresh prices -> every signal NO_TRADE
        seed(c.app)
        out = eng.on_bar(BAR_END, NOW)
        assert out["approved"] == 0
    ok = BreezeFeed([])
    ok.note_reply({"Success": [{"close": 1}], "Error": None})
    assert ok.login_state["state"] == "OK"


# ---------------- partial and failed import ----------------
def test_failed_import_keeps_last_good_data_and_shows_a_rejected_row(tmp_path):
    from app.data_status import data_status
    from tests.test_data_import import manual_in
    from app.calendar import MarketCalendar
    s, store, m = manual_in(tmp_path, fii="date,fii_net_cr,dii_net_cr\n2026-11-13,-100,50\n")
    m.load_all()
    res = m.import_text("fii_dii", "date,fii_net_cr,dii_net_cr\n2026-11-14,-200,50\n2026-11-15,oops,1\n")
    assert res["ok"] is False and res["errors"]
    store.log_import_failure("fii_dii", NOW, "; ".join(res["errors"]))           # what the route does
    got = [r["date"].isoformat() for r in store.fii_dii_before(datetime(2026, 11, 17).date(), 10)]
    assert got == ["2026-11-13"]                                               # all-or-nothing: the last good data
    assert "2026-11-14" not in (tmp_path / "fii_dii.csv").read_text()
    ds = data_status(s, store, MarketCalendar(), "sim", manual=m, now=NOW)
    rej = [r for r in ds["rows"] if r["source"] == "Rejected import: fii_dii"]
    assert rej and rej[0]["colour"] == "amber" and rej[0]["coverage"] == "; ".join(res["errors"])


def test_rejected_import_route_is_logged():
    c, _ = make()
    with c:
        r = c.post("/data/import/fii_dii", json={"text": "nonsense\n"})
        assert r.status_code == 400
        assert [f["kind"] for f in c.app.state.store.import_failures(datetime(2000, 1, 1, tzinfo=timezone.utc))] == ["fii_dii"]


# ---------------- laptop sleep ----------------
def test_laptop_sleep_clock_jump(tmp_path):
    c, eng = make(str(tmp_path / "sleep.db"))
    with c:
        last = seed(c.app)
        tick(c.app, BAR_END - timedelta(seconds=1), last)
        out = eng.on_bar(BAR_END, NOW)                                         # a signal valid until 10:45
        sid = out["stored"][0]["signal_id"]
        pid = add_position(c, stop=1070.0)
        w = watchdog(c.app)
        w.check(NOW)
        wake = NOW + timedelta(minutes=50)                                     # asleep 10:30 -> 11:20
        # on wake: every price is STALE until fresh ticks arrive
        assert c.app.state.hub.quote("RELIANCE", wake)["status"] == "STALE"
        assert "ASLEEP" in w.check(wake)
        a = [x for x in c.app.state.alerts.rows() if x["kind"] == "ASLEEP"][0]
        assert "asleep from 10:30 to 11:20" in a["text"]
        assert "asleep from 10:30 to 11:20" in w.status(wake)["banners"][-1]["text"]
        # the expired signal is not sent and its pending paper trade is not filled
        eng.paper.fill_pending(wake, eng.signals)
        assert c.get("/api/paper").json()["closed"][0]["status"] == "NOT_FILLED"
        from app.alerts import Notifier
        n = c.app.state.notifier
        row = {"signal_id": sid, "score": 50, "alert_allowed": True, "valid_until": eng.signals[sid].valid_until,
               "card": out["stored"][0]["card"]}
        n.on_bar([row], [], wake)
        assert c.app.state.alerts.get(f"signal:{sid}") is None
        # fresh ticks: the exit call due in the gap is evaluated on fresh prices and sent
        tick(c.app, wake + timedelta(seconds=2), last)
        bar = BAR_END + timedelta(minutes=50)
        out2 = eng.on_bar(bar, wake + timedelta(seconds=3))
        assert any(d["position_id"] == pid and d["kind"] == "STOP" for d in out2["due"])


def test_breeze_feed_reconnects_itself_with_the_fake_sdk():
    from tests.test_breeze_feed import FakeBreezeSDK, feed_with
    sdk = FakeBreezeSDK()
    sdk.disconnects = 0
    sdk.ws_disconnect = lambda: setattr(sdk, "disconnects", sdk.disconnects + 1)
    feed = feed_with(sdk)
    feed.start(lambda t: None)
    n = len(sdk.subscribed)
    feed.reconnect()
    assert sdk.disconnects == 1 and len(sdk.subscribed) == 2 * n and len(feed._by_token) == n
