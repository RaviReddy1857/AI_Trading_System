"""Phase 6 journal: stored signals, my decisions on them, open and closed positions (taken from a card or
entered by hand), paper trades, and the account state of each capital pool for the risk engine.

Research and suggestions only. Every write here is a journal entry: nothing is sent to the broker, and
nothing here can place, change or close an order. A position is "closed" only when I say so
(`close_position`) or, for a paper trade, when the paper trader records its exit.

Two capital pools (user decision 2026-11-16, docs/STATUS.md issue 95):
- STOCK_OPTIONS: stock option selling (every position with a sold stock option leg). Capital:
  `stock_option_selling.backtest.capital_inr`.
- CASH_INDEX: intraday and swing stocks, index options, stock option buying. Capital: `account.capital_inr`.
The pool of a signal is the pool whose capital the risk engine sizes it on (`risk.engine.is_stock_spread`).
Each pool's realised and open P&L and equity history are kept apart, so a loss in one pool never stops or
halves the other. Deposits and withdrawals (capital_log) belong to the CASH_INDEX pool (the trading account).

The deposit rule (one rule, for the equity figures and the drawdown alike): a pool's equity at a day's close is
its configured capital + the net deposits dated after the pool's first equity point up to that day (CASH_INDEX
only; money already in the account when the journal began is in the configured capital) + realised P&L to date
+ open P&L. The drawdown (`app.risk.limits.drawdown_state`) takes the same flows out of each day's change, so a
deposit or a withdrawal alone moves equity but never the drawdown, and a trading loss after a deposit is measured
against the equity including the deposit.

Nothing is assumed. A position whose open P&L, risk or committed capital cannot be computed makes its pool
unknown (PORTFOLIO_UNKNOWN), and the risk engine says NO TRADE for that pool.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .models import Signal, unhedged_short_legs
from .quality import IST

TRADE_TYPES = ("INTRADAY_STOCKS", "SWING_STOCKS", "INDEX_OPTION_BUYING", "INDEX_OPTION_SELLING",
               "STOCK_OPTION_BUYING", "STOCK_OPTION_SELLING")
TRADE_TYPE_NAMES = {"INTRADAY_STOCKS": "Intraday stocks", "SWING_STOCKS": "Swing stocks",
                    "INDEX_OPTION_BUYING": "Index option buying", "INDEX_OPTION_SELLING": "Index option selling",
                    "STOCK_OPTION_BUYING": "Stock option buying", "STOCK_OPTION_SELLING": "Stock option selling"}
STOCK_OPTIONS, CASH_INDEX = "STOCK_OPTIONS", "CASH_INDEX"
POOLS = (CASH_INDEX, STOCK_OPTIONS)
TOOK, SKIPPED = "TOOK", "SKIPPED"
_NS = uuid.UUID("2b7f4c1e-6a0d-4e8b-9f3a-5c1d2e3f4a5b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY, created_utc TEXT NOT NULL, strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL, trade_type TEXT NOT NULL, pool TEXT NOT NULL, status TEXT NOT NULL,
    valid_until_utc TEXT NOT NULL, score REAL, signal_json TEXT NOT NULL, decision_json TEXT NOT NULL,
    card_json TEXT, stored_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signals_created ON signals (created_utc);
CREATE TABLE IF NOT EXISTS journal (
    signal_id TEXT PRIMARY KEY, decision TEXT NOT NULL, at_utc TEXT NOT NULL, note TEXT,
    fill_price REAL, fill_time_utc TEXT, fills_json TEXT, position_id TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY, origin TEXT NOT NULL, signal_id TEXT, trade_type TEXT NOT NULL,
    pool TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL, legs_json TEXT, quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL, entry_utc TEXT NOT NULL, stop REAL, target REAL, initial_stop REAL,
    underlying_stop REAL, underlying_target REAL, invalidation_json TEXT, margin_inr REAL,
    strategy_id TEXT, horizon TEXT, regime_at_entry TEXT, hedged INTEGER NOT NULL, status TEXT NOT NULL,
    opened_utc TEXT NOT NULL, closed_utc TEXT, plan_json TEXT NOT NULL, note TEXT, fill_estimated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS position_closes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL, at_utc TEXT NOT NULL, units INTEGER NOT NULL,
    exit_prices_json TEXT NOT NULL, gross_inr REAL, costs_inr REAL, net_inr REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS paper_trades (
    signal_id TEXT PRIMARY KEY, trade_type TEXT NOT NULL, pool TEXT NOT NULL, symbol TEXT NOT NULL,
    direction TEXT NOT NULL, legs_json TEXT, quantity INTEGER NOT NULL, status TEXT NOT NULL,
    planned_fill_utc TEXT, entry_utc TEXT, entry_price REAL, entry_prices_json TEXT, entry_source TEXT,
    stop REAL, target REAL, time_exit_utc TEXT, risk_inr REAL, margin_inr REAL, exit_utc TEXT, exit_price REAL,
    exit_prices_json TEXT, exit_reason TEXT, gross_inr REAL, costs_inr REAL, net_pnl_inr REAL, note TEXT,
    horizon TEXT, strategy_id TEXT
);
CREATE TABLE IF NOT EXISTS position_marks (
    ref TEXT NOT NULL, day TEXT NOT NULL, at_utc TEXT NOT NULL, open_pnl_inr REAL NOT NULL,
    PRIMARY KEY (ref, day)
);
CREATE TABLE IF NOT EXISTS equity_history (
    pool TEXT NOT NULL, day TEXT NOT NULL, equity_inr REAL NOT NULL, realised_cum_inr REAL NOT NULL,
    open_pnl_inr REAL NOT NULL, written_utc TEXT NOT NULL, note TEXT, PRIMARY KEY (pool, day)
);
CREATE TABLE IF NOT EXISTS alert_switches (
    trade_type TEXT PRIMARY KEY, is_on INTEGER NOT NULL, changed_utc TEXT
);
CREATE TABLE IF NOT EXISTS alert_switch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_type TEXT NOT NULL, old INTEGER NOT NULL, new INTEGER NOT NULL,
    at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engine_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_utc TEXT NOT NULL);
"""


class JournalError(ValueError):
    """A journal entry that cannot be stored as given (bad input, unknown signal, expired, duplicate)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _num(x) -> float | None:
    """A real, finite number, or None (never zero for unknown)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def _d(x) -> Decimal:
    return Decimal(str(x))


def _paise(x) -> Decimal:
    return _d(x).quantize(Decimal("0.01"))


def ist_day(t: datetime) -> date:
    return t.astimezone(IST).date()


# ---------------- trade types and pools ----------------
def trade_type_of(asset_class: str, horizon: str | None, legs) -> str:
    """One of TRADE_TYPES. Options: selling when any leg is sold (a spread, a condor or an unhedged short),
    buying when every leg is bought."""
    if asset_class == "CASH_EQUITY":
        return "INTRADAY_STOCKS" if horizon == "INTRADAY" else "SWING_STOCKS"
    selling = any(_leg(x, "action") == "SELL" for x in legs or ())
    if asset_class == "INDEX_OPTION":
        return "INDEX_OPTION_SELLING" if selling else "INDEX_OPTION_BUYING"
    if asset_class == "STOCK_OPTION":
        return "STOCK_OPTION_SELLING" if selling else "STOCK_OPTION_BUYING"
    raise ValueError(f"unknown asset class {asset_class!r}")


def pool_of_trade_type(trade_type: str) -> str:
    """STOCK_OPTIONS for stock option selling, CASH_INDEX for everything else (the capital the risk engine
    sizes it on: stock option spreads use stock_option_selling.backtest.capital_inr, the rest account.capital_inr)."""
    if trade_type not in TRADE_TYPES:
        raise ValueError(f"unknown trade type {trade_type!r}")
    return STOCK_OPTIONS if trade_type == "STOCK_OPTION_SELLING" else CASH_INDEX


