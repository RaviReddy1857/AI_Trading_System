"""Phase 9: reversal detector for washed-out stocks. A WATCH list and candidate trades, never a signal:
nothing here alerts until the user approves it (stock_option_selling.reversal.alerts_approved and
strategies.status).

Three states, each computed for a day from that day's bars and earlier ones only (ReversalScanner):
1. WASHED_OUT: the close is `washed_out_drawdown_pct` or more below the 52-week high and has been below the
   200-day average for at least `min_sessions_below_long` sessions in a row.
2. BASE_FORMING: washed out, no new low for `base_no_new_low_sessions`, and the `range_sessions` ATR
   is below `range_shrink_ratio` x the `range_compare_sessions` ATR.
   The base low is the lowest low of the 52-week window.
3. REVERSAL_CONFIRMED (on one day): a base was forming within the last `base_valid_sessions`, the low
   has held, and the close is above the 20-day average and above the last swing high, on volume above
   its 20-day average, with the `relative_return_sessions` return ahead of NIFTY. For `lift_sessions`
   afterwards (while the base low holds) the falling-stock block is lifted, only for put spreads whose
   sold strike is below the base low.

On confirmation there are two candidate trades: a bull call spread (BullCallSpread) and a put credit
spread with the sold strike below the base low (ReversalPutSpread). Prior breakdown levels (swing
lows the fall broke, above the price) are shown as stall zones.

`reversal_outcomes` measures every past confirmation: the move 10, 20 and 40 sessions later and the
share that went on to a new low, against matching baselines measured the same way on the same stocks
and sessions (defined in `reversal_outcomes`, before any result was seen).
"""
from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..history import bar_date
from .stock_profile import pivots
from ..indicators import percentile
from ..strategies.base import fmt
from .base import OptionContext, OptionStrategy, leg_view, liquidity_problem, metrics, pick_expiry, round_trip_costs
from .greeks import days_to_expiry
from .payoff import InvalidStructure, OptionLeg, debit_spread
from .sizing import ZERO_SIZE, size_spread
from ..quality import IST
from .stock_selling import EVENT_COVERAGE_UNKNOWN, StockOptionSelling, event_coverage, events_before_expiry

REVERSAL_CONFIRMED = "REVERSAL_CONFIRMED"


@dataclass(frozen=True)
class ReversalDay:
    washed_out: bool = False
    base_forming: bool = False
    confirmed: bool = False               # confirmed ON this day
    drawdown_pct: float | None = None
    high_52w: float | None = None
    base_low: float | None = None         # the base's low (base forming), or the confirmed base's low
    last_confirmed: int | None = None     # index of the newest confirmation on or before this day
    lift_active: bool = False
    reasons: tuple[str, ...] = ()


