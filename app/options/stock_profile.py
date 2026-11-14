"""Phase 9: what a stock has done before, computed from stored daily bars. Pure functions, no I/O.

- trend_block: the falling-stock block for put selling (DOWNTREND_BLOCK) and its mirror for call
  selling in a strong uptrend (UPTREND_BLOCK).
- stock_loss_limit: the per-stock campaign limit (STOCK_LOSS_LIMIT).
- range_speed_profile: typical monthly high-low range, days from swing low to swing high, ranging
  or trending now, how often a strike at a given distance was passed within the holding period, and
  the move implied by current option prices beside the moves that happened.
- scenario_history: 30-day moves of the stock after past FII selling streaks, India VIX spikes, RBI
  rate decisions and its own results. HISTORY, NOT A FORECAST. A scenario whose inputs are not in
  the data files says "no data"; nothing is ever filled in.
- alert_kinds / position_alerts: what changed for an open spread since the previous session.

Every function sees only the bars it is given: the caller passes bars up to the moment in question
(and none before 2019-01-22, where Breeze history is unreliable), so nothing uses later data.
"""
from __future__ import annotations

import bisect
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..history import bar_date
from ..indicators import last, percentile, sma
from ..strategies.base import fmt, ok

HISTORY_LABEL = "history, not a forecast"
DOWNTREND_BLOCK, UPTREND_BLOCK, STOCK_LOSS_LIMIT = "DOWNTREND_BLOCK", "UPTREND_BLOCK", "STOCK_LOSS_LIMIT"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


# ---------------- 1. falling-stock block ----------------
def trend_block(daily: Sequence, index_daily: Sequence, right: str, cfg) -> tuple[str | None, list[str]]:
    """(reason code or None, the conditions that apply). Puts (right PE) are blocked when the stock
    closed below its long average, made its 52-week low in the last `extreme_recent_sessions`, or its
    return over `relative_return_sessions` trails the index by more than `max_underperformance_pct`
    points. Calls (CE): the mirror image. Without enough history for a condition the answer is
    INSUFFICIENT_HISTORY: unknown never passes."""
    need = max(cfg.long_sma_days, cfg.extreme_lookback_sessions, cfg.relative_return_sessions + 1)
    if len(daily) < need:
        return INSUFFICIENT_HISTORY, [f"{len(daily)} daily bars, the trend block needs {need}"]
    closes = [b.close for b in daily]
    avg = last(sma(closes, cfg.long_sma_days))
    if not ok(avg):
        return INSUFFICIENT_HISTORY, [f"{cfg.long_sma_days}-day average unavailable"]
    idx = {bar_date(b): b.close for b in index_daily}
    d0, d1 = bar_date(daily[-1 - cfg.relative_return_sessions]), bar_date(daily[-1])
    if d0 not in idx or d1 not in idx:
        return INSUFFICIENT_HISTORY, [f"no index close for {d0} or {d1}: relative return unknown"]
    stock_ret = (closes[-1] / closes[-1 - cfg.relative_return_sessions] - 1) * 100
    index_ret = (idx[d1] / idx[d0] - 1) * 100
    rel = stock_ret - index_ret
    window, recent = daily[-cfg.extreme_lookback_sessions:], daily[-cfg.extreme_recent_sessions:]
    why = []
    if right == "PE":
        if closes[-1] < avg:
            why.append(f"close {fmt(closes[-1])} is below the {cfg.long_sma_days}-day average {fmt(avg)}")
        low = min(b.low for b in window)
        if min(b.low for b in recent) <= low:
            why.append(f"52-week low {fmt(low)} was made in the last {cfg.extreme_recent_sessions} sessions")
        if rel < -cfg.max_underperformance_pct:
            why.append(f"{cfg.relative_return_sessions}-session return {stock_ret:.1f}% trails the index "
                       f"({index_ret:.1f}%) by {-rel:.1f} points, more than {cfg.max_underperformance_pct:g}")
        return (DOWNTREND_BLOCK if why else None), why
    if closes[-1] > avg:
        why.append(f"close {fmt(closes[-1])} is above the {cfg.long_sma_days}-day average {fmt(avg)}")
    high = max(b.high for b in window)
    if max(b.high for b in recent) >= high:
        why.append(f"52-week high {fmt(high)} was made in the last {cfg.extreme_recent_sessions} sessions")
    if rel > cfg.max_underperformance_pct:
        why.append(f"{cfg.relative_return_sessions}-session return {stock_ret:.1f}% leads the index "
                   f"({index_ret:.1f}%) by {rel:.1f} points, more than {cfg.max_underperformance_pct:g}")
    return (UPTREND_BLOCK if why else None), why


