"""Phase 9 portfolio limits: CORRELATED_LIMIT, CORRELATION_UNKNOWN, PORTFOLIO_UNKNOWN, and their use in the
backtest with every stream side by side."""
import math
from collections import Counter
from datetime import date, timedelta

import pytest

from app.history import daily_start
from app.models import Bar
from app.options.option_history import OptionHistory
from app.options.portfolio import (CORRELATED_LIMIT, CORRELATION_UNKNOWN, PORTFOLIO_UNKNOWN, Correlations, Holding,
                                   PortfolioGate, correlation, daily_returns, linked_groups, portfolio_check)
from app.options.stock_backtest import variant
from functools import partial
from tests.test_phase9_core import engine_for, ist_date, settings, stock_world, weekdays_back

CFG = settings(account={"capital_inr": 1000000.0}).stock_option_selling.portfolio.model_copy(
    update={"max_total_risk_pct": 10.0, "group_max_risk_pct": 5.0})
CAP = 1000000.0
SECTORS = {"A": "Banks", "B": "Banks", "C": "IT", "D": "IT", "E": "Energy", "X": None}

pc = partial(portfolio_check, history_verified=True)


def none_corr(a, b):
    return 0.1


def test_ACCEPTANCE_every_position_counts_toward_the_count_and_total_risk_limits():
    h = [Holding(s, 10000) for s in "ACEACE"[:5]]
    assert pc("D", 10000, h[:4], SECTORS, none_corr, CFG, CAP)[0] is None
    code, why, facts = pc("D", 10000, h[:4] + [Holding("E", 10000), Holding("A", 1000)], SECTORS, none_corr, CFG, CAP)
    assert code == CORRELATED_LIMIT and "max_open_spreads 6" in why and facts["open_positions_with_this"] == 7
    # total risk: 10% of Rs 10,00,000 = Rs 1,00,000, the proposed position included; exactly at the limit is allowed
    three = [Holding("A", 25000), Holding("C", 25000), Holding("D", 20000)]
    assert pc("E", 30000, three, SECTORS, none_corr, CFG, CAP)[0] is None
    code, why, facts = pc("E", 30001, three, SECTORS, none_corr, CFG, CAP)
    assert code == CORRELATED_LIMIT and "10% of capital" in why and facts["total_risk_inr"] == 100001


def test_ACCEPTANCE_groups_are_connected_components_of_sector_or_correlation_links():
    corr = {("A", "C"): 0.1, ("A", "D"): 0.1, ("A", "E"): 0.1, ("B", "C"): 0.1, ("B", "D"): 0.1, ("B", "E"): 0.1,
            ("C", "E"): 0.75, ("D", "E"): 0.1}
    f = lambda a, b: corr.get(tuple(sorted((a, b))), 0.1)
    groups, problems = linked_groups(["A", "B", "C", "D", "E"], SECTORS, f, 0.7)
    assert problems == [] and groups == [{"C", "D", "E"}, {"A", "B"}]        # D-C by sector, C-E by correlation
    # the proposed position is in its group: two IT positions already open, an Energy one linked to them is refused
    code, why, facts = pc("E", 5000, [Holding("C", 5000), Holding("D", 5000)], SECTORS, f, CFG, CAP)
    assert code == CORRELATED_LIMIT and "group_max_positions 2" in why and facts["group"] == ["C", "D", "E"]
    assert pc("E", 5000, [Holding("C", 5000), Holding("A", 5000)], SECTORS, f, CFG, CAP)[0] is None
    # group risk: 5% of Rs 10,00,000 = Rs 50,000
    code, why, _ = pc("B", 26000, [Holding("A", 25000)], SECTORS, f, CFG, CAP)
    assert code == CORRELATED_LIMIT and "5% of capital" in why
    assert pc("B", 25000, [Holding("A", 25000)], SECTORS, f, CFG, CAP)[0] is None
    # two positions in the same stock are two positions of one group
    assert pc("A", 1000, [Holding("A", 1000), Holding("A", 1000)], SECTORS, f, CFG, CAP)[0] == CORRELATED_LIMIT


