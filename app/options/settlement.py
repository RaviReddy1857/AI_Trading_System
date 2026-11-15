"""Phase 9 M3: settlement readiness for short STOCK option legs (stock options settle physically).

Alerts and estimates only. Nothing here closes, changes or stores a position: every function is a pure
function of the position, the last price, the time and config.yaml (`stock_option_selling.settlement`),
and returns new dictionaries. Sending and repeating the alerts is phase 6; the user marks a position
closed in the journal.

It applies to every open position with a sold stock option leg: taken from a card, entered by hand, or
UNHEDGED. Days are NSE trading days before expiry: E-7 = seven trading days left, E = expiry day.

1. Alert level of a sold leg (`leg_level`):
   - nothing before `alert_from_trading_days` (E-7), and never for a leg far out of the money;
   - NOTICE: near the money (strike within `near_money_pct` of spot, not in the money), E-7 and E-6;
   - WARNING: in the money, E-7 and E-6;
   - URGENT: in the money or near the money from `urgent_from_trading_days` (E-5) on, or past an ICICI
     cut-off; and whenever the spot price or the expiry is not known ("price unknown": never silence).
   A position's level only goes up while it is open (`escalate`).
2. Delivery and margin estimate (`delivery_estimate`): what settling at the last price would mean, for the
   whole position and for one lot, netted with a bought leg only when that leg would settle in the money
   too; and the expiry-week delivery margin from ICICI's schedule. A rule that is not filled in gives
   "margin unknown", never zero.
3. Planned-exit deadline (`exit_deadline`): the stricter (earlier) of the forced exit and ICICI's own
   times. A position still open after it gets a red alert on every call until it is closed (`overdue`).
4. Every ICICI rule used carries its `confirmed` flag; an unconfirmed one is named in `not_confirmed` as
   "ICICI rule not confirmed: ...".
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..config import hhmm
from ..quality import IST
from .stock_selling import trading_days_to

NOTICE, WARNING, URGENT = "NOTICE", "WARNING", "URGENT"
LEVELS = (None, NOTICE, WARNING, URGENT)
NOT_CONFIRMED = "ICICI rule not confirmed"
MARGIN_UNKNOWN = "margin unknown"
PRICE_UNKNOWN = "price unknown"
MARKET_CLOSE = time(15, 30)


@dataclass(frozen=True)
class HeldLeg:
    action: str                   # SELL | BUY
    right: str                    # PE | CE
    strike: float
    lots: int
    lot_size: int

    @property
    def shares(self) -> int:
        return self.lots * self.lot_size


@dataclass(frozen=True)
class HeldPosition:
    """An open stock option position as the user holds it (frozen: nothing here can change it)."""
    underlying: str
    expiry: date | None           # None = not known
    legs: tuple[HeldLeg, ...]
    label: str = ""               # e.g. "from a card", "manual", "UNHEDGED"

    @property
    def shorts(self) -> tuple[HeldLeg, ...]:
        return tuple(x for x in self.legs if x.action == "SELL")

    @property
    def longs(self) -> tuple[HeldLeg, ...]:
        return tuple(x for x in self.legs if x.action == "BUY")


def escalate(previous: str | None, now: str | None) -> str | None:
    """The higher of two levels: a position's level never drops while it is open."""
    return max(previous, now, key=LEVELS.index)


def in_the_money(leg: HeldLeg, spot: float) -> bool:
    return spot < leg.strike if leg.right == "PE" else spot > leg.strike


def moneyness(leg: HeldLeg, spot: float | None, near_money_pct: float) -> dict:
    """Where the leg stands against the last price. `distance_pct`: strike against spot, % of spot;
    `itm_inr_per_share` / `itm_inr`: how far in the money (0 when it is not)."""
    if spot is None or not spot > 0:
        return {"state": PRICE_UNKNOWN, "spot": None, "distance_pct": None, "itm_inr_per_share": None, "itm_inr": None}
    itm = in_the_money(leg, spot)
    dist = abs(leg.strike - spot) / spot * 100.0
    depth = abs(leg.strike - spot) if itm else 0.0
    state = "IN_THE_MONEY" if itm else ("NEAR_THE_MONEY" if dist <= near_money_pct else "OUT_OF_THE_MONEY")
    return {"state": state, "spot": spot, "distance_pct": round(dist, 2), "itm_inr_per_share": round(depth, 2),
            "itm_inr": round(depth * leg.shares, 2)}


