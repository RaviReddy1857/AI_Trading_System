"""Two books: my REAL book (my positions and the approved signals awaiting my decision) decides every signal card;
paper trades run in their own simulated PAPER book with its own capital and limits (config `paper:`).

The GRASIM case of 2026-11-17: no open positions, three open paper trades (SBIN x2, ADANIENT, Rs 1,48,144.05 at entry
prices) made the GRASIM card NO_TRADE with RISK_BUDGET_USED (Rs 1,97,768.05 committed). Paper trades no longer count
against my real capital, and the card shows what the commitment is made of."""
from __future__ import annotations

import pytest

from dataclasses import replace
from datetime import timedelta

from app.cards import build_card
from app.journal import CASH_INDEX, paper_book, pool_account
from app.paper import PaperTrader
from app.risk.engine import decide
from tests.phase5_fixtures import GOOD_MARKET, SECTORS, ZeroCosts, cash_signal, no_corr
from tests.phase6_fixtures import NOW, FakeMarks, approved, new_journal, settings

PAPER_OPEN = [("sbin-1", "SBIN", 45, 1105.75), ("sbin-2", "SBIN", 45, 1106.50), ("adani-1", "ADANIENT", 18, 2699.60)]


pytestmark = pytest.mark.usefixtures("fixed_clock")      # the app reads the clock: the fixtures' day


def _journal(s=None):
    s = s or settings()
    j, store = new_journal(s)
    j.ensure_equity_start(CASH_INDEX, NOW - timedelta(days=1), s.account.capital_inr)
    return s, j


def _open_paper(j, sid, symbol, qty, price, status="OPEN", pnl=None, margin=None):
    row = {"signal_id": sid, "trade_type": "INTRADAY_STOCKS", "pool": CASH_INDEX, "symbol": symbol,
           "direction": "LONG", "legs_json": None, "quantity": qty, "status": status, "entry_utc": NOW.isoformat(),
           "entry_price": price, "stop": price * 0.98, "target": price * 1.04, "horizon": "INTRADAY",
           "strategy_id": "orb_v2", "margin_inr": margin if margin is not None else qty * price, "risk_inr": 700.0,
           "note": '{"valid_until_utc": "%s"}' % (NOW + timedelta(minutes=15)).isoformat()}
    if pnl is not None:
        row.update(exit_utc=NOW.isoformat(), net_pnl_inr=pnl, exit_reason="STOP")
    j.upsert_paper(row)


def _decide(s, j, sig, marks=None):
    acct, _ = pool_account(j, s, CASH_INDEX, NOW, marks or FakeMarks(), ZeroCosts())
    return decide(sig, acct, GOOD_MARKET, s, ZeroCosts(), no_corr, SECTORS)


def _card(sig, d):
    return build_card(sig, d, score=None, regime="TRENDING_UP", bias="BULLISH", events_week=[], track=None,
                      alert=None, now=NOW)


# ---------------- the real book ----------------
def test_open_paper_trades_do_not_use_my_real_capital_grasim_case():
    s, j = _journal()
    for sid, sym, q, px in PAPER_OPEN:
        _open_paper(j, sid, sym, q, px)
    sig = cash_signal(entry=1000.0, stop=990.0, target=1030.0)          # cash value about Rs 50,000
    d = _decide(s, j, sig)
    assert d.status == "APPROVED", d.reasons
    m = _card(sig, d)["commitment"]
    assert m["committed_inr"] == 0.0 and m["items"] == [] and m["book"].startswith("REAL")
    # the paper book holds them: Rs 1,48,144.05 at entry prices
    assert paper_book(j, s, CASH_INDEX, NOW, strategy_id="orb_v2")["committed_inr"] == 148144.05


