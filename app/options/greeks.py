"""Black-Scholes price, implied volatility and delta for European options (NSE index and stock
options are European). Pure functions: numbers are computed here by code, never by an LLM.

Inputs: spot S, strike K, time to expiry t in YEARS, risk-free rate r and dividend yield q as
decimals (0.065 = 6.5%), volatility sigma as a decimal, right "CE" (call) or "PE" (put).
The rate and dividend yield are assumptions from config.yaml -> options.

Unknown stays unknown: every function returns None instead of a guess when an input is missing,
non-finite or out of range, or when the price is outside what any volatility can produce.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timezone

from ..quality import IST
from ..session import DERIVATIVES, hours as session_hours

RIGHTS = ("CE", "PE")
MIN_SIGMA, MAX_SIGMA = 0.0005, 5.0          # 0.05% .. 500% annualised


def _ok(*xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def years_to_expiry(asof: datetime, expiry: date) -> float | None:
    """Calendar time from `asof` to the options' close on the expiry date (app/session.py: 15:30 IST before 3 Aug
    2026, 15:40 after), in years (365 days). None if past."""
    end = datetime.combine(expiry, session_hours(DERIVATIVES, expiry).close, IST)
    secs = (end - asof.astimezone(IST)).total_seconds()
    return secs / (365.0 * 86400.0) if secs > 0 else None


def days_to_expiry(asof: datetime | date, expiry: date) -> int:
    """Whole calendar days from the date of `asof` (IST) to the expiry date."""
    d = asof.astimezone(IST).date() if isinstance(asof, datetime) else asof
    return (expiry - d).days


def _d1(s: float, k: float, t: float, r: float, q: float, sigma: float) -> float:
    return (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))


def bs_price(s: float, k: float, t: float, r: float, q: float, sigma: float, right: str) -> float | None:
    if right not in RIGHTS or not _ok(s, k, t, r, q, sigma) or min(s, k, t, sigma) <= 0:
        return None
    d1 = _d1(s, k, t, r, q, sigma)
    d2 = d1 - sigma * math.sqrt(t)
    df_q, df_r = math.exp(-q * t), math.exp(-r * t)
    if right == "CE":
        return s * df_q * norm_cdf(d1) - k * df_r * norm_cdf(d2)
    return k * df_r * norm_cdf(-d2) - s * df_q * norm_cdf(-d1)


def bs_delta(s: float, k: float, t: float, r: float, q: float, sigma: float, right: str) -> float | None:
    """Delta per unit of the underlying: 0..1 for a call, -1..0 for a put."""
    if right not in RIGHTS or not _ok(s, k, t, r, q, sigma) or min(s, k, t, sigma) <= 0:
        return None
    d1 = _d1(s, k, t, r, q, sigma)
    df_q = math.exp(-q * t)
    return df_q * norm_cdf(d1) if right == "CE" else -df_q * norm_cdf(-d1)


def price_bounds(s: float, k: float, t: float, r: float, q: float, right: str) -> tuple[float, float]:
    """(lowest, highest) price any volatility can give: discounted intrinsic value .. discounted S or K."""
    fwd_s, pv_k = s * math.exp(-q * t), k * math.exp(-r * t)
    if right == "CE":
        return max(0.0, fwd_s - pv_k), fwd_s
    return max(0.0, pv_k - fwd_s), pv_k


def implied_vol(price: float, s: float, k: float, t: float, r: float, q: float, right: str,
                tol: float = 1e-7) -> float | None:
    """The volatility at which Black-Scholes gives `price` (bisection; the price rises with
    volatility). None if the price is at or below the no-volatility value, at or above the upper
    bound, or needs a volatility outside 0.05%..500%."""
    if right not in RIGHTS or not _ok(price, s, k, t, r, q) or min(price, s, k, t) <= 0:
        return None
    lo_p, hi_p = price_bounds(s, k, t, r, q, right)
    if price <= lo_p or price >= hi_p:
        return None
    lo, hi = MIN_SIGMA, MAX_SIGMA
    p_lo, p_hi = bs_price(s, k, t, r, q, lo, right), bs_price(s, k, t, r, q, hi, right)
    if p_lo is None or p_hi is None or price < p_lo or price > p_hi:
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        p = bs_price(s, k, t, r, q, mid, right)
        if p is None:
            return None
        if p < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def iv_and_delta(price: float | None, s: float | None, k: float, t: float | None, r: float, q: float,
                 right: str) -> tuple[float | None, float | None]:
    """(implied volatility, delta) from an option price; (None, None) when either cannot be computed."""
    if price is None or s is None or t is None:
        return None, None
    iv = implied_vol(price, s, k, t, r, q, right)
    if iv is None:
        return None, None
    return iv, bs_delta(s, k, t, r, q, iv, right)


def iv_rank(current: float | None, history: list[float], min_history: int) -> float | None:
    """IV rank 0..100: where `current` sits between the lowest and highest value of `history`
    (the past year, oldest first, not including `current`). None with too little history, a
    non-finite value, or a flat history."""
    vals = [v for v in history if _ok(v) and v > 0]
    if not _ok(current) or current <= 0 or len(vals) < min_history:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return None
    return max(0.0, min(100.0, (current - lo) / (hi - lo) * 100.0))


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)
