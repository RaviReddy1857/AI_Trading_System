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
     cut-off; and whenever the expiry is not known.
   The price is the last valid continuous-session price, with its time (`spot_as_of`): auction, rejected,
   quarantined and stale ticks never replace it and never by themselves change the level (user design,
   2026-11-19). With no valid price at all, a sold leg is assumed NEAR the money (conservative: NOTICE on E-7 and
   E-6, URGENT from E-5) and the alert says so. A dead or noisy feed is a separate notice
   (`feed.held_price_notice`, app/engine.py), never a settlement level.
   A position's level only goes up while it is open (`escalate`).
2. Delivery and margin estimate (`delivery_estimate`): what settling at the last price would mean, for the
   whole position and for one lot. Bought legs that would settle in the money too are allocated ONCE across
   the whole position (same right; the most exposed sold legs first, see `allocate_hedges`): shares and cash
   are computed for every allocated portion, and sold quantity without a bought share is fully deliverable.
   Also the expiry-week delivery margin from ICICI's schedule. A rule that is not filled in gives
   "margin unknown", never zero.
3. Planned-exit deadline (`exit_deadline`): the stricter (earlier) of the forced exit and ICICI's own
   times. The forced exit is due at `settlement.forced_exit_by` (15:00) on its day, while the stock still trades
   continuously: from then the alert is sent as "exit now" (`due_now`), whatever the moneyness. Its hard limit is
   the options' close that day (15:40 from 3 Aug 2026); an ICICI time is its own hard limit. A position still open
   after the earliest hard limit gets a red alert on every call until it is closed (`overdue`).
   Trading days are counted with the holiday calendar (data/holidays.csv). When no COVERAGE row verifies the
   holiday list for the expiry month, a holiday may be missing: the count is then taken on weekdays LESS ONE
   (one session earlier, enough for one missing holiday but not for two), and the alert says so (`calendar`)
   and marks its deadline UNVERIFIED in the alert, the action and the Telegram text (`DEADLINE_UNVERIFIED`).
4. Every ICICI rule used carries its `confirmed` flag; an unconfirmed one is named in `not_confirmed` as
   "ICICI rule not confirmed: ...".
