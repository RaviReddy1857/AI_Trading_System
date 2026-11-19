"""Phase 6 step 1: the live signal loop (paper mode). Nothing is ever sent to the broker.

On every closed 5-minute bar during market hours: read the regime, run the strategies allowed in it (cash
strategies on 5-minute bars built from the stored 1-minute bars; swing strategies once a session, at its first
bar, on the completed daily bars; phase 4 option strategies through the Options page's live context, long options
only with the underlying's own live signal as the trigger), pass every signal through the risk engine with the
account state of its own capital pool, score it, build its card, and store it (APPROVED or NO_TRADE with the
reasons). Every approved signal is paper-traded. Then every open position I hold is re-evaluated for exit calls.

Signals are deterministic: the id comes from the strategy, version, symbol, direction and bar time, and the store
ignores a second copy, so a restart or a repeated bar never issues a signal twice. A signal equal to one still
valid (same strategy, symbol, direction and legs) is not issued again. Expired signals are never revived: a stored
signal is shown past its valid-until as expired, and the risk engine refuses it (SIGNAL_EXPIRED).

Session hours come from the dated session calendar (app/session.py). Bars are processed until the last segment closes
(15:30 before 3 Aug 2026; 15:40 after, for option exits). New signals are made only on bars that close inside the
index's continuous trading (never in the closing auction); a cash intraday signal only on a bar closing before the
intraday cutoff of the day (dated: `backtest.intraday_exit_time` 15:15 before 3 Aug 2026, the session period's
`cash_intraday_cutoff` 15:00 after) and inside its stock's continuous trading. After the
last close the session is closed (closing equity): from 3 Aug 2026 a held position in an F&O stock waits for Breeze's
daily bar, the official closing-auction price, until `stock_option_selling.after_close.bars_deadline`; without it
the close is written from the last continuous trade and labelled APPROXIMATE.
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

from .cards import build_card
from .execution import SIGNAL
from .journal import (CASH_INDEX, POOLS, STOCK_OPTIONS, close_equity_day, ist_day, pool_of_trade_type,
                      record_marks, signal_trade_type)
from .models import Bar, Signal, utcnow
from .paper import FIVE, bar_of
from .positions import ExitInputs, evaluate_positions
from .quality import IST, MARKET_OPEN
from .session import INDEX, at_utc, calendar as session_calendar, segment_for
from .scoring import ScoreInputs, rank, score
from .strategies.base import MarketContext

log = logging.getLogger("engine")


def floor_bar(now: datetime) -> datetime:
    """The latest 5-minute boundary at or before `now` (a bar CLOSES there)."""
    t = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return t - timedelta(minutes=t.minute % 5)


def session_open_utc(day: date) -> datetime:
    return datetime.combine(day, MARKET_OPEN, IST).astimezone(timezone.utc)


def five_minute_bars(bars_1m: list[Bar], symbol: str, day: date, until: datetime) -> list[Bar]:
    """Completed 5-minute bars of a session from its 1-minute bars (a bucket ends at or before `until`)."""
    out = []
    t = session_open_utc(day)
    while t + FIVE <= until:
        b = bar_of(bars_1m, symbol, t)
        if b is not None:
            out.append(b)
        t += FIVE
    return out


def signal_from_dict(d: dict) -> Signal:
    from .options.payoff import OptionLeg
    legs = tuple(OptionLeg(x["action"], x["right"], float(x["strike"]), date.fromisoformat(x["expiry"][:10]),
                           int(x["lots"]), float(x["price"]), int(x["lot_size"])) for x in d["legs"]) if d.get("legs") else None
    return Signal(signal_id=d["signal_id"], created_at=datetime.fromisoformat(d["created_at"]),
                  strategy_id=d["strategy_id"], strategy_version=d["strategy_version"], symbol=d["symbol"],
                  asset_class=d["asset_class"], direction=d["direction"], horizon=d["horizon"], entry=d["entry"],
                  stop=d["stop"], target=d["target"], valid_until=datetime.fromisoformat(d["valid_until"]),
                  regime=d["regime"], why_now=tuple(d["why_now"]), data_as_of=datetime.fromisoformat(d["data_as_of"]),
                  entry_zone=tuple(d["entry_zone"]) if d.get("entry_zone") else None, legs=legs, details=d.get("details"))


def legs_key(sig: Signal) -> tuple:
    return (sig.strategy_id, sig.symbol, sig.direction,
            tuple((x.action, x.right, x.strike, x.expiry) for x in sig.legs) if sig.legs else ())


class SignalEngine:
    def __init__(self, *, settings, store, hub, journal, marker, risk, regime, calendar, desk, source, costs,
                 execbook, paper, exits, now_fn=utcnow, notifier=None, strategies=None):
        self.s, self.store, self.hub, self.j, self.m = settings, store, hub, journal, marker
        self.risk, self.regime, self.cal, self.desk, self.source = risk, regime, calendar, desk, source
        self.costs, self.x, self.paper, self.exits, self.now_fn, self.notifier = (costs, execbook, paper, exits,
                                                                                  now_fn, notifier)
        self.problems: list[str] = []
        if strategies is None:
            from .strategies import build_strategies
            try:
                strategies = build_strategies(settings)
            except Exception as e:                       # missing cost rates: no cash strategy can run (shown)
                strategies = []
                self.problems.append(f"cash strategies not running: {e}")
        self.cash = [st for st in strategies if st.interval == "5minute"]
        self.swing = [st for st in strategies if st.interval == "1day"]
        self.sectors = {i.symbol: i.sector for i in settings.watchlist}
        self.kinds = {i.symbol: i.kind for i in settings.watchlist}
        last = journal.get_state("last_bar_utc")
        self.last_bar = datetime.fromisoformat(last) if last else None
        self.signals: dict[str, Signal] = {}
        self._corr = None
        self._corr_day = None
        self.last_summary: dict = {}
        self._noticed_day, self._noticed = None, set()      # held-price notices sent this trading date (memory only)

    # ---- restart ----
    def restore(self, now: datetime) -> dict:
        """Reload what a restart must not lose: unexpired signals (for pending paper fills and duplicates), and the
        pending paper trade of an unexpired approved signal that has none (a database written before signals and
        paper trades were saved in one transaction). Open positions, switches, exit calls and unconfirmed alerts
        live in the database and are read on every bar."""
        from .models import RiskDecision
        n = recreated = 0
        for row in self.j.unexpired_signals(now):
            sig = signal_from_dict(row["signal"])
            self.signals[row["signal_id"]] = sig
            n += 1
            if row["status"] == "APPROVED" and self.j.paper_trade(row["signal_id"]) is None:
                d = row["decision"]
                dec = RiskDecision(status="APPROVED", reason_codes=(), quantity=d.get("quantity"), lots=d.get("lots"),
                                   risk_inr=d.get("risk_inr"), risk_pct=d.get("risk_pct"),
                                   est_profit_inr=d.get("est_profit_inr"), est_loss_inr=d.get("est_loss_inr"),
                                   costs_inr=d.get("costs_inr"), margin_inr=d.get("margin_inr"),
                                   capital_fit=d.get("capital_fit"))
                if self.paper.open(sig, dec, now) is not None:
                    recreated += 1
        for row in self.j.paper_trades(status="PENDING"):
            if row["signal_id"] not in self.signals:
                r = self.j.signal(row["signal_id"])
                if r:
                    self.signals[row["signal_id"]] = signal_from_dict(r["signal"])
        return {"unexpired_signals": n, "paper_trades_recreated": recreated, "open_positions": len(self.j.positions("OPEN")),
                "pending_paper": len(self.j.paper_trades(status="PENDING"))}

    # ---- the loop ----
    def step(self, now: datetime | None = None) -> dict | None:
        """One pass of the loop (called every `engine.poll_seconds`). Returns the bar summary when a bar closed."""
        now = now or self.now_fn()
        day = ist_day(now)
        self.paper.expire_pending(now, self.signals)       # whatever the hour: a late quote never fills it
        if not self.cal.is_trading_day(day):
            return None
        if self.cal.is_open(now):
            self.paper.fill_pending(now, self.signals)
        b = floor_bar(now)
        open_t, close_t = session_open_utc(day), self.cal.last_close_utc(day)
        if open_t < b <= close_t and (self.last_bar is None or b > self.last_bar):
            return self.on_bar(b, now)
        if now > close_t + timedelta(minutes=1) and self.j.get_state(f"equity_closed:{day}") is None:
            self.close_session(day, now)
        return None

    def close_session(self, day: date, now: datetime) -> dict | None:
        """Closing equity of the day. A held cash position in a stock whose segment had a closing auction (F&O stocks
        from 3 Aug 2026) is marked at Breeze's daily bar close (the official closing-auction price) once it is
        stored; until then this waits (None), up to `after_close.bars_deadline`, then closes on the last marks
        (the last continuous trade) labelled APPROXIMATE."""
        from .config import hhmm
        from .history import daily_start
        from .journal import open_pnl
        held = [p for pool in POOLS for p in self.j.held_positions(pool)
                if not p.is_option and self.cal.hours(segment_for(self.s, p.symbol), day).has_auction]
        closes, missing = {}, []
        for sym in sorted({p.symbol for p in held}):
            got = self.store.history_bars("bars_1d", sym, self.source, since=daily_start(day), limit=1)
            if got and got[0].start == daily_start(day):
                closes[sym] = got[0].close
            else:
                missing.append(sym)
        deadline = at_utc(day, hhmm(self.s.stock_option_selling.after_close.bars_deadline))
        if missing and now < deadline:
            key = f"equity_close_waiting:{day}"
            was = json.loads(self.j.get_state(key) or "{}")
            if was.get("waiting_for") != missing:              # written once per change, not on every poll
                self.j.set_state(key, json.dumps({"waiting_for": missing, "since_utc": now.isoformat(),
                                                  "until_utc": deadline.isoformat()}), now)
            return None
        for p in held:
            if p.symbol in closes:
                v = open_pnl(p, [closes[p.symbol]], self.costs)
                if v is not None:
                    self.j.save_mark(p.position_id, now, float(v) / p.quantity)
        if missing:
            note = (f"close APPROXIMATE: no Breeze daily bar (official closing-auction close) by "
                    f"{self.s.stock_option_selling.after_close.bars_deadline} for {', '.join(missing)}: their last "
                    f"continuous trade was used")
        elif closes:
            note = f"close: official closing-auction close (Breeze daily bar) for {', '.join(sorted(closes))}"
        else:
            note = "close"
        flows = tuple(self.store.capital_flows())
        out = {pool: close_equity_day(self.j, self.s, pool, now, flows, note=note) for pool in POOLS}
        self.j.set_state(f"equity_closed:{day}", json.dumps({"at_utc": now.isoformat(), "note": note}), now)
        return out

    def on_bar(self, b: datetime, now: datetime | None = None) -> dict:
        """Everything that happens when the 5-minute bar ending at `b` has closed."""
        now = now or b
        day = ist_day(b)
        reg = self.regime.evaluate(now)
        regime, bias = reg.get("regime"), reg.get("bias")
        monday = day - timedelta(days=day.weekday())
        try:
            events_week = [{**e, "date": e["date"].isoformat()} for e in self.store.events_between(monday, monday + timedelta(days=6))]
        except Exception:
            events_week = None
        self._week_events = None                  # read once per bar, for the week's event coverage on the cards
        bars = {i.symbol: self.store.bars_1m_between(i.symbol, session_open_utc(day), b) for i in self.s.watchlist}
        five = {s: five_minute_bars(v, s, day, b) for s, v in bars.items()}
        daily = {i.symbol: self.store.history_bars("bars_1d", i.symbol, self.source, before=session_open_utc(day),
                                                    limit=300) for i in self.s.watchlist}
        candidates: list[tuple[Signal, frozenset | None]] = []
        first_bar = b == session_open_utc(day) + FIVE or (self.last_bar is None or ist_day(self.last_bar) != day)
        # new signals only on a bar that closes inside the index's continuous trading (never in the closing auction
        # or after it); exits and paper trades below run on every bar to the last close (options to 15:40)
        new_signals = b < self.cal.hours(INDEX, day).continuous_end_utc
        # cash intraday: no entry from the day's cutoff (15:15 before 3 Aug 2026, 15:00 after)
        from .config import hhmm
        cutoff = at_utc(day, session_calendar().intraday_exit(day, hhmm(self.s.backtest.intraday_exit_time)))
        for st in (self.cash if new_signals and b < cutoff else ()):
            for i in self.s.watchlist:
                if i.kind != "stock" or not five[i.symbol] or five[i.symbol][-1].start + FIVE != b:
                    continue
                if not b < self.cal.hours(segment_for(self.s, i.symbol), day).continuous_end_utc:
                    continue                      # the stock's own continuous trading has ended
                ctx = MarketContext(i.symbol, i.kind, b, regime or "UNKNOWN", intraday=tuple(five[i.symbol]),
                                    daily=tuple(daily[i.symbol]), status=self._status(i.symbol, now),
                                    prior_sessions=self._prior(i.symbol, day, st.prior_sessions_needed),
                                    index_daily=tuple(daily.get(self.s.regime.index_symbol, ())))
                candidates += [(sig, st.allowed_regimes) for sig in self._safe(st, ctx)]
        if first_bar and new_signals:
            prev = self.cal.previous_trading_day(day)
            for st in self.swing:
                for i in self.s.watchlist:
                    d = [x for x in daily[i.symbol] if x.start.astimezone(IST).date() <= prev]
                    if i.kind != "stock" or not d or d[-1].start.astimezone(IST).date() != prev:
                        continue
                    # as of the stock's official close of the previous session (15:35 from 3 Aug 2026)
                    asof = self.cal.hours(segment_for(self.s, i.symbol), prev).close_utc
                    ctx = MarketContext(i.symbol, i.kind, asof, regime or "UNKNOWN", daily=tuple(d),
                                        status=self._status(i.symbol, now),
                                        index_daily=tuple(x for x in daily.get(self.s.regime.index_symbol, ())
                                                          if x.start.astimezone(IST).date() <= prev))
                    candidates += [(sig, st.allowed_regimes) for sig in self._safe(st, ctx)]
        if new_signals:
            candidates += self._option_signals(now, [s for s, _ in candidates])
        stored, approved = [], []
        for sig, allowed in candidates:
            out = self._handle(sig, allowed, now, regime, bias, events_week, five, daily)
            if out is not None:
                stored.append(out)
                if out["status"] == "APPROVED":
                    approved.append(out)
        # paper exits on this bar, exit calls for my positions, marks for the pools
        closed = self.paper.manage(now, b - FIVE)
        due = self.evaluate_exits(now, regime, five, daily)
        try:
            notices = self.price_notices(now)
        except Exception:
            log.exception("held-price notices not checked")
            notices = []
        for pool in POOLS:
            record_marks(self.j, pool, now, self.m.marks, self.costs)
        self.last_bar = b
        self.j.set_state("last_bar_utc", b.isoformat(), now)
        if self.notifier is not None:
            try:
                self.notifier.on_bar(rank(approved), due, now, self.kill_reasons(now), price_notices=notices)
            except Exception:
                log.exception("alerts failed (signals and paper trading carry on)")
        self.last_summary = {"bar_utc": b.isoformat(), "regime": regime, "signals": len(stored),
                             "approved": len(approved), "paper_closed": len(closed), "exit_calls_due": len(due),
                             "price_notices": len(notices)}
        return {**self.last_summary, "stored": stored, "due": due, "notices": notices}

    # ---- helpers ----
    def _coverage(self, sig, now):
        """The week's event coverage for a card (None when the events cannot be read: shown as missing)."""
        from .manual_data import week_coverage
        try:
            day = ist_day(now)
            friday = day - timedelta(days=day.weekday()) + timedelta(days=4)
            if getattr(self, "_week_events", None) is None:
                self._week_events = self.store.all_events()
            stocks = [i.symbol for i in self.s.watchlist if i.kind == "stock"]
            return week_coverage(self._week_events, stocks, day, friday, self.s.data_status.coverage_max_age_days,
                                 extra=[sig.symbol])
        except Exception:
            log.exception("event coverage not known")
            return None

    def _safe(self, st, ctx) -> list[Signal]:
        try:
            return st.evaluate(ctx)
        except Exception:
            log.exception("strategy %s failed on %s", st.id, ctx.symbol)
            return []

    def _status(self, symbol: str, now: datetime) -> str:
        return self.hub.quote(symbol, now)["status"]

    def _prior(self, symbol: str, day: date, n: int):
        if n <= 0:
            return ()
        rows = self.store.history_bars("bars_5m", symbol, self.source, before=session_open_utc(day), limit=n * 80)
        by: dict[date, list[Bar]] = {}
        for x in rows:
            by.setdefault(x.start.astimezone(IST).date(), []).append(x)
        return tuple(tuple(by[d]) for d in sorted(by)[-n:])

    def _option_signals(self, now: datetime, cash_signals: list[Signal]) -> list[tuple[Signal, None]]:
        out = []
        triggers = {s.symbol: s for s in cash_signals if s.strategy_id in self.s.strategies.long_option.trigger_strategies}
        for sym in self.desk.underlyings():
            try:
                v = self.desk.view(sym, now, trigger=triggers.get(sym), keep_objects=True)
            except Exception:
                log.exception("option view failed for %s", sym)
                continue
            for idea in v.get("ideas", []):
                st, sigs = idea.get("objects", (None, []))
                out += [(s, None) for s in sigs]
        return out

    def _corr_fn(self, day: date):
        if self._corr_day != day:
            try:
                self._corr = self.risk.correlations(self.source)
            except Exception:
                self._corr = None
            self._corr_day = day
        c = self._corr
        return (lambda a, b: c.at(day, a, b)) if c is not None else (lambda a, b: None)

    def _handle(self, sig: Signal, allowed, now, regime, bias, events_week, five, daily) -> dict | None:
        from .risk.engine import decide
        from .switches import alert_allowed
        from .track import strategy_record
        if self.j.has_signal(sig.signal_id):
            return None                                          # never issued twice (restart, same bar)
        key = legs_key(sig)
        for other in self.signals.values():
            if legs_key(other) == key and other.valid_until > now:
                return None                                      # the same idea is still valid: not repeated
        tt = signal_trade_type(sig)
        pool = pool_of_trade_type(tt)
        acct = self.risk.account_state(pool, now)
        market = self.risk.market_state(sig.symbol, now, regime)
        day = ist_day(now)
        universe = [i.symbol for i in self.s.watchlist if i.kind == "stock"]
        try:
            decision = decide(sig, acct, market, self.s, self.costs, self._corr_fn(day), self.sectors,
                              allowed_regimes=allowed, universe=universe)
        except Exception as e:
            log.exception("risk decision failed for %s", sig.signal_id)
            return None
        sc = score(self._score_inputs(sig, regime, bias, five, daily, now), self.s.scoring)
        status = self.s.strategies.status_of(sig.strategy_id)
        allowed_alert, why = alert_allowed(self.j.switches(), tt, status)
        card = build_card(sig, decision, score=sc, regime=regime, bias=bias, events_week=events_week,
                          track=strategy_record(self.store, self.j, self.s, sig.strategy_id), now=now,
                          alert={"allowed": allowed_alert and decision.status == "APPROVED", "why": why},
                          coverage=self._coverage(sig, now))
        # the signal and its pending paper trade in one transaction: a crash never leaves one without the other
        if not self.j.save_signal(sig, decision, now, score=sc["score"], card=card,
                                  paper=self.paper.admit(self.paper.pending_row(sig, decision), now)):
            return None
        self.signals[sig.signal_id] = sig
        quotes = self.paper._quotes(sig, now)
        self.x.record_quotes(sig, SIGNAL, quotes, now, (decision.lots if sig.legs else decision.quantity),
                             self.m.spot(sig.symbol, now))
        return {"signal_id": sig.signal_id, "status": decision.status, "score": sc["score"], "trade_type": tt,
                "strategy_id": sig.strategy_id, "card": card, "alert_allowed": card["alert"]["allowed"],
                "valid_until": sig.valid_until}

    def _score_inputs(self, sig: Signal, regime, bias, five, daily, now) -> ScoreInputs:
        sym = sig.symbol
        if sig.legs:
            from .journal import Leg, Position
            view = Position("x", "PAPER", None, signal_trade_type(sig), "", sym, sig.direction,
                            tuple(Leg(x.action, x.right, x.strike, x.expiry, x.lots, x.lot_size, x.price) for x in sig.legs),
                            1, sig.entry, now).view
            direction = {"BULLISH": "LONG", "BEARISH": "SHORT"}.get(view, "NEUTRAL")
            entry, target = self.m.spot(sym, now), (sig.details or {}).get("underlying_target")
        else:
            direction, entry, target = sig.direction, sig.entry, sig.target
        bars = five.get(sym, [])
        d = daily.get(sym, [])
        closes_15 = [bars[k + 2].close for k in range(0, len(bars) - 2, 3)]
        prior = self._prior(sym, ist_day(now), 1)
        if prior:
            p = list(prior[-1])
            closes_15 = [p[k + 2].close for k in range(0, len(p) - 2, 3)] + closes_15
        avg_vol = None
        if bars and bars[-1].volume is not None and len(bars) >= 2:
            earlier = [x.volume for x in bars[:-1] if x.volume is not None]
            avg_vol = statistics.fmean(earlier) if earlier else None

        def ret(s):
            dd = daily.get(s) or []
            px = self.m.spot(s, now)
            return (px / dd[-1].close - 1) * 100 if px and dd and dd[-1].close else None

        sector = self.sectors.get(sym)
        peers = [r for r in (ret(s) for s, sec in self.sectors.items() if sec == sector and s != sym and self.kinds.get(s) == "stock")
                 if r is not None]
        return ScoreInputs(direction=direction, entry=entry, target=target, closes_5m=[x.close for x in bars],
                           closes_15m=closes_15, daily_closes=[x.close for x in d], daily_highs=[x.high for x in d],
                           daily_lows=[x.low for x in d], bar_volume=bars[-1].volume if bars else None,
                           avg_volume=avg_vol, stock_return_pct=ret(sym), index_return_pct=ret(self.s.regime.index_symbol),
                           peer_returns_pct=peers, regime=regime, bias=bias)

    # ---- exit calls ----
    def kill_reasons(self, now: datetime) -> tuple[str, ...]:
        from .risk.limits import kill_switch
        m = self.risk.market_state(self.s.regime.index_symbol, now)
        ks = kill_switch(self.s.regime.index_symbol, m.index_status, m.quarantined_recent, m.market_open,
                         self.s.risk.kill_switch)
        return tuple(r for r in ks if "market hours" not in r)

    def evaluate_exits(self, now: datetime, regime, five=None, daily=None) -> list[dict]:
        from .options.stock_selling import trend_of
        kill = self.kill_reasons(now)
        p9 = self.s.stock_option_selling

        def inputs_for(pos):
            prices, why = self.m.marks(pos, now)
            trend = close = None
            if pos.trade_type == "STOCK_OPTION_SELLING":
                d = (daily or {}).get(pos.symbol) or self.store.history_bars("bars_1d", pos.symbol, self.source, limit=300)
                trend = trend_of(d, p9.sma_short, p9.sma_mid, p9.atr_days)[0] if d else None
                f = (five or {}).get(pos.symbol)
                close = f[-1].close if f else None
            # the settlement price from ONE hub snapshot: the last valid continuous-session price and its time
            snap = self.hub.settlement_snapshot(pos.symbol, now)
            return ExitInputs(now=now, prices=prices, price_why=why, spot=self.m.spot(pos.symbol, now), regime=regime,
                              kill_reasons=kill, stock_trend=trend, stock_close=close, last_price=snap["price"],
                              last_price_at=snap["at"], auction=snap["phase"] == "AUCTION")
        return evaluate_positions(self.j, self.exits, self.s, self.cal, now, inputs_for, self.costs)

    def price_notices(self, now: datetime) -> list[dict]:
        """The held-price notices due now (`feed.held_price_notice`): for each underlying of my open positions in
        continuous trading, no valid tick for `no_valid_tick_minutes` (since its last valid tick today, or the open)
        or more than `rejection_share_pct` of at least `min_ticks` ticks rejected in the last
        `rejection_window_minutes`. ONE per symbol per trading date (kept in memory only, reset each date); it never
        changes a settlement alert and nothing is written to a position."""
        cfg = self.s.feed.held_price_notice
        day = ist_day(now)
        if self._noticed_day != day:
            self._noticed_day, self._noticed = day, set()
        out = []
        for sym in sorted({p.symbol for p in self.j.positions("OPEN")}):
            if sym in self._noticed:
                continue
            snap = self.hub.settlement_snapshot(sym, now, cfg.rejection_window_minutes)
            if snap["phase"] != "OPEN":
                continue                                   # only in continuous trading: never in the auction or closed
            opened = self.cal.hours(self.hub.segment(sym), day).open_utc
            since = snap["valid_today_at"] or opened
            why = None
            if now - since >= timedelta(minutes=cfg.no_valid_tick_minutes) and now - opened >= timedelta(
                    minutes=cfg.no_valid_tick_minutes):
                why = f"no valid tick for {(now - since).total_seconds() / 60:.0f} minutes"
            elif snap["ticks"] >= cfg.min_ticks and snap["rejected"] * 100.0 > cfg.rejection_share_pct * snap["ticks"]:
                why = (f"{snap['rejected']} of the last {snap['ticks']} ticks rejected by the quality gate in "
                       f"{cfg.rejection_window_minutes:g} minutes")
            if why is None:
                continue
            self._noticed.add(sym)
            last = (f"; settlement alerts use the last valid price {snap['price']:g} as of "
                    f"{snap['at'].astimezone(IST):%H:%M}" if snap["price"] is not None else
                    "; no valid price yet: settlement alerts assume the sold legs near the money")
            out.append({"symbol": sym, "day": day.isoformat(), "since": since, "why": why,
                        "text": f"PRICE UNAVAILABLE - {sym}: price unavailable since {since.astimezone(IST):%H:%M} "
                                f"({why}), check in ICICI Direct{last}. Notice only: no settlement level changes."})
        return out