class ReversalScanner:
    """The reversal state of one stock on every day of its history. The state of day i uses bars up
    to and including i only; a price break (unadjusted split or bonus) starts the history afresh."""

    def __init__(self, daily: Sequence, index_daily: Sequence, cfg, long_sma_days: int, breaks: Sequence[int] = (),
                 pivot_bars: int = 5):
        self.bars, self.cfg, self.pivot_bars = list(daily), cfg, pivot_bars
        self.dates = [bar_date(b) for b in self.bars]
        self._pos = {d: i for i, d in enumerate(self.dates)}
        self.breaks = tuple(sorted(breaks))       # bar indexes that opened across an unadjusted price break
        self.pivots: list[tuple[str, int, int, float]] = []   # confirmed pivots: (L | H, index, confirmed at, price)
        self.days: list[ReversalDay] = []
        self._scan({bar_date(b): b.close for b in index_daily}, long_sma_days, sorted(breaks))

    def _scan(self, idx: dict, n_long: int, breaks: list[int]) -> None:
        c = self.cfg
        bars = self.bars
        closes = [b.close for b in bars]
        need = max(c.high_lookback_sessions, n_long)
        seg = 0                     # first index of the current unbroken stretch
        below_run = 0               # consecutive sessions closing below the long average
        last_h = None               # price (high) of the newest confirmed pivot high
        base_days: list[tuple[int, float]] = []       # (index, base low) of recent base-forming days
        last_conf, conf_low = None, None
        # confirmed pivots of each unbroken stretch (a pivot never spans a price break)
        starts = [0] + list(breaks)
        ends = list(breaks) + [len(bars)]
        for a, z in zip(starts, ends):
            self.pivots += [(k, j + a, at + a, px) for k, j, at, px in pivots(bars[a:z], self.pivot_bars)]
        by_confirm: dict[int, list] = {}
        for pv in self.pivots:
            by_confirm.setdefault(pv[2], []).append(pv)
        nb = 0
        for i, b in enumerate(bars):
            while nb < len(breaks) and breaks[nb] <= i:
                seg, below_run, last_h = breaks[nb], 0, None
                base_days, last_conf, conf_low = [], None, None
                nb += 1
            for kind, j, at, px in by_confirm.get(i, ()):              # usable from its confirmation session on
                if kind == "H":
                    last_h = px
            x = closes[i]
            if i - seg + 1 < need:
                self.days.append(ReversalDay(last_confirmed=last_conf))
                continue
            window = bars[i - c.high_lookback_sessions + 1:i + 1]
            high = max(w.high for w in window)
            dd = (1 - x / high) * 100 if high > 0 else None
            avg_long = sum(closes[i - n_long + 1:i + 1]) / n_long
            below_run = below_run + 1 if x < avg_long else 0
            washed = dd is not None and dd >= c.washed_out_drawdown_pct and below_run >= c.min_sessions_below_long
            # ---- base forming ----
            base, base_low = False, None
            if washed:
                lows = [w.low for w in window]
                base_low = min(lows)
                age = len(lows) - 1 - max(k for k, v in enumerate(lows) if v == base_low)
                r, m = c.range_sessions, c.range_compare_sessions
                tr = [max(w.high - w.low, abs(w.high - v.close), abs(w.low - v.close))
                      for v, w in zip(bars[i - m:i], bars[i - m + 1:i + 1])]          # true ranges of the last m sessions
                atr_long, atr_short = statistics.mean(tr), statistics.mean(tr[-r:])
                base = age >= c.base_no_new_low_sessions and atr_long > 0 and atr_short < c.range_shrink_ratio * atr_long
            # ---- confirmation ----
            base_days = [(j, low) for j, low in base_days if i - j <= c.base_valid_sessions]
            confirmed, why = False, ()
            if base_days and (last_conf is None or i - last_conf >= c.rearm_sessions):
                j, low = base_days[-1]
                n, v = c.confirm_sma_days, c.volume_sma_days
                avg = sum(closes[i - n + 1:i + 1]) / n
                vols = [w.volume for w in bars[i - v:i]]
                k = c.relative_return_sessions
                d0, d1 = self.dates[i - k], self.dates[i]
                held = min(w.low for w in bars[j:i + 1]) >= low
                vol_ok = b.volume is not None and all(z is not None for z in vols) and b.volume > sum(vols) / v
                rel_ok = d0 in idx and d1 in idx and idx[d0] > 0 and (x / closes[i - k] - 1) > (idx[d1] / idx[d0] - 1)
                if held and x > avg and last_h is not None and x > last_h and vol_ok and rel_ok:
                    confirmed, last_conf, conf_low = True, i, low
                    why = (f"close {fmt(x)} above the {n}-day average {fmt(avg)}",
                           f"close above the last confirmed swing high {fmt(last_h)}",
                           f"volume {b.volume:,.0f} above its {v}-day average {sum(vols) / v:,.0f}",
                           f"{k}-session return {(x / closes[i - k] - 1) * 100:.1f}% ahead of the index "
                           f"({(idx[d1] / idx[d0] - 1) * 100:.1f}%)",
                           f"base low {fmt(low)} has held")
            if base:
                base_days.append((i, base_low))
            lift = (last_conf is not None and i - last_conf <= c.lift_sessions
                    and min(w.low for w in bars[last_conf:i + 1]) >= conf_low)
            self.days.append(ReversalDay(washed, base, confirmed, None if dd is None else round(dd, 1), high,
                                         conf_low if lift else base_low, last_conf, lift, why))

    def stall_zones(self, i: int, n: int = 3) -> list[float]:
        """Prior breakdown levels above the price on day i: confirmed pivot lows of the 52-week window
        (confirmed by day i) that the fall later broke, nearest first. A rebound often stalls there."""
        first = i - self.cfg.high_lookback_sessions
        x = self.bars[i].close
        levels = sorted({round(px, 2) for kind, p, at, px in self.pivots
                         if kind == "L" and at <= i and p >= first and px > x})
        return levels[:n]

    def info(self, i: int) -> dict:
        """What the strategies and the pages read for day i."""
        d = self.days[i]
        state = REVERSAL_CONFIRMED if d.lift_active else ("BASE_FORMING" if d.base_forming
                                                         else ("WASHED_OUT" if d.washed_out else None))
        conf = d.last_confirmed
        return {"state": state, "confirmed_today": d.confirmed, "lift_active": d.lift_active, "base_low": d.base_low,
                "drawdown_pct": d.drawdown_pct, "high_52w": d.high_52w,
                "confirmed_on": self.dates[conf].isoformat() if conf is not None and d.lift_active else None,
                "reasons": list(self.days[conf].reasons) if conf is not None and d.lift_active else [],
                "stall_zones": self.stall_zones(i) if state else []}

    def info_on(self, day: date) -> dict:
        i = self._pos.get(day)
        return self.info(i) if i is not None else {"state": None, "confirmed_today": False, "lift_active": False,
                                                   "base_low": None, "stall_zones": [], "reasons": []}


