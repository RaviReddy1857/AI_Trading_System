"""Codex review of d7581e7..0f908c2 (2026-11-16): negative inputs, NaN / infinity passing limit checks, and the
index spread width search ending on a failure that belongs to one width only. Each test failed before the fix."""
import math
from dataclasses import replace
from decimal import Decimal

import pytest

from app.options.base import OptionQuote, liquidity_problem
from app.options.portfolio import Holding, _components, correlation, portfolio_check
from app.options.sizing import size_spread
from app.risk.engine import decide
from app.risk.limits import correlated_group, loss_stops
from app.risk.portfolio import capital_fit, position_problems
from app.risk.sizing import cash_quantity, lots_within, option_lots
from app.models import RiskDecision
from tests.phase5_fixtures import (SECTORS, ZeroCosts, account, bull_put, cash_signal, leg, market, no_corr, pos,
                                   settings)
from tests.test_phase4 import EXP_M, bs_chain, make_ctx, strat

S = settings()
NAN, INF = float("nan"), float("inf")


def run(sig=None, acct=None, s=S, corr=no_corr, sectors=SECTORS, **kw):
    return decide(sig or cash_signal(), acct or account(), market(), s, ZeroCosts(), corr, sectors, **kw)


# ---------------- 1. negative inputs are invalid and block ----------------
def test_negative_commitment_is_not_netted_against_the_pool():
    """Review case: Rs 3,00,000 and -Rs 2,00,000 committed, a new Rs 20,000 position. Summed, the -2,00,000
    would hide 3,00,000 committed (above the whole 2,00,000 capital) and the trade would pass the pool:
    it must be NO_TRADE because a negative commitment is invalid."""
    held = (pos("INFY", committed=300000.0, pid="a"), pos("TCS", committed=-200000.0, pid="b"))
    d = run(acct=account(positions=held), corr=lambda a, b: 0.1)
    assert d.status == "NO_TRADE" and "PORTFOLIO_UNKNOWN" in d.reason_codes
    assert any("committed_inr is negative" in r for r in d.reasons)


@pytest.mark.parametrize("field", ["risk_inr", "committed_inr"])
@pytest.mark.parametrize("value", [-0.01, -1000.0])
def test_negative_risk_or_commitment_is_a_problem(field, value):
    p = replace(pos("INFY"), **{field: value})
    assert position_problems([p]) and run(acct=account(positions=(p,))).status == "NO_TRADE"


def test_zero_risk_and_commitment_are_valid():
    assert position_problems([pos("INFY", risk=0.0, committed=0.0)]) == []


@pytest.mark.parametrize("capital", [-200000.0, 0.0, NAN, INF])
def test_negative_or_unknown_capital_blocks(capital):
    assert "CAPITAL_UNKNOWN" in run(acct=account(capital_inr=capital)).reason_codes


@pytest.mark.parametrize("lots", [0, -1])
def test_non_positive_leg_lots_block(lots):
    """A bought option with 0 or -1 lots (a sold leg like that is already refused by the Signal model)."""
    from tests.phase5_fixtures import option_signal
    sig = option_signal([leg("BUY", "CE", 24000, 100.0, lots)], 100.0, 50.0, 200.0, direction="LONG")
    d = run(sig, acct=account(capital_inr=1000000.0))
    assert d.status == "NO_TRADE" and "DATA_MISSING" in d.reason_codes


def test_phase9_portfolio_negative_or_nan_risk_is_unknown():
    cfg = S.stock_option_selling.portfolio
    hold = [Holding("ONGC", -50000.0)]
    assert portfolio_check("RELIANCE", 2000.0, hold, SECTORS, lambda a, b: 0.1, cfg, 1e6, True)[0] == "PORTFOLIO_UNKNOWN"
    assert portfolio_check("RELIANCE", NAN, [], SECTORS, lambda a, b: 0.1, cfg, 1e6, True)[0] == "PORTFOLIO_UNKNOWN"
    assert portfolio_check("RELIANCE", -1.0, [], SECTORS, lambda a, b: 0.1, cfg, 1e6, True)[0] == "PORTFOLIO_UNKNOWN"


