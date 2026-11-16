"""Phase 5 risk engine (P5.7, step 8 and the acceptance checks): APPROVED with quantity and rupee risk,
or NO_TRADE with a reason code; unknown inputs always NO_TRADE; no path raises risk above the configured
percentages; changing capital rescales everything."""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.costs import CostModel
from app.models import RiskDecision
from app.risk.engine import REASON_CODES, decide
from app.risk.portfolio import OpenPosition
from tests.phase5_fixtures import (GOOD_ACCOUNT, GOOD_MARKET, NOW, SECTORS, FlatCosts, ZeroCosts, account,
                                   bull_put, cash_signal, leg, long_call, market, no_corr, option_signal, pos,
                                   settings)

S = settings()
D1, D2 = date(2026, 11, 12), date(2026, 11, 13)


def run(sig=None, acct=GOOD_ACCOUNT, mkt=GOOD_MARKET, s=S, costs=None, corr=no_corr, sectors=SECTORS, **kw):
    return decide(sig or cash_signal(), acct, mkt, s, ZeroCosts() if costs is None else costs, corr, sectors, **kw)


# ---------------- approvals with hand calculations ----------------
def test_cash_approved_hand_calculation():
    """Capital 2,00,000, 1% = 2,000 budget; entry 100, stop 90: 2,000 / 10 = 200 shares; position value
    20,000 (cap 50,000); target 120: profit 200 x 20 = 4,000; no costs in this fixture."""
    d = run()
    assert d.status == "APPROVED" and d.reason_codes == ()
    assert (d.quantity, d.lots, d.risk_inr, d.risk_pct) == (200, None, 2000.0, 1.0)
    assert (d.est_profit_inr, d.est_loss_inr, d.costs_inr, d.margin_inr) == (4000.0, 2000.0, 0.0, 20000.0)
    assert d.capital_fit == "FITS" and d.size_factor == 1.0


def test_cash_short_approved():
    d = run(cash_signal(entry=100.0, stop=110.0, target=80.0, direction="SHORT"))
    assert d.status == "APPROVED" and d.quantity == 200 and d.est_profit_inr == 4000.0


def test_credit_spread_approved_hand_calculation():
    """Option budget 2.5% of 2,00,000 = 5,000. One lot: (100 - 30) x 75 = 5,250 > 5,000 -> ZERO_SIZE.
    At 4,20,000 capital the budget is 10,500 = 2 lots exactly: risk 10,500, margin 2 x 40,000 = 80,000,
    profit at target (30 - 15) x 150 = 2,250."""
    assert run(bull_put()).reason_codes == ("ZERO_SIZE",)
    d = run(bull_put(), acct=account(capital_inr=420000.0))
    assert d.status == "APPROVED"
    assert (d.lots, d.quantity, d.risk_inr, d.margin_inr, d.est_profit_inr, d.est_loss_inr) == \
        (2, 150, 10500.0, 80000.0, 2250.0, 10500.0)
    assert d.risk_pct == 2.5


def test_long_option_approved_with_costs():
    """Capital 10,00,000: budget 25,000. One lot 100 x 75 + Rs 40 costs = 7,540; 3 lots = 22,620.
    Loss at the stop (50): (100 - 50) x 225 + 120 = 11,370; profit at the target (200): 100 x 225 - 120 = 22,380.
    Premium paid (cash needed) 22,500."""
    d = run(long_call(), acct=account(capital_inr=1000000.0), costs=FlatCosts())
    assert d.status == "APPROVED"
    assert (d.lots, d.risk_inr, d.est_loss_inr, d.est_profit_inr, d.costs_inr, d.margin_inr) == \
        (3, 22620.0, 11370.0, 22380.0, 120.0, 22500.0)


def test_real_cost_model_keeps_risk_within_budget():
    """With the config.yaml rates the cash quantity drops below the 200 of the formula, and the risk
    including every charge and slippage stays within 2,000."""
    d = run(costs=CostModel(S.costs))
    assert d.status == "APPROVED" and d.quantity < 200 and d.costs_inr > 0
    assert d.risk_inr <= 2000.0 and d.risk_inr == pytest.approx(d.quantity * 10 + d.costs_inr, abs=0.01)


