"""Phase 6 M1, step 15 (issue 95): two capital pools. The journal records realised and open P&L and the
equity history per pool, and the risk engine receives the account state of the signal's own pool, so a loss in
one pool never stops or halves the other. Also: what is not known makes only its own pool unknown."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.journal import CASH_INDEX, STOCK_OPTIONS, pool_account, record_marks
from app.risk.engine import decide
from tests.phase5_fixtures import GOOD_MARKET, SECTORS, ZeroCosts, bull_put, cash_signal, no_corr
from tests.phase6_fixtures import NOW, FakeMarks, approved, new_journal, settings

STOCK_SPREAD = dict(symbol="RELIANCE", asset="STOCK_OPTION", strategy="stock_option_selling", lot_size=500,
                    sold=(1300, 12.0), bought=(1260, 4.0), margin_per_lot=90000.0)


def journal_with_start(s):
    j, store = new_journal(s)
    for pool, cap in ((CASH_INDEX, s.account.capital_inr), (STOCK_OPTIONS, s.stock_option_selling.backtest.capital_inr)):
        j.ensure_equity_start(pool, NOW, cap)
    return j


def stock_short_put(j, price=10.0, lots=1):
    return j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-16", "margin_inr": 90000,
                                  "legs": [{"action": "SELL", "right": "PE", "strike": 1300, "expiry": "2026-11-24",
                                            "lots": lots, "lot_size": 500, "price": 12.0},
                                           {"action": "BUY", "right": "PE", "strike": 1260, "expiry": "2026-11-24",
                                            "lots": lots, "lot_size": 500, "price": 4.0}]}, NOW)


def decide_for(sig, s, j, marks):
    pool = STOCK_OPTIONS if sig.asset_class == "STOCK_OPTION" else CASH_INDEX
    acct, facts = pool_account(j, s, pool, NOW, marks, ZeroCosts())
    if pool == STOCK_OPTIONS:      # phase 9 portfolio limits also need the trade history verified (live: phase 9 decides)
        acct = replace(acct, history_verified=True)
    return decide(sig, acct, GOOD_MARKET, s, ZeroCosts(), no_corr, SECTORS, universe=["RELIANCE"]), facts


def test_a_stock_option_loss_stops_only_the_stock_pool():
    s = settings()                                   # cash pool Rs 2,00,000; stock pool Rs 10,00,000 (daily stop 20,000)
    j = journal_with_start(s)
    pos = stock_short_put(j)
    # closed today at a loss: sold leg bought back at 52, bought leg sold at 4 -> (12 - 52) x 500 = -20,000
    j.close_position(pos.position_id, {"exit_prices": [52.0, 4.0]}, NOW, ZeroCosts())
    assert j.realised(STOCK_OPTIONS, since=date(2026, 11, 16)) == Decimal("-20000.00")
    assert j.realised(CASH_INDEX) == Decimal("0.00")
    marks = FakeMarks()
    d_stock, _ = decide_for(bull_put(**STOCK_SPREAD), s, j, marks)
    assert "DAILY_STOP" in d_stock.reason_codes                         # 20,000 = 2% of Rs 10,00,000
    d_cash, f = decide_for(cash_signal(entry=100.0, stop=95.0, target=110.0), s, j, marks)
    assert d_cash.status == "APPROVED" and d_cash.size_factor == 1.0, d_cash.reasons
    assert f["realised_today_inr"] == 0.0


def test_a_cash_loss_stops_only_the_cash_pool():
    s = settings()
    j = journal_with_start(s)
    p = j.add_manual_position({"symbol": "INFY", "trade_type": "INTRADAY_STOCKS", "direction": "LONG", "quantity": 100,
                               "entry_price": 1500.0, "entry_date": "2026-11-16", "stop": 1400.0}, NOW)
    j.close_position(p.position_id, {"exit_price": 1460.0}, NOW, ZeroCosts())         # -4,000 = 2% of Rs 2,00,000
    marks = FakeMarks()
    assert "DAILY_STOP" in decide_for(cash_signal(), s, j, marks)[0].reason_codes
    d = decide_for(bull_put(**STOCK_SPREAD), s, j, marks)[0]
    assert "DAILY_STOP" not in d.reason_codes and d.status == "APPROVED", d.reasons


def test_drawdown_halves_only_its_own_pool():
    s = settings()
    j = journal_with_start(s)
    # cash pool: its peak equity was Rs 2,22,300 at an October close; today Rs 2,00,000 = 10.03% below the peak,
    # with no loss this month (so the monthly pause does not apply)
    j.write_equity(CASH_INDEX, date(2026, 10, 30), 222300.0, 22300.0, 0.0, NOW)
    marks = FakeMarks()
    d_cash = decide_for(cash_signal(entry=100.0, stop=95.0, target=110.0), s, j, marks)[0]
    assert d_cash.status == "APPROVED" and d_cash.size_factor == 0.5, d_cash.reasons
    assert any(w.startswith("SIZE_HALVED") for w in d_cash.warnings)
    d_stock = decide_for(bull_put(**STOCK_SPREAD), s, j, marks)[0]
    assert d_stock.size_factor == 1.0 and not any(w.startswith("SIZE_HALVED") for w in d_stock.warnings)


def test_unknown_price_makes_only_that_pool_unknown():
    s = settings()
    j = journal_with_start(s)
    stock_short_put(j)                                          # open, no quote for its legs
    marks = FakeMarks()
    acct, f = pool_account(j, s, STOCK_OPTIONS, NOW, marks, ZeroCosts())
    assert acct.positions[0].open_pnl_today_inr is None and any("open P&L not known" in u for u in f["unknown"])
    assert "PORTFOLIO_UNKNOWN" in decide_for(bull_put(**STOCK_SPREAD), s, j, marks)[0].reason_codes
    assert decide_for(cash_signal(), s, j, marks)[0].status == "APPROVED"


def test_open_pnl_today_is_measured_from_the_previous_days_mark():
    s = settings()
    j = journal_with_start(s)
    p = j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 10,
                               "entry_price": 1000.0, "entry_date": "2026-11-12", "stop": 900.0}, NOW)
    marks = FakeMarks({"INFY": 1050.0})
    yesterday = NOW - timedelta(days=3)                          # Friday's session
    acct, f = pool_account(j, s, CASH_INDEX, NOW, marks, ZeroCosts())
    assert acct.positions[0].open_pnl_today_inr is None and "no mark recorded" in f["unknown"][0]
    record_marks(j, CASH_INDEX, yesterday, FakeMarks({"INFY": 1080.0}), ZeroCosts())     # +800 at Friday's mark
    acct, f = pool_account(j, s, CASH_INDEX, NOW, marks, ZeroCosts())
    op = acct.positions[0]
    assert (op.open_pnl_today_inr, op.open_pnl_month_inr) == (-300.0, 500.0)     # 500 - 800; since entry (this month)
    assert op.risk_inr == 1000.0 and op.committed_inr == 10000.0 and f["unknown"] == []
    assert acct.equity_curve[-1] == (date(2026, 11, 16), 200500.0)


def test_paper_trades_are_never_in_my_real_book_and_a_taken_signal_is_my_position():
    s = settings()
    j = journal_with_start(s)
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    j.save_signal(sig, approved(quantity=10), NOW)
    row = {"signal_id": sig.signal_id, "trade_type": "INTRADAY_STOCKS", "pool": CASH_INDEX, "symbol": "RELIANCE",
           "direction": "LONG", "legs_json": None, "quantity": 10, "status": "OPEN", "entry_utc": NOW.isoformat(),
           "entry_price": 100.0, "stop": 95.0, "target": 110.0, "horizon": "INTRADAY", "strategy_id": "orb_v2"}
    j.upsert_paper(row)
    assert j.held_positions(CASH_INDEX) == []                     # the paper trade runs in the paper book
    j.record_decision(sig.signal_id, {"decision": "TOOK", "fill_price": 100.2}, NOW)
    held = j.held_positions(CASH_INDEX)
    assert len(held) == 1 and held[0].origin == "SIGNAL"          # my position


def test_deposits_belong_to_the_cash_pool_only():
    s = settings()
    j = journal_with_start(s)
    flows = ((date(2026, 11, 14), 50000.0),)
    a_cash, _ = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts(), flows)
    a_stock, _ = pool_account(j, s, STOCK_OPTIONS, NOW, FakeMarks(), ZeroCosts(), flows)
    assert a_cash.flows == flows and a_stock.flows == ()
    assert a_stock.capital_inr == 1000000 and a_cash.capital_inr == 200000


def test_close_equity_day_uses_the_sessions_last_mark():
    from app.journal import close_equity_day
    s = settings()
    j = journal_with_start(s)
    j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 10,
                           "entry_price": 1000.0, "entry_date": "2026-11-16", "stop": 900.0}, NOW)
    after_close = NOW + timedelta(hours=6)
    assert close_equity_day(j, s, CASH_INDEX, after_close) is None           # no mark today: nothing written
    record_marks(j, CASH_INDEX, NOW + timedelta(hours=4), FakeMarks({"INFY": 990.0}), ZeroCosts())
    out = close_equity_day(j, s, CASH_INDEX, after_close)
    assert out["equity_inr"] == 199900.0
    assert j.equity_rows(CASH_INDEX)[-1] == (date(2026, 11, 16), 199900.0)
