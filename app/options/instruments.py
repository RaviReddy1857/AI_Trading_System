"""Lot sizes, expiry dates and listed strikes, read from the broker's instrument list.

Source: ICICI's NSE F&O list (FONSEScripMaster.txt inside the public SecurityMaster.zip that
breeze-connect downloads; read in app/feeds/breeze_feed.py). Nothing here is hardcoded: a lot size
or expiry that is not in the stored list is UNKNOWN (None), and callers then give NO TRADE
(LOT_SIZE_UNKNOWN). The list only holds contracts that are listed today; expired contracts are
found by app/options/option_history.py instead.

Sim mode has no broker list: `sim_specs` builds clearly fake contracts around the simulated price
(stored with source 'sim'), so the page and tests work offline.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..calendar import MarketCalendar
from ..models import utcnow
from ..store import Store

log = logging.getLogger("instruments")

FNO_FIELDS = ("InstrumentName", "ShortName", "ExpiryDate", "StrikePrice", "OptionType", "LotSize")
OPTION_INSTRUMENTS = ("OPTIDX", "OPTSTK")


@dataclass(frozen=True)
class ContractSpec:
    """One listed expiry of one underlying."""
    underlying: str
    expiry: date
    lot_size: int
    kind: str                     # weekly | monthly
    strikes: tuple[float, ...]    # listed strikes, ascending
    as_of: datetime


def parse_fno_master(lines) -> list[dict]:
    """Option rows of FONSEScripMaster.txt: breeze code, expiry, strike, right (CE/PE), lot size.
    Rows that cannot be read are skipped (they are simply unknown)."""
    reader = csv.reader(lines)
    header = [h.strip().strip('"') for h in next(reader, [])]
    missing = [f for f in FNO_FIELDS if f not in header]
    if missing:
        raise ValueError(f"F&O instrument list has no {missing} column(s)")
    ix = {f: header.index(f) for f in FNO_FIELDS}
    out = []
    for row in reader:
        if len(row) < len(header):
            continue
        get = {f: row[i].strip().strip('"').strip() for f, i in ix.items()}
        if get["InstrumentName"] not in OPTION_INSTRUMENTS or get["OptionType"] not in ("CE", "PE"):
            continue
        try:
            expiry = datetime.strptime(get["ExpiryDate"], "%d-%b-%Y").date()
            strike = float(get["StrikePrice"])
            lot = int(float(get["LotSize"]))
        except ValueError:
            continue
        if strike <= 0 or lot <= 0:
            continue
        out.append({"code": get["ShortName"], "expiry": expiry, "strike": strike,
                    "right": get["OptionType"], "lot_size": lot})
    return out


def monthly_expiries(expiries) -> set[date]:
    """The last listed expiry of each calendar month is that month's monthly contract."""
    last: dict[tuple[int, int], date] = {}
    for e in expiries:
        key = (e.year, e.month)
        if key not in last or e > last[key]:
            last[key] = e
    return set(last.values())


def build_specs(rows: list[dict], code_to_symbol: dict[str, str], as_of: datetime) -> list[ContractSpec]:
    """Contract specs for the symbols in `code_to_symbol` (breeze code -> our symbol). An expiry
    whose rows disagree on the lot size is left out (unknown), never guessed."""
    grouped: dict[tuple[str, date], dict] = {}
    for r in rows:
        sym = code_to_symbol.get(r["code"])
        if sym is None:
            continue
        g = grouped.setdefault((sym, r["expiry"]), {"lots": set(), "strikes": set()})
        g["lots"].add(r["lot_size"])
        g["strikes"].add(r["strike"])
    by_sym: dict[str, list[date]] = {}
    for sym, e in grouped:
        by_sym.setdefault(sym, []).append(e)
    monthly = {sym: monthly_expiries(es) for sym, es in by_sym.items()}
    out = []
    for (sym, e), g in sorted(grouped.items()):
        if len(g["lots"]) != 1:
            log.warning("%s %s: instrument list has lot sizes %s; expiry left out", sym, e, sorted(g["lots"]))
            continue
        out.append(ContractSpec(sym, e, next(iter(g["lots"])), "monthly" if e in monthly[sym] else "weekly",
                                tuple(sorted(g["strikes"])), as_of))
    return out


def nice_step(price: float) -> float:
    """A round strike spacing of roughly 1% of the price (used for fake sim strikes and as the first
    guess for expired stock contracts, whose real spacing is not in the instrument list)."""
    steps = (0.5, 1, 2.5, 5, 10, 20, 50, 100, 250, 500, 1000)
    ok = [s for s in steps if s <= price * 0.0125]
    return ok[-1] if ok else steps[0]