"""
from __future__ import annotations

import math
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
NO_VALID_PRICE = "no valid price of the stock, so the sold leg is assumed near the money (conservative)"


def usable_spot(spot) -> float | None:
    """The stock's last price when it can be used: a finite number above 0 (None, NaN, infinity, 0 or a negative
    price are unknown - PRICE_UNKNOWN, never assumed out of the money)."""
    try:
        return float(spot) if spot is not None and not isinstance(spot, bool) and math.isfinite(spot) and spot > 0 else None
    except TypeError:
        return None


def usable_margin_base(base) -> tuple[float | None, str | None]:
    """(base, problem): the margin base the user entered when it is a finite number at or above 0; None with the
    problem when it is NaN, infinite, negative or not a number (MARGIN_UNKNOWN); (None, None) when not entered."""
    if base is None:
        return None, None
    try:
        if not isinstance(base, bool) and math.isfinite(base) and base >= 0:
            return float(base), None
    except TypeError:
        pass
    return None, f"the margin base entered ({base!r}) is not a finite amount at or above 0"


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
    spot = usable_spot(spot)
    if spot is None:
        return {"state": PRICE_UNKNOWN, "spot": None, "distance_pct": None, "itm_inr_per_share": None, "itm_inr": None}
    itm = in_the_money(leg, spot)
    dist = abs(leg.strike - spot) / spot * 100.0
    depth = abs(leg.strike - spot) if itm else 0.0
    state = "IN_THE_MONEY" if itm else ("NEAR_THE_MONEY" if dist <= near_money_pct else "OUT_OF_THE_MONEY")
    return {"state": state, "spot": spot, "distance_pct": round(dist, 2), "itm_inr_per_share": round(depth, 2),
            "itm_inr": round(depth * leg.shares, 2)}


CALENDAR_COVERED = "trading days counted with the holiday calendar (coverage verified)"
HOLIDAY_COVERAGE_MISSING = "HOLIDAY COVERAGE MISSING"
# a deadline counted without a verified holiday list: weekdays less one is one session early for ONE missing holiday,
# but two or more missing holidays in the expiry month put it late, so it is never presented as verified
DEADLINE_UNVERIFIED = "deadline unverified: holiday calendar missing; confirm before relying on it"
COVERAGE_HOW = ("Add the NSE holiday list to data/holidays.csv with a COVERAGE row for the range it covers, e.g. "
                "',2027-01-01,,COVERAGE through 2027-12-31 NSE circular <number>'.")


def uncovered_years(calendar, first: date, last: date) -> list[int]:
    """The calendar years from `first` to `last` whose holiday list is not VERIFIED complete for the part of the range
    inside them (a COVERAGE row of data/holidays.csv, `MarketCalendar.covers`). Holiday rows alone never count: a year
    with some holidays entered may still miss one. No calendar: all."""
    covers = getattr(calendar, "covers", None)
    out = []
    for y in range(first.year, last.year + 1):
        a, b = max(first, date(y, 1, 1)), min(last, date(y, 12, 31))
        if covers is None or not covers(a, b):
            out.append(y)
    return out


@dataclass(frozen=True)
class DayCount:
    """How trading days to one expiry are counted. `covered`: the holiday calendar covers the expiry month
    (the 31 days up to expiry). Not covered: weekdays less one - one session earlier, never later."""
    expiry: date | None
    covered: bool
    note: str
    is_trading_day: Callable[[date], bool] | None = None

    def left(self, today: date) -> int | None:
        """Trading days to expiry (0 on expiry day, negative after it); None when the expiry is not known."""
        if self.expiry is None:
            return None
        if today > self.expiry:
            return -trading_days_to(self.expiry, today, self.is_trading_day)
        n = trading_days_to(today, self.expiry, self.is_trading_day)
        return n if self.covered else max(n - 1, 0)

    def session(self, n: int) -> date:
        """The session with n trading days left to expiry (0: expiry day); not covered: one session earlier."""
        d, n = self.expiry, n if self.covered else n + 1
        while n > 0:
            d -= timedelta(days=1)
            if d.weekday() < 5 and (self.is_trading_day is None or self.is_trading_day(d)):
                n -= 1
        return d


def day_count(expiry: date | None, calendar=None) -> DayCount:
    """`calendar`: the app's MarketCalendar (holidays.csv), or None when there is none."""
    if expiry is None:
        return DayCount(None, False, "the expiry is not known")
    missing = uncovered_years(calendar, expiry - timedelta(days=31), expiry)
    if not missing:
        return DayCount(expiry, True, CALENDAR_COVERED, calendar.is_trading_day)
    why = "no holiday calendar" if calendar is None else \
        f"the holiday list is not verified complete for {', '.join(map(str, missing))} (no COVERAGE row)"
    return DayCount(expiry, False, f"{HOLIDAY_COVERAGE_MISSING}: {why}: trading days are counted on weekdays less one, "
                                   f"so alerts and the deadline come one session earlier. {COVERAGE_HOW}",
                    calendar.is_trading_day if calendar is not None else None)


def days_left(today: date, expiry: date | None, calendar=None) -> int | None:
    """Trading days to expiry by the holiday calendar (conservative when it does not cover the expiry month)."""
    return day_count(expiry, calendar).left(today)


