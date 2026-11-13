"""Phase 4: option Greeks, payoff, instrument list, option chain, option history, option strategies,
option backtests with a no-filter baseline, margins, routes. Acceptance checks are marked ACCEPTANCE."""
import math
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.backtest import Engine
from app.calendar import MarketCalendar
from app.config import Settings, SwingBreakoutParams
from app.costs import CostModel, money
from app.history import daily_start
from app.models import Bar, InvalidSignal, Signal
from app.options import OPTION_CLASSES, build_option_strategies
from app.options.analytics import OptionDesk, atm_iv_pct, highest_oi_strike, oi_change, put_call_ratio
from app.options.backtest import HistoryQuotes, OptionEngine, compare, model_prices, run_option_backtest
from app.options.base import ChainQuotes, ExpiryInfo, OptionContext, OptionQuote, metrics, pick_expiry
from app.options.chain import BAD_OPTION_QUOTE, ChainLoader, parse_chain_rows, wanted_specs
from app.options.greeks import (bs_delta, bs_price, implied_vol, iv_and_delta, iv_rank, norm_cdf, years_to_expiry)
from app.options.instruments import (ContractSpec, InstrumentBook, build_specs, monthly_expiries, parse_fno_master,
                                     refresh_from_broker, sim_specs)
from app.options.margin import MarginBook, MarginEntry, import_margins, parse_margins
from app.options.option_history import (BreezeOptionFetcher, OptionHistory, OptionHistoryError, atm_candidates,
                                        atm_iv_series, candidate_expiries, discover_expiries, parse_option_rows)
from app.options.payoff import (InvalidStructure, OptionLeg, check_hedged, credit_spread, iron_condor, long_option,
                                pnl_at_expiry)
from app.quality import IST
from app.regime import REGIMES
from app.store import Store
from app.strategies import Strategy
from tests.test_phase3 import FixedRegime, settings

UTC = timezone.utc
NOW = datetime(2026, 11, 13, 5, 0, tzinfo=UTC)          # Friday 10:30 IST
EXP_W = date(2026, 11, 17)                              # 4 days away: weekly
EXP_M = date(2026, 12, 8)                               # 25 days away: monthly
RATE = 0.065


# ---------------- Black-Scholes, implied volatility, delta ----------------
def test_ACCEPTANCE_iv_and_delta_match_a_hand_calculation():
    """S=100, K=100, t=1y, r=5%, q=0, sigma=20%:
    d1 = (0 + (0.05 + 0.02) * 1) / 0.2 = 0.35, d2 = 0.15
    call = 100*N(0.35) - 100*e^-0.05*N(0.15) = 63.6831 - 95.1229*0.559618 = 10.4506
    put  = call - S + K*e^-0.05 = 10.4506 - 100 + 95.1229 = 5.5735 ; call delta = N(0.35) = 0.6368"""
    assert norm_cdf(0.35) == pytest.approx(0.636831, abs=1e-6)
    assert bs_price(100, 100, 1, 0.05, 0, 0.2, "CE") == pytest.approx(10.4506, abs=1e-4)
    assert bs_price(100, 100, 1, 0.05, 0, 0.2, "PE") == pytest.approx(5.5735, abs=1e-4)
    assert bs_delta(100, 100, 1, 0.05, 0, 0.2, "CE") == pytest.approx(0.6368, abs=1e-4)
    assert bs_delta(100, 100, 1, 0.05, 0, 0.2, "PE") == pytest.approx(0.6368 - 1, abs=1e-4)
    assert implied_vol(10.4506, 100, 100, 1, 0.05, 0, "CE") == pytest.approx(0.20, abs=1e-4)
    iv, delta = iv_and_delta(5.5735, 100, 100, 1, 0.05, 0, "PE")
    assert iv == pytest.approx(0.20, abs=1e-4) and delta == pytest.approx(-0.3632, abs=1e-4)


@given(s=st.floats(50, 50000), m=st.floats(0.7, 1.3), t=st.floats(0.01, 1.0), sigma=st.floats(0.05, 1.5),
       right=st.sampled_from(["CE", "PE"]))
@hyp_settings(max_examples=150, deadline=None)
def test_implied_vol_inverts_the_price(s, m, t, sigma, right):
    k = s * m
    p = bs_price(s, k, t, RATE, 0.0, sigma, right)
    iv = implied_vol(p, s, k, t, RATE, 0.0, right)
    if iv is not None:                                   # None only when the price carries no volatility information
        assert bs_price(s, k, t, RATE, 0.0, iv, right) == pytest.approx(p, rel=1e-4, abs=1e-4)
        d = bs_delta(s, k, t, RATE, 0.0, iv, right)
        assert (0 <= d <= 1) if right == "CE" else (-1 <= d <= 0)


@pytest.mark.parametrize("args", [
    (None, 100, 100, 0.1), (float("nan"), 100, 100, 0.1), (-1, 100, 100, 0.1), (5, 0, 100, 0.1),
    (5, 100, 100, 0), (10, 100, 50, 0.1), (150, 100, 100, 0.1), (5, 100, 100, float("inf"))])   # 10 < intrinsic 50
def test_unknown_inputs_give_no_iv_never_a_guess(args):
    price, s, k, t = args
    assert iv_and_delta(price, s, k, t, RATE, 0.0, "CE") == (None, None)
    assert bs_price(100, 100, 1, RATE, 0, 0.2, "XX") is None


def test_iv_rank():
    hist = [10 + i * 0.05 for i in range(250)]            # 10 .. 22.45
    assert iv_rank(22.45, hist, 200) == pytest.approx(100)
    assert iv_rank(10.0, hist, 200) == pytest.approx(0)
    assert iv_rank(16.225, hist, 200) == pytest.approx(50, abs=0.1)
    assert iv_rank(15, hist[:150], 200) is None           # too little history: unknown
    assert iv_rank(None, hist, 200) is None and iv_rank(15, [12.0] * 250, 200) is None


def test_years_to_expiry_is_none_after_expiry():
    assert years_to_expiry(NOW, EXP_M) == pytest.approx((25 + 5 / 24) / 365, abs=1e-6)
    assert years_to_expiry(datetime(2026, 12, 8, 11, 0, tzinfo=UTC), EXP_M) is None


# ---------------- payoff (property-based) ----------------
def leg(action, right, strike, price, lot=65, expiry=EXP_M, lots=1):
    return OptionLeg(action, right, strike, expiry, lots, price, lot)


def test_payoff_examples_by_hand():
    """Bull put 24000/23800, credit 40, lot 65, costs 150: received 2,600; max profit 2,450;
    max loss (200-40)*65 + 150 = 10,550; breakeven 24000 - 40 + 150/65 = 23,962.31."""
    p = credit_spread("PE", 24000, 23800, 40, 65, 1, 150)
    assert (p.net_premium_inr, p.max_profit_inr, p.max_loss_inr) == (Decimal("2600.00"), Decimal("2450.00"), Decimal("10550.00"))
    assert float(p.breakevens[0]) == pytest.approx(23962.3077, abs=1e-4)
    assert p.as_dict(200000)["max_loss_pct_of_capital"] == 5.28 or p.as_dict(200000)["max_loss_pct_of_capital"] == 5.27
    c = iron_condor(23800, 23700, 24600, 24700, 30, 65, 1, 300)
    assert (c.max_profit_inr, c.max_loss_inr) == (Decimal("1650.00"), Decimal("4850.00"))
    lo = long_option("CE", 24000, 150, 65, 1, 70)
    assert lo.max_loss_inr == Decimal("9820.00") and lo.max_profit_inr is None        # premium + costs; unlimited upside
    assert long_option("PE", 100, 5, 10, 1, 20).max_profit_inr == Decimal("930.00")     # (100-5)*10 - 20


money_st = st.decimals(min_value=1, max_value=500, places=2)


@given(ks=st.integers(100, 60000), width=st.integers(1, 2000), frac=st.floats(0.01, 0.99), lot=st.integers(1, 5000),
       costs=st.decimals(min_value=0, max_value=5000, places=2), right=st.sampled_from(["PE", "CE"]),
       spot=st.floats(1, 120000))
@hyp_settings(max_examples=200, deadline=None)
def test_credit_spread_payoff_is_bounded_at_every_expiry_price(ks, width, frac, lot, costs, right, spot):
    kl = ks - width if right == "PE" else ks + width
    if kl <= 0:
        return
    credit = round(width * frac, 2)
    if not (0 < credit < width):
        return
    p = credit_spread(right, ks, kl, credit, lot, 1, costs)
    legs = [leg("SELL", right, ks, credit + 1.0, lot), leg("BUY", right, kl, 1.0, lot)]
    pnl = pnl_at_expiry(legs, round(spot, 2), costs)
    assert -p.max_loss_inr - Decimal("0.02") <= pnl <= p.max_profit_inr + Decimal("0.02")
    assert p.max_loss_inr > 0 and p.max_profit_inr + p.max_loss_inr == money(Decimal(width) * lot)
    worst = kl                                           # at the bought strike (or beyond) the loss is the maximum
    assert pnl_at_expiry(legs, worst, costs) == pytest.approx(-p.max_loss_inr, abs=Decimal("0.02"))
    if p.max_profit_inr > 0:
        assert abs(pnl_at_expiry(legs, p.breakevens[0], costs)) <= Decimal("0.02") + Decimal(lot) * Decimal("0.01")
    else:
        assert p.breakevens == ()                            # costs eat the whole credit: it cannot break even


@given(mid=st.integers(1000, 50000), gap=st.integers(10, 2000), wp=st.integers(5, 1000), wc=st.integers(5, 1000),
       frac=st.floats(0.01, 0.99), lot=st.integers(1, 2000), costs=st.decimals(min_value=0, max_value=3000, places=2),
       spot=st.floats(1, 120000))
@hyp_settings(max_examples=200, deadline=None)
def test_iron_condor_payoff_is_bounded(mid, gap, wp, wc, frac, lot, costs, spot):
    ps, cs = mid - gap, mid + gap
    pl, cl = ps - wp, cs + wc
    if pl <= 0:
        return
    credit = round(min(wp, wc) * frac, 2)
    if credit <= 0:
        return
    p = iron_condor(ps, pl, cs, cl, credit, lot, 1, costs)
    legs = [leg("SELL", "PE", ps, credit + 1.0, lot), leg("BUY", "PE", pl, 1.0, lot),
            leg("SELL", "CE", cs, 1.0, lot), leg("BUY", "CE", cl, 1.0, lot)]       # net premium = credit
    pnl = pnl_at_expiry(legs, round(spot, 2), costs)
    assert -p.max_loss_inr - Decimal("0.02") <= pnl <= p.max_profit_inr + Decimal("0.02")
    assert pnl_at_expiry(legs, mid, costs) == pytest.approx(p.max_profit_inr, abs=Decimal("0.02"))


