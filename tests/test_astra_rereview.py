"""Codex Astra re-review of main at 4ea35e0 (19-Nov-2026), items 2-6: each test fails on the code before its fix.
Item 1 (close records in the same snapshot): tests/test_astra_rereview_snapshot.py."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.alerts import exit_text
from app.calendar import MarketCalendar, coverage_through
from app.config import load_settings
from app.engine import SignalEngine
from app.options.reversal import build_reversal_strategies
from app.options.settlement import (DEADLINE_UNVERIFIED, PRICE_UNKNOWN, URGENT, HeldLeg, HeldPosition, days_left,
                                    rules_view, settlement_alert)
from app.positions import ExitCallBook
from app.pricing import INVALID, Marker
from app.quality import IST
from app.risk.engine import MARGIN_UNKNOWN, decide
from tests.phase5_fixtures import GOOD_MARKET, SECTORS, account
from tests.phase6_fixtures import ZeroCosts, new_journal
from tests.phase6_fixtures import settings as p6_settings
from tests.test_phase9_core import settings as p9_settings
from tests.test_phase9_reversal import reversal_ctx


def low_corr(a, b):
    return 1.0 if a == b else 0.1


# ================= 2. MEDIUM: a debit spread commits its net debit plus costs; a credit spread needs a margin =================
def test_a_producer_shaped_reversal_bull_call_goes_through_the_capital_check_to_a_decision():
    s = p9_settings()
    _, call = build_reversal_strategies(s)
    ctx, _ = reversal_ctx(s)
    sigs = call.evaluate(ctx)
    assert sigs, call.last_no_trade
    sig = sigs[0]
    assert "margin_per_lot_inr" not in sig.details and sig.details["margin_inr"] is None    # the producer gives none
    mkt = replace(GOOD_MARKET, now=sig.created_at + timedelta(minutes=1), regime=sig.regime)
    d = decide(sig, account(capital_inr=s.account.capital_inr, history_verified=True), mkt, s, ctx.costs, low_corr,
               SECTORS)
    assert MARGIN_UNKNOWN not in d.reason_codes, d.reasons
    assert d.status == "APPROVED", d.reasons
    assert d.facts["risk_path"] == "stock_option_selling" and d.facts["sizing"]["structure"] == "DEBIT_SPREAD"
    bought, sold = sig.legs
    lot = bought.lot_size
    paid = round((bought.price - sold.price) * lot * d.lots, 2)
    # committed = the sized net debit paid in full + the estimated costs = the spread's maximum loss
    assert d.margin_inr == pytest.approx(paid + d.costs_inr, abs=0.02)
    assert d.margin_inr == pytest.approx(d.risk_inr, abs=0.02)
    assert d.facts["capital_fit"]["capital_source"] == "stock_option_selling.backtest.capital_inr"


def test_a_credit_spread_without_a_verified_margin_entry_is_still_margin_unknown():
    s = p9_settings(stock_option_selling={"min_credit_to_max_loss": 0})
    put, _ = build_reversal_strategies(s)
    ctx, _ = reversal_ctx(s)
    sigs = put.evaluate(ctx)
    assert sigs, put.last_no_trade
    sig = sigs[0]
    mkt = replace(GOOD_MARKET, now=sig.created_at + timedelta(minutes=1), regime=sig.regime)
    acct = account(capital_inr=s.account.capital_inr, history_verified=True)
    assert sig.details.get("margin_per_lot_inr")                     # the producer's verified entry: margin known
    ok = decide(sig, acct, mkt, s, ctx.costs, low_corr, SECTORS)
    assert MARGIN_UNKNOWN not in ok.reason_codes, ok.reasons
    unverified = replace(sig, details={k: v for k, v in sig.details.items() if k != "margin_per_lot_inr"})
    d = decide(unverified, acct, mkt, s, ctx.costs, low_corr, SECTORS)
    assert d.status == "NO_TRADE" and MARGIN_UNKNOWN in d.reason_codes   # never assumed for a credit spread


# ================= 3. MEDIUM: the live reader carries the quote's status to the settlement alert =================
E_NOW = datetime(2026, 11, 16, 5, 1, tzinfo=timezone.utc)          # Monday 10:31 IST
COVERED = MarketCalendar({date(2026, 1, 1): "COVERAGE through 2026-12-31 test list"})


class _Hub:
    """The hub's quote for the stock as the test sets it (the quality gate's last good tick and its status)."""

    def __init__(self, q):
        self.q = q

    def quote(self, symbol, now):
        return self.q


class _Risk:
    def market_state(self, index, now):
        return replace(GOOD_MARKET, now=now)


def _expiry_at(n: int, cal) -> date:
    today = E_NOW.astimezone(IST).date()
    d = today
    while days_left(today, d, cal) != n:
        d += timedelta(days=1)
    return d


def _exit_calls_with(quote, left: int = 8):
    """The engine's exit evaluation (reader -> ExitInputs -> settlement alert -> calls due) for a held stock put
    spread at E-`left` (default 8) with the sold 1300 put far out of the money, the stock's quote being `quote`."""
    s = p6_settings()
    j, store = new_journal(s)
    exp = _expiry_at(left, COVERED)
    j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-16", "margin_inr": 90000,
                           "legs": [{"action": "SELL", "right": "PE", "strike": 1300, "expiry": exp.isoformat(),
                                     "lots": 1, "lot_size": 500, "price": 12.0},
                                    {"action": "BUY", "right": "PE", "strike": 1260, "expiry": exp.isoformat(),
                                     "lots": 1, "lot_size": 500, "price": 4.0}]}, E_NOW)
    hub = _Hub(quote)
    eng = SignalEngine(settings=s, store=store, hub=hub, journal=j, marker=Marker(s, store, hub, "sim"), risk=_Risk(),
                       regime=None, calendar=COVERED, desk=None, source="sim", costs=ZeroCosts(), execbook=None,
                       paper=None, exits=ExitCallBook(store, s), now_fn=lambda: E_NOW, strategies=[])
    return eng.evaluate_exits(E_NOW, "TRENDING_UP", five={}, daily={"RELIANCE": []})