def test_capital_fit_and_sizing_refuse_negative_or_non_finite():
    for bad in (dict(capital=-1.0), dict(committed=-1.0), dict(new_inr=-1.0), dict(new_inr=NAN), dict(capital=INF)):
        args = {"capital": 200000.0, "committed": 0.0, "new_inr": 1000.0, **bad}
        with pytest.raises(ValueError):
            capital_fit(args["capital"], args["committed"], args["new_inr"], 25.0)
    for bad in ((NAN, 1.0, 25.0, 100.0, 90.0), (200000.0, 1.0, 25.0, NAN, 90.0), (-200000.0, 1.0, 25.0, 100.0, 90.0)):
        with pytest.raises(ValueError):
            cash_quantity(*bad)
    with pytest.raises(ValueError):
        cash_quantity(200000.0, 1.0, 25.0, 100.0, 90.0, held_risk_inr=NAN)


# ---------------- 2. NaN and infinity are unknown, never a pass ----------------
@pytest.mark.parametrize("c", [NAN, INF, -INF])
def test_non_finite_correlation_is_unknown_in_phase5(c):
    """NaN > 0.8 is False: before the fix a NaN correlation silently meant 'not correlated'."""
    code, _, _, _ = correlated_group("RELIANCE", [pos("ONGC")], lambda a, b: c, 0.8)
    assert code == "CORRELATION_UNKNOWN"
    d = run(acct=account(positions=(pos("ONGC"),)), corr=lambda a, b: c, sectors={**SECTORS, "ONGC": "Other"})
    assert "CORRELATION_UNKNOWN" in d.reason_codes


@pytest.mark.parametrize("c", [NAN, INF])
def test_non_finite_correlation_is_unknown_in_phase9(c):
    groups, problems = _components(["A", "B"], {"A": "x", "B": "y"}, lambda a, b: c, 0.7)
    assert problems and [len(g) for g in groups] == [1, 1]
    cfg = S.stock_option_selling.portfolio
    code, _, _ = portfolio_check("A", 1000.0, [Holding("B", 1000.0)], {"A": "x", "B": "y"}, lambda a, b: c, cfg, 1e6,
                                 True)
    assert code == "CORRELATION_UNKNOWN"


def test_pearson_never_returns_nan():
    from datetime import date, timedelta
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
    a = {d: (NAN if i == 3 else 0.01 * (i % 7 - 3)) for i, d in enumerate(days)}
    b = {d: 0.01 * (i % 5 - 2) for i, d in enumerate(days)}
    assert correlation(a, b, days, 50) is None


@pytest.mark.parametrize("value", [NAN, INF, -1.0])
def test_loss_stops_capital_not_finite_or_negative(value):
    got, _ = loss_stops(value, 0.0, 0.0, [], 2.0, 6.0)
    assert [c for c, _ in got] == ["PORTFOLIO_UNKNOWN"]


def test_risk_decision_refuses_non_finite_figures():
    with pytest.raises(ValueError):
        RiskDecision("APPROVED", (), 10, None, NAN, 1.0, None, None, None, None, None)
    with pytest.raises(ValueError):
        RiskDecision("APPROVED", (), 10, None, INF, 1.0, None, None, None, None, None)


def test_lots_within_and_option_lots_refuse_non_finite():
    with pytest.raises(ValueError):
        lots_within(NAN, lambda n: Decimal(100 * n))
    with pytest.raises(ValueError):
        lots_within(1000.0, lambda n: Decimal("NaN"))
    with pytest.raises(ValueError):
        option_lots([leg("BUY", "CE", 24000, 100.0)], ZeroCosts(), INF)


