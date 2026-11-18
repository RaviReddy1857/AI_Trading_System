"""Codex Astra audit of main (18-Nov-2026): each test fails on the code before its fix."""
from __future__ import annotations

import math
import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from app.journal import CASH_INDEX, STOCK_OPTIONS, pool_account, pool_of_trade_type, signal_trade_type
from app.paper import PaperTrader
from app.risk.engine import INCONSISTENT_SIGNAL, decide, is_stock_spread, signal_inconsistencies
from tests.phase5_fixtures import (GOOD_MARKET, SECTORS, FlatCosts, ZeroCosts, account, bull_put, cash_signal, leg,
                                   option_signal)
from tests.phase6_fixtures import NOW, FakeMarks, new_journal, settings

pytestmark = pytest.mark.usefixtures("fixed_clock")


def low_corr(a, b):
    return 1.0 if a == b else 0.1


# ================= 1. HIGH: one consistent snapshot of the real book =================
class _TookMidRead:
    """The journal's connection, with a TOOK recorded by another thread right after the open positions are read."""

    def __init__(self, journal, signal_id):
        self.j, self.real, self.sid, self.thread = journal, journal._db, signal_id, None

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, *args):
        cur = self.real.execute(sql, *args)
        if self.thread is None and "FROM positions WHERE" in sql and "status" in sql:
            self.thread = threading.Thread(
                target=self.j.record_decision, args=(self.sid, {"decision": "TOOK", "fill_price": 1000.0}, NOW))
            self.thread.start()
            self.thread.join(0.5)               # the old code released the lock here: the TOOK lands between reads
        return cur


def test_a_took_recorded_between_the_reads_is_counted_exactly_once():
    s = settings()
    j, _ = new_journal(s)
    j.ensure_equity_start(CASH_INDEX, NOW - timedelta(days=1), s.account.capital_inr)
    sig = replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id="race-1")
    acct0, _ = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())
    d = decide(sig, acct0, GOOD_MARKET, s, ZeroCosts(), low_corr, SECTORS)
    assert d.status == "APPROVED", d.reasons
    j.save_signal(sig, d, NOW, paper=PaperTrader.pending_row(sig, d))
    proxy = _TookMidRead(j, "race-1")
    j._db = proxy
    marks = FakeMarks({"RELIANCE": 1000.0})
    during, _ = pool_account(j, s, CASH_INDEX, NOW, marks, ZeroCosts())
    proxy.thread.join(10)
    j._db = proxy.real
    after, _ = pool_account(j, s, CASH_INDEX, NOW, marks, ZeroCosts())
    on_reliance = lambda a: [p.origin for p in a.positions if p.symbol == "RELIANCE"]   # noqa: E731
    assert on_reliance(during) == ["PENDING"]          # read before the TOOK: still the approved signal
    assert on_reliance(after) == ["SIGNAL"]            # read after it: my position, no longer a reservation
    assert j.decision("race-1")["decision"] == "TOOK"


def test_book_snapshot_matches_the_separate_readers_when_nothing_changes():
    s = settings()
    j, _ = new_journal(s)
    j.ensure_equity_start(CASH_INDEX, NOW - timedelta(days=1), s.account.capital_inr)
    sig = replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id="snap-1")
    d = decide(sig, pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())[0], GOOD_MARKET, s, ZeroCosts(),
               low_corr, SECTORS)
    j.save_signal(sig, d, NOW)
    held, pending = j.book_snapshot(CASH_INDEX, NOW)
    assert held == j.held_positions(CASH_INDEX) == [] and pending == j.pending_reservations(CASH_INDEX, NOW)
    assert [p["signal_id"] for p in pending] == ["snap-1"]
    assert not j._db.in_transaction                     # the read transaction is closed


# ================= 2. HIGH: one leg-based classification for pool and risk path =================
def _reliance_put_spread(direction: str, lots: int = 35):
    sig = bull_put(symbol="RELIANCE", sold=(2500, 30.0), bought=(2400, 10.0), lots=lots, lot_size=250,
                   margin_per_lot=150000.0, asset="STOCK_OPTION", strategy="stock_option_selling")
    if direction == "LONG":                             # the model's LONG rule: stop < entry < target
        return replace(sig, direction="LONG", stop=sig.target, target=sig.stop)
    return replace(sig, direction=direction)