@given(k=st.integers(10, 60000), prem=money_st, lot=st.integers(1, 5000), spot=st.floats(0.5, 200000),
       costs=st.decimals(min_value=0, max_value=3000, places=2), right=st.sampled_from(["PE", "CE"]))
@hyp_settings(max_examples=200, deadline=None)
def test_long_option_never_loses_more_than_premium_plus_costs(k, prem, lot, spot, costs, right):
    p = long_option(right, k, prem, lot, 1, costs)
    pnl = pnl_at_expiry([leg("BUY", right, k, float(prem), lot)], round(spot, 2), costs)
    assert pnl >= -p.max_loss_inr - Decimal("0.02")
    assert p.max_loss_inr == money(prem * lot) + costs
    if p.max_profit_inr is not None:
        assert pnl <= p.max_profit_inr + Decimal("0.02")


@pytest.mark.parametrize("call", [
    lambda: credit_spread("PE", 24000, 24100, 40, 65),        # bought put above the sold put
    lambda: credit_spread("CE", 24000, 23900, 40, 65),
    lambda: credit_spread("PE", 24000, 23800, 250, 65),       # credit above the width: bad prices
    lambda: credit_spread("PE", 24000, 23800, 40, 0),         # lot size unknown
    lambda: iron_condor(23800, 23900, 24600, 24700, 30, 65),
    lambda: long_option("CE", 24000, 0, 65), lambda: long_option("CE", 24000, float("nan"), 65)])
def test_invalid_structures_are_rejected(call):
    with pytest.raises(InvalidStructure):
        call()


def test_ACCEPTANCE_a_short_leg_without_its_hedge_is_rejected():
    short = leg("SELL", "PE", 24000, 50)
    assert check_hedged([short]) == ["UNHEDGED_SHORT"]
    assert check_hedged([short, leg("BUY", "PE", 23800, 20)]) == []
    assert check_hedged([short, leg("BUY", "CE", 23800, 20)]) == ["UNHEDGED_SHORT"]             # wrong right
    assert check_hedged([short, leg("BUY", "PE", 23800, 20, expiry=EXP_W)]) == ["UNHEDGED_SHORT"]  # other expiry
    assert check_hedged([short, leg("BUY", "PE", 24100, 20)]) == ["UNHEDGED_SHORT"]             # not further out
    assert check_hedged([leg("SELL", "PE", 24000, 50, lots=2), leg("BUY", "PE", 23800, 20)]) == ["UNHEDGED_SHORT"]
    two_shorts = [short, leg("SELL", "PE", 23900, 40), leg("BUY", "PE", 23700, 15)]
    assert check_hedged(two_shorts) == ["UNHEDGED_SHORT"]                                       # one hedge covers one short
    assert check_hedged([leg("BUY", "CE", 24000, 100)]) == []


# ---------------- instrument list: lot sizes and expiries, never hardcoded ----------------
FNO_HEADER = '"Token","InstrumentName","ShortName","Series","ExpiryDate","StrikePrice","OptionType","LotSize","ExchangeCode"'


def fno_lines(rows):
    return [FNO_HEADER] + [f'"1","{inst}","{code}","OPTION","{exp}","{k}","{right}","{lot}","X"'
                           for inst, code, exp, k, right, lot in rows]


def fixture_fno(nifty_lot=65):
    rows = []
    for exp in ("17-Nov-2026", "23-Nov-2026", "01-Dec-2026", "29-Dec-2026"):
        for k in (23900, 24000, 24100):
            for r in ("CE", "PE"):
                rows.append(("OPTIDX", "NIFTY", exp, k, r, nifty_lot))
    rows += [("OPTIDX", "CNXBAN", "23-Nov-2026", 55000, "CE", 30), ("OPTIDX", "CNXBAN", "23-Nov-2026", 55000, "PE", 30),
             ("OPTSTK", "RELIND", "23-Nov-2026", 1340, "CE", 500), ("OPTSTK", "RELIND", "23-Nov-2026", 1340, "PE", 500),
             ("OPTSTK", "INFTEC", "29-Dec-2026", 1100, "CE", 400), ("OPTSTK", "INFTEC", "29-Dec-2026", 1120, "CE", 500),
             ("FUTIDX", "NIFTY", "23-Nov-2026", 0, "XX", 65), ("OPTSTK", "NOTWATCHED", "23-Nov-2026", 100, "CE", 10)]
    return fno_lines(rows)


def test_ACCEPTANCE_lot_sizes_and_expiries_come_from_the_instrument_list():
    s = settings()
    store = Store(":memory:")
    n = refresh_from_broker(store, s.watchlist, now=NOW, lines=fixture_fno())
    book = InstrumentBook(store, "breeze")
    assert n == 6                                          # 4 NIFTY, 1 BANKNIFTY, 1 RELIANCE; INFY's lot sizes disagree
    nifty = book.specs("NIFTY", date(2026, 11, 13), NOW)
    assert [(x.expiry, x.kind, x.lot_size) for x in nifty] == [
        (date(2026, 11, 17), "weekly", 65), (date(2026, 11, 23), "monthly", 65),
        (date(2026, 12, 1), "weekly", 65), (date(2026, 12, 29), "monthly", 65)]
    assert nifty[0].strikes == (23900.0, 24000.0, 24100.0)
    assert book.lot_size("BANKNIFTY", date(2026, 11, 23), NOW) == 30
    assert book.lot_size("RELIANCE", date(2026, 11, 23), NOW) == 500
    assert book.specs("INFY", now=NOW) == []               # conflicting lot sizes: unknown, not guessed
    assert book.lot_size("TCS", date(2026, 11, 23), NOW) is None
    # the lot size is whatever the list says: a revised list changes it
    refresh_from_broker(store, s.watchlist, now=NOW, lines=fixture_fno(nifty_lot=75))
    assert InstrumentBook(store, "breeze").lot_size("NIFTY", date(2026, 11, 17), NOW) == 75


def test_stale_instrument_list_is_unknown():
    s = settings()
    store = Store(":memory:")
    refresh_from_broker(store, s.watchlist, now=NOW, lines=fixture_fno())
    book = InstrumentBook(store, "breeze", max_age_days=5)
    assert book.is_fresh(NOW + timedelta(days=4)) and not book.is_fresh(NOW + timedelta(days=6))
    assert book.lot_size("NIFTY", date(2026, 11, 17), NOW + timedelta(days=6)) is None


def test_monthly_is_the_last_expiry_of_its_month_and_bad_rows_are_skipped():
    es = [date(2026, 11, 17), date(2026, 11, 23), date(2026, 12, 1), date(2026, 12, 29)]
    assert monthly_expiries(es) == {date(2026, 11, 23), date(2026, 12, 29)}
    rows = parse_fno_master(fno_lines([("OPTIDX", "NIFTY", "bad-date", 24000, "CE", 65),
                                       ("OPTIDX", "NIFTY", "17-Nov-2026", 0, "CE", 65),
                                       ("OPTIDX", "NIFTY", "17-Nov-2026", 24000, "CE", 0),
                                       ("OPTIDX", "NIFTY", "17-Nov-2026", 24000, "CE", 65)]))
    assert len(rows) == 1 and rows[0]["lot_size"] == 65
    with pytest.raises(ValueError):
        parse_fno_master(['"Token","ShortName"'])


