"""Phase 6 step 8: exit calls for every open position of every trade type, taken from a card or entered by hand.

Re-evaluated on every closed bar. A call is advice only: nothing is sent to the broker and nothing here closes or
changes a position (I mark it closed under /journal/). Each call states the ACTION (for example "Exit all 2 lots
now", "Move stop to 1,245.50") and the REASON with the values that triggered it.

Kinds (`exit_calls.kind`):
- TARGET, STOP: on the position's own price (share price; option premium; the cost per share to buy a spread back)
  and, for options with underlying levels, on the underlying. The stop is my stop or the system's plan stop after a
  breakeven or trailing call, whichever is tighter.
- BREAKEVEN (gain of `exits.breakeven_after_r` x the initial risk R), TRAIL (from `exits.trail_after_r`, the stop
  `exits.trail_distance_r` x R behind the best price seen), PARTIAL (at `exits.partial_at_r`, book
  `exits.partial_pct` of two or more units, once).
- TIME: intraday at the earlier of `backtest.intraday_exit_time` and `session.cash_intraday_cutoff` (15:00;
  ICICI's own square-off time is not confirmed, and the call says so); swing after the strategy's `max_hold_days` sessions; long
  options at the strategy's exit before expiry or its holding limit; index spreads `exit_days_before_expiry`;
  stock option spreads by the phase 9 rules (time exit, forced exit before expiry week).
- ENTRY_INVALIDATED: the underlying closes beyond the level the entry needed (`invalidation`, with
  `exits.invalidation_buffer_pct`).
- REGIME_FLIP: the regime is no longer one the strategy is allowed in, or it turned against the position (a
  bullish position in TRENDING_DOWN, a bearish one in TRENDING_UP, a selling position in VOLATILE or EVENT).
- KILL_SWITCH: the kill switch is on for a reason other than market hours.
- Stock option spreads (phase 9, `app.options.stock_selling.exit_calls`): STRIKE_BREACHED, TREND_FLIP,
  FORCED_EXPIRY_EXIT; and SETTLEMENT (physical-settlement readiness, `app.options.settlement`).

Repeats: a call is ACTIVE once raised. TARGET, STOP, TIME, ENTRY_INVALIDATED, STRIKE_BREACHED, TREND_FLIP,
FORCED_EXPIRY_EXIT and SETTLEMENT stay active until I mark the position closed. BREAKEVEN and TRAIL stay active
until my confirmed stop is at the suggested level; PARTIAL until a partial close is recorded; REGIME_FLIP and
KILL_SWITCH while their condition holds - resolved only when an evaluation with known inputs finds it no longer
holds (REGIME_FLIP: a known regime, not UNKNOWN; both: the position's prices known), never by missing data. An active call is sent again every `alerts.exit_call_repeat_minutes`
(SETTLEMENT: `alerts.settlement_repeat_minutes` of its level; overdue settlement: every evaluation).

A stale price never triggers and never silences a call: with no LIVE price the price rules are not evaluated
("price stale: exit calls paused since hh:mm"), active calls keep repeating, and the time, regime and kill-switch
rules still run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone

from .config import hhmm
from .journal import Position, ist_day
from .quality import IST

TARGET, STOP, BREAKEVEN, TRAIL, PARTIAL, TIME = "TARGET", "STOP", "BREAKEVEN", "TRAIL", "PARTIAL", "TIME"
ENTRY_INVALIDATED, REGIME_FLIP, KILL_SWITCH = "ENTRY_INVALIDATED", "REGIME_FLIP", "KILL_SWITCH"
STRIKE_BREACHED, TREND_FLIP, FORCED_EXPIRY_EXIT, SETTLEMENT = (
    "STRIKE_BREACHED", "TREND_FLIP", "FORCED_EXPIRY_EXIT", "SETTLEMENT")
GENERIC_KINDS = (TARGET, STOP, BREAKEVEN, TRAIL, PARTIAL, TIME, ENTRY_INVALIDATED, REGIME_FLIP, KILL_SWITCH)
STICKY = {TARGET, STOP, TIME, ENTRY_INVALIDATED, STRIKE_BREACHED, TREND_FLIP, FORCED_EXPIRY_EXIT, SETTLEMENT}
WHILE_TRUE = {REGIME_FLIP, KILL_SWITCH}

SCHEMA = """
CREATE TABLE IF NOT EXISTS exit_calls (
    position_id TEXT NOT NULL, kind TEXT NOT NULL, first_utc TEXT NOT NULL, last_utc TEXT NOT NULL,
    repeat_no INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, values_json TEXT NOT NULL,
    active INTEGER NOT NULL, resolved_utc TEXT, resolved_why TEXT, level TEXT,
    PRIMARY KEY (position_id, kind)
);
CREATE TABLE IF NOT EXISTS exit_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL, kind TEXT NOT NULL, at_utc TEXT NOT NULL,
    repeat_no INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, values_json TEXT NOT NULL, level TEXT
);
"""


def fmt(x) -> str:
    return f"{x:,.2f}"


def units_text(pos: Position, n: int | None = None) -> str:
    n = pos.quantity if n is None else n
    return f"{n} lot{'s' if n != 1 else ''}" if pos.is_option else f"{n} share{'s' if n != 1 else ''}"


@dataclass(frozen=True)
class ExitInputs:
    """What one evaluation of one position may look at. None = not known (never a pass, never a trigger)."""
    now: datetime
    prices: list[float] | None           # closing prices of the position's legs (app.pricing.Marker.marks)
    price_why: str = ""                  # why prices are None
    spot: float | None = None            # the underlying's LIVE last price (cash: the share)
    regime: str | None = None
    kill_reasons: tuple[str, ...] = ()   # kill-switch reasons other than "outside market hours"
    stock_trend: str | None = None       # phase 9 trend of the stock (UP / DOWN / None)
    stock_close: float | None = None     # the stock's last completed close (phase 9 strike breach)


def _r_units(pos: Position) -> tuple[str, float | None, float | None]:
    """(kind of price, value now per share, entry per share) for the position's own price."""
    return ("spread" if pos.is_option and pos.selling else "premium" if pos.is_option else "share", None, pos.entry_price)