# ---------------- every reason code ----------------
def cash_pos(symbol, **kw):
    return pos(symbol, **kw)


CODE_CASES = [
    ("capital unknown", dict(acct=account(capital_inr=None)), "CAPITAL_UNKNOWN"),
    ("capital NaN", dict(acct=account(capital_inr=float("nan"))), "CAPITAL_UNKNOWN"),
    ("capital zero", dict(acct=account(capital_inr=0.0)), "CAPITAL_UNKNOWN"),
    ("positions unknown", dict(acct=account(positions=None)), "PORTFOLIO_UNKNOWN"),
    ("a position's risk unknown", dict(acct=account(positions=(pos("INFY", risk=None),))), "PORTFOLIO_UNKNOWN"),
    ("a position's margin unknown", dict(acct=account(positions=(pos("INFY", committed=None),))), "PORTFOLIO_UNKNOWN"),
    ("open P&L unknown", dict(acct=account(positions=(pos("INFY", today=None),))), "PORTFOLIO_UNKNOWN"),
    ("realised today unknown", dict(acct=account(realised_today_inr=None)), "PORTFOLIO_UNKNOWN"),
    ("realised month unknown", dict(acct=account(realised_month_inr=None)), "PORTFOLIO_UNKNOWN"),
    ("equity history unknown", dict(acct=account(equity_curve=None)), "PORTFOLIO_UNKNOWN"),
    ("deposit amount unknown", dict(acct=account(flows=((D2, float("nan")),))), "PORTFOLIO_UNKNOWN"),
    ("price stale", dict(mkt=market(price_status="STALE")), "DATA_STALE"),
    ("price unknown", dict(mkt=market(price_status=None)), "DATA_STALE"),
    ("decision time unknown", dict(mkt=market(now=None)), "DATA_MISSING"),
    ("signal expired (at valid_until)", dict(mkt=market(now=cash_signal().valid_until)), "SIGNAL_EXPIRED"),
    ("regime not allowed", dict(mkt=market(regime="RANGE"), allowed_regimes=frozenset({"TRENDING_UP"})),
     "OUTSIDE_REGIME"),
    ("regime unknown", dict(mkt=market(regime=None), allowed_regimes=frozenset({"TRENDING_UP"})), "OUTSIDE_REGIME"),
    ("index stale", dict(mkt=market(index_status="STALE")), "KILL_SWITCH"),
    ("index status unknown", dict(mkt=market(index_status=None)), "KILL_SWITCH"),
    ("outside market hours", dict(mkt=market(market_open=False)), "KILL_SWITCH"),
    ("market hours unknown", dict(mkt=market(market_open=None)), "KILL_SWITCH"),
    ("quarantine spike", dict(mkt=market(quarantined_recent=20)), "KILL_SWITCH"),
    ("quarantine count unknown", dict(mkt=market(quarantined_recent=None)), "KILL_SWITCH"),
    ("cost rates unknown", dict(costs=False), "DATA_MISSING"),
    ("sector unknown", dict(sectors={}), "DATA_MISSING"),
    ("correlation unknown", dict(acct=account(positions=(pos("ONGC"),)), sectors={**SECTORS, "ONGC": "Other"}),
     "CORRELATION_UNKNOWN"),
    ("daily stop", dict(acct=account(realised_today_inr=-4000.0)), "DAILY_STOP"),
    ("monthly pause", dict(acct=account(realised_month_inr=-12000.0)), "MONTHLY_PAUSE"),
    ("drawdown pause", dict(acct=account(equity_curve=((D1, 200000.0), (D2, 170000.0)))), "DRAWDOWN_PAUSE"),
    ("zero size", dict(acct=account(capital_inr=500.0)), "ZERO_SIZE"),
    ("capital pool used", dict(acct=account(positions=(pos("INFY", committed=140000.0),)),
                               corr=lambda a, b: 0.1), "RISK_BUDGET_USED"),
    ("third trade in the sector", dict(acct=account(positions=(pos("ONGC"), pos("BPCL"))), corr=lambda a, b: 0.1),
     "SECTOR_LIMIT"),
    ("correlated budget used", dict(acct=account(positions=(pos("INFY", risk=2000.0),)), corr=lambda a, b: 0.9),
     "CORRELATED_LIMIT"),
]


