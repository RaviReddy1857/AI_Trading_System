"""Codex Astra re-review of main at 4ea35e0 (19-Nov-2026), items 2-6: each test fails on the code before its fix.
Item 1 (close records in the same snapshot): tests/test_astra_rereview_snapshot.py."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.alerts import exit_text
from app.calendar import MarketCalendar, coverage_through
from app.config import load_settings
from app.options.reversal import build_reversal_strategies
from app.options.settlement import DEADLINE_UNVERIFIED, HeldLeg, HeldPosition, rules_view, settlement_alert
from app.quality import IST
from app.risk.engine import MARGIN_UNKNOWN, decide
from tests.phase5_fixtures import GOOD_MARKET, SECTORS, account
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


# ================= 3. (replaced 19-Nov-2026) =================
# The quote-status escalation of item 3 was replaced by the last-valid-price design (user, 19-Nov-2026):
# tests/test_settlement_last_price.py.


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