def test_phase9_size_spread_non_finite_budget_is_zero_lots():
    one = [leg("SELL", "PE", 24000, 60.0), leg("BUY", "PE", 23900, 30.0)]
    assert size_spread("credit", one, 75, ZeroCosts(), NAN)[0] == 0
    assert size_spread("credit", one, 75, ZeroCosts(), INF)[0] == 0


@pytest.mark.parametrize("oi", [NAN, INF])
def test_non_finite_open_interest_is_unknown(oi):
    """NaN < minimum is False: before the fix a NaN open interest passed the liquidity filter."""
    q = OptionQuote(24000.0, "PE", 10.0, 10.2, 10.1, oi, None, None)
    ctx = make_ctx(min_oi_lots=100)
    assert "open interest unknown" in (liquidity_problem(ctx, q, 65) or "")


@pytest.mark.parametrize("sid", ["credit_spread", "long_option"])
@pytest.mark.parametrize("rank", [NAN, INF])
def test_non_finite_iv_rank_is_unknown(sid, rank):
    from tests.test_phase4 import trigger_signal
    st = strat(sid)
    st.evaluate(make_ctx(iv_rank=rank, trigger=trigger_signal() if sid == "long_option" else None))
    assert any(r.startswith("IV_RANK_UNKNOWN") for r in st.last_no_trade), st.last_no_trade


@pytest.mark.parametrize("m", [NAN, INF, 0.0, -100.0])
def test_non_finite_or_non_positive_margin_entry_is_unknown(m):
    cs = strat("credit_spread")
    assert cs.evaluate(make_ctx(margin=lambda structure, width: m)) == []
    assert any(r.startswith("MARGIN_UNKNOWN") for r in cs.last_no_trade), cs.last_no_trade


@pytest.mark.parametrize("budget", [NAN, INF])
def test_non_finite_width_search_budget_is_no_trade(budget):
    cs = strat("credit_spread")
    assert cs.evaluate(make_ctx(option_risk_budget_inr=budget)) == []
    assert cs.last_no_trade and cs.last_no_trade[-1].startswith("ZERO_SIZE")


@pytest.mark.parametrize("rank", [NAN, INF])
def test_stock_selling_non_finite_iv_rank_is_unknown(rank, monkeypatch):
    """The trend rule is made to pass so the IV-rank rule is the one decided (NaN < 50 is False: it passed)."""
    import app.options.stock_selling as ss
    monkeypatch.setattr(ss, "trend_of", lambda *a, **k: ("UP", None, "up trend (fixture)"))
    st = ss.build_stock_selling(S)
    assert st.entry_checks(make_ctx(underlying="RELIANCE", kind="stock", iv_rank=rank)) is False
    assert st.last_no_trade[-1].startswith("IV_RANK_UNKNOWN")
    assert st.entry_checks(make_ctx(underlying="RELIANCE", kind="stock", iv_rank=70.0)) is True


# ---------------- 3. width search: a failure of one width moves on to the next narrower ----------------
STEP50 = bs_chain(24000.0, EXP_M, step=50)


def chain_with(fn):
    return {EXP_M: [fn(q) for q in STEP50]}


def sold_strike():
    return strat("credit_spread").evaluate(make_ctx(chains={EXP_M: STEP50}))[0].legs[0].strike


def width(sig):
    return abs(sig.legs[0].strike - sig.legs[1].strike)


def test_illiquid_configured_hedge_moves_to_the_next_width():
    """Review case: the 250-point hedge has zero open interest. Before the fix the evaluation ended there
    (ILLIQUID); now 200 points is tried and used, and every rule still applies to it."""
    ks = sold_strike()
    zero = chain_with(lambda q: replace(q, oi=0.0) if (q.right == "PE" and q.strike == ks - 250) else q)
    cs = strat("credit_spread")
    out = cs.evaluate(make_ctx(chains=zero, option_risk_budget_inr=1e9))
    assert out and width(out[0]) == 200, cs.last_no_trade
    assert any("250: ILLIQUID" in w for w in out[0].why_now)
    assert liquidity_problem(make_ctx(chains=zero), make_ctx(chains=zero).quotes.quote(EXP_M, ks - 200, "PE"), 65) is None


