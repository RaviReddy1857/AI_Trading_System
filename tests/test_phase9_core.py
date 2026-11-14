"""Phase 9 core: stock option selling rules, shortlist, exits and rolls, the backtest with 5-minute and
daily exit checks beside its no-filter baseline, and the budgeted, resumable option-history download.
Acceptance checks of docs/implementation.md phase 9 that belong to the core are marked ACCEPTANCE."""
import math
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.backtest import Engine
from app.calendar import MarketCalendar
from app.config import Settings, StockOptionSelling as SosParams
from app.costs import CostModel
from app.history import daily_start
from app.manual_data import Event
from app.models import Bar, unhedged_short_legs
from app.options.backtest import OptionEngine, intraday_spread_exit, model_prices, run_option_backtest
from app.options.base import ChainQuotes, ExpiryInfo, OptionContext, OptionQuote
from app.options.greeks import bs_delta, bs_price, years_to_expiry
from app.options.option_history import (INTRADAY, IntradayBar, OptionHistory, OptionIntraday, budgeted_fetcher,
                                        parse_option_rows)
from app.options.shortlist import run_shortlist
from app.options.stock_backtest import coverage, download_blocks, liquid_stocks
from app.options.stock_selling import (SpreadPosition, StockOptionSelling, build_shortlist, build_stock_selling,
                                       exit_calls, exit_levels, roll_choices, support_resistance, trend_of)
from app.quality import IST
from app.store import Store
from tests.test_phase3 import FixedRegime, settings

UTC = timezone.utc
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)          # Friday 15:30 IST: the close
EXP = date(2026, 12, 8)                                  # 25 days away
EXP_NEXT = date(2026, 12, 29)                            # 46 days away
RATE = 0.065
LOT = 250