# ---------------- 2. per-stock campaign limit ----------------
@dataclass(frozen=True)
class ClosedTrade:
    """A closed position in one stock: a signal that was taken or a position entered by hand."""
    exit_date: date
    stopped: bool                 # closed by the stop
    net_pnl: float                # rupees after costs
    origin: str = "SIGNAL"        # SIGNAL | MANUAL


def stock_loss_limit(trades: Sequence[ClosedTrade], today: date, cfg, capital_inr: float) -> tuple[date, str] | None:
    """(blocked until, why) if the stock is cooling off today, else None. A cooling-off period starts
    on the exit date of the trade that completes `max_stopped_trades` stops, or takes the net result
    to a loss of `max_cumulative_loss_pct` of capital, within the last `window_days`. Only trades
    closed on or before `today` count."""
    limit = capital_inr * cfg.max_cumulative_loss_pct / 100.0
    done = sorted((t for t in trades if t.exit_date <= today), key=lambda t: t.exit_date)
    best = None
    for t in done:
        recent = [x for x in done if t.exit_date - timedelta(days=cfg.window_days) < x.exit_date <= t.exit_date]
        stops = sum(1 for x in recent if x.stopped)
        net = sum(x.net_pnl for x in recent)
        why = None
        if stops >= cfg.max_stopped_trades and t.stopped:
            why = f"{stops} stopped trades in {cfg.window_days} days (the last on {t.exit_date:%d-%b-%Y})"
        elif net <= -limit and t.net_pnl < 0:
            why = (f"net loss Rs {fmt(-net)} in {cfg.window_days} days to {t.exit_date:%d-%b-%Y}, at or above "
                   f"{cfg.max_cumulative_loss_pct:g}% of capital (Rs {fmt(limit)})")
        if why:
            until = t.exit_date + timedelta(days=cfg.cooling_off_days)
            if today < until and (best is None or until > best[0]):
                best = (until, f"{why}; no new signal in this stock until {until:%d-%b-%Y}")
    return best


# ---------------- 3. range and speed profile ----------------
def swings(closes: Sequence[float], pct: float) -> list[tuple[str, int]]:
    """Confirmed swing points ('L' | 'H', index) of a zigzag: a low is confirmed once the close has
    risen `pct` % from it, a high once it has fallen `pct` % from it."""
    t = pct / 100.0
    out: list[tuple[str, int]] = []
    if not closes:
        return out
    lo = hi = 0
    direction = None
    for i, c in enumerate(closes):
        if direction is None:
            if c < closes[lo]:
                lo = i
            if c > closes[hi]:
                hi = i
            if c >= closes[lo] * (1 + t):
                out.append(("L", lo))
                direction, hi = "up", i
            elif c <= closes[hi] * (1 - t):
                out.append(("H", hi))
                direction, lo = "down", i
        elif direction == "up":
            if c > closes[hi]:
                hi = i
            elif c <= closes[hi] * (1 - t):
                out.append(("H", hi))
                direction, lo = "down", i
        else:
            if c < closes[lo]:
                lo = i
            elif c >= closes[lo] * (1 + t):
                out.append(("L", lo))
                direction, hi = "up", i
    return out


def state_now(closes: Sequence[float], lookback: int, trending_efficiency: float) -> tuple[str | None, float | None]:
    """(TRENDING_UP | TRENDING_DOWN | RANGING | None, efficiency). Efficiency = net move over the
    lookback divided by the distance travelled day by day (1 = a straight line, 0 = back where it began)."""
    if len(closes) < lookback + 1:
        return None, None
    seg = closes[-lookback - 1:]
    path = sum(abs(b - a) for a, b in zip(seg, seg[1:]))
    if path <= 0:
        return "RANGING", 0.0
    eff = abs(seg[-1] - seg[0]) / path
    if eff < trending_efficiency:
        return "RANGING", round(eff, 3)
    return ("TRENDING_UP" if seg[-1] > seg[0] else "TRENDING_DOWN"), round(eff, 3)


