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
def trend_block(daily: Sequence, index_daily: Sequence, right: str, cfg, sma_long: int = 200) -> tuple[str | None, list[str]]:
    """(reason code or None, the conditions that apply). Puts (right PE) are blocked when the stock
    closed below its long average, made its 52-week low in the last `new_low_lookback`, or its
    return over `rel_strength_lookback` trails the index by more than `rel_strength_max_lag_pct`
    points. Calls (CE): the mirror image. Without enough history for a condition the answer is
    INSUFFICIENT_HISTORY: unknown never passes."""
    need = max(sma_long, cfg.low_52w_sessions, cfg.rel_strength_lookback + 1)
    if len(daily) < need:
        return INSUFFICIENT_HISTORY, [f"{len(daily)} daily bars, the trend block needs {need}"]
    closes = [b.close for b in daily]
    avg = last(sma(closes, sma_long))
    if not ok(avg):
        return INSUFFICIENT_HISTORY, [f"{sma_long}-day average unavailable"]
    idx = {bar_date(b): b.close for b in index_daily}
    d0, d1 = bar_date(daily[-1 - cfg.rel_strength_lookback]), bar_date(daily[-1])
    if d0 not in idx or d1 not in idx:
        return INSUFFICIENT_HISTORY, [f"no index close for {d0} or {d1}: relative return unknown"]
    stock_ret = (closes[-1] / closes[-1 - cfg.rel_strength_lookback] - 1) * 100
    index_ret = (idx[d1] / idx[d0] - 1) * 100
    rel = stock_ret - index_ret
    window, recent = daily[-cfg.low_52w_sessions:], daily[-cfg.new_low_lookback:]
    why = []
    if right == "PE":
        if closes[-1] < avg:
            why.append(f"close {fmt(closes[-1])} is below the {sma_long}-day average {fmt(avg)}")
        low = min(b.close for b in window)                     # a 52-week low is the lowest CLOSE of the window
        if min(b.close for b in recent) <= low:
            why.append(f"52-week low {fmt(low)} was made in the last {cfg.new_low_lookback} sessions")
        if rel < -cfg.rel_strength_max_lag_pct:
            why.append(f"{cfg.rel_strength_lookback}-session return {stock_ret:.1f}% trails the index "
                       f"({index_ret:.1f}%) by {-rel:.1f} points, more than {cfg.rel_strength_max_lag_pct:g}")
        return (DOWNTREND_BLOCK if why else None), why
    if closes[-1] > avg:
        why.append(f"close {fmt(closes[-1])} is above the {sma_long}-day average {fmt(avg)}")
    high = max(b.close for b in window)
    if max(b.close for b in recent) >= high:
        why.append(f"52-week high {fmt(high)} was made in the last {cfg.new_low_lookback} sessions")
    if rel > cfg.rel_strength_max_lag_pct:
        why.append(f"{cfg.rel_strength_lookback}-session return {stock_ret:.1f}% leads the index "
                   f"({index_ret:.1f}%) by {rel:.1f} points, more than {cfg.rel_strength_max_lag_pct:g}")
    return (UPTREND_BLOCK if why else None), why


# ---------------- market block ----------------
MARKET_WEAK, MARKET_UNKNOWN = "MARKET_WEAK", "MARKET_UNKNOWN"


def breadth_pct(stocks: Sequence[Sequence[float]], sma_days: int, min_coverage_pct: float, total: int) -> float | None:
    """Share (%) of stocks whose last close is above the average of their last `sma_days` closes.
    `stocks`: each stock's closes up to the decision day (those without a bar that day are left out by
    the caller). None when fewer than `min_coverage_pct` of the `total` stocks have enough closes."""
    usable = [c for c in stocks if len(c) >= sma_days]
    if total <= 0 or len(usable) / total * 100 < min_coverage_pct:
        return None
    above = sum(1 for c in usable if c[-1] > sum(c[-sma_days:]) / sma_days)
    return round(above / len(usable) * 100, 1)