def test_ACCEPTANCE_unknown_sector_or_correlation_blocks_and_missing_portfolio_is_never_zero():
    f = lambda a, b: None
    code, why, _ = pc("C", 5000, [Holding("A", 5000)], SECTORS, f, CFG, CAP)
    assert code == CORRELATION_UNKNOWN and "A-C" in why                 # different sectors, correlation not measurable
    assert pc("B", 5000, [Holding("A", 5000)], SECTORS, f, CFG, CAP)[0] is None   # same sector: no correlation needed
    code, why, _ = pc("X", 5000, [Holding("A", 5000)], SECTORS, none_corr, CFG, CAP)
    assert code == CORRELATION_UNKNOWN and "X: sector not known" in why
    assert pc("X", 5000, [], SECTORS, f, CFG, CAP)[0] == CORRELATION_UNKNOWN
    code, why, _ = pc("A", 5000, None, SECTORS, none_corr, CFG, CAP)
    assert code == PORTFOLIO_UNKNOWN and "not known" in why
    assert pc("A", 5000, [], SECTORS, f, CFG, CAP)[0] is None       # nothing open: nothing to relate to


def test_ACCEPTANCE_portfolio_unknown_without_verified_closed_and_manual_history():
    """Review fix 2026-11-15: known open holdings are not enough; the closed and manual-trade history must be
    verified too. A backtest's simulated history counts as verified; live it stays unknown (no phase 6 journal)."""
    code, why, _ = portfolio_check("A", 5000, [], SECTORS, none_corr, CFG, CAP)            # history not verified
    assert code == PORTFOLIO_UNKNOWN and "history is not verified" in why
    assert portfolio_check("A", 5000, [], SECTORS, none_corr, CFG, CAP, history_verified=True)[0] is None
    assert PortfolioGate.history_verified is True


def test_ACCEPTANCE_groups_are_components_of_the_whole_universe_and_the_same_as_displayed():
    """Review fix 2026-11-15: A (Banks) and D (IT) are linked only through stocks nobody holds (A-E and C-E
    correlated, C and D in one sector). Built from the holdings alone they are separate groups; built from the
    universe they are one group, so a third position in it is refused, and the display shows that group."""
    from app.options.portfolio import universe_groups
    corr = {("A", "E"): 0.8, ("C", "E"): 0.8}
    f = lambda a, b: corr.get(tuple(sorted((a, b))), 0.1)
    held = [Holding("A", 5000), Holding("D", 5000)]
    assert pc("E", 5000, held, SECTORS, f, CFG, CAP)[0] is None                          # holdings only: {A, E} and {D}
    code, why, facts = pc("E", 5000, held, SECTORS, f, CFG, CAP, universe=["A", "B", "C", "D", "E"])
    assert code == CORRELATED_LIMIT and facts["group"] == ["A", "B", "C", "D", "E"] and facts["group_positions"] == 3
    shown = universe_groups(["A", "B", "C", "D", "E"], SECTORS, f, 0.7)["groups"]
    assert shown == [facts["group"]]
    # an unknown link that cannot join two groups holding positions changes nothing
    g = lambda a, b: None if {a, b} == {"B", "E"} else f(a, b)
    assert pc("A", 1000, [], SECTORS, g, CFG, CAP, universe=["A", "B", "C", "D", "E"])[0] is None


def bars_of(sym, closes, days):
    return [Bar(sym, daily_start(d), c, c, c, c, 1.0) for d, c in zip(days, closes)]


def test_correlation_uses_completed_sessions_only_and_needs_the_overlap():
    days = weekdays_back(date(2026, 11, 13), 80)
    a = [100 * (1 + 0.01 * math.sin(i)) for i in range(80)]
    b = [50 * (1 + 0.01 * math.sin(i)) for i in range(80)]           # the same returns
    c = [100 * (1 - 0.01 * math.sin(i)) for i in range(80)]          # nearly the opposite
    ra, rb, rc = (daily_returns(bars_of(s, x, days), days) for s, x in (("A", a), ("B", b), ("C", c)))
    win = days[-60:]
    assert correlation(ra, rb, win, 50) == pytest.approx(1.0, abs=1e-9)
    assert correlation(ra, rc, win, 50) < -0.9
    assert correlation(ra, rb, win[:40], 50) is None                    # only 40 common observations
    # a missing session is not bridged: no return on the day after a gap
    gap_days = days[:30] + days[31:]
    rg = daily_returns(bars_of("G", a[:30] + a[31:], gap_days), days)
    assert days[31] not in rg and days[32] in rg and days[30] not in rg
    # the value on a day does not change when later sessions are added
    corr = Correlations(lambda s: bars_of(s, {"A": a, "B": c}[s], days), days, lambda s: (), 60, 50)
    early = corr.at(days[69], "A", "B")
    later_bars = {"A": a[:70] + [100.0] * 10, "B": c[:70] + [200.0] * 10}
    corr2 = Correlations(lambda s: bars_of(s, later_bars[s], days), days, lambda s: (), 60, 50)
    assert corr2.at(days[69], "A", "B") == pytest.approx(early, abs=1e-12)


