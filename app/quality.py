"""Quality gate. A tick that fails any check is quarantined and never reaches
bars or (later) strategies. Unknown values stay unknown; they are never
converted to zero."""
from __future__ import annotations

import math
from collections.abc import Collection
from datetime import date, datetime, time, timedelta, timezone

from .models import Tick
from .session import AUCTION, CLOSED, IST, calendar as session_calendar

MARKET_OPEN = time(9, 15)
# No single close any more: each segment has its own, by date (app/session.py; closing auction from 3 Aug 2026).

BAD_PRICE = "BAD_PRICE"
CROSSED_QUOTE = "CROSSED_QUOTE"
PRICE_JUMP = "PRICE_JUMP"
VOLUME_WENT_BACK = "VOLUME_WENT_BACK"
BAD_VOLUME = "BAD_VOLUME"            # negative or non-finite cumulative volume
BAD_PREV_CLOSE = "BAD_PREV_CLOSE"    # negative or non-finite previous close
# The problems of LIVE PRICE TICKS of stocks and indices (`check_tick`): the only quality events the kill switch counts.
# Option chain rows and history bars are logged under other reasons and never count toward it.
TICK_REASONS = frozenset({BAD_PRICE, CROSSED_QUOTE, PRICE_JUMP, VOLUME_WENT_BACK, BAD_VOLUME, BAD_PREV_CLOSE})


def is_market_hours(now_utc: datetime, holidays: Collection[date] = (), segment: str | None = None,
                    continuous: bool = False) -> bool:
    """A trading day (weekday, not in data/holidays.csv) and, by the session calendar (app/session.py):
    - `segment` None: some segment is still trading (09:15 to the last close: 15:30 before 3 Aug 2026, 15:40 after);
    - a segment: that segment is OPEN or in its closing auction; with `continuous` only while it is OPEN
      (continuous trading: prices are trades)."""
    cal = session_calendar()
    if segment is None:
        return cal.any_open(now_utc, holidays)
    ph = cal.phase(segment, now_utc, holidays)
    return ph == "OPEN" if continuous else ph != CLOSED


JUMP_WINDOW_S = 120  # only compare against a recent tick, so an overnight gap
                     # or a long pause cannot block a symbol forever


def is_finite(x) -> bool:
    """True only for a real, finite number (rejects None, NaN, +inf, -inf, non-numbers)."""
    try:
        return x is not None and math.isfinite(x)
    except TypeError:
        return False


def check_tick(tick: Tick, prev_good: Tick | None, max_jump_pct: float) -> list[str]:
    """Return a list of reason codes. Empty list = tick is good."""
    reasons: list[str] = []
    if not is_finite(tick.last) or tick.last <= 0:
        return [BAD_PRICE]
    # A field that is present must be a finite, non-negative number; None means "not provided".
    if any(q is not None and not (is_finite(q) and q >= 0) for q in (tick.bid, tick.ask)):
        return [BAD_PRICE]
    if tick.cum_volume is not None and not (is_finite(tick.cum_volume) and tick.cum_volume >= 0):
        reasons.append(BAD_VOLUME)
    if tick.prev_close is not None and not (is_finite(tick.prev_close) and tick.prev_close >= 0):
        reasons.append(BAD_PREV_CLOSE)
    if reasons:
        return reasons
    if tick.bid is not None and tick.ask is not None and tick.bid > 0 and tick.ask > 0:
        if tick.bid > tick.ask:
            reasons.append(CROSSED_QUOTE)
    if prev_good is not None and prev_good.last > 0:
        move = abs(tick.last - prev_good.last) / prev_good.last * 100.0
        gap_s = (tick.received_at - prev_good.received_at).total_seconds()
        if move > max_jump_pct and gap_s <= JUMP_WINDOW_S:
            reasons.append(PRICE_JUMP)
        if (
            tick.cum_volume is not None
            and prev_good.cum_volume is not None
            and tick.cum_volume < prev_good.cum_volume
            and tick.received_at.astimezone(IST).date()
            == prev_good.received_at.astimezone(IST).date()
        ):
            reasons.append(VOLUME_WENT_BACK)
    return reasons


def freshness(last_good_at: datetime | None, now_utc: datetime, stale_after_s: int,
              enforce_hours: bool = True, holidays: Collection[date] = (), segment: str | None = None) -> str:
    """LIVE, STALE, CLOSED or NO_DATA for one symbol; AUCTION while its segment (`segment`, app/session.py) is in
    the closing auction (F&O stocks and indices from 3 Aug 2026: auction indications or indicative values, never
    usable). Without a segment: CLOSED outside every segment's hours."""
    if enforce_hours:
        if segment is None:
            if not is_market_hours(now_utc, holidays):
                return "CLOSED"
        else:
            ph = session_calendar().phase(segment, now_utc, holidays)
            if ph == CLOSED:
                return "CLOSED"
            if ph == AUCTION:
                return "AUCTION"
    if last_good_at is None:
        return "NO_DATA"
    age = (now_utc - last_good_at).total_seconds()
    return "LIVE" if age <= stale_after_s else "STALE"