def own_price(pos: Position, prices: list[float] | None) -> float | None:
    """The position's own price now: the share price; the premium of a bought option (sum of bought legs); the cost
    per share to buy a spread back (sold legs at their price less bought legs)."""
    if prices is None:
        return None
    if not pos.is_option:
        return prices[0]
    if not pos.selling:
        return sum(prices)
    return sum(p if x.action == "SELL" else -p for x, p in zip(pos.legs, prices))


def time_exit_at(pos: Position, settings, calendar) -> tuple[datetime | None, str]:
    """When the position's time exit is due (UTC) and why; (None, why) when no time rule applies."""
    from .session import DERIVATIVES, calendar as session_calendar, segment_for
    exit_t = hhmm(settings.backtest.intraday_exit_time)
    entry_day = ist_day(pos.entry_utc)
    if pos.trade_type == "INTRADAY_STOCKS" or pos.horizon == "INTRADAY":
        sc = session_calendar()
        t = min(exit_t, sc.intraday_cutoff)
        note = "" if sc.cutoff_confirmed else " (ICICI's own intraday square-off time after the closing auction is not confirmed)"
        return datetime.combine(entry_day, t, IST).astimezone(timezone.utc), f"intraday: out by {t:%H:%M} IST{note}"
    strat = getattr(settings.strategies, pos.strategy_id, None) if pos.strategy_id else None
    hold = getattr(strat, "max_hold_days", None)
    sessions_end = None
    if hold and pos.trade_type in ("SWING_STOCKS", "INDEX_OPTION_BUYING", "STOCK_OPTION_BUYING"):
        d = entry_day
        for _ in range(hold):
            d = calendar.next_trading_day(d)
        # the last 5-minute bar of continuous trading that day: the share's own segment (15:10 bar for an F&O stock
        # from 3 Aug 2026), the options' for an option position (15:35 bar); 15:25 for both before 3 Aug 2026
        seg = DERIVATIVES if pos.is_option else segment_for(settings, pos.symbol)
        sessions_end = (session_calendar().hours(seg, d).continuous_end_utc - timedelta(minutes=5),
                        f"held {hold} sessions (max_hold_days of {pos.strategy_id})")
    expiry_rule = None
    if pos.is_option:
        expiry = min(x.expiry for x in pos.legs)
        lo = settings.strategies.long_option
        if pos.trade_type == "STOCK_OPTION_BUYING":
            n = lo.stock_exit_days_before_expiry
        elif pos.trade_type == "INDEX_OPTION_BUYING":
            n = lo.index_exit_days_before_expiry
        elif pos.trade_type == "INDEX_OPTION_SELLING":
            n = settings.strategies.credit_spread.exit_days_before_expiry
        else:
            n = None                                  # stock option selling: phase 9 rules (time and forced exit)
        if n is not None:
            expiry_rule = (datetime.combine(expiry - timedelta(days=n), exit_t, IST).astimezone(timezone.utc),
                           f"{n} day(s) before the {expiry:%d-%b-%Y} expiry"
                           + (" (stock options settle physically)" if pos.trade_type == "STOCK_OPTION_BUYING" else ""))
    rules = [r for r in (sessions_end, expiry_rule) if r is not None]
    if not rules:
        return None, "no time rule for this position"
    return min(rules, key=lambda r: r[0])