def test_no_lot_size_or_expiry_is_hardcoded_in_the_option_code():
    """Lot sizes of the real contracts (65, 30, 75, 25, 50, 15 for the indices over the years) must not
    appear as literals next to the word lot in app/options, and no expiry date literal either."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "options"
    hits = []
    for f in root.glob("*.py"):
        if f.name in ("instruments.py",):                 # sim_specs derives a FAKE lot from the price (no table)
            continue
        text = f.read_text(encoding="utf-8")
        hits += [f"{f.name}: {m.group(0)}" for m in re.finditer(r"lot_size\s*=\s*\d+", text)]
        hits += [f"{f.name}: {m.group(0)}" for m in re.finditer(r"date\(20\d\d,\s*\d+,\s*\d+\)", text)]
    assert hits == []


def test_sim_specs_are_fake_but_complete():
    specs = sim_specs({"NIFTY": 24000.0, "RELIANCE": 1340.0}, date(2026, 11, 13), MarketCalendar(), NOW, {"NIFTY"})
    nifty = [x for x in specs if x.underlying == "NIFTY"]
    assert {x.kind for x in nifty} == {"weekly", "monthly"} and all(x.expiry >= date(2026, 11, 13) for x in specs)
    assert all(x.lot_size >= 1 and 24000.0 in x.strikes for x in nifty)
    assert {x.kind for x in specs if x.underlying == "RELIANCE"} == {"monthly"}


# ---------------- option chain: quality gate ----------------
def chain_row(k, bid, ask, ltp=None, oi=1000, **kw):
    return {"strike_price": k, "best_bid_price": bid, "best_offer_price": ask, "ltp": ltp if ltp is not None else bid,
            "open_interest": oi, "chnge_oi": 10, "total_quantity_traded": "500", "spot_price": "24000", **kw}


def test_ACCEPTANCE_rows_with_missing_or_crossed_bid_ask_are_quarantined():
    rows = [chain_row(24000, 100, 101), chain_row(24100, 0, 60), chain_row(24200, 40, None),
            chain_row(24300, 30, 29.5), chain_row(24400, -1, 5), chain_row(None, 5, 6), chain_row(24500, 10, 10.5, oi=-5),
            chain_row(24600, "nan", 4)]
    good, bad = parse_chain_rows(rows, "CE")
    assert [g["strike"] for g in good] == [24000.0]
    assert sorted(r for r, _ in bad) == ["crossed bid/ask", "missing bid/ask", "missing bid/ask", "missing bid/ask",
                                         "missing bid/ask", "negative open interest", "no strike"]


class FakeChainSource:
    name, remote = "sim", False

    def __init__(self, rows_by_right, fail=()):
        self.rows, self.fail, self.calls = rows_by_right, set(fail), []

    def fetch(self, inst, spec, right):
        self.calls.append((inst.symbol, spec.expiry, right))
        if inst.symbol in self.fail:
            raise RuntimeError("boom")
        return self.rows(spec, right) if callable(self.rows) else self.rows[right]


def saved_book(store, specs):
    book = InstrumentBook(store, "sim")
    book.save(specs)
    return book


def spec(sym="NIFTY", expiry=EXP_M, kind="monthly", lot=65, strikes=(23900.0, 24000.0, 24100.0)):
    return ContractSpec(sym, expiry, lot, kind, tuple(strikes), NOW)


def test_chain_loader_stores_good_rows_and_logs_quarantined_ones():
    s = settings()
    store = Store(":memory:")
    book = saved_book(store, [spec(expiry=EXP_W, kind="weekly"), spec(), spec("BANKNIFTY", lot=30)])
    src = FakeChainSource({"CE": [chain_row(24000, 100, 101), chain_row(24100, 0, 0)],
                           "PE": [chain_row(24000, 90, 91), chain_row(23900, 50, 49)]}, fail={"BANKNIFTY"})
    out = ChainLoader(s, store, src, book).load(now=NOW)
    assert out["NIFTY:2026-12-08"] == {"saved": 2, "quarantined": 2}
    assert "error" in out["BANKNIFTY:2026-12-08"]                       # one failing symbol does not stop the others
    rows = store.latest_option_chain("sim", "NIFTY", EXP_M)
    assert [(r["strike"], r["right"], r["bid"], r["ask"], r["oi"]) for r in rows] == [
        (24000.0, "CE", 100.0, 101.0, 1000.0), (24000.0, "PE", 90.0, 91.0, 1000.0)]
    assert rows[0]["as_of"] == NOW
    ev = [e for e in store.recent_quality_events(20) if e["reason"] == BAD_OPTION_QUOTE]
    assert len(ev) == 4                                                 # 2 bad rows x 2 NIFTY expiries
    assert ChainLoader(s, store, src, book).load(["TCS"], now=NOW)["TCS"]["error"].startswith("no contracts")


def test_wanted_specs_weekly_and_monthly():
    s = settings()
    store = Store(":memory:")
    specs = [spec(expiry=date(2026, 11, 17), kind="weekly"), spec(expiry=date(2026, 11, 23)),
             spec(expiry=date(2026, 12, 1), kind="weekly"), spec(expiry=date(2026, 12, 29)),
             spec("BANKNIFTY", date(2026, 11, 23), lot=30), spec("BANKNIFTY", date(2026, 12, 29), lot=30),
             spec("BANKNIFTY", date(2027, 1, 25), lot=30), spec("RELIANCE", date(2026, 11, 23), lot=500)]
    book = saved_book(store, specs)
    today = date(2026, 11, 13)
    assert [x.expiry for x in wanted_specs(book, s, "NIFTY", today, NOW)] == [
        date(2026, 11, 17), date(2026, 11, 23), date(2026, 12, 29)]
    assert [x.expiry for x in wanted_specs(book, s, "BANKNIFTY", today, NOW)] == [date(2026, 11, 23), date(2026, 12, 29)]
    assert [x.expiry for x in wanted_specs(book, s, "RELIANCE", today, NOW)] == [date(2026, 11, 23)]
    assert wanted_specs(book, s, "INDIAVIX", today, NOW) == []


# ---------------- a fixture chain priced by Black-Scholes ----------------
def bs_chain(spot=24000.0, expiry=EXP_M, iv=0.15, lo=22000, hi=26000, step=100, put_wall=23500, call_wall=24500,
             now=NOW, spread=0.005, lot=65):
    t = years_to_expiry(now, expiry)
    out = []
    for k in range(lo, hi + 1, step):
        for right in ("CE", "PE"):
            p = bs_price(spot, k, t, RATE, 0.0, iv, right)
            if p < 1:
                continue
            oi = lot * (4000 if (right == "PE" and k == put_wall) or (right == "CE" and k == call_wall) else 800)
            out.append(OptionQuote(float(k), right, round(p * (1 - spread), 2), round(p * (1 + spread), 2),
                                   round(p, 2), float(oi), 50.0, 1000.0))
    return out


def make_ctx(s=None, **over):
    s = s or settings()
    spot = over.pop("spot", 24000.0)
    chains = over.pop("chains", None) or {EXP_W: bs_chain(spot, EXP_W), EXP_M: bs_chain(spot, EXP_M)}
    base = dict(
        underlying="NIFTY", kind="index", asof=NOW, regime="TRENDING_UP", spot=spot,
        expiries=(ExpiryInfo(EXP_W, "weekly", 65), ExpiryInfo(EXP_M, "monthly", 65)),
        quotes=ChainQuotes(spot, chains), costs=CostModel(s.costs), capital_inr=s.account.capital_inr, rate=RATE,
        iv_reference_pct=15.0, iv_rank=70.0, allowed_kinds=("weekly", "monthly"),
        margin=lambda structure, width: 40000.0, min_oi_lots=100, max_spread_pct=10.0)
    base.update(over)
    return OptionContext(**base)


def strat(sid, s=None):
    return build_option_strategies(s or settings(), [sid])[0]


# ---------------- credit spread ----------------
def test_credit_spread_signal_carries_every_figure():
    s = settings()
    cs = strat("credit_spread", s)
    ctx = make_ctx(s)
    out = cs.evaluate(ctx)
    assert len(out) == 1, cs.last_no_trade
    sig = out[0]
    assert (sig.asset_class, sig.direction, sig.horizon, sig.symbol) == ("INDEX_OPTION", "SPREAD", "MONTHLY", "NIFTY")
    sell, buy = sig.legs
    assert (sell.action, sell.right, buy.action, buy.right) == ("SELL", "PE", "BUY", "PE")     # bull put in TRENDING_UP
    assert sell.expiry == buy.expiry == EXP_M and sell.lot_size == buy.lot_size == 65
    # sold strike: |delta| <= 0.20 from its own price, and the next strike toward the money breaks the limit
    m_here, m_inner = metrics(ctx, EXP_M, sell.strike, "PE"), metrics(ctx, EXP_M, sell.strike + 100, "PE")
    assert abs(m_here[2]) <= 0.20 < abs(m_inner[2])
    assert sell.strike < 23500                                   # beyond the highest-OI put strike (support)
    assert buy.strike == sell.strike - 200                       # ~1% of 24000 = 240 -> nearest listed strike
    d = sig.details
    credit = round(sell.price - buy.price, 2)
    assert sell.price == ctx.quotes.quote(EXP_M, sell.strike, "PE").bid         # sell at the bid, buy at the ask
    assert buy.price == ctx.quotes.quote(EXP_M, buy.strike, "PE").ask
    assert d["premium"] == d["net_credit"] == credit and d["lot_size"] == 65
    assert d["net_premium_inr"] == pytest.approx(credit * 65, abs=0.01)
    assert d["max_loss_inr"] == pytest.approx((200 - credit) * 65 + d["costs_inr"], abs=0.01)
    assert d["max_profit_inr"] == pytest.approx(credit * 65 - d["costs_inr"], abs=0.01)
    assert d["max_loss_pct_of_capital"] == pytest.approx(d["max_loss_inr"] / 200000 * 100, abs=0.01)
    assert d["margin_inr"] == 40000.0 and len(d["breakevens"]) == 1 and d["breakevens"][0] < sell.strike
    assert d["costs_inr"] > 0 and d["iv_rank"] == 70.0 and d["days_to_expiry"] == 25
    assert sig.entry == credit and sig.target == pytest.approx(credit * 0.5, abs=0.01) and sig.stop == pytest.approx(credit * 2, abs=0.01)
    assert any("TRENDING_UP" in w for w in sig.why_now) and any("delta" in w for w in sig.why_now)
    assert cs.evaluate(ctx)[0] == sig                            # deterministic: same context, same signal
    assert sig.to_dict()["legs"][0]["action"] == "SELL"


def test_bear_call_spread_in_a_down_trend_and_the_oi_rule_moves_the_strike():
    cs = strat("credit_spread")
    sig = cs.evaluate(make_ctx(regime="TRENDING_DOWN"))[0]
    sell, buy = sig.legs
    assert (sell.right, buy.right) == ("CE", "CE") and buy.strike > sell.strike > 24500
    far_wall = {EXP_M: bs_chain(put_wall=23000)}
    assert cs.evaluate(make_ctx(chains=far_wall)) == [] and codes(cs) == ["CREDIT_TOO_SMALL"]   # that far out pays too little
    cs = strat("credit_spread", settings(strategies={"credit_spread": {"min_credit_pct_of_width": 0}}))
    sig2 = cs.evaluate(make_ctx(chains=far_wall))[0]
    assert sig2.legs[0].strike == 22900.0                        # first strike beyond the highest-OI put at 23000
    assert any("beyond the highest-OI PE strike 23000" in w for w in sig2.why_now)


def codes(strategy):
    return [r.split(":")[0] for r in strategy.last_no_trade]


def test_ACCEPTANCE_no_signal_if_bid_ask_lot_size_or_margin_is_unknown():
    cs = strat("credit_spread")
    no_quotes = {EXP_M: [replace(q, bid=None, ask=None) for q in bs_chain()]}
    assert cs.evaluate(make_ctx(chains=no_quotes)) == [] and "NO_STRIKE" in codes(cs)
    assert cs.evaluate(make_ctx(expiries=(ExpiryInfo(EXP_M, "monthly", None),))) == [] and codes(cs) == ["LOT_SIZE_UNKNOWN"]
    assert cs.evaluate(make_ctx(margin=lambda structure, width: None)) == [] and codes(cs) == ["MARGIN_UNKNOWN"]
    assert cs.evaluate(make_ctx(margin=None)) == [] and codes(cs) == ["MARGIN_UNKNOWN"]
    assert "width 200" in cs.last_no_trade[0]                    # tells the user which margin to enter
    lo = strat("long_option")
    trig = trigger_signal()
    assert lo.evaluate(make_ctx(chains=no_quotes, trigger=trig, iv_rank=30.0)) == [] and "DATA_MISSING" in codes(lo)
    assert lo.evaluate(make_ctx(expiries=(ExpiryInfo(EXP_M, "monthly", None),), trigger=trig, iv_rank=30.0)) == []
    assert codes(lo) == ["LOT_SIZE_UNKNOWN"]
    ic = strat("iron_condor")
    assert ic.evaluate(make_ctx(regime="RANGE", margin=lambda structure, width: None)) == [] and codes(ic) == ["MARGIN_UNKNOWN"]


def test_ACCEPTANCE_stock_option_selling_returns_no_trade():
    for sid in ("credit_spread", "iron_condor"):
        st_ = strat(sid)
        for regime in ("TRENDING_UP", "RANGE", "TRENDING_DOWN"):
            ctx = make_ctx(underlying="RELIANCE", kind="stock", regime=regime)
            assert st_.evaluate(ctx) == []
            assert codes(st_) == ["STOCK_OPTION_SELLING_NOT_ALLOWED"]
        assert st_.evaluate(make_ctx(underlying="RELIANCE", kind="stock", no_filter=True, baseline_right="PE")) == []


@pytest.mark.parametrize("over,code", [
    (dict(regime="RANGE"), "OUTSIDE_REGIME"), (dict(regime="EVENT"), "OUTSIDE_REGIME"), (dict(regime="UNKNOWN"), "OUTSIDE_REGIME"),
    (dict(iv_rank=None), "IV_RANK_UNKNOWN"), (dict(iv_rank=30.0), "IV_RANK_TOO_LOW"),
    (dict(status="STALE"), "DATA_STALE"), (dict(spot=None), "DATA_MISSING"),
    (dict(expiries=(ExpiryInfo(EXP_W, "weekly", 65),)), "NO_EXPIRY"),
    (dict(min_oi_lots=5000), "ILLIQUID"), (dict(max_spread_pct=0.5), "ILLIQUID")])
def test_credit_spread_no_trade_reasons(over, code):
    cs = strat("credit_spread")
    if over.get("spot", 1) is None:
        ctx = replace(make_ctx(), spot=None)
    else:
        ctx = make_ctx(**over)
    assert cs.evaluate(ctx) == [] and code in codes(cs)


def test_credit_too_small_and_no_hedge_means_no_signal():
    cs = strat("credit_spread", settings(strategies={"credit_spread": {"min_credit_pct_of_width": 60}}))
    assert cs.evaluate(make_ctx()) == [] and codes(cs) == ["CREDIT_TOO_SMALL"]
    cs = strat("credit_spread")
    short = cs.evaluate(make_ctx())[0].legs[0].strike
    only_short = {EXP_M: [q for q in bs_chain() if not (q.right == "PE" and q.strike < short)]}
    assert cs.evaluate(make_ctx(chains=only_short)) == [] and codes(cs) == ["UNHEDGED_SHORT"]   # never a sold option alone


# ---------------- iron condor ----------------
def test_iron_condor_only_in_range_never_on_event_days():
    ic = strat("iron_condor")
    for regime in REGIMES:
        out = ic.evaluate(make_ctx(regime=regime))
        assert (len(out) == 1) == (regime == "RANGE"), (regime, ic.last_no_trade)
    sig = ic.evaluate(make_ctx(regime="RANGE"))[0]
    ps, pl, cs_, cl = sig.legs
    assert [(x.action, x.right) for x in sig.legs] == [("SELL", "PE"), ("BUY", "PE"), ("SELL", "CE"), ("BUY", "CE")]
    assert pl.strike < ps.strike < 24000 < cs_.strike < cl.strike and {x.expiry for x in sig.legs} == {EXP_W}
    assert sig.horizon == "WEEKLY" and sig.details["structure"] == "IRON_CONDOR" and len(sig.details["breakevens"]) == 2
    credit = round(ps.price - pl.price + cs_.price - cl.price, 2)
    width = max(ps.strike - pl.strike, cl.strike - cs_.strike)
    assert sig.details["max_loss_inr"] == pytest.approx((width - credit) * 65 + sig.details["costs_inr"], abs=0.01)
    assert check_hedged(sig.legs) == []
    assert ic.evaluate(make_ctx(regime="RANGE", allowed_kinds=("monthly",))) == [] and codes(ic) == ["NO_WEEKLY_OPTIONS"]


# ---------------- directional option buying ----------------
def trigger_signal(direction="LONG", sid="swing_pullback_v2", symbol="NIFTY", horizon="SWING"):
    stop, target = (23800.0, 24400.0) if direction == "LONG" else (24200.0, 23600.0)
    return Signal(signal_id="t1", created_at=NOW, strategy_id=sid, strategy_version="2/x", symbol=symbol,
                  asset_class="CASH_EQUITY", direction=direction, horizon=horizon, entry=24000.0, stop=stop,
                  target=target, valid_until=NOW + timedelta(minutes=15), regime="TRENDING_UP",
                  why_now=("pullback to the 20-day average", "close above it"), data_as_of=NOW)


def test_long_option_buys_near_the_money_and_shows_max_loss():
    lo = strat("long_option")
    ctx = make_ctx(trigger=trigger_signal(), iv_rank=30.0)
    sig = lo.evaluate(ctx)[0]
    (leg_,) = sig.legs
    assert (leg_.action, leg_.right, leg_.strike, leg_.expiry) == ("BUY", "CE", 24000.0, EXP_M)   # ATM, >= 7 days away
    assert leg_.price == ctx.quotes.quote(EXP_M, 24000.0, "CE").ask
    d = sig.details
    assert d["total_cost_inr"] == d["max_loss_inr"] == pytest.approx(leg_.price * 65 + d["costs_inr"], abs=0.01)
    assert d["margin_inr"] == pytest.approx(leg_.price * 65, abs=0.01) and d["max_profit_inr"] is None
    assert sig.stop == pytest.approx(leg_.price * 0.6, abs=0.01) and sig.target == pytest.approx(leg_.price * 1.8, abs=0.01)
    assert (sig.direction, sig.asset_class, sig.horizon) == ("LONG", "INDEX_OPTION", "SWING")
    put = lo.evaluate(make_ctx(trigger=trigger_signal("SHORT", "orb_v2", horizon="INTRADAY"), iv_rank=30.0))[0]
    assert put.legs[0].right == "PE" and put.horizon == "INTRADAY"


def test_long_option_rules():
    lo = strat("long_option")
    assert lo.evaluate(make_ctx(iv_rank=30.0)) == [] and codes(lo) == ["NO_TRIGGER"]
    assert lo.evaluate(make_ctx(trigger=trigger_signal(), iv_rank=75.0)) == [] and codes(lo) == ["IV_RANK_TOO_HIGH"]
    assert lo.evaluate(make_ctx(trigger=trigger_signal(), iv_rank=None)) == [] and codes(lo) == ["IV_RANK_UNKNOWN"]
    assert lo.evaluate(make_ctx(trigger=trigger_signal(sid="orb"), iv_rank=30.0)) == [] and codes(lo) == ["NO_TRIGGER"]
    assert lo.evaluate(make_ctx(trigger=trigger_signal(symbol="TCS"), iv_rank=30.0)) == [] and codes(lo) == ["NO_TRIGGER"]
    assert lo.evaluate(make_ctx(trigger=trigger_signal(), iv_rank=30.0, regime="EVENT")) == []
    wide = {EXP_M: bs_chain(spread=0.08)}                       # 16% bid-ask width: too illiquid to buy
    assert lo.evaluate(make_ctx(chains=wide, trigger=trigger_signal(), iv_rank=30.0)) == [] and codes(lo) == ["ILLIQUID"]


def test_ACCEPTANCE_stock_options_are_buy_only_with_an_expiry_week_warning():
    lo = strat("long_option")
    chain = bs_chain(1340.0, EXP_M, iv=0.25, lo=1200, hi=1480, step=20, put_wall=1300, call_wall=1400, lot=500)
    ctx = make_ctx(underlying="RELIANCE", kind="stock", spot=1340.0, chains={EXP_M: chain},
                   expiries=(ExpiryInfo(EXP_M, "monthly", 500),), allowed_kinds=("monthly",),
                   trigger=trigger_signal(symbol="RELIANCE"), iv_rank=40.0)
    sig = lo.evaluate(ctx)[0]
    assert sig.asset_class == "STOCK_OPTION" and [x.action for x in sig.legs] == ["BUY"]
    w = sig.details["warnings"]
    assert len(w) == 1 and "physically settled" in w[0] and "2026-12-01" in w[0]      # 7 days before the 8-Dec expiry
    assert w[0] in sig.why_now and sig.details["exit_by"] == "2026-12-01"
    # an expiry inside (or right before) expiry week is not bought at all
    near = date(2026, 11, 20)
    ctx2 = replace(ctx, expiries=(ExpiryInfo(near, "monthly", 500),), quotes=ChainQuotes(1340.0, {near: chain}))
    assert lo.evaluate(ctx2) == [] and codes(lo) == ["NO_EXPIRY"]


def test_option_signal_schema_needs_legs_and_details():
    with pytest.raises(InvalidSignal):
        Signal(signal_id="x", created_at=NOW, strategy_id="credit_spread", strategy_version="1", symbol="NIFTY",
               asset_class="INDEX_OPTION", direction="SPREAD", horizon="MONTHLY", entry=10, stop=20, target=5,
               valid_until=NOW + timedelta(minutes=5), regime="RANGE", why_now=("x",), data_as_of=NOW)


def test_option_strategy_versions_and_status():
    s = settings()
    a = build_option_strategies(s)
    assert [x.id for x in a] == ["long_option", "credit_spread", "iron_condor"] == list(OPTION_CLASSES)
    b = strat("credit_spread", settings(strategies={"credit_spread": {"max_short_delta": 0.15}}))
    assert b.version != a[1].version and a[1].version.startswith("1/")
    assert all(not x.alerts_enabled and x.mode == "paper_only" for x in a)      # no alerts until evidence says otherwise
    with pytest.raises(ValueError):
        build_option_strategies(s, ["nope"])


def test_pick_expiry_window():
    ctx = make_ctx()
    assert pick_expiry(ctx, 20, 35, ("monthly",)).expiry == EXP_M
    assert pick_expiry(ctx, 3, 7, ("weekly", "monthly")).expiry == EXP_W
    assert pick_expiry(ctx, 26, 35, ("monthly",)) is None and pick_expiry(ctx, 5, 7, ("weekly",)) is None


# ---------------- analytics ----------------
def test_put_call_ratio_highest_oi_and_oi_change():
    q = [OptionQuote(24000, "CE", 10, 11, 10.5, 1000, 100), OptionQuote(24500, "CE", 5, 6, 5.5, 3000, -50),
         OptionQuote(24000, "PE", 10, 11, 10.5, 2000, 400), OptionQuote(23500, "PE", 5, 6, 5.5, 4000, 200)]
    assert put_call_ratio(q) == 1.5
    assert highest_oi_strike(q, "CE") == 24500 and highest_oi_strike(q, "PE") == 23500
    assert oi_change(q) == {"calls": 50, "largest_increase_calls": 24000, "puts": 600, "largest_increase_puts": 24000}
    assert put_call_ratio([x for x in q if x.right == "CE"]) is None and highest_oi_strike([], "CE") is None
    t = years_to_expiry(NOW, EXP_M)
    assert atm_iv_pct(bs_chain(), 24000.0, t, RATE, 0.0) == pytest.approx(15.0, abs=0.3)


def test_atm_iv_series_recovers_the_volatility():
    d0, exp = date(2026, 10, 1), date(2026, 10, 27)
    closes = {d0 + timedelta(days=i): 1000.0 + i for i in range(10)}
    from app.options.option_history import OptionBar
    bars = {}
    for d, s in closes.items():
        p = bs_price(s, 1000, years_to_expiry(datetime.combine(d, time(15, 30), IST), exp), RATE, 0, 0.3, "CE")
        bars[d] = OptionBar(d, p, p, p, p)
    series = atm_iv_series({(exp, 1000.0, "CE"): bars, (exp, 1200.0, "CE"): bars}, closes, RATE)
    assert len(series) == 10 and all(v == pytest.approx(30.0, abs=0.01) for v in series.values())


# ---------------- margins ----------------
def test_margin_parse_lookup_and_age():
    parsed, errors = parse_margins("underlying,structure,width,margin_per_lot_inr,as_of\n"
                                   "nifty,credit_spread,200,\"38,500\",2026-11-10\nNIFTY,IRON_CONDOR,100,41000,2026-11-13\n")
    assert errors == [] and len(parsed) == 2
    book = MarginBook(list(parsed.values()), max_age_days=7)
    today = date(2026, 11, 13)
    assert book.lookup("NIFTY", "CREDIT_SPREAD", 200, today) == 38500.0
    assert book.lookup("NIFTY", "CREDIT_SPREAD", 250, today) is None            # another width: unknown
    assert book.lookup("BANKNIFTY", "CREDIT_SPREAD", 200, today) is None
    assert book.lookup("NIFTY", "CREDIT_SPREAD", 200, date(2026, 11, 18)) is None   # older than 7 days: unknown
    assert MarginBook([MarginEntry("NIFTY", "CREDIT_SPREAD", 200, 1.0, date(2026, 12, 1))], 7).lookup(
        "NIFTY", "CREDIT_SPREAD", 200, today) is None                           # dated in the future
    _, errors = parse_margins("underlying,structure,width,margin_per_lot_inr,as_of\nNIFTY,NAKED_PUT,200,1,2026-11-10\n"
                              "NIFTY,CREDIT_SPREAD,0,1,2026-11-10\nNIFTY,CREDIT_SPREAD,200,abc,2026-11-10\n")
    assert len(errors) == 3
    assert parse_margins("a,b\n1,2\n")[1]


def test_margin_import_merges_and_is_all_or_nothing(tmp_path):
    s = settings(options={"margin": {"csv": str(tmp_path / "margins.csv")}})
    store = Store(":memory:")
    head = "underlying,structure,width,margin_per_lot_inr,as_of\n"
    assert import_margins(s, store, head + "NIFTY,CREDIT_SPREAD,200,38500,2026-11-13\n")["ok"]
    assert not import_margins(s, store, head + "NIFTY,CREDIT_SPREAD,300,x,2026-11-13\nNIFTY,IRON_CONDOR,100,1,2026-11-13\n")["ok"]
    r = import_margins(s, store, head + "NIFTY,IRON_CONDOR,100,41000,2026-11-13\nNIFTY,CREDIT_SPREAD,200,39000,2026-11-13\n")
    assert r["ok"] and r["rows_total"] == 2
    assert {(m["structure"], m["margin_per_lot_inr"]) for m in store.margins()} == {("CREDIT_SPREAD", 39000.0), ("IRON_CONDOR", 41000.0)}
    assert "39000" in (tmp_path / "margins.csv").read_text()


# ---------------- option history: parsing, fetching, expiry discovery ----------------
def hist_row(d, o=10, h=12, l=9, c=11, vol=1000, oi=5000):
    return {"datetime": f"{d} 00:00:00", "open": o, "high": h, "low": l, "close": c, "volume": vol, "open_interest": oi}


def test_parse_option_rows_applies_the_bar_quality_rules():
    rows = [hist_row("2026-10-01"), hist_row("2026-10-02", o=0), hist_row("2026-10-05", h=5), hist_row("2026-10-06", vol=-1),
            {"open": 1, "high": 1, "low": 1, "close": 1}, hist_row("2026-10-07", c="nan")]
    good, bad = parse_option_rows(rows)
    assert len(good) == 1 and good[0][1:] == (10.0, 12.0, 9.0, 11.0, 1000.0, 5000.0)
    assert good[0][0] == daily_start(date(2026, 10, 1)).isoformat() and len(bad) == 5


class FakeOptionFeed:
    """Stands in for BreezeFeed.option_historical_raw: `book` maps (code, expiry, strike, right) to rows."""
    def __init__(self, book=None, error=None):
        self.book, self.error, self.calls = book or {}, error, []

    def option_historical_raw(self, code, expiry_iso, strike, right, interval, a, b):
        self.calls.append((code, expiry_iso[:10], float(strike), right, interval))
        if self.error:
            return {"Success": None, "Status": 500, "Error": self.error}
        return {"Success": self.book.get((code, expiry_iso[:10], float(strike), right), []), "Status": 200, "Error": None}


def fetcher(feed, max_requests=100, **kw):
    sleeps = []
    f = BreezeOptionFetcher(feed, {"NIFTY": "NIFTY", "RELIANCE": "RELIND"}, 0.7, 5, 1, 3.0, max_requests,
                            sleep=sleeps.append, **kw)
    return f, sleeps


def test_option_history_is_fetched_once_paced_and_cached_even_when_empty():
    feed = FakeOptionFeed({("NIFTY", "2026-10-27", 23000.0, "call"): [hist_row("2026-10-26"), hist_row("2026-10-27", o=-1)]})
    f, sleeps = fetcher(feed)
    store = Store(":memory:")
    h = OptionHistory(store, "breeze", f, now=NOW)
    bars = h.contract("NIFTY", date(2026, 10, 27), 23000, "CE")
    assert list(bars) == [date(2026, 10, 26)] and bars[date(2026, 10, 26)].oi == 5000.0
    assert h.contract("NIFTY", date(2026, 10, 27), 23100, "CE") == {}           # nothing exists (asked twice: one retry)
    assert h.bar("NIFTY", date(2026, 10, 27), 23000, "CE", date(2026, 10, 26)).close == 11.0
    assert h.bar("NIFTY", date(2026, 10, 27), 23100, "CE", date(2026, 10, 26)) is None
    assert len(feed.calls) == 3 and sleeps == [0.7, 3.0]
    assert feed.calls[0] == ("NIFTY", "2026-10-27", 23000.0, "call", "1day")
    # a new object (a later run) reads the store: no request at all, also for the empty contract
    h2 = OptionHistory(store, "breeze", f, now=NOW)
    assert list(h2.contract("NIFTY", date(2026, 10, 27), 23000, "CE")) == [date(2026, 10, 26)]
    assert h2.contract("NIFTY", date(2026, 10, 27), 23100, "CE") == {} and len(feed.calls) == 3
    assert [e["reason"] for e in store.recent_quality_events(5)] == ["BAD_BAR"]     # the bad row was quarantined
    assert h.download_stats()["contracts_downloaded"] == 1 and h.download_stats()["contracts_empty"] == 1


def test_unknown_contract_without_a_fetcher_error_or_budget_is_none_not_empty():
    store = Store(":memory:")
    h = OptionHistory(store, "breeze")
    assert h.contract("NIFTY", date(2026, 10, 27), 23000, "CE") is None and len(h.not_stored) == 1
    f, _ = fetcher(FakeOptionFeed(error="Session expired"))
    h = OptionHistory(store, "breeze", f, now=NOW)
    assert h.contract("NIFTY", date(2026, 10, 27), 23000, "CE") is None and "Session expired" in h.error
    assert h.contract("NIFTY", date(2026, 10, 27), 23100, "CE") is None and f.requests == 1    # stopped after the error
    assert store.option_fetch_log("breeze", "NIFTY") == {}       # an error is never recorded as "no data"
    f, _ = fetcher(FakeOptionFeed(), max_requests=1)
    h = OptionHistory(store, "breeze", f, now=NOW)
    h.contract("NIFTY", date(2026, 10, 27), 23000, "CE", expect_data=False)
    assert h.contract("NIFTY", date(2026, 10, 27), 23100, "CE") is None and f.requests == 1    # budget used up
    with pytest.raises(OptionHistoryError):
        fetcher(FakeOptionFeed())[0].fetch("TCS", date(2026, 10, 27), 100, "CE")


def test_candidate_expiries_and_atm_candidates():
    c = candidate_expiries("monthly", date(2026, 11, 1), 1)
    assert c[:3] == [date(2026, 11, 24), date(2026, 11, 23), date(2026, 11, 20)] and date(2026, 11, 26) in c
    w = candidate_expiries("weekly", date(2026, 11, 9), 3)
    assert w[:3] == [date(2026, 11, 12), date(2026, 11, 11), date(2026, 11, 10)] and len(w) == 5
    assert atm_candidates(24037, 100) == [24000, 24100, 23900]
    assert atm_candidates(1347, None) == [1350, 1345, 1347.5]    # round steps of about 1% of the price, then finer


def test_discover_expiries_finds_the_day_the_contract_last_traded():
    # November 2026: the usual Tuesday (24th) is a holiday, so the contract expired on Monday the 23rd.
    # A contract "expiring" on the 24th exists with one stale bar: it must not be taken for the expiry.
    book = {("NIFTY", "2026-11-24", 24000.0, "call"): [hist_row("2026-11-10")],
            ("NIFTY", "2026-11-23", 24000.0, "call"): [hist_row("2026-11-20"), hist_row("2026-11-23")],
            ("NIFTY", "2026-10-27", 24000.0, "call"): [hist_row("2026-10-26"), hist_row("2026-10-27")]}
    feed = FakeOptionFeed(book)
    f, _ = fetcher(feed)
    store = Store(":memory:")
    h = OptionHistory(store, "breeze", f, now=NOW)
    out = discover_expiries(h, "NIFTY", "monthly", date(2026, 9, 1), date(2026, 11, 30), lambda d: 24010.0, 100)
    assert h.expiries("NIFTY") == [(date(2026, 10, 27), "monthly"), (date(2026, 11, 23), "monthly")]
    assert out["found"] == 2 and out["not_found"] == ["2026-09"] and out["stopped"] is None
    n = len(feed.calls)
    discover_expiries(h, "NIFTY", "monthly", date(2026, 9, 1), date(2026, 11, 30), lambda d: 24010.0, 100)
    assert len(feed.calls) == n                                   # found months and searched months are not asked again


# ---------------- option backtests ----------------
def weekdays(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def world(first=date(2024, 1, 1), last=date(2025, 12, 31), regime="TRENDING_UP", s=None, drift=0.0, vix=None):
    """A store with NIFTY and INDIAVIX daily bars (sim source), a base engine with a fixed regime, and
    model prices. NIFTY wobbles around 24,000; VIX rises slowly, so its IV rank stays at 100."""
    s = s or settings(backtest={"test_start": {"1day": date(2025, 6, 2)}, "holdout_start": {"1day": date(2025, 10, 1)}})
    store = Store(":memory:")
    days = weekdays(first, last)
    bars = []
    for i, d in enumerate(days):
        px = 24000 + 60 * math.sin(i / 5) + drift * i
        bars.append(Bar("NIFTY", daily_start(d), px - 10, px + 45, px - 45, px, None))
        v = vix(i) if vix else 12 + i * 0.01
        bars.append(Bar("INDIAVIX", daily_start(d), v, v + 0.2, v - 0.2, v, None))
    store.save_history_bars("bars_1d", bars, "sim")
    base = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime(regime))
    return s, store, base, model_prices(s, base)


def materialise(store, model, sym, first, last, lo=22500, hi=25500, step=100):
    """Write the model's bars into option_bars, as if they had been downloaded: a stored option history."""
    n = 0
    for exp, kind in model.expiries(sym):
        if not (first <= exp <= last):
            continue
        store.save_option_expiry("sim", sym, exp, kind, NOW)
        days = weekdays(exp - timedelta(days=60), exp)
        for k in range(lo, hi + 1, step):
            for right in ("CE", "PE"):
                rows = []
                for d in days:
                    b = model.bar(sym, exp, k, right, d)
                    if b is not None:
                        rows.append((daily_start(d).isoformat(), b.open, b.high, b.low, b.close, 1000.0, 65000.0))
                n += store.save_option_bars("sim", sym, exp, k, right, "1day", rows, NOW)
    return n