def sim_specs(prices: dict[str, float], today: date, calendar: MarketCalendar, as_of: datetime,
              weekly: set[str] = frozenset()) -> list[ContractSpec]:
    """FAKE contracts for sim mode: a monthly expiry on the last trading Tuesday of this month and
    the next two (weekly Tuesdays for symbols in `weekly`), strikes around the simulated price, and a
    lot size derived from the price (about Rs 5 lakh of contract value). Not real exchange values."""
    def on_or_before(d: date) -> date:
        while not calendar.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def last_tuesday(y: int, m: int) -> date:
        d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        while d.weekday() != 1:
            d -= timedelta(days=1)
        return on_or_before(d)

    months = []
    y, m = today.year, today.month
    while len(months) < 3:
        e = last_tuesday(y, m)
        if e >= today:
            months.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    out = []
    for sym, px in sorted(prices.items()):
        if not (isinstance(px, (int, float)) and px > 0):
            continue
        step = nice_step(px)
        atm = round(px / step) * step
        strikes = tuple(round(atm + i * step, 2) for i in range(-25, 26) if atm + i * step > 0)
        lot = max(5, round(500000 / px / 5) * 5)
        expiries = {e: "monthly" for e in months}
        if sym in weekly:
            d = today
            while len([k for k in expiries.values() if k == "weekly"]) < 4:
                if d.weekday() == 1:
                    expiries.setdefault(on_or_before(d), "weekly")
                d += timedelta(days=1)
                if (d - today).days > 60:
                    break
        out += [ContractSpec(sym, e, lot, kind, strikes, as_of) for e, kind in sorted(expiries.items())]
    return out


class InstrumentBook:
    """Stored contract specs for one data source ('breeze' or 'sim')."""

    def __init__(self, store: Store, source: str, max_age_days: int = 5):
        self.store = store
        self.source = source
        self.max_age = timedelta(days=max_age_days)
        self._specs: dict[str, list[ContractSpec]] | None = None

    def save(self, specs: list[ContractSpec]) -> int:
        self.store.replace_instruments(self.source, [
            (s.underlying, s.expiry.isoformat(), s.lot_size, s.kind, list(s.strikes), s.as_of.isoformat())
            for s in specs])
        self._specs = None
        return len(specs)

    def _load(self) -> dict[str, list[ContractSpec]]:
        if self._specs is None:
            out: dict[str, list[ContractSpec]] = {}
            for r in self.store.instruments(self.source):
                out.setdefault(r["underlying"], []).append(ContractSpec(
                    r["underlying"], r["expiry"], r["lot_size"], r["kind"], tuple(r["strikes"]), r["as_of"]))
            self._specs = out
        return self._specs

    def as_of(self) -> datetime | None:
        specs = [s for v in self._load().values() for s in v]
        return max((s.as_of for s in specs), default=None)

    def is_fresh(self, now: datetime | None = None) -> bool:
        """The list changes as contracts are listed and lot sizes are revised: a list older than
        max_age_days counts as unknown."""
        a = self.as_of()
        return a is not None and (now or utcnow()) - a <= self.max_age

    def specs(self, underlying: str, today: date | None = None, now: datetime | None = None) -> list[ContractSpec]:
        """Unexpired contracts of an underlying, nearest first. Empty when the list is missing or stale."""
        if not self.is_fresh(now):
            return []
        return [s for s in self._load().get(underlying, []) if today is None or s.expiry >= today]

    def spec(self, underlying: str, expiry: date, now: datetime | None = None) -> ContractSpec | None:
        return next((s for s in self.specs(underlying, now=now) if s.expiry == expiry), None)

    def lot_size(self, underlying: str, expiry: date, now: datetime | None = None) -> int | None:
        s = self.spec(underlying, expiry, now)
        return s.lot_size if s else None

    def nearest_lot_size(self, underlying: str, today: date | None = None) -> int | None:
        """Lot size of the nearest unexpired contract (used to express backtest results per lot at
        today's lot size; past lot sizes are not in the list)."""
        specs = self.specs(underlying, today)
        return specs[0].lot_size if specs else None

    def underlyings(self) -> list[str]:
        return sorted(self._load())


def refresh_from_broker(store: Store, watchlist, now: datetime | None = None, lines=None) -> int:
    """Read ICICI's F&O list and store the contracts of every watchlist symbol (source 'breeze').
    `lines` lets tests pass the file's text. No login is needed."""
    if lines is None:
        from ..feeds.breeze_feed import nfo_scrip_master_lines
        lines = nfo_scrip_master_lines()
    code_to_symbol = {i.breeze_code: i.symbol for i in watchlist}
    specs = build_specs(parse_fno_master(lines), code_to_symbol, now or utcnow())
    return InstrumentBook(store, "breeze").save(specs)
