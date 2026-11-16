"""Phase 6 M2, step 4: the paper trader. Fills at the next bar's first LIVE quote (never on the signal bar), tracked
to stop, target or time exit (stop first when one bar touches both), with costs to the paise; a stale quote never
moves a paper trade; no fresh quote before valid-until = NOT_FILLED; a fill beyond the stop or target is not made."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.calendar import MarketCalendar
from app.execution import ExecutionBook
from app.models import Bar
from app.paper import PaperTrader
from app.pricing import LIVE, STALE, LegQuote
from tests.phase6_fixtures import FlatCosts, approved, bull_put, cash_signal, new_journal, no_trade, settings

T0 = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)              # 10:30 IST: the signal bar's close


class Costs(FlatCosts):
    """Flat Rs 20 per equity round trip, Rs 40 a lot per option leg round trip, no slippage."""

    def fill_price(self, segment, price, side):
        return Decimal(str(price))


class FakeMarker:
    def __init__(self):
        self.cash_q, self.opt_q = {}, {}

    def cash(self, symbol, now):
        return self.cash_q.get(symbol) or LegQuote("MISSING", None, None, None, None, "feed tick")

    def option(self, u, expiry, strike, right, now):
        return self.opt_q.get((strike, right)) or LegQuote("MISSING", None, None, None, None, "option chain")

    def spot(self, symbol, now):
        q = self.cash_q.get(symbol)
        return q.last if q and q.usable else None

    def marks(self, pos, now):
        if not pos.is_option:
            q = self.cash(pos.symbol, now)
            return ([q.bid], "") if q.usable else (None, "stale")
        out = []
        for x in pos.legs:
            q = self.option(pos.symbol, x.expiry, x.strike, x.right, now)
            if not q.usable:
                return None, "stale"
            out.append(q.bid if x.action == "BUY" else q.ask)
        return out, ""


def trader():
    s = settings()
    j, store = new_journal(s)
    m = FakeMarker()
    xb = ExecutionBook(store, s)
    return PaperTrader(s, j, m, store, xb, MarketCalendar(), Costs()), j, m, store, xb


def q(bid, ask, last, at, status=LIVE):
    return LegQuote(status, bid, ask, last, at, "feed tick")


def test_fill_waits_for_the_next_bars_first_quote():
    p, j, m, _, xb = trader()
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), T0)
    m.cash_q["RELIANCE"] = q(99.9, 100.1, 100.0, T0 - timedelta(seconds=2))     # last quote of the signal bar
    p.fill_pending(T0 + timedelta(seconds=1), {sig.signal_id: sig})
    assert j.paper_trade(sig.signal_id)["status"] == "PENDING"
    m.cash_q["RELIANCE"] = q(100.3, 100.5, 100.4, T0 + timedelta(seconds=3))    # first quote of the next bar
    p.fill_pending(T0 + timedelta(seconds=4), {sig.signal_id: sig})
    row = j.paper_trade(sig.signal_id)
    assert row["status"] == "OPEN" and row["entry_price"] == 100.5 and "ask" in row["entry_source"]
    moments = {r["moment"]: r for r in xb.rows(sig.signal_id)}
    assert moments["PLANNED_FILL"]["bid"] == 100.3 and moments["PAPER_FILL"]["price"] == 100.5


def test_not_filled_without_a_fresh_quote_before_valid_until():
    p, j, m, _, xb = trader()
    sig = cash_signal()
    p.open(sig, approved(quantity=10), T0)
    m.cash_q["RELIANCE"] = q(99.9, 100.1, 100.0, T0 + timedelta(seconds=3), status=STALE)
    p.fill_pending(sig.valid_until, {sig.signal_id: sig})
    row = j.paper_trade(sig.signal_id)
    assert row["status"] == "NOT_FILLED" and "no LIVE quote" in row["exit_reason"]
    pf = [r for r in xb.rows(sig.signal_id) if r["moment"] == "PLANNED_FILL"][0]
    assert pf["status"] == "STALE" and pf["bid"] == 99.9                        # recorded as it was, never zero


def test_fill_beyond_the_stop_is_not_made():
    p, j, m, _, _ = trader()
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), T0)
    m.cash_q["RELIANCE"] = q(94.8, 95.0, 94.9, T0 + timedelta(seconds=3))
    p.fill_pending(T0 + timedelta(seconds=4), {sig.signal_id: sig})
    assert j.paper_trade(sig.signal_id)["exit_reason"].startswith("FILL_BEYOND_STOP_OR_TARGET")


def test_no_trade_is_never_paper_traded():
    p, j, _, _, _ = trader()
    assert p.open(cash_signal(), no_trade("DAILY_STOP"), T0) is None and j.paper_trades() == []


def _open_cash(p, j, m, store, entry_ask=100.5):
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), T0)
    m.cash_q["RELIANCE"] = q(entry_ask - 0.2, entry_ask, entry_ask - 0.1, T0 + timedelta(seconds=3))
    p.fill_pending(T0 + timedelta(seconds=4), {sig.signal_id: sig})
    return sig


def test_stop_first_when_one_bar_touches_both_and_pnl_to_the_paise():
    p, j, m, store, _ = trader()
    sig = _open_cash(p, j, m, store)
    bar_start = T0 + timedelta(minutes=5)
    for k, (o, h, l, c) in enumerate([(100.6, 111.0, 100.0, 105.0), (105.0, 106.0, 94.0, 96.0)]):
        store.save_bar(Bar("RELIANCE", bar_start + timedelta(minutes=k), o, h, l, c, 100, 5), "sim")
    out = p.manage(bar_start + timedelta(minutes=5), bar_start)
    row = j.paper_trade(sig.signal_id)
    assert out[0]["reason"] == "STOP" and row["exit_price"] == 95.0
    # 10 x (95 - 100.5) = -55.00; Rs 20 charges -> -75.00
    assert (row["gross_inr"], row["costs_inr"], row["net_pnl_inr"]) == (-55.0, 20.0, -75.0)


def test_target_and_time_exit():
    p, j, m, store, _ = trader()
    sig = _open_cash(p, j, m, store)
    t = T0 + timedelta(minutes=5)
    store.save_bar(Bar("RELIANCE", t, 101.0, 103.0, 100.8, 102.0, 100, 5), "sim")
    assert p.manage(t + timedelta(minutes=5), t) == []
    exit_bar = datetime(2026, 11, 16, 9, 40, tzinfo=timezone.utc)              # 15:10-15:15 IST: the bar ending 15:15
    store.save_bar(Bar("RELIANCE", exit_bar, 102.0, 103.0, 101.0, 102.5, 100, 5), "sim")
    out = p.manage(exit_bar + timedelta(minutes=5), exit_bar)
    row = j.paper_trade(sig.signal_id)
    assert out[0]["reason"] == "TIME" and row["exit_price"] == 102.5 and row["net_pnl_inr"] == 0.0   # 20 - 20


def test_option_spread_fills_at_the_touch_and_stops_on_the_buy_back_cost():
    p, j, m, _, xb = trader()
    sig = bull_put(sold=(24000, 60.0), bought=(23900, 30.0))           # credit 30, stop 60, target 15
    p.open(sig, approved(quantity=75, lots=1, margin=40000.0), T0)
    after = T0 + timedelta(seconds=30)
    m.opt_q[(24000, "PE")] = LegQuote(LIVE, 59.0, 61.0, 60.0, after, "option chain")
    m.opt_q[(23900, "PE")] = LegQuote(LIVE, 29.0, 31.0, 30.0, after, "option chain")
    p.fill_pending(after + timedelta(seconds=1), {sig.signal_id: sig})
    row = j.paper_trade(sig.signal_id)
    assert row["status"] == "OPEN" and row["entry_price"] == 28.0          # sell 24000 at 59 bid, buy 23900 at 31 ask
    # a stale quote never moves it
    m.opt_q[(24000, "PE")] = LegQuote(STALE, 90.0, 95.0, 92.0, after, "option chain")
    assert p.manage(after + timedelta(minutes=5)) == []
    # live: buy back the sold leg at 70 (ask), sell the bought leg at 8 (bid): cost 62 >= stop 60
    m.opt_q[(24000, "PE")] = LegQuote(LIVE, 68.0, 70.0, 69.0, after, "option chain")
    m.opt_q[(23900, "PE")] = LegQuote(LIVE, 8.0, 9.0, 8.5, after, "option chain")
    out = p.manage(after + timedelta(minutes=10))
    row = j.paper_trade(sig.signal_id)
    # sold leg (59 - 70) x 75 = -825; bought leg (8 - 31) x 75 = -1,725; charges Rs 40 + 40
    assert out[0]["reason"] == "STOP" and (row["gross_inr"], row["costs_inr"], row["net_pnl_inr"]) == (-2550.0, 80.0, -2630.0)
