"""Phase 6 M2, acceptance: an end-to-end test from fixture bars to a stored signal card - 1-minute bars and LIVE
ticks in, the live loop builds 5-minute bars, runs a strategy, the risk engine sizes it with the pool's account,
the card is stored and served by GET /api/signals, the signal is paper-traded at the NEXT bar's first quote, and a
restart neither duplicates nor revives it."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Bar, Tick
from app.strategies.base import Strategy, next_weekday_open_window
from tests.phase6_fixtures import app_client
from tests.test_step1 import sim_settings

IST_OFF = timedelta(hours=5, minutes=30)
DAY_OPEN = datetime(2026, 11, 16, 3, 45, tzinfo=timezone.utc)            # Monday 09:15 IST
BAR_END = DAY_OPEN + timedelta(minutes=75)                                # 10:30 IST


class BreakoutFixture(Strategy):
    """A fixture strategy: LONG when the last 5-minute close is above every earlier high of the session."""
    id = "orb_v2"                                 # a configured trigger/status id, so the card has a status label
    base_version = "fixture"
    interval = "5minute"
    allowed_regimes = frozenset({"TRENDING_UP"})

    def __init__(self):
        from pydantic import BaseModel

        class P(BaseModel):
            k: int = 1
        super().__init__(P())

    def _evaluate(self, ctx):
        bars = ctx.intraday
        if len(bars) < 3 or bars[-1].close <= max(b.high for b in bars[:-1]):
            return []
        last = bars[-1]
        return [self.signal(ctx, "LONG", last.close, last.close - 5.0, last.close + 10.0,
                            ctx.asof + timedelta(minutes=15), [f"close {last.close} above the session high"])]


class FixedRegime:
    def evaluate(self, now=None):
        return {"regime": "TRENDING_UP", "bias": "BULLISH"}


def seed(app, until=BAR_END):
    store, hub = app.state.store, app.state.hub
    t, px = DAY_OPEN, 1000.0
    while t < until:
        px += 0.5 if t < until - timedelta(minutes=5) else 6.0                # a breakout in the last bar
        store.save_bar(Bar("RELIANCE", t, px - 0.2, px + 0.3, px - 0.4, px, 1000, 10), "sim")
        store.save_bar(Bar("NIFTY", t, 24000, 24001, 23999, 24000, None, 10), "sim")
        t += timedelta(minutes=1)
    return px


def tick(app, at, px=None, symbol="RELIANCE"):
    app.state.hub.on_tick(Tick(symbol, px or 1036.0, bid=(px or 1036.0) - 0.1, ask=(px or 1036.0) + 0.1,
                               cum_volume=None, source="sim", received_at=at))
    app.state.hub.on_tick(Tick("NIFTY", 24000.0, source="sim", received_at=at))


def make(db=":memory:"):
    c = app_client(sim_settings(), db_path=db)
    eng = c.app.state.engine
    eng.cash, eng.swing = [BreakoutFixture()], []
    eng.regime = FixedRegime()
    eng.desk = type("NoOptions", (), {"underlyings": lambda self: []})()
    pin_equity_start(eng)
    return c, eng


def signals_at(c, at=BAR_END + timedelta(seconds=5)):
    """GET /api/signals with the app's clock at the fixture time: it lists the last 24 hours by the clock, and the
    fixture day is in the past, so the real clock would leave the fixture cards out from a day later."""
    from unittest import mock
    with mock.patch("app.main.utcnow", return_value=at):
        return c.get("/api/signals").json()


def pin_equity_start(eng):
    """The app writes each pool's starting equity at the REAL clock's yesterday; from the fixture day on that is not
    before the fixture day, and the drawdown check would find no equity history. Date one before it as well."""
    from app.journal import POOLS, pool_capital
    for pool in POOLS:
        eng.j.write_equity(pool, DAY_OPEN.date() - timedelta(days=3), pool_capital(eng.s, pool), 0.0, 0.0, DAY_OPEN,
                           "test start: the pool's capital before the fixture day")


def test_fixture_bars_to_a_stored_card_and_a_paper_trade(tmp_path):
    c, eng = make(str(tmp_path / "e2e.db"))
    with c:
        last = seed(c.app)
        now = BAR_END + timedelta(seconds=3)
        tick(c.app, BAR_END - timedelta(seconds=1), last)                    # the last quote of the SIGNAL bar
        out = eng.on_bar(BAR_END, now)
        assert out["signals"] == 1 and out["approved"] == 1, out
        sid = out["stored"][0]["signal_id"]
        r = signals_at(c)
        card = r["cards"][0]
        assert card["signal_id"] == sid and card["status"] == "APPROVED" and not card["collapsed"]
        for k in ("instrument", "direction", "entry_zone", "stop", "target", "quantity", "valid_until", "strategy_id",
                  "strategy_version", "regime", "data_as_of", "why_now", "estimate", "track_record", "score"):
            assert card[k] is not None, k
        assert card["estimate"]["label"] == "Estimate if target is hit, not a forecast"
        assert any("regime is TRENDING_UP" in w for w in card["why_now"])
        assert card["alert"]["allowed"] is False                     # switch off by default
        # paper trade: pending until the NEXT bar's first quote; the signal-time quote is never the fill
        paper = c.get("/api/paper").json()["open"]
        assert [p["status"] for p in paper] == ["PENDING"]
        eng.paper.fill_pending(now + timedelta(seconds=1), eng.signals)          # only a quote from the signal bar
        assert c.get("/api/paper").json()["open"][0]["status"] == "PENDING"
        tick(c.app, BAR_END + timedelta(seconds=40), last + 1.0)
        eng.paper.fill_pending(BAR_END + timedelta(seconds=41), eng.signals)
        p = c.get("/api/paper").json()["open"][0]
        assert p["status"] == "OPEN" and p["entry_price"] == pytest.approx(last + 1.1)  # the ask of the next bar's first quote
        detail = c.get(f"/api/signals/{sid}").json()
        moments = {(x["moment"], x["leg"]) for x in detail["execution"]}
        assert {("SIGNAL", 0), ("PLANNED_FILL", 0), ("PAPER_FILL", 0)} <= moments


def test_restart_does_not_duplicate_or_revive(tmp_path):
    db = str(tmp_path / "restart.db")
    c, eng = make(db)
    with c:
        last = seed(c.app)
        now = BAR_END + timedelta(seconds=3)
        tick(c.app, now - timedelta(seconds=1), last)
        assert eng.on_bar(BAR_END, now)["signals"] == 1
    c2, eng2 = make(db)                                                     # restart mid-session, same database
    with c2:
        restored = eng2.restore(now)
        assert restored["unexpired_signals"] == 1 and restored["pending_paper"] == 1
        tick(c2.app, now, last)
        eng2.last_bar = None
        assert eng2.on_bar(BAR_END, now + timedelta(seconds=5))["signals"] == 0      # same bar again: not issued twice
        later = BAR_END + timedelta(minutes=30)                                       # past valid-until
        r = signals_at(c2)
        assert len(r["cards"]) == 1
        from app.cards import card_view
        assert card_view(r["cards"][0], later)["expired"] is True and not card_view(r["cards"][0], later)["actionable"]


def test_kill_switch_makes_every_signal_no_trade():
    c, eng = make()
    with c:
        last = seed(c.app)
        now = BAR_END + timedelta(seconds=3)
        c.app.state.hub.on_tick(Tick("RELIANCE", last, bid=last - 0.1, ask=last + 0.1, source="sim",
                                     received_at=now - timedelta(seconds=1)))
        c.app.state.hub.on_tick(Tick("NIFTY", 23500.0, source="sim", received_at=now - timedelta(minutes=10)))  # STALE
        out = eng.on_bar(BAR_END, now)
        assert out["signals"] == 1 and out["approved"] == 0
        card = signals_at(c)["cards"][0]
        assert card["collapsed"] and "KILL_SWITCH" in card["reason_codes"]
        assert c.get("/api/paper").json()["open"] == []                   # NO_TRADE is never paper-traded