def signal_trade_type(signal: Signal) -> str:
    return trade_type_of(signal.asset_class, signal.horizon, signal.legs)


def pool_capital(settings, pool: str) -> float:
    if pool == STOCK_OPTIONS:
        return settings.stock_option_selling.backtest.capital_inr
    return settings.account.capital_inr


def paper_capital(settings, pool: str) -> float:
    """The capital of each strategy's paper book in a pool: the pool's capital (as the real book)."""
    return pool_capital(settings, pool)


def _leg(leg, name):
    return leg.get(name) if isinstance(leg, dict) else getattr(leg, name, None)


# ---------------- positions ----------------
@dataclass(frozen=True)
class Leg:
    action: str          # BUY | SELL
    right: str           # CE | PE
    strike: float
    expiry: date
    lots: int
    lot_size: int
    price: float         # entry premium per share

    @property
    def shares(self) -> int:
        return self.lots * self.lot_size

    @property
    def sign(self) -> int:
        return 1 if self.action == "BUY" else -1

    def to_dict(self) -> dict:
        return {"action": self.action, "right": self.right, "strike": self.strike, "expiry": self.expiry.isoformat(),
                "lots": self.lots, "lot_size": self.lot_size, "price": self.price}

    @staticmethod
    def from_dict(d: dict) -> "Leg":
        return Leg(d["action"], d["right"], float(d["strike"]), date.fromisoformat(str(d["expiry"])[:10]),
                   int(d["lots"]), int(d["lot_size"]), float(d["price"]))


@dataclass(frozen=True)
class Position:
    """An open or closed position. Cash: `quantity` shares at `entry_price`. Options: `legs` (lots of each
    leg), `quantity` = lots of the position (every leg holds the same number of lots); `entry_price` is the
    premium paid per share (long option) or the net credit per share (spreads); `stop` and `target` are
    on the same price (option premium, or the cost per share to buy the spread back)."""
    position_id: str
    origin: str                       # SIGNAL | MANUAL | PAPER
    signal_id: str | None
    trade_type: str
    pool: str
    symbol: str                       # the stock or index (underlying for options)
    direction: str                    # LONG | SHORT | SPREAD
    legs: tuple[Leg, ...] | None
    quantity: int                     # cash: shares; options: lots
    entry_price: float
    entry_utc: datetime
    stop: float | None = None
    target: float | None = None
    initial_stop: float | None = None
    underlying_stop: float | None = None
    underlying_target: float | None = None
    invalidation: dict | None = None  # {"level": x, "side": "below"|"above", "why": "..."} on the underlying
    margin_inr: float | None = None
    strategy_id: str | None = None
    horizon: str | None = None
    regime_at_entry: str | None = None
    hedged: bool = True
    status: str = "OPEN"
    opened_utc: datetime | None = None
    closed_utc: datetime | None = None
    plan: dict = field(default_factory=dict)
    note: str = ""
    fill_estimated: bool = False

    @property
    def is_option(self) -> bool:
        return bool(self.legs)

    @property
    def view(self) -> str:
        """BULLISH, BEARISH or NEUTRAL: what the position needs the underlying to do."""
        if not self.is_option:
            return "BULLISH" if self.direction == "LONG" else "BEARISH"
        # with a sold leg, the sold legs carry the risk (bull put spread: bullish); else the bought legs
        main = [x for x in self.legs if x.action == "SELL"] or list(self.legs)
        score = 0
        for x in main:
            up = (x.right == "CE") == (x.action == "BUY")      # long call or short put gains when price rises
            score += x.lots if up else -x.lots
        return "BULLISH" if score > 0 else "BEARISH" if score < 0 else "NEUTRAL"

    @property
    def selling(self) -> bool:
        return any(x.action == "SELL" for x in self.legs or ())

    def to_view(self) -> dict:
        return {"position_id": self.position_id, "origin": self.origin, "signal_id": self.signal_id,
                "trade_type": self.trade_type, "trade_type_name": TRADE_TYPE_NAMES[self.trade_type],
                "pool": self.pool, "symbol": self.symbol, "direction": self.direction,
                "legs": [x.to_dict() for x in self.legs] if self.legs else None, "quantity": self.quantity,
                "unit": "lots" if self.is_option else "shares", "entry_price": self.entry_price,
                "entry_utc": self.entry_utc.isoformat(), "stop": self.stop, "target": self.target,
                "initial_stop": self.initial_stop, "underlying_stop": self.underlying_stop,
                "underlying_target": self.underlying_target, "invalidation": self.invalidation,
                "margin_inr": self.margin_inr, "strategy_id": self.strategy_id, "horizon": self.horizon,
                "regime_at_entry": self.regime_at_entry, "hedged": self.hedged,
                "flag": None if self.hedged else "UNHEDGED", "status": self.status,
                "closed_utc": self.closed_utc.isoformat() if self.closed_utc else None, "plan": self.plan,
                "note": self.note, "fill_estimated": self.fill_estimated, "view": self.view}


def scale_legs(legs, lots: int) -> tuple[Leg, ...]:
    return tuple(replace(x, lots=lots) for x in legs)


def pnl(pos: Position, exit_prices, units: int, costs) -> tuple[Decimal, Decimal | None, Decimal | None]:
    """(gross, charges, net) in rupees for closing `units` (shares or lots) at `exit_prices` (cash: one price;
    options: one price per leg, in leg order). Charges are the cost model's estimate of the round trip
    (None when the rates are missing: then the net is not known either)."""
    from .costs import MissingCostRates
    if not pos.is_option:
        px = exit_prices[0] if isinstance(exit_prices, (list, tuple)) else exit_prices
        entry, exit_ = _d(pos.entry_price), _d(px)
        sign = 1 if pos.direction == "LONG" else -1
        gross = (exit_ - entry) * units * sign
        buy, sell = (entry * units, exit_ * units) if sign > 0 else (exit_ * units, entry * units)
        seg = "intraday_equity" if pos.trade_type == "INTRADAY_STOCKS" else "delivery_equity"
        try:
            charges = costs.equity(seg, buy, sell).total if costs is not None else None
        except MissingCostRates:
            charges = None
        return _paise(gross), charges, None if charges is None else _paise(gross - charges)
    prices = list(exit_prices)
    if len(prices) != len(pos.legs):
        raise ValueError(f"{len(pos.legs)} leg prices needed, {len(prices)} given")
    gross, charges = Decimal(0), Decimal(0)
    for leg, px in zip(pos.legs, prices):
        shares = units * leg.lot_size
        entry, exit_ = _d(leg.price) * shares, _d(px) * shares
        gross += (exit_ - entry) * leg.sign
        try:
            buy, sell = (entry, exit_) if leg.action == "BUY" else (exit_, entry)
            charges = None if charges is None or costs is None else charges + costs.options(units, buy, sell).total
        except MissingCostRates:
            charges = None
    return _paise(gross), charges, None if charges is None else _paise(gross - charges)


