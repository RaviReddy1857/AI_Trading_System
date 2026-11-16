"""Phase 6 signal score: 0-100 from confluence, computed by code. A ranking aid, never a probability.

Six factors, each 0-100 points (None = not known; the score is the weighted mean of the known factors and lists
the unknown ones; with fewer than `scoring.min_known_factors` known factors there is no score and the signal is
ranked after every scored one):

1. timeframes: the 5-minute, 15-minute and daily trends agree with the trade (trend = last close above / below the
   simple average of the last `sma_5m`, `sma_15m`, `sma_daily` closes). Points = 100 x agreeing / 3; needs all three.
2. volume: the signal bar's volume against the average volume of the same bar in the previous sessions (this
   session's earlier bars when there are none). 0 points at 0.5x or less, 100 at 2x or more, linear between.
   Indices (no volume): not known.
3. relative strength: today's return of the stock minus NIFTY's, and minus the median of its sector peers (each in
   percentage points, averaged). 50 + 10 per point for a long (50 - 10 for a short), clamped to 0-100.
4. regime fit: LONG: TRENDING_UP 100, RANGE 50, VOLATILE 25, TRENDING_DOWN 0, EVENT 0. SHORT mirrored. A NEUTRAL
   trade (iron condor): RANGE 100, TRENDING 50, VOLATILE 0, EVENT 0. UNKNOWN regime: not known.
5. FII bias: agrees 100, NEUTRAL 50, against 0 (NEUTRAL trade: 50 whatever the bias). UNKNOWN: not known.
6. room to the nearest level: for a long, the lowest daily high above the entry among the last `sr_lookback_days`
   sessions (resistance); points = 100 x (resistance - entry) / (target - entry), clamped; no level in the way = 100.
   Short: the highest daily low below the entry (support). NEUTRAL: not applied (not known).
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

FACTORS = ("timeframes", "volume", "relative_strength", "regime_fit", "fii_bias", "room_to_level")


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class ScoreInputs:
    direction: str                         # LONG | SHORT | NEUTRAL (the trade's view of the underlying)
    entry: float | None                    # the underlying's price at the signal
    target: float | None                   # the underlying's target (None for options without one)
    closes_5m: Sequence[float] = ()        # this session's completed 5-minute closes (oldest first)
    closes_15m: Sequence[float] = ()       # 15-minute closes (prior sessions allowed), oldest first
    daily_closes: Sequence[float] = ()     # completed daily closes, oldest first
    daily_highs: Sequence[float] = ()
    daily_lows: Sequence[float] = ()
    bar_volume: float | None = None        # the signal bar's volume
    avg_volume: float | None = None        # the same bar's average volume in earlier sessions
    stock_return_pct: float | None = None  # today, from the previous close
    index_return_pct: float | None = None
    peer_returns_pct: Sequence[float] = ()
    regime: str | None = None
    bias: str | None = None
    extra: dict = field(default_factory=dict)


def trend(closes: Sequence[float], n: int) -> str | None:
    if len(closes) < n or not all(_ok(c) for c in closes[-n:]):
        return None
    avg = sum(closes[-n:]) / n
    return "UP" if closes[-1] > avg else "DOWN" if closes[-1] < avg else "FLAT"


def factor_points(x: ScoreInputs, cfg) -> dict[str, tuple[float | None, str]]:
    """factor -> (points or None, why)."""
    out: dict[str, tuple[float | None, str]] = {}
    want = {"LONG": "UP", "SHORT": "DOWN"}.get(x.direction)
    trends = [trend(x.closes_5m, cfg.sma_5m), trend(x.closes_15m, cfg.sma_15m), trend(x.daily_closes, cfg.sma_daily)]
    if want is None:
        out["timeframes"] = (None, "a neutral trade has no direction to agree with")
    elif any(t is None for t in trends):
        out["timeframes"] = (None, "not enough bars for every timeframe")
    else:
        agree = sum(1 for t in trends if t == want)
        out["timeframes"] = (round(100 * agree / 3, 1), f"5m {trends[0]}, 15m {trends[1]}, daily {trends[2]}: "
                                                         f"{agree} of 3 agree")
    if _ok(x.bar_volume) and _ok(x.avg_volume) and x.avg_volume > 0:
        r = x.bar_volume / x.avg_volume
        out["volume"] = (round(clamp((r - 0.5) / 1.5 * 100), 1), f"signal bar volume {r:.2f}x its average")
    else:
        out["volume"] = (None, "volume not known")
    if want is not None and _ok(x.stock_return_pct) and _ok(x.index_return_pct) and x.peer_returns_pct:
        vs_index = x.stock_return_pct - x.index_return_pct
        vs_peers = x.stock_return_pct - statistics.median(x.peer_returns_pct)
        d = (vs_index + vs_peers) / 2
        pts = 50 + 10 * d if want == "UP" else 50 - 10 * d
        out["relative_strength"] = (round(clamp(pts), 1), f"{vs_index:+.2f} pts vs NIFTY, {vs_peers:+.2f} pts vs sector peers")
    else:
        out["relative_strength"] = (None, "returns of the stock, NIFTY or the sector peers not known"
                                    if want is not None else "a neutral trade has no direction")
    table = {"LONG": {"TRENDING_UP": 100, "RANGE": 50, "VOLATILE": 25, "TRENDING_DOWN": 0, "EVENT": 0},
             "SHORT": {"TRENDING_DOWN": 100, "RANGE": 50, "VOLATILE": 25, "TRENDING_UP": 0, "EVENT": 0},
             "NEUTRAL": {"RANGE": 100, "TRENDING_UP": 50, "TRENDING_DOWN": 50, "VOLATILE": 0, "EVENT": 0}}
    pts = table.get(x.direction, {}).get(x.regime or "")
    out["regime_fit"] = (None, f"regime {x.regime or 'UNKNOWN'}") if pts is None else (float(pts), f"regime {x.regime}")
    if x.bias in ("BULLISH", "BEARISH", "NEUTRAL"):
        if x.direction == "NEUTRAL" or x.bias == "NEUTRAL":
            out["fii_bias"] = (50.0, f"FII bias {x.bias}")
        else:
            agrees = (x.bias == "BULLISH") == (x.direction == "LONG")
            out["fii_bias"] = (100.0 if agrees else 0.0, f"FII bias {x.bias}")
    else:
        out["fii_bias"] = (None, "FII bias not known")
    out["room_to_level"] = _room(x, cfg)
    return out


def _room(x: ScoreInputs, cfg) -> tuple[float | None, str]:
    if x.direction not in ("LONG", "SHORT") or not (_ok(x.entry) and _ok(x.target)) or x.target == x.entry:
        return None, "no directional target to compare with a level"
    n = cfg.sr_lookback_days
    if len(x.daily_highs) < n or len(x.daily_lows) < n:
        return None, f"fewer than {n} daily bars"
    if x.direction == "LONG":
        above = [h for h in x.daily_highs[-n:] if _ok(h) and h > x.entry]
        if not above:
            return 100.0, f"no {n}-day high above the entry"
        lvl = min(above)
        return round(clamp((lvl - x.entry) / (x.target - x.entry) * 100), 1), f"resistance {lvl:,.2f} ({n}-day highs)"
    below = [lo for lo in x.daily_lows[-n:] if _ok(lo) and lo < x.entry]
    if not below:
        return 100.0, f"no {n}-day low below the entry"
    lvl = max(below)
    return round(clamp((x.entry - lvl) / (x.entry - x.target) * 100), 1), f"support {lvl:,.2f} ({n}-day lows)"


def score(x: ScoreInputs, cfg) -> dict:
    """{"score": 0-100 or None, "factors": {name: {points, weight, why}}, "unknown": [...], "note"}."""
    pts = factor_points(x, cfg)
    weights = cfg.weights.model_dump()
    known = {k: v for k, (v, _) in pts.items() if v is not None}
    total_w = sum(weights[k] for k in known)
    s = None
    if len(known) >= cfg.min_known_factors and total_w > 0:
        s = round(sum(known[k] * weights[k] for k in known) / total_w, 1)
    return {"score": s,
            "factors": {k: {"points": v, "weight": weights[k], "why": why} for k, (v, why) in pts.items()},
            "unknown": [k for k in FACTORS if pts[k][0] is None],
            "note": "0-100 from confluence; a ranking aid, never a probability"
                    + ("" if s is not None else f"; fewer than {cfg.min_known_factors} factors known: not scored")}


def rank(rows: list[dict]) -> list[dict]:
    """Highest score first; unscored after every scored one; ties by signal_id (deterministic)."""
    return sorted(rows, key=lambda r: (r.get("score") is None, -(r.get("score") or 0), r.get("signal_id", "")))
