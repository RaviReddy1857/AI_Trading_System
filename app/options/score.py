"""Phase 9 (M2): the candidate score. A ranking aid, never a probability.

Six factors, each mapped to 0-100 points by a fixed rule written down here BEFORE any result was
produced, combined as a weighted mean (equal weights by default, `score.weights` in config.yaml):

1. iv_rank: the stock's IV rank as it is (0-100). Higher implied volatility pays more premium.
2. iv_falling: IV against its peak of the last `iv_peak_sessions` (20) sessions. At or below
   `iv_falling_ratio` (90%) of the peak = 100 points (IV is coming down); at the peak = 0 (IV is rising
   or at its high); linear between.
3. rel_strength: the stock's `rel_strength_lookback` (63-session) return minus NIFTY's, and minus the
   median of its sector peers (watchlist stocks of the same sector), averaged (index only when it has
   no peer with data). For a put spread 50 + 5 points per percentage point, for a call spread 50 - 5,
   clamped to 0-100: +10 points ahead gives 100 for a put spread.
4. strike_distance: the sold strike's distance from the price against (a) the expected move to expiry
   (spot x IV x sqrt(DTE/365)) and (b) the 90th percentile of the stock's 21-session falls since
   2019-01-22 (rises for a call spread), each from windows completed before the decision. Each part gives
   50 points at 1.5x or more of its yardstick, linearly from 0; a 90th-percentile fall of zero or less
   gives that part its 50 points.
5. range_profile: the share of past start days on which a strike this far away was passed within the
   holding period (two years of closes before the decision): 0% = 100 points, 50% or more = 0.
6. liquidity: the sold strike's open interest in lots against the minimum: at the minimum 0 points, at
   10x or more 100, on a log scale.

A factor whose inputs are unknown has no points; the total is then unknown ("score incomplete") and
the candidate is ranked after every fully scored one. Unknown never becomes zero.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from ..indicators import percentile
from ..strategies.base import ok
from .stock_profile import breach_frequency, expected_move

FACTORS = ("iv_rank", "iv_falling", "rel_strength", "strike_distance", "range_profile", "liquidity")


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def iv_falling_points(iv_now: float | None, iv_window: Sequence[float], ratio: float) -> float | None:
    """100 at or below `ratio` x the window's peak, 0 at the peak, linear between."""
    xs = [x for x in iv_window if ok(x)]
    if not ok(iv_now) or not xs:
        return None
    peak = max(xs + [iv_now])
    r = iv_now / peak
    if r <= ratio:
        return 100.0
    return _clamp((1.0 - r) / (1.0 - ratio) * 100.0)


def rel_strength_points(stock_ret: float | None, index_ret: float | None, peer_rets: Sequence[float], right: str) -> float | None:
    """Points from the return against the index and against the median of the sector peers (in %)."""
    if stock_ret is None or index_ret is None:
        return None
    parts = [stock_ret - index_ret]
    if peer_rets:
        parts.append(stock_ret - statistics.median(peer_rets))
    rel = sum(parts) / len(parts)
    return _clamp(50.0 + 5.0 * rel) if right == "PE" else _clamp(50.0 - 5.0 * rel)


def fall_percentile(closes: Sequence[float], sessions: int, pct: float, right: str) -> float | None:
    """The `pct` percentile of the stock's `sessions`-session falls (rises for a call spread), in %
    of the starting price, over every window that ends on or before the last close given."""
    moves = []
    for i in range(len(closes) - sessions):
        a, b = closes[i], closes[i + sessions]
        if a > 0:
            m = (b / a - 1) * 100
            moves.append(-m if right == "PE" else m)
    if len(moves) < 30:
        return None
    return percentile(moves, pct)


def strike_distance_points(distance_pct: float | None, em_pct: float | None, fall_pct: float | None) -> float | None:
    """50 points for each yardstick the distance reaches 1.5x of, linearly from 0. A 90th-percentile
    fall of zero or less (the stock did not fall over 21 sessions even in its worst tenth of windows)
    leaves nothing to beat: that half is full."""
    if distance_pct is None or em_pct is None or fall_pct is None or em_pct <= 0:
        return None
    half = lambda r: 50.0 * min(r, 1.5) / 1.5
    return round(half(distance_pct / em_pct) + (50.0 if fall_pct <= 0 else half(distance_pct / fall_pct)), 4)


def range_profile_points(breach_pct: float | None) -> float | None:
    return None if breach_pct is None else _clamp(100.0 - 2.0 * breach_pct)


def liquidity_points(oi_lots: float | None, min_lots: float) -> float | None:
    if oi_lots is None or oi_lots <= 0 or min_lots <= 0:
        return None
    return _clamp(math.log10(max(oi_lots / min_lots, 1.0)) * 100.0)


def score(points: dict, weights: dict) -> dict:
    """{"total": 0-100 or None, "factors": points, "contributions": weight share x points}."""
    w = {k: float(weights.get(k, 0.0)) for k in FACTORS}
    total_w = sum(w.values())
    known = all(points.get(k) is not None for k in FACTORS if w[k] > 0)
    contrib = {k: (None if points.get(k) is None else round(w[k] / total_w * points[k], 2)) for k in FACTORS} \
        if total_w > 0 else {k: None for k in FACTORS}
    total = round(sum(v for v in contrib.values() if v is not None), 2) if known and total_w > 0 else None
    return {"total": total, "factors": {k: (None if points.get(k) is None else round(points[k], 2)) for k in FACTORS},
            "contributions": contrib, "complete": total is not None,
            "missing": [k for k in FACTORS if points.get(k) is None and w[k] > 0]}


def candidate_score(ctx, right: str, short_strike: float, oi_lots: float | None, dte_days: int, cfg, sos) -> dict:
    """The score of a candidate from what is known at its decision time (ctx.market carries the inputs
    the strategy cannot compute itself: the IV window, the index and sector returns)."""
    m = ctx.market or {}
    closes = [b.close for b in ctx.daily]
    distance = abs(ctx.spot - short_strike) / ctx.spot * 100 if ok(ctx.spot) else None
    em = expected_move(ctx.spot, ctx.iv_reference_pct, dte_days)
    em_pct = None if em is None else em / ctx.spot * 100
    floor_closes = [b.close for b in ctx.daily if m.get("stats_floor") is None or b.start >= m["stats_floor"]]
    points = {
        "iv_rank": None if ctx.iv_rank is None else _clamp(ctx.iv_rank),
        "iv_falling": iv_falling_points(ctx.iv_reference_pct, m.get("iv_window") or (), cfg.iv_falling_ratio),
        "rel_strength": rel_strength_points(m.get("stock_ret_pct"), m.get("index_ret_pct"), m.get("peer_rets_pct") or (), right),
        "strike_distance": strike_distance_points(distance, em_pct,
                                                  fall_percentile(floor_closes, cfg.hist_fall_sessions, cfg.hist_fall_percentile, right)),
        "range_profile": range_profile_points(
            breach_frequency(closes[-504:], distance, right, sos.profile.holding_sessions)["breached_pct"]
            if distance is not None else None),
        "liquidity": liquidity_points(oi_lots, sos.min_open_interest_lots),
    }
    out = score(points, cfg.weights.model_dump())
    out["inputs"] = {"distance_pct": None if distance is None else round(distance, 2),
                     "expected_move_pct": None if em_pct is None else round(em_pct, 2), "oi_lots": oi_lots}
    return out