@pytest.mark.parametrize("name, kw, code", CODE_CASES, ids=[c[0] for c in CODE_CASES])
def test_reason_codes(name, kw, code):
    kw = dict(kw)
    if kw.get("costs") is False:
        kw["costs"] = None
        d = decide(cash_signal(), kw.get("acct", GOOD_ACCOUNT), kw.get("mkt", GOOD_MARKET), S, None, no_corr, SECTORS)
    else:
        d = run(**kw)
    assert d.status == "NO_TRADE" and code in d.reason_codes, (d.reason_codes, d.reasons)
    assert d.quantity is None and d.lots is None and d.risk_inr is None
    assert all(c in REASON_CODES for c in d.reason_codes)
    assert all(any(r.startswith(c + ":") for r in d.reasons) for c in d.reason_codes)


def test_margin_unknown_blocks_spread():
    d = run(bull_put(margin_per_lot=None), acct=account(capital_inr=420000.0))
    assert d.status == "NO_TRADE" and d.reason_codes == ("MARGIN_UNKNOWN",)


def test_lot_size_unknown_blocks():
    d = run(option_signal([leg("BUY", "CE", 24000, 100.0, 1, 0)], 100.0, 50.0, 200.0, direction="LONG"),
            acct=account(capital_inr=1000000.0))
    assert d.reason_codes == ("LOT_SIZE_UNKNOWN",)


def test_unhedged_short_blocks():
    """The Signal model already refuses a naked short; the engine checks again for anything else passed in."""
    good = bull_put()
    naked = SimpleNamespace(**{k: getattr(good, k) for k in good.__dataclass_fields__})
    naked.legs = (good.legs[0],)
    d = decide(naked, account(capital_inr=420000.0), GOOD_MARKET, S, ZeroCosts(), no_corr, SECTORS)
    assert d.reason_codes == ("UNHEDGED_SHORT",)


def test_max_open_selling_spreads_blocks():
    held = (pos("BANKNIFTY", kind="INDEX_SELLING_SPREAD", pid="a"), pos("BANKNIFTY", kind="INDEX_SELLING_SPREAD", pid="b"))
    sectors = {**SECTORS, "BANKNIFTY": "Index B"}
    d = run(bull_put(), acct=account(capital_inr=420000.0, positions=held), sectors=sectors, corr=lambda a, b: 0.1)
    assert d.reason_codes == ("RISK_BUDGET_USED",) and "max_open_selling_spreads" in d.reasons[0]
    d = run(bull_put(), acct=account(capital_inr=420000.0, positions=held[:1]), sectors=sectors, corr=lambda a, b: 0.1)
    assert d.status == "APPROVED"


def test_all_codes_are_reported_together():
    d = run(acct=account(realised_today_inr=-5000.0, positions=None), mkt=market(price_status="STALE", market_open=False))
    # positions unknown: the loss stop cannot be evaluated, so PORTFOLIO_UNKNOWN stands for it
    assert set(d.reason_codes) == {"PORTFOLIO_UNKNOWN", "DATA_STALE", "KILL_SWITCH"}
    d = run(acct=account(realised_today_inr=-5000.0), mkt=market(price_status="STALE", market_open=False))
    assert set(d.reason_codes) == {"DAILY_STOP", "DATA_STALE", "KILL_SWITCH"}


def test_no_trade_decision_needs_a_code():
    with pytest.raises(ValueError):
        RiskDecision("NO_TRADE", (), None, None, None, None, None, None, None, None, None)
    with pytest.raises(ValueError):
        RiskDecision("APPROVED", (), 0, None, 100.0, 1.0, None, None, None, None, None)


