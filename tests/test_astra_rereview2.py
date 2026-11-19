"""Codex Astra review of 4ea35e0..4e9fb4d (19-Nov-2026): rejected ticks, the closing auction, one forced-exit cutoff.
Each test fails on the code before its fix."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.calendar import MarketCalendar
from app.engine import SignalEngine
from app.hub import MarketHub
from app.models import Tick
from app.options.settlement import DEADLINE_UNVERIFIED, PRICE_UNKNOWN, URGENT, days_left
from app.positions import ExitCallBook, ExitInputs, evaluate_positions
from app.pricing import REJECTED, STALE, Marker, SpotReading
from app.quality import IST
from app.store import Store
from tests.phase5_fixtures import GOOD_MARKET
from tests.phase6_fixtures import ZeroCosts, new_journal, settings

S = settings()
COVERED = MarketCalendar({date(2026, 1, 1): "COVERAGE through 2026-12-31 test list"})
DAY = date(2026, 11, 16)                                                   # a Monday


def ist(h, m, s=0, d=DAY):
    return datetime.combine(d, time(h, m, s), IST).astimezone(timezone.utc)


def tick(when, last=1400.0, bid=1399.9, ask=1400.1):
    return Tick("RELIANCE", last, bid=bid, ask=ask, source="breeze", received_at=when)


def crossed(when):
    return tick(when, 1400.0, bid=1410.0, ask=1390.0)                   # bid above ask: rejected by the quality gate


def new_hub():
    return MarketHub(S, Store(":memory:"), "breeze", MarketCalendar())


class _Risk:
    def market_state(self, index, now):
        return GOOD_MARKET.__class__(**{**GOOD_MARKET.__dict__, "now": now})


def expiry_at(n: int, today: date = DAY) -> date:
    d = today
    while days_left(today, d, COVERED) != n:
        d += timedelta(days=1)
    return d


def engine_for(hub, left: int, now: datetime):
    """A journal with a held RELIANCE put spread (sold 1300 PE, far out of the money at 1400) at E-`left`, and the
    engine whose exit evaluation reads the stock's quote from `hub` (reader -> ExitInputs -> settlement alert)."""
    j, store = new_journal(S)
    exp = expiry_at(left, now.astimezone(IST).date())
    pos = j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-16", "margin_inr": 90000,
                                 "legs": [{"action": "SELL", "right": "PE", "strike": 1300, "expiry": exp.isoformat(),
                                           "lots": 1, "lot_size": 500, "price": 12.0},
                                          {"action": "BUY", "right": "PE", "strike": 1260, "expiry": exp.isoformat(),
                                           "lots": 1, "lot_size": 500, "price": 4.0}]}, ist(9, 30))
    book = ExitCallBook(store, S)
    eng = SignalEngine(settings=S, store=store, hub=hub, journal=j, marker=Marker(S, store, hub, "breeze"),
                       risk=_Risk(), regime=None, calendar=COVERED, desk=None, source="breeze", costs=ZeroCosts(),
                       execbook=None, paper=None, exits=book, now_fn=lambda: now, strategies=[])
    return eng, j, book, pos


def settlement_due(hub, now, left=8):
    eng, j, book, pos = engine_for(hub, left, now)
    due = [c for c in eng.evaluate_exits(now, "TRENDING_UP", five={}, daily={"RELIANCE": []}) if c["kind"] == "SETTLEMENT"]
    return due, j, book, pos


def reading(hub, now):
    return Marker(S, Store(":memory:"), hub, "breeze").spot_reading("RELIANCE", now)


# ================= 1. HIGH: a rejected tick is kept for settlement until a newer valid tick =================
def test_a_crossed_tick_without_a_prior_good_tick_is_stale_then_urgent_after_the_grace():
    assert S.stock_option_selling.settlement.rejected_quote_grace_minutes == 5
    hub = new_hub()
    hub.on_tick(crossed(ist(10, 30)))
    q = hub.quote("RELIANCE", ist(10, 31))
    assert q["last"] is None and q["rejected_why"] == "CROSSED_QUOTE"       # never a price
    r = reading(hub, ist(10, 34, 59))
    assert (r.price, r.status, r.rejected) == (None, STALE, False) and "CROSSED_QUOTE" in r.why
    assert settlement_due(hub, ist(10, 34, 59))[0] == []                    # within the grace: silent at E-8
    r = reading(hub, ist(10, 35))
    assert r.status == REJECTED and r.rejected
    due = settlement_due(hub, ist(10, 35))[0]
    assert len(due) == 1 and due[0]["level"] == URGENT and "CROSSED_QUOTE" in due[0]["reason"]


