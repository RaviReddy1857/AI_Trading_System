"""Phase 6 M2, step 13 acceptance: execution records per leg at signal time and at the planned fill (bid, ask,
depth, quote time, status), the paper fill and my actual fill; a missing or stale quote stored as such (never
zero); the slippage report groups by stock and strike-distance band and reconciles to the stored records."""
from datetime import datetime, timedelta, timezone

import pytest

from app.execution import ACTUAL_FILL, PAPER_FILL, PLANNED_FILL, SIGNAL, ExecutionBook, band_of, legs_of
from app.pricing import LIVE, MISSING, STALE, LegQuote
from tests.phase6_fixtures import bull_put, cash_signal, settings
from app.store import Store

T = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)


def book():
    return ExecutionBook(Store(":memory:"), settings())


def lq(bid, ask, last, status=LIVE, depth=None, src="feed tick"):
    return LegQuote(status, bid, ask, last, T, src, depth=depth)


def test_every_leg_recorded_at_signal_and_planned_fill_with_status():
    xb = book()
    sig = bull_put(sold=(24000, 60.0), bought=(23900, 30.0))
    xb.record_quotes(sig, SIGNAL, [lq(59, 61, 60, src="option chain"), lq(None, None, None, MISSING, src="option chain")],
                     T, 1, 24200.0)
    xb.record_quotes(sig, PLANNED_FILL, [lq(58, 60, 59, STALE), lq(29, 31, 30)], T + timedelta(seconds=30), 1, 24210.0)
    rows = {(r["moment"], r["leg"]): r for r in xb.rows(sig.signal_id)}
    assert rows[(SIGNAL, 1)]["status"] == MISSING and rows[(SIGNAL, 1)]["bid"] is None   # never zero
    assert rows[(PLANNED_FILL, 0)]["status"] == STALE and rows[(PLANNED_FILL, 0)]["bid"] == 58
    assert rows[(SIGNAL, 0)]["side"] == "SELL" and rows[(SIGNAL, 0)]["strike"] == 24000 and rows[(SIGNAL, 0)]["spot"] == 24200.0
    # a moment of a leg is recorded once (a restart never overwrites it)
    xb.record_quotes(sig, SIGNAL, [lq(1, 2, 1.5), lq(1, 2, 1.5)], T, 1, 1.0)
    assert {(r["moment"], r["leg"]): r for r in xb.rows(sig.signal_id)}[(SIGNAL, 0)]["bid"] == 59


def test_depth_is_stored_when_sent():
    xb = book()
    sig = cash_signal()
    depth = {"bids": [{"price": 99.9, "quantity": 500}], "asks": [{"price": 100.1, "quantity": 300}]}
    xb.record_quotes(sig, PLANNED_FILL, [lq(99.9, 100.1, 100.0, depth=depth)], T, 10, 100.0)
    assert xb.rows(sig.signal_id)[0]["depth"] == depth
    assert xb.leg_rows()[0]["depth_at_touch"] == 300.0                    # a purchase meets the asks


def test_slippage_report_reconciles_by_hand():
    xb = book()
    cash = cash_signal(entry=100.0, stop=95.0, target=110.0)               # buy 10 shares
    xb.record_quotes(cash, PLANNED_FILL, [lq(100.0, 100.4, 100.2)], T, 10, 100.2)
    xb.record_fill(legs_of(cash), cash.signal_id, PAPER_FILL, [100.4], T, 10, "ask")
    xb.record_fill(legs_of(cash), cash.signal_id, ACTUAL_FILL, [100.5], T, 10, "my fill")
    spread = bull_put(sold=(24000, 60.0), bought=(23900, 30.0))
    xb.record_quotes(spread, PLANNED_FILL, [lq(59, 61, 60, src="option chain"), lq(None, None, None, MISSING)], T, 75, 24200.0)
    xb.record_fill(legs_of(spread), spread.signal_id, PAPER_FILL, [59.0, 31.0], T, 75, "touch")
    rows = {(r["signal_id"], r["leg"]): r for r in xb.leg_rows()}
    c = rows[(cash.signal_id, 0)]
    assert (c["mid"], c["touch"]) == (pytest.approx(100.2), 100.4)
    assert (c["paper_vs_mid"], c["paper_vs_mid_inr"], c["paper_vs_touch"]) == (pytest.approx(0.2), 2.0, 0.0)
    assert (c["actual_vs_mid_inr"], c["actual_vs_touch_inr"]) == (3.0, 1.0)
    assert c["paper_vs_mid_pct"] == pytest.approx(0.2 / 100.2 * 100, abs=1e-4)
    s0 = rows[(spread.signal_id, 0)]                                        # a sale: reference - fill
    assert (s0["mid"], s0["touch"], s0["paper_vs_mid"], s0["paper_vs_mid_inr"]) == (60.0, 59, 1.0, 75.0)
    assert s0["paper_vs_mid_pct"] == pytest.approx(100 / 60, abs=1e-4) and s0["band"] == "0-1%"   # 200 / 24200 = 0.83%
    s1 = rows[(spread.signal_id, 1)]
    assert s1["no_quote"] and s1["paper_vs_mid"] is None                   # no quote: counted, never estimated
    rep = xb.report()
    stock = {g["group"]: g for g in rep["by_stock"]}
    assert stock["RELIANCE"]["paper_vs_mid_inr"] == 2.0 and stock["RELIANCE"]["actual_fills"] == 1
    nifty = stock["NIFTY"]
    assert (nifty["legs"], nifty["trades"], nifty["no_quote"], nifty["paper_vs_mid_inr"]) == (2, 1, 1, 75.0)
    assert nifty["low_sample"] and nifty["depth_note"] == "depth not sent by the feed"
    bands = {g["group"]: g for g in rep["by_strike_band"]}
    assert set(bands) == {"cash", "0-1%", "1-2%"} and bands["0-1%"]["avg_spread"] == 2.0   # hedge 23900: 1.24% from spot
    assert bands["1-2%"]["no_quote"] == 1
    csv = xb.csv()
    assert csv.splitlines()[0].startswith("signal_id,leg,symbol,side") and len(csv.splitlines()) == 4


@pytest.mark.parametrize("strike,spot,band", [(None, 100, "cash"), (24000, 24200, "0-1%"), (24500, 24000, "2-5%"),
                                             (26500, 24000, "10%+"), (24000, None, "spot unknown")])
def test_bands(strike, spot, band):
    assert band_of(strike, spot, [1, 2, 5, 10]) == band