def breach_frequency(closes: Sequence[float], distance_pct: float, right: str, holding: int) -> dict:
    """How often a strike `distance_pct` % away from the close (below it for a put, above it for a
    call) was passed by a later close within `holding` sessions. Every start day whose whole holding
    period lies inside the data is one case."""
    n = hit = 0
    for i in range(len(closes) - holding):
        level = closes[i] * (1 - distance_pct / 100.0) if right == "PE" else closes[i] * (1 + distance_pct / 100.0)
        ahead = closes[i + 1:i + 1 + holding]
        n += 1
        if (min(ahead) < level) if right == "PE" else (max(ahead) > level):
            hit += 1
    return {"cases": n, "breached": hit, "breached_pct": round(hit / n * 100, 1) if n else None}


def _window(daily: Sequence, years: int, floor: date | None) -> list:
    if not daily:
        return []
    end = bar_date(daily[-1])
    start = end - timedelta(days=round(365.25 * years))
    if floor is not None:
        start = max(start, floor)
    return [b for b in daily if bar_date(b) >= start]


def range_speed_profile(daily: Sequence, cfg, floor: date | None = None, right: str | None = None,
                        distance_pct: float | None = None, atm_iv_pct: float | None = None) -> dict:
    """The stock's profile over each window in `cfg.windows_years`, from bars on or after `floor`.
    `distance_pct` and `right`: the proposed sold strike's distance from the price, for the breach
    history. `atm_iv_pct`: current at-the-money implied volatility, for the implied move.
    Anything that cannot be computed is None with the bar count shown, never a guess."""
    out = {"label": HISTORY_LABEL, "as_of": bar_date(daily[-1]).isoformat() if daily else None,
           "data_from": floor.isoformat() if floor else None, "windows": {}}
    closes_all = [b.close for b in daily if floor is None or bar_date(b) >= floor]
    state, eff = state_now(closes_all, cfg.state_lookback_sessions, cfg.trending_efficiency)
    out["state_now"] = state
    out["state_note"] = (f"net move / distance travelled over {cfg.state_lookback_sessions} sessions = {eff}; "
                         f"trending at or above {cfg.trending_efficiency:g}") if eff is not None else "not enough bars"
    h = cfg.holding_sessions
    implied = None if not ok(atm_iv_pct) else round(atm_iv_pct * (h / 252.0) ** 0.5, 2)
    out["implied_move_pct"] = implied
    out["implied_move_note"] = (f"one standard deviation over {h} sessions from the at-the-money implied "
                                f"volatility {atm_iv_pct:.1f}%" if implied is not None
                                else "no current at-the-money implied volatility (no fresh option chain)")
    for years in cfg.windows_years:
        bars = _window(daily, years, floor)
        closes = [b.close for b in bars]
        months: dict[tuple[int, int], list] = {}
        for b in bars:
            d = bar_date(b)
            months.setdefault((d.year, d.month), []).append(b)
        keys = sorted(months)[1:-1]                      # the first and the current month are partial
        ranges = [(max(b.high for b in months[k]) - min(b.low for b in months[k])) / months[k][0].open * 100
                  for k in keys if months[k][0].open > 0]
        piv = swings(closes, cfg.swing_pct)
        up = [(j - i, (closes[j] / closes[i] - 1) * 100) for (a, i), (b2, j) in zip(piv, piv[1:]) if a == "L" and b2 == "H"]
        down = [(j - i, (closes[j] / closes[i] - 1) * 100) for (a, i), (b2, j) in zip(piv, piv[1:]) if a == "H" and b2 == "L"]
        moves = [abs(closes[i + h] / closes[i] - 1) * 100 for i in range(len(closes) - h)]
        w = {"years": years, "bars": len(bars), "from": bar_date(bars[0]).isoformat() if bars else None,
             "months": len(ranges),
             "monthly_range_median_pct": round(statistics.median(ranges), 2) if ranges else None,
             "monthly_range_p80_pct": round(percentile(ranges, 80), 2) if ranges else None,
             "upswings": len(up),
             "avg_sessions_low_to_high": round(statistics.mean(x[0] for x in up), 1) if up else None,
             "avg_upswing_pct": round(statistics.mean(x[1] for x in up), 1) if up else None,
             "downswings": len(down),
             "avg_sessions_high_to_low": round(statistics.mean(x[0] for x in down), 1) if down else None,
             "avg_downswing_pct": round(statistics.mean(x[1] for x in down), 1) if down else None,
             "holding_sessions": h,
             "typical_move_pct": round(statistics.median(moves), 2) if moves else None,
             "move_p80_pct": round(percentile(moves, 80), 2) if moves else None,
             "breach": None}
        if distance_pct is not None and right in ("PE", "CE"):
            w["breach"] = {"distance_pct": round(distance_pct, 2), "side": right,
                           **breach_frequency(closes, distance_pct, right, h)}
        out["windows"][str(years)] = w
    return out


