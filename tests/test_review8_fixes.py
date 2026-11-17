"""Review 8 (Codex re-check of 9dd57af..d35c2e6):
1. paper entry time is the fill quote's time, not the poll's; a late poll checks every price from the fill on;
2. first-quote captures survive a restart; a lost capture is filled from a substitute marked "first quote
   unavailable", with its price and time, and left out of the slippage report;
3. option and spread fills use the earliest chain snapshot (within the signal's validity) that quotes every leg;
4. a Telegram HTTP 200 whose body is not a Bot API reply is an uncertain delivery, never a plain failure.
"""
from __future__ import annotations

import json
from datetime import timedelta

from app.calendar import MarketCalendar
from app.execution import ExecutionBook
from app.hub import MarketHub
from app.models import Bar, Tick
from app.paper import PaperTrader
from app.pricing import FIRST_QUOTE_UNAVAILABLE, LIVE, MISSING, Marker
from app.store import Store
from tests.phase6_fixtures import EXPIRY, T0, approved, bull_put, cash_signal, settings
from tests.test_phase6_paper import Costs, q, trader
from tests.test_step1 import sim_settings

S = lambda n: timedelta(seconds=n)                                    # noqa: E731
M = lambda n: timedelta(minutes=n)                                    # noqa: E731


# ---------------- 1. entry at the fill quote's time; a late poll checks every price since ----------------
def _bars(store, rows):
    for k, (o, h, l, c) in enumerate(rows):
        store.save_bar(Bar("RELIANCE", T0 + M(k), o, h, l, c, 100, 5), "sim")


def test_1_late_poll_after_a_stop_breach_and_recovery_is_stopped_out():
    p, j, m, store, _ = trader()
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), T0)
    m.cash_q["RELIANCE"] = q(100.3, 100.5, 100.4, T0 + S(1))             # first quote 10:30:01
    _bars(store, [(100.4, 100.8, 100.2, 100.5),                         # 10:30
                  (100.0, 100.1, 94.0, 94.5),                           # 10:31: the stop is breached
                  (94.5, 99.0, 94.4, 98.8),                             # 10:32
                  (98.8, 101.0, 98.5, 100.9)])                          # 10:33: recovered
    p.fill_pending(T0 + M(4), {sig.signal_id: sig})                    # the poll runs at 10:34
    row = j.paper_trade(sig.signal_id)
    assert row["entry_utc"] == (T0 + S(1)).isoformat() and row["entry_price"] == 100.5
    assert row["status"] == "CLOSED" and row["exit_reason"] == "STOP" and row["exit_price"] == 95.0
    assert p.manage(T0 + M(5), T0) == []                               # never closed twice


def test_1_without_a_breach_the_late_fill_stays_open_and_the_bar_check_sees_the_fill_minute():
    p, j, m, store, _ = trader()
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), T0)
    m.cash_q["RELIANCE"] = q(100.3, 100.5, 100.4, T0 + S(1))
    _bars(store, [(100.4, 100.8, 100.2, 100.5), (100.5, 101.0, 100.1, 100.9)])
    p.fill_pending(T0 + M(4), {sig.signal_id: sig})
    assert j.paper_trade(sig.signal_id)["status"] == "OPEN"
    store.save_bar(Bar("RELIANCE", T0 + M(2), 100.9, 111.0, 100.8, 110.5, 100, 5), "sim")   # 10:32: target
    out = p.manage(T0 + M(5), T0)
    assert out[0]["reason"] == "TARGET"


# ---------------- 2. first-quote captures survive a restart; a lost one is a marked substitute ----------------
def _hub(path):
    return MarketHub(sim_settings(), Store(path), "sim")


def _tick(hub, at, px):
    hub.on_tick(Tick("RELIANCE", px, bid=px - 0.1, ask=px + 0.1, received_at=at))