# ---------------- fixtures: daily bars and a live chain ----------------
def weekdays_back(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def stock_bars(sym="RELIANCE", n=320, start=1000.0, drift=1.0, end=date(2026, 11, 13), wobble=6.0, volume=1e6):
    """`n` daily bars ending on `end`. The price is `start` 120 sessions before the end and moves
    `drift` a session, with a small wave."""
    bars = []
    for i, d in enumerate(weekdays_back(end, n)):
        j = i - (n - 120)
        px = start + drift * j + wobble * math.sin(j / 3)
        bars.append(Bar(sym, daily_start(d), px - 2, px + 8, px - 8, px, volume))
    return bars


def index_bars(bars, level=24000.0, drift=0.0):
    return tuple(Bar("NIFTY", b.start, level + drift * i, level + drift * i + 50, level + drift * i - 50,
                     level + drift * i, None) for i, b in enumerate(bars))


def chain(spot, expiry=EXP, iv=0.25, lo=800, hi=1500, step=10, oi_lots=500, lot=LOT, asof=NOW, oi=None, wide=()):
    """Black-Scholes quotes with a 2% bid-ask width. `oi`: {(strike, right): lots}. `wide`: strikes with a 40% width."""
    t = years_to_expiry(asof, expiry)
    out = []
    for k in range(lo, hi + 1, step):
        for right in ("CE", "PE"):
            p = max(0.1, bs_price(spot, k, t, RATE, 0.0, iv, right))
            half = p * (0.2 if (k, right) in wide else 0.01)
            lots = (oi or {}).get((k, right), oi_lots)
            out.append(OptionQuote(float(k), right, round(p - half, 2), round(p + half, 2), round(p, 2), lots * lot))
    return out


def ctx_for(s: Settings, bars, quotes=None, iv_rank=80.0, margin=40000.0, events=(), expiries=None, sym="RELIANCE",
            iv=0.25, **over) -> OptionContext:
    spot = bars[-1].close
    chains = quotes if isinstance(quotes, dict) else {EXP: quotes if quotes is not None else chain(spot, iv=iv)}
    sos = s.stock_option_selling
    base = dict(
        underlying=sym, kind="stock", asof=NOW, regime="RANGE", spot=spot,
        expiries=expiries or tuple(ExpiryInfo(e, "monthly", LOT) for e in sorted(chains)),
        quotes=ChainQuotes(spot, chains), costs=CostModel(s.costs), capital_inr=s.account.capital_inr, rate=RATE,
        iv_reference_pct=iv * 100, iv_rank=iv_rank, margin=lambda structure, width: margin, require_margin=True,
        historical=False, max_spread_pct=sos.max_bid_ask_spread_pct, min_oi_lots=sos.min_open_interest,
        events=tuple(events), daily=tuple(bars), index_daily=index_bars(bars))
    base.update(over)
    return OptionContext(**base)


@pytest.fixture(scope="module")
def s():
    return settings()


@pytest.fixture()
def strat(s):
    return build_stock_selling(s)


def legs_of(sig):
    return next(x for x in sig.legs if x.action == "SELL"), next(x for x in sig.legs if x.action == "BUY")


# ---------------- trend, support, resistance ----------------
def test_trend_and_support_resistance_by_hand():
    up, down = stock_bars(drift=1.0), stock_bars(start=1200, drift=-1.0)
    assert trend_of(up, 20, 50, 14)[0] == "UP" and trend_of(down, 20, 50, 14)[0] == "DOWN"
    flat = stock_bars(drift=0.0, wobble=0.0)
    assert trend_of(flat, 20, 50, 14)[0] is None                  # close equals both averages: no clear trend
    assert trend_of(up[:30], 20, 50, 14)[0] is None                # not enough history is unknown, not a trend
    lows, highs = [b.low for b in up[-20:]], [b.high for b in up[-20:]]
    assert support_resistance(up, 20) == (min(lows), max(highs))
    assert support_resistance(up[:10], 20) == (None, None)
    assert 0 < trend_of(up, 20, 50, 14)[1] <= 1


# ---------------- short strike ----------------
def test_ACCEPTANCE_short_strike_is_beyond_support_beyond_the_highest_oi_strike_and_within_the_delta_limit(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    support, _ = support_resistance(bars, 20)
    wall = float(int(support // 10) * 10 - 10)                     # highest-OI put strike just below support
    quotes = chain(spot, oi={(int(wall), "PE"): 5000})
    sig = strat.evaluate(ctx_for(s, bars, quotes))
    assert sig, strat.last_no_trade
    short, long_ = legs_of(sig[0])
    t = years_to_expiry(NOW, EXP)
    by = {(q.strike, q.right): q for q in quotes}

    def delta(k):
        from app.options.greeks import iv_and_delta
        return iv_and_delta(by[(k, "PE")].mid, spot, k, t, RATE, 0.0, "PE")[1]

    assert short.right == "PE" and short.strike < support and short.strike < wall
    assert abs(delta(short.strike)) <= s.stock_option_selling.max_short_delta
    nearer = short.strike + 10                                     # the next strike toward the money breaks a rule
    assert not (nearer < support and nearer < wall and abs(delta(nearer)) <= s.stock_option_selling.max_short_delta)
    assert sig[0].asset_class == "STOCK_OPTION" and sig[0].details["side"] == "PE"
    assert long_.strike < short.strike and long_.expiry == short.expiry == EXP


def test_ACCEPTANCE_bear_call_short_strike_is_above_resistance(s, strat):
    bars = stock_bars(start=1200, drift=-1.0)
    spot = bars[-1].close
    _, resistance = support_resistance(bars, 20)
    strat = build_stock_selling(settings(stock_option_selling={"min_net_credit_pct_of_max_loss": 5}))
    sig = strat.evaluate(ctx_for(s, bars, chain(spot)))
    assert sig, strat.last_no_trade
    short, long_ = legs_of(sig[0])
    assert short.right == "CE" and short.strike > resistance and long_.strike > short.strike
    assert abs(sig[0].details["legs"][0]["delta"]) <= s.stock_option_selling.max_short_delta


def test_ACCEPTANCE_no_signal_when_no_strike_meets_all_three_conditions(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    # the highest-OI put strike is the lowest listed strike: nothing lies beyond it
    assert strat.evaluate(ctx_for(s, bars, chain(spot, oi={(800, "PE"): 9000}))) == []
    assert any(r.startswith("NO_STRIKE") for r in strat.last_no_trade)
    # no strike below support at all
    support, _ = support_resistance(bars, 20)
    lo = int(support // 10) * 10 + 10
    assert strat.evaluate(ctx_for(s, bars, chain(spot, lo=lo))) == []
    assert any(r.startswith("NO_STRIKE") for r in strat.last_no_trade)
    # delta: with a very high implied volatility no strike within reach is at or below the limit
    tight = build_stock_selling(settings(stock_option_selling={"max_short_delta": 0.01}))
    assert tight.evaluate(ctx_for(s, bars, chain(spot, iv=0.9), iv=0.9)) == []
    assert any(r.startswith("NO_STRIKE") for r in tight.last_no_trade)


# ---------------- hedge strike ----------------
def test_ACCEPTANCE_hedge_is_two_strikes_out_same_expiry_and_within_the_max_loss_limit(s, strat):
    bars = stock_bars()
    sig = strat.evaluate(ctx_for(s, bars))[0]
    short, long_ = legs_of(sig)
    assert short.strike - long_.strike == 20 and sig.details["hedge_strikes_out"] == 2      # 10-point strikes
    assert long_.expiry == short.expiry and long_.right == short.right
    limit = s.account.capital_inr * s.stock_option_selling.max_loss_per_stock_spread_pct / 100
    assert 0 < sig.details["max_loss_inr"] <= limit
    # max loss = (width - credit) x lot + costs, by hand
    d = sig.details
    assert d["max_loss_inr"] == pytest.approx((20 - d["net_credit"]) * LOT + d["costs_inr"], abs=0.02)
    assert unhedged_short_legs(sig.legs) == []


def test_ACCEPTANCE_hedge_moves_to_three_strikes_out_when_two_is_illiquid_and_no_signal_when_neither_works(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    base = strat.evaluate(ctx_for(s, bars))[0]
    short, long_ = legs_of(base)
    two, three = int(short.strike - 20), int(short.strike - 30)
    sig = strat.evaluate(ctx_for(s, bars, chain(spot, oi={(two, "PE"): 5})))            # 5 lots: below 100
    assert sig and legs_of(sig[0])[1].strike == three and sig[0].details["hedge_strikes_out"] == 3
    sig = strat.evaluate(ctx_for(s, bars, chain(spot, wide={(two, "PE")})))            # bid-ask width 40%
    assert sig and legs_of(sig[0])[1].strike == three
    assert strat.evaluate(ctx_for(s, bars, chain(spot, oi={(two, "PE"): 5, (three, "PE"): 5}))) == []
    assert any(r.startswith("ILLIQUID") for r in strat.last_no_trade)
    # max loss above the limit at both widths: no signal
    small = build_stock_selling(settings(stock_option_selling={"max_loss_per_stock_spread_pct": 1.0}))
    assert small.evaluate(ctx_for(s, bars)) == []
    assert any(r.startswith("MAX_LOSS_ABOVE_LIMIT") for r in small.last_no_trade)


def test_ACCEPTANCE_a_short_leg_without_a_hedge_is_never_suggested(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    base = strat.evaluate(ctx_for(s, bars))[0]
    short = legs_of(base)[0]
    quotes = [q for q in chain(spot) if not (q.right == "PE" and q.strike < short.strike)]   # nothing further out
    assert strat.evaluate(ctx_for(s, bars, quotes)) == []
    assert any(r.startswith("UNHEDGED_SHORT") for r in strat.last_no_trade)
    # the illiquid sold strike itself is refused too
    assert strat.evaluate(ctx_for(s, bars, chain(spot, oi={(int(short.strike), "PE"): 5}))) == []
    assert any(r.startswith("ILLIQUID") for r in strat.last_no_trade)


@given(iv=st.floats(0.12, 0.7), drift=st.sampled_from([-1.5, -1.0, 1.0, 1.5]), wobble=st.floats(0, 12),
       lot=st.sampled_from([50, 250, 700, 2400]))
@hyp_settings(max_examples=40, deadline=None)
def test_every_signal_is_hedged_inside_the_window_and_inside_the_loss_limit(iv, drift, wobble, lot):
    cfg = settings()
    stg = build_stock_selling(cfg)
    bars = stock_bars(start=1000 if drift > 0 else 1300, drift=drift, wobble=wobble)
    spot = bars[-1].close
    c = ctx_for(cfg, bars, chain(spot, iv=iv, lot=lot), iv=iv, expiries=(ExpiryInfo(EXP, "monthly", lot),))
    for sig in stg.evaluate(c):
        short, long_ = legs_of(sig)
        assert unhedged_short_legs(sig.legs) == [] and short.expiry == long_.expiry and short.right == long_.right
        assert (long_.strike < short.strike) if short.right == "PE" else (long_.strike > short.strike)
        assert short.right == ("PE" if drift > 0 else "CE")
        assert sig.details["max_loss_inr"] <= cfg.account.capital_inr * cfg.stock_option_selling.max_loss_per_stock_spread_pct / 100
        assert sig.details["max_profit_inr"] > 0 and 20 <= sig.details["days_to_expiry"] <= 35
        assert sig.target < sig.entry < sig.stop


# ---------------- entry window, events, trend, IV rank, margin ----------------
@pytest.mark.parametrize("expiry,ok", [(date(2026, 11, 24), False), (date(2026, 12, 2), False), (date(2026, 12, 3), True),
                                       (date(2026, 12, 18), True), (date(2026, 12, 19), False), (date(2027, 1, 26), False)])
def test_ACCEPTANCE_no_signal_outside_20_to_35_days_to_expiry_or_in_expiry_week(s, strat, expiry, ok):
    bars = stock_bars()
    c = ctx_for(s, bars, {expiry: chain(bars[-1].close, expiry=expiry)})
    out = strat.evaluate(c)
    assert bool(out) == ok
    if not ok:
        assert any(r.startswith("NO_EXPIRY") for r in strat.last_no_trade)


def test_ACCEPTANCE_no_entry_when_results_or_a_corporate_action_fall_before_expiry(s, strat):
    bars = stock_bars()
    for kind in ("RESULTS", "BOARD_MEETING", "CORPORATE_ACTION"):
        assert strat.evaluate(ctx_for(s, bars, events=[Event(date(2026, 11, 25), kind, "", "RELIANCE")])) == []
        assert any(r.startswith("EVENT_BEFORE_EXPIRY") for r in strat.last_no_trade)
    assert strat.evaluate(ctx_for(s, bars, events=[Event(date(2026, 11, 25), "RESULTS", "", "TCS")]))      # another stock
    assert strat.evaluate(ctx_for(s, bars, events=[Event(date(2026, 12, 9), "RESULTS", "", "RELIANCE")]))  # after expiry
    assert strat.evaluate(ctx_for(s, bars, events=[Event(date(2026, 11, 25), "RBI_POLICY", "", "")]))      # market-wide


def test_no_signal_without_a_clear_trend_a_known_high_iv_rank_a_margin_or_a_live_price(s, strat):
    bars = stock_bars()
    flat = stock_bars(drift=0.0, wobble=0.0)
    cases = [(ctx_for(s, flat), "NO_TREND"), (ctx_for(s, bars, iv_rank=None), "IV_RANK_UNKNOWN"),
             (ctx_for(s, bars, iv_rank=30.0), "IV_RANK_TOO_LOW"), (ctx_for(s, bars, margin=None), "MARGIN_UNKNOWN"),
             (ctx_for(s, bars, status="STALE"), "DATA_STALE"), (ctx_for(s, bars, kind="index"), "NOT_A_STOCK"),
             (ctx_for(s, bars, expiries=(ExpiryInfo(EXP, "monthly", None),)), "LOT_SIZE_UNKNOWN")]
    for c, code in cases:
        assert strat.evaluate(c) == [] and any(r.startswith(code) for r in strat.last_no_trade), code
    off = build_stock_selling(settings(stock_option_selling={"enabled": False}))
    assert off.evaluate(ctx_for(s, bars)) == [] and any(r.startswith("DISABLED") for r in off.last_no_trade)
    with pytest.raises(ValueError):                                 # a live context cannot switch the margin rule off
        ctx_for(s, bars, require_margin=False)


def test_strategy_identity_status_and_determinism(s, strat):
    assert strat.id == "stock_option_selling" and strat.version.startswith("1/")
    assert (strat.alerts_enabled, strat.mode) == (False, "paper_only") and "UNPROVEN" in strat.status_label
    other = build_stock_selling(settings(stock_option_selling={"max_short_delta": 0.15}))
    assert other.version != strat.version                           # a threshold change is a new version
    bars = stock_bars()
    a, b = strat.evaluate(ctx_for(s, bars))[0], strat.evaluate(ctx_for(s, bars))[0]
    assert a.to_dict() == b.to_dict()
    # the index credit spread still refuses stocks, and this strategy refuses an index
    from app.options import build_option_strategies
    cs = build_option_strategies(s, ["credit_spread"])[0]
    assert cs.evaluate(ctx_for(s, bars)) == [] and any("STOCK_OPTION_SELLING_NOT_ALLOWED" in r for r in cs.last_no_trade)


def test_config_is_validated():
    for bad in ({"entry_min_days_to_expiry": 40}, {"exit_days_before_expiry": 5}, {"hedge_strikes_out": [3, 2]},
                {"hedge_strikes_out": [2]}, {"trend_fast_sma": 60}, {"max_short_delta": 0.6},
                {"backtest": {"exit_checks": []}}, {"backtest": {"exit_checks": ["hourly"]}}):
        with pytest.raises(Exception):
            settings(stock_option_selling=bad)
    with pytest.raises(Exception):
        settings(options={"backtest": {"daily_request_budget": -1}})
    p = SosParams()
    assert (p.entry_min_dte, p.entry_max_dte, p.exit_days_before_expiry) == (20, 35, 7)
    assert settings().options.backtest.daily_request_budget == 3500


# ---------------- shortlist ----------------
def shortlist_contexts(s):
    good = stock_bars("RELIANCE")
    strong = stock_bars("TCS", drift=2.0)
    illiquid = stock_bars("INFY")
    event = stock_bars("HDFCBANK")
    flat = stock_bars("ICICIBANK", drift=0.0, wobble=0.0)
    return [
        ctx_for(s, good, iv_rank=60.0, sym="RELIANCE"),
        ctx_for(s, strong, chain(strong[-1].close, hi=1700), iv_rank=95.0, sym="TCS"),
        ctx_for(s, illiquid, chain(illiquid[-1].close, oi_lots=10), sym="INFY"),
        ctx_for(s, event, events=[Event(date(2026, 11, 20), "RESULTS", "Q3", "HDFCBANK")], sym="HDFCBANK"),
        ctx_for(s, flat, sym="ICICIBANK"),
    ]


def test_ACCEPTANCE_shortlist_excludes_illiquid_stocks_and_events_and_shows_a_reason_per_candidate(s, strat):
    rows = build_shortlist(strat, shortlist_contexts(s))
    by = {r["stock"]: r for r in rows}
    top = [r for r in rows if r["status"] == "SHORTLISTED"]
    assert [r["stock"] for r in top] == ["TCS", "RELIANCE"] and [r["rank"] for r in top] == [1, 2]
    assert top[0]["score"] > top[1]["score"]                        # higher IV rank and a clearer trend rank first
    for r in top:
        assert r["side"] == "PE" and r["expiry"] == EXP.isoformat() and len(r["reasons"]) >= 4
        assert set(r["scores"]) == {"iv_rank", "trend_clarity", "distance", "liquidity"}
        assert all(0 <= v <= 1 for v in r["scores"].values())
    w = s.stock_option_selling.weights
    sc = top[0]["scores"]
    assert top[0]["score"] == pytest.approx(sc["iv_rank"] * w.iv_rank + sc["trend_clarity"] * w.trend_clarity
                                            + sc["distance"] * w.distance + sc["liquidity"] * w.liquidity, abs=1e-3)
    assert by["INFY"]["status"] == "EXCLUDED" and any(x.startswith("ILLIQUID") for x in by["INFY"]["reasons"])
    assert by["HDFCBANK"]["status"] == "EXCLUDED" and any(x.startswith("EVENT_BEFORE_EXPIRY") for x in by["HDFCBANK"]["reasons"])
    assert by["ICICIBANK"]["status"] == "EXCLUDED" and any(x.startswith("NO_TREND") for x in by["ICICIBANK"]["reasons"])
    # the list is cut at shortlist_size, and a valid setup without a margin entry is still listed (no signal)
    one = build_stock_selling(settings(stock_option_selling={"shortlist_size": 1}))
    rows1 = build_shortlist(one, shortlist_contexts(s))
    assert [r["stock"] for r in rows1 if r["status"] == "SHORTLISTED"] == ["TCS"]
    assert "ranked 2" in next(r for r in rows1 if r["stock"] == "RELIANCE")["reasons"][-1]
    no_margin = build_shortlist(strat, [ctx_for(s, stock_bars(), margin=None)])[0]
    assert no_margin["status"] == "SHORTLISTED" and any(x.startswith("MARGIN_UNKNOWN") for x in no_margin["reasons"])


def test_shortlist_is_stored_and_served_read_only(s, strat):
    store = Store(":memory:")
    rows = build_shortlist(strat, shortlist_contexts(s))
    store.save_shortlist(date(2026, 11, 13), rows, NOW)
    got = store.shortlist()
    assert got["session_date"] == "2026-11-13" and [r["stock"] for r in got["rows"][:2]] == ["TCS", "RELIANCE"]
    assert got["rows"][0]["scores"] == rows[0]["scores"] and got["rows"][0]["reasons"] == rows[0]["reasons"]
    store.save_shortlist(date(2026, 11, 13), rows[:1], NOW)         # a rebuild replaces the day
    assert len(store.shortlist(date(2026, 11, 13))["rows"]) == 1
    assert Store(":memory:").shortlist() == {"session_date": None, "created_utc": None, "rows": []}


def test_run_shortlist_uses_only_chains_and_bars_of_the_session(s):
    """RELIANCE has a daily bar and a chain stored on the session: assessed. TCS has no chain from that
    day and INFY no daily bar for it: both are left out with the reason, never guessed."""
    from app.options.instruments import ContractSpec, InstrumentBook
    store = Store(":memory:")
    session = date(2026, 11, 13)
    idx = [Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None) for d in weekdays_back(session, 5)]
    rel, tcs = stock_bars("RELIANCE"), stock_bars("TCS")
    infy = stock_bars("INFY", end=date(2026, 11, 12))
    store.save_history_bars("bars_1d", idx + rel + tcs + infy, "sim")
    strikes = tuple(float(k) for k in range(800, 1501, 10))
    InstrumentBook(store, "sim").save([ContractSpec(sym, EXP, LOT, "monthly", strikes, NOW)
                                       for sym in ("RELIANCE", "TCS", "INFY")])

    def save_chain(sym, spot, as_of):
        store.save_option_chain("sim", [(sym, EXP.isoformat(), q.strike, q.right, as_of.isoformat(), q.bid, q.ask, q.last,
                                         q.oi, 0.0, 100.0, spot) for q in chain(spot)])
    save_chain("RELIANCE", rel[-1].close, NOW)
    save_chain("TCS", tcs[-1].close, NOW - timedelta(days=1))
    save_chain("INFY", infy[-1].close, NOW)
    out = run_shortlist(s, store, "sim", now=NOW + timedelta(minutes=30))
    by = {r["stock"]: r for r in out["rows"]}
    assert out["session_date"] == "2026-11-13" and set(by) == {"RELIANCE", "TCS", "INFY"}
    assert by["TCS"]["status"] == "EXCLUDED" and by["TCS"]["reasons"][0].startswith("NO_CHAIN")
    assert by["INFY"]["status"] == "EXCLUDED" and by["INFY"]["reasons"][0].startswith("DATA_STALE")
    # RELIANCE was assessed by the rules (its stock IV rank has no stored history here, so it is excluded for that)
    assert any(x.startswith("IV_RANK_UNKNOWN") for x in by["RELIANCE"]["reasons"])
    assert store.shortlist()["session_date"] == "2026-11-13"


def test_shortlist_route_and_page_are_read_only(s):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app) as c:
        r = c.get("/api/stock-option-selling/shortlist")
        assert r.status_code == 200 and r.json()["rows"] == [] and "UNPROVEN" in r.json()["label"]
        app.state.store.save_shortlist(date(2026, 11, 13), build_shortlist(build_stock_selling(s), shortlist_contexts(s)), NOW)
        j = c.get("/api/stock-option-selling/shortlist").json()
        assert j["rows"][0]["stock"] == "TCS" and j["rows"][0]["status"] == "SHORTLISTED" and j["alerts"] is False
        page = c.get("/stock-option-selling")
        assert page.status_code == 200 and "RESEARCH ONLY" in page.text
        assert c.post("/api/stock-option-selling/shortlist", json={}).status_code in (403, 405)   # no write route here


# ---------------- exit and adjustment calls, roll ----------------
POS = SpreadPosition("RELIANCE", "PE", 1060.0, 1040.0, EXP, 4.0, LOT)


def kinds(calls):
    return [c["kind"] for c in calls]


def test_ACCEPTANCE_profit_target_stop_threat_and_forced_exit_calls_fire(s):
    p = s.stock_option_selling
    assert exit_levels(4.0, p) == (2.0, 8.0)                        # keep 50% of the credit; stop at 2x the credit
    today = date(2026, 11, 20)
    assert exit_calls(POS, p, today, 3.0, 1100.0, "UP") == []
    assert kinds(exit_calls(POS, p, today, 2.0, 1100.0, "UP")) == ["TARGET"]
    assert kinds(exit_calls(POS, p, today, 1.2, 1100.0, "UP")) == ["TARGET"]
    stop = exit_calls(POS, p, today, 8.0, 1100.0, "UP")
    assert kinds(stop) == ["STOP"] and stop[0]["pnl_before_costs_inr"] == (4.0 - 8.0) * LOT
    assert kinds(exit_calls(POS, p, today, 3.0, 1059.0, "UP")) == ["STRIKE_BREACHED"]
    assert kinds(exit_calls(POS, p, today, 3.0, 1060.0, "UP")) == []            # at the strike is not beyond it
    assert kinds(exit_calls(POS, p, today, 3.0, 1100.0, "DOWN")) == ["TREND_FLIP"]
    assert kinds(exit_calls(POS, p, today, 3.0, 1100.0, None)) == []            # no clear trend is not a flip
    forced = exit_calls(POS, p, date(2026, 12, 1), 3.0, 1100.0, "UP")           # 7 days to expiry
    assert kinds(forced) == ["FORCED_EXPIRY_EXIT"] and forced[0]["action"] == "Exit now: physical settlement"
    assert kinds(exit_calls(POS, p, date(2026, 11, 30), 3.0, 1100.0, "UP")) == []
    # the same state gives the same call every time it is asked: it repeats until the position is closed
    assert exit_calls(POS, p, date(2026, 12, 3), 9.0, 1050.0, "DOWN") == exit_calls(POS, p, date(2026, 12, 3), 9.0, 1050.0, "DOWN")
    assert kinds(exit_calls(POS, p, date(2026, 12, 3), 9.0, 1050.0, "DOWN")) == ["FORCED_EXPIRY_EXIT", "STOP",
                                                                               "STRIKE_BREACHED", "TREND_FLIP"]
    # an unknown price is reported, never read as "no exit"
    assert kinds(exit_calls(POS, p, today, None, 1100.0, "UP")) == ["DATA_MISSING"]
    call = SpreadPosition("RELIANCE", "CE", 1200.0, 1220.0, EXP, 4.0, LOT)
    assert kinds(exit_calls(call, p, today, 3.0, 1201.0, "UP")) == ["STRIKE_BREACHED", "TREND_FLIP"]


def test_ACCEPTANCE_threat_shows_exit_and_roll_with_numbers_and_a_roll_is_never_unhedged(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    both = {EXP: chain(spot), EXP_NEXT: chain(spot, expiry=EXP_NEXT)}
    c = ctx_for(s, bars, both, expiries=(ExpiryInfo(EXP, "monthly", LOT), ExpiryInfo(EXP_NEXT, "monthly", LOT)))
    held = SpreadPosition("RELIANCE", "PE", 1100.0, 1080.0, EXP, 4.0, LOT)       # struck nearer the money than the rules now give
    out = roll_choices(strat, c, held, close_cost=7.0)
    assert out["exit"]["cost_to_close_inr"] == 7.0 * LOT
    assert out["exit"]["realised_pnl_inr"] < (4.0 - 7.0) * LOT                   # the loss plus closing charges
    roll = out["roll"]
    assert roll is not None and roll["expiry"] == EXP.isoformat()
    assert unhedged_short_legs(roll["legs"]) == [] and roll["legs"][0]["strike"] < 1100.0
    assert roll["net_per_share"] == pytest.approx(roll["new_credit_per_share"] - 7.0, abs=0.01)
    assert roll["net_is"] == "debit" and roll["new_max_loss_inr"] > 0 and roll["new_breakevens"] and roll["margin_inr"] == 40000.0
    # the same spread is not a roll: with only this month available and the same strikes there is none
    now_legs = legs_of(strat.evaluate(ctx_for(s, bars))[0])
    same = SpreadPosition("RELIANCE", "PE", now_legs[0].strike, now_legs[1].strike, EXP, 4.0, LOT)
    assert roll_choices(strat, ctx_for(s, bars), same, 7.0)["roll"] is None
    # a falling stock does not pass the falling-stock block: the alert is "exit", and it says why
    down = stock_bars(start=1200, drift=-1.0)
    out = roll_choices(strat, ctx_for(s, down, chain(down[-1].close)), held, 7.0)
    assert out["action"] == "EXIT" and out["roll"] is None and out["roll_note"].startswith("exit: DOWNTREND_BLOCK")


# ---------------- 5-minute exit check ----------------
def ib(hhmm, o, c=None, d=date(2025, 7, 10)):
    h, m = map(int, hhmm.split(":"))
    c = o if c is None else c
    return IntradayBar(datetime.combine(d, time(h, m), IST).astimezone(UTC), o, max(o, c), min(o, c), c)


def test_intraday_exit_decides_on_a_bar_close_and_fills_at_the_next_bar_open():
    age = timedelta(minutes=30)
    short = [ib("09:15", 5.0), ib("09:20", 5.0, 9.0), ib("09:25", 9.5, 9.0), ib("09:30", 9.0)]
    long_ = [ib("09:15", 1.0), ib("09:20", 1.0), ib("09:25", 1.2, 1.0), ib("09:30", 1.0)]
    # cost at 09:20 close = 9 - 1 = 8 = the stop: decided there, filled at the 09:25 opens (never the same bar)
    r = intraday_spread_exit([short, long_], [1, -1], 2.0, 8.0, age)
    assert r[0] == "STOP" and r[1] == short[2].start and r[2] == [9.5, 1.2]
    # target: cost at or below 2
    r = intraday_spread_exit([[ib("10:00", 4.0, 2.5), ib("10:05", 2.4)], [ib("10:00", 1.0, 0.5), ib("10:05", 0.6)]],
                             [1, -1], 2.0, 8.0, age)
    assert r[0] == "TARGET" and r[2] == [2.4, 0.6]
    # nothing reached: None
    assert intraday_spread_exit([[ib("10:00", 4.0)], [ib("10:00", 1.0)]], [1, -1], 2.0, 8.0, age) is None
    assert intraday_spread_exit([[], []], [1, -1], 2.0, 8.0, age) is None
    # decided on the last bar of the session: not fillable today
    assert intraday_spread_exit([[ib("15:25", 9.5)], [ib("15:25", 1.0)]], [1, -1], 2.0, 8.0, age) == ("STOP", None, None)


def test_intraday_exit_uses_a_leg_last_trade_only_while_it_is_fresh():
    age = timedelta(minutes=30)
    long_ = [ib("09:15", 1.0)]
    spike = [ib("09:15", 5.0), ib("09:40", 9.5), ib("09:45", 9.6)]
    # the bought leg last traded 25 minutes before 09:40: still usable, so the stop is seen and filled at 09:45
    r = intraday_spread_exit([spike, long_], [1, -1], 2.0, 8.0, age)
    assert r == ("STOP", spike[2].start, [9.6, 1.0])
    # 50 minutes old at 10:05: the cost is unknown and nothing is decided on it
    late = [ib("09:15", 5.0), ib("10:05", 9.5), ib("10:10", 9.6)]
    assert intraday_spread_exit([late, long_], [1, -1], 2.0, 8.0, age) is None
    # the look-ahead guard: a later bar never decides an earlier time (one bar, decision only)
    assert intraday_spread_exit([[ib("11:00", 3.0, 9.5)], [ib("11:00", 1.0)]], [1, -1], 2.0, 8.0, age) == ("STOP", None, None)


# ---------------- 5-minute history: parsing, paging, storage, budget ----------------
def raw5(d, hhmm, px=10.0):
    return {"datetime": f"{d} {hhmm}:00", "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 10,
            "open_interest": 1000}


def test_parse_option_rows_intraday_keeps_session_bars_at_their_own_time():
    rows = [raw5("2025-07-10", "09:00"), raw5("2025-07-10", "09:15"), raw5("2025-07-10", "15:25"),
            raw5("2025-07-10", "15:30"), raw5("2025-07-10", "10:00", px=-1)]
    good, bad = parse_option_rows(rows, intraday=True)
    assert [datetime.fromisoformat(g[0]).astimezone(IST).strftime("%H:%M") for g in good] == ["09:15", "15:25"]
    assert len(bad) == 1                                            # pre-open and post-close bars are dropped, not "bad"


class PagedFeed:
    """Serves one contract's 5-minute rows, at most `page` per reply, cut at the end or at the start."""
    def __init__(self, rows, page, cut="end", error_after=None):
        self.rows, self.page, self.cut, self.calls, self.error_after = rows, page, cut, [], error_after

    def option_historical_raw(self, code, expiry_iso, strike, right, interval, a, b):
        self.calls.append((interval, a, b))
        if self.error_after is not None and len(self.calls) > self.error_after:
            return {"Success": None, "Status": 401, "Error": "Session key is expired"}
        lo = datetime.fromisoformat(a.replace("Z", "+00:00"))
        hi = datetime.fromisoformat(b.replace("Z", "+00:00"))
        inside = [r for r in self.rows
                  if lo <= datetime.fromisoformat(r["datetime"]).replace(tzinfo=IST).astimezone(UTC) < hi]
        part = inside[:self.page] if self.cut == "end" else inside[-self.page:]
        return {"Success": part, "Status": 200, "Error": None}


def five_minute_rows(first: date, last: date, per_day=75):
    rows, d = [], first
    while d <= last:
        if d.weekday() < 5:
            t0 = datetime.combine(d, time(9, 15))
            rows += [raw5(d.isoformat(), (t0 + timedelta(minutes=5 * i)).strftime("%H:%M"), 10 + i * 0.01) for i in range(per_day)]
        d += timedelta(days=1)
    return rows


@pytest.mark.parametrize("cut", ["end", "start"])
def test_intraday_fetch_continues_a_reply_cut_at_the_row_limit(monkeypatch, cut):
    import app.options.option_history as oh
    monkeypatch.setattr(oh, "PAGE_ROWS", 300)
    monkeypatch.setattr(oh, "MAX_PAGES", 10)
    exp = date(2025, 7, 29)
    first, last = OptionIntraday.window(exp)
    rows = five_minute_rows(first, last)                            # 26 sessions x 75 bars = 1,950 rows
    feed = PagedFeed(rows, 300, cut)
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    got = f.fetch_intraday("RELIANCE", exp, 1100, "PE", first, last)
    assert len(got) == len(rows) and [g["datetime"] for g in got] == sorted(r["datetime"] for r in rows)
    assert len(feed.calls) == 7 and all(c[0] == "5minute" for c in feed.calls)        # 6 full pages and the short last one
    thin = PagedFeed(rows[:40], 300)
    f2 = oh.BreezeOptionFetcher(thin, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    assert len(f2.fetch_intraday("RELIANCE", exp, 1100, "PE", first, last)) == 40 and len(thin.calls) == 1


def test_option_intraday_stores_once_and_treats_days_outside_its_window_as_unknown():
    import app.options.option_history as oh
    exp = date(2025, 7, 29)
    first, last = OptionIntraday.window(exp)
    assert (first, last) == (date(2025, 6, 19), date(2025, 7, 24))
    feed = PagedFeed(five_minute_rows(date(2025, 7, 9), date(2025, 7, 10), per_day=3), 1000)
    store = Store(":memory:")
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    intr = OptionIntraday(OptionHistory(store, "breeze", f, now=NOW))
    day = intr.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 10))
    assert [b.start.astimezone(IST).strftime("%H:%M") for b in day] == ["09:15", "09:20", "09:25"]
    assert intr.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 11)) == []        # asked for: it did not trade
    assert intr.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 28)) is None      # outside the stored window: unknown
    assert intr.session("RELIANCE", exp, 1100, "PE", date(2025, 6, 18)) is None
    n = len(feed.calls)
    again = OptionIntraday(OptionHistory(store, "breeze", f, now=NOW))               # a later run: read from the store
    assert len(again.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 9))) == 3 and len(feed.calls) == n
    assert store.option_fetch_log("breeze", "RELIANCE", INTRADAY) == {(exp, 1100.0, "PE"): 6}
    # no fetcher: an unknown contract is None, and it is counted as wanted but not stored
    offline = OptionIntraday(OptionHistory(store, "breeze"))
    assert offline.session("RELIANCE", exp, 1120, "PE", date(2025, 7, 10)) is None
    assert offline.download_stats()["contracts_wanted_but_not_stored"] == 1