def test_ACCEPTANCE_backtest_streams_run_side_by_side_and_the_group_limit_refuses_entries_and_rolls():
    """The bull put and bear call baselines of one stock run side by side as two streams. With one position
    allowed per group, the second stream's entry is refused while the first one is open."""
    w = stock_world()
    cfg, store, base, _ = w
    one = settings(backtest=cfg.backtest.model_dump(), options=cfg.options.model_dump(),
                   stock_option_selling={"portfolio": {"enabled": True, "max_open_spreads": 6, "max_total_risk_pct": 100.0,
                                                       "correlation_window": 60, "correlation_threshold": 0.7,
                                                       "correlation_min_overlap": 50, "group_max_positions": 1,
                                                       "group_max_risk_pct": 100.0}})
    eng = engine_for(one, store, base, OptionHistory(store, "sim"))
    eng.set_expiry_floor(date(2025, 3, 1))
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    st = variant(one)
    free = [t for v in ("baseline:PE", "baseline:CE") for t in eng.run_stream(st, "RELIANCE", dev, v, Counter())]
    sk = Counter()
    gate = PortfolioGate(one, eng)
    held = eng.run_portfolio(st, [("RELIANCE", "baseline:PE"), ("RELIANCE", "baseline:CE")], dev, sk, gate)
    assert sk["NO_SIGNAL:CORRELATED_LIMIT"] >= 1 and len(held) < len(free)
    assert gate.summary()["refused"] == {"CORRELATED_LIMIT": sk["NO_SIGNAL:CORRELATED_LIMIT"]}
    # never two positions open at once
    spans = sorted((ist_date(t.entry_time), ist_date(t.exit_time)) for t in held)
    assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:]))
    # without a gate run_portfolio gives exactly the separate streams' trades
    same = eng.run_portfolio(st, [("RELIANCE", "baseline:PE"), ("RELIANCE", "baseline:CE")], dev, Counter(),
                             lambda *a: None)
    key = lambda t: (t.variant, t.signal_id, t.exit_time, t.net_pnl)
    assert sorted(map(key, same)) == sorted(map(key, free))
    assert eng._gate is None and eng._book is None                       # nothing left behind


def test_report_carries_the_portfolio_counts_and_the_correlated_groups():
    from app.options.stock_backtest import groups_at_end
    w = stock_world()
    cfg, store, base, _ = w
    eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    g = groups_at_end(cfg, eng, ["RELIANCE"], dev)["correlation_groups_at_end"]
    assert g["groups"] == [["RELIANCE"]] and g["sizes"] == [1] and g["date"] == dev[-1].isoformat()
    from tests.test_phase9_core import run_bt
    rep = run_bt(w)["report"]
    assert set(rep["portfolio_limits"]) == {"intraday", "daily"}
    assert rep["portfolio_limits"]["intraday"]["checks"] >= 1


# ---------------- research candidates versus actionable signals ----------------
def live_setup():
    from datetime import datetime, timezone
    from tests.test_phase9_core import EXP, ctx_for, stock_bars
    from app.options.stock_selling import build_stock_selling
    cfg = settings()
    bars = stock_bars()
    ctx = ctx_for(cfg, bars)
    sig = build_stock_selling(cfg).evaluate(ctx)[0]
    now = datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc)          # 11:30 IST, a Friday: market hours
    return cfg, ctx, sig, now, EXP