def test_a_hedged_put_spread_labelled_long_is_inconsistent_and_never_sized():
    """Audit case: a hedged RELIANCE put spread labelled LONG took the index/buying path (account capital, no history
    check, no lot cap) and was APPROVED for 35 lots."""
    sig = _reliance_put_spread("LONG")
    assert signal_trade_type(sig) == "STOCK_OPTION_SELLING" and pool_of_trade_type(signal_trade_type(sig)) == STOCK_OPTIONS
    assert is_stock_spread(sig)                         # the same classification as the journal's pool
    d = decide(sig, account(capital_inr=10_000_000.0), GOOD_MARKET, settings(), FlatCosts(), low_corr, SECTORS)
    assert d.status == "NO_TRADE" and INCONSISTENT_SIGNAL in d.reason_codes, d.reasons
    assert d.lots is None and d.facts["would_be"]["lots"] is None    # not sized at all
    assert d.facts["capital_source"] == "stock_option_selling.backtest.capital_inr"


def test_the_same_spread_labelled_spread_takes_the_stock_option_path_with_its_checks():
    sig = _reliance_put_spread("SPREAD")
    assert signal_inconsistencies(sig) == []
    d = decide(sig, account(capital_inr=10_000_000.0, history_verified=False), GOOD_MARKET, settings(), FlatCosts(),
               low_corr, SECTORS)
    assert d.facts["risk_path"] == "stock_option_selling" and d.status == "NO_TRADE"
    assert "PORTFOLIO_UNKNOWN" in d.reason_codes        # the phase 9 history check applies
    d = decide(sig, account(history_verified=True), GOOD_MARKET, settings(), FlatCosts(), low_corr, SECTORS)
    assert d.facts["sizing"]["lots"] <= d.facts["sizing"]["signalled_lots"] == 35   # never more than signalled


def test_a_stock_bull_call_debit_spread_labelled_long_is_consistent_and_uses_the_stock_option_pool():
    legs = [leg("BUY", "CE", 2500, 40.0, 1, 250), leg("SELL", "CE", 2600, 15.0, 1, 250)]
    sig = option_signal(legs, 25.0, 12.5, 50.0, symbol="RELIANCE", asset="STOCK_OPTION", direction="LONG",
                        strategy="reversal_bull_call_spread")
    assert signal_inconsistencies(sig) == [] and is_stock_spread(sig)
    assert pool_of_trade_type(signal_trade_type(sig)) == STOCK_OPTIONS
    d = decide(sig, account(), GOOD_MARKET, settings(), FlatCosts(), low_corr, SECTORS)
    assert d.facts["risk_path"] == "stock_option_selling"


@pytest.mark.parametrize("make, why", [
    (lambda: replace(cash_signal(), direction="SPREAD"), "LONG or SHORT"),
    (lambda: option_signal([leg("BUY", "CE", 24000, 100.0)], 100.0, 50.0, 200.0, direction="SPREAD"), "must be LONG"),
    (lambda: replace(bull_put(), direction="SHORT"), "LONG or SPREAD"),
    (lambda: option_signal([leg("BUY", "CE", 24000, 50.0), leg("SELL", "CE", 24100, 50.0)], 1.0, 2.0, 0.5,
                           direction="SPREAD"), "equal"),
])
def test_other_inconsistent_combinations_are_refused(make, why):
    sig = make()
    problems = signal_inconsistencies(sig)
    assert problems and why in problems[0]
    d = decide(sig, account(), GOOD_MARKET, settings(), FlatCosts(), low_corr, SECTORS)
    assert d.status == "NO_TRADE" and INCONSISTENT_SIGNAL in d.reason_codes


# ================= 3. MEDIUM: created_at <= now < valid_until; every leg's contract still trading =================
from datetime import date, datetime, time  # noqa: E402

from app.quality import IST  # noqa: E402
from app.risk.engine import CONTRACT_EXPIRED, SIGNAL_NOT_YET_VALID, time_problems  # noqa: E402


def _codes(sig, now):
    return {c for c, _ in time_problems(sig, now)}


def test_a_signal_created_after_the_decision_time_is_blocked():
    sig = cash_signal()                                         # created 10:30 IST on 16-Nov
    assert _codes(sig, sig.created_at - timedelta(minutes=1)) == {SIGNAL_NOT_YET_VALID}
    assert _codes(sig, sig.created_at) == set()                 # created_at itself is valid
    d = decide(sig, account(), replace(GOOD_MARKET, now=sig.created_at - timedelta(minutes=1)), settings(), ZeroCosts(),
               low_corr, SECTORS)
    assert d.status == "NO_TRADE" and SIGNAL_NOT_YET_VALID in d.reason_codes


