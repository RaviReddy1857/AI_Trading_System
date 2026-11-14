from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Tick:
    symbol: str
    last: float
    bid: float | None = None       # None = not provided (indices have no bid/ask)
    ask: float | None = None
    cum_volume: float | None = None  # total traded quantity for the day
    prev_close: float | None = None
    source: str = "sim"
    received_at: datetime = field(default_factory=utcnow)
    day_high: float | None = None    # session high/low as reported by the feed, if any
    day_low: float | None = None


@dataclass
class Bar:
    symbol: str
    start: datetime      # minute start, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float | None = 0.0   # None = unknown (index history carries no volume)
    ticks: int = 0


# ---------------- phase 3: signals ----------------
ASSET_CLASSES = ("CASH_EQUITY", "INDEX_OPTION", "STOCK_OPTION")
DIRECTIONS = ("LONG", "SHORT", "SPREAD")
HORIZONS = ("INTRADAY", "SWING", "WEEKLY", "MONTHLY")


class InvalidSignal(ValueError):
    pass


UNHEDGED_SHORT = "UNHEDGED_SHORT"


class UnhedgedShort(ValueError):
    """A sold option without a bought option of the same underlying, right and expiry further out."""


def _leg_value(leg, name, default=None):
    v = leg.get(name) if isinstance(leg, dict) else getattr(leg, name, None)
    return default if v is None else v


def unhedged_short_legs(legs, expiry=None, lots=None, lot_size=None) -> list:
    """The sold legs that are NOT covered. A sold option is covered only by a bought option of the same
    right and expiry, further out of the money (puts: lower strike; calls: higher), with at least as
    many shares; each bought option covers one sold option. All legs of one signal or trade belong to
    one underlying. Legs may be OptionLeg objects or dicts (stored backtest trades); `expiry`, `lots`
    and `lot_size` are the trade-level values used when a leg dict does not carry its own.
    A leg whose action, right, strike or size cannot be read counts as uncovered: unknown is never
    treated as hedged."""
    def read(leg):
        try:
            n = int(_leg_value(leg, "lots", lots)) * int(_leg_value(leg, "lot_size", lot_size))
            return (str(_leg_value(leg, "action")), str(_leg_value(leg, "right")), float(_leg_value(leg, "strike")),
                    str(_leg_value(leg, "expiry", expiry)), n)
        except (TypeError, ValueError):
            return None

    rows = [(leg, read(leg)) for leg in legs or ()]
    bought = [list(r) for _, r in rows if r is not None and r[0] == "BUY"]
    out = []
    for leg, r in rows:
        if r is None:
            out.append(leg)
            continue
        action, right, strike, exp, need = r
        if action == "BUY":
            continue
        if action != "SELL" or right not in ("CE", "PE") or exp == "None" or need < 1:
            out.append(leg)
            continue
        for b in bought:
            further = b[2] < strike if right == "PE" else b[2] > strike
            if b[1] == right and b[3] == exp and further and b[4] >= need:
                b[4] -= need
                break
        else:
            out.append(leg)
    return out