@pytest.fixture(scope="module")
def stored_world():
    s, store, base, model = world()
    materialise(store, model, "NIFTY", date(2025, 6, 1), date(2025, 10, 31))
    return s, store, base, model


def run(s, store, base, prices, sid, lots=None, **kw):
    eng = OptionEngine(s, store, "sim", prices, base, lot_sizes=lots if lots is not None else {"NIFTY": 50})
    return run_option_backtest(s, store, "sim", strat(sid, s), prices, ["NIFTY"], engine=eng, save=False, now=NOW, **kw)


def test_ACCEPTANCE_backtest_uses_stored_option_history_and_a_fixture_bar_changes_the_result(stored_world):
    s, store, base, _ = stored_world
    res = run(s, store, base, OptionHistory(store, "sim"), "credit_spread")
    rep, trades = res["report"], [t for t in res["trades"] if t["variant"] == "strategy"]
    assert rep["price_source"] == "option_history" and rep["payoff_simulation"] is False
    assert "stored Breeze option prices" in rep["price_source_label"]
    assert trades and all(t["price_source"] == "option_history" for t in res["trades"])
    t0 = trades[0]
    short = next(x for x in t0["legs"] if x["action"] == "SELL")
    entry_d = datetime.fromisoformat(t0["entry_time"]).astimezone(IST).date()
    exp = date.fromisoformat(t0["expiry"])
    # change ONE stored bar: the sold leg's open on the entry day is Rs 3 higher
    bars = store.option_bars("sim", "NIFTY")[(exp, short["strike"], "PE")]
    old = next(b for b in bars if b[0] == daily_start(entry_d))
    try:
        store.save_option_bars("sim", "NIFTY", exp, short["strike"], "PE", "1day",
                               [(old[0].isoformat(), old[1] + 3, max(old[2], old[1] + 3), *old[3:])], NOW)
        res2 = run(s, store, base, OptionHistory(store, "sim"), "credit_spread")
        u0 = [t for t in res2["trades"] if t["variant"] == "strategy"][0]
        new_fill = float(money(Decimal(str(old[1] + 3)) * Decimal("0.995")))
        assert next(x for x in u0["legs"] if x["action"] == "SELL")["entry_price"] == new_fill
        assert u0["entry_net"] == pytest.approx(t0["entry_net"] + 3 * 0.995, abs=0.011)
        assert u0["net_pnl"] > t0["net_pnl"] and res2["report"]["overall"]["net_pnl_inr"] != rep["overall"]["net_pnl_inr"]
    finally:
        store.save_option_bars("sim", "NIFTY", exp, short["strike"], "PE", "1day",
                               [(old[0].isoformat(), *old[1:])], NOW)