def test_a_crossed_tick_after_a_good_tick_hides_the_old_price_and_escalates_after_the_grace():
    hub = new_hub()
    hub.on_tick(tick(ist(10, 30)))
    hub.on_tick(crossed(ist(10, 30, 10)))
    assert hub.quote("RELIANCE", ist(10, 30, 20))["last"] == 1400.0        # the good tick stays the hub's price
    assert hub.live_quote("RELIANCE", ist(10, 30, 20))["last"] == 1400.0   # strategies: unchanged
    r = reading(hub, ist(10, 30, 20))                                       # the good tick is still fresh ...
    assert r.price is None and r.status == STALE                            # ... but never LIVE for settlement
    assert settlement_due(hub, ist(10, 34, 59))[0] == []
    due = settlement_due(hub, ist(10, 35))[0]                               # 5 minutes since the last valid tick
    assert len(due) == 1 and due[0]["level"] == URGENT
    assert any("rejected" in x and "CROSSED_QUOTE" in x for x in due[0]["values"]["alert"]["invalid_inputs"])


def test_a_rejection_followed_by_a_valid_tick_within_the_grace_never_escalates():
    hub = new_hub()
    hub.on_tick(tick(ist(10, 30)))
    hub.on_tick(crossed(ist(10, 31)))
    hub.on_tick(tick(ist(10, 34), 1401.0, 1400.9, 1401.1))                  # valid again within 5 minutes
    q = hub.quote("RELIANCE", ist(10, 34, 10))
    assert q["rejected_at"] is None and q["last"] == 1401.0
    r = reading(hub, ist(10, 34, 10))
    assert (r.price, r.status) == (1401.0, "LIVE")
    assert settlement_due(hub, ist(10, 34, 10))[0] == []
    hub.on_tick(tick(ist(10, 38)))                                          # and still nothing later on
    assert settlement_due(hub, ist(10, 38, 10))[0] == []


def test_a_quarantined_jump_and_a_non_finite_tick_are_rejections_too():
    hub = new_hub()
    hub.on_tick(tick(ist(10, 0)))
    hub.on_tick(tick(ist(10, 0, 5), 2000.0, 1999.9, 2000.1))               # a price jump: quarantined
    assert hub.quote("RELIANCE", ist(10, 0, 6))["rejected_why"]
    assert reading(hub, ist(10, 6)).status == REJECTED
    hub2 = new_hub()
    hub2.on_tick(tick(ist(10, 0), float("nan"), None, None))
    assert hub2.quote("RELIANCE", ist(10, 0, 1))["last"] is None and hub2.quote("RELIANCE", ist(10, 0, 1))["rejected_at"]


def test_rejected_ticks_stay_out_of_bars_and_first_quotes():
    hub = new_hub()
    hub.on_tick(crossed(ist(10, 30, 1)))
    assert hub.first_quote("RELIANCE", ist(10, 30)) is None                # never a fill price
    hub.on_tick(tick(ist(10, 30, 5)))
    assert hub.first_quote("RELIANCE", ist(10, 30))["last"] == 1400.0


def test_a_rejection_of_an_earlier_day_or_outside_continuous_trading_does_not_escalate():
    hub = new_hub()
    hub.on_tick(crossed(ist(15, 20, d=date(2026, 11, 13))))                 # Friday
    assert reading(hub, ist(10, 0)).status != REJECTED                      # Monday: no tick yet today
    assert reading(hub, ist(16, 0, d=date(2026, 11, 13))).status == "CLOSED"


# ================= 2. MEDIUM: the closing auction is an absent quote, never kept as an escalation =================
def test_in_the_auction_at_e8_the_quote_is_absent_and_silent():
    hub = new_hub()
    hub.on_tick(tick(ist(15, 10)))                                          # continuous trading (the auction from 15:15)
    r = reading(hub, ist(15, 25))
    assert (r.price, r.status, r.rejected) == (None, "AUCTION", False)
    assert settlement_due(hub, ist(15, 25), left=8)[0] == []


