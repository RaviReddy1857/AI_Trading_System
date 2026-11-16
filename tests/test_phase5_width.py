"""Phase 5 decision (user, 2026-11-16): the option risk limit is never raised. When one lot of an index spread
at the configured width risks more than the option risk budget (capital x risk.max_loss_per_option_pct), the
narrower listed widths down to `min_hedge_width_points` (100) are tried and the WIDEST that sizes to at least one
lot is used. If none fits: NO TRADE (ZERO_SIZE)."""
from datetime import date
from decimal import Decimal

import pytest

from app.models import utcnow
from app.options.base import index_option_budget
from app.options.credit_spread import build_side, one_lot_loss
from app.risk.engine import decide
from tests.phase5_fixtures import SECTORS, account, market, no_corr
from tests.test_phase4 import EXP_M, EXP_W, bs_chain, desk_world, make_ctx, settings, strat

STEP50 = {EXP_W: bs_chain(24000.0, EXP_W, step=50), EXP_M: bs_chain(24000.0, EXP_M, step=50)}


def condor_settings():
    """Iron condor hedged 1% out (250 points on this chain) so there is room to narrow; config.yaml has 0.5%."""
    s = settings()
    raw = s.model_dump()
    raw["strategies"]["iron_condor"]["hedge_width_pct"] = 1.0
    return type(s).model_validate(raw)


def strat_for(sid):
    return strat(sid, condor_settings()) if sid == "iron_condor" else strat(sid)


def ctx(budget, regime="TRENDING_UP", **kw):
    return make_ctx(chains=STEP50, regime=regime, option_risk_budget_inr=budget, **kw)


def loss_at(sid, width, regime="TRENDING_UP"):
    """One lot's cost-inclusive max loss of the strategy's structure with its hedge(s) `width` points out:
    the hand calculation the strategy has to match (same sold strike, same chain, same cost model)."""
    st = strat_for(sid)
    c = ctx(None, regime)
    exp = next(e for e in c.expiries if e.expiry == (EXP_M if sid == "credit_spread" else EXP_W))
    p = st.params
    rights = ["PE"] if sid == "credit_spread" else ["PE", "CE"]
    sides = []
    for r in rights:
        first = build_side(st, c, exp, r, p)
        k = first[0][0].strike - width if r == "PE" else first[0][0].strike + width
        sides.append(build_side(st, c, exp, r, p, hedge_strike=k))
    return one_lot_loss(c, exp, sides)


def width_of(sig):
    sold = [x for x in sig.legs if x.action == "SELL"]
    return max(abs(s.strike - next(b.strike for b in sig.legs if b.action == "BUY" and b.right == s.right))
               for s in sold)


# 1% of 24,000 = 240 points -> the nearest listed strike on this 50-point chain is 250 points out
CONFIGURED = 250


def test_losses_fall_with_width():
    losses = [loss_at("credit_spread", w) for w in (250, 200, 150, 100, 50)]
    assert all(a > b for a, b in zip(losses, losses[1:])) and losses[-1] > 0


def test_unchanged_without_a_budget_or_when_the_configured_width_fits():
    cs = strat("credit_spread")
    assert width_of(cs.evaluate(ctx(None))[0]) == CONFIGURED              # no budget: phase 4 behaviour
    big = float(loss_at("credit_spread", CONFIGURED))
    sig = cs.evaluate(ctx(big))[0]                                         # exactly at the budget: it fits
    assert width_of(sig) == CONFIGURED and not any("narrowed" in w for w in sig.why_now)


@pytest.mark.parametrize("width", [200, 150, 100])
def test_widest_width_that_fits_one_lot(width):
    """Budget exactly one lot at `width`: every wider width is above it, so `width` is the widest that fits."""
    budget = float(loss_at("credit_spread", width))
    sig = strat("credit_spread").evaluate(ctx(budget))
    assert sig, "expected a signal"
    assert width_of(sig[0]) == width
    assert Decimal(str(sig[0].details["max_loss_inr"])) == loss_at("credit_spread", width) <= Decimal(str(budget))
    assert any(f"narrowed from {CONFIGURED} to {width:g} points" in w for w in sig[0].why_now)