def test_2_capture_restart_later_quote_fills_at_the_captured_first_quote(tmp_path):
    db = str(tmp_path / "hub.db")
    hub = _hub(db)
    _tick(hub, T0 - S(2), 100.0)
    _tick(hub, T0 + S(4), 100.4)                                       # the first quote of the next bar
    del hub                                                            # restart
    hub2 = _hub(db)
    _tick(hub2, T0 + S(70), 103.0)                                     # a later quote, after the restart
    first = hub2.first_quote("RELIANCE", T0)
    assert first["at"] == T0 + S(4) and first["ask"] == 100.5 and first["substitute"] is False

    s = settings()
    store = hub2.store
    from app.journal import Journal
    j = Journal(store, s)
    xb = ExecutionBook(store, s)
    p = PaperTrader(s, j, Marker(s, store, hub2, "sim"), store, xb, MarketCalendar(), Costs())
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), T0)
    p.fill_pending(T0 + S(75), {sig.signal_id: sig})
    row = j.paper_trade(sig.signal_id)
    assert row["entry_price"] == 100.5 and row["entry_utc"] == (T0 + S(4)).isoformat()
    assert FIRST_QUOTE_UNAVAILABLE not in row["entry_source"]


def test_2_a_lost_capture_is_marked_shows_the_substitute_and_is_left_out_of_slippage(tmp_path):
    hub = _hub(str(tmp_path / "fresh.db"))                             # the capture was lost (nothing kept)
    _tick(hub, T0 + S(70), 103.0)                                      # first quote received after the restart
    first = hub.first_quote("RELIANCE", T0)
    assert first["substitute"] is True and first["at"] == T0 + S(70)

    s = settings()
    store = hub.store
    from app.journal import Journal
    j = Journal(store, s)
    xb = ExecutionBook(store, s)
    p = PaperTrader(s, j, Marker(s, store, hub, "sim"), store, xb, MarketCalendar(), Costs())
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), T0)
    p.fill_pending(T0 + S(75), {sig.signal_id: sig})
    row = j.paper_trade(sig.signal_id)
    assert row["status"] == "OPEN" and row["entry_price"] == 103.1
    src = row["entry_source"]
    assert src.startswith(FIRST_QUOTE_UNAVAILABLE) and "103.1" in src and (T0 + S(70)).isoformat() in src
    leg = xb.leg_rows()[0]
    assert leg["first_quote_unavailable"] is True and leg["no_quote"] is False
    assert leg["paper_vs_touch"] is None and leg["paper_vs_mid_inr"] is None
    grp = xb.report()["by_stock"][0]
    assert grp["first_quote_unavailable"] == 1 and grp["paper_vs_touch_pct"] is None


def test_2_a_hub_running_before_the_planned_fill_is_never_a_substitute(tmp_path):
    hub = _hub(str(tmp_path / "run.db"))
    _tick(hub, T0 - S(2), 100.0)
    _tick(hub, T0 + S(70), 103.0)                                      # quiet minute, but the app was running
    assert hub.first_quote("RELIANCE", T0)["substitute"] is False


# ---------------- 3. options: the earliest snapshot quoting every leg, within the signal's validity ----------------
def _chain(store, at, rows):
    store.save_option_chain("sim", [("NIFTY", EXPIRY.isoformat(), k, "PE", at.isoformat(), b, a, (a + b) / 2 if a and b
                                     else None, None, None, None, 24100.0) for k, b, a in rows])


def _opt_trader():
    s = settings()
    store = Store(":memory:")
    from app.journal import Journal
    j = Journal(store, s)
    hub = MarketHub(sim_settings(), store, "sim")
    return PaperTrader(s, j, Marker(s, store, hub, "sim"), store, ExecutionBook(store, s), MarketCalendar(),
                       Costs()), j, store


def test_3_first_snapshot_missing_a_leg_the_second_complete_fills_from_the_second():
    p, j, store = _opt_trader()
    sig = bull_put(sold=(24000, 60.0), bought=(23900, 30.0))
    p.open(sig, approved(quantity=75, lots=1, margin=40000.0), T0)
    _chain(store, T0 + S(10), [(24000, 58.0, 60.0)])                    # the 23900 leg is missing
    _chain(store, T0 + S(40), [(24000, 59.0, 61.0), (23900, 29.0, 31.0)])
    p.fill_pending(T0 + S(50), {sig.signal_id: sig})
    row = j.paper_trade(sig.signal_id)
    assert row["status"] == "OPEN" and row["entry_price"] == 28.0          # 59 bid - 31 ask, both from 10:30:40
    assert json.loads(row["entry_prices_json"]) == [59.0, 31.0] and row["entry_utc"] == (T0 + S(40)).isoformat()