# ---------------- correlated positions count as one ----------------
@pytest.mark.parametrize("held_risk, qty, code", [
    (1000.0, 100, None),           # 2,000 - 1,000 -> 100 shares: the group's risk 2,000 = budget (at)
    (1000.01, 99, None),           # remaining 999.99 -> 99 shares
    (2000.0, None, "CORRELATED_LIMIT"),
])
def test_correlated_group_budget(held_risk, qty, code):
    held = (pos("ONGC", risk=held_risk, committed=1000.0),)
    sectors = {**SECTORS, "ONGC": "Other"}
    d = run(acct=account(positions=held), corr=lambda a, b: 0.85, sectors=sectors)
    assert d.quantity == qty and (code in d.reason_codes if code else d.status == "APPROVED")
    d = run(acct=account(positions=held), corr=lambda a, b: 0.8, sectors=sectors)    # 0.8 is not above 0.8
    assert d.status == "APPROVED" and d.quantity == 200


# ---------------- drawdown halves sizes ----------------
@pytest.mark.parametrize("equity, qty", [(180020.0, 200), (180000.0, 100), (179980.0, 100)])
def test_drawdown_halves_quantity(equity, qty):
    """Peak 2,00,000. 9.99% -> 200 shares; 10% -> budget 1,000 -> 100 shares; 10.01% -> 100."""
    d = run(acct=account(equity_curve=((D1, 200000.0), (D2, equity))))
    assert d.status == "APPROVED" and d.quantity == qty
    assert (d.size_factor == 0.5) is (qty == 100)
    assert any(w.startswith("SIZE_HALVED") for w in d.warnings) is (qty == 100)


def test_drawdown_halves_lots():
    """Capital 4,20,000, budget 10,500 -> 2 lots; at a 10% drawdown 5,250 -> 1 lot."""
    a = account(capital_inr=420000.0, equity_curve=((D1, 420000.0), (D2, 378000.0)))
    d = run(bull_put(), acct=a)
    assert d.status == "APPROVED" and d.lots == 1 and d.risk_inr == 5250.0


# ---------------- phase 9 stock option spreads: capital advisory ----------------
def stock_spread(margin=230000.0):
    """RELIANCE bull put: sold 1300 PE at 20, bought 1290 PE at 14, lot 500: credit 6, width 10,
    max loss (10 - 6) x 500 = 2,000 a lot. Signalled with 1 lot."""
    return bull_put("RELIANCE", (1300, 20.0), (1290, 14.0), 1, 500, margin, "STOCK_OPTION", "stock_option_selling")


@pytest.mark.parametrize("capital, fit, warning", [
    (200000.0, "NEEDS_MORE_CAPITAL", "shortfall Rs 30,000.00"),
    (300000.0, "TIGHT", "TIGHT_CAPITAL"),
    (1000000.0, "FITS", None),
])
def test_stock_spread_capital_is_advisory(capital, fit, warning):
    d = run(stock_spread(), acct=account(capital_inr=capital))
    assert d.status == "APPROVED" and d.capital_fit == fit          # never hidden for capital
    assert d.lots == 1 and d.risk_inr == 2000.0 and d.margin_inr == 230000.0
    assert (any(warning in w for w in d.warnings) if warning else d.warnings == ())


def test_stock_spread_never_more_lots_than_signalled_and_within_its_budget():
    """Budget 2.5% of 10,00,000 = 25,000 would allow 12 lots; the signal had 1."""
    assert run(stock_spread(), acct=account(capital_inr=1000000.0)).lots == 1
    # capital 79,999.60: budget 1,999.99 < one lot's 2,000 -> ZERO_SIZE (exactly at 80,000: 2,000 -> 1 lot)
    assert run(stock_spread(), acct=account(capital_inr=79999.6)).reason_codes[0] == "ZERO_SIZE"
    assert run(stock_spread(), acct=account(capital_inr=80000.0)).lots == 1