def _call(kind, action, reason, **values) -> dict:
    return {"kind": kind, "action": action, "reason": reason, "values": values}


def evaluate(pos: Position, x: ExitInputs, settings, calendar, costs=None) -> tuple[list[dict], dict]:
    """(calls that apply now, plan updates). Pure: reads only its arguments."""
    e = settings.exits
    calls: list[dict] = []
    plan = dict(pos.plan)
    stock_selling = pos.trade_type == "STOCK_OPTION_SELLING"
    up = pos.view != "BEARISH"
    px = own_price(pos, x.prices)
    stale = px is None
    if stale:
        plan.setdefault("stale_since_utc", x.now.isoformat())
    else:
        plan.pop("stale_since_utc", None)

    # ---- phase 9 rules for stock option spreads (target, stop, time, forced exit, strike breach, trend flip) ----
    p9_stop = p9_target = None
    if stock_selling and pos.legs and len(pos.legs) == 2 and pos.hedged:
        from .options.stock_selling import SpreadPosition, exit_calls as p9_calls, exit_levels
        sold = next(l for l in pos.legs if l.action == "SELL")
        bought = next(l for l in pos.legs if l.action == "BUY")
        sp = SpreadPosition(pos.symbol, sold.right, sold.strike, bought.strike, sold.expiry, pos.entry_price,
                            sold.lot_size, pos.quantity)
        p9_target, p9_stop = exit_levels(pos.entry_price, settings.stock_option_selling)
        leg_px = None if x.prices is None else (x.prices[pos.legs.index(sold)], x.prices[pos.legs.index(bought)])
        for c in p9_calls(sp, settings.stock_option_selling, ist_day(x.now), px, x.stock_close, x.stock_trend,
                          costs, leg_px):
            if c["kind"] == "DATA_MISSING":
                continue                                    # a stale price pauses the price rules (shown on the position)
            kind = {"TIME_EXIT": TIME}.get(c["kind"], c["kind"])
            action = c["action"]
            if kind in (TARGET, STOP, TIME, FORCED_EXPIRY_EXIT):
                action = f"{c['action']}: close all {units_text(pos)}"
            calls.append(_call(kind, action, c["reason"], **{k: v for k, v in c.items()
                                                            if k not in ("kind", "action", "reason")}))

    # ---- the position's own price: stop, target, breakeven, trail, partial ----
    stop = pos.stop if pos.stop is not None else p9_stop
    target = pos.target if pos.target is not None else p9_target
    initial = pos.initial_stop if pos.initial_stop is not None else stop
    spread = pos.is_option and pos.selling
    # "favourable" direction of the own price: up for a long share or bought option, down for a short share or a spread
    fav_up = (pos.direction == "LONG") if not pos.is_option else not spread
    plan_stop = plan.get("plan_stop")
    eff_stop = stop
    if plan_stop is not None and stop is not None:
        eff_stop = max(stop, plan_stop) if fav_up else min(stop, plan_stop)
    elif plan_stop is not None:
        eff_stop = plan_stop
    if not stale and not stock_selling:
        if eff_stop is not None and ((px <= eff_stop) if fav_up else (px >= eff_stop)):
            calls.append(_call(STOP, f"Exit all {units_text(pos)} now: stop reached",
                               f"{_price_name(pos)} {fmt(px)} is at or {'below' if fav_up else 'above'} the stop {fmt(eff_stop)}",
                               price=px, stop=eff_stop))
        elif target is not None and ((px >= target) if fav_up else (px <= target)):
            calls.append(_call(TARGET, f"Exit all {units_text(pos)} now: target reached",
                               f"{_price_name(pos)} {fmt(px)} is at or {'above' if fav_up else 'below'} the target {fmt(target)}",
                               price=px, target=target))
    if not stale and initial is not None:
        risk = (pos.entry_price - initial) if fav_up else (initial - pos.entry_price)
        if risk > 0:
            gain_r = ((px - pos.entry_price) if fav_up else (pos.entry_price - px)) / risk
            best = plan.get("best_price")
            best = px if best is None else (max(best, px) if fav_up else min(best, px))
            plan["best_price"] = best
            confirmed = pos.stop
            tighter = (lambda a, b: a > b) if fav_up else (lambda a, b: a < b)    # a is a tighter stop than b

            def suggest(kind, level, action, reason):
                level = round(level, 2)
                cur = plan.get("plan_stop", confirmed)
                if cur is None or tighter(level, cur):
                    plan["plan_stop"] = level
                ps = plan["plan_stop"]
                if confirmed is None or tighter(ps, confirmed):
                    calls.append(_call(kind, action.format(fmt(ps)), reason, gain_r=round(gain_r, 2), stop_now=confirmed,
                                       suggested_stop=ps, price=px))

            if gain_r >= e.breakeven_after_r:
                suggest(BREAKEVEN, pos.entry_price, "Move stop to {} (breakeven)",
                        f"{_price_name(pos)} {fmt(px)} is {gain_r:.2f}R in profit (R = {fmt(risk)}); "
                        f"at {e.breakeven_after_r:g}R the stop moves to the entry {fmt(pos.entry_price)}")
            if gain_r >= e.trail_after_r:
                level = best - e.trail_distance_r * risk if fav_up else best + e.trail_distance_r * risk
                suggest(TRAIL, level, "Trail the stop to {}",
                        f"{gain_r:.2f}R in profit; the stop trails {e.trail_distance_r:g}R ({fmt(e.trail_distance_r * risk)}) "
                        f"behind the best {_price_name(pos)} {fmt(best)}")
            if gain_r >= e.partial_at_r and not plan.get("partial_booked") and pos.quantity >= 2:
                n = max(1, int(pos.quantity * e.partial_pct / 100))
                calls.append(_call(PARTIAL, f"Book partial profit: close {units_text(pos, n)} of {pos.quantity}",
                                   f"{gain_r:.2f}R in profit, at or above {e.partial_at_r:g}R; book {e.partial_pct:g}%",
                                   gain_r=round(gain_r, 2), units=n, price=px))
    # underlying levels of an option position
    if pos.is_option and x.spot is not None:
        if pos.underlying_stop is not None and ((x.spot <= pos.underlying_stop) if up else (x.spot >= pos.underlying_stop)):
            calls.append(_call(STOP, f"Exit all {units_text(pos)} now: underlying stop reached",
                               f"{pos.symbol} at {fmt(x.spot)} is beyond the underlying stop {fmt(pos.underlying_stop)}",
                               spot=x.spot, underlying_stop=pos.underlying_stop))
        if pos.underlying_target is not None and ((x.spot >= pos.underlying_target) if up else (x.spot <= pos.underlying_target)):
            calls.append(_call(TARGET, f"Exit all {units_text(pos)} now: underlying target reached",
                               f"{pos.symbol} at {fmt(x.spot)} reached the underlying target {fmt(pos.underlying_target)}",
                               spot=x.spot, underlying_target=pos.underlying_target))

    # ---- time ----
    if not stock_selling:
        due, why = time_exit_at(pos, settings, calendar)
        if due is not None and x.now >= due:
            calls.append(_call(TIME, f"Exit all {units_text(pos)} now: time exit",
                               f"{why}; due {due.astimezone(IST):%d-%b %H:%M} IST", due_utc=due.isoformat()))

    # ---- entry reason invalidated (on the underlying's price) ----
    inv = pos.invalidation
    if inv and x.spot is not None:
        buf = e.invalidation_buffer_pct / 100
        lvl = inv["level"]
        broke = x.spot < lvl * (1 - buf) if inv["side"] == "below" else x.spot > lvl * (1 + buf)
        if broke and not any(c["kind"] == STOP for c in calls):
            calls.append(_call(ENTRY_INVALIDATED, f"Exit all {units_text(pos)} now: the entry reason no longer holds",
                               f"{pos.symbol} at {fmt(x.spot)} is {inv['side']} {fmt(lvl)} ({inv['why']}) by more than "
                               f"{e.invalidation_buffer_pct:g}%", spot=x.spot, level=lvl))

    # ---- regime flip ----
    reg = x.regime
    if reg and reg != "UNKNOWN":
        allowed = None
        if pos.strategy_id:
            allowed = _allowed_regimes(pos.strategy_id)
        against = (pos.view == "BULLISH" and reg == "TRENDING_DOWN") or (pos.view == "BEARISH" and reg == "TRENDING_UP") \
            or (pos.selling and reg in ("VOLATILE", "EVENT"))
        outside = allowed is not None and reg not in allowed and reg != pos.regime_at_entry
        if against or outside:
            why = (f"regime is now {reg}, against this {pos.view.lower()} position" if against else
                   f"regime is now {reg}, outside {pos.strategy_id}'s regimes {sorted(allowed)}")
            act = (f"Exit all {units_text(pos)}: regime turned against the position" if against or pos.horizon == "INTRADAY"
                   else f"Review: regime changed; keep the stop at {fmt(eff_stop) if eff_stop is not None else 'your stop'}")
            calls.append(_call(REGIME_FLIP, act, why, regime=reg, regime_at_entry=pos.regime_at_entry))

    # ---- kill switch ----
    if x.kill_reasons:
        urgent = pos.trade_type == "INTRADAY_STOCKS" or pos.selling
        act = (f"Exit all {units_text(pos)} now: kill switch on" if urgent else
               f"Review now: kill switch on; keep the stop at {fmt(eff_stop) if eff_stop is not None else 'your stop'}")
        calls.append(_call(KILL_SWITCH, act, "; ".join(x.kill_reasons), reasons=list(x.kill_reasons)))
    return calls, plan