def market_weak(index_daily: Sequence, breadth: float | None, cfg, long_sma_days: int,
                breadth_sma_days: int = 50) -> tuple[str | None, list[str]]:
    """(MARKET_WEAK | MARKET_UNKNOWN | None, why). Weak = the index closed below its long average, or
    breadth is below `market_breadth_min_pct`. Without enough index history or a breadth figure the state is
    unknown, which blocks like weak: unknown never passes."""
    if len(index_daily) < long_sma_days:
        return MARKET_UNKNOWN, [f"{len(index_daily)} index bars, the market block needs {long_sma_days}"]
    if breadth is None:
        return MARKET_UNKNOWN, ["market breadth unknown (too few stocks with usable price history that day)"]
    closes = [b.close for b in index_daily]
    avg = sum(closes[-long_sma_days:]) / long_sma_days
    why = []
    if closes[-1] < avg:
        why.append(f"the index closed at {fmt(closes[-1])}, below its {long_sma_days}-day average {fmt(avg)}")
    if breadth < cfg.market_breadth_min_pct:
        why.append(f"only {breadth:g}% of stocks are above their {breadth_sma_days}-session average "
                   f"(minimum {cfg.market_breadth_min_pct:g}%)")
    return (MARKET_WEAK if why else None), why


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
    on the exit date of the trade that completes `stock_loss_max_stops` stops, or takes the net result
    to a loss of `stock_loss_max_pct_capital` of capital, within the last `stock_loss_window_days`. Only trades
    closed on or before `today` count."""
    limit = capital_inr * cfg.stock_loss_max_pct_capital / 100.0
    done = sorted((t for t in trades if t.exit_date <= today), key=lambda t: t.exit_date)
    best = None
    for t in done:
        recent = [x for x in done if t.exit_date - timedelta(days=cfg.stock_loss_window_days) < x.exit_date <= t.exit_date]
        stops = sum(1 for x in recent if x.stopped)
        net = sum(x.net_pnl for x in recent)
        why = None
        if stops >= cfg.stock_loss_max_stops and t.stopped:
            why = f"{stops} stopped trades in {cfg.stock_loss_window_days} days (the last on {t.exit_date:%d-%b-%Y})"
        elif net <= -limit and t.net_pnl < 0:
            why = (f"net loss Rs {fmt(-net)} in {cfg.stock_loss_window_days} days to {t.exit_date:%d-%b-%Y}, at or above "
                   f"{cfg.stock_loss_max_pct_capital:g}% of capital (Rs {fmt(limit)})")
        if why:
            until = t.exit_date + timedelta(days=cfg.stock_cooloff_days)
            if today < until and (best is None or until > best[0]):
                best = (until, f"{why}; no new signal in this stock until {until:%d-%b-%Y}")
    return best


# ---------------- 3. range and speed profile ----------------
def pivots(bars: Sequence, n: int) -> list[tuple[str, int, int, float]]:
    """Confirmed swing pivots of daily bars: (L | H, index, index at which it is confirmed, price).
    A pivot low at j: its low is below the lows of the n sessions before it and at or below the lows of
    the n sessions after it (so of equal lows only the first counts); a pivot high is the mirror image
    on highs. It is confirmed at j + n: it may be used only from that session on, never on day j."""
    out = []
    lows, highs = [b.low for b in bars], [b.high for b in bars]
    for j in range(n, len(bars) - n):
        if lows[j] < min(lows[j - n:j]) and lows[j] <= min(lows[j + 1:j + n + 1]):
            out.append(("L", j, j + n, lows[j]))
        if highs[j] > max(highs[j - n:j]) and highs[j] >= max(highs[j + 1:j + n + 1]):
            out.append(("H", j, j + n, highs[j]))
    return out


def alternating(piv: Sequence[tuple[str, int, int, float]]) -> list[tuple[str, int, int, float]]:
    """Pivots in time order with runs of the same kind reduced to their extreme (lowest low, highest
    high), so lows and highs alternate: the swings of the stock."""
    out: list = []
    for x in sorted(piv, key=lambda p: (p[1], p[0])):
        if out and out[-1][0] == x[0]:
            if (x[0] == "L" and x[3] < out[-1][3]) or (x[0] == "H" and x[3] > out[-1][3]):
                out[-1] = x
            continue
        out.append(x)
    return out


def support_resistance(daily: Sequence, lookback: int, n: int) -> tuple[float | None, float | None]:
    """(support, resistance) at the close of the last bar given: the nearest confirmed pivot low below
    the close and the nearest confirmed pivot high above it, among the pivots of the last `lookback`
    sessions. None when there is none (or too little history): unknown, never a guess."""
    if len(daily) < lookback:
        return None, None
    seg = list(daily[-(lookback + n):])
    first = len(seg) - lookback
    close = seg[-1].close
    piv = [p for p in pivots(seg, n) if p[1] >= first]           # confirmed by the last bar by construction
    lows = [p[3] for p in piv if p[0] == "L" and p[3] < close]
    highs = [p[3] for p in piv if p[0] == "H" and p[3] > close]
    return (max(lows) if lows else None), (min(highs) if highs else None)


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
                        distance_pct: float | None = None, atm_iv_pct: float | None = None, pivot_bars: int = 5,
                        dte_days: float | None = None) -> dict:
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
    spot = closes_all[-1] if closes_all else None
    em = expected_move(spot, atm_iv_pct, dte_days)
    out["expected_move_inr"] = None if em is None else round(em, 2)
    out["implied_move_pct"] = None if em is None else round(em / spot * 100, 2)
    out["expected_move_dte"] = dte_days
    out["implied_move_note"] = (f"expected move to expiry ({dte_days:g} days): spot x IV {atm_iv_pct:.1f}% x sqrt(DTE/365); "
                                f"a one-standard-deviation model estimate, not a guaranteed range" if em is not None
                                else "no expected move: the at-the-money implied volatility or the days to expiry are "
                                     "not known (no fresh option chain)")
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
        piv = alternating(pivots(bars, pivot_bars))              # confirmed 5-bar pivots, lows and highs in turn
        pairs = list(zip(piv, piv[1:]))
        up = [(y[1] - x[1], (y[3] / x[3] - 1) * 100) for x, y in pairs if x[0] == "L" and y[0] == "H"]
        down = [(y[1] - x[1], (y[3] / x[3] - 1) * 100) for x, y in pairs if x[0] == "H" and y[0] == "L"]
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


def expected_move(spot: float | None, iv_pct: float | None, dte_days: float | None) -> float | None:
    """expected_move (section 2): spot x IV x sqrt(DTE / 365), IV as an annualized decimal. A
    one-standard-deviation model estimate, not a guaranteed range. None when an input is unknown."""
    if not (ok(spot) and ok(iv_pct) and dte_days is not None and dte_days > 0):
        return None
    return spot * iv_pct / 100.0 * (dte_days / 365.0) ** 0.5


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


def vix_spike_at(closes: Sequence[float], i: int, cfg) -> bool | None:
    """vix_spike on session i (section 2): India VIX up at least `vix_spike_rise_pct` over the last
    `vix_spike_sessions` sessions, or closing above its `vix_spike_percentile` of the previous
    `vix_percentile_lookback` closes. True if either holds; False when both are known not to hold;
    None when the answer is unknown (too little history for a part that does not already say yes)."""
    n = cfg.vix_spike_sessions
    rise = None
    if i >= n and closes[i - n] > 0:
        rise = (closes[i] / closes[i - n] - 1) * 100 >= cfg.vix_spike_rise_pct
    high = None
    if i >= cfg.vix_percentile_lookback:
        level = percentile(list(closes[i - cfg.vix_percentile_lookback:i]), cfg.vix_spike_percentile)
        high = level is not None and closes[i] > level
    if rise or high:
        return True
    if rise is None or high is None:
        return None
    return False


def vix_spike_series(vix_daily: Sequence, cfg) -> dict[date, bool | None]:
    closes = [b.close for b in vix_daily]
    return {bar_date(b): vix_spike_at(closes, i, cfg) for i, b in enumerate(vix_daily)}


def vix_spike_dates(vix_daily: Sequence, cfg, floor: date | None = None) -> list[date]:
    """First sessions of India VIX spike episodes (a spike day after a known non-spike day)."""
    flags = vix_spike_series(vix_daily, cfg)
    out, prev = [], None
    for d in sorted(flags):
        if flags[d] is True and prev is False and (floor is None or d >= floor):
            out.append(d)
        prev = flags[d]
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
    support: float | None = None          # nearest confirmed pivot low below the close (support_resistance)
    resistance: float | None = None
    low_52w: float | None = None          # of the sessions BEFORE this one
    high_52w: float | None = None
    sma_long: float | None = None
    fii_bias: str | None = None           # BEARISH = selling streak, BULLISH = buying streak, NEUTRAL, None
    vix_spike: bool | None = None         # vix_spike at this close (section 2); None = unknown
    regime: str | None = None
    news_tags: tuple[str, ...] = ()       # from the phase 8 news tagging, e.g. EVENT_RISK
    reversal: bool = False                # a reversal was confirmed for this stock on this session (reversal.py)
    market_weak: bool | None = None       # MARKET_WEAK applies at this close (None = unknown)


def snapshot(daily: Sequence, sr_lookback: int, block_cfg, fii_bias: str | None = None,
             vix_spike: bool | None = None, regime: str | None = None, news_tags: Sequence[str] = (),
             reversal: bool = False, market_weak: bool | None = None, pivot_bars: int = 5,
             sma_long: int = 200) -> Snapshot | None:
    """The snapshot at the close of the last bar given."""
    if not daily:
        return None
    closes = [b.close for b in daily]
    support, resistance = support_resistance(daily, sr_lookback, pivot_bars)
    before = daily[-1 - block_cfg.low_52w_sessions:-1] if len(daily) > block_cfg.low_52w_sessions else None
    n = sma_long
    avg = sum(closes[-n:]) / n if len(closes) >= n else None
    return Snapshot(bar_date(daily[-1]), closes[-1], support, resistance,
                    min(b.low for b in before) if before else None, max(b.high for b in before) if before else None,
                    avg if ok(avg) else None, fii_bias, vix_spike, regime, tuple(news_tags), reversal, market_weak)


ALERT_KINDS = ("BELOW_SUPPORT", "NEW_52W_LOW", "BROKE_LONG_AVERAGE", "FII_STREAK", "VIX_SPIKE", "REGIME_TURN", "MARKET_WEAK",
               "EVENT_RISK")


def alert_kinds(prev: Snapshot | None, now: Snapshot | None, right: str, reversal_alerts: bool = False) -> list[dict]:
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
    if now.vix_spike is True and prev.vix_spike is False:
        add("VIX_SPIKE", "India VIX spiked: up 20% or more over 5 sessions, or above its 80th percentile of the past year "
                         "(section 2 vix_spike)")
    if now.regime in ("VOLATILE", "EVENT") and prev.regime is not None and prev.regime != now.regime:
        add("REGIME_TURN", f"market regime turned {now.regime} (was {prev.regime})")
    if put and now.market_weak is True and prev.market_weak is False:
        add("MARKET_WEAK", "the market turned weak: the index below its long average or breadth under the minimum")
    if "EVENT_RISK" in now.news_tags and "EVENT_RISK" not in prev.news_tags:
        add("EVENT_RISK", "news tagged the stock EVENT_RISK")
    # The same alert for every open position in the stock, put or call spread, from a signal or entered
    # by hand. Produced only once the user has approved reversal alerts (reversal.alerts_approved).
    if reversal_alerts and now.reversal and not prev.reversal:
        add("REVERSAL_CONFIRMED", f"a reversal was confirmed: close {fmt(now.close)} above the 20-day average and the "
                                  f"last swing high, on higher volume and ahead of the index"
                                  + (" (against this bear call spread)" if not put else ""))
    return out
