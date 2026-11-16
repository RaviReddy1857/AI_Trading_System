"""Phase 6 M2, steps 2-2c and 7: the signal card (every field; NO_TRADE collapsed; 'Why now'; the rupee estimate
labelled "not a forecast"; past results with their sample size, "low sample" under 30, no hit rate without trades;
expired cards greyed and not actionable) and the 0-100 score from confluence (each factor by hand; never a
probability; unscored signals ranked last)."""
from datetime import timedelta

import pytest

from app.cards import ESTIMATE_LABEL, build_card, card_view
from app.config import Scoring
from app.scoring import ScoreInputs, factor_points, rank, score
from app.track import backtest_record, paper_record, type_record
from tests.phase6_fixtures import NOW, T0, approved, bull_put, cash_signal, new_journal, no_trade, settings

TRACK = {"backtest": {"trades": 0}, "paper": {"trades": 0}, "label": "x"}


def card(sig, dec, **kw):
    return build_card(sig, dec, score={"score": 60.0}, regime="TRENDING_UP", bias="BULLISH",
                      events_week=kw.pop("events", []), track=TRACK, alert={"allowed": False, "why": "switch off"},
                      now=NOW, **kw)


def test_approved_cash_card_fields_and_estimate_by_hand():
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0, entry_zone=(99.8, 100.2))
    c = card(sig, approved(quantity=10, risk=70.0, profit=80.0, costs=20.0),
             events=[{"date": "2026-11-18", "type": "RESULTS", "symbol": "RELIANCE"}])
    for k in ("instrument", "direction", "entry_zone", "stop", "target", "quantity", "valid_until", "strategy_id",
              "strategy_version", "regime", "data_as_of"):
        assert c[k] not in (None, ""), k
    assert c["entry_zone"] == [99.8, 100.2] and not c["collapsed"] and c["quantity"] == 10
    assert c["why_now"][0] == "fixture" and "market regime is TRENDING_UP" in c["why_now"]
    assert "FII bias is BULLISH" in c["why_now"] and any("2026-11-18 RESULTS RELIANCE" in w for w in c["why_now"])
    e = c["estimate"]
    assert e["label"] == ESTIMATE_LABEL == "Estimate if target is hit, not a forecast"
    assert (e["profit_if_target_inr"], e["loss_if_stop_inr"], e["costs_inr"], e["risk_reward"]) == (80.0, 70.0, 20.0, 1.14)
    assert e["breakevens"] == [102.0]                                         # 100 + Rs 20 / 10 shares


def test_option_selling_estimate_shows_premium_collected():
    sig = bull_put(sold=(24000, 60.0), bought=(23900, 30.0), lots=2)
    c = card(sig, approved(quantity=150, lots=2, risk=10000.0, profit=2000.0, costs=160.0, margin=80000.0))
    e = c["estimate"]
    assert e["premium_collected_inr"] == 4500.0 and e["max_profit_inr"] == 4340.0      # 30 x 150 - 160
    assert c["lots"] == 2 and c["trade_type"] == "INDEX_OPTION_SELLING"


def test_no_trade_card_is_collapsed_with_every_reason():
    c = card(cash_signal(), no_trade("DATA_STALE", "DAILY_STOP"))
    assert c["collapsed"] and c["estimate"] is None and c["quantity"] is None
    assert c["reason_codes"] == ["DATA_STALE", "DAILY_STOP"] and len(c["reasons"]) == 2


def test_expired_card_is_greyed_and_cannot_be_acted_on():
    sig = cash_signal()
    c = card(sig, approved())
    assert card_view(c, sig.valid_until - timedelta(seconds=1))["actionable"] is True
    v = card_view(c, sig.valid_until)
    assert v["expired"] is True and v["actionable"] is False
    assert card_view(c, NOW, {"decision": "SKIPPED"})["actionable"] is False


# ---------------- track records ----------------
def _run(trades, net=-100.0, wr=40.0, source="breeze"):
    return {"run_id": "r1", "version": "v", "report": {"data_source": source,
                                                         "overall": {"trades": trades, "win_rate_pct": wr, "net_pnl_inr": net}}}


