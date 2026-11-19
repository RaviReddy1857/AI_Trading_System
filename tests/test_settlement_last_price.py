"""The last-valid-price settlement design (user, 19-Nov-2026), replacing the quote-status escalation of 4e9fb4d/1b9b9b9.

- Settlement moneyness uses the last valid continuous-session price and its time, from ONE hub snapshot
  (`MarketHub.settlement_snapshot`); auction, rejected, quarantined and stale ticks never replace it and never change
  the level by themselves.
- The level (and what is kept on the position) comes only from the schedule, that moneyness, an invalid margin base
  and an overdue exit; with no valid price at all the sold leg is assumed near the money, and the alert says so.
- A dead or noisy feed is ONE "price unavailable since HH:MM, check in ICICI Direct" notice per symbol per session
  (`SignalEngine.price_notices`), never a settlement level, reset each trading date.
- The closing auction (15:15-15:35 for F&O stocks) is silent; the text says "auction in progress, last price as of HH:MM".
Everything runs through the real hub (`on_tick`, its session status) and the engine's exit evaluation."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.alerts import AlertBook, Notifier, exit_text
from app.calendar import MarketCalendar
from app.engine import SignalEngine
from app.hub import MarketHub
from app.models import Tick
from app.options.settlement import NOTICE, PRICE_UNKNOWN, URGENT, WARNING, days_left
from app.positions import ExitCallBook
from app.pricing import Marker
from app.quality import IST
from app.store import Store
from tests.phase5_fixtures import GOOD_MARKET
from tests.phase6_fixtures import ZeroCosts, new_journal, settings

S = settings()
COVERED = MarketCalendar({date(2026, 1, 1): "COVERAGE through 2026-12-31 test list"})
MON, TUE = date(2026, 11, 16), date(2026, 11, 17)


def ist(h, m, s=0, d=MON):
    return datetime.combine(d, time(h, m, s), IST).astimezone(timezone.utc)


def tick(when, last):
    return Tick("RELIANCE", last, bid=last - 0.1, ask=last + 0.1, source="breeze", received_at=when)


def crossed(when):
    return Tick("RELIANCE", 1400.0, bid=1410.0, ask=1390.0, source="breeze", received_at=when)   # rejected


class _Risk:
    def market_state(self, index, now):
        return GOOD_MARKET.__class__(**{**GOOD_MARKET.__dict__, "now": now})


def expiry_at(n: int, today: date = MON) -> date:
    d = today
    while days_left(today, d, COVERED) != n:
        d += timedelta(days=1)
    return d


class World:
    """A held RELIANCE put spread (sold 1300 PE, bought 1260 PE) at E-`left` on Monday 16 Nov, a real hub fed tick by
    tick, and the engine evaluating my positions on it."""

    def __init__(self, left: int, calendar=COVERED, store=None, journal=None, hub=None):
        self.hub = hub or MarketHub(S, Store(":memory:"), "breeze", MarketCalendar())
        if journal is None:
            self.j, store = new_journal(S)
        else:
            self.j = journal
        self.store = store
        exp = expiry_at(left)
        self.pos = self.j.positions("OPEN")[0] if journal is not None else self.j.add_manual_position({
            "symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-16", "margin_inr": 90000,
            "legs": [{"action": "SELL", "right": "PE", "strike": 1300, "expiry": exp.isoformat(), "lots": 1,
                      "lot_size": 500, "price": 12.0},
                     {"action": "BUY", "right": "PE", "strike": 1260, "expiry": exp.isoformat(), "lots": 1,
                      "lot_size": 500, "price": 4.0}]}, ist(9, 30))
        self.book = ExitCallBook(store, S)
        self.alerts = AlertBook(store, S)
        self.notifier = Notifier(S, self.alerts, self.j, _Risk(), None, calendar)
        self.eng = SignalEngine(settings=S, store=store, hub=self.hub, journal=self.j, notifier=self.notifier,
                                marker=Marker(S, store, self.hub, "breeze"), risk=_Risk(), regime=None,
                                calendar=calendar, desk=None, source="breeze", costs=ZeroCosts(), execbook=None,
                                paper=None, exits=self.book, now_fn=lambda: None, strategies=[])

    def ticks(self, start, end, every_s, pattern):
        """Feed ticks from `start` to `end` every `every_s` seconds; `pattern` cycles: a price (valid) or None (crossed)."""
        t, i = start, 0
        while t <= end:
            p = pattern[i % len(pattern)]
            self.hub.on_tick(tick(t, p) if p is not None else crossed(t))
            t, i = t + timedelta(seconds=every_s), i + 1

    def step(self, now):
        """One evaluation: (settlement calls due, price notices due)."""
        due = [c for c in self.eng.evaluate_exits(now, "TRENDING_UP", five={}, daily={"RELIANCE": []})
               if c["kind"] == "SETTLEMENT"]
        return due, self.eng.send_price_notices(now)

    def call(self):
        return next((c for c in self.book.calls(self.pos.position_id) if c["kind"] == "SETTLEMENT"), None)

    def level(self):
        return self.j.position(self.pos.position_id).plan.get("settlement_level")


AUCTION_STEPS = [ist(15, 15), ist(15, 20), ist(15, 25), ist(15, 30), ist(15, 34, 59)]


# ---------------- 15:15 -> 15:40 -> the next open, through the hub's real status transitions ----------------
def test_far_out_of_the_money_through_the_auction_the_close_and_the_next_open_nothing_is_raised_or_kept():
    w = World(left=7)
    w.ticks(ist(15, 0), ist(15, 14, 50), 10, [1400.0])
    assert w.step(ist(15, 14, 55)) == ([], [])
    for now in AUCTION_STEPS + [ist(15, 40)]:
        snap = w.hub.settlement_snapshot("RELIANCE", now)
        assert snap["price"] == 1400.0 and snap["at"] == ist(15, 14, 50)    # auction ticks never replace it
        w.hub.on_tick(tick(now, 1250.0))                                   # an auction indication deep ITM
        assert w.step(now) == ([], [])
    assert w.hub.settlement_snapshot("RELIANCE", ist(15, 25))["phase"] == "AUCTION"
    assert w.hub.settlement_snapshot("RELIANCE", ist(15, 40))["phase"] == "CLOSED"
    assert w.step(ist(9, 15, d=TUE)) == ([], [])                          # the open, no tick yet (E-6)
    w.ticks(ist(9, 15, 5, d=TUE), ist(9, 19, 55, d=TUE), 10, [1401.0])
    assert w.step(ist(9, 20, d=TUE)) == ([], [])
    assert w.call() is None and w.level() is None


def test_an_alert_in_the_money_keeps_its_level_through_the_auction_and_is_not_paged_again():
    w = World(left=7)
    w.ticks(ist(15, 0), ist(15, 10), 10, [1290.0])
    due, _ = w.step(ist(15, 10, 5))                                       # ITM at E-7: WARNING, sent once
    assert [c["level"] for c in due] == [WARNING] and w.level() == WARNING
    for now in AUCTION_STEPS:
        w.hub.on_tick(tick(now, 1350.0))                                   # auction indication: never the price
        due, notes = w.step(now)
        assert due == [] and notes == []                                   # silent
        c = w.call()
        assert c["level"] == WARNING and "auction in progress, last price as of 15:10" in c["reason"]
        assert c["values"]["alert"]["price"]["spot"] == 1290.0
    assert w.step(ist(15, 40)) == ([], []) and w.level() == WARNING        # closed: nothing new, nothing kept
    assert "auction in progress" not in w.call()["reason"]
    due, _ = w.step(ist(9, 20, d=TUE))                                     # the normal repeat after 60 minutes
    assert [c["level"] for c in due] == [WARNING] and w.level() == WARNING


# ---------------- alternating good and bad ticks ----------------
def test_alternating_good_and_bad_ticks_never_change_settlement_and_give_one_notice_when_mostly_bad():
    w = World(left=7)
    w.ticks(ist(10, 0), ist(10, 9, 50), 10, [1400.0, None])               # 1 good : 1 rejected (50%)
    snap = w.hub.settlement_snapshot("RELIANCE", ist(10, 9, 55))          # window (10:04:55, 10:09:55]: 30 ticks
    assert snap["price"] == 1400.0 and snap["at"] == ist(10, 9, 40) and snap["rejected"] * 2 == snap["ticks"]
    assert w.step(ist(10, 9, 55)) == ([], [])                              # 50% is not above 50%: no notice
    w.ticks(ist(10, 10), ist(10, 19, 50), 10, [1400.0, None, None])        # 1 good : 2 rejected
    due, notes = w.step(ist(10, 20))
    assert due == [] and len(notes) == 1 and notes[0]["symbol"] == "RELIANCE"
    assert "check in ICICI Direct" in notes[0]["text"] and "rejected by the quality gate" in notes[0]["why"]
    w.ticks(ist(10, 20), ist(10, 39, 50), 10, [1400.0, None, None])
    for m in (25, 30, 40):
        assert w.step(ist(10, m)) == ([], [])                              # once per session; settlement untouched
    assert w.call() is None and w.level() is None


# ---------------- no valid tick all session at E-6 ----------------
def test_no_valid_tick_all_session_at_e6_assumes_near_the_money_and_gives_one_notice():
    w = World(left=6)
    w.ticks(ist(9, 15), ist(9, 20, 50), 30, [None])                        # only rejected ticks
    due, notes = w.step(ist(9, 21))
    assert [c["level"] for c in due] == [NOTICE]                           # E-6 near the money (assumed)
    a = due[0]["values"]["alert"]
    assert a["price"]["assumed_near_money"] is True and a["price"]["spot"] is None
    assert PRICE_UNKNOWN in due[0]["reason"] and "assumed near the money" in due[0]["reason"]
    assert len(notes) == 1 and "price unavailable since 09:15" in notes[0]["text"]
    assert "assume the sold legs near the money" in notes[0]["text"]
    assert w.level() == NOTICE                                             # the conservative level is the schedule's
    for now in (ist(9, 26), ist(10, 30), ist(14, 0)):
        due, notes = w.step(now)
        assert notes == [] and all(c["level"] == NOTICE for c in due)
    assert "price unavailable" not in exit_text({**w.call(), "symbol": "RELIANCE", "trade_type": "STOCK_OPTION_SELLING",
                                                 "repeat_no": 1})          # the notice never enters the alert


def test_the_notice_is_only_in_continuous_trading_and_waits_five_minutes_after_the_open():
    w = World(left=9)                                                      # far from expiry: no settlement alert
    assert w.step(ist(9, 19, 59))[1] == []                                 # under 5 minutes since the 09:15 open
    assert len(w.step(ist(9, 20))[1]) == 1
    w2 = World(left=9)
    assert w2.step(ist(15, 25))[1] == [] and w2.step(ist(15, 40))[1] == [] and w2.step(ist(8, 0))[1] == []


# ---------------- a price counts only from today or the previous trading session ----------------
THU, FRI = date(2026, 11, 12), date(2026, 11, 13)


def test_a_price_from_two_sessions_ago_at_e6_is_no_valid_price_and_assumes_near_the_money():
    w = World(left=6)
    w.ticks(ist(15, 0, d=THU), ist(15, 10, d=THU), 10, [1400.0])           # Thursday: two sessions before Monday
    assert w.hub.settlement_snapshot("RELIANCE", ist(9, 16))["price"] == 1400.0     # the hub still has it ...
    due, _ = w.step(ist(9, 16))                                            # ... but it is too old for settlement
    assert [c["level"] for c in due] == [NOTICE]                           # E-6, assumed near the money
    a = due[0]["values"]["alert"]
    assert a["price"]["assumed_near_money"] is True and a["price"]["spot"] is None
    assert "assumed near the money" in due[0]["reason"]


def test_a_price_from_the_previous_trading_session_counts_and_the_count_skips_holidays():
    w = World(left=6)
    w.ticks(ist(15, 0, d=FRI), ist(15, 10, d=FRI), 10, [1400.0])           # Friday: the previous session
    due, _ = w.step(ist(9, 16))
    assert due == [] and w.level() is None                                 # far out of the money at Friday's price
    holiday = MarketCalendar({FRI: "a holiday", date(2026, 1, 1): "COVERAGE through 2026-12-31 test list"})
    h = World(left=6, calendar=holiday)
    h.ticks(ist(15, 0, d=THU), ist(15, 10, d=THU), 10, [1400.0])           # Friday a holiday: Thursday IS the previous
    assert h.step(ist(9, 16))[0] == [] and h.level() is None


# ---------------- an overnight rejection does not carry into the next session ----------------
def test_an_overnight_rejection_does_not_carry_into_the_next_session():
    w = World(left=9)
    w.ticks(ist(15, 5), ist(15, 14, 50), 10, [1400.0, None, None])         # Monday close: mostly rejected
    assert len(w.step(ist(15, 14, 55))[1]) == 1
    w.ticks(ist(9, 15, 5, d=TUE), ist(9, 19, 55, d=TUE), 10, [1401.0])     # Tuesday: clean ticks
    snap = w.hub.settlement_snapshot("RELIANCE", ist(9, 20, d=TUE))
    assert snap["rejected"] == 0 and snap["price"] == 1401.0
    assert w.step(ist(9, 20, d=TUE))[1] == []
    # Tuesday's own outage still gets its own single notice (reset per trading date)
    notes = w.step(ist(9, 30, d=TUE))[1]
    assert len(notes) == 1 and "since 09:19" in notes[0]["text"]
    assert w.step(ist(9, 40, d=TUE))[1] == []


# ---------------- deadline, margin and overdue escalations persist and resend as before ----------------
def test_e5_in_the_money_is_urgent_kept_and_resent_on_its_interval_then_overdue_every_evaluation():
    w = World(left=5)
    w.ticks(ist(9, 30), ist(9, 40), 10, [1290.0])
    due, _ = w.step(ist(9, 40, 5))
    assert [c["level"] for c in due] == [URGENT] and w.level() == URGENT
    assert w.step(ist(9, 45))[0] == []                                     # inside the 15-minute URGENT interval
    assert [c["level"] for c in w.step(ist(9, 55, 5))[0]] == [URGENT]      # resent on the interval
    w.ticks(ist(10, 0), ist(10, 5), 10, [1400.0])                          # far OTM later: the level never drops
    w.step(ist(10, 10))
    assert w.level() == URGENT and w.call()["level"] == URGENT
    hard = datetime.fromisoformat(w.call()["values"]["alert"]["hard_limit"])
    for k in (1, 2):                                                       # overdue: every evaluation
        due, _ = w.step(hard + timedelta(minutes=k))
        assert [c["overdue"] for c in due] == [True]


def test_an_invalid_margin_base_still_escalates():
    from app.options.settlement import HeldLeg, HeldPosition, settlement_alert
    pos = HeldPosition("RELIANCE", expiry_at(7), (HeldLeg("SELL", "PE", 1300.0, 1, 500), HeldLeg("BUY", "PE", 1260.0, 1, 500)))
    a = settlement_alert(pos, 1400.0, ist(10, 0), S.stock_option_selling, 1_000_000.0, calendar=COVERED,
                         base_margin_inr=-1.0, spot_as_of=ist(9, 59))
    assert a["level"] == URGENT and a["invalid_inputs"] and a["price"]["note"] == "last price 1400 as of 09:59"


def test_the_grace_key_is_gone_and_the_notice_thresholds_are_validated():
    from pydantic import ValidationError

    from app.config import HeldPriceNotice, Settlement
    assert "rejected_quote_grace_minutes" not in Settlement.model_fields
    with pytest.raises(ValidationError):
        HeldPriceNotice(rejection_share_pct=100)
    with pytest.raises(ValidationError):
        HeldPriceNotice(no_valid_tick_minutes=0)
    cfg = S.feed.held_price_notice
    assert (cfg.no_valid_tick_minutes, cfg.rejection_window_minutes, cfg.rejection_share_pct, cfg.min_ticks) == (5, 5, 50, 10)