def test_stock_spread_blocks_on_unknown_margin_and_history():
    assert "MARGIN_UNKNOWN" in run(stock_spread(None), acct=account(capital_inr=1000000.0)).reason_codes
    d = run(stock_spread(), acct=account(capital_inr=1000000.0, history_verified=False))
    assert d.reason_codes == ("PORTFOLIO_UNKNOWN",)


def test_stock_spread_phase9_portfolio_limit():
    """Phase 9 group limit: at most 2 positions per correlated group (same sector). Two Oil & Gas spreads
    held -> CORRELATED_LIMIT for a third."""
    held = tuple(OpenPosition(f"p{s}", s, "STOCK_SPREAD", 2000.0, 100000.0, 0.0, 0.0) for s in ("ONGC", "BPCL"))
    d = run(stock_spread(), acct=account(capital_inr=1000000.0, positions=held), corr=lambda a, b: 0.1)
    assert d.reason_codes == ("CORRELATED_LIMIT",)


def test_stock_spread_still_stops_on_kill_switch_and_loss_stop():
    a = account(capital_inr=1000000.0)
    assert "KILL_SWITCH" in run(stock_spread(), acct=a, mkt=market(index_status="STALE")).reason_codes
    assert "DAILY_STOP" in run(stock_spread(), acct=replace(a, realised_today_inr=-20000.0)).reason_codes


# ---------------- step 8: changing capital rescales everything ----------------
def test_capital_change_rescales_every_limit_and_size():
    s2, s4 = settings(200000.0), settings(400000.0)
    l2, l4 = s2.limits_in_rupees(), s4.limits_in_rupees()
    assert all(l4[k] == 2 * l2[k] for k in l2)
    c2 = run(cash_signal(), acct=account(capital_inr=200000.0), s=s2)
    c4 = run(cash_signal(), acct=account(capital_inr=400000.0), s=s4)
    assert (c2.quantity, c4.quantity) == (200, 400) and c4.risk_inr == 2 * c2.risk_inr
    sig = bull_put("NIFTY", (24000, 60.0), (23950, 30.0))       # width 50, credit 30: (50 - 30) x 75 = 1,500 a lot
    o2 = run(sig, acct=account(capital_inr=200000.0), s=s2)
    o4 = run(sig, acct=account(capital_inr=400000.0), s=s4)
    assert (o2.lots, o4.lots) == (3, 6)                         # 5,000 / 1,500 = 3; 10,000 / 1,500 = 6
    # the daily stop moves with capital: a 5,000 loss stops 2,00,000 (4,000) but not 4,00,000 (8,000)
    assert "DAILY_STOP" in run(acct=account(capital_inr=200000.0, realised_today_inr=-5000.0), s=s2).reason_codes
    assert run(acct=account(capital_inr=400000.0, realised_today_inr=-5000.0), s=s4).status == "APPROVED"


def test_phase9_defaults_unchanged():
    p = S.stock_option_selling
    assert (p.max_risk_per_position_pct, p.min_credit_to_max_loss, p.short_delta_max) == (2.5, 0.10, 0.20)
    assert (p.portfolio.max_open_spreads, p.portfolio.max_total_risk_pct, p.portfolio.correlation_threshold,
            p.portfolio.group_max_positions, p.portfolio.group_max_risk_pct) == (6, 10, 0.7, 2, 5)
    assert p.backtest.capital_inr == 1000000


def test_deterministic():
    a = account(positions=(pos("INFY", risk=500.0),))
    assert run(acct=a, corr=lambda x, y: 0.3).to_dict() == run(acct=a, corr=lambda x, y: 0.3).to_dict()


# ---------------- acceptance: no path raises risk above the configured percentages ----------------
@hsettings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(capital=st.floats(10000, 5_000_000), entry=st.floats(1, 5000), dist_pct=st.floats(0.1, 20),
       held_risk=st.floats(0, 50000), held_commit=st.floats(0, 3_000_000), corr=st.floats(-1, 1),
       dd=st.floats(0, 14.9), short=st.booleans(), costs=st.sampled_from(["zero", "flat", "real"]))