def test_an_expired_contract_is_blocked_even_inside_the_signals_valid_until():
    old = date(2026, 11, 13)                                    # a Friday before NOW (16-Nov)
    sig = option_signal([leg("BUY", "CE", 24000, 100.0, expiry=old)], 100.0, 50.0, 200.0, direction="LONG",
                        strategy="long_option")
    assert CONTRACT_EXPIRED in _codes(sig, NOW)
    d = decide(sig, account(), GOOD_MARKET, settings(), FlatCosts(), low_corr, SECTORS)
    assert d.status == "NO_TRADE" and CONTRACT_EXPIRED in d.reason_codes
    # expiring today: tradeable in the morning, not after continuous derivatives trading ends
    today = NOW.astimezone(IST).date()
    sig = option_signal([leg("BUY", "CE", 24000, 100.0, expiry=today)], 100.0, 50.0, 200.0, direction="LONG",
                        strategy="long_option")
    sig = replace(sig, valid_until=datetime.combine(today, time(23, 0), IST))
    assert CONTRACT_EXPIRED not in _codes(sig, NOW)
    # derivatives trade continuously to 15:40 from 3 Aug 2026 (app/session.py)
    assert CONTRACT_EXPIRED not in _codes(sig, datetime.combine(today, time(15, 39), IST))
    assert CONTRACT_EXPIRED in _codes(sig, datetime.combine(today, time(15, 40), IST))


def test_a_morning_swing_signal_on_the_previous_sessions_close_stays_valid():
    sig = cash_signal(horizon="SWING")
    sig = replace(sig, created_at=sig.created_at - timedelta(hours=19), data_as_of=sig.data_as_of - timedelta(hours=19),
                  valid_until=NOW + timedelta(hours=6))         # built on Friday's close, decided Monday morning
    assert _codes(sig, NOW) == set()
    d = decide(sig, account(), GOOD_MARKET, settings(), ZeroCosts(), low_corr, SECTORS)
    assert not {SIGNAL_NOT_YET_VALID, "SIGNAL_EXPIRED", CONTRACT_EXPIRED} & set(d.reason_codes), d.reasons


# ================= 4. MEDIUM: settlement inputs must be usable; unknown escalates =================
from app.options.settlement import (MARGIN_UNKNOWN, PRICE_UNKNOWN, URGENT, delivery_estimate,  # noqa: E402
                                    delivery_margin, moneyness, settlement_alert)
from tests.test_phase9_settlement import CAP, SOS, at, put_spread  # noqa: E402


@pytest.mark.parametrize("spot", [math.inf, -math.inf, math.nan, -5.0, 0.0])
def test_a_spot_that_is_not_a_finite_positive_price_is_price_unknown_and_escalates(spot):
    pos = put_spread()
    assert moneyness(pos.legs[0], spot, 5.0)["state"] == PRICE_UNKNOWN
    a = settlement_alert(pos, spot, at(7), SOS, CAP)            # E-7: an OTM leg alone would be quiet
    assert a is not None and a["level"] == URGENT
    assert all(x["state"] == PRICE_UNKNOWN for x in a["legs"])
    assert a["estimate"]["position"]["legs"][0]["at_last_price"] is None     # never assumed out of the money
    assert a["estimate"]["margin"]["status"] == MARGIN_UNKNOWN


def test_infinite_spot_no_longer_reads_as_far_out_of_the_money():
    """Before the fix: inf passed `spot > 0`, the distance was infinite, the leg OUT_OF_THE_MONEY and the alert quiet."""
    a = settlement_alert(put_spread(), math.inf, at(3), SOS, CAP)
    assert a is not None and a["level"] == URGENT and a["legs"][0]["state"] == PRICE_UNKNOWN


@pytest.mark.parametrize("base", [-1.0, math.nan, math.inf, "100"])
def test_a_margin_base_that_is_not_finite_and_non_negative_is_margin_unknown_and_escalates(base):
    pos = put_spread()
    m = delivery_margin(pos, 990.0, 2, SOS, base, CAP)
    assert m["status"] == MARGIN_UNKNOWN and m["margin_inr"] is None and m.get("invalid_input") is True
    est = delivery_estimate(pos, 990.0, 2, SOS, CAP, base)
    assert est["margin"]["status"] == MARGIN_UNKNOWN
    a = settlement_alert(pos, 1100.0, at(7), SOS, CAP, base_margin_inr=base)   # E-7 and far OTM: quiet otherwise
    assert a is not None and a["level"] == URGENT and a["invalid_inputs"]
    assert settlement_alert(pos, 1100.0, at(7), SOS, CAP, base_margin_inr=50000.0) is None   # valid: still quiet