def leg_level(leg: HeldLeg, spot: float | None, left: int | None, cfg, past_cutoff: bool = False) -> tuple[str | None, str]:
    """(level, why) of one SOLD leg. `left`: trading days to expiry (None = expiry not known).
    `past_cutoff`: an ICICI cut-off has passed. `cfg`: stock_option_selling.settlement."""
    if left is None:
        return URGENT, f"{PRICE_UNKNOWN}: the expiry is not known"
    if left > cfg.alert_from_trading_days:
        return None, f"{left} trading days to expiry: alerts start at E-{cfg.alert_from_trading_days}"
    m = moneyness(leg, spot, cfg.near_money_pct)
    if m["state"] == PRICE_UNKNOWN:
        # no valid price at all: the conservative assumption, near the money (the schedule then decides the level)
        lv = URGENT if left <= cfg.urgent_from_trading_days or past_cutoff else NOTICE
        return lv, (f"{PRICE_UNKNOWN}: {NO_VALID_PRICE}; E-{max(left, 0)}"
                    + (", at or inside E-%d" % cfg.urgent_from_trading_days if left <= cfg.urgent_from_trading_days
                       else ", past an ICICI cut-off" if past_cutoff else ""))
    if m["state"] == "OUT_OF_THE_MONEY":
        return None, f"out of the money by {m['distance_pct']:.2f}% of spot (more than {cfg.near_money_pct:g}%)"
    where = "in the money" if m["state"] == "IN_THE_MONEY" else f"within {cfg.near_money_pct:g}% of spot"
    if left <= cfg.urgent_from_trading_days or past_cutoff:
        why = "past an ICICI cut-off" if past_cutoff and left > cfg.urgent_from_trading_days else \
            f"E-{max(left, 0)}, at or inside E-{cfg.urgent_from_trading_days}"
        return URGENT, f"{where}; {why}"
    return (WARNING if m["state"] == "IN_THE_MONEY" else NOTICE), f"{where}; E-{left}"


# ---------------- ICICI's rules: which are used, and which are not confirmed ----------------
def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, IST)


def forced_exit_cutoff(expiry: date | None, sos, calendar=None) -> dict | None:
    """The system's forced exit for an expiry, counted with the holiday calendar exactly as the settlement alert counts
    it (`day_count`; not covered: one session earlier): the session `forced_exit_trading_days_before_expiry` trading
    days before expiry (`day`), exit by `settlement.forced_exit_by` that day (`at`) while the stock trades continuously,
    the options' close as the hard limit, and `verified` False when the holiday list does not cover the expiry month
    (DEADLINE_UNVERIFIED). One source for the SETTLEMENT and FORCED_EXPIRY_EXIT calls. None when the expiry is not
    known."""
    if expiry is None:
        return None
    from ..session import DERIVATIVES, hours as session_hours
    dc = day_count(expiry, calendar)
    forced = sos.forced_exit_trading_days_before_expiry
    by = getattr(getattr(sos, "settlement", None), "forced_exit_by", "15:00")
    fday = dc.session(forced)
    # exit by forced_exit_by (15:00), while the stock trades continuously; hard limit: the options' close that day
    # (15:30 before 3 Aug 2026, 15:40 after)
    return {"day": fday, "at": _at(fday, hhmm(by)), "hard_limit": _at(fday, session_hours(DERIVATIVES, fday).close),
            "by": by, "forced": forced, "verified": dc.covered, "days": dc}