# ---------------- 4. scenario history ----------------
def fii_streak_starts(rows: Sequence[tuple[date, float]], streak: int, selling: bool = True,
                      sessions: Sequence[date] | None = None) -> list[date]:
    """Dates on which a run of `streak` straight sessions of FII net selling (or buying) was first
    completed. `rows`: (date, FII net in Rs crore), any order. With `sessions` (the market's session
    dates, ascending) a run only continues across consecutive sessions: a day missing from the file
    breaks it, so a gap in the file never makes a streak."""
    pos = {d: i for i, d in enumerate(sessions)} if sessions is not None else None
    out, run, prev = [], 0, None
    for d, net in sorted(rows):
        if pos is not None and (d not in pos or prev is None or prev not in pos or pos[d] != pos[prev] + 1):
            run = 0
        hit = net < 0 if selling else net > 0
        run = run + 1 if hit else 0
        if run == streak:
            out.append(d)
        prev = d
    return out


def vix_spike_dates(vix_daily: Sequence, pct: float, floor: date | None = None) -> list[date]:
    """Sessions on which India VIX closed at least `pct` % above the previous session's close."""
    out = []
    for a, b in zip(vix_daily, vix_daily[1:]):
        if a.close > 0 and (b.close / a.close - 1) * 100 >= pct and (floor is None or bar_date(b) >= floor):
            out.append(bar_date(b))
    return out


def scenario_history(daily: Sequence, triggers: dict[str, dict], cfg, floor: date | None = None) -> list[dict]:
    """For each scenario, the distribution of the stock's move over `scenario_horizon_days` calendar
    days after each past trigger date. `triggers`: name -> {"dates": [...] or None, "missing": what
    to fill in when dates is None}. The move runs from the close of the trigger day (the first
    session on or after it) to the close of the first session on or after trigger + horizon. Only
    cases whose whole horizon had passed by the last bar given are counted, so nothing looks ahead.
    A scenario without input data reports "no data"; fewer than `scenario_min_cases` cases are
    marked LOW SAMPLE."""
    dates = [bar_date(b) for b in daily]
    closes = [b.close for b in daily]
    out = []
    for name, t in triggers.items():
        row = {"scenario": name, "label": HISTORY_LABEL, "horizon_days": cfg.scenario_horizon_days, "cases": 0,
               "status": "no data", "note": ""}
        if t.get("dates") is None:
            row["note"] = t.get("missing") or "input data not available"
            out.append(row)
            continue
        moves, seen = [], set()
        for d in sorted(set(t["dates"])):
            if floor is not None and d < floor:
                continue
            i = bisect.bisect_left(dates, d)
            if i >= len(dates) or (dates[i] - d).days > 5 or i in seen:
                continue
            j = bisect.bisect_left(dates, dates[i] + timedelta(days=cfg.scenario_horizon_days))
            if j >= len(dates):
                continue                                   # the horizon has not passed yet: not a case
            seen.add(i)
            moves.append((closes[j] / closes[i] - 1) * 100)
        row["cases"] = len(moves)
        if not moves:
            row["note"] = "no past case inside the stored price history"
            out.append(row)
            continue
        row.update({
            "status": "LOW SAMPLE" if len(moves) < cfg.scenario_min_cases else "ok",
            "note": (f"LOW SAMPLE: {len(moves)} cases (fewer than {cfg.scenario_min_cases})"
                     if len(moves) < cfg.scenario_min_cases else f"{len(moves)} cases"),
            "median_pct": round(statistics.median(moves), 2), "mean_pct": round(statistics.mean(moves), 2),
            "p10_pct": round(percentile(moves, 10), 2), "p25_pct": round(percentile(moves, 25), 2),
            "p75_pct": round(percentile(moves, 75), 2), "p90_pct": round(percentile(moves, 90), 2),
            "worst_pct": round(min(moves), 2), "best_pct": round(max(moves), 2),
            "down_pct_of_cases": round(sum(1 for m in moves if m < 0) / len(moves) * 100, 1)})
        out.append(row)
    return out


# ---------------- 5. open-position alerts ----------------
@dataclass(frozen=True)
class Snapshot:
    """What is known about a stock and the market at one close. None = unknown."""
    day: date
    close: float | None = None
    support: float | None = None          # lowest low of the support/resistance lookback
    resistance: float | None = None
    low_52w: float | None = None          # of the sessions BEFORE this one
    high_52w: float | None = None
    sma_long: float | None = None
    fii_bias: str | None = None           # BEARISH = selling streak, BULLISH = buying streak, NEUTRAL, None
    vix_change_pct: float | None = None   # India VIX close against the previous close
    regime: str | None = None
    news_tags: tuple[str, ...] = ()       # from the phase 8 news tagging, e.g. EVENT_RISK