def _settlement(due):
    return [c for c in due if c["kind"] == "SETTLEMENT"]


def test_at_e8_an_absent_quote_is_silent():
    due = _exit_calls_with({"status": "NO_DATA", "bid": None, "ask": None, "last": None, "at": None})
    assert _settlement(due) == []


STALE_Q = {"status": "STALE", "bid": 1400.0, "ask": 1401.0, "last": 1400.5, "at": E_NOW - timedelta(minutes=30)}
MISSING_Q = {"status": "NO_DATA", "bid": None, "ask": None, "last": None, "at": None}


def test_at_e8_a_stale_quote_is_silent_like_a_missing_one():
    assert _settlement(_exit_calls_with(STALE_Q)) == []          # your decision of 19-Nov-2026: STALE is absent


@pytest.mark.parametrize("left", [7, 6, 5, 3, 1])
def test_from_e7_inside_a_stale_quote_escalates_exactly_like_a_missing_one(left):
    stale, missing = _settlement(_exit_calls_with(STALE_Q, left)), _settlement(_exit_calls_with(MISSING_Q, left))
    assert len(stale) == len(missing) == 1
    s, m = stale[0], missing[0]
    assert s["level"] == m["level"] == URGENT and s["reason"] == m["reason"] and s["action"] == m["action"]
    assert PRICE_UNKNOWN in s["reason"] and "rejected" not in s["reason"]
    sa, ma = s["values"]["alert"], m["values"]["alert"]
    assert sa["trading_days_left"] == ma["trading_days_left"] == left
    assert sa["invalid_inputs"] == ma["invalid_inputs"] == [] and sa["legs"] == ma["legs"]