def _price_name(pos: Position) -> str:
    if not pos.is_option:
        return "price"
    return "cost to close" if pos.selling else "premium"


def _allowed_regimes(strategy_id: str):
    from .strategies import CLASSES
    cls = CLASSES.get(strategy_id)
    if cls is not None:
        return cls.allowed_regimes
    from .options.credit_spread import CreditSpread
    from .options.iron_condor import IronCondor
    from .options.long_option import LongOption
    for c in (CreditSpread, IronCondor, LongOption):
        if c.id == strategy_id:
            return c.allowed_regimes
    return None


# ---------------- settlement readiness (sending of the phase 9 M3 alerts) ----------------
def settlement_call(pos: Position, spot: float | None, now: datetime, settings, calendar) -> dict | None:
    """The settlement-readiness alert of a position with a sold STOCK option leg, as an exit call (kind SETTLEMENT)
    whose level only rises while the position is open. None when nothing applies yet."""
    if pos.trade_type != "STOCK_OPTION_SELLING" or not pos.legs:
        return None
    from .options.settlement import HeldLeg, HeldPosition, settlement_alert
    expiries = {x.expiry for x in pos.legs}
    held = HeldPosition(pos.symbol, min(expiries), tuple(HeldLeg(x.action, x.right, x.strike, x.lots, x.lot_size)
                                                          for x in pos.legs),
                        "UNHEDGED" if not pos.hedged else ("from a card" if pos.origin == "SIGNAL" else "manual"))
    a = settlement_alert(held, spot, now, settings.stock_option_selling, settings.stock_option_selling.backtest.capital_inr,
                         previous_level=pos.plan.get("settlement_level"), calendar=calendar)
    if a is None:
        return None
    reason = "; ".join(f"{l['leg']}: {l['why']}" for l in a["legs"])
    if a["overdue"]:
        reason = f"OVERDUE: {a['overdue'].get('text', 'planned exit deadline passed')}; " + reason
    if a["not_confirmed"]:
        reason += "; " + "; ".join(a["not_confirmed"])
    # no level of its own: URGENT once overdue (the hard limit), WARNING while only the planned exit time has come
    level = a["level"] or ("URGENT" if a["overdue"] or not a.get("due_now") else "WARNING")
    return {"kind": SETTLEMENT, "action": a["action"], "reason": reason, "level": level,
            "overdue": bool(a["overdue"]), "values": {"alert": a}}


