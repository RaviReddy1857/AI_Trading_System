"""Codex Astra review of 4ea35e0..4e9fb4d (19-Nov-2026), item 3: FORCED_EXPIRY_EXIT and SETTLEMENT share one
forced-exit cutoff. Items 1 and 2 of that review (rejected ticks and the auction as quote statuses) were replaced by
the last-valid-price design: tests/test_settlement_last_price.py."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from app.calendar import MarketCalendar
from app.options.settlement import DEADLINE_UNVERIFIED
from app.positions import ExitCallBook, ExitInputs, evaluate_positions
from app.quality import IST
from tests.phase6_fixtures import ZeroCosts, new_journal, settings

S = settings()
EXP = date(2026, 12, 29)                                                    # a Tuesday


def ist(h, m, s=0, d=date(2026, 11, 16)):
    return datetime.combine(d, time(h, m, s), IST).astimezone(timezone.utc)


def both_calls(cal, today: date):
    j, store = new_journal(S)
    book = ExitCallBook(store, S)
    j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-12-01", "margin_inr": 90000,
                           "legs": [{"action": "SELL", "right": "PE", "strike": 1300, "expiry": EXP.isoformat(),
                                     "lots": 1, "lot_size": 500, "price": 12.0},
                                    {"action": "BUY", "right": "PE", "strike": 1260, "expiry": EXP.isoformat(),
                                     "lots": 1, "lot_size": 500, "price": 4.0}]}, ist(9, 30, d=date(2026, 12, 1)))
    now = ist(9, 20, d=today)
    due = evaluate_positions(j, book, S, cal, now, lambda p: ExitInputs(now, [12.0, 4.0], spot=1290.0,  # ITM: an alert
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