def test_between_widths_takes_the_wider_that_fits():
    """Budget 1 paisa under one lot at 250 (above 200): 200 is chosen. Just under 200: 150, never 100."""
    budget = float(loss_at("credit_spread", CONFIGURED)) - 0.01
    assert width_of(strat("credit_spread").evaluate(ctx(budget))[0]) == 200
    budget = float(loss_at("credit_spread", 200)) - 0.01
    assert width_of(strat("credit_spread").evaluate(ctx(budget))[0]) == 150


def test_none_fits_down_to_100_points_is_no_trade():
    """Budget 1 paisa under one lot at 100 points: no signal (ZERO_SIZE), even though a 50-point spread is listed
    and would fit - widths below 100 are never tried."""
    l100, l50 = loss_at("credit_spread", 100), loss_at("credit_spread", 50)
    budget = float(l100) - 0.01
    assert l50 <= Decimal(str(budget))                                     # 50 would fit, but is not allowed
    cs = strat("credit_spread")
    assert cs.evaluate(ctx(budget)) == []
    assert cs.last_no_trade[-1].startswith("ZERO_SIZE") and "down to 100 points" in cs.last_no_trade[-1]


def test_iron_condor_narrows_both_sides():
    l100 = loss_at("iron_condor", 100, "RANGE")
    ic = strat_for("iron_condor")
    full = ic.evaluate(ctx(None, "RANGE"))[0]
    assert width_of(full) == CONFIGURED
    sig = ic.evaluate(ctx(float(l100), "RANGE"))[0]
    assert width_of(sig) == 100 and Decimal(str(sig.details["max_loss_inr"])) == l100
    assert ic.evaluate(ctx(float(l100) - 0.01, "RANGE")) == [] and ic.last_no_trade[-1].startswith("ZERO_SIZE")


def test_the_risk_engine_sizes_the_narrowed_spread_to_one_lot():
    """The strategy's one-lot check and the engine's sizing agree: at capital C with budget 2.5% of C equal to
    one lot at 100 points, the 100-point spread is approved at exactly 1 lot, risk = budget."""
    l100 = loss_at("credit_spread", 100)
    capital = float(l100 / Decimal("0.025"))
    s = settings()
    s = s.model_copy(update={"account": s.account.model_copy(update={"capital_inr": capital})})
    budget = index_option_budget(s)
    assert Decimal(str(budget)) == l100
    sig = strat("credit_spread", s).evaluate(ctx(budget))[0]
    sig_details = {**sig.details, "margin_per_lot_inr": 40000.0}
    sig = sig.__class__(**{**{k: getattr(sig, k) for k in sig.__dataclass_fields__}, "details": sig_details})
    d = decide(sig, account(capital_inr=capital), market(now=sig.created_at), s, make_ctx().costs, no_corr, SECTORS)
    assert d.status == "APPROVED" and d.lots == 1 and Decimal(str(d.risk_inr)) == l100


def test_index_budget_is_capital_times_option_pct():
    s = settings()
    assert index_option_budget(s) == 5000.0                               # 2.5% of Rs 2,00,000
    s4 = s.model_copy(update={"account": s.account.model_copy(update={"capital_inr": 400000.0})})
    assert index_option_budget(s4) == 10000.0


def test_options_page_at_2_lakh_shows_no_trade_with_the_width_reason():
    """The Options page (live desk) passes the budget: at Rs 2,00,000 the fixture's NIFTY spread does not fit one
    lot at any width from 200 down to 100 points, so it is NO TRADE with that reason."""
    s, store, desk = desk_world(capital=200000.0)
    idea = {i["strategy_id"]: i for i in desk.view("NIFTY")["ideas"]}["credit_spread"]
    assert idea["status"] == "NO_TRADE" and idea["reasons"][0].startswith("ZERO_SIZE")
    assert "down to 100 points" in idea["reasons"][0]


def test_phase4_option_backtest_context_carries_the_index_budget():
    from tests.test_phase4 import world
    from app.options.backtest import OptionEngine
    s, store, base, model = world()
    eng = OptionEngine(s, store, "sim", model, base, lot_sizes={"NIFTY": 50})
    c = eng.context(strat("credit_spread", s), "NIFTY", date(2025, 7, 1), 24000.0, "strategy")
    assert c.kind == "index" and c.option_risk_budget_inr == index_option_budget(s)