# ---------------- the call book: activation, repeats, resolution ----------------
class ExitCallBook:
    def __init__(self, store, settings):
        self.s = settings
        self._db, self._lock = store._db, store._lock
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    _COLS = ["position_id", "kind", "first_utc", "last_utc", "repeat_no", "action", "reason", "values_json", "active",
             "resolved_utc", "resolved_why", "level"]

    def calls(self, position_id: str | None = None, active_only: bool = False) -> list[dict]:
        q, args = f"SELECT {','.join(self._COLS)} FROM exit_calls WHERE 1=1", []
        if position_id:
            q, args = q + " AND position_id=?", args + [position_id]
        if active_only:
            q += " AND active=1"
        with self._lock:
            rows = self._db.execute(q + " ORDER BY first_utc, kind", args).fetchall()
        out = [dict(zip(self._COLS, r)) for r in rows]
        for d in out:
            d["values"] = json.loads(d.pop("values_json"))
            d["active"] = bool(d["active"])
        return out

    def log(self, position_id: str | None = None) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT position_id, kind, at_utc, repeat_no, action, reason, level FROM exit_call_log"
                                    + (" WHERE position_id=?" if position_id else "") + " ORDER BY id",
                                    (position_id,) if position_id else ()).fetchall()
        return [dict(zip(["position_id", "kind", "at_utc", "repeat_no", "action", "reason", "level"], r)) for r in rows]

    def interval(self, kind: str, level: str | None) -> timedelta:
        a = self.s.alerts
        if kind == SETTLEMENT and level:
            return timedelta(minutes=getattr(a.settlement_repeat_minutes, level, a.exit_call_repeat_minutes))
        return timedelta(minutes=a.exit_call_repeat_minutes)

    def apply(self, pos: Position, calls: list[dict], now: datetime, unresolvable=frozenset()) -> list[dict]:
        """Record this evaluation: activate new calls, refresh active ones, resolve what no longer applies, and return
        the calls DUE to be sent now (new, or repeated after their interval), each with its alert key.
        `unresolvable`: the kinds this evaluation could not judge (its inputs were not known); they stay active."""
        current = {c["kind"]: c for c in calls}
        existing = {c["kind"]: c for c in self.calls(pos.position_id)}
        due = []
        with self._lock:
            for kind, c in current.items():
                old = existing.get(kind)
                level = c.get("level")
                vals = json.dumps(c.get("values") or {}, sort_keys=True, default=str)
                if old is None or not old["active"]:
                    n = (old["repeat_no"] + 1) if old else 1
                    self._db.execute("INSERT OR REPLACE INTO exit_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (pos.position_id, kind, now.isoformat(), now.isoformat(), n, c["action"], c["reason"],
                                      vals, 1, None, None, level))
                    due.append({**c, "repeat_no": n})
                    continue
                send = now - datetime.fromisoformat(old["last_utc"]) >= self.interval(kind, level or old["level"]) \
                    or (kind == SETTLEMENT and c.get("overdue"))
                n = old["repeat_no"] + (1 if send else 0)
                self._db.execute("UPDATE exit_calls SET action=?, reason=?, values_json=?, level=?, repeat_no=?, last_utc=? "
                                 "WHERE position_id=? AND kind=?",
                                 (c["action"], c["reason"], vals, level or old["level"], n,
                                  now.isoformat() if send else old["last_utc"], pos.position_id, kind))
                if send:
                    due.append({**c, "repeat_no": n})
            for kind, old in existing.items():
                if not old["active"] or kind in current:
                    continue
                resolve = None
                if kind in WHILE_TRUE and kind not in unresolvable:
                    resolve = "condition no longer holds"
                elif kind in (BREAKEVEN, TRAIL):
                    sug = (old["values"] or {}).get("suggested_stop")
                    fav_up = (pos.direction == "LONG") if not pos.is_option else not pos.selling
                    if pos.stop is not None and sug is not None and ((pos.stop >= sug) if fav_up else (pos.stop <= sug)):
                        resolve = f"stop confirmed at {fmt(pos.stop)}"
                elif kind == PARTIAL and pos.plan.get("partial_booked"):
                    resolve = "partial profit recorded"
                if resolve:
                    self._db.execute("UPDATE exit_calls SET active=0, resolved_utc=?, resolved_why=? WHERE position_id=? AND kind=?",
                                     (now.isoformat(), resolve, pos.position_id, kind))
                    continue
                # sticky: repeat on its interval although the condition is not re-confirmed now (e.g. a stale price)
                if now - datetime.fromisoformat(old["last_utc"]) >= self.interval(kind, old["level"]):
                    n = old["repeat_no"] + 1
                    self._db.execute("UPDATE exit_calls SET repeat_no=?, last_utc=? WHERE position_id=? AND kind=?",
                                     (n, now.isoformat(), pos.position_id, kind))
                    due.append({"kind": kind, "action": old["action"], "reason": old["reason"], "values": old["values"],
                                "level": old["level"], "repeat_no": n, "still_active": True})
            for c in due:
                self._db.execute("INSERT INTO exit_call_log (position_id, kind, at_utc, repeat_no, action, reason, values_json, level)"
                                 " VALUES (?,?,?,?,?,?,?,?)",
                                 (pos.position_id, c["kind"], now.isoformat(), c["repeat_no"], c["action"], c["reason"],
                                  json.dumps(c.get("values") or {}, sort_keys=True, default=str), c.get("level")))
            self._db.commit()
        for c in due:
            c["position_id"] = pos.position_id
            c["key"] = f"exit:{pos.position_id}:{c['kind']}:{c['repeat_no']}"
        return due

    def resolve_closed(self, position_id: str, now: datetime) -> None:
        with self._lock:
            self._db.execute("UPDATE exit_calls SET active=0, resolved_utc=?, resolved_why='position closed' "
                             "WHERE position_id=? AND active=1", (now.isoformat(), position_id))
            self._db.commit()