def test_3_legs_are_never_mixed_across_snapshots_and_a_leg_without_bid_and_ask_does_not_count():
    s = settings()
    store = Store(":memory:")
    mk = Marker(s, store, None, "sim")
    legs = bull_put().legs
    _chain(store, T0 + S(10), [(24000, 58.0, 60.0)])
    _chain(store, T0 + S(20), [(23900, 28.0, 30.0)])
    _chain(store, T0 + S(30), [(24000, 59.0, 61.0), (23900, None, 31.0)])
    qs = mk.first_options("NIFTY", legs, T0, T0 + M(1))
    assert all(x.status == MISSING for x in qs) and "none with a bid and ask for every leg" in qs[0].note
    _chain(store, T0 + S(50), [(24000, 60.0, 62.0), (23900, 30.0, 32.0)])
    qs = mk.first_options("NIFTY", legs, T0, T0 + M(1))
    assert [x.status for x in qs] == [LIVE, LIVE] and {x.quote_time for x in qs} == {T0 + S(50)}
    assert (qs[0].bid, qs[1].ask) == (60.0, 32.0)


def test_3_a_complete_snapshot_after_valid_until_is_not_used():
    p, j, store = _opt_trader()
    sig = bull_put()
    p.open(sig, approved(quantity=75, lots=1, margin=40000.0), T0)
    _chain(store, T0 + S(10), [(24000, 58.0, 60.0)])
    _chain(store, sig.valid_until + S(5), [(24000, 59.0, 61.0), (23900, 29.0, 31.0)])
    p.fill_pending(sig.valid_until - S(1), {sig.signal_id: sig})
    assert j.paper_trade(sig.signal_id)["status"] == "PENDING"
    p.fill_pending(sig.valid_until + S(10), {sig.signal_id: sig})
    assert j.paper_trade(sig.signal_id)["status"] == "NOT_FILLED"


# ---------------- 4. HTTP 200 with an unexpected body is an uncertain delivery ----------------
class Replies:
    def __init__(self, *bodies):
        self.bodies, self.delivered = list(bodies), []

    def __call__(self, url, body, timeout):
        self.delivered.append(json.loads(body)["text"])
        status, raw = self.bodies.pop(0) if self.bodies else (200, b'{"ok": true, "result": {"message_id": 1}}')
        return status, raw


def test_4_unexpected_200_bodies_are_uncertain_and_a_clear_refusal_is_not():
    from app.telegram import SendError
    from tests.test_review7_fixes import _sender
    for raw in (b"[]", b"not json", b'{"result": {}}', b'{"ok": "yes"}', b"null", b"\xff\xfe"):
        try:
            _sender(Replies((200, raw))).send("x")
            raise AssertionError(f"no error for {raw!r}")
        except SendError as e:
            assert e.uncertain, raw
    for status, raw in ((200, b'{"ok": false, "description": "Bad Request"}'), (502, b"[]"), (400, b"oops")):
        try:
            _sender(Replies((status, raw))).send("x")
            raise AssertionError("no error")
        except SendError as e:
            assert not e.uncertain and "t0ken" not in str(e)
    _sender(Replies((200, b'{"ok": true, "result": {"message_id": 7}}'))).send("x")       # delivered: no error


def test_4_an_empty_list_reply_then_success_delivers_an_information_message_once():
    from datetime import datetime, timezone
    from tests.test_review7_fixes import _book, _sender
    now = datetime(2026, 11, 16, 5, 1, tzinfo=timezone.utc)
    book, _, _ = _book()
    tr = Replies((200, b"[]"))
    book.enqueue("signal:r8", "SIGNAL", "SIGNAL - RELIANCE LONG", now)
    book.dispatch(now, _sender(tr))
    assert book.get("signal:r8")["status"] == "UNCERTAIN"
    for k in range(1, 6):
        book.dispatch(now + M(k), _sender(tr))
    assert tr.delivered == ["SIGNAL - RELIANCE LONG"]                     # sent once, never resent