BASELINES = {
    "washed_out_not_confirmed": "matching baseline: every session of the same stocks in the same period on which the "
                                "stock was washed out (more than washed_out_drawdown_pct below its 52-week high, "
                                "min_sessions_below_long sessions below its long average) and no reversal was confirmed",
    "every_session": "reference: every session of the same stocks in the same period",
}


def _rows(moves: dict[int, list[float]], horizons: Sequence[int], min_cases: int) -> list[dict]:
    rows = []
    for h in horizons:
        m = moves[h]
        rows.append({"sessions": h, "cases": len(m), "low_sample": len(m) < min_cases,
                     "median_pct": round(statistics.median(m), 2) if m else None,
                     "mean_pct": round(statistics.mean(m), 2) if m else None,
                     "p25_pct": round(percentile(m, 25), 2) if m else None,
                     "p75_pct": round(percentile(m, 75), 2) if m else None,
                     "worst_pct": round(min(m), 2) if m else None, "best_pct": round(max(m), 2) if m else None,
                     "up_pct_of_cases": round(sum(1 for z in m if z > 0) / len(m) * 100, 1) if m else None})
    return rows


def _new_low(flags: list[bool], longest: int, min_cases: int) -> dict:
    return {"within_sessions": longest, "cases": len(flags),
            "share_pct": round(sum(flags) / len(flags) * 100, 1) if flags else None, "low_sample": len(flags) < min_cases}


# outcome windows (review fix 2026-11-16): a horizon is counted in market sessions, every one of them must have a
# price for the stock, and no unadjusted price break may lie inside; otherwise the window is excluded, by reason
PAST_PERIOD = "horizon past the tested period"
NOT_A_SESSION = "start day is not a market session"
MISSING_SESSION = "a market session in the window has no price"
PRICE_BREAK = "an unadjusted price break inside the window"
GROUPS = ("confirmations", "washed_out_not_confirmed", "every_session")


def market_sessions(sessions: Sequence[date], calendar=None) -> tuple[list[date], list[int]]:
    """The market sessions from the first to the last of `sessions` (the index's session dates): every
    trading day of the holiday calendar (weekdays less data/holidays.csv) in each year the calendar
    covers, plus every date in `sessions`. A year the calendar holds no holiday for has not been entered
    (NSE publishes a list every year): there the index's sessions alone are the market sessions, so a
    day missing from the index too goes unnoticed. Returns (sessions, the years not covered)."""
    if not sessions:
        return [], []
    first, last = min(sessions), max(sessions)
    have = {h.year for h in calendar.holidays} if calendar is not None else set()
    out = set(sessions)
    d = first
    while d <= last:
        if d.year in have and calendar.is_trading_day(d):
            out.add(d)
        d += timedelta(days=1)
    return sorted(out), [y for y in range(first.year, last.year + 1) if y not in have]