def days_left(today: date, expiry: date | None, is_trading_day: Callable[[date], bool] | None = None) -> int | None:
    """Trading days to expiry (0 on expiry day, negative after it); None when the expiry is not known."""
    if expiry is None:
        return None
    if today > expiry:
        return -trading_days_to(expiry, today, is_trading_day)
    return trading_days_to(today, expiry, is_trading_day)


def leg_level(leg: HeldLeg, spot: float | None, left: int | None, cfg, past_cutoff: bool = False) -> tuple[str | None, str]:
    """(level, why) of one SOLD leg. `left`: trading days to expiry (None = expiry not known).
    `past_cutoff`: an ICICI cut-off has passed. `cfg`: stock_option_selling.settlement."""
    if left is None:
        return URGENT, f"{PRICE_UNKNOWN}: the expiry is not known"
    if left > cfg.alert_from_trading_days:
        return None, f"{left} trading days to expiry: alerts start at E-{cfg.alert_from_trading_days}"
    m = moneyness(leg, spot, cfg.near_money_pct)
    if m["state"] == PRICE_UNKNOWN:
        return URGENT, f"{PRICE_UNKNOWN}: no usable price of the stock, {max(left, 0)} trading day(s) to expiry"
    if m["state"] == "OUT_OF_THE_MONEY":
        return None, f"out of the money by {m['distance_pct']:.2f}% of spot (more than {cfg.near_money_pct:g}%)"
    where = "in the money" if m["state"] == "IN_THE_MONEY" else f"within {cfg.near_money_pct:g}% of spot"
    if left <= cfg.urgent_from_trading_days or past_cutoff:
        why = "past an ICICI cut-off" if past_cutoff and left > cfg.urgent_from_trading_days else \
            f"E-{max(left, 0)}, at or inside E-{cfg.urgent_from_trading_days}"
        return URGENT, f"{where}; {why}"
    return (WARNING if m["state"] == "IN_THE_MONEY" else NOTICE), f"{where}; E-{left}"


# ---------------- ICICI's rules: which are used, and which are not confirmed ----------------
def _session_before_expiry(expiry: date, n: int, is_trading_day=None) -> date:
    """The session with n trading days left to expiry (n = 0: expiry day itself)."""
    d = expiry
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5 and (is_trading_day is None or is_trading_day(d)):
            n -= 1
    return d


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, IST)


def cutoffs(pos: HeldPosition, sos, is_trading_day=None) -> list[dict]:
    """Every moment by which the position should be out, earliest first: the system's forced exit and
    ICICI's own times, each with what set it and whether ICICI's rule is confirmed. `sos`:
    stock_option_selling. Empty when the expiry is not known."""
    if pos.expiry is None:
        return []
    b = sos.settlement.broker
    forced = sos.forced_exit_trading_days_before_expiry
    out = [{"at": _at(_session_before_expiry(pos.expiry, forced, is_trading_day), MARKET_CLOSE), "broker": False,
            "confirmed": True, "rule": "forced exit",
            "what": f"forced exit: no position inside the last {forced} trading days (the close of E-{forced})"}]
    dm = b.delivery_margin
    if dm.schedule_pct and pos.shorts:
        first = max(dm.schedule_pct)
        day = _session_before_expiry(pos.expiry, first, is_trading_day)
        t = hhmm(dm.collected_daily_at) if first in dm.collected_daily_on_days and dm.collected_daily_at else time(9, 15)
        out.append({"at": _at(day, t), "broker": True, "confirmed": dm.confirmed, "rule": "delivery margin",
                    "what": f"ICICI delivery margin starts ({dm.schedule_pct[first]:g}% of {dm.base or 'the margin base'} "
                            f"on E-{first})"})
    t = b.auto_close.deadline(has_long=bool(pos.longs), has_short=bool(pos.shorts))
    if t is not None:
        out.append({"at": _at(pos.expiry, t), "broker": True, "confirmed": b.auto_close.confirmed, "rule": "auto close",
                    "what": "ICICI closes open option positions itself on expiry day (earliest stated time for the legs held)"})
    ob = b.obligation
    if ob.call_shares_in_demat_by and any(x.right == "CE" for x in pos.shorts):
        out.append({"at": _at(pos.expiry, hhmm(ob.call_shares_in_demat_by)), "broker": True,
                    "confirmed": ob.call_shares_deadline_confirmed, "rule": "call shares deadline",
                    "what": "shares for a sold call must be in demat by this time on expiry day"})
    return sorted(out, key=lambda c: c["at"])