def unresolvable_kinds(x: ExitInputs) -> frozenset:
    """The while-true calls an evaluation cannot resolve: with the position's prices missing, none; with the regime
    UNKNOWN (or not known), REGIME_FLIP. Missing data never silences a call."""
    out = set()
    if x.prices is None:
        out |= WHILE_TRUE
    if not x.regime or x.regime == "UNKNOWN":
        out.add(REGIME_FLIP)
    return frozenset(out)


def evaluate_positions(journal, book: ExitCallBook, settings, calendar, now: datetime, inputs_for, costs=None) -> list[dict]:
    """Evaluate every open position I hold (taken or manual; paper trades are managed by app/paper.py): store the
    calls and the plan, resolve the calls of closed positions, and return the calls due to be sent now."""
    due: list[dict] = []
    for p in journal.positions("CLOSED"):
        if book.calls(p.position_id, active_only=True):
            book.resolve_closed(p.position_id, now)
    for pos in journal.positions("OPEN"):
        x = inputs_for(pos)
        calls, plan = evaluate(pos, x, settings, calendar, costs)
        s = settlement_call(pos, x.spot, now, settings, calendar)
        if s is not None:
            calls.append(s)
            from .options.settlement import escalate
            plan["settlement_level"] = escalate(plan.get("settlement_level"), s["level"])
        if plan != pos.plan:
            journal.update_plan(pos.position_id, plan)
            pos = replace(pos, plan=plan)
        for c in book.apply(pos, calls, now, unresolvable_kinds(x)):
            due.append({**c, "symbol": pos.symbol, "trade_type": pos.trade_type, "hedged": pos.hedged,
                        "origin": pos.origin})
    return due
