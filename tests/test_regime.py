"""Regime rules on fixture data: every regime, UNKNOWN on missing data, determinism."""
import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from app.calendar import MarketCalendar
from app.config import Regime as RegimeParams
from app.config import Settings, load_settings
from app.hub import MarketHub
from app.manual_data import ManualData
from app.regime import RegimeService, classify, fii_bias, rules_version
from app.store import Store
from tests.phase2_fixtures import (SESSION, T0, live_ticks, make_inputs, settings_with_tmp_data,
                                   store_history)

P = RegimeParams()


# ---------------- each regime ----------------
def test_trending_up():
    r = classify(make_inputs("up"), P)
    assert r.regime == "TRENDING_UP"
    assert any("above both" in x for x in r.reasons)
    assert r.values["breadth_pct"] == 80.0 and r.values["atr"] == 100.0


def test_trending_down():
    assert classify(make_inputs("down"), P).regime == "TRENDING_DOWN"


def test_range_when_price_between_averages():
    r = classify(make_inputs("flat"), P)
    assert r.regime == "RANGE" and "between the averages" in r.reasons[0]


def test_range_when_breadth_disagrees_with_trend():
    from tests.phase2_fixtures import stock_data
    r = classify(make_inputs("up", stocks=stock_data(n_above=2)), P)   # 40% < 60%
    assert r.regime == "RANGE" and "below 60%" in r.reasons[0]


def test_volatile_on_high_vix():
    r = classify(make_inputs("up", vix_now=25.0), P)
    assert r.regime == "VOLATILE" and "India VIX 25.00" in r.reasons[0]


def test_volatile_on_wide_day_range():
    inp = make_inputs("up")
    inp = replace(inp, index_day_high=inp.index_price + 100, index_day_low=inp.index_price - 60)
    r = classify(inp, P)                               # range 160 > 1.5 x ATR 100
    assert r.regime == "VOLATILE" and "1.60x" in r.reasons[0]


def test_range_of_exactly_limit_is_not_volatile():
    inp = make_inputs("up")
    inp = replace(inp, index_day_high=inp.index_price + 75, index_day_low=inp.index_price - 75)
    assert classify(inp, P).regime == "TRENDING_UP"   # 150 is not above 150


def test_event_day_forces_event_even_when_volatile():
    ev = [{"type": "RBI_POLICY", "symbol": "", "note": "policy decision"}]
    r = classify(make_inputs("up", vix_now=25.0, events=ev), P)
    assert r.regime == "EVENT" and "RBI_POLICY" in r.reasons[0]


def test_single_stock_results_do_not_force_market_event():
    ev = [{"type": "RESULTS", "symbol": "INFY", "note": "Q3"}]
    r = classify(make_inputs("up", events=ev), P)
    assert r.regime == "TRENDING_UP"
    assert r.values["events"] == ["RESULTS INFY: Q3"]


def test_event_type_not_in_config_does_not_force():
    ev = [{"type": "EXPIRY", "symbol": "", "note": ""}]
    params = RegimeParams(event_types=["BUDGET"])
    assert classify(make_inputs("up", events=ev), params).regime == "TRENDING_UP"
    assert classify(make_inputs("up", events=ev), P).regime == "EVENT"


# ---------------- UNKNOWN, never a guess ----------------
@pytest.mark.parametrize("change, text", [
    ({"vix_now": None}, "India VIX is not LIVE"),
    ({"vix_history": [12.0] * 50}, "India VIX has 50 daily closes"),
    ({"index_price": None}, "NIFTY price is not LIVE"),
    ({"index_day_high": None}, "day high/low is unknown"),
    ({"events_error": "2 invalid row(s)"}, "event calendar could not be read"),
    ({"data_problems": ["NIFTY daily history ends 2026-10-01"]}, "history ends"),
])
def test_missing_input_gives_unknown(change, text):
    r = classify(make_inputs("up", **change), P)
    assert r.regime == "UNKNOWN"
    assert any(text in x for x in r.reasons), r.reasons


def test_missing_history_gives_unknown():
    inp = make_inputs("up")
    r = classify(replace(inp, index_history=inp.index_history[-30:]), P)
    assert r.regime == "UNKNOWN" and "30 completed daily bars; 50 needed" in r.reasons[0]
    assert classify(replace(inp, index_history=[], vix_history=[]), P).regime == "UNKNOWN"