def test_margin_only_for_100_points_moves_down_to_100():
    """Review case: margin is entered only for the 100-point width. Before the fix: MARGIN_UNKNOWN at 250 and no
    signal; now 250, 200 and 150 fail on margin and 100 is used, with its own margin on the card."""
    cs = strat("credit_spread")
    ctx = make_ctx(chains={EXP_M: STEP50}, option_risk_budget_inr=1e9,
                   margin=lambda structure, w: 39000.0 if w == 100 else None)
    out = cs.evaluate(ctx)
    assert out and width(out[0]) == 100, cs.last_no_trade
    assert out[0].details["margin_per_lot_inr"] == 39000.0
    note = next(w for w in out[0].why_now if "narrowed" in w)
    assert all(f"{w}: MARGIN_UNKNOWN" in note for w in (250, 200, 150))


def test_no_width_passes_lists_every_width_and_why():
    ks = sold_strike()
    illiquid_250 = chain_with(lambda q: replace(q, oi=0.0) if (q.right == "PE" and q.strike == ks - 250) else q)
    cs = strat("credit_spread")
    ctx = make_ctx(chains=illiquid_250, option_risk_budget_inr=1e9,
                   margin=lambda structure, w: 39000.0 if w == 250 else None)
    assert cs.evaluate(ctx) == []
    text = cs.last_no_trade[-1]
    assert text.startswith("ILLIQUID") and "no width from 250 down to 100 points passes every rule" in text
    for w, why in ((250, "ILLIQUID"), (200, "MARGIN_UNKNOWN"), (150, "MARGIN_UNKNOWN"), (100, "MARGIN_UNKNOWN")):
        assert f"{w}: {why}" in text
    assert len(cs.last_no_trade) == 1                       # one NO_TRADE record, not one per width


def test_sold_leg_failures_still_end_the_search():
    """A failure of the SOLD option is the same at every width: no width is tried."""
    ks = sold_strike()
    sold_illiquid = chain_with(lambda q: replace(q, oi=0.0) if (q.right == "PE" and q.strike == ks) else q)
    cs = strat("credit_spread")
    assert cs.evaluate(make_ctx(chains=sold_illiquid, option_risk_budget_inr=1e9)) == []
    assert len(cs.last_no_trade) == 1 and cs.last_no_trade[0].startswith(("ILLIQUID", "NO_STRIKE"))
    assert "width" not in cs.last_no_trade[0]


def test_iron_condor_moves_past_an_illiquid_call_hedge():
    from tests.test_phase5_width import condor_settings
    from tests.test_phase4 import EXP_W
    chain_w = bs_chain(24000.0, EXP_W, step=50)
    ic = strat("iron_condor", condor_settings())
    base = ic.evaluate(make_ctx(chains={EXP_W: chain_w, EXP_M: STEP50}, regime="RANGE", option_risk_budget_inr=1e9))[0]
    call_sold = next(x for x in base.legs if x.action == "SELL" and x.right == "CE").strike
    bad = [replace(q, oi=0.0) if (q.right == "CE" and q.strike == call_sold + 250) else q for q in chain_w]
    out = ic.evaluate(make_ctx(chains={EXP_W: bad, EXP_M: STEP50}, regime="RANGE", option_risk_budget_inr=1e9))
    assert out, ic.last_no_trade
    widths = {x.right: abs(x.strike - next(y.strike for y in out[0].legs if y.right == x.right and y.action != x.action))
              for x in out[0].legs if x.action == "SELL"}
    assert widths == {"PE": 200, "CE": 200}