def cutoffs(pos: HeldPosition, sos, calendar=None) -> list[dict]:
    """Every moment by which the position should be out, earliest first: the system's forced exit and
    ICICI's own times, each with what set it and whether ICICI's rule is confirmed. `sos`:
    stock_option_selling. Empty when the expiry is not known."""
    if pos.expiry is None:
        return []
    fc = forced_exit_cutoff(pos.expiry, sos, calendar)
    dc = fc["days"]
    b = sos.settlement.broker
    forced = fc["forced"]
    out = [{"at": fc["at"], "hard_limit": fc["hard_limit"], "broker": False, "confirmed": True, "rule": "forced exit",
            "what": f"forced exit: no position inside the last {forced} trading days (by "
                    f"{fc['by']} on E-{forced}, while the stock trades continuously)"}]
    dm = b.delivery_margin
    if dm.schedule_pct and pos.shorts:
        first = max(dm.schedule_pct)
        day = dc.session(first)
        t = hhmm(dm.collected_daily_at) if first in dm.collected_daily_on_days and dm.collected_daily_at else time(9, 15)
        out.append({"at": _at(day, t), "broker": True, "confirmed": dm.confirmed, "rule": "delivery margin",
                    "what": f"ICICI delivery margin starts ({dm.schedule_pct[first]:g}% of {dm.base or 'the margin base'} "
                            f"on E-{first})"})
    t = b.auto_close.deadline(has_long=bool(pos.longs), has_short=bool(pos.shorts))
    if t is not None:
        out.append({"at": _at(dc.session(0), t), "broker": True, "confirmed": b.auto_close.confirmed, "rule": "auto close",
                    "what": "ICICI closes open option positions itself on expiry day (earliest stated time for the legs held)"})
    ob = b.obligation
    if ob.call_shares_in_demat_by and any(x.right == "CE" for x in pos.shorts):
        out.append({"at": _at(dc.session(0), hhmm(ob.call_shares_in_demat_by)), "broker": True,
                    "confirmed": ob.call_shares_deadline_confirmed, "rule": "call shares deadline",
                    "what": "shares for a sold call must be in demat by this time on expiry day"})
    for c in out:
        c.setdefault("hard_limit", c["at"])          # an ICICI time is its own hard limit
    return sorted(out, key=lambda c: c["at"])


def hard_limit(cs: Sequence[dict]) -> dict | None:
    """The cut-off whose hard limit comes first (the red, URGENT escalation starts there)."""
    return min(cs, key=lambda c: c["hard_limit"]) if cs else None


def exit_deadline(pos: HeldPosition, sos, calendar=None) -> dict | None:
    """The planned-exit deadline: the earliest cut-off. None when the expiry is not known (the alert is
    then URGENT "price unknown" and the position counts as overdue)."""
    cs = cutoffs(pos, sos, calendar)
    return cs[0] if cs else None


def overdue(pos: HeldPosition, now: datetime, closed: bool, sos, calendar=None) -> dict | None:
    """The red alert for a planned exit that is not confirmed: returned on EVERY call after the deadline
    while the position is not marked closed (the caller repeats it), None once it is closed."""
    if closed or not pos.shorts:
        return None
    dl = hard_limit(cutoffs(pos, sos, calendar))
    if dl is None:
        return {"kind": "EXIT_OVERDUE", "colour": "red", "repeat": True, "deadline": None,
                "text": f"{pos.underlying}: expiry not known, so no exit deadline can be set: check this position now"}
    if now < dl["hard_limit"]:
        return None
    verified = day_count(pos.expiry, calendar).covered
    return {"kind": "EXIT_OVERDUE", "colour": "red", "repeat": True, "deadline": dl["hard_limit"].isoformat(),
            "deadline_verified": verified,
            "text": f"{pos.underlying}: NOT marked closed after the exit deadline {dl['hard_limit']:%d-%b-%Y %H:%M} "
                    f"({dl['what']}{'' if verified else '; ' + DEADLINE_UNVERIFIED}). Exit the position and mark it "
                    "closed in the journal."}


# ---------------- delivery and margin estimate ----------------
def _pct(x: float | None, capital: float) -> float | None:
    return None if x is None or not capital > 0 else round(x / capital * 100.0, 2)


