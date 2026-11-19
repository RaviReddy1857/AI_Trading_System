"""Codex Astra re-review of main at 4ea35e0 (19-Nov-2026): each test fails on the code before its fix."""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from app.journal import CASH_INDEX, pool_account
from app.paper import PaperTrader
from app.risk.engine import decide
from tests.phase5_fixtures import GOOD_MARKET, SECTORS, ZeroCosts, cash_signal
from tests.phase6_fixtures import NOW, FakeMarks, new_journal, settings

pytestmark = pytest.mark.usefixtures("fixed_clock")


def low_corr(a, b):
    return 1.0 if a == b else 0.1


# ================= 1. HIGH: close records read in the same snapshot as the open positions =================
class _ClosedMidRead:
    """The journal's connection, with a close recorded by another thread right after the open positions are read."""

    def __init__(self, journal, position_id, exit_price):
        self.j, self.real, self.pid, self.exit, self.thread = journal, journal._db, position_id, exit_price, None

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, *args):
        cur = self.real.execute(sql, *args)
        if self.thread is None and "FROM positions WHERE" in sql and "status='OPEN'" in sql:
            self.thread = threading.Thread(target=self.j.close_position,
                                           args=(self.pid, {"exit_price": self.exit}, NOW, ZeroCosts()))
            self.thread.start()
            self.thread.join(0.5)               # the old code read realised P&L later: the close lands in between
        return cur


def _today_total(acct):
    """What the daily loss stop sees: realised today plus every open position's P&L today."""
    return acct.realised_today_inr + sum(p.open_pnl_today_inr for p in acct.positions)


@pytest.mark.parametrize("exit_price, expected", [(1010.0, 100.0), (985.0, -150.0)])   # 10 shares: a gain, a loss
def test_a_close_recorded_while_the_book_is_read_is_counted_exactly_once(exit_price, expected):
    s = settings()
    j, _ = new_journal(s)
    j.ensure_equity_start(CASH_INDEX, NOW - timedelta(days=1), s.account.capital_inr)
    sig = replace(cash_signal(entry=1000.0, stop=980.0, target=1050.0), signal_id="close-race")
    d = decide(sig, pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())[0], GOOD_MARKET, s, ZeroCosts(),
               low_corr, SECTORS)
    assert d.status == "APPROVED", d.reasons
    j.save_signal(sig, d, NOW, paper=PaperTrader.pending_row(sig, d))
    j.record_decision("close-race", {"decision": "TOOK", "fill_price": 1000.0, "quantity": 10}, NOW)
    pid = j.held_positions(CASH_INDEX)[0].position_id
    marks = FakeMarks({"RELIANCE": exit_price})
    before, _ = pool_account(j, s, CASH_INDEX, NOW, marks, ZeroCosts())
    assert _today_total(before) == pytest.approx(expected)              # open, not closed yet
    proxy = _ClosedMidRead(j, pid, exit_price)
    j._db = proxy
    during, facts = pool_account(j, s, CASH_INDEX, NOW, marks, ZeroCosts())
    proxy.thread.join(10)
    j._db = proxy.real
    after, _ = pool_account(j, s, CASH_INDEX, NOW, marks, ZeroCosts())
    assert _today_total(during) == pytest.approx(expected)              # once: still open, not yet realised
    assert facts["realised_today_inr"] == 0.0 and len(during.positions) == 1
    assert _today_total(after) == pytest.approx(expected)               # once: realised, no longer open
    assert after.positions == () and after.realised_today_inr == pytest.approx(expected)
    assert after.realised_month_inr == pytest.approx(expected)


def test_the_account_snapshot_closes_its_read_transaction_and_agrees_with_the_readers():
    s = settings()
    j, _ = new_journal(s)
    j.ensure_equity_start(CASH_INDEX, NOW - timedelta(days=1), s.account.capital_inr)
    snap = j.account_snapshot(CASH_INDEX, NOW)
    assert not j._db.in_transaction
    assert snap.equity == j.equity_rows(CASH_INDEX)
    assert snap.realised() == j.realised(CASH_INDEX) and snap.realised(since=NOW.date()) == j.realised(CASH_INDEX, NOW.date())