def test_ACCEPTANCE_a_signal_is_actionable_only_with_live_fresh_known_inputs():
    from app.options.actionable import actionable_check
    cfg, ctx, sig, now, exp = live_setup()
    codes = lambda w: [x.split(":")[0] for x in w]
    sectors = {"RELIANCE": "Oil Gas & Consumable Fuels"}
    fresh = {exp: now - timedelta(minutes=2)}
    ok, why = actionable_check(cfg, sig, ctx, now, fresh, [], sectors, none_corr, history_verified=True)
    assert ok is True and why == []
    # known open positions but no verified closed / manual history (live, before phase 6): unknown
    assert codes(actionable_check(cfg, sig, ctx, now, fresh, [], sectors, none_corr)[1]) == ["PORTFOLIO_UNKNOWN"]
    # the portfolio is not known (no position records yet): never actionable
    ok, why = actionable_check(cfg, sig, ctx, now, fresh, None, sectors, none_corr)
    assert ok is False and codes(why) == ["PORTFOLIO_UNKNOWN"]
    # a stale chain, after the close, a stale price, no bid/ask, no margin
    assert codes(actionable_check(cfg, sig, ctx, now, {exp: now - timedelta(minutes=20)}, [], sectors, none_corr, history_verified=True)[1]) == ["DATA_STALE"]
    assert codes(actionable_check(cfg, sig, ctx, now, {}, [], sectors, none_corr, history_verified=True)[1]) == ["DATA_STALE"]
    after = now + timedelta(hours=6)                                  # 17:30 IST
    assert "outside continuous trading" in actionable_check(cfg, sig, ctx, after, {exp: after}, [], sectors, none_corr, history_verified=True)[1][0]
    from dataclasses import replace
    assert "STALE" in actionable_check(cfg, sig, replace(ctx, status="STALE"), now, fresh, [], sectors, none_corr, history_verified=True)[1][0]
    from app.options.base import ChainQuotes, OptionQuote
    short = sig.legs[0]
    bare = ChainQuotes(ctx.spot, {exp: [OptionQuote(short.strike, short.right, None, None, 5.0, 1e6)]})
    assert any("no bid and ask" in w for w in actionable_check(cfg, sig, replace(ctx, quotes=bare), now, fresh, [], sectors, none_corr, history_verified=True)[1])
    nomargin = replace(sig, details={**sig.details, "margin_inr": None})
    assert codes(actionable_check(cfg, nomargin, ctx, now, fresh, [], sectors, none_corr, history_verified=True)[1]) == ["MARGIN_UNKNOWN"]
    full = [Holding(s, 1000) for s in ("TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "LT")]
    sec = {**sectors, "TCS": "IT", "INFY": "IT", "HDFCBANK": "Fin", "ICICIBANK": "Fin", "SBIN": "Fin", "LT": "Cap"}
    assert codes(actionable_check(cfg, sig, ctx, now, fresh, full, sec, none_corr, history_verified=True)[1]) == ["CORRELATED_LIMIT"]
    assert actionable_check(cfg, None, ctx, now, fresh, [], sectors, none_corr)[0] is False


def test_ACCEPTANCE_shortlist_rows_are_labelled_research_with_their_snapshot_and_are_not_actionable():
    from tests.test_phase9_core import test_run_shortlist_uses_only_chains_and_bars_of_the_session as build
    from app.options.shortlist import run_shortlist
    from app.store import Store
    import tests.test_phase9_core as core
    store = Store(":memory:")
    session = date(2026, 11, 13)
    idx = [Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None) for d in weekdays_back(session, 320)]
    rel = core.stock_bars("RELIANCE")
    store.save_history_bars("bars_1d", idx + rel, "sim")
    from app.options.instruments import ContractSpec, InstrumentBook
    InstrumentBook(store, "sim").save([ContractSpec("RELIANCE", core.EXP, core.LOT, "monthly",
                                                    tuple(float(k) for k in range(800, 1501, 10)), core.NOW)])
    store.save_option_chain("sim", [("RELIANCE", core.EXP.isoformat(), q.strike, q.right, core.NOW.isoformat(), q.bid, q.ask,
                                     q.last, q.oi, 0.0, 100.0, rel[-1].close) for q in core.chain(rel[-1].close)])
    out = run_shortlist(settings(), store, "sim", now=core.NOW + timedelta(minutes=30), save=False)
    row = next(r for r in out["rows"] if r["stock"] == "RELIANCE")
    assert row["label"] == "research candidate" and row["actionable"] is False
    assert row["snapshot"]["session_date"] == "2026-11-13" and row["snapshot"]["daily_bar_date"] == "2026-11-13"
    assert row["snapshot"]["chain_as_of_utc"] == {core.EXP.isoformat(): core.NOW.isoformat()}
    assert any(w.startswith(("PORTFOLIO_UNKNOWN", "no complete setup")) for w in row["not_actionable_because"])


@pytest.mark.usefixtures("fixed_clock")
def test_shortlist_api_shows_the_correlated_groups():
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app) as c:
        j = c.get("/api/stock-option-selling/shortlist").json()
        assert "correlation_groups" in j and "research candidate" in j["note"] and "PORTFOLIO_UNKNOWN" in j["note"]
        assert "Correlated groups" in c.get("/stock-option-selling").text