def allocate_hedges(pos: HeldPosition, spot: float, scale: float = 1.0) -> tuple[dict[int, list], list]:
    """Which bought quantity nets which sold quantity if the position settled at `spot`.

    A position is one underlying and one expiry; within it only legs of the same right net each other,
    and only when both would settle in the money. Every bought share is allocated ONCE:
    - sold legs are served most exposed first (deepest in the money: the highest strike for puts, the
      lowest for calls; equal strikes in the order held);
    - bought legs are used deepest in the money first (the highest strike for puts, the lowest for
      calls; equal strikes in the order held);
    - a bought leg is spread over as many sold legs as its quantity covers, and a sold leg may be
      covered by several bought legs.
    Sold quantity left without a bought share is fully deliverable. Returns
    ({index of the sold leg in pos.legs: [(bought leg, shares)]}, [(bought leg, shares not allocated)]),
    quantities multiplied by `scale`."""
    out: dict[int, list] = {}
    spare: list = []
    for right in ("PE", "CE"):
        deep = (lambda x: -x[1].strike) if right == "PE" else (lambda x: x[1].strike)
        live = [(n, x) for n, x in enumerate(pos.legs) if x.right == right and in_the_money(x, spot)]
        sold = sorted((x for x in live if x[1].action == "SELL"), key=deep)
        have = [[x, x.shares * scale] for _, x in sorted((x for x in live if x[1].action == "BUY"), key=deep)]
        for n, leg in sold:
            need = leg.shares * scale
            for h in have:
                take = min(need, h[1])
                if take > 0:
                    out.setdefault(n, []).append((h[0], take))
                    h[1] -= take
                    need -= take
        spare += [(x, q) for x, q in have if q > 1e-9]
    return out, spare


def _delivery(pos: HeldPosition, spot: float | None, scale: float, capital: float) -> dict:
    """Settlement of the position at the last price, each quantity multiplied by `scale` (1 = the whole
    position). For each sold leg: what it obliges if assigned (known without a price), and what would
    happen at the last price. Bought legs that would settle in the money too are allocated once across
    the whole position (`allocate_hedges`); each allocated portion is netted (no shares change hands,
    the difference of the two strikes is paid) and the rest of the sold quantity is delivered in full."""
    spot = usable_spot(spot)
    known = spot is not None
    alloc, spare = allocate_hedges(pos, spot, scale) if known else ({}, [])
    rows = []
    for n, leg in enumerate(pos.legs):
        if leg.action != "SELL":
            continue
        sh = leg.shares * scale
        put = leg.right == "PE"
        row = {"leg": f"sold {leg.strike:g} {leg.right}", "shares": round(sh, 4),
               # the obligation itself, whatever the price: a put buys the shares at the strike, a call delivers them
               "if_assigned": {"shares_to_buy" if put else "shares_to_deliver": round(sh, 4),
                               "cash_needed_inr" if put else "cash_received_inr": round(sh * leg.strike, 2),
                               "cash_pct_of_capital": _pct(sh * leg.strike, capital)}}
        if not known:
            row["at_last_price"] = None                     # unknown: never assumed out of the money
            rows.append(row)
            continue
        if not in_the_money(leg, spot):
            row["at_last_price"] = {"settles": False, "note": "out of the money at the last price: no delivery"}
            rows.append(row)
            continue
        # each allocated portion: a put pair buys at the sold strike and sells at the bought one (cash comes
        # back from the bought put); a call pair delivers at the sold strike and buys at the bought one
        parts = [{"leg": f"bought {x.strike:g} {x.right}", "shares": round(q, 4),
                  "cash_received_from_bought_leg_inr" if put else "cash_paid_on_bought_leg_inr": round(q * x.strike, 2),
                  "net_cash_paid_inr": round(q * ((leg.strike - x.strike) if put else (x.strike - leg.strike)), 2)}
                 for x, q in alloc.get(n, [])]
        netted = sum(q for _, q in alloc.get(n, []))
        net_cash = sum(q * ((leg.strike - x.strike) if put else (x.strike - leg.strike)) for x, q in alloc.get(n, []))
        left = sh - netted
        at = {"settles": True, "shares_to_buy" if put else "shares_to_deliver": round(left, 4),
              "netted_with_bought_leg_shares": round(netted, 4),
              "net_cash_paid_on_netted_shares_inr": round(net_cash, 2),
              "netted_with": parts,
              "delivery_value_inr": round(left * leg.strike, 2),
              "delivery_value_pct_of_capital": _pct(left * leg.strike, capital)}
        if put:
            at["shares_worth_at_spot_inr"] = round(left * spot, 2)
        else:
            at["cost_to_buy_shares_at_spot_if_not_held_inr"] = round(left * spot, 2)
            at["cost_pct_of_capital"] = _pct(left * spot, capital)
        row["at_last_price"] = at
        rows.append(row)
    out = {"legs": rows}
    if spare:
        # a bought option in the money is exercised too: more bought than sold leaves a delivery of its own
        out["bought_in_the_money_not_netted"] = [
            {"leg": f"bought {x.strike:g} {x.right}", "shares": round(q, 4),
             "note": "in the money and not netted with a sold leg: it settles by delivery itself "
                     + ("(shares to deliver)" if x.right == "PE" else "(shares to buy at the strike)")} for x, q in spare]
    return out


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
    base_margin_inr, base_problem = usable_margin_base(base_margin_inr)
    if base_problem:
        return {**out, "status": MARGIN_UNKNOWN, "colour": "red", "invalid_input": True, "why": base_problem}
    if left is None or usable_spot(spot) is None:
        return {**out, "status": MARGIN_UNKNOWN, "colour": "red",
                "why": "the expiry is not known" if left is None else f"{PRICE_UNKNOWN}: no usable price of the stock"}
    spot = usable_spot(spot)
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


