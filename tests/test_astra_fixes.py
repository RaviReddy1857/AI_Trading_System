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