def test_my_real_positions_still_count_and_the_card_lists_them():
    s, j = _journal()
    j.add_manual_position({"symbol": "INFY", "trade_type": "INTRADAY_STOCKS", "direction": "LONG", "quantity": 120,
                           "entry_price": 1000.0, "entry_date": "2026-11-16", "stop": 900.0}, NOW)
    sig = cash_signal(entry=1000.0, stop=990.0, target=1030.0)
    d = _decide(s, j, sig, FakeMarks({"INFY": 1000.0}))
    assert d.status == "NO_TRADE" and "RISK_BUDGET_USED" in d.reason_codes, d.reasons
    m = _card(sig, d)["commitment"]
    assert m["committed_inr"] == 120000.0 and m["after_inr"] > m["usable_inr"] == 150000.0
    assert m["items"] == [{"what": "your position (entered by hand)", "symbol": "INFY", "id": m["items"][0]["id"],
                           "committed_inr": 120000.0}]


def test_approved_signals_awaiting_my_decision_reserve_until_taken_skipped_or_expired_not_until_paper_fill():
    s, j = _journal()
    first = replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id="wait-1")
    d1 = _decide(s, j, first)
    j.save_signal(first, d1, NOW, paper=PaperTrader.pending_row(first, d1))
    acct, f = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())
    assert f["pending_reserved"] == 1 and acct.positions[0].origin == "PENDING"
    j.upsert_paper({**j.paper_trade("wait-1"), "status": "OPEN", "entry_utc": NOW.isoformat(), "entry_price": 1000.2})
    assert len(pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())[0].positions) == 1   # paper fill: still mine to decide
    second = replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id="wait-2")
    m = _card(second, _decide(s, j, second))["commitment"]
    assert m["items"][0]["what"] == "approved signal awaiting your decision" and m["items"][0]["id"] == "pending:wait-1"
    j.record_decision("wait-1", {"decision": "SKIPPED"}, NOW)
    assert pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())[0].positions == ()
    later = first.valid_until + timedelta(seconds=1)
    third = replace(first, signal_id="wait-3")
    j.save_signal(third, d1, NOW)
    assert pool_account(j, s, CASH_INDEX, later, FakeMarks(), ZeroCosts())[0].positions == ()


def test_paper_losses_do_not_trigger_my_daily_stop():
    s, j = _journal()                                                 # daily stop 2% of 2,00,000 = 4,000
    _open_paper(j, "lost-1", "SBIN", 45, 1105.75, status="CLOSED", pnl=-5000.0)
    acct, f = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())
    assert f["realised_today_inr"] == 0.0
    d = _decide(s, j, cash_signal(entry=1000.0, stop=990.0, target=1030.0))
    assert d.status == "APPROVED" and "DAILY_STOP" not in d.reason_codes


# ---------------- the paper books: one per strategy ----------------
def _trader(s, j):
    return PaperTrader(s, j, None, None, None, None, None)


def _sig(sid, strategy="orb_v2", entry=1000.0):
    return replace(cash_signal(entry=entry, stop=entry * 0.99, target=entry * 1.03), signal_id=sid, strategy_id=strategy)


def test_a_strategy_paper_trades_every_approved_signal_at_the_real_books_size_beyond_the_pool_capital():
    s, j = _journal()
    p = _trader(s, j)
    sigs = [_sig(f"pp-{k}") for k in range(6)]
    for sig in sigs:
        p.open(sig, approved(quantity=49, margin=49000.0), NOW)
    rows = [j.paper_trade(x.signal_id) for x in sigs]
    assert [r["status"] for r in rows] == ["PENDING"] * 6                  # 6 x 49,000 = 2,94,000: all kept
    assert all(r["quantity"] == 49 for r in rows)                          # the real book's approved size
    book = paper_book(j, s, CASH_INDEX, NOW, strategy_id="orb_v2")
    assert (book["capital_inr"], book["committed_inr"], book["open_or_pending"]) == (200000.0, 294000.0, 6)
    assert book["over_capital"] is True and book["fits"] is True           # reported, never a refusal
    assert pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())[0].positions == ()   # my real book: empty