def _window(dates: Sequence[date], pos: dict, breaks: Sequence[int], i: int, h: int, mkt: list[date],
            mkt_pos: dict, last: date) -> tuple[int | None, str | None]:
    """(index of the bar h market sessions after bar i, None) or (None, why the window is excluded)."""
    k = mkt_pos.get(dates[i])
    if k is None:
        return None, NOT_A_SESSION
    if k + h >= len(mkt) or mkt[k + h] > last:
        return None, PAST_PERIOD
    if any(d not in pos for d in mkt[k + 1:k + h + 1]):
        return None, MISSING_SESSION
    j = pos[mkt[k + h]]
    if any(i < b <= j for b in breaks):
        return None, PRICE_BREAK
    return j, None


def reversal_outcomes(scanners: dict[str, ReversalScanner], sessions: Sequence[date], horizons: Sequence[int],
                      min_cases: int, calendar=None) -> dict:
    """Every confirmation inside `sessions` (the tested period): the close-to-close move after each
    horizon, and the share whose low later fell below the base low within the longest horizon.
    HISTORY, NOT A FORECAST.

    A horizon of h is h MARKET sessions (`market_sessions`: the holiday calendar's trading days, and
    the index's sessions where the calendar has no holidays for the year). A window counts only if
    the whole of it lies inside `sessions`, the stock has a price on every market session in it, and
    no unadjusted price break (split, bonus, demerger: `ReversalScanner.breaks`) lies inside it;
    otherwise it is excluded, by reason. The same rule applies to the confirmations and both baselines.
    `windows` gives, for every group and horizon (and the new-low window), the eligible starts, the
    completed cases and the exclusions by reason; they always add up.

    Baselines (`baselines`), the same stocks and the same tested sessions, measured exactly the same
    way (close of the day to the close h sessions later; a new low = a later low under the reference
    low within the longest horizon):
    - washed_out_not_confirmed (the MATCHING baseline): every session on which the stock was washed out
      and no reversal was confirmed that day; its reference low is the lowest low of the 52-week window
      (`high_lookback_sessions`) that day, as a base low is for a confirmation;
    - every_session (a reference for the drift of the period): every session, confirmation days
      included, the same 52-week low.
    `versus_matching_baseline`: the confirmations' median and up-share minus the matching baseline's,
    per horizon, and the new-low share minus its; `low_sample` when either side has fewer than
    `min_cases` cases. Baseline sessions overlap (consecutive days share most of their horizon), so
    their cases are not independent; the count is the number of sessions, not of separate episodes."""
    allowed = set(sessions)
    last = max(sessions) if sessions else None
    mkt, uncovered = market_sessions(sessions, calendar)
    mkt_pos = {d: k for k, d in enumerate(mkt)}
    longest = max(horizons)
    nl_key = f"new_low_within_{longest}"
    keys = [str(h) for h in horizons] + [nl_key]
    acct = {g: {k: {"eligible_starts": 0, "completed": 0, "excluded": {}} for k in keys} for g in GROUPS}
    moves = {g: {h: [] for h in horizons} for g in GROUPS}
    lows = {g: [] for g in GROUPS}
    events = []

    def count(groups, key, why):
        for g in groups:
            a = acct[g][key]
            a["eligible_starts"] += 1
            if why is None:
                a["completed"] += 1
            else:
                a["excluded"][why] = a["excluded"].get(why, 0) + 1

    for sym, sc in sorted(scanners.items()):
        lookback = sc.cfg.high_lookback_sessions
        pos = {d: i for i, d in enumerate(sc.dates)}
        breaks = tuple(getattr(sc, "breaks", ()))
        for i, day in enumerate(sc.days):
            if sc.dates[i] not in allowed:
                continue
            week52_low = min(b.low for b in sc.bars[max(0, i - lookback + 1):i + 1])
            groups = ["every_session"]
            refs = {"every_session": week52_low}
            if day.confirmed:
                events.append({"stock": sym, "date": sc.dates[i].isoformat(), "close": sc.bars[i].close,
                               "base_low": day.base_low})
                groups.append("confirmations")
                refs["confirmations"] = day.base_low
            elif day.washed_out:
                groups.append("washed_out_not_confirmed")
                refs["washed_out_not_confirmed"] = week52_low
            for h in horizons:
                j, why = _window(sc.dates, pos, breaks, i, h, mkt, mkt_pos, last)
                count(groups, str(h), why)
                if j is not None:
                    move = (sc.bars[j].close / sc.bars[i].close - 1) * 100
                    for g in groups:
                        moves[g][h].append(move)
            j, why = _window(sc.dates, pos, breaks, i, longest, mkt, mkt_pos, last)
            count(groups, nl_key, why)
            if j is not None:
                low = min(b.low for b in sc.bars[i + 1:j + 1])
                for g in groups:
                    lows[g].append(low < refs[g])
    for g in GROUPS:
        for k in keys:
            a = acct[g][k]
            done = len(lows[g]) if k == nl_key else len(moves[g][int(k)])
            assert a["completed"] == done and a["eligible_starts"] == done + sum(a["excluded"].values()), (g, k, a)
    rows = _rows(moves["confirmations"], horizons, min_cases)
    nl = _new_low(lows["confirmations"], longest, min_cases)
    baselines = {k: {"what": BASELINES[k], "outcomes": _rows(moves[k], horizons, min_cases),
                     "new_low_afterwards": _new_low(lows[k], longest, min_cases)} for k in BASELINES}
    match = baselines["washed_out_not_confirmed"]
    diff = lambda a, b: None if a is None or b is None else round(a - b, 2)
    versus = {"baseline": "washed_out_not_confirmed",
              "outcomes": [{"sessions": r["sessions"], "median_minus_baseline_pct": diff(r["median_pct"], b["median_pct"]),
                            "up_share_minus_baseline_pts": diff(r["up_pct_of_cases"], b["up_pct_of_cases"]),
                            "cases": r["cases"], "baseline_cases": b["cases"],
                            "low_sample": r["low_sample"] or b["low_sample"]}
                           for r, b in zip(rows, match["outcomes"])],
              "new_low_share_minus_baseline_pts": diff(nl["share_pct"], match["new_low_afterwards"]["share_pct"]),
              "new_low_low_sample": nl["low_sample"] or match["new_low_afterwards"]["low_sample"],
              "note": "a difference is evidence only when neither side is LOW SAMPLE; baseline sessions overlap"}
    sess_note = ("horizons in market sessions from the holiday calendar"
                 + (f"; holidays.csv has no holidays for {', '.join(map(str, uncovered))}, so there the index's "
                    f"sessions are the market sessions" if uncovered else ""))
    return {"label": "history, not a forecast", "confirmations": len(events), "stocks": len({e["stock"] for e in events}),
            "outcomes": rows, "new_low_afterwards": nl, "baselines": baselines, "versus_matching_baseline": versus,
            "windows": acct, "market_sessions": {"note": sess_note, "calendar_not_covering": uncovered},
            "min_cases": min_cases, "events": events}