def test_ACCEPTANCE_every_option_report_has_the_no_filter_baseline_with_the_same_period_costs_and_source(stored_world):
    s, store, base, model = stored_world
    for sid, variants, prices in (("credit_spread", {"baseline:PE", "baseline:CE"}, OptionHistory(store, "sim")),
                                  ("iron_condor", {"baseline"}, model), ("long_option", {"baseline"}, model)):
        res = run(s, store, base, prices, sid)
        rep = res["report"]
        b = rep["baseline"]
        base_trades = [t for t in res["trades"] if t["variant"] != "strategy"]
        assert b["overall"]["trades"] == len(base_trades) > 0, sid
        assert set(b["by_variant"]) == variants == {t["variant"] for t in base_trades}
        assert {t["price_source"] for t in res["trades"]} == {rep["price_source"]}          # never mixed
        lo, hi = date.fromisoformat(rep["period"]["from"]), date.fromisoformat(rep["period"]["to"])
        for t in res["trades"]:                                    # same period for strategy and baseline
            assert lo <= datetime.fromisoformat(t["signal_time"]).astimezone(IST).date() <= hi
            assert t["costs"]["total"] > 0 and t["costs"]["brokerage"] == 49.0 * 2 * len(t["legs"])   # same cost model
            assert t["lots"] == 1 and t["lot_size"] == 50
        assert rep["comparison"]["verdict"] and "baseline_avg_net_per_trade_inr" in rep["comparison"]
        assert rep["cost_label"].startswith("standard rates") and rep["holdout"]["evaluated"] is False