# ================= 5. MEDIUM: verified holiday coverage, kept apart from holiday rows =================
from app.calendar import MarketCalendar  # noqa: E402
from app.manual_data import parse_holidays  # noqa: E402
from app.options.settlement import HOLIDAY_COVERAGE_MISSING, day_count, rules_view  # noqa: E402

NSE_2026 = {date(2026, 1, 26): "Republic Day", date(2026, 11, 24): "Gurpurb", date(2026, 12, 25): "Christmas"}


def test_holiday_rows_alone_do_not_prove_coverage_and_the_count_is_conservative():
    exp = date(2026, 12, 29)
    cal = MarketCalendar(NSE_2026)
    dc = day_count(exp, cal)
    assert dc.covered is False and HOLIDAY_COVERAGE_MISSING in dc.note and "COVERAGE through" in dc.note
    covered = day_count(exp, MarketCalendar({**NSE_2026, date(2026, 1, 1): "COVERAGE through 2026-12-31 NSE"}))
    assert covered.covered is True
    today = date(2026, 12, 21)
    assert dc.left(today) == covered.left(today) - 1             # one session earlier: never later
    assert HOLIDAY_COVERAGE_MISSING in rules_view(SOS, cal, today)["calendar"]["note"]


def test_a_coverage_row_is_not_a_holiday_and_ranges_join_up():
    cal = MarketCalendar({**NSE_2026, date(2026, 1, 1): "COVERAGE through 2026-12-31 NSE circular",
                          date(2027, 1, 1): "COVERAGE through 2027-06-30 NSE circular"})
    assert date(2026, 1, 1) not in cal.holidays and cal.is_trading_day(date(2026, 1, 1))
    assert date(2026, 12, 25) in cal.holidays
    assert cal.covers(date(2026, 12, 1), date(2027, 3, 1)) is True
    assert cal.covers(date(2027, 6, 1), date(2027, 7, 15)) is False


def test_the_holidays_file_format_for_coverage_rows():
    text = ("Sr. No,Date,Day,Description\n"
            "1,2027-01-26,Tuesday,Republic Day\n"
            ",2027-01-01,,COVERAGE through 2027-12-31 NSE circular NSE/CMO/00000 of 01-Dec-2026\n")
    parsed, errors = parse_holidays(text)
    assert errors == [] and MarketCalendar(parsed).covers(date(2027, 1, 1), date(2027, 12, 31))
    assert MarketCalendar(parsed).holidays == frozenset({date(2027, 1, 26)})
    _, errors = parse_holidays("Date,Description\n2027-01-01,COVERAGE of 2027\n")
    assert errors and "COVERAGE through YYYY-MM-DD" in errors[0]
    _, errors = parse_holidays("Date,Description\n2027-01-26,Republic Day\n2027-01-26,COVERAGE through 2027-12-31\n")
    assert errors


# ================= 6. MEDIUM: kill-switch inputs must be real =================
from types import SimpleNamespace  # noqa: E402

from app.risk.limits import kill_switch  # noqa: E402

KS = SimpleNamespace(quarantine_window_minutes=5, quarantine_max_events=20)


@pytest.mark.parametrize("market_open", [None, "yes", 1, 0, "False", math.nan])
def test_market_open_that_is_not_a_bool_turns_the_switch_on(market_open):
    out = kill_switch("NIFTY", "LIVE", 0, market_open, KS)
    assert out and any("market hours not known" in x for x in out)


@pytest.mark.parametrize("count", [None, -1, 2.5, math.nan, math.inf, True, "3"])
def test_a_quarantine_count_that_is_not_a_non_negative_whole_number_turns_the_switch_on(count):
    out = kill_switch("NIFTY", "LIVE", count, True, KS)
    assert out and any("quarantined-tick count not known" in x for x in out)


def test_valid_inputs_keep_the_switch_off():
    assert kill_switch("NIFTY", "LIVE", 0, True, KS) == []
    assert kill_switch("NIFTY", "LIVE", 19, True, KS) == []
    assert kill_switch("NIFTY", "LIVE", 20, True, KS) != []