@pytest.mark.parametrize("quote, why", [
    ({"status": "LIVE", "bid": 1410.0, "ask": 1400.0, "last": 1405.0, "at": E_NOW}, "CROSSED"),     # bid above ask
    ({"status": "LIVE", "bid": None, "ask": None, "last": math.nan, "at": E_NOW}, INVALID),         # no finite last
])
def test_at_e8_a_malformed_quote_is_urgent(quote, why):
    due = _settlement(_exit_calls_with(quote))
    assert len(due) == 1 and due[0]["level"] == URGENT
    a = due[0]["values"]["alert"]
    assert a["trading_days_left"] == 8 and a["legs"][0]["level"] is None          # E-8: quiet by the day count alone
    assert any(PRICE_UNKNOWN in x and "rejected" in x and why in x for x in a["invalid_inputs"])
    text = exit_text({**due[0], "repeat_no": 1})
    assert "rejected" in due[0]["reason"] and "rejected" in text                  # the Telegram text says why


def test_the_reader_tells_absent_from_rejected():
    s = p6_settings()
    _, store = new_journal(s)

    def read(q):
        return Marker(s, store, _Hub(q), "sim").spot_reading("RELIANCE", E_NOW)

    ok = read({"status": "LIVE", "bid": 1400.0, "ask": 1401.0, "last": 1400.5, "at": E_NOW})
    assert (ok.price, ok.status, ok.rejected) == (1400.5, "LIVE", False)
    for q in ({"status": "NO_DATA", "bid": None, "ask": None, "last": None, "at": None},
              {"status": "STALE", "bid": 1400.0, "ask": 1401.0, "last": 1400.5, "at": E_NOW},
              {"status": "CLOSED", "bid": 1400.0, "ask": 1401.0, "last": 1400.5, "at": E_NOW}):
        r = read(q)
        assert r.price is None and not r.rejected and r.why
    bad = read({"status": "LIVE", "bid": 1410.0, "ask": 1400.0, "last": 1405.0, "at": E_NOW})
    assert bad.price is None and bad.rejected and bad.status == "CROSSED"
    neg = Marker(s, store, _Hub({"status": "LIVE", "bid": None, "ask": None, "last": -1.0, "at": E_NOW}), "sim")
    assert neg.spot("RELIANCE", E_NOW) is None and neg.spot_reading("RELIANCE", E_NOW).status == INVALID


# ================= 4. MEDIUM: "COVERAGE through YYYY-MM-DD" from the start, optional trailing source =================
@pytest.mark.parametrize("note, end", [
    ("COVERAGE through 2027-12-31", date(2027, 12, 31)),
    ("COVERAGE through 2027-12-31 NSE circular NSE/CMO/00000", date(2027, 12, 31)),
    ("  COVERAGE  through 2027-06-30  ", date(2027, 6, 30)),
])
def test_a_coverage_row_that_reads_coverage_through_a_date_is_accepted(note, end):
    assert coverage_through(note) == end
    assert MarketCalendar({date(2027, 1, 1): note}).covers(date(2027, 1, 1), end)


@pytest.mark.parametrize("note", [
    "COVERAGE NOT VERIFIED through 2027-12-31",
    "COVERAGE of 2027 through 2027-12-31",
    "COVERAGE (partial) through 2027-12-31 NSE",
    "NSE COVERAGE through 2027-12-31",
    "COVERAGE through 2027-12-31x",
    "COVERAGE through 2027-12-31, to be checked",
    "COVERAGE through 2027-13-01",
    "COVERAGE through",
    "COVERAGE",
    "",
    None,
])
def test_every_other_coverage_description_is_rejected(note):
    from app.manual_data import parse_holidays
    assert coverage_through(note) is None
    cal = MarketCalendar({date(2027, 1, 1): note or "COVERAGE"})
    assert cal.coverage == () and not cal.covers(date(2027, 1, 1), date(2027, 1, 31))
    if note and note.upper().startswith("COVERAGE") and "," not in note:
        _, errors = parse_holidays(f"Date,Description\n2027-01-01,{note}\n")
        assert errors and "COVERAGE through YYYY-MM-DD" in errors[0]