def test_low_breadth_coverage_gives_unknown():
    from tests.phase2_fixtures import stock_data
    r = classify(make_inputs("up", stocks=stock_data(n_above=3, missing=2)), P)   # 3 of 5 = 60%
    assert r.regime == "UNKNOWN" and "breadth covers 3 of 5" in r.reasons[0]


def test_missing_data_beats_event_day():
    ev = [{"type": "BUDGET", "symbol": "", "note": ""}]
    r = classify(make_inputs("up", vix_now=None, events=ev), P)
    assert r.regime == "UNKNOWN" and any("event day" in x for x in r.reasons)


# ---------------- determinism ----------------
@pytest.mark.parametrize("trend", ["up", "down", "flat"])
def test_same_input_same_regime(trend):
    inp = make_inputs(trend)
    first = classify(inp, P)
    for _ in range(5):
        again = classify(copy.deepcopy(inp), P)
        assert (again.regime, again.reasons, again.values, again.rules_version) == \
               (first.regime, first.reasons, first.values, first.rules_version)


def test_stock_order_does_not_matter():
    inp = make_inputs("up")
    reordered = replace(inp, stocks=dict(reversed(list(inp.stocks.items()))))
    assert classify(reordered, P).reasons == classify(inp, P).reasons


def test_rules_version_tracks_thresholds():
    assert rules_version(P).startswith("regime_v1/")
    assert rules_version(P) == rules_version(RegimeParams())
    assert rules_version(P) != rules_version(RegimeParams(volatile_range_atr_mult=2.0))


def test_config_rejects_inconsistent_thresholds():
    with pytest.raises(ValueError):
        RegimeParams(sma_fast_days=50, sma_slow_days=20)
    with pytest.raises(ValueError):
        RegimeParams(breadth_up_pct=40, breadth_down_pct=60)
    with pytest.raises(ValueError):
        RegimeParams(event_types=["PARTY"])
    raw = load_settings().model_dump()
    raw["regime"]["vix_symbol"] = "NOTLISTED"
    with pytest.raises(ValueError):
        Settings.model_validate(raw)


# ---------------- FII/DII bias ----------------
CAL = MarketCalendar()


def rows(*nets, end=date(2026, 11, 10)):
    """Most recent first, on consecutive trading days ending at `end`."""
    out, d = [], end
    for v in nets:
        out.append({"date": d, "fii_net_cr": v, "dii_net_cr": None})
        d = CAL.previous_trading_day(d)
    return out


def test_bias_bearish_after_three_selling_days():
    bias, why = fii_bias(rows(-100, -2500.5, -10), SESSION, CAL, 3)
    assert bias == "BEARISH" and "net sellers for 3" in why[0]


def test_bias_bullish_after_three_buying_days():
    assert fii_bias(rows(10, 20, 30), SESSION, CAL, 3)[0] == "BULLISH"


@pytest.mark.parametrize("nets", [(-10, -20, 30), (10, -20, 30), (0, 10, 10)])
def test_bias_neutral_without_streak(nets):
    assert fii_bias(rows(*nets), SESSION, CAL, 3)[0] == "NEUTRAL"


def test_bias_unknown_when_yesterday_missing():
    bias, why = fii_bias(rows(-10, -20, -30, end=date(2026, 11, 9)), SESSION, CAL, 3)
    assert bias == "UNKNOWN" and "2026-11-10" in why[0]
    assert fii_bias([], SESSION, CAL, 3)[0] == "UNKNOWN"


def test_bias_skips_weekends_and_holidays():
    # Session Mon 2026-11-16; Fri 13th is a holiday, so the streak is Thu 12, Wed 11, Tue 10.
    cal = MarketCalendar({date(2026, 11, 13): "test holiday"})
    data = [{"date": date(2026, 11, d), "fii_net_cr": -5.0, "dii_net_cr": 1.0} for d in (12, 11, 10)]
    assert fii_bias(data, date(2026, 11, 16), cal, 3)[0] == "BEARISH"
    assert fii_bias(data, date(2026, 11, 16), MarketCalendar(), 3)[0] == "UNKNOWN"


# ---------------- RegimeService: stored history + live ticks ----------------
def service(tmp_path, feed_name="breeze", holidays=None):
    s = settings_with_tmp_data(tmp_path)
    store = Store(":memory:")
    cal = MarketCalendar()
    manual = ManualData(s, store, cal)
    manual.load_all()                     # empty tmp holidays.csv
    cal.set_holidays(holidays or {})
    hub = MarketHub(s, store, feed_name, cal)
    return RegimeService(s, store, hub, cal, manual), store, hub, cal