def price_note(spot, spot_as_of: datetime | None, now: datetime, auction: bool = False) -> dict:
    """What price the alert used, and when: the last valid continuous-session price with its time (the date too when
    it is not today's), the closing auction, or the conservative assumption when there is no valid price."""
    px = usable_spot(spot)
    when = None
    if px is not None and spot_as_of is not None:
        t = spot_as_of.astimezone(IST)
        when = f"{t:%H:%M}" if t.date() == now.astimezone(IST).date() else f"{t:%d-%b %H:%M}"
    if px is None:
        note = ("auction in progress; " if auction else "") + NO_VALID_PRICE
    elif auction:
        note = f"auction in progress, last price as of {when or 'an earlier time'}"
    else:
        note = f"last price {px:g}" + (f" as of {when}" if when else "")
    return {"spot": px, "as_of": spot_as_of.isoformat() if px is not None and spot_as_of else None,
            "auction": bool(auction), "assumed_near_money": px is None, "note": note}


def settlement_alert(pos: HeldPosition, spot: float | None, now: datetime, sos, capital: float,
                     previous_level: str | None = None, closed: bool = False, calendar=None,
                     base_margin_inr: float | None = None, near_month: bool = True,
                     spot_as_of: datetime | None = None, auction: bool = False) -> dict | None:
    """The settlement-readiness alert of one open position at `now`, or None when the position is
    closed, has no sold leg, or nothing applies yet (no level and not overdue). `previous_level`: the
    level of its last alert (the level never drops while the position is open). The result states the
    level, each sold leg (strike, spot, moneyness in % and rupees, days left), the estimate, the
    deadline, the action and every unconfirmed ICICI rule used. It is advice: nothing is changed.
    `spot`: the last valid continuous-session price, received at `spot_as_of`; `auction`: the stock is in its closing
    auction now (the text says "auction in progress, last price as of HH:MM"; nothing else changes). Without a valid
    price the sold legs are assumed near the money (`leg_level`). The level comes only from the schedule, the
    moneyness, an invalid margin base and an overdue exit."""
    if closed or not pos.shorts:
        return None
    st = sos.settlement
    today = now.astimezone(IST).date()
    dc = day_count(pos.expiry, calendar)
    left = dc.left(today)
    cs = cutoffs(pos, sos, calendar)
    past = any(c["broker"] and now >= c["at"] for c in cs)
    legs, level = [], None
    for leg in pos.shorts:
        lv, why = leg_level(leg, spot, left, st, past)
        level = escalate(level, lv)
        legs.append({"leg": f"sold {leg.strike:g} {leg.right}", "strike": leg.strike, "right": leg.right,
                     "lots": leg.lots, "lot_size": leg.lot_size, "level": lv, "why": why,
                     **moneyness(leg, spot, st.near_money_pct)})
    level = escalate(previous_level, level)
    estimate = delivery_estimate(pos, spot, left, sos, capital, base_margin_inr, near_month)
    invalid = [] if usable_margin_base(base_margin_inr)[1] is None else [usable_margin_base(base_margin_inr)[1]]
    price = price_note(spot, spot_as_of, now, auction)
    if invalid:
        level = escalate(level, URGENT)          # an unusable input never makes the alert quieter
    late = overdue(pos, now, closed, sos, calendar)
    if late:
        level = escalate(level, URGENT)          # overdue: URGENT whatever the schedule gave, before the plan is saved
    dl = cs[0] if cs else None
    due_now = dl is not None and late is None and now >= dl["at"]     # the planned exit time has come: exit now
    if level is None and late is None and not due_now:
        return None
    by = f"{dl['at']:%d-%b-%Y %H:%M}" if dl else "now (the expiry is not known)"
    kind = "spread" if pos.longs else "position"
    hard = hard_limit(cs)
    action = f"Exit the {kind} by {by}"
    if due_now:
        action = (f"Exit the {kind} NOW: due by {by}" + (" while the stock trades continuously" if dl["rule"] ==
                                                          "forced exit" else "")
                  + f"; hard limit {hard['hard_limit']:%H:%M}")
    verified = dl is None or dc.covered
    if not verified:
        action += f" ({DEADLINE_UNVERIFIED})"      # the alert card, the exit-call API and the Telegram text carry it
    return {"underlying": pos.underlying, "label": pos.label, "level": level, "colour": "red" if late or level == URGENT else None,
            "expiry": pos.expiry.isoformat() if pos.expiry else None, "trading_days_left": left,
            # how the days were counted; not covered = one session earlier, shown on the card in red
            "calendar": {"covered": dc.covered, "note": dc.note, "colour": None if dc.covered else "red"},
            "legs": legs, "estimate": estimate, "invalid_inputs": invalid, "price": price,
            "deadline": dl["at"].isoformat() if dl else None, "deadline_set_by": dl["what"] if dl else None,
            "deadline_verified": verified, "deadline_note": None if verified else DEADLINE_UNVERIFIED,
            "hard_limit": hard["hard_limit"].isoformat() if hard else None, "due_now": due_now,
            "cutoffs": [{**c, "at": c["at"].isoformat(), "hard_limit": c["hard_limit"].isoformat()} for c in cs],
            "action": action, "overdue": late,
            "not_confirmed": not_confirmed(pos, sos, cs),
            "note": "alert and estimate only: the system never closes or changes a position"}


def rules_view(sos, calendar=None, today: date | None = None) -> dict:
    """ICICI's rules as stored, for the stock option page: each with its confirmed flag and, when it is
    not confirmed, the label shown on every card and alert that uses it. With `today`: whether the
    holiday calendar covers the expiries of the next 70 days (the entry window and the month after)."""
    cal = None
    if today is not None:
        missing = uncovered_years(calendar, today - timedelta(days=31), today + timedelta(days=70))
        cal = {"covered": not missing, "colour": None if not missing else "red",
               "note": CALENDAR_COVERED if not missing else
               f"{HOLIDAY_COVERAGE_MISSING}: the holiday list is not verified complete for "
               f"{', '.join(map(str, missing))}: for expiries there, trading days are counted on weekdays less one "
               f"(alerts one session earlier). {COVERAGE_HOW}",
               "deadline_verified": not missing, "deadline_note": None if not missing else DEADLINE_UNVERIFIED}
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
            "calendar": cal,
            "positions_note": "No open positions are recorded yet (phase 6 adds the journal): the rules and estimates are "
                              "built and tested, alerts are not sent."}