# ---------------- candidate 2: put credit spread below the base low ----------------
class ReversalPutSpread(StockOptionSelling):
    """A bull put spread entered on the day a reversal is confirmed, with the sold strike below the
    base low. The stock's structure rules all apply (delta, hedge, max loss, liquidity, entry window,
    events, minimum credit, margin); the up-trend and IV-rank entry rules do not, and the falling-stock
    block is lifted because the strike sits below the base low. Watch only until approved."""
    id = "reversal_put_spread"
    below_base_low = True

    def entry_checks(self, ctx: OptionContext) -> bool:
        p = self.params
        if ctx.kind != "stock":
            self.no("NOT_A_STOCK", "reversal candidates are for stocks")
            return False
        if ctx.no_filter:
            return True
        if not (p.enabled and p.reversal.enabled):
            self.no("DISABLED", "stock_option_selling.reversal.enabled is false")
            return False
        if not (ctx.market.get("reversal") or {}).get("confirmed_today"):
            self.no("NO_REVERSAL", "no reversal was confirmed for this stock today")
            return False
        return True

    def side(self, ctx: OptionContext) -> str | None:
        if ctx.no_filter:
            return ctx.baseline_right if ctx.baseline_right in ("PE", "CE") else None
        return "PE"


# ---------------- candidate 1: bull call spread ----------------
class BullCallSpread(OptionStrategy):
    """Buy the call nearest the money and sell the call `width_strikes` strikes higher, same monthly
    expiry, on the day a reversal is confirmed. The loss is limited to the debit paid plus costs.
    Signal prices are per share of the spread's VALUE (bought call minus sold call): entry = debit,
    target above it, stop below it. Watch only until approved."""
    id = "reversal_bull_call_spread"
    base_version = "1"
    horizon = "MONTHLY"
    allowed_regimes = frozenset()
    structure = "DEBIT_SPREAD"
    expiry_kinds = ("monthly",)
    needs_daily = True
    report_open_at_cutoff = True

    def __init__(self, params, sos):
        super().__init__(params)
        self.sos = sos                    # stock_option_selling: liquidity and max-loss limits

    def entry_checks(self, ctx: OptionContext) -> bool:
        if ctx.kind != "stock":
            self.no("NOT_A_STOCK", "reversal candidates are for stocks")
            return False
        if ctx.no_filter:
            return True
        if not self.sos.reversal.enabled:
            self.no("DISABLED", "stock_option_selling.reversal.enabled is false")
            return False
        if not (ctx.market.get("reversal") or {}).get("confirmed_today"):
            self.no("NO_REVERSAL", "no reversal was confirmed for this stock today")
            return False
        return True

    def pick(self, ctx: OptionContext):
        p = self.params
        exp = pick_expiry(ctx, p.entry_min_dte, p.entry_max_dte, self.expiry_kinds)
        if exp is None:
            self.no("NO_EXPIRY", f"no monthly expiry {p.entry_min_dte}-{p.entry_max_dte} days away")
            return None
        if not exp.lot_size:
            self.no("LOT_SIZE_UNKNOWN", f"{ctx.underlying} {exp.expiry}: lot size not in the instrument list")
            return None
        return exp

    def _evaluate(self, ctx: OptionContext):
        from dataclasses import replace
        p, sos = self.params, self.sos
        if not self.entry_checks(ctx):
            return []
        exp = self.pick(ctx)
        if exp is None:
            return []
        if not ctx.no_filter:
            today = ctx.asof.astimezone(IST).date()
            ev = events_before_expiry(ctx.events, ctx.underlying, today, exp.expiry)
            if ev:
                return self.no("EVENT_BEFORE_EXPIRY", ", ".join(f"{e.type} on {e.date:%d-%b-%Y}" for e in ev[:3]))
            if not ctx.historical:
                covered, text = event_coverage(ctx.events, ctx.underlying, today, exp.expiry, sos.event_coverage_max_age_days)
                if not covered:
                    return self.no(EVENT_COVERAGE_UNKNOWN, text)
        lctx = replace(ctx, max_spread_pct=sos.max_bid_ask_pct_of_mid, min_oi_lots=sos.min_open_interest_lots)
        bought = None
        for k in ctx.quotes.atm_candidates(exp.expiry):
            m = metrics(ctx, exp.expiry, k, "CE")
            if m is not None and m[0].buy_price(ctx.historical) is not None and not liquidity_problem(lctx, m[0], exp.lot_size):
                bought = (k, *m)
                break
        if bought is None:
            return self.no("NO_STRIKE", "no liquid call with a usable price near the money")
        kl, ql, iv_l, d_l = bought
        n, sold = 0, None
        for k in sorted(x for x in ctx.quotes.strikes(exp.expiry) if x > kl)[:p.width_strikes + 4]:
            q = ctx.quotes.quote(exp.expiry, k, "CE")
            if q is None or q.sell_price(ctx.historical) is None:
                continue
            n += 1
            if n == p.width_strikes:
                sold = (k, q)
                break
        if sold is None:
            return self.no("NO_STRIKE", f"no priced call {p.width_strikes} strikes above {kl:g}")
        ks, qs = sold
        lp = liquidity_problem(lctx, qs, exp.lot_size)
        if lp:
            return self.no("ILLIQUID", lp)
        buy, sell = round(ql.buy_price(ctx.historical), 2), round(qs.sell_price(ctx.historical), 2)
        debit, width = buy - sell, ks - kl
        if debit <= 0 or debit >= width:
            return self.no("NO_DEBIT", f"{kl:g}/{ks:g} CE: net debit {debit:.2f} on width {width:g}")
        one = [OptionLeg("BUY", "CE", kl, exp.expiry, 1, buy, exp.lot_size),
               OptionLeg("SELL", "CE", ks, exp.expiry, 1, sell, exp.lot_size)]
        budget = ctx.capital_inr * sos.max_risk_per_position_pct / 100.0
        try:
            lots, legs, pay = size_spread("debit", one, exp.lot_size, ctx.costs, budget, ctx.max_lots)
        except InvalidStructure as e:
            return self.no("NO_DEBIT", str(e))
        if pay.max_profit_inr <= 0:
            return self.no("NO_PROFIT_AFTER_COSTS", f"{kl:g}/{ks:g} CE: the costs take the whole possible profit")
        if lots == 0:
            return self.no(ZERO_SIZE, f"one lot's max loss Rs {fmt(float(pay.max_loss_inr))} is above the position risk "
                                      f"budget of {sos.max_risk_per_position_pct:g}% of capital (Rs {fmt(budget)}): 0 whole lots")
        target = debit + (width - debit) * p.target_pct_of_max_profit / 100.0
        stop = debit * (1 - p.stop_pct_of_debit / 100.0)
        if round(stop, 2) <= 0 or round(stop, 2) >= round(debit, 2) or round(target, 2) <= round(debit, 2):
            return self.no("NO_DEBIT", f"debit {debit:.2f} leaves no room for a stop and a target")
        rev = ctx.market.get("reversal") or {}
        ms = metrics(ctx, exp.expiry, ks, "CE")
        dte = days_to_expiry(ctx.asof, exp.expiry)
        details = {**pay.as_dict(ctx.capital_inr), "expiry": exp.expiry.isoformat(), "expiry_kind": exp.kind,
                   "days_to_expiry": dte, "lot_size": exp.lot_size, "lots": lots, "premium": round(debit, 2),
                   "net_debit": round(debit, 2), "total_cost_inr": float(-pay.net_premium_inr), "margin_inr": None,
                   "margin_note": "a debit spread is paid for in full: the debit is the most it can lose",
                   "iv_rank": ctx.iv_rank, "price_basis": "last traded (history)" if ctx.historical else "buy at ask, sell at bid",
                   "legs": [leg_view(legs[0], iv_l, d_l, ql), leg_view(legs[1], ms[1] if ms else None, ms[2] if ms else None, qs)],
                   "stall_zones": rev.get("stall_zones", []), "base_low": rev.get("base_low"),
                   "warnings": ["Stock options settle physically: close before expiry week.",
                                "Watch only: reversal candidates do not alert until you approve them."],
                   "sizing_note": "one lot by the normal rules: earlier losses in this stock never raise the size",
                   "exit_rules": {"sell_at_or_above": round(target, 2), "stop_sell_at_or_below": round(stop, 2),
                                  "exit_days_before_expiry": p.exit_days_before_expiry}}
        why = [("no-filter baseline: bull call spread on a fixed schedule" if ctx.no_filter
                else f"{REVERSAL_CONFIRMED}: " + "; ".join(rev.get("reasons", [])[:3])),
               f"bull call spread on {ctx.underlying} {exp.expiry:%d-%b-%Y} ({dte} days): buy {kl:g} CE at {fmt(buy)}, "
               f"sell {ks:g} CE at {fmt(sell)}",
               f"net debit {fmt(debit)} x {lots} lot(s) of {exp.lot_size}; max loss Rs {fmt(float(pay.max_loss_inr))}, max profit "
               f"Rs {fmt(float(pay.max_profit_inr))}; breakeven {', '.join(fmt(float(b)) for b in pay.breakevens)}",
               f"exit: sell at {fmt(target)} or higher, stop at {fmt(stop)}, out {p.exit_days_before_expiry} days before expiry"]
        if rev.get("stall_zones"):
            why.append("stall zones (prior breakdown levels): " + ", ".join(fmt(z) for z in rev["stall_zones"]))
        return [self.option_signal(ctx, "LONG", "MONTHLY", debit, stop, target, why, legs, details)]


def build_reversal_strategies(settings) -> tuple[ReversalPutSpread, BullCallSpread]:
    put = ReversalPutSpread(settings.stock_option_selling)
    call = BullCallSpread(settings.stock_option_selling.reversal.bull_call, settings.stock_option_selling)
    for st in (put, call):
        status = settings.strategies.status_of(st.id)
        st.alerts_enabled, st.mode, st.status_label = status.alerts, status.mode, status.label
    return put, call