def test_baseline_has_no_entry_rules_but_the_strategy_does(stored_world):
    s, store, _, model = stored_world
    quiet = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))       # credit spread needs a trend
    res = run(s, store, quiet, OptionHistory(store, "sim"), "credit_spread")
    assert res["report"]["overall"]["trades"] == 0 and res["report"]["baseline"]["overall"]["trades"] > 0
    assert res["report"]["comparison"]["verdict"].startswith("no trades")
    # one baseline entry per expiry and side, on the first session inside the entry window
    for v in ("baseline:PE", "baseline:CE"):
        ts = [t for t in res["trades"] if t["variant"] == v]
        assert len({t["expiry"] for t in ts}) == len(ts)
        for t in ts:
            dte = (date.fromisoformat(t["expiry"]) - datetime.fromisoformat(t["signal_time"]).astimezone(IST).date()).days
            assert 20 <= dte <= 35
            assert all(x["right"] == v[-2:] for x in t["legs"])


def test_ACCEPTANCE_model_prices_are_labelled_payoff_simulation():
    s, store, base, model = world()
    rep = run(s, store, base, model, "credit_spread")
    assert rep["report"]["payoff_simulation"] is True and "payoff simulation" in rep["report"]["price_source_label"]
    assert rep["trades"] and {t["price_source"] for t in rep["trades"]} == {"model"}
    assert any("payoff simulation" in n for n in rep["report"]["notes"])


def test_ACCEPTANCE_option_entry_is_the_next_session_open_never_the_signal_day(stored_world):
    s, store, base, _ = stored_world
    hist = OptionHistory(store, "sim")
    res = run(s, store, base, hist, "credit_spread")
    sessions = base.session_dates()
    for t in res["trades"]:
        sig_d = datetime.fromisoformat(t["signal_time"]).astimezone(IST).date()
        ent_d = datetime.fromisoformat(t["entry_time"]).astimezone(IST).date()
        assert ent_d == sessions[sessions.index(sig_d) + 1]
        exp = date.fromisoformat(t["expiry"])
        for x in t["legs"]:
            bar = hist.bar("NIFTY", exp, x["strike"], x["right"], ent_d)
            slip = Decimal("0.995") if x["action"] == "SELL" else Decimal("1.005")
            assert x["entry_price"] == float(money(Decimal(str(bar.open)) * slip))      # that day's open, with slippage
        assert datetime.fromisoformat(t["exit_time"]) > datetime.fromisoformat(t["entry_time"])
        assert date.fromisoformat(t["expiry"]) > datetime.fromisoformat(t["exit_time"]).astimezone(IST).date() or \
            t["exit_reason"] in ("EXPIRY_EXIT",)


def test_ACCEPTANCE_a_spread_trade_reconciles_to_the_paise(stored_world):
    """Recomputed independently from the stored bars: fills = open x (1 -/+ 0.5%) to the paise; gross =
    (credit in - debit out) x 50; charges per leg at the test rates (brokerage 49 per lot per order,
    STT 0.15% of premium sold, exchange 0.03553% and SEBI 0.0001% of both sides, stamp 0.003% of buys,
    IPFT 0.0000001%, GST 18% of brokerage + exchange + SEBI + IPFT)."""
    s, store, base, _ = stored_world
    hist = OptionHistory(store, "sim")
    res = run(s, store, base, hist, "credit_spread")
    t = next(x for x in res["trades"] if x["variant"] == "strategy" and x["exit_reason"] in ("TARGET", "STOP", "TIME"))
    exp = date.fromisoformat(t["expiry"])
    ent = datetime.fromisoformat(t["entry_time"]).astimezone(IST).date()
    ext = datetime.fromisoformat(t["exit_time"]).astimezone(IST).date()
    D, q = Decimal, Decimal(50)

    def paise(x):
        return x.quantize(D("0.01"), rounding="ROUND_HALF_UP")

    gross, charges = D(0), D(0)
    for x in t["legs"]:
        o_in = D(str(hist.bar("NIFTY", exp, x["strike"], x["right"], ent).open))
        o_out = D(str(hist.bar("NIFTY", exp, x["strike"], x["right"], ext).open))
        if x["action"] == "SELL":
            sell_v, buy_v = paise(paise(o_in * D("0.995")) * q), paise(paise(o_out * D("1.005")) * q)
        else:
            buy_v, sell_v = paise(paise(o_in * D("1.005")) * q), paise(paise(o_out * D("0.995")) * q)
        gross += sell_v - buy_v
        brokerage = D("98.00")
        stt = paise(sell_v * D("0.0015"))
        exch = paise(buy_v * D("0.0003553")) + paise(sell_v * D("0.0003553"))
        sebi = paise(buy_v * D("0.000001")) + paise(sell_v * D("0.000001"))
        ipft = paise(buy_v * D("0.000000001")) + paise(sell_v * D("0.000000001"))
        stamp = paise(buy_v * D("0.00003"))
        gst = paise((brokerage + exch + sebi + ipft) * D("0.18"))
        charges += brokerage + stt + exch + sebi + ipft + stamp + gst
    assert D(str(t["gross_pnl"])) == gross
    assert D(str(t["costs"]["total"])) == charges
    assert D(str(t["net_pnl"])) == gross - charges


