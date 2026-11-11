"""Quality gate. A tick that fails any check is quarantined and never reaches
bars or (later) strategies. Unknown values stay unknown; they are never
converted to zero."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from .models import Tick

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

BAD_PRICE = "BAD_PRICE"
CROSSED_QUOTE = "CROSSED_QUOTE"
PRICE_JUMP = "PRICE_JUMP"
VOLUME_WENT_BACK = "VOLUME_WENT_BACK"


def is_market_hours(now_utc: datetime) -> bool:
    """Weekday 09:15-15:30 IST. Exchange holidays are added in a later step."""
    now = now_utc.astimezone(IST)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


JUMP_WINDOW_S = 120  # only compare against a recent tick, so an overnight gap
                     # or a long pause cannot block a symbol forever


def check_tick(tick: Tick, prev_good: Tick | None, max_jump_pct: float) -> list[str]:
    """Return a list of reason codes. Empty list = tick is good."""
    reasons: list[str] = []
    if tick.last is None or tick.last != tick.last or tick.last <= 0:
        return [BAD_PRICE]
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
              enforce_hours: bool = True) -> str:
    """LIVE, STALE, CLOSED or NO_DATA for one symbol."""
    if enforce_hours and not is_market_hours(now_utc):
        return "CLOSED"
    if last_good_at is None:
        return "NO_DATA"
    age = (now_utc - last_good_at).total_seconds()
    return "LIVE" if age <= stale_after_s else "STALE"