def test_cash_risk_never_above_limits(capital, entry, dist_pct, held_risk, held_commit, corr, dd, short, costs):
    stop = entry * (1 + dist_pct / 100) if short else entry * (1 - dist_pct / 100)
    stop = round(stop, 2)
    if stop <= 0 or stop == round(entry, 2):
        return
    entry = round(entry, 2)
    target = round(entry - 2 * (stop - entry), 2) if short else round(entry + 2 * (entry - stop), 2)
    if target <= 0:
        return
    sig = cash_signal(entry=entry, stop=stop, target=target, direction="SHORT" if short else "LONG")
    held = (OpenPosition("h", "ONGC", "CASH", held_risk, held_commit, 0.0, 0.0),)
    a = account(capital_inr=capital, positions=held,
                equity_curve=((D1, 100000.0), (D2, 100000.0 * (1 - dd / 100))))
    cm = {"zero": ZeroCosts(), "flat": FlatCosts(), "real": CostModel(S.costs)}[costs]
    d = decide(sig, a, GOOD_MARKET, S, cm, lambda x, y: corr, {**SECTORS, "ONGC": "Other"})
    factor = 0.5 if dd >= 10 else 1.0
    budget = Decimal(str(capital)) * Decimal("0.01") * Decimal(str(factor))
    if d.status == "APPROVED":
        assert d.size_factor == factor
        own_and_group = Decimal(str(d.risk_inr)) + (Decimal(str(held_risk)) if corr > 0.8 else 0)
        assert own_and_group <= budget + Decimal("0.01")
        assert d.quantity * entry <= capital * 0.25 * factor + 0.01                     # position value cap
        assert Decimal(str(held_commit)) + Decimal(str(d.margin_inr)) <= Decimal(str(capital)) * Decimal("0.75") + Decimal("0.01")
        undoubled = decide(sig, replace(a, equity_curve=((D1, 100000.0),)), GOOD_MARKET, S, cm, lambda x, y: corr,
                           {**SECTORS, "ONGC": "Other"})
        # halving never adds size (the full size may itself be blocked, e.g. by the pool: compare its computed size)
        full = undoubled.quantity if undoubled.status == "APPROVED" else undoubled.facts["would_be"]["quantity"]
        assert full is None or full >= d.quantity
    else:
        assert d.reason_codes


@hsettings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(capital=st.floats(50000, 5_000_000), width=st.sampled_from([50, 100, 200, 300]),
       credit_frac=st.floats(0.05, 0.6), lot=st.sampled_from([15, 25, 35, 65, 75]), dd=st.floats(0, 14.9),
       costs=st.sampled_from(["zero", "flat", "real"]), stock=st.booleans())
def test_option_risk_never_above_limits(capital, width, credit_frac, lot, dd, costs, stock):
    credit = round(width * credit_frac, 2)
    sold = round(credit + 10, 2)
    if stock:
        sig = bull_put("RELIANCE", (1300, sold), (1300 - width, 10.0), 2, lot, 50000.0, "STOCK_OPTION",
                       "stock_option_selling")
        pct = S.stock_option_selling.max_risk_per_position_pct
    else:
        sig = bull_put("NIFTY", (24000, sold), (24000 - width, 10.0), 1, lot, 50000.0)
        pct = S.risk.max_loss_per_option_pct
    a = account(capital_inr=capital, equity_curve=((D1, 100000.0), (D2, 100000.0 * (1 - dd / 100))))
    cm = {"zero": ZeroCosts(), "flat": FlatCosts(), "real": CostModel(S.costs)}[costs]
    d = decide(sig, a, GOOD_MARKET, S, cm, no_corr, SECTORS)
    factor = 0.5 if dd >= 10 else 1.0
    if d.status == "APPROVED":
        assert Decimal(str(d.risk_inr)) <= Decimal(str(capital)) * Decimal(str(pct)) / 100 * Decimal(str(factor)) + Decimal("0.01")
        assert d.lots >= 1 and (not stock or d.lots <= 2)
    else:
        assert d.reason_codes