def test_ACCEPTANCE_a_trade_crossing_a_missing_option_bar_is_dropped_not_given_an_invented_exit():
    s, store, base, model = world()
    materialise(store, model, "NIFTY", date(2025, 6, 1), date(2025, 8, 31))
    res = run(s, store, base, OptionHistory(store, "sim"), "credit_spread")
    t = next(x for x in res["trades"] if x["variant"] == "strategy")
    ent = datetime.fromisoformat(t["entry_time"]).astimezone(IST).date()
    ext = datetime.fromisoformat(t["exit_time"]).astimezone(IST).date()
    assert (ext - ent).days >= 1
    hedge = next(x for x in t["legs"] if x["action"] == "BUY")
    store._db.execute("DELETE FROM option_bars WHERE underlying='NIFTY' AND expiry=? AND strike=? AND right='PE' AND start_utc=?",
                      (t["expiry"], hedge["strike"], daily_start(ent + timedelta(days=0)).isoformat()))
    res2 = run(s, store, base, OptionHistory(store, "sim"), "credit_spread")
    assert res2["report"]["skipped_signals"].get("NO_SIGNAL:NO_STRIKE", 0) >= 0
    ids = {x["signal_id"] for x in res2["trades"]}
    assert t["signal_id"] not in ids                              # no entry without a bar for every leg
    assert res2["report"]["data_exclusions"]["NO_OPTION_BAR_AT_ENTRY"] >= 1
    # now a bar missing in the middle of a holding period
    s2, store2, base2, model2 = world()
    materialise(store2, model2, "NIFTY", date(2025, 6, 1), date(2025, 8, 31))
    r1 = run(s2, store2, base2, OptionHistory(store2, "sim"), "credit_spread")
    held = next(x for x in r1["trades"] if x["variant"] == "strategy" and
                (datetime.fromisoformat(x["exit_time"]) - datetime.fromisoformat(x["entry_time"])).days >= 2)
    ent = datetime.fromisoformat(held["entry_time"]).astimezone(IST).date()
    mid = base2.next_session(ent)
    hedge = next(x for x in held["legs"] if x["action"] == "BUY")
    store2._db.execute("DELETE FROM option_bars WHERE underlying='NIFTY' AND expiry=? AND strike=? AND right='PE' AND start_utc=?",
                       (held["expiry"], hedge["strike"], daily_start(mid).isoformat()))
    r2 = run(s2, store2, base2, OptionHistory(store2, "sim"), "credit_spread")
    assert held["signal_id"] not in {x["signal_id"] for x in r2["trades"]}
    assert r2["report"]["data_exclusions"]["DROPPED_MISSING_OPTION_BAR"] == r1["report"]["data_exclusions"]["DROPPED_MISSING_OPTION_BAR"] + 1


def test_ACCEPTANCE_no_option_backtest_tests_a_session_before_2019_01_22():
    s = settings()                                               # real config: backtest.test_start 1day = 2019-01-22
    assert s.backtest.test_start["1day"] == date(2019, 1, 22)
    s = settings(options={"backtest": {"start": {}}})           # no later start for the condor in this test
    s, store, base, model = world(date(2017, 6, 1), date(2019, 12, 31), s=s)
    for sid in ("long_option", "credit_spread", "iron_condor"):
        res = run(s, store, base, model, sid)
        assert res["trades"], sid
        assert res["report"]["period"]["from"] >= "2019-01-22"
        assert min(datetime.fromisoformat(t["signal_time"]).astimezone(IST).date() for t in res["trades"]) >= date(2019, 1, 22)
    assert min(base.session_dates()) < date(2019, 1, 22)         # older bars exist and are simply not tested


def test_unknown_lot_size_means_no_option_trades(stored_world):
    s, store, base, _ = stored_world
    res = run(s, store, base, OptionHistory(store, "sim"), "credit_spread", lots={})
    assert res["trades"] == [] and res["report"]["lot_sizes"] == {"NIFTY": None}
    assert res["report"]["baseline"]["skipped_signals"]["NO_SIGNAL:LOT_SIZE_UNKNOWN"] > 0


def test_not_stored_contracts_are_counted_as_unknown_not_traded():
    s, store, base, model = world()
    for exp, kind in model.expiries("NIFTY"):
        store.save_option_expiry("sim", "NIFTY", exp, kind, NOW)
    hist = OptionHistory(store, "sim")                           # no bars stored, no fetcher
    res = run(s, store, base, hist, "credit_spread")
    assert res["trades"] == [] and res["report"]["download"]["contracts_wanted_but_not_stored"] > 0


def test_option_backtest_is_deterministic_and_long_option_uses_the_trigger(stored_world):
    s, store, base, model = stored_world
    a, b = run(s, store, base, model, "long_option"), run(s, store, base, model, "long_option")
    assert a["trades"] == b["trades"] and a["report"]["overall"] == b["report"]["overall"]
    for t in a["trades"]:
        assert t["structure"] == "LONG_OPTION" and len(t["legs"]) == 1 and t["legs"][0]["action"] == "BUY"
        if t["variant"] == "strategy":
            assert any("trigger: swing_pullback_v2" in w for w in t["why_now"])
    months = [t["signal_time"][:7] for t in a["trades"] if t["variant"] == "baseline"]
    assert len(months) == len(set(months))                       # baseline: one at-the-money call per month


def test_history_quotes_have_no_bid_ask_and_no_chain_wide_oi(stored_world):
    s, store, base, _ = stored_world
    hq = HistoryQuotes(OptionHistory(store, "sim"), "NIFTY", date(2025, 7, 1), 24000.0, 100)
    exp = next(e for e, _ in store.option_expiries("sim", "NIFTY") if e > date(2025, 7, 20))
    q = hq.quote(exp, 24000.0, "CE")
    assert q.bid is None and q.ask is None and q.last > 0 and not q.has_bid_ask
    assert q.price(historical=True) == q.last and q.price(historical=False) is None      # live code refuses it
    assert hq.oi_available is False and hq.highest_oi_strike(exp, "PE") is None
    assert hq.quote(exp, 99999.0, "CE") is None


def test_compare_verdicts():
    def s_(trades, net, low=False):
        return {"trades": trades, "net_pnl_inr": net, "low_sample": low, "profit_factor": None}
    assert compare(s_(0, 0, True), s_(10, 100))["verdict"] == "no trades: no evidence"
    assert compare(s_(40, -100), s_(100, -1000))["verdict"] == "no edge vs baseline"          # losing is never an edge
    assert compare(s_(40, 400), s_(100, 2000))["verdict"] == "no edge vs baseline"            # 10 per trade vs 20
    assert compare(s_(40, 4000), s_(100, 2000))["verdict"] == "beats baseline after costs"
    assert compare(s_(5, 4000, True), s_(100, 2000))["verdict"] == "beats baseline after costs (LOW SAMPLE)"


# ---------------- issue 42: daily trades crossing a missing daily bar ----------------
class SwingStub(Strategy):
    id, horizon, interval = "swingstub", "SWING", "1day"
    allowed_regimes = frozenset(REGIMES)

    def __init__(self, fire_on):
        super().__init__(SwingBreakoutParams())
        self.fire_on = fire_on

    def _evaluate(self, ctx):
        from app.strategies.base import next_weekday_open_window
        if ctx.daily[-1].start != daily_start(self.fire_on):
            return []
        c = ctx.daily[-1].close
        return [self.signal(ctx, "LONG", c, c - 50, c + 500, next_weekday_open_window(self.fire_on), ["t"])]


def swing_engine(drop=()):
    s = settings()
    store = Store(":memory:")
    days = weekdays(date(2026, 10, 1), date(2026, 10, 28))
    bars = [Bar("TCS", daily_start(x), 100 + i, 101 + i, 99 + i, 100.5 + i, 1e5) for i, x in enumerate(days)]
    store.save_history_bars("bars_1d", [b for b in bars if b.start.astimezone(IST).date() not in drop], "sim")
    store.save_history_bars("bars_1d", [replace(b, symbol="NIFTY") for b in bars], "sim")
    return Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime()), days


def test_issue_42_swing_trade_crossing_a_missing_daily_bar_is_dropped():
    eng, days = swing_engine()
    skipped = Counter()
    (t,) = eng.run_swing(SwingStub(days[5]), "TCS", "stock", days, skipped, max_hold=4)
    assert t.exit_reason == "TIME" and not skipped
    # the stock has no bar for days[7], a day the index traded, while the trade is open
    eng, _ = swing_engine(drop={days[7]})
    skipped = Counter()
    assert eng.run_swing(SwingStub(days[5]), "TCS", "stock", days, skipped, max_hold=4) == []
    assert skipped == Counter({"DROPPED_MISSING_DAILY_BAR_DURING_TRADE": 1})
    # a missing bar AFTER the exit does not matter
    eng, _ = swing_engine(drop={days[12]})
    assert len(eng.run_swing(SwingStub(days[5]), "TCS", "stock", days, Counter(), max_hold=4)) == 1
    # the entry bar itself missing: the signal is not entered on a later day
    eng, _ = swing_engine(drop={days[6]})
    skipped = Counter()
    assert eng.run_swing(SwingStub(days[5]), "TCS", "stock", days, skipped, max_hold=4) == []
    assert sum(skipped.values()) == 1 and set(skipped) <= {"NO_NEXT_BAR", "NEXT_BAR_NOT_NEXT_SESSION"}
    assert eng.sessions_between(days[5], days[7]) == 1 and eng.sessions_between(days[5], days[6]) == 0