# ================= 5. MEDIUM: a deadline counted without a verified calendar is marked UNVERIFIED =================
EXP5 = date(2026, 12, 29)                                       # a Tuesday
TWO_MISSING = {date(2026, 12, 24): "holiday A", date(2026, 12, 25): "holiday B"}   # both in the expiry month
AT5 = datetime.combine(date(2026, 12, 17), time(10, 0), IST).astimezone(timezone.utc)


def _spread():
    return HeldPosition("RELIANCE", EXP5, (HeldLeg("SELL", "PE", 1000.0, 1, 250), HeldLeg("BUY", "PE", 980.0, 1, 250)))


def test_with_two_holidays_missing_the_computed_deadline_is_late_and_marked_unverified():
    sos = load_settings().stock_option_selling
    truth = MarketCalendar({**TWO_MISSING, date(2026, 1, 1): "COVERAGE through 2026-12-31 NSE"})
    entered = MarketCalendar({})                                 # neither holiday entered, no COVERAGE row
    good = settlement_alert(_spread(), 990.0, AT5, sos, 1_000_000.0, calendar=truth)
    bad = settlement_alert(_spread(), 990.0, AT5, sos, 1_000_000.0, calendar=entered)
    assert good["deadline_verified"] is True and good["deadline_note"] is None
    assert DEADLINE_UNVERIFIED not in good["action"]
    # weekdays less one covers ONE missing holiday; with two the computed deadline is after the true one
    assert datetime.fromisoformat(bad["deadline"]) > datetime.fromisoformat(good["deadline"])
    assert bad["deadline_verified"] is False and bad["deadline_note"] == DEADLINE_UNVERIFIED
    assert DEADLINE_UNVERIFIED in bad["action"]                 # the card and the exit-call API
    assert DEADLINE_UNVERIFIED == "deadline unverified: holiday calendar missing; confirm before relying on it"
    rv = rules_view(sos, entered, date(2026, 12, 17))["calendar"]      # the settlement API
    assert rv["deadline_verified"] is False and rv["deadline_note"] == DEADLINE_UNVERIFIED
    assert rules_view(sos, truth, date(2026, 10, 1))["calendar"]["deadline_verified"] is True   # +70 days: 2026 only


def test_the_unverified_deadline_reaches_the_telegram_text_and_the_overdue_alert():
    from app.journal import Leg, Position
    from app.positions import settlement_call
    s = p6_settings()
    legs = (Leg("SELL", "PE", 1000.0, EXP5, 1, 250, 12.0), Leg("BUY", "PE", 980.0, EXP5, 1, 250, 4.0))
    pos = Position("p", "MANUAL", None, "STOCK_OPTION_SELLING", "STOCK_OPTIONS", "RELIANCE", "SPREAD", legs, 1, 8.0,
                   datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc), margin_inr=90000.0, horizon="MONTHLY")
    c = settlement_call(pos, 990.0, AT5, s, MarketCalendar({}))
    text = exit_text({**c, "symbol": "RELIANCE", "trade_type": "STOCK_OPTION_SELLING", "hedged": True})
    assert DEADLINE_UNVERIFIED in text
    late = datetime.combine(EXP5, time(15, 59), IST).astimezone(timezone.utc)
    od = settlement_alert(_spread(), 990.0, late, s.stock_option_selling, 1_000_000.0, calendar=MarketCalendar({}))
    assert od["overdue"]["deadline_verified"] is False and DEADLINE_UNVERIFIED in od["overdue"]["text"]


# ================= 6. LOW: the read-only SQLite URI is built from the resolved path =================
def test_the_read_only_uri_opens_a_real_file_read_only(tmp_path):
    import sqlite3
    from pathlib import Path
    p = tmp_path / "x.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE t (a)")
    db.commit()
    db.close()
    uri = Path(p).resolve().as_uri() + "?mode=ro"
    assert uri.startswith("file:///")
    ro = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t VALUES (1)")
    finally:
        ro.close()