def snapshot(daily: Sequence, sr_lookback: int, block_cfg, fii_bias: str | None = None,
             vix_change_pct: float | None = None, regime: str | None = None, news_tags: Sequence[str] = ()) -> Snapshot | None:
    """The snapshot at the close of the last bar given."""
    if not daily:
        return None
    closes = [b.close for b in daily]
    recent = daily[-sr_lookback:] if len(daily) >= sr_lookback else None
    before = daily[-1 - block_cfg.extreme_lookback_sessions:-1] if len(daily) > block_cfg.extreme_lookback_sessions else None
    n = block_cfg.long_sma_days
    avg = sum(closes[-n:]) / n if len(closes) >= n else None
    return Snapshot(bar_date(daily[-1]), closes[-1],
                    min(b.low for b in recent) if recent else None, max(b.high for b in recent) if recent else None,
                    min(b.low for b in before) if before else None, max(b.high for b in before) if before else None,
                    avg if ok(avg) else None, fii_bias, vix_change_pct, regime, tuple(news_tags))


ALERT_KINDS = ("BELOW_SUPPORT", "NEW_52W_LOW", "BROKE_LONG_AVERAGE", "FII_STREAK", "VIX_SPIKE", "REGIME_TURN", "EVENT_RISK")


def alert_kinds(prev: Snapshot | None, now: Snapshot | None, right: str, vix_spike_pct: float) -> list[dict]:
    """What changed against an open spread between two closes. For a bull put spread (PE): the stock
    closed below the support it was above, below its 52-week low, or fell through its long average;
    an FII selling streak began; India VIX spiked; the regime turned VOLATILE or EVENT; news tagged
    the stock EVENT_RISK. For a bear call spread (CE) the price conditions and the FII streak are
    mirrored. Each is reported once, on the session it happens: a condition that was already true at
    the previous close is not a change. Unknown values raise no alert."""
    if prev is None or now is None or now.close is None or prev.close is None:
        return []
    out = []

    def add(kind: str, text: str) -> None:
        out.append({"kind": kind, "what_changed": text})

    put = right == "PE"
    level = prev.support if put else prev.resistance
    if level is not None and ((prev.close >= level and now.close < level) if put else (prev.close <= level and now.close > level)):
        add("BELOW_SUPPORT" if put else "ABOVE_RESISTANCE",
            f"closed at {fmt(now.close)}, {'below the support' if put else 'above the resistance'} {fmt(level)} "
            f"(previous close {fmt(prev.close)})")
    ext = now.low_52w if put else now.high_52w
    if ext is not None and ((now.close < ext) if put else (now.close > ext)):
        add("NEW_52W_LOW" if put else "NEW_52W_HIGH",
            f"closed at {fmt(now.close)}, {'below the 52-week low' if put else 'above the 52-week high'} {fmt(ext)}")
    if prev.sma_long is not None and now.sma_long is not None:
        crossed = (prev.close >= prev.sma_long and now.close < now.sma_long) if put \
            else (prev.close <= prev.sma_long and now.close > now.sma_long)
        if crossed:
            add("BROKE_LONG_AVERAGE", f"closed at {fmt(now.close)}, {'below' if put else 'above'} its long average "
                                      f"{fmt(now.sma_long)} (previous close {fmt(prev.close)} was on the other side)")
    against = "BEARISH" if put else "BULLISH"
    if now.fii_bias == against and prev.fii_bias is not None and prev.fii_bias != against:
        add("FII_STREAK", f"an FII {'selling' if put else 'buying'} streak began (bias was {prev.fii_bias}, now {against})")
    if now.vix_change_pct is not None and now.vix_change_pct >= vix_spike_pct:
        add("VIX_SPIKE", f"India VIX rose {now.vix_change_pct:.1f}% in the session (spike level {vix_spike_pct:g}%)")
    if now.regime in ("VOLATILE", "EVENT") and prev.regime is not None and prev.regime != now.regime:
        add("REGIME_TURN", f"market regime turned {now.regime} (was {prev.regime})")
    if "EVENT_RISK" in now.news_tags and "EVENT_RISK" not in prev.news_tags:
        add("EVENT_RISK", "news tagged the stock EVENT_RISK")
    return out