def test_service_live_trending_up(tmp_path):
    svc, store, hub, cal = service(tmp_path)
    store_history(store, cal, SESSION)
    live_ticks(hub, T0, nifty=22700.0)
    out = svc.evaluate(T0 + timedelta(seconds=5))
    assert out["basis"] == "LIVE" and out["session_date"] == "2026-11-11"
    assert out["regime"] == "TRENDING_UP", out["reasons"]


def test_service_missing_vix_tick_is_unknown(tmp_path):
    svc, store, hub, cal = service(tmp_path)
    store_history(store, cal, SESSION)
    live_ticks(hub, T0, nifty=22700.0, skip=("INDIAVIX",))
    out = svc.evaluate(T0 + timedelta(seconds=5))
    assert out["regime"] == "UNKNOWN" and any("India VIX is not LIVE" in r for r in out["reasons"])


def test_service_stale_prices_are_unknown(tmp_path):
    svc, store, hub, cal = service(tmp_path)
    store_history(store, cal, SESSION)
    live_ticks(hub, T0, nifty=22700.0)
    assert svc.evaluate(T0 + timedelta(seconds=90))["regime"] == "UNKNOWN"   # older than 30 s


def test_service_without_history_is_unknown(tmp_path):
    svc, store, hub, cal = service(tmp_path)
    live_ticks(hub, T0, nifty=22700.0)
    out = svc.evaluate(T0 + timedelta(seconds=5))
    assert out["regime"] == "UNKNOWN"
    assert any("0 completed daily bars" in r for r in out["reasons"])


def test_service_outdated_history_is_unknown(tmp_path):
    svc, store, hub, cal = service(tmp_path)
    store_history(store, cal, date(2026, 11, 4))      # history stops a week early
    live_ticks(hub, T0, nifty=22700.0)
    out = svc.evaluate(T0 + timedelta(seconds=5))
    assert out["regime"] == "UNKNOWN" and any("history ends" in r for r in out["reasons"])


def test_service_event_in_store_forces_event(tmp_path):
    svc, store, hub, cal = service(tmp_path)
    store_history(store, cal, SESSION)
    store.replace_events([("2026-11-11", "EXPIRY", "", "NIFTY weekly expiry")])
    live_ticks(hub, T0, nifty=22700.0)
    out = svc.evaluate(T0 + timedelta(seconds=5))
    assert out["regime"] == "EVENT" and "EXPIRY: NIFTY weekly expiry" in out["reasons"][0]


def test_service_after_close_uses_last_close_for_next_session(tmp_path):
    svc, store, hub, cal = service(tmp_path)
    store_history(store, cal, date(2026, 11, 12))     # includes today's (11th) completed bar
    evening = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)   # 17:30 IST
    out = svc.evaluate(evening)
    assert out["basis"] == "LAST_CLOSE" and out["basis_date"] == "2026-11-11"
    assert out["session_date"] == "2026-11-12"
    assert out["regime"] != "UNKNOWN", out["reasons"]


def test_service_on_holiday_is_closed_and_plans_next_session(tmp_path):
    svc, store, hub, cal = service(tmp_path, holidays={SESSION: "test holiday"})
    store_history(store, cal, SESSION)
    out = svc.evaluate(T0)
    assert out["basis"] == "LAST_CLOSE" and out["holiday"] == "test holiday"
    assert out["session_date"] == "2026-11-12" and out["basis_date"] == "2026-11-10"


def test_regime_log_written_only_on_change(tmp_path):
    svc, store, hub, cal = service(tmp_path)
    store_history(store, cal, SESSION)
    live_ticks(hub, T0, nifty=22700.0)
    for i in range(3):
        svc.evaluate(T0 + timedelta(seconds=1 + i))
    assert len(store.regime_history()) == 1
    live_ticks(hub, T0 + timedelta(seconds=10), nifty=22700.0, vix=30.0)
    # VIX 12 -> 30 is a big jump; the quality gate quarantines it, so the regime does not change
    svc.evaluate(T0 + timedelta(seconds=11))
    assert len(store.regime_history()) == 1
    live_ticks(hub, T0 + timedelta(seconds=200), nifty=22700.0, vix=30.0)   # after the jump window
    out = svc.evaluate(T0 + timedelta(seconds=201))
    assert out["regime"] == "VOLATILE"
    hist = store.regime_history()
    assert [h["regime"] for h in hist] == ["VOLATILE", "TRENDING_UP"]
    assert hist[0]["rules_version"].startswith("regime_v1/")