def test_one_strategys_paper_trades_never_stop_another_and_the_cap_is_per_strategy():
    s, j = _journal()
    p = _trader(s, j)
    for k in range(10):
        p.open(_sig(f"a-{k}", "orb_v2"), approved(quantity=10, margin=10000.0), NOW)
    p.open(_sig("a-10", "orb_v2"), approved(quantity=10, margin=10000.0), NOW)          # the 11th of orb_v2
    for k in range(3):
        p.open(_sig(f"b-{k}", "vwap_pullback_v2"), approved(quantity=10, margin=10000.0), NOW)
    assert j.paper_trade("a-10")["status"] == "NOT_OPENED"
    assert "11 paper trades of orb_v2 open or pending with this one, above paper.max_open_trades 10" in \
        j.paper_trade("a-10")["exit_reason"]
    assert [j.paper_trade(f"b-{k}")["status"] for k in range(3)] == ["PENDING"] * 3
    assert paper_book(j, s, CASH_INDEX, NOW, strategy_id="vwap_pullback_v2")["open_or_pending"] == 3
    assert s.paper.max_open_trades == 10


def test_max_open_trades_comes_from_config():
    base = settings()
    s = base.model_copy(update={"paper": base.paper.model_copy(update={"max_open_trades": 2})})
    _, j = _journal(s)
    p = _trader(s, j)
    for k in range(3):
        p.open(_sig(f"sm-{k}", entry=100.0), approved(quantity=10, margin=1000.0), NOW)
    assert [j.paper_trade(f"sm-{k}")["status"] for k in range(3)] == ["PENDING", "PENDING", "NOT_OPENED"]


def test_expired_pending_and_closed_paper_trades_free_the_strategys_cap():
    base = settings()
    s = base.model_copy(update={"paper": base.paper.model_copy(update={"max_open_trades": 2})})
    _, j = _journal(s)
    p = _trader(s, j)
    first = [_sig(f"ex-{k}") for k in range(2)]
    for sig in first:
        p.open(sig, approved(quantity=10, margin=10000.0), NOW)
    later = first[0].valid_until + timedelta(seconds=1)
    p.open(_sig("ex-late"), approved(quantity=10, margin=10000.0), later)
    assert j.paper_trade("ex-late")["status"] == "PENDING"


def test_every_approved_signal_reaches_the_strategys_paper_track_record():
    from app.track import paper_record
    s, j = _journal()
    p = _trader(s, j)
    for k in range(5):                                                  # other strategies fill their books
        p.open(_sig(f"o-{k}", "vwap_pullback_v2"), approved(quantity=49, margin=49000.0), NOW)
    for k in range(4):
        p.open(_sig(f"r-{k}", "orb_v2"), approved(quantity=49, margin=49000.0), NOW)
    assert [j.paper_trade(f"r-{k}")["status"] for k in range(4)] == ["PENDING"] * 4    # none refused
    for k in range(4):                                                  # each closes at a loss of Rs 100
        j.upsert_paper({**j.paper_trade(f"r-{k}"), "status": "CLOSED", "entry_utc": NOW.isoformat(),
                        "entry_price": 1000.0, "exit_utc": NOW.isoformat(), "net_pnl_inr": -100.0})
    assert paper_record(j, ("orb_v2",), 30)["trades"] == 4


def test_engine_card_is_approved_with_open_paper_trades_and_api_shows_both_books(tmp_path):
    from tests.test_phase6_engine import BAR_END, make, seed, tick
    c, eng = make(str(tmp_path / "books.db"))
    with c:
        for sid, sym, q, px in PAPER_OPEN:
            _open_paper(eng.j, sid, sym, q, px)
        last = seed(c.app)
        tick(c.app, BAR_END - timedelta(seconds=1), last)
        out = eng.on_bar(BAR_END, BAR_END + timedelta(seconds=3))
        assert out["approved"] == 1, out["stored"][0]["card"]["reasons"]
        card = c.get("/api/signals").json()["cards"][0]
        assert card["commitment"]["committed_inr"] == 0.0 and card["commitment"]["items"] == []
        paper = c.get("/api/paper").json()
        book = paper["books"][CASH_INDEX]["orb_v2"]
        assert book["committed_inr"] >= 148144.05 and book["book"] == "PAPER" and book["strategy_id"] == "orb_v2"
        assert "commitHtml(c.commitment)" in c.get("/signals").text