def test_issue_42_is_reported_under_data_exclusions():
    from app.backtest import DATA_EXCLUSION_KEYS, data_exclusions
    assert "DROPPED_MISSING_DAILY_BAR_DURING_TRADE" in DATA_EXCLUSION_KEYS
    x = data_exclusions(Counter({"DROPPED_MISSING_DAILY_BAR_DURING_TRADE": 3}), settings())
    assert x["trades_dropped_missing_daily_bar"] == 3


# ---------------- config ----------------
def test_option_config_is_validated():
    s = settings()
    assert s.option_underlying("NIFTY").expiries == ["weekly", "monthly"]
    assert s.option_underlying("BANKNIFTY").expiries == ["monthly"]
    assert s.option_underlying("RELIANCE").expiries == ["monthly"] and s.option_underlying("INDIAVIX") is None
    raw = s.model_dump()
    for path, value in ((("options", "underlyings"), {"NOPE": {}}), (("options", "iv_reference_symbol"), "NOPE"),
                        (("options", "risk_free_rate_pct"), float("inf")), (("options", "backtest", "trigger"), "nope"),
                        (("strategies", "credit_spread", "max_short_delta"), 0.9),
                        (("strategies", "credit_spread", "entry_min_dte"), 40),
                        (("strategies", "long_option", "stock_exit_days_before_expiry"), 2),
                        (("strategies", "iron_condor", "stop_multiple_of_credit"), 1.0)):
        bad = Settings.model_validate(raw).model_dump()
        node = bad
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
        with pytest.raises(ValueError):
            Settings.model_validate(bad)


# ---------------- desk, routes and page ----------------
def desk_world(tmp_path=None, regime="TRENDING_UP", margin=True):
    s = settings()
    store = Store(":memory:")
    days = weekdays(date(2025, 10, 1), date(2026, 11, 12))
    store.save_history_bars("bars_1d", [Bar("INDIAVIX", daily_start(d), 12, 13, 11, 12 + i * 0.01, None)
                                        for i, d in enumerate(days)], "sim")
    strikes = tuple(float(k) for k in range(22000, 26001, 100))
    book = saved_book(store, [spec(expiry=EXP_W, kind="weekly", strikes=strikes), spec(strikes=strikes)])

    def rows(sp, right):
        return [{"strike_price": q.strike, "best_bid_price": q.bid, "best_offer_price": q.ask, "ltp": q.last,
                 "open_interest": q.oi, "chnge_oi": q.oi_change, "total_quantity_traded": 10, "spot_price": 24000}
                for q in bs_chain(expiry=sp.expiry) if q.right == right]

    ChainLoader(s, store, FakeChainSource(rows), book).load(["NIFTY"], now=NOW)
    if margin:
        store.replace_margins([("NIFTY", "CREDIT_SPREAD", 200.0, 38500.0, "2026-11-12"),
                               ("NIFTY", "IRON_CONDOR", 100.0, 41000.0, "2026-11-12")])
    prices = {"NIFTY": 24000.0, "INDIAVIX": 15.0}
    return s, store, OptionDesk(s, store, "sim", prices.get, lambda: regime, lambda: NOW)


def test_desk_shows_a_live_spread_signal_with_all_figures():
    s, store, desk = desk_world()
    v = desk.view("NIFTY")
    assert v["spot"] == 24000.0 and v["regime"] == "TRENDING_UP" and v["problems"] == []
    assert v["iv"]["rank"] == 100.0 and "India VIX" in v["iv"]["basis"]
    assert [(e["expiry"], e["kind"], e["lot_size"]) for e in v["expiries"]] == [("2026-11-17", "weekly", 65), ("2026-12-08", "monthly", 65)]
    monthly = next(c for c in v["chains"] if c["expiry"] == "2026-12-08")
    assert monthly["stale"] is False and monthly["support_highest_oi_put"] == 23500 and monthly["resistance_highest_oi_call"] == 24500
    assert monthly["put_call_ratio"] is not None and monthly["atm_iv_pct"] == pytest.approx(15, abs=0.5)
    row = next(r for r in monthly["table"] if r["strike"] == 24000)
    assert row["ce"]["delta"] == pytest.approx(0.55, abs=0.05) and row["pe"]["iv_pct"] == pytest.approx(15, abs=0.5)
    ideas = {i["strategy_id"]: i for i in v["ideas"]}
    cs = ideas["credit_spread"]
    assert cs["status"] == "SIGNAL" and cs["alerts"] is False
    d = cs["signals"][0]["details"]
    for key in ("premium", "lot_size", "max_loss_inr", "max_loss_pct_of_capital", "margin_inr", "breakevens", "costs_inr"):
        assert d[key] is not None, key
    assert d["margin_inr"] == 38500.0
    assert ideas["iron_condor"]["status"] == "NO_TRADE" and ideas["iron_condor"]["reasons"][0].startswith("OUTSIDE_REGIME")
    assert ideas["long_option"]["status"] == "NO_TRADE" and "phase 6" in ideas["long_option"]["reasons"][0]
    assert desk.view("NIFTY")["ideas"] == v["ideas"]             # same inputs, same output


def test_desk_no_trade_when_chain_stale_spot_missing_margin_or_list_unknown():
    s, store, desk = desk_world(margin=False)
    idea = {i["strategy_id"]: i for i in desk.view("NIFTY")["ideas"]}["credit_spread"]
    assert idea["status"] == "NO_TRADE" and idea["reasons"][0].startswith("MARGIN_UNKNOWN")
    s, store, desk = desk_world()
    late = NOW + timedelta(seconds=s.options.chain.max_age_seconds + 1)
    v = desk.view("NIFTY", now=late)
    assert all(c["stale"] for c in v["chains"]) and any(p.startswith("DATA_STALE") for p in v["problems"])
    assert all(i["status"] == "NO_TRADE" and i["reasons"][0].startswith("DATA_MISSING") for i in v["ideas"])
    desk.spot_fn = {"INDIAVIX": 15.0}.get                        # NIFTY price not LIVE
    v = desk.view("NIFTY")
    assert v["spot"] is None and all(i["status"] == "NO_TRADE" for i in v["ideas"])
    assert {i["reasons"][0].split(":")[0] for i in v["ideas"]} == {"DATA_STALE"}
    v = desk.view("NIFTY", now=NOW + timedelta(days=30))         # instrument list too old: lot size unknown
    assert any(p.startswith("LOT_SIZE_UNKNOWN") for p in v["problems"])
    assert all(i["reasons"][0].startswith("LOT_SIZE_UNKNOWN") for i in v["ideas"])
    v = desk.view("RELIANCE")                                    # nothing loaded for this stock
    assert any(p.startswith("LOT_SIZE_UNKNOWN") for p in v["problems"]) and v["iv"]["rank"] is None


def test_option_routes_page_and_margin_import(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed
    s = settings(options={"margin": {"csv": str(tmp_path / "margins.csv")}}, history={"sim_seed_on_start": False})
    app = create_app(s, feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app) as c:
        assert app.state.load_instruments() > 0                  # sim: fake contracts around the fake price
        loaded = app.state.load_chains()                         # only NIFTY has a (fake) price in this test
        assert [k for k in loaded if k.startswith("NIFTY:")] and all("error" not in v for k, v in loaded.items() if k.startswith("NIFTY:"))
        idx = c.get("/api/options").json()
        assert idx["underlyings"][:2] == ["NIFTY", "BANKNIFTY"] and "RELIANCE" in idx["underlyings"] and "INDIAVIX" not in idx["underlyings"]
        v = c.get("/api/options/nifty").json()
        assert v["underlying"] == "NIFTY" and v["spot"] == 23500.0 and v["feed"] == "sim"
        assert v["expiries"] and all(e["lot_size"] > 0 for e in v["expiries"])
        assert v["chains"] and v["chains"][0]["rows_stored"] > 0 and v["chains"][0]["stale"] is False
        assert {i["strategy_id"] for i in v["ideas"]} == {"long_option", "credit_spread", "iron_condor"}
        assert all(i["status"] == "NO_TRADE" and i["reasons"] for i in v["ideas"])       # regime UNKNOWN here
        assert c.get("/api/options/INDIAVIX").status_code == 404 and c.get("/api/options/NOPE").status_code == 404
        page = c.get("/options")
        assert page.status_code == 200 and "RESEARCH ONLY" in page.text and "Max loss" in page.text and "Margin" in page.text
        assert "/options" in c.get("/").text and "PAYOFF SIMULATION" in c.get("/backtests").text
        h = {"Host": f"127.0.0.1:{s.server.port}", "Content-Type": "application/json"}
        body = {"text": "underlying,structure,width,margin_per_lot_inr,as_of\nNIFTY,CREDIT_SPREAD,250,38500,2026-11-13\n"}
        r = c.post("/data/import/margins", json=body, headers=h)
        assert r.status_code == 200 and r.json()["rows_saved"] == 1
        assert c.post("/data/import/margins", json={"text": "x,y\n1,2\n"}, headers=h).status_code == 400
        assert c.post("/data/import/margins", json=body, headers={**h, "Host": "evil.example"}).status_code == 403
        assert (tmp_path / "margins.csv").exists()
        assert any(m["width"] == 250 for m in c.get("/api/options/NIFTY").json()["margins"])


def test_breeze_feed_option_calls_are_read_only_data_calls():
    from app.feeds.breeze_feed import BreezeFeed

    class Sdk:
        def __init__(self):
            self.calls = []

        def get_historical_data_v2(self, **kw):
            self.calls.append(("hist", kw))
            return {"Success": [{"close": 1}], "Status": 200, "Error": None}

        def get_option_chain_quotes(self, **kw):
            self.calls.append(("chain", kw))
            return {"Success": None}

    f = BreezeFeed([])
    f._sdk = Sdk()
    assert f.option_historical("NIFTY", "2026-10-27T06:00:00.000Z", 23000.0, "call", "1day", "a", "b") == [{"close": 1}]
    kind, kw = f._sdk.calls[0]
    assert kind == "hist" and kw["product_type"] == "options" and kw["exchange_code"] == "NFO"
    assert kw["strike_price"] == "23000" and kw["right"] == "call" and kw["expiry_date"].startswith("2026-10-27")
    assert f.option_chain("NIFTY", "2026-11-17T06:00:00.000Z", "put") == []
    assert f._sdk.calls[1][1]["right"] == "put"