def exit_deadline(pos: HeldPosition, sos, is_trading_day=None) -> dict | None:
    """The planned-exit deadline: the earliest cut-off. None when the expiry is not known (the alert is
    then URGENT "price unknown" and the position counts as overdue)."""
    cs = cutoffs(pos, sos, is_trading_day)
    return cs[0] if cs else None


def overdue(pos: HeldPosition, now: datetime, closed: bool, sos, is_trading_day=None) -> dict | None:
    """The red alert for a planned exit that is not confirmed: returned on EVERY call after the deadline
    while the position is not marked closed (the caller repeats it), None once it is closed."""
    if closed or not pos.shorts:
        return None
    dl = exit_deadline(pos, sos, is_trading_day)
    if dl is None:
        return {"kind": "EXIT_OVERDUE", "colour": "red", "repeat": True, "deadline": None,
                "text": f"{pos.underlying}: expiry not known, so no exit deadline can be set: check this position now"}
    if now < dl["at"]:
        return None
    return {"kind": "EXIT_OVERDUE", "colour": "red", "repeat": True, "deadline": dl["at"].isoformat(),
            "text": f"{pos.underlying}: NOT marked closed after the exit deadline {dl['at']:%d-%b-%Y %H:%M} "
                    f"({dl['what']}). Exit the position and mark it closed in the journal."}


# ---------------- delivery and margin estimate ----------------
def _pct(x: float | None, capital: float) -> float | None:
    return None if x is None or not capital > 0 else round(x / capital * 100.0, 2)


def _delivery(pos: HeldPosition, spot: float | None, scale: float, capital: float) -> dict:
    """Settlement of the position at the last price, each quantity multiplied by `scale` (1 = the whole
    position). For each sold leg: what it obliges if assigned (known without a price), and what would
    happen at the last price, netted with bought legs of the same right that would settle in the money."""
    rows = []
    for leg in pos.shorts:
        sh = leg.shares * scale
        put = leg.right == "PE"
        row = {"leg": f"sold {leg.strike:g} {leg.right}", "shares": round(sh, 4),
               # the obligation itself, whatever the price: a put buys the shares at the strike, a call delivers them
               "if_assigned": {"shares_to_buy" if put else "shares_to_deliver": round(sh, 4),
                               "cash_needed_inr" if put else "cash_received_inr": round(sh * leg.strike, 2),
                               "cash_pct_of_capital": _pct(sh * leg.strike, capital)}}
        if spot is None or not spot > 0:
            row["at_last_price"] = None                     # unknown: never assumed out of the money
            rows.append(row)
            continue
        if not in_the_money(leg, spot):
            row["at_last_price"] = {"settles": False, "note": "out of the money at the last price: no delivery"}
            rows.append(row)
            continue
        hedge = [x for x in pos.longs if x.right == leg.right and in_the_money(x, spot)]
        netted = min(sh, sum(x.shares for x in hedge) * scale)
        # cash per netted share: a put pair buys at the sold strike and sells at the bought one; a call pair the reverse
        net_cash = sum(min(sh, x.shares * scale) * ((leg.strike - x.strike) if put else (x.strike - leg.strike))
                       for x in hedge[:1]) if hedge else 0.0
        left = sh - netted
        at = {"settles": True, "shares_to_buy" if put else "shares_to_deliver": round(left, 4),
              "netted_with_bought_leg_shares": round(netted, 4),
              "net_cash_paid_on_netted_shares_inr": round(net_cash, 2),
              "delivery_value_inr": round(left * leg.strike, 2),
              "delivery_value_pct_of_capital": _pct(left * leg.strike, capital)}
        if put:
            at["shares_worth_at_spot_inr"] = round(left * spot, 2)
        else:
            at["cost_to_buy_shares_at_spot_if_not_held_inr"] = round(left * spot, 2)
            at["cost_pct_of_capital"] = _pct(left * spot, capital)
        row["at_last_price"] = at
        rows.append(row)
    return {"legs": rows}