def test_backtest_record_low_sample_boundary():
    assert backtest_record(_run(29), 30)["low_sample"] is True
    r = backtest_record(_run(30, net=300.0), 30)
    assert r["low_sample"] is False and r["avg_inr"] == 10.0 and r["win_rate_pct"] == 40.0
    none = backtest_record(None, 30)
    assert none["trades"] == 0 and none["win_rate_pct"] is None and "no hit rate" in none["note"]
    assert backtest_record(_run(40, source="sim"), 30)["note"].startswith("SIMULATED DATA")


def test_paper_record_and_type_label():
    s = settings()
    j, store = new_journal(s)
    for k, net in enumerate([100.0, -50.0, 25.0]):
        j.upsert_paper({"signal_id": f"s{k}", "trade_type": "SWING_STOCKS", "pool": "CASH_INDEX", "symbol": "INFY",
                        "direction": "LONG", "quantity": 1, "status": "CLOSED", "net_pnl_inr": net,
                        "strategy_id": "swing_pullback_v2", "planned_fill_utc": T0.isoformat()})
    r = paper_record(j, ("swing_pullback_v2",), 30)
    assert (r["trades"], r["win_rate_pct"], r["net_inr"], r["low_sample"]) == (3, 66.7, 75.0, True)
    t = type_record(store, j, s, "SWING_STOCKS")
    assert t["paper"]["trades"] == 3 and "negative in backtests" in t["label"]
    empty = type_record(store, j, s, "INDEX_OPTION_BUYING")
    assert empty["label"] == "UNPROVEN: no track record yet"


# ---------------- score ----------------
CFG = Scoring()


def base(**kw):
    d = dict(direction="LONG", entry=100.0, target=110.0, closes_5m=[100.0] * 11 + [101.0],
             closes_15m=[100.0] * 7 + [101.0], daily_closes=[90.0] * 19 + [100.0], daily_highs=[95.0] * 20,
             daily_lows=[85.0] * 20, bar_volume=150.0, avg_volume=100.0, stock_return_pct=1.0, index_return_pct=0.5,
             peer_returns_pct=[0.0, 0.2, 0.4], regime="TRENDING_UP", bias="BULLISH")
    d.update(kw)
    return ScoreInputs(**d)


def test_each_factor_by_hand():
    f = {k: v for k, (v, _) in factor_points(base(), CFG).items()}
    assert f["timeframes"] == 100.0                                    # all three above their averages
    assert f["volume"] == pytest.approx(66.7, abs=0.05)                # (1.5 - 0.5) / 1.5 x 100
    # vs NIFTY +0.5, vs peers' median 0.2 -> +0.8; average 0.65 -> 50 + 6.5
    assert f["relative_strength"] == 56.5
    assert f["regime_fit"] == 100.0 and f["fii_bias"] == 100.0
    assert f["room_to_level"] == 100.0                                 # no 20-day high above the entry
    f2 = {k: v for k, (v, _) in factor_points(base(daily_highs=[104.0] * 20), CFG).items()}
    assert f2["room_to_level"] == 40.0                                 # (104 - 100) / (110 - 100)
    short = {k: v for k, (v, _) in factor_points(base(direction="SHORT", target=90.0), CFG).items()}
    assert short["timeframes"] == 0.0 and short["fii_bias"] == 0.0 and short["regime_fit"] == 0.0


def test_score_is_the_weighted_mean_of_known_factors_and_never_a_probability():
    out = score(base(bar_volume=None), CFG)
    assert "volume" in out["unknown"] and out["score"] == round((100 + 56.5 + 100 + 100 + 100) / 5, 1)
    assert "never a probability" in out["note"]
    few = score(base(closes_5m=[], bar_volume=None, peer_returns_pct=[], regime="UNKNOWN", bias="UNKNOWN",
                     daily_highs=[]), CFG)
    assert few["score"] is None and "not scored" in few["note"]


def test_rank_puts_unscored_last_and_is_deterministic():
    rows = [{"signal_id": "b", "score": None}, {"signal_id": "c", "score": 50}, {"signal_id": "a", "score": 50},
            {"signal_id": "d", "score": 80}]
    assert [r["signal_id"] for r in rank(rows)] == ["d", "a", "c", "b"]