def test_in_the_auction_from_e7_it_escalates_like_missing_but_is_never_kept():
    hub = new_hub()
    hub.on_tick(tick(ist(15, 10)))
    due, j, book, pos = settlement_due(hub, ist(15, 25), left=7)
    assert len(due) == 1 and due[0]["level"] == URGENT and PRICE_UNKNOWN in due[0]["reason"]
    assert "rejected" not in due[0]["reason"] and due[0]["values"]["alert"]["invalid_inputs"] == []
    assert j.position(pos.position_id).plan.get("settlement_level") is None     # not kept on the position
    # a missing quote at E-7 gives the same alert, and that one IS kept
    m_due, mj, _, mpos = settlement_due(new_hub(), ist(10, 31), left=7)
    assert m_due[0]["level"] == URGENT and m_due[0]["reason"] == due[0]["reason"]
    assert mj.position(mpos.position_id).plan.get("settlement_level") == URGENT
    # the next evaluation with a usable price (far out of the money at E-7: no alert) resolves the auction's call
    later = ist(15, 40)
    sq = SpotReading(1400.0, "LIVE", "")
    evaluate_positions(j, book, S, COVERED, later,
                       lambda p: ExitInputs(later, None, spot=1400.0, spot_quote=sq, regime="TRENDING_UP"), ZeroCosts())
    calls = {c["kind"]: c for c in book.calls(pos.position_id)}
    assert calls["SETTLEMENT"]["active"] is False and "closing auction" in calls["SETTLEMENT"]["resolved_why"]
    assert j.position(pos.position_id).plan.get("settlement_level") is None


# ================= 3. MEDIUM: FORCED_EXPIRY_EXIT and SETTLEMENT share one forced-exit cutoff =================
EXP = date(2026, 12, 29)                                                    # a Tuesday


def both_calls(cal, today: date):
    j, store = new_journal(S)
    book = ExitCallBook(store, S)
    j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-12-01", "margin_inr": 90000,
                           "legs": [{"action": "SELL", "right": "PE", "strike": 1300, "expiry": EXP.isoformat(),
                                     "lots": 1, "lot_size": 500, "price": 12.0},
                                    {"action": "BUY", "right": "PE", "strike": 1260, "expiry": EXP.isoformat(),
                                     "lots": 1, "lot_size": 500, "price": 4.0}]}, ist(9, 30, d=date(2026, 12, 1)))
    now = ist(9, 20, d=today)
    sq = SpotReading(1290.0, "LIVE", "")                                    # in the money: a settlement alert
    due = evaluate_positions(j, book, S, cal, now, lambda p: ExitInputs(now, [12.0, 4.0], spot=1290.0, spot_quote=sq,
                                                                        regime="TRENDING_UP"), ZeroCosts())
    return {c["kind"]: c for c in due}


def _forced_cutoff(settlement_call):
    a = settlement_call["values"]["alert"]
    return next(c["at"] for c in a["cutoffs"] if c["rule"] == "forced exit"), a["deadline_verified"]


def test_a_weekday_holiday_near_expiry_moves_both_calls_to_the_same_day():
    cal = MarketCalendar({date(2026, 12, 25): "Christmas", date(2026, 1, 1): "COVERAGE through 2026-12-31 NSE"})
    # 28, 24, 23, 22, 21 Dec: E-5 is Monday 21 Dec (without the holiday it would be Tuesday 22 Dec)
    assert "FORCED_EXPIRY_EXIT" not in both_calls(cal, date(2026, 12, 18))
    calls = both_calls(cal, date(2026, 12, 21))
    fx, st = calls["FORCED_EXPIRY_EXIT"], calls["SETTLEMENT"]
    at, verified = _forced_cutoff(st)
    assert fx["values"]["deadline"] == at == ist(15, 0, d=date(2026, 12, 21)).astimezone(IST).isoformat()
    assert fx["values"]["deadline_verified"] is verified is True
    assert "21-Dec-2026" in fx["reason"] and DEADLINE_UNVERIFIED not in fx["reason"] + fx["action"]
    assert DEADLINE_UNVERIFIED not in st["action"]


def test_without_coverage_both_calls_give_the_same_earlier_day_and_the_same_warning():
    cal = MarketCalendar({date(2026, 12, 25): "Christmas"})                 # holiday entered, no COVERAGE row
    # not covered: one session earlier for both - E-6 by the entered list, Friday 18 Dec
    assert "FORCED_EXPIRY_EXIT" not in both_calls(cal, date(2026, 12, 17))
    calls = both_calls(cal, date(2026, 12, 18))
    fx, st = calls["FORCED_EXPIRY_EXIT"], calls["SETTLEMENT"]
    at, verified = _forced_cutoff(st)
    assert fx["values"]["deadline"] == at == ist(15, 0, d=date(2026, 12, 18)).astimezone(IST).isoformat()
    assert fx["values"]["deadline_verified"] is verified is False
    assert DEADLINE_UNVERIFIED in fx["reason"] and DEADLINE_UNVERIFIED in fx["action"]
    assert DEADLINE_UNVERIFIED in st["action"]