def delivery_margin(pos: HeldPosition, spot: float | None, left: int | None, sos,
                    base_margin_inr: float | None = None, capital: float = 0.0, near_month: bool = True) -> dict:
    """ICICI's expiry-week delivery margin for the position today: the schedule's share of the margin
    base (VaR + ELM + adhoc). `base_margin_inr`: that base for the whole position in rupees, as the
    user reads it from ICICI; without it the share is shown and the rupee amount is unknown. A rule
    that is not filled in, or an unknown price or expiry: "margin unknown" - never zero."""
    dm = sos.settlement.broker.delivery_margin
    out = {"rule": "delivery margin", "confirmed": dm.confirmed, "base": dm.base or None, "pct_of_base": None,
           "margin_inr": None, "margin_pct_of_capital": None, "schedule_pct": dict(sorted(dm.schedule_pct.items(), reverse=True))}
    if not dm.schedule_pct or dm.ctm_within_pct_of_spot is None:
        return {**out, "status": MARGIN_UNKNOWN, "colour": "red", "why": "ICICI's delivery-margin rule is not filled in"}
    if left is None or spot is None or not spot > 0:
        return {**out, "status": MARGIN_UNKNOWN, "colour": "red",
                "why": "the expiry is not known" if left is None else "no usable price of the stock"}
    first = max(dm.schedule_pct)
    hit = [x for x in pos.shorts if moneyness(x, spot, dm.ctm_within_pct_of_spot)["state"] != "OUT_OF_THE_MONEY"]
    if not hit or (dm.near_month_only and not near_month):
        return {**out, "status": "NOT_APPLICABLE", "pct_of_base": 0.0, "margin_inr": 0.0, "margin_pct_of_capital": 0.0,
                "why": (f"no sold leg is in the money or within {dm.ctm_within_pct_of_spot:g}% of spot" if not hit
                        else "the rule covers near-month contracts only")}
    if left > first:
        return {**out, "status": "NOT_STARTED", "pct_of_base": 0.0, "margin_inr": 0.0, "margin_pct_of_capital": 0.0,
                "why": f"E-{left}: the schedule starts on E-{first} at {dm.schedule_pct[first]:g}%"}
    pct = dm.schedule_pct[max(left, 0)]
    inr = None if base_margin_inr is None else round(base_margin_inr * pct / 100.0, 2)
    return {**out, "status": "CHARGED" if inr is not None else MARGIN_UNKNOWN,
            "colour": None if inr is not None else "red", "pct_of_base": pct, "margin_inr": inr,
            "margin_pct_of_capital": _pct(inr, capital),
            "why": f"E-{max(left, 0)}: {pct:g}% of {dm.base or 'the margin base'}"
                   + ("" if inr is not None else f"; rupee amount unknown: the {dm.base or 'margin base'} of this position "
                                                 f"is not entered")}


def delivery_estimate(pos: HeldPosition, spot: float | None, left: int | None, sos, capital: float,
                      base_margin_inr: float | None = None, near_month: bool = True) -> dict:
    """Item 2: the delivery obligation at the last price (the whole position and one lot) and the
    expiry-week margin. All figures are computed here; rupee figures also as % of capital."""
    lots = max((x.lots for x in pos.shorts), default=0)
    return {"position": _delivery(pos, spot, 1.0, capital),
            "one_lot": _delivery(pos, spot, 1.0 / lots, capital) if lots else {"legs": []},
            "margin": delivery_margin(pos, spot, left, sos, base_margin_inr, capital, near_month),
            "label": "estimate at the last price, not a forecast"}


# ---------------- the alert of one position ----------------
def not_confirmed(pos: HeldPosition, sos, used_cutoffs: Sequence[dict]) -> list[str]:
    """ "ICICI rule not confirmed: <rule>" for every ICICI rule this position's alert uses."""
    b = sos.settlement.broker
    names = [c["rule"] for c in used_cutoffs if c["broker"] and not c["confirmed"]]
    if pos.shorts and not b.delivery_margin.confirmed and "delivery margin" not in names:
        names.append("delivery margin")
    if pos.shorts and not b.obligation.confirmed:
        names.append("settlement obligation")
    if not b.fresh_position_block.confirmed:
        names.append("fresh-position block in the last days (not known)")
    return [f"{NOT_CONFIRMED}: {n}" for n in dict.fromkeys(names)]