def max_loss_inr(pos: Position, costs) -> Decimal | None:
    """Most the position can still lose from its entry, charges included. Cash: at the stop if one is set,
    else the whole value for a long and unknown for a short (no limit). Options: the payoff's max loss
    (spreads, condors, long options; app.risk.sizing.option_lots); an unhedged short put loses at most the
    strike less the premium; an unhedged short call has no limit (unknown)."""
    from .costs import MissingCostRates
    from .options.payoff import OptionLeg
    from .risk.sizing import option_lots
    try:
        if not pos.is_option:
            q = pos.quantity
            if pos.stop is not None:
                _, ch, net = pnl(pos, pos.stop, q, costs)
                return None if net is None else max(_paise(0), -net)
            if pos.direction != "LONG":
                return None
            _, ch, _ = pnl(pos, 0.0, q, costs)
            return None if ch is None else _paise(_d(pos.entry_price) * q + ch)
        legs = [OptionLeg(x.action, x.right, x.strike, x.expiry, pos.quantity, x.price, x.lot_size) for x in pos.legs]
        if pos.hedged:
            try:
                n, pay, _ = option_lots(legs, costs, 1e15, max_lots=pos.quantity)
                if n == pos.quantity and pay is not None:
                    return _paise(pay.max_loss_inr)
            except ValueError:
                pass
        if all(x.action == "BUY" for x in pos.legs):
            _, ch, _ = pnl(pos, [0.0] * len(pos.legs), pos.quantity, costs)
            prem = sum((_d(x.price) * pos.quantity * x.lot_size for x in pos.legs), Decimal(0))
            return None if ch is None else _paise(prem + ch)
        if len(pos.legs) == 1 and pos.legs[0].right == "PE":
            leg = pos.legs[0]
            _, ch, _ = pnl(pos, [leg.strike], pos.quantity, costs)
            return None if ch is None else _paise((_d(leg.strike) - _d(leg.price)) * pos.quantity * leg.lot_size + ch)
        return None
    except (MissingCostRates, ValueError, ArithmeticError):
        return None


def committed_inr(pos: Position) -> Decimal | None:
    """Capital in the position: cash value (cash), premium paid (bought options), margin blocked (anything
    with a sold leg; None until it is known - never assumed)."""
    if not pos.is_option:
        return _paise(_d(pos.entry_price) * pos.quantity)
    if not pos.selling:
        return _paise(sum((_d(x.price) * pos.quantity * x.lot_size for x in pos.legs), Decimal(0)))
    return None if pos.margin_inr is None else _paise(pos.margin_inr)


def kind_of_trade_type(trade_type: str) -> str:
    from .risk.portfolio import CASH, INDEX_SELLING_SPREAD, OPTION_BUY, OTHER, STOCK_SPREAD
    return {"INTRADAY_STOCKS": CASH, "SWING_STOCKS": CASH, "INDEX_OPTION_BUYING": OPTION_BUY,
            "STOCK_OPTION_BUYING": OPTION_BUY, "INDEX_OPTION_SELLING": INDEX_SELLING_SPREAD,
            "STOCK_OPTION_SELLING": STOCK_SPREAD}.get(trade_type, OTHER)


def position_kind(pos: Position) -> str:
    return kind_of_trade_type(pos.trade_type)


# ---------------- manual position input ----------------
def parse_manual(payload: dict, now: datetime, max_note: int = 200) -> Position:
    """A position I entered by hand (POST /journal/positions). Cash: symbol, trade_type (INTRADAY_STOCKS or
    SWING_STOCKS), direction (LONG/SHORT), quantity, entry_price, entry_date, optional stop and target.
    Options: symbol, kind ("index" or "stock"), legs [{action, right, strike, expiry, lots, lot_size, price}],
    entry_date, optional stop / target (option premium, or the cost per share to buy a spread back),
    underlying_stop, underlying_target, invalidation_level (+ invalidation_side), margin_inr.
    A sold leg without a bought leg of the same right and expiry is stored and flagged UNHEDGED."""
    errors: list[str] = []
    sym = payload.get("symbol")
    if not isinstance(sym, str) or not sym.strip() or len(sym) > 30:
        errors.append("symbol must be text")
        sym = ""
    sym = sym.strip().upper()
    try:
        entry_day = date.fromisoformat(str(payload.get("entry_date", "")))
        if entry_day > ist_day(now):
            errors.append("entry_date is in the future")
    except ValueError:
        errors.append("entry_date must be YYYY-MM-DD")
        entry_day = ist_day(now)
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > max_note:
        errors.append(f"note must be text of at most {max_note} characters")
        note = ""

    def opt_price(name):
        v = payload.get(name)
        if v is None:
            return None
        f = _num(v)
        if f is None or f <= 0:
            errors.append(f"{name} must be a positive number")
            return None
        return f

    stop, target = opt_price("stop"), opt_price("target")
    u_stop, u_target, inv = opt_price("underlying_stop"), opt_price("underlying_target"), opt_price("invalidation_level")
    margin = opt_price("margin_inr")
    entry_utc = datetime.combine(entry_day, datetime.min.time(), IST).replace(hour=9, minute=15).astimezone(timezone.utc)
    if entry_day == ist_day(now):
        entry_utc = now
    legs_in = payload.get("legs")
    if legs_in is None:
        tt = payload.get("trade_type")
        if tt not in ("INTRADAY_STOCKS", "SWING_STOCKS"):
            errors.append("trade_type must be INTRADAY_STOCKS or SWING_STOCKS for a cash position (or give legs)")
        direction = payload.get("direction")
        if direction not in ("LONG", "SHORT"):
            errors.append("direction must be LONG or SHORT")
        q = payload.get("quantity")
        if isinstance(q, bool) or not isinstance(q, int) or q < 1:
            errors.append("quantity must be a whole number of shares, at least 1")
        entry = opt_price("entry_price")
        if entry is None and "entry_price" not in payload:
            errors.append("entry_price is needed")
        if direction == "SHORT" and tt == "SWING_STOCKS":
            errors.append("a swing (delivery) position cannot be short")
        if not errors and stop is not None and target is not None:
            if direction == "LONG" and not (stop < entry < target):
                errors.append("LONG needs stop < entry_price < target")
            if direction == "SHORT" and not (target < entry < stop):
                errors.append("SHORT needs target < entry_price < stop")
        if errors:
            raise JournalError("; ".join(errors))
        pid = str(uuid.uuid4())
        return Position(pid, "MANUAL", None, tt, CASH_INDEX, sym, direction, None, q, entry, entry_utc, stop=stop,
                        target=target, initial_stop=stop,
                        invalidation=None if inv is None else {"level": inv, "side": _side(payload, direction == "LONG", errors),
                                                               "why": "level I entered"},
                        horizon="INTRADAY" if tt == "INTRADAY_STOCKS" else "SWING", opened_utc=now, note=note)
    kind = payload.get("kind")
    if kind not in ("index", "stock"):
        errors.append('kind must be "index" or "stock" for an option position')
    if not isinstance(legs_in, list) or not 1 <= len(legs_in) <= 4:
        raise JournalError("; ".join(errors + ["legs must be a list of 1 to 4 option legs"]))
    legs: list[Leg] = []
    for i, x in enumerate(legs_in):
        if not isinstance(x, dict):
            errors.append(f"leg {i + 1} must be an object")
            continue
        try:
            if x.get("action") not in ("BUY", "SELL") or x.get("right") not in ("CE", "PE"):
                raise ValueError("action must be BUY or SELL and right CE or PE")
            ints = [x.get("lots"), x.get("lot_size")]
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in ints):
                raise ValueError("lots and lot_size must be whole numbers, at least 1")
            nums = [_num(x.get("strike")), _num(x.get("price"))]
            if nums[0] is None or nums[0] <= 0 or nums[1] is None or nums[1] < 0:
                raise ValueError("strike must be positive and price zero or more")
            legs.append(Leg(x["action"], x["right"], nums[0], date.fromisoformat(str(x.get("expiry", ""))),
                            x["lots"], x["lot_size"], nums[1]))
        except ValueError as e:
            errors.append(f"leg {i + 1}: {e}")
    if legs and len({x.lots for x in legs}) != 1:
        errors.append("every leg must hold the same number of lots (enter different sizes as separate positions)")
    if legs and len({x.lot_size for x in legs}) != 1:
        errors.append("every leg must have the same lot size")
    if errors:
        raise JournalError("; ".join(errors))
    asset = "INDEX_OPTION" if kind == "index" else "STOCK_OPTION"
    tt = trade_type_of(asset, None, legs)
    hedged = not unhedged_short_legs([x.to_dict() for x in legs])
    lots = legs[0].lots
    net = sum((_d(x.price) * -x.sign for x in legs), Decimal(0))   # credit received (+) or debit paid (-) per share
    if all(x.action == "BUY" for x in legs):
        direction, entry = "LONG", float(-net)
    else:
        direction, entry = "SPREAD", float(net)
    if direction == "SPREAD" and entry <= 0 and len(legs) > 1:
        entry = float(net)                                       # a debit spread: entry is the net debit (negative)
    view_pos = Position("x", "MANUAL", None, tt, pool_of_trade_type(tt), sym, direction, tuple(legs), lots,
                        entry, entry_utc)
    side = _side(payload, view_pos.view != "BEARISH", errors)
    if errors:
        raise JournalError("; ".join(errors))
    return replace(view_pos, position_id=str(uuid.uuid4()), stop=stop, target=target, initial_stop=stop,
                   underlying_stop=u_stop, underlying_target=u_target,
                   invalidation=None if inv is None else {"level": inv, "side": side, "why": "level I entered"},
                   margin_inr=margin, hedged=hedged, opened_utc=now, note=note,
                   horizon="MONTHLY" if tt == "STOCK_OPTION_SELLING" else None)


