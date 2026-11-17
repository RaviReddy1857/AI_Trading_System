"""Two books: my REAL book (my positions and the approved signals awaiting my decision) decides every signal card;
paper trades run in their own simulated PAPER book with its own capital and limits (config `paper:`).

The GRASIM case of 2026-11-17: no open positions, three open paper trades (SBIN x2, ADANIENT, Rs 1,48,144.05 at entry
prices) made the GRASIM card NO_TRADE with RISK_BUDGET_USED (Rs 1,97,768.05 committed). Paper trades no longer count
against my real capital, and the card shows what the commitment is made of."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from app.cards import build_card
from app.journal import CASH_INDEX, paper_book, pool_account
from app.paper import PaperTrader
from app.risk.engine import decide
from tests.phase5_fixtures import GOOD_MARKET, SECTORS, ZeroCosts, cash_signal, no_corr
from tests.phase6_fixtures import NOW, FakeMarks, approved, new_journal, settings

PAPER_OPEN = [("sbin-1", "SBIN", 45, 1105.75), ("sbin-2", "SBIN", 45, 1106.50), ("adani-1", "ADANIENT", 18, 2699.60)]


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
    assert paper_book(j, s, CASH_INDEX, NOW)["committed_inr"] == 148144.05


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


# ---------------- the paper book ----------------
def _trader(s, j):
    return PaperTrader(s, j, None, None, None, None, None)


def test_paper_book_has_its_own_capital_limit_and_refuses_with_not_opened():
    s, j = _journal()
    p = _trader(s, j)
    sigs = [replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id=f"pp-{k}") for k in range(4)]
    for sig in sigs:
        p.open(sig, approved(quantity=49, margin=49000.0), NOW)
    st = [j.paper_trade(x.signal_id)["status"] for x in sigs]
    assert st == ["PENDING", "PENDING", "PENDING", "NOT_OPENED"]          # 4 x 49,000 > 1,50,000 usable
    why = j.paper_trade("pp-3")["exit_reason"]
    assert why.startswith("PAPER_BOOK_LIMIT: paper capital committed would be Rs 196,000.00")
    book = paper_book(j, s, CASH_INDEX, NOW)
    assert book["book"] == "PAPER" and book["committed_inr"] == 147000.0 and book["open_or_pending"] == 3
    # my real book is untouched by the paper book
    assert pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())[0].positions == ()


def test_paper_book_capital_pct_and_max_open_trades_come_from_config():
    base = settings()
    s = base.model_copy(update={"paper": base.paper.model_copy(update={"capital_pct": 50.0, "max_open_trades": 2})})
    _, j = _journal(s)
    p = _trader(s, j)
    small = [replace(cash_signal(entry=100.0, stop=99.0, target=103.0), signal_id=f"sm-{k}") for k in range(3)]
    for sig in small:
        p.open(sig, approved(quantity=10, margin=1000.0), NOW)
    assert [j.paper_trade(x.signal_id)["status"] for x in small] == ["PENDING", "PENDING", "NOT_OPENED"]
    assert "above paper.max_open_trades 2" in j.paper_trade("sm-2")["exit_reason"]
    assert paper_book(j, s, CASH_INDEX, NOW)["capital_inr"] == 100000.0   # 50% of 2,00,000


def test_expired_pending_paper_trades_free_the_paper_book():
    s, j = _journal()
    p = _trader(s, j)
    sigs = [replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id=f"ex-{k}") for k in range(3)]
    for sig in sigs:
        p.open(sig, approved(quantity=49, margin=49000.0), NOW)
    later = sigs[0].valid_until + timedelta(seconds=1)
    late = replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id="ex-late")
    p.open(late, approved(quantity=49, margin=49000.0), later)
    assert j.paper_trade("ex-late")["status"] == "PENDING"


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
        assert paper["book"][CASH_INDEX]["committed_inr"] >= 148144.05 and paper["book"][CASH_INDEX]["book"] == "PAPER"
        assert "commitHtml(c.commitment)" in c.get("/signals").text