def settlement_alert(pos: HeldPosition, spot: float | None, now: datetime, sos, capital: float,
                     previous_level: str | None = None, closed: bool = False, is_trading_day=None,
                     base_margin_inr: float | None = None, near_month: bool = True) -> dict | None:
    """The settlement-readiness alert of one open position at `now`, or None when the position is
    closed, has no sold leg, or nothing applies yet (no level and not overdue). `previous_level`: the
    level of its last alert (the level never drops while the position is open). The result states the
    level, each sold leg (strike, spot, moneyness in % and rupees, days left), the estimate, the
    deadline, the action and every unconfirmed ICICI rule used. It is advice: nothing is changed."""
    if closed or not pos.shorts:
        return None
    st = sos.settlement
    today = now.astimezone(IST).date()
    left = days_left(today, pos.expiry, is_trading_day)
    cs = cutoffs(pos, sos, is_trading_day)
    past = any(c["broker"] and now >= c["at"] for c in cs)
    legs, level = [], None
    for leg in pos.shorts:
        lv, why = leg_level(leg, spot, left, st, past)
        level = escalate(level, lv)
        legs.append({"leg": f"sold {leg.strike:g} {leg.right}", "strike": leg.strike, "right": leg.right,
                     "lots": leg.lots, "lot_size": leg.lot_size, "level": lv, "why": why,
                     **moneyness(leg, spot, st.near_money_pct)})
    level = escalate(previous_level, level)
    late = overdue(pos, now, closed, sos, is_trading_day)
    if level is None and late is None:
        return None
    dl = cs[0] if cs else None
    by = f"{dl['at']:%d-%b-%Y %H:%M}" if dl else "now (the expiry is not known)"
    kind = "spread" if pos.longs else "position"
    return {"underlying": pos.underlying, "label": pos.label, "level": level, "colour": "red" if late or level == URGENT else None,
            "expiry": pos.expiry.isoformat() if pos.expiry else None, "trading_days_left": left,
            "legs": legs, "estimate": delivery_estimate(pos, spot, left, sos, capital, base_margin_inr, near_month),
            "deadline": dl["at"].isoformat() if dl else None, "deadline_set_by": dl["what"] if dl else None,
            "cutoffs": [{**c, "at": c["at"].isoformat()} for c in cs],
            "action": f"Exit the {kind} by {by}", "overdue": late,
            "not_confirmed": not_confirmed(pos, sos, cs),
            "note": "alert and estimate only: the system never closes or changes a position"}


def rules_view(sos) -> dict:
    """ICICI's rules as stored, for the stock option page: each with its confirmed flag and, when it is
    not confirmed, the label shown on every card and alert that uses it."""
    b = sos.settlement.broker
    st = sos.settlement

    def row(name: str, confirmed: bool, value: str, source: str = "") -> dict:
        return {"rule": name, "confirmed": confirmed, "value": value or "not known", "source": source,
                "label": "" if confirmed else f"{NOT_CONFIRMED}: {name}"}

    dm, ob = b.delivery_margin, b.obligation
    sched = ", ".join(f"E-{d}: {p:g}%" if d else f"E: {p:g}%" for d, p in sorted(dm.schedule_pct.items(), reverse=True))
    return {"alerts_from": {"from_trading_days": st.alert_from_trading_days, "urgent_from_trading_days": st.urgent_from_trading_days,
                       "near_money_pct": st.near_money_pct,
                       "forced_exit_trading_days": sos.forced_exit_trading_days_before_expiry},
            "rules": [
                row("delivery margin", dm.confirmed,
                    f"{sched} of {dm.base}; in the money or within {dm.ctm_within_pct_of_spot:g}% of spot" if sched else "",
                    dm.source),
                row("auto close", b.auto_close.confirmed,
                    "; ".join(f"long options {s.long_options or '-'}, short options {s.short_options or '-'}"
                              for s in b.auto_close.sources), "; ".join(s.source for s in b.auto_close.sources)),
                row("fresh-position block in the last days", b.fresh_position_block.confirmed, b.fresh_position_block.rule,
                    b.fresh_position_block.source),
                row("settlement obligation", ob.confirmed,
                    "sold put: strike x lot size per lot in cash; sold call: the shares in demat" if ob.confirmed or ob.source else "",
                    ob.source),
                row("call shares deadline", ob.call_shares_deadline_confirmed,
                    f"in demat by {ob.call_shares_in_demat_by} on expiry day" if ob.call_shares_in_demat_by else ""),
            ],
            "positions_note": "No open positions are recorded yet (phase 6 adds the journal): the rules and estimates are "
                              "built and tested, alerts are not sent."}