def _side(payload, bullish: bool, errors) -> str:
    s = payload.get("invalidation_side")
    if s is None:
        return "below" if bullish else "above"
    if s not in ("below", "above"):
        errors.append('invalidation_side must be "below" or "above"')
        return "below"
    return s


# ---------------- the journal ----------------
class Journal:
    """Persistence for phase 6 on the app's Store (same SQLite file, connection and lock)."""

    def __init__(self, store, settings):
        self.store, self.s = store, settings
        self._db, self._lock = store._db, store._lock
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ---- engine state (restart bookkeeping) ----
    def get_state(self, key: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_state(self, key: str, value: str, now: datetime) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO engine_state VALUES (?,?,?)", (key, value, now.isoformat()))
            self._db.commit()

    # ---- signals ----
    def save_signal(self, signal: Signal, decision, now: datetime, score: float | None = None,
                    card: dict | None = None, paper: dict | None = None) -> bool:
        """Store a signal and its risk decision, and with `paper` its pending paper trade, in ONE transaction (a
        crash never leaves an approved signal without its paper trade). Signals are never edited after creation:
        a second save of the same signal_id (a restart, the same bar again) is ignored, its paper row too. True
        when stored now."""
        tt = signal_trade_type(signal)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (signal.signal_id, signal.created_at.isoformat(), signal.strategy_id, signal.strategy_version,
                 signal.symbol, tt, pool_of_trade_type(tt), decision.status, signal.valid_until.isoformat(), score,
                 json.dumps(signal.to_dict(), sort_keys=True, default=str),
                 json.dumps(decision.to_dict(), sort_keys=True, default=str),
                 None if card is None else json.dumps(card, sort_keys=True, default=str), now.isoformat()))
            stored = cur.rowcount == 1
            try:
                if stored and paper is not None:
                    self._upsert_paper(paper)
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
            return stored

    def has_signal(self, signal_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone() is not None

    _SIG_KEYS = ["signal_id", "created_utc", "strategy_id", "strategy_version", "symbol", "trade_type", "pool", "status",
                 "valid_until_utc", "score", "signal_json", "decision_json", "card_json", "stored_utc"]

    def _sig_row(self, r) -> dict:
        d = dict(zip(self._SIG_KEYS, r))
        d["signal"] = json.loads(d.pop("signal_json"))
        d["decision"] = json.loads(d.pop("decision_json"))
        c = d.pop("card_json")
        d["card"] = json.loads(c) if c else None
        return d

    def signal(self, signal_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(f"SELECT {','.join(self._SIG_KEYS)} FROM signals WHERE signal_id=?",
                                 (signal_id,)).fetchone()
        return self._sig_row(r) if r else None

    def signals_since(self, since: datetime, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._db.execute(f"SELECT {','.join(self._SIG_KEYS)} FROM signals WHERE created_utc >= ? "
                                    "ORDER BY created_utc DESC, signal_id LIMIT ?", (since.isoformat(), limit)).fetchall()
        return [self._sig_row(r) for r in rows]

    def unexpired_signals(self, now: datetime) -> list[dict]:
        with self._lock:
            rows = self._db.execute(f"SELECT {','.join(self._SIG_KEYS)} FROM signals WHERE valid_until_utc > ? "
                                    "ORDER BY created_utc", (now.isoformat(),)).fetchall()
        return [self._sig_row(r) for r in rows]

    def set_card(self, signal_id: str, card: dict) -> None:
        """The card is the signal's display; it may be rebuilt (expiry, journal state). The signal and its
        decision never change."""
        with self._lock:
            self._db.execute("UPDATE signals SET card_json=? WHERE signal_id=?",
                             (json.dumps(card, sort_keys=True, default=str), signal_id))
            self._db.commit()

    # ---- decisions (I took this trade / I skipped it) ----
    def decision(self, signal_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT signal_id,decision,at_utc,note,fill_price,fill_time_utc,fills_json,position_id "
                                 "FROM journal WHERE signal_id=?", (signal_id,)).fetchone()
        if not r:
            return None
        d = dict(zip(["signal_id", "decision", "at_utc", "note", "fill_price", "fill_time_utc", "fills", "position_id"], r))
        d["fills"] = json.loads(d["fills"]) if d["fills"] else None
        return d

    def decisions(self) -> dict[str, dict]:
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT signal_id FROM journal").fetchall()]
        return {i: self.decision(i) for i in ids}

    def record_decision(self, signal_id: str, payload: dict, now: datetime) -> dict:
        """TOOK or SKIPPED, with an optional note and my fill price(s). TOOK opens a position (origin SIGNAL)
        that gets exit calls; it is refused after the signal's valid-until and for a NO_TRADE signal. One
        decision per signal. Journal only: nothing is sent anywhere."""
        row = self.signal(signal_id)
        if row is None:
            raise JournalError("unknown signal_id", 404)
        if self.decision(signal_id) is not None:
            raise JournalError("a decision is already recorded for this signal", 409)
        decision = payload.get("decision")
        if decision not in (TOOK, SKIPPED):
            raise JournalError("decision must be TOOK or SKIPPED")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 200:
            raise JournalError("note must be text of at most 200 characters")
        sig = row["signal"]
        legs = sig.get("legs") or []
        fill_time = now
        if payload.get("fill_time") is not None:
            try:
                fill_time = datetime.fromisoformat(str(payload["fill_time"]))
                if fill_time.tzinfo is None:
                    fill_time = fill_time.replace(tzinfo=IST)
                fill_time = fill_time.astimezone(timezone.utc)
            except ValueError:
                raise JournalError("fill_time must be an ISO date-time")
            if fill_time > now + timedelta(minutes=1):
                raise JournalError("fill_time is in the future")
        position = None
        fills = None
        fill_price = None
        if decision == TOOK:
            if now >= datetime.fromisoformat(row["valid_until_utc"]):
                raise JournalError("the signal expired at its valid-until time and cannot be acted on", 409)
            if row["status"] != "APPROVED":
                raise JournalError("a NO_TRADE signal cannot be taken", 409)
            fills, fill_price = self._fills(payload, legs)
            position = self._position_from_signal(row, payload, fills, fill_price, fill_time, now)
        with self._lock:
            self._db.execute("INSERT INTO journal VALUES (?,?,?,?,?,?,?,?)",
                             (signal_id, decision, now.isoformat(), note, fill_price,
                              fill_time.isoformat() if decision == TOOK else None,
                              json.dumps(fills) if fills else None, position.position_id if position else None))
            if position is not None:
                self._insert_position(position)
            self._db.commit()
        return {"ok": True, "decision": decision, "position_id": position.position_id if position else None,
                "fill_estimated": position.fill_estimated if position else None}

    @staticmethod
    def _fills(payload: dict, legs: list) -> tuple[list | None, float | None]:
        """My actual fill price(s): one `fill_price` (cash or a single leg), or `fills` = [price per leg]."""
        fp = payload.get("fill_price")
        fl = payload.get("fills")
        if fl is not None:
            if not isinstance(fl, list) or len(fl) != max(len(legs), 1) or any(_num(x) is None or x < 0 for x in fl):
                raise JournalError(f"fills must list one price per leg ({max(len(legs), 1)})")
            return [float(x) for x in fl], None if legs else float(fl[0])
        if fp is not None:
            if _num(fp) is None or fp <= 0:
                raise JournalError("fill_price must be a positive number")
            if len(legs) > 1:
                raise JournalError("a multi-leg signal needs fills: one price per leg")
            return [float(fp)], float(fp)
        return None, None

    def _position_from_signal(self, row: dict, payload: dict, fills, fill_price, fill_time, now) -> Position:
        sig, dec = row["signal"], row["decision"]
        legs_in = sig.get("legs") or []
        approved = dec.get("lots") if legs_in else dec.get("quantity")
        units = payload.get("quantity", payload.get("lots", approved))
        if isinstance(units, bool) or not isinstance(units, int) or units < 1:
            raise JournalError("quantity (shares) or lots must be a whole number, at least 1")
        est = fills is None
        details = sig.get("details") or {}
        if legs_in:
            prices = fills or [x["price"] for x in legs_in]
            legs = tuple(Leg(x["action"], x["right"], float(x["strike"]), date.fromisoformat(x["expiry"][:10]), units,
                             int(x["lot_size"]), float(p)) for x, p in zip(legs_in, prices))
            net = sum((_d(x.price) * -x.sign for x in legs), Decimal(0))
            direction = sig["direction"]
            entry = float(-net) if direction == "LONG" else float(net)
            margin = dec.get("margin_inr")
            per_lot = details.get("margin_per_lot_inr")
            if _num(per_lot) is not None:
                margin = float(_paise(_d(per_lot) * units))
            elif direction == "LONG":
                margin = None
        else:
            legs, direction = None, sig["direction"]
            entry = fills[0] if fills else float(sig["entry"])
            margin = None
        inv = invalidation_of(sig)
        tt = row["trade_type"]
        return Position(str(uuid.uuid5(_NS, "signal|" + row["signal_id"])), "SIGNAL", row["signal_id"], tt, row["pool"],
                        sig["symbol"], direction, legs, units, entry, fill_time, stop=sig["stop"], target=sig["target"],
                        initial_stop=sig["stop"], underlying_stop=details.get("underlying_stop"),
                        underlying_target=details.get("underlying_target"), invalidation=inv, margin_inr=margin,
                        strategy_id=sig["strategy_id"], horizon=sig["horizon"], regime_at_entry=sig["regime"],
                        hedged=not legs or not unhedged_short_legs([x.to_dict() for x in legs]), opened_utc=now,
                        note=str(payload.get("note", "")), fill_estimated=est)

    # ---- positions ----
    def add_manual_position(self, payload: dict, now: datetime) -> Position:
        pos = parse_manual(payload, now)
        with self._lock:
            self._insert_position(pos)
            self._db.commit()
        return pos

    _POS_COLS = ["position_id", "origin", "signal_id", "trade_type", "pool", "symbol", "direction", "legs_json",
                 "quantity", "entry_price", "entry_utc", "stop", "target", "initial_stop", "underlying_stop",
                 "underlying_target", "invalidation_json", "margin_inr", "strategy_id", "horizon", "regime_at_entry",
                 "hedged", "status", "opened_utc", "closed_utc", "plan_json", "note", "fill_estimated"]

    def _insert_position(self, p: Position) -> None:
        self._db.execute(f"INSERT INTO positions ({','.join(self._POS_COLS)}) VALUES ({','.join('?' * len(self._POS_COLS))})",
                         (p.position_id, p.origin, p.signal_id, p.trade_type, p.pool, p.symbol, p.direction,
                          json.dumps([x.to_dict() for x in p.legs]) if p.legs else None, p.quantity, p.entry_price,
                          p.entry_utc.isoformat(), p.stop, p.target, p.initial_stop, p.underlying_stop,
                          p.underlying_target, json.dumps(p.invalidation) if p.invalidation else None, p.margin_inr,
                          p.strategy_id, p.horizon, p.regime_at_entry, int(p.hedged), p.status,
                          (p.opened_utc or p.entry_utc).isoformat(), None, json.dumps(p.plan, sort_keys=True), p.note,
                          int(p.fill_estimated)))

    def _pos(self, r) -> Position:
        d = dict(zip(self._POS_COLS, r))
        return Position(d["position_id"], d["origin"], d["signal_id"], d["trade_type"], d["pool"], d["symbol"],
                        d["direction"], tuple(Leg.from_dict(x) for x in json.loads(d["legs_json"])) if d["legs_json"] else None,
                        d["quantity"], d["entry_price"], datetime.fromisoformat(d["entry_utc"]), d["stop"], d["target"],
                        d["initial_stop"], d["underlying_stop"], d["underlying_target"],
                        json.loads(d["invalidation_json"]) if d["invalidation_json"] else None, d["margin_inr"],
                        d["strategy_id"], d["horizon"], d["regime_at_entry"], bool(d["hedged"]), d["status"],
                        datetime.fromisoformat(d["opened_utc"]),
                        datetime.fromisoformat(d["closed_utc"]) if d["closed_utc"] else None,
                        json.loads(d["plan_json"]), d["note"] or "", bool(d["fill_estimated"]))

    def position(self, position_id: str) -> Position | None:
        with self._lock:
            r = self._db.execute(f"SELECT {','.join(self._POS_COLS)} FROM positions WHERE position_id=?",
                                 (position_id,)).fetchone()
        return self._pos(r) if r else None

    def positions(self, status: str | None = "OPEN", pool: str | None = None) -> list[Position]:
        q, args = f"SELECT {','.join(self._POS_COLS)} FROM positions WHERE 1=1", []
        if status:
            q, args = q + " AND status=?", args + [status]
        if pool:
            q, args = q + " AND pool=?", args + [pool]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY opened_utc, position_id", args).fetchall()
        return [self._pos(r) for r in rows]

    def update_plan(self, position_id: str, plan: dict) -> None:
        with self._lock:
            self._db.execute("UPDATE positions SET plan_json=? WHERE position_id=?",
                             (json.dumps(plan, sort_keys=True), position_id))
            self._db.commit()

    def set_stop(self, position_id: str, payload: dict, now: datetime) -> Position:
        """I moved my stop (after a breakeven or trailing call): journal only."""
        pos = self.position(position_id)
        if pos is None:
            raise JournalError("unknown position_id", 404)
        if pos.status != "OPEN":
            raise JournalError("the position is closed", 409)
        stop = _num(payload.get("stop"))
        if stop is None or stop <= 0:
            raise JournalError("stop must be a positive number")
        plan = {**pos.plan, "stop_confirmed_utc": now.isoformat(), "plan_stop": stop}
        with self._lock:
            self._db.execute("UPDATE positions SET stop=?, plan_json=? WHERE position_id=?",
                             (stop, json.dumps(plan, sort_keys=True), position_id))
            self._db.commit()
        return self.position(position_id)

    def close_position(self, position_id: str, payload: dict, now: datetime, costs) -> dict:
        """I closed all or part of a position (POST /journal/positions/{id}/close): exit price (cash, or a
        single leg) or `exit_prices` (one per leg), optional `quantity` / `lots` for a partial close, note.
        The realised P&L is computed by code with estimated charges. Journal only."""
        pos = self.position(position_id)
        if pos is None:
            raise JournalError("unknown position_id", 404)
        if pos.status != "OPEN":
            raise JournalError("the position is already closed", 409)
        n_legs = len(pos.legs) if pos.legs else 1
        prices = payload.get("exit_prices")
        if prices is None and payload.get("exit_price") is not None:
            prices = [payload.get("exit_price")]
        if (not isinstance(prices, list) or len(prices) != n_legs
                or any(_num(x) is None or x < 0 or (not pos.is_option and x <= 0) for x in prices)):
            raise JournalError(f"exit_price (or exit_prices: one per leg, {n_legs}) is needed")
        units = payload.get("quantity", payload.get("lots", pos.quantity))
        if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= pos.quantity:
            raise JournalError(f"quantity / lots must be a whole number from 1 to {pos.quantity}")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 200:
            raise JournalError("note must be text of at most 200 characters")
        at = now
        if payload.get("exit_time") is not None:
            try:
                at = datetime.fromisoformat(str(payload["exit_time"]))
                at = (at if at.tzinfo else at.replace(tzinfo=IST)).astimezone(timezone.utc)
            except ValueError:
                raise JournalError("exit_time must be an ISO date-time")
            if at > now + timedelta(minutes=1) or at < pos.entry_utc:
                raise JournalError("exit_time must be after the entry and not in the future")
        gross, charges, net = pnl(pos, [float(x) for x in prices], units, costs)
        left = pos.quantity - units
        with self._lock:
            self._db.execute("INSERT INTO position_closes (position_id,at_utc,units,exit_prices_json,gross_inr,costs_inr,"
                             "net_inr,note) VALUES (?,?,?,?,?,?,?,?)",
                             (position_id, at.isoformat(), units, json.dumps([float(x) for x in prices]), float(gross),
                              None if charges is None else float(charges), None if net is None else float(net), note))
            if left == 0:
                self._db.execute("UPDATE positions SET status='CLOSED', closed_utc=? WHERE position_id=?",
                                 (at.isoformat(), position_id))
            else:
                legs = json.dumps([x.to_dict() for x in scale_legs(pos.legs, left)]) if pos.legs else None
                plan = {**pos.plan, "partial_booked": True}
                self._db.execute("UPDATE positions SET quantity=?, legs_json=?, plan_json=? WHERE position_id=?",
                                 (left, legs, json.dumps(plan, sort_keys=True), position_id))
            self._db.commit()
        return {"ok": True, "position_id": position_id, "closed_units": units, "remaining": left,
                "status": "CLOSED" if left == 0 else "OPEN", "gross_inr": float(gross),
                "estimated_charges_inr": None if charges is None else float(charges),
                "net_inr": None if net is None else float(net),
                "note": "charges are the cost model's estimate; phase 7 compares them with the contract note"}

    def closes(self, position_id: str | None = None) -> list[dict]:
        q = "SELECT id,position_id,at_utc,units,exit_prices_json,gross_inr,costs_inr,net_inr,note FROM position_closes"
        with self._lock:
            rows = self._db.execute(q + (" WHERE position_id=?" if position_id else "") + " ORDER BY at_utc, id",
                                    (position_id,) if position_id else ()).fetchall()
        keys = ["id", "position_id", "at_utc", "units", "exit_prices", "gross_inr", "costs_inr", "net_inr", "note"]
        out = [dict(zip(keys, r)) for r in rows]
        for d in out:
            d["exit_prices"] = json.loads(d["exit_prices"])
        return out

    # ---- paper trades (rows written by app/paper.py) ----
    _PAPER_COLS = ["signal_id", "trade_type", "pool", "symbol", "direction", "legs_json", "quantity", "status",
                   "planned_fill_utc", "entry_utc", "entry_price", "entry_prices_json", "entry_source", "stop", "target",
                   "time_exit_utc", "risk_inr", "margin_inr", "exit_utc", "exit_price", "exit_prices_json", "exit_reason",
                   "gross_inr", "costs_inr", "net_pnl_inr", "note", "horizon", "strategy_id"]

    def upsert_paper(self, row: dict) -> None:
        with self._lock:
            self._upsert_paper(row)
            self._db.commit()

    def _upsert_paper(self, row: dict) -> None:
        vals = [row.get(k) for k in self._PAPER_COLS]
        self._db.execute(f"INSERT OR REPLACE INTO paper_trades ({','.join(self._PAPER_COLS)}) VALUES "
                         f"({','.join('?' * len(self._PAPER_COLS))})", vals)

    def paper_trades(self, status: str | None = None, pool: str | None = None) -> list[dict]:
        q, args = f"SELECT {','.join(self._PAPER_COLS)} FROM paper_trades WHERE 1=1", []
        if status:
            q, args = q + " AND status=?", args + [status]
        if pool:
            q, args = q + " AND pool=?", args + [pool]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY planned_fill_utc, signal_id", args).fetchall()
        return [dict(zip(self._PAPER_COLS, r)) for r in rows]

    def paper_trade(self, signal_id: str) -> dict | None:
        rows = [r for r in self.paper_trades() if r["signal_id"] == signal_id]
        return rows[0] if rows else None

    @staticmethod
    def paper_position(row: dict) -> Position:
        """A paper trade seen as a position (for open P&L and the account state)."""
        legs = tuple(Leg.from_dict(x) for x in json.loads(row["legs_json"])) if row["legs_json"] else None
        if legs and row.get("entry_prices_json"):
            legs = tuple(replace(x, price=p) for x, p in zip(legs, json.loads(row["entry_prices_json"])))
        return Position(f"paper:{row['signal_id']}", "PAPER", row["signal_id"], row["trade_type"], row["pool"],
                        row["symbol"], row["direction"], legs, row["quantity"], row["entry_price"],
                        datetime.fromisoformat(row["entry_utc"]), stop=row["stop"], target=row["target"],
                        margin_inr=row["margin_inr"], strategy_id=row["strategy_id"], horizon=row["horizon"])

    # ---- marks (open P&L recorded during the day, so a later day knows the previous close's figure) ----
    def save_mark(self, ref: str, now: datetime, open_pnl: float) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO position_marks VALUES (?,?,?,?)",
                             (ref, ist_day(now).isoformat(), now.isoformat(), float(open_pnl)))
            self._db.commit()

    def mark_on(self, ref: str, day: date) -> float | None:
        """The last open P&L per unit recorded on `day` (the day's close figure once the session ended)."""
        with self._lock:
            r = self._db.execute("SELECT open_pnl_inr FROM position_marks WHERE ref=? AND day=?",
                                 (ref, day.isoformat())).fetchone()
        return r[0] if r else None

    def last_mark_before(self, ref: str, day: date) -> tuple[date, float, str] | None:
        with self._lock:
            r = self._db.execute("SELECT day, open_pnl_inr, at_utc FROM position_marks WHERE ref=? AND day < ? "
                                 "ORDER BY day DESC LIMIT 1", (ref, day.isoformat())).fetchone()
        return (date.fromisoformat(r[0]), r[1], r[2]) if r else None

    # ---- switches ----
    def switches(self) -> dict[str, dict]:
        with self._lock:
            rows = dict((r[0], (bool(r[1]), r[2])) for r in
                        self._db.execute("SELECT trade_type, is_on, changed_utc FROM alert_switches").fetchall())
        return {t: {"on": rows.get(t, (False, None))[0], "changed_utc": rows.get(t, (False, None))[1]} for t in TRADE_TYPES}

    def set_switch(self, trade_type: str, on: bool, now: datetime) -> dict:
        if trade_type not in TRADE_TYPES:
            raise JournalError(f"type must be one of {list(TRADE_TYPES)}")
        if not isinstance(on, bool):
            raise JournalError("on must be true or false")
        old = self.switches()[trade_type]["on"]
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO alert_switches VALUES (?,?,?)", (trade_type, int(on), now.isoformat()))
            self._db.execute("INSERT INTO alert_switch_log (trade_type, old, new, at_utc) VALUES (?,?,?,?)",
                             (trade_type, int(old), int(on), now.isoformat()))
            self._db.commit()
        return {"type": trade_type, "old": old, "new": on, "changed_utc": now.isoformat()}

    def switch_log(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT trade_type, old, new, at_utc FROM alert_switch_log ORDER BY id DESC LIMIT ?",
                                    (limit,)).fetchall()
        return [{"type": r[0], "old": bool(r[1]), "new": bool(r[2]), "at_utc": r[3]} for r in rows]

    # ---- equity history per pool ----
    def equity_rows(self, pool: str) -> list[tuple[date, float]]:
        with self._lock:
            rows = self._db.execute("SELECT day, equity_inr FROM equity_history WHERE pool=? ORDER BY day",
                                    (pool,)).fetchall()
        return [(date.fromisoformat(d), e) for d, e in rows]

    def write_equity(self, pool: str, day: date, equity: float, realised_cum: float, open_pnl: float, now: datetime,
                     note: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO equity_history VALUES (?,?,?,?,?,?,?)",
                             (pool, day.isoformat(), float(equity), float(realised_cum), float(open_pnl),
                              now.isoformat(), note))
            self._db.commit()

    def ensure_equity_start(self, pool: str, now: datetime, capital: float) -> None:
        """The first equity point of a pool: its capital at the close before the journal started (paper mode
        starts at the configured capital). Written once."""
        if not self.equity_rows(pool):
            self.write_equity(pool, ist_day(now) - timedelta(days=1), capital, 0.0, 0.0, now,
                              "start: the pool's capital before the journal began")

    # ---- realised P&L per pool ----
    def realised(self, pool: str, since: date | None = None, until: date | None = None) -> Decimal | None:
        """Realised P&L of a pool in my real book (my closed positions only; paper trades run in the paper book), after
        estimated charges, for closes dated (IST) from `since` to `until` inclusive. None when a figure is not known.

        Same basis as the open P&L (`pool_account`): with `since`, a close of a position opened before `since` counts
        only its change from the last closing mark before `since` (net of the close less that mark per unit x units
        closed), so a position that gained up to yesterday's close and lost today shows today's loss whether it is
        still open or closed. No such mark: not known (None), as for the open P&L. Without `since`: since entry."""
        total = Decimal(0)

        def amount(ref: str, entry_utc: datetime, net, units: int) -> Decimal | None:
            if net is None:
                return None
            if since is None or ist_day(entry_utc) >= since:
                return _d(net)
            m = self.last_mark_before(ref, since)
            if m is None:
                return None
            return _paise(_d(net) - _d(m[1]) * units)

        pos_by_id = {p.position_id: p for p in self.positions(status=None)}
        for c in self.closes():
            p = pos_by_id.get(c["position_id"])
            if p is None or p.pool != pool:
                continue
            day = ist_day(datetime.fromisoformat(c["at_utc"]))
            if (since and day < since) or (until and day > until):
                continue
            v = amount(p.position_id, p.entry_utc, c["net_inr"], c["units"])
            if v is None:
                return None
            total += v
        return _paise(total)

    def held_positions(self, pool: str) -> list[Position]:
        """My open positions in a pool (the real book). Paper trades are never counted here: they run in the paper
        book (`paper_book`)."""
        return list(self.positions("OPEN", pool))

    def pending_reservations(self, pool: str, now: datetime) -> list[dict]:
        """The real book's reservations: approved signals of the pool still within their valid-until on which I have
        recorded no decision. Each reserves its approved risk and margin, a sector slot and a place in its correlated
        group until I take it (it is then my position), skip it, or it expires - so several approvals before I act
        cannot together exceed the limits. Their paper trades do not matter here (the paper book)."""
        decided = set(self.decisions())
        out = []
        for r in self.unexpired_signals(now):
            if r["pool"] != pool or r["status"] != "APPROVED" or r["signal_id"] in decided:
                continue
            d = r["decision"] or {}
            out.append({"signal_id": r["signal_id"], "symbol": r["symbol"], "trade_type": r["trade_type"],
                        "risk_inr": d.get("risk_inr"), "margin_inr": d.get("margin_inr")})
        return out

    # ---- the paper book (simulated; its own capital and limits) ----
    def paper_open(self, pool: str, now: datetime, strategy_id: str | None = None) -> list[dict]:
        """Paper trades that occupy a paper book (one strategy's when `strategy_id` is given): OPEN ones, and PENDING
        ones within their valid-until."""
        out = []
        for t in self.paper_trades(pool=pool):
            if strategy_id is not None and t["strategy_id"] != strategy_id:
                continue
            if t["status"] == "OPEN":
                out.append(t)
            elif t["status"] == "PENDING":
                try:
                    vu = datetime.fromisoformat(json.loads(t["note"] or "{}")["valid_until_utc"])
                except (KeyError, ValueError, TypeError):
                    vu = None                           # not known: it keeps its place
                if vu is None or now < vu:
                    out.append(t)
        return out


def invalidation_of(sig: dict) -> dict | None:
    """The level whose loss says the entry reason no longer holds, on the underlying: the entry zone's low
    (long) or high (short) when the strategy gave a zone, else the signal bar's close (the level the entry
    needed); for option signals the details' `invalidation` when the strategy recorded one (the sold strike
    of a credit spread)."""
    det = sig.get("details") or {}
    if isinstance(det.get("invalidation"), dict):
        return det["invalidation"]
    if sig.get("asset_class") != "CASH_EQUITY":
        legs = sig.get("legs") or []
        sold = [x for x in legs if x.get("action") == "SELL"]
        if len(sold) == 1:
            k = float(sold[0]["strike"])
            return {"level": k, "side": "below" if sold[0]["right"] == "PE" else "above",
                    "why": f"the sold {k:g} {sold[0]['right']} strike"}
        return None
    zone = sig.get("entry_zone")
    if sig.get("direction") == "LONG":
        return {"level": float(zone[0]) if zone else float(sig["entry"]), "side": "below",
                "why": "back below the entry zone" if zone else "back below the signal bar's close"}
    return {"level": float(zone[1]) if zone else float(sig["entry"]), "side": "above",
            "why": "back above the entry zone" if zone else "back above the signal bar's close"}


# ---------------- the account state of one pool, for the risk engine ----------------
def open_pnl(pos: Position, prices, costs) -> Decimal | None:
    """Open P&L of the whole position if closed at `prices` now, after the estimated round-trip charges."""
    if prices is None:
        return None
    try:
        _, _, net = pnl(pos, prices, pos.quantity, costs)
    except (ValueError, ArithmeticError):
        return None
    return net


def pool_account(journal: Journal, settings, pool: str, now: datetime, marks, costs, flows=()):
    """The risk engine's AccountState for one pool at `now`, and a facts dict saying what is not known.

    `marks(pos, now) -> (prices or None, why)` (app.pricing.Marker.marks). Open P&L today = open P&L now
    less the last mark recorded on an earlier day (since entry when opened today); this month: less the
    last mark before the month began (since entry when opened this month). A position whose figure cannot
    be computed is passed with None, so the engine says PORTFOLIO_UNKNOWN with the position named.
    Equity = pool capital + net deposits since the first equity point (`net_flows`; CASH_INDEX only) + realised
    to date + open P&L now; earlier closes from `equity_history`. Approved paper trades not filled yet are passed
    as positions with their approved risk and margin and no P&L (`Journal.pending_reservations`). This is my REAL
    book only: paper trades (open, pending or closed) are never counted here; they run in `paper_book`."""
    from .risk.portfolio import AccountState, OpenPosition
    cap = pool_capital(settings, pool)
    today = ist_day(now)
    month_start = today.replace(day=1)
    held = journal.held_positions(pool)
    rows, unknown, open_now_total = [], [], Decimal(0)
    for p in held:
        prices, why = marks(p, now)
        now_pnl = open_pnl(p, prices, costs)
        units = p.quantity

        def since(day_from: date) -> Decimal | None:
            if now_pnl is None:
                return None
            if ist_day(p.entry_utc) >= day_from:
                return now_pnl
            m = journal.last_mark_before(p.position_id, day_from)
            if m is None:
                return None
            return now_pnl - _paise(_d(m[1]) * units)

        today_pnl, month_pnl = since(today), since(month_start)
        risk, committed = max_loss_inr(p, costs), committed_inr(p)
        if now_pnl is None:
            unknown.append(f"{p.position_id} ({p.symbol}): open P&L not known - {why or 'charges unknown'}")
        elif today_pnl is None or month_pnl is None:
            unknown.append(f"{p.position_id} ({p.symbol}): no mark recorded at an earlier close")
        if risk is None:
            unknown.append(f"{p.position_id} ({p.symbol}): maximum loss not known (no stop, or no limit)")
        if committed is None:
            unknown.append(f"{p.position_id} ({p.symbol}): margin blocked not entered")
        open_now_total = None if (open_now_total is None or now_pnl is None) else open_now_total + now_pnl
        rows.append(OpenPosition(p.position_id, p.symbol, position_kind(p), None if risk is None else float(risk),
                                 None if committed is None else float(committed),
                                 None if today_pnl is None else float(today_pnl),
                                 None if month_pnl is None else float(month_pnl), origin=p.origin))
    # approved signals awaiting my decision reserve their approved risk and margin (no P&L yet): sector,
    # correlated-group, capital-pool and selling-spread limits count them until I take or skip them, or they expire
    pending = journal.pending_reservations(pool, now)
    for t in pending:
        risk, margin = _num(t["risk_inr"]), _num(t["margin_inr"])
        if risk is None:
            unknown.append(f"pending {t['signal_id']} ({t['symbol']}): approved risk not known")
        if margin is None:
            unknown.append(f"pending {t['signal_id']} ({t['symbol']}): approved margin not known")
        rows.append(OpenPosition(f"pending:{t['signal_id']}", t["symbol"], kind_of_trade_type(t["trade_type"]), risk,
                                 margin, 0.0, 0.0, origin="PENDING"))
    r_today = journal.realised(pool, since=today)
    r_month = journal.realised(pool, since=month_start)
    r_all = journal.realised(pool)
    stored = [(d, e) for d, e in journal.equity_rows(pool) if d < today]
    curve = None
    if r_all is not None and open_now_total is not None and stored:
        deposits = net_flows(journal, pool, flows, today)
        curve = tuple(stored) + ((today, float(_paise(_d(cap) + deposits + r_all + open_now_total))),)
    state = AccountState(capital_inr=cap, positions=tuple(rows), realised_today_inr=None if r_today is None else float(r_today),
                         realised_month_inr=None if r_month is None else float(r_month), equity_curve=curve,
                         flows=tuple(flows) if pool == CASH_INDEX else ())
    facts = {"pool": pool, "capital_inr": cap, "open_positions": len(rows) - len(pending),
             "pending_reserved": len(pending), "unknown": unknown,
             "realised_today_inr": state.realised_today_inr, "realised_month_inr": state.realised_month_inr,
             "realised_total_inr": None if r_all is None else float(r_all),
             "open_pnl_inr": None if open_now_total is None else float(open_now_total),
             "equity_inr": curve[-1][1] if curve else None,
             "equity_points": len(curve) if curve else 0}
    return state, facts


def paper_book(journal: Journal, settings, pool: str, now: datetime, new_row: dict | None = None,
               strategy_id: str | None = None) -> dict:
    """One strategy's paper book in a pool (the strategy of `new_row`, else `strategy_id`): the pool's capital
    (`paper_capital`), what its open and pending paper trades commit (cash value at entry; options: premium paid or the
    approved margin; pending: the approved margin or cash value), and whether `new_row` (a pending paper trade of that
    strategy) is admitted. Sizes are the real book's (the approved decision's quantity or lots). Only the safety cap
    `paper.max_open_trades` (open + pending, this strategy only) refuses a trade, so other strategies never stop it and
    its track record holds every approved signal; the commitment is reported (`over_capital` when above the capital),
    never a refusal. Never touches my real book."""
    sid = (new_row or {}).get("strategy_id") or strategy_id
    cap = paper_capital(settings, pool)
    held = journal.paper_open(pool, now, sid)
    committed, unknown = Decimal(0), []
    for t in held:
        c = committed_inr(journal.paper_position(t)) if t["status"] == "OPEN" else _num(t["margin_inr"])
        if c is None:
            unknown.append(f"paper {t['symbol']} ({t['signal_id']}): commitment not known")
            continue
        committed += _paise(_d(c))
    new_inr = _num((new_row or {}).get("margin_inr")) or 0.0
    after = committed + _paise(_d(new_inr))
    cap_n = settings.paper.max_open_trades
    n = len(held) + (1 if new_row is not None else 0)
    why = []
    if new_row is not None and n > cap_n:
        why.append(f"PAPER_BOOK_LIMIT: {n} paper trades of {sid} open or pending with this one, above "
                   f"paper.max_open_trades {cap_n} (a per-strategy safety cap)")
    return {"book": "PAPER", "pool": pool, "strategy_id": sid, "capital_inr": float(cap),
            "committed_inr": float(committed), "new_inr": float(new_inr), "after_inr": float(after),
            "over_capital": float(after) > cap, "open_or_pending": len(held), "max_open_trades": cap_n,
            "unknown": unknown, "fits": not why, "why": why}


def paper_books(journal: Journal, settings, now: datetime) -> dict:
    """Every strategy's paper book, per pool: {pool: {strategy_id: paper_book}} (strategies with paper trades)."""
    out = {}
    for pool in POOLS:
        ids = sorted({t["strategy_id"] for t in journal.paper_trades(pool=pool) if t["strategy_id"]})
        out[pool] = {sid: paper_book(journal, settings, pool, now, strategy_id=sid) for sid in ids}
    return out


def record_marks(journal: Journal, pool: str, now: datetime, marks, costs) -> int:
    """Save each held position's open P&L per unit (share or lot) for today, so later days know the
    previous close's figure (the last mark of a day stands for its close). Returns the number saved."""
    n = 0
    for p in journal.held_positions(pool):
        prices, _ = marks(p, now)
        v = open_pnl(p, prices, costs)
        if v is not None:
            journal.save_mark(p.position_id, now, float(v) / p.quantity)
            n += 1
    return n


def net_flows(journal: Journal, pool: str, flows, upto: date) -> Decimal:
    """Net deposits (deposits +, withdrawals -) of a pool dated after its first equity point up to `upto` inclusive:
    the part of the equity that is money put in, not profit (the deposit rule, module docstring). CASH_INDEX only."""
    if pool != CASH_INDEX or not flows:
        return Decimal(0)
    rows = journal.equity_rows(pool)
    start = rows[0][0] if rows else None
    return sum((_d(a) for d, a in flows if (start is None or d > start) and d <= upto), Decimal(0))


def close_equity_day(journal: Journal, settings, pool: str, now: datetime, flows=()) -> dict | None:
    """Write today's closing equity of a pool after the close, from the last marks of the session, with the net
    deposits to date (`net_flows`). Nothing is written when a figure is not known (a position without a mark
    today, a close without a net figure)."""
    r_all = journal.realised(pool)
    total = Decimal(0)
    for p in journal.held_positions(pool):           # the last mark of the session (prices are CLOSED now)
        per_unit = journal.mark_on(p.position_id, ist_day(now))
        if per_unit is None:
            return None
        total += _paise(_d(per_unit) * p.quantity)
    if r_all is None:
        return None
    eq = _paise(_d(pool_capital(settings, pool)) + net_flows(journal, pool, flows, ist_day(now)) + r_all + total)
    journal.write_equity(pool, ist_day(now), float(eq), float(r_all), float(total), now, "close")
    return {"pool": pool, "day": ist_day(now).isoformat(), "equity_inr": float(eq)}