def test_a_breeze_error_stops_all_downloading_and_is_never_stored_as_no_data():
    import app.options.option_history as oh
    exp = date(2025, 7, 29)
    feed = PagedFeed([], 1000, error_after=0)
    store = Store(":memory:")
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    hist = OptionHistory(store, "breeze", f, now=NOW)
    intr = OptionIntraday(hist)
    assert intr.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 10)) is None
    assert "Session key is expired" in hist.error and len(feed.calls) == 1
    assert hist.contract("RELIANCE", exp, 1100, "PE") is None and len(feed.calls) == 1   # no further request at all
    assert store.option_fetch_log("breeze", "RELIANCE", INTRADAY) == {}
    assert store.option_fetch_log("breeze", "RELIANCE") == {}


def test_a_request_with_no_reply_is_repeated_but_an_error_reply_from_breeze_is_not():
    import app.options.option_history as oh

    class Flaky:
        def __init__(self, fail):
            self.fail, self.calls = fail, 0

        def option_historical_raw(self, *a):
            self.calls += 1
            if self.calls <= self.fail:
                raise TimeoutError("no reply within 60 s")
            return {"Success": [{"datetime": "2025-07-10 00:00:00", "open": 1, "high": 2, "low": 1, "close": 2,
                                 "volume": 1, "open_interest": 1}], "Status": 200, "Error": None}

    sleeps = []
    feed = Flaky(2)
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 3.0, 100, sleep=sleeps.append)
    assert len(f.fetch("RELIANCE", date(2025, 7, 29), 1100, "PE")) == 1
    assert feed.calls == 3 and f.requests == 3 and sleeps == [3.0, 3.0]        # two timeouts, then the reply; all counted
    dead = Flaky(99)
    f2 = oh.BreezeOptionFetcher(dead, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    hist = OptionHistory(Store(":memory:"), "breeze", f2, now=NOW)
    assert hist.contract("RELIANCE", date(2025, 7, 29), 1100, "PE") is None
    assert dead.calls == 3 and "TimeoutError" in hist.error and "after 3 attempts" in hist.error
    # an error reply (expired session) is not asked again: one request, and the download stops
    expired = PagedFeed([], 1000, error_after=0)
    f3 = oh.BreezeOptionFetcher(expired, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    hist3 = OptionHistory(Store(":memory:"), "breeze", f3, now=NOW)
    assert hist3.contract("RELIANCE", date(2025, 7, 29), 1100, "PE") is None
    assert len(expired.calls) == 1 and "Session key is expired" in hist3.error
    # the retries never go past the request budget
    f4 = oh.BreezeOptionFetcher(Flaky(99), {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 2, sleep=lambda x: None)
    with pytest.raises(oh.RequestBudgetUsed):
        f4.fetch("RELIANCE", date(2025, 7, 29), 1100, "PE")
    assert f4.requests == 2


def test_the_daily_request_budget_is_counted_across_runs_and_resets_the_next_day(s):
    store = Store(":memory:")
    small = settings(options={"backtest": {**s.options.backtest.model_dump(), "daily_request_budget": 5}})
    feed = PagedFeed([], 1000)
    day1 = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
    f = budgeted_fetcher(small, store, feed, now=day1, sleep=lambda x: None)
    assert f.max_requests == 5
    hist = OptionHistory(store, "breeze", f, now=NOW)
    for k in (1000, 1010):                                          # an empty reply is asked twice (one retry)
        assert hist.contract("RELIANCE", date(2025, 7, 29), k, "PE") == {}
    assert store.requests_used(date(2026, 11, 14)) == 4
    assert hist.contract("RELIANCE", date(2025, 7, 29), 1020, "PE") is None          # 1 left: the retry does not fit
    assert store.requests_used(date(2026, 11, 14)) == 5 and f.budget_left == 0
    assert budgeted_fetcher(small, store, feed, now=day1 + timedelta(hours=3), sleep=lambda x: None).max_requests == 0
    assert budgeted_fetcher(small, store, feed, run_limit=2, now=day1 + timedelta(days=1)).max_requests == 2
    assert budgeted_fetcher(small, store, feed, now=day1 + timedelta(days=1)).max_requests == 5
    assert store.requests_used(date(2026, 11, 15)) == 0


# ---------------- backtest world: a stock with stored daily and 5-minute option history ----------------
LOT_BT = {"RELIANCE": 500}
BT_FIRST, BT_LAST = date(2025, 3, 1), date(2025, 12, 31)


def weekdays(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def stock_world(materialise=True, shocks=None, drift=0.5, hi=1500):
    """`shocks`: {date: rupees} - the stock's price level shifts by that much from that session on."""
    cfg = settings(backtest={"test_start": {"1day": date(2025, 6, 2)}, "holdout_start": {"1day": date(2025, 10, 1)}},
                   options={"iv_rank_min_history_days": 20, "iv_rank_lookback_days": 60})
    store = Store(":memory:")
    bars = []
    for i, d in enumerate(weekdays(date(2024, 1, 1), date(2025, 12, 31))):
        px = 24000 + 60 * math.sin(i / 5)
        bars.append(Bar("NIFTY", daily_start(d), px - 10, px + 45, px - 45, px, None))
        v = 24 + i * 0.01
        bars.append(Bar("INDIAVIX", daily_start(d), v, v + 0.2, v - 0.2, v, None))
        sp = 1000 + drift * i + 8 * math.sin(i / 4) + sum(v for k, v in (shocks or {}).items() if d >= k)
        bars.append(Bar("RELIANCE", daily_start(d), sp - 1, sp + 6, sp - 6, sp, 2e6))
    store.save_history_bars("bars_1d", bars, "sim")
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    model = model_prices(cfg, base)
    for exp, _ in model.expiries("RELIANCE"):
        store.save_option_expiry("sim", "NIFTY", exp, "monthly", NOW)       # stocks expire with the index monthly
    if materialise:
        write_option_history(store, model, hi=hi)
    return cfg, store, base, model


def write_option_history(store, model, lo=900, hi=1500, step=10):
    """The model's daily bars, and three 5-minute bars a day (open, midday, close) inside the 5-minute
    window, written as if they had been downloaded."""
    for exp, _ in model.expiries("RELIANCE"):
        if not (BT_FIRST <= exp <= BT_LAST):
            continue
        w0, w1 = OptionIntraday.window(exp)
        for k in range(lo, hi + 1, step):
            for right in ("CE", "PE"):
                rows, five = [], []
                for d in weekdays(exp - timedelta(days=60), exp):
                    b = model.bar("RELIANCE", exp, k, right, d)
                    if b is None:
                        continue
                    rows.append((daily_start(d).isoformat(), b.open, b.high, b.low, b.close, 1000.0, 500000.0))
                    if w0 <= d <= w1:
                        for hh, mm, px in ((9, 15, b.open), (12, 0, round((b.open + b.close) / 2, 2)), (15, 25, b.close)):
                            at = datetime.combine(d, time(hh, mm), IST).astimezone(UTC)
                            five.append((at.isoformat(), px, px, px, px, 100.0, 500000.0))
                store.save_option_bars("sim", "RELIANCE", exp, k, right, "1day", rows, NOW)
                store.save_option_bars("sim", "RELIANCE", exp, k, right, INTRADAY, five, NOW)


@pytest.fixture(scope="module")
def world():
    return stock_world()


def engine_for(cfg, store, base, hist, with_intraday=True):
    return OptionEngine(cfg, store, "sim", hist, base, lot_sizes=LOT_BT,
                        intraday=OptionIntraday(hist) if with_intraday else None)


def run_bt(w, exit_checks=("intraday", "daily"), with_intraday=True, hist=None):
    cfg, store, base, _ = w
    hist = hist or OptionHistory(store, "sim")
    eng = engine_for(cfg, store, base, hist, with_intraday)
    eng.set_expiry_floor(BT_FIRST)                                  # option history is stored from here on
    return run_option_backtest(cfg, store, "sim", build_stock_selling(cfg), hist, ["RELIANCE"], engine=eng, save=False,
                               now=NOW, exit_checks=exit_checks)


def ist_date(text):
    return datetime.fromisoformat(text).astimezone(IST).date()


def test_ACCEPTANCE_stock_backtest_shows_strategy_and_baseline_side_by_side_under_both_exit_checks(world):
    res = run_bt(world)
    rep, trades = res["report"], res["trades"]
    assert rep["strategy_id"] == "stock_option_selling" and rep["price_source"] == "option_history"
    assert rep["payoff_simulation"] is False and rep["exit_check"] == "intraday"
    assert rep["eligibility_label"] == "excludes margin and bid/ask eligibility checks"
    both = rep["exit_check_comparison"]
    assert list(both) == ["intraday", "daily"]
    for mode in both:
        assert both[mode]["strategy"]["trades"] > 0 and both[mode]["baseline"]["trades"] > 0
        assert set(both[mode]["baseline_by_variant"]) == {"baseline:PE", "baseline:CE"}
        assert both[mode]["comparison"]["verdict"]
    assert rep["overall"] == both["intraday"]["strategy"] and rep["baseline"]["overall"] == both["intraday"]["baseline"]
    assert {t["exit_check"] for t in trades} == {"intraday", "daily"}
    assert {t["variant"] for t in trades} == {"strategy", "baseline:PE", "baseline:CE"}
    assert rep["holdout"]["evaluated"] is False and rep["period"]["to"] < "2025-10-01"
    assert rep["download"]["contracts_wanted_but_not_stored"] == 0
    # the baseline is the fixed schedule: one attempt per expiry and side
    sch = rep["baseline"]["schedule"]
    assert sch["attempts"] == sch["entered"] + len(sch["skipped"]) and sch["entered"] >= 4


def test_every_backtest_trade_is_hedged_enters_at_the_next_session_and_is_flat_before_expiry_week(world):
    cfg, _, base, _ = world
    res = run_bt(world)
    sessions = base.session_dates()
    assert res["trades"]
    for t in res["trades"]:
        assert unhedged_short_legs(t["legs"], t["expiry"], t["lots"], t["lot_size"]) == []
        sell, buy = (next(x for x in t["legs"] if x["action"] == a) for a in ("SELL", "BUY"))
        assert sell["right"] == buy["right"] and sell["expiry"] == buy["expiry"]
        assert t["symbol"] == "RELIANCE" and t["lot_size"] == 500 and t["lots"] == 1
        signal_d, entry_d, exit_d = ist_date(t["signal_time"]), ist_date(t["entry_time"]), ist_date(t["exit_time"])
        assert entry_d == sessions[sessions.index(signal_d) + 1]            # never the signal day
        exp = date.fromisoformat(t["expiry"])
        assert 20 <= (exp - signal_d).days <= 35
        assert exit_d < exp - timedelta(days=exp.weekday())                # before the Monday of expiry week
        assert (exp - exit_d).days >= cfg.stock_option_selling.exit_days_before_expiry - 3
        assert t["exit_reason"] in ("TARGET", "STOP", "TIME", "STRIKE_BREACHED", "PERIOD_END")
        if t["variant"] == "strategy":
            assert sell["right"] == "PE"                                    # the fixture stock is in an up trend
    # a forced (time) exit fills at 09:15 of the first session within 7 days of expiry
    timed = [t for t in res["trades"] if t["exit_reason"] == "TIME"]
    for t in timed:
        exp, exit_d = date.fromisoformat(t["expiry"]), ist_date(t["exit_time"])
        assert (exp - exit_d).days <= 7 and datetime.fromisoformat(t["exit_time"]).astimezone(IST).time() == time(9, 15)


def test_stock_backtest_is_deterministic(world):
    a, b = run_bt(world), run_bt(world)
    strip = lambda r: {k: v for k, v in r["report"].items() if k not in ("run_id", "created_utc")}
    assert a["trades"] == b["trades"] and strip(a) == strip(b)


def test_intraday_check_exits_on_a_5_minute_bar_where_the_daily_check_sees_nothing():
    """A stop touched at 10:00 on one day, with both legs trading, and gone again by the close. The
    5-minute check exits at the 10:05 open; the daily check never sees it and its trade is unchanged."""
    w = stock_world()
    cfg, store, _, _ = w
    before = run_bt(w)
    pick = next(t for t in before["trades"] if t["exit_check"] == "daily" and t["variant"] == "baseline:PE"
                and (ist_date(t["exit_time"]) - ist_date(t["entry_time"])).days >= 5)
    exp = date.fromisoformat(pick["expiry"])
    sell, buy = (next(x for x in pick["legs"] if x["action"] == a) for a in ("SELL", "BUY"))
    day = next(d for d in weekdays(ist_date(pick["entry_time"]) + timedelta(days=1), ist_date(pick["exit_time"])))
    credit = pick["entry_net"]
    stop = round(credit / 0.995 * 2.0, 2) + 5                       # well above 2x the signal credit

    def add(strike, rows):
        store.save_option_bars("sim", "RELIANCE", exp, strike, "PE", INTRADAY,
                               [(datetime.combine(day, time(h, m), IST).astimezone(UTC).isoformat(), px, px, px, px, 1.0, 1.0)
                                for h, m, px in rows], NOW)

    same = lambda r, mode: [t for t in r["trades"] if t["exit_check"] == mode and t["signal_id"] == pick["signal_id"]][0]
    # only the sold leg trades at 10:00; the bought leg's last trade (09:15) is 45 minutes old: cost unknown, no exit
    add(sell["strike"], [(10, 0, stop + 1.0), (10, 5, stop + 1.5)])
    mid = run_bt(w)
    assert same(mid, "intraday")["exit_time"] == same(before, "intraday")["exit_time"]
    # now the bought leg trades in those bars too: the stop is seen at the 10:00 close and filled at the 10:05 open
    add(buy["strike"], [(10, 0, 1.0), (10, 5, 1.1)])
    after = run_bt(w)
    hit, daily = same(after, "intraday"), same(after, "daily")
    assert hit["exit_reason"] == "STOP" and hit["exit_basis"] == "5-minute bar"
    assert datetime.fromisoformat(hit["exit_time"]).astimezone(IST) == datetime.combine(day, time(10, 5), IST)
    out = {x["strike"]: x["exit_price"] for x in hit["legs"]}
    assert out[sell["strike"]] == pytest.approx((stop + 1.5) * 1.005, abs=0.011)     # bought back at the open plus slippage
    assert out[buy["strike"]] == pytest.approx(1.1 * 0.995, abs=0.011)
    assert hit["net_pnl"] < 0
    assert daily == same(before, "daily")                                           # daily closes did not change
    assert after["report"]["exit_check_comparison"]["intraday"]["baseline"]["net_pnl_inr"] \
        < before["report"]["exit_check_comparison"]["intraday"]["baseline"]["net_pnl_inr"]


def test_days_without_stored_5_minute_bars_are_checked_on_the_daily_close_and_counted(world):
    with_bars = run_bt(world)
    without = run_bt(world, with_intraday=False)
    x = without["report"]["exit_check_comparison"]["intraday"]
    assert x["days_checked_on_daily_close_instead"] > 0
    assert with_bars["report"]["exit_check_comparison"]["intraday"]["days_checked_on_daily_close_instead"] == 0
    key = lambda t: (t["variant"], t["signal_id"], t["exit_time"], t["exit_reason"], t["net_pnl"])
    intraday = sorted(key(t) for t in without["trades"] if t["exit_check"] == "intraday")
    daily = sorted(key(t) for t in without["trades"] if t["exit_check"] == "daily")
    assert intraday == daily                                        # with no 5-minute bars at all it is the daily check


def test_strike_breach_exit_can_be_switched_off(world):
    cfg, store, base, _ = world
    on = build_stock_selling(cfg)
    off_cfg = Settings.model_validate({**cfg.model_dump(), "stock_option_selling": {
        **cfg.stock_option_selling.model_dump(), "backtest": {**cfg.stock_option_selling.backtest.model_dump(),
                                                              "exit_on_strike_breach": False}}})
    off = build_stock_selling(off_cfg)
    assert on.threat_exit is True and off.threat_exit is False
    hist = OptionHistory(store, "sim")
    eng = engine_for(cfg, store, base, hist)
    sessions = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    calls = eng.run_stream(on, "RELIANCE", sessions, "baseline:CE", Counter(), exit_check="daily")
    plain = eng.run_stream(off, "RELIANCE", sessions, "baseline:CE", Counter(), exit_check="daily")
    assert all(t.exit_reason != "STRIKE_BREACHED" for t in plain)
    # a bear call spread on a rising stock: where the breach rule fires, the sold strike was passed at that close
    closes = {daily_start(ist_date(b.start.isoformat())): b.close for b in base.daily("RELIANCE")}
    for t in calls:
        if t.exit_reason == "STRIKE_BREACHED":
            decided = max(d for d in base.session_dates() if d < ist_date(t.exit_time))
            assert closes[daily_start(decided)] > next(x["strike"] for x in t.legs if x["action"] == "SELL")


# ---------------- liquidity ranking ----------------
def test_liquid_stocks_rank_by_median_traded_value_and_leave_out_what_cannot_be_ranked():
    cfg = settings()
    store = Store(":memory:")
    days = weekdays_back(date(2025, 9, 30), 80)
    bars = [Bar("NIFTY", daily_start(d), 1, 1, 1, 1, None) for d in days]
    bars += [Bar("INDIAVIX", daily_start(d), 12, 12, 12, 12, None) for d in days]
    for sym, px, vol in (("RELIANCE", 1000, 3e6), ("TCS", 3000, 2e6), ("INFY", 1500, 1e6), ("HDFCBANK", 900, 5e6)):
        bars += [Bar(sym, daily_start(d), px, px, px, px, vol) for d in days]
    bars += [Bar("ICICIBANK", daily_start(d), 1000, 1000, 1000, 1000, 9e9) for d in days[-10:]]     # too short to rank
    store.save_history_bars("bars_1d", bars, "sim")
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    eng = OptionEngine(cfg, store, "sim", OptionHistory(store, "sim"), base,
                       lot_sizes={"RELIANCE": 500, "TCS": 175, "INFY": 400, "ICICIBANK": 700})      # HDFCBANK: no lot size
    ranked = liquid_stocks(cfg, eng)
    assert [r["stock"] for r in ranked] == ["TCS", "RELIANCE", "INFY"] and [r["rank"] for r in ranked] == [1, 2, 3]
    assert ranked[0]["median_traded_value_inr"] == 6e9
    assert liquid_stocks(cfg, eng, before=days[30]) == []            # fewer than 60 sessions before that date


# ---------------- download: budget, error, resume ----------------
class StoreFeed:
    """Stands in for Breeze: serves the option bars of a fully stocked store in Breeze's raw format."""
    def __init__(self, source_store, error_after=None):
        self.src, self.calls, self.error_after = source_store, [], error_after

    def option_historical_raw(self, code, expiry_iso, strike, right, interval, a, b):
        self.calls.append((expiry_iso[:10], float(strike), right, interval))
        if self.error_after is not None and len(self.calls) > self.error_after:
            return {"Success": None, "Status": 401, "Error": "Session key is expired"}
        assert code == "RELIND"
        r = {"call": "CE", "put": "PE"}[right]
        bars = self.src.option_contract_bars("sim", "RELIANCE", date.fromisoformat(expiry_iso[:10]), strike, r, interval)
        rows = [{"datetime": start.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"), "open": o, "high": h, "low": l,
                 "close": c, "volume": v, "open_interest": oi} for start, o, h, l, c, v, oi in bars]
        return {"Success": rows, "Status": 200, "Error": None}


def download_into(target, feed, limit=100000):
    import app.options.option_history as oh
    cfg, store, base, _ = target
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, limit, sleep=lambda x: None)
    hist = OptionHistory(store, "sim", f, now=NOW)
    eng = engine_for(cfg, store, base, hist)
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    return hist, eng, dev


def test_download_fills_an_empty_store_and_the_backtest_then_matches_the_full_one(world):
    empty = stock_world(materialise=False)
    feed = StoreFeed(world[1])
    hist, eng, dev = download_into(empty, feed)
    status = {}
    lines = []
    out = download_blocks(empty[0], eng, build_stock_selling(empty[0]), ["RELIANCE"], dev, status, progress=lines.append)
    assert out["stopped"] is None and status["complete"] == {"RELIANCE": [2025]} and out["requests_this_run"] == len(feed.calls)
    assert {c[3] for c in feed.calls} == {"1day", "5minute"} and lines and "complete" in lines[0]
    assert coverage(status, ["RELIANCE"], [2025]) == ([2025], ["RELIANCE"])
    key = lambda t: (t["variant"], t["exit_check"], t["signal_id"], t["exit_time"], t["exit_reason"], t["net_pnl"])
    assert sorted(map(key, run_bt(empty)["trades"])) == sorted(map(key, run_bt(world)["trades"]))
    # an offline recheck (after a run that could not record its progress) marks the same thing without a request
    offline_eng = engine_for(empty[0], empty[1], empty[2], OptionHistory(empty[1], "sim"))
    marks, saved = {}, []
    out3 = download_blocks(empty[0], offline_eng, build_stock_selling(empty[0]), ["RELIANCE", "TCS"], dev, marks,
                           progress=lambda x: None, offline=True, checkpoint=lambda st: saved.append(dict(st["complete"])))
    assert marks["complete"] == {"RELIANCE": [2025]} and out3["requests_this_run"] == 0      # TCS has nothing stored
    assert saved and saved[0] == {"RELIANCE": [2025]}                                        # recorded as it goes
    # a second run asks for nothing: everything stored (or asked for) is skipped
    n = len(feed.calls)
    hist2, eng2, _ = download_into(empty, feed)
    out2 = download_blocks(empty[0], eng2, build_stock_selling(empty[0]), ["RELIANCE"], dev, {}, progress=lambda x: None)
    assert len(feed.calls) == n and out2["requests_this_run"] == 0 and out2["stopped"] is None


def test_download_stops_cleanly_on_a_session_error_records_progress_and_resumes(world):
    empty = stock_world(materialise=False)
    feed = StoreFeed(world[1], error_after=25)
    hist, eng, dev = download_into(empty, feed)
    status = {}
    out = download_blocks(empty[0], eng, build_stock_selling(empty[0]), ["RELIANCE", "TCS"], dev, status, progress=lambda x: None)
    assert out["stopped"].startswith("Breeze error") and "Session key is expired" in out["stopped"]
    assert status["complete"] == {} and len(feed.calls) == 26       # the failing request was the last one sent
    assert coverage(status, ["RELIANCE"], [2025]) == ([], [])
    stored = sum(1 for n in empty[1].option_fetch_log("sim", "RELIANCE").values()) \
        + len(empty[1].option_fetch_log("sim", "RELIANCE", INTRADAY))
    assert 0 < stored <= 25                                         # what arrived before the error is kept
    # after a fresh login: the same command carries on and does not ask again for what is stored
    feed2 = StoreFeed(world[1])
    hist2, eng2, _ = download_into(empty, feed2)
    out2 = download_blocks(empty[0], eng2, build_stock_selling(empty[0]), ["RELIANCE"], dev, status, progress=lambda x: None)
    assert out2["stopped"] is None and status["complete"] == {"RELIANCE": [2025]}
    asked_before = set(feed.calls[:25])
    assert not (set(feed2.calls) & {c for c in asked_before if c[3] == "5minute"})


def test_download_stops_when_the_request_budget_is_used_and_marks_nothing_complete(world):
    empty = stock_world(materialise=False)
    feed = StoreFeed(world[1])
    hist, eng, dev = download_into(empty, feed, limit=30)
    status = {}
    out = download_blocks(empty[0], eng, build_stock_selling(empty[0]), ["RELIANCE", "TCS"], dev, status, progress=lambda x: None)
    assert len(feed.calls) == 30 and out["requests_this_run"] == 30
    assert out["stopped"] == "request budget used" and status["complete"] == {}
    rep = run_bt(empty, hist=OptionHistory(empty[1], "sim"))["report"]
    assert rep["download"]["contracts_wanted_but_not_stored"] > 0   # the report on partial data says it is incomplete


def test_coverage_uses_only_completely_downloaded_stocks_and_years():
    status = {"complete": {"A": [2024, 2025, 2026], "B": [2025, 2026], "C": [2026], "D": [2024]}}
    assert coverage(status, ["A", "B", "C", "D"], [2023, 2024, 2025, 2026]) == ([2024, 2025, 2026], ["A"])
    assert coverage(status, ["B", "C", "D"], [2024, 2025, 2026]) == ([2025, 2026], ["B"])
    assert coverage(status, ["C", "D"], [2024, 2025, 2026]) == ([2026], ["C"])
    assert coverage({}, ["A"], [2025]) == ([], [])