@dataclass(frozen=True)
class Signal:
    """A trade idea (architecture.md section 6). Prices are computed by code, never by an LLM.
    `entry` is the reference price (the signal bar's close); a fill happens later, at the next
    bar's open at the earliest. `entry_zone` is the acceptable fill range, if the strategy gives one."""
    signal_id: str
    created_at: datetime            # UTC: close time of the bar that produced the signal
    strategy_id: str
    strategy_version: str
    symbol: str
    asset_class: str
    direction: str
    horizon: str
    entry: float
    stop: float
    target: float
    valid_until: datetime           # UTC
    regime: str
    why_now: tuple[str, ...]
    data_as_of: datetime            # UTC: newest data the strategy saw
    entry_zone: tuple[float, float] | None = None
    legs: tuple | None = None       # options only (phase 4): OptionLeg (action, right, strike, expiry, lots, price)
    # options only (phase 4): figures computed by code for ONE lot - premium, lot size, total cost,
    # max loss in rupees and % of capital, max profit, breakevens, costs, margin, warnings
    details: dict | None = None

    def __post_init__(self):
        problems = signal_problems(self)
        if problems:
            raise InvalidSignal("; ".join(problems))

    @property
    def reason(self) -> str:
        return "; ".join(self.why_now)

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id, "created_at": self.created_at.isoformat(),
            "strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
            "symbol": self.symbol, "asset_class": self.asset_class, "direction": self.direction,
            "horizon": self.horizon, "entry": self.entry, "stop": self.stop, "target": self.target,
            "entry_zone": list(self.entry_zone) if self.entry_zone else None,
            "valid_until": self.valid_until.isoformat(), "regime": self.regime,
            "why_now": list(self.why_now), "data_as_of": self.data_as_of.isoformat(),
            "legs": [leg.to_dict() if hasattr(leg, "to_dict") else leg for leg in self.legs] if self.legs else None,
            "details": self.details,
        }


def signal_problems(s: "Signal") -> list[str]:
    """Schema check for a Signal: types, enumerations, finite positive prices, and a stop and
    target on the correct sides of the entry."""
    import math
    out = []
    for name in ("signal_id", "strategy_id", "strategy_version", "symbol", "regime"):
        if not isinstance(getattr(s, name), str) or not getattr(s, name):
            out.append(f"{name} must be a non-empty string")
    if s.asset_class not in ASSET_CLASSES:
        out.append(f"asset_class {s.asset_class!r} not in {ASSET_CLASSES}")
    if s.direction not in DIRECTIONS:
        out.append(f"direction {s.direction!r} not in {DIRECTIONS}")
    if s.horizon not in HORIZONS:
        out.append(f"horizon {s.horizon!r} not in {HORIZONS}")
    for name in ("created_at", "valid_until", "data_as_of"):
        v = getattr(s, name)
        if not isinstance(v, datetime) or v.tzinfo is None:
            out.append(f"{name} must be a timezone-aware datetime")
    prices = {"entry": s.entry, "stop": s.stop, "target": s.target}
    bad = [k for k, v in prices.items()
           if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0]
    if bad:
        out.append(f"{', '.join(bad)} must be finite and above zero")
    elif s.direction == "LONG" and not (s.stop < s.entry < s.target):
        out.append(f"LONG needs stop < entry < target (got {s.stop}, {s.entry}, {s.target})")
    elif s.direction == "SHORT" and not (s.target < s.entry < s.stop):
        out.append(f"SHORT needs target < entry < stop (got {s.target}, {s.entry}, {s.stop})")
    times_ok = all(isinstance(getattr(s, n), datetime) and getattr(s, n).tzinfo is not None
                   for n in ("created_at", "valid_until", "data_as_of"))
    if times_ok and not out and s.valid_until <= s.created_at:
        out.append("valid_until must be after created_at")
    if s.entry_zone is not None:
        lo, hi = s.entry_zone
        if not (0 < lo <= hi):
            out.append("entry_zone must be (low, high) with 0 < low <= high")
    if not s.why_now or not all(isinstance(x, str) and x for x in s.why_now):
        out.append("why_now must list at least one reason")
    if times_ok and s.data_as_of > s.created_at:
        out.append("data_as_of is later than created_at (look-ahead)")
    if s.asset_class in ("INDEX_OPTION", "STOCK_OPTION"):
        if not s.legs:
            out.append("an option signal must list its legs")
        if not isinstance(s.details, dict):
            out.append("an option signal must carry its computed figures (details)")
        if s.legs and unhedged_short_legs(s.legs):
            out.append(f"{UNHEDGED_SHORT}: a sold option has no bought option of the same underlying, right "
                       f"and expiry further out of the money")
    return out
