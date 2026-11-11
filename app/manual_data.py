"""Manual data the user maintains as CSV files: event calendar, NSE holidays and
daily FII/DII cash flows.

Each file is parsed all-or-nothing: if any row is invalid, nothing is replaced and
the errors are reported, so a typo can never silently drop an event or a holiday.
The CSV file is the source of truth; its rows are copied into SQLite on load."""
from __future__ import annotations

import csv
import io
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .calendar import MarketCalendar
from .config import EVENT_TYPES, Settings
from .store import Store

log = logging.getLogger("manual_data")

KINDS = ("events", "holidays", "fii_dii")
MAX_IMPORT_BYTES = 200_000


@dataclass(frozen=True)
class Event:
    date: date
    type: str
    note: str
    symbol: str = ""          # blank = market-wide


@dataclass(frozen=True)
class FiiDii:
    date: date
    fii_net_cr: float
    dii_net_cr: float | None


# ---------------- parsing helpers ----------------
def parse_date(text: str) -> date:
    s = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unreadable date '{s}' (use YYYY-MM-DD or DD-Mon-YYYY)")


def parse_amount(text: str) -> float:
    s = (text or "").strip().replace(",", "").replace("₹", "")
    if s.startswith("(") and s.endswith(")"):      # accounting negative
        s = "-" + s[1:-1]
    if s in ("", "-", "--"):
        raise ValueError("amount is empty")
    v = float(s)
    if v != v:
        raise ValueError("amount is not a number")
    return v


def _rows(text: str) -> list[list[str]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return [[c.strip() for c in r] for r in csv.reader(lines)]


def _norm(h: str) -> str:
    return "".join(ch for ch in h.lower() if ch.isalnum())


def _find(header: list[str], *names: str) -> int | None:
    norm = [_norm(h) for h in header]
    for n in names:
        for i, h in enumerate(norm):
            if h.startswith(n):
                return i
    return None


def _cell(row: list[str], i: int | None) -> str:
    return row[i] if i is not None and i < len(row) else ""


# ---------------- events ----------------
def parse_events(text: str) -> tuple[list[Event], list[str]]:
    rows = _rows(text)
    if not rows:
        return [], []
    head, body = rows[0], rows[1:]
    di, ti, ni, si = _find(head, "date"), _find(head, "type"), _find(head, "note"), _find(head, "symbol")
    if di is None or ti is None:
        return [], ["events header must contain 'date' and 'type' (optional 'note', 'symbol')"]
    out, errors = [], []
    for n, r in enumerate(body, start=2):
        try:
            d = parse_date(_cell(r, di))
            t = _cell(r, ti).upper()
            if t not in EVENT_TYPES:
                raise ValueError(f"type '{t}' is not one of {list(EVENT_TYPES)}")
            out.append(Event(d, t, _cell(r, ni), _cell(r, si).upper()))
        except ValueError as e:
            errors.append(f"line {n}: {e}")
    return sorted(set(out), key=lambda e: (e.date, e.type, e.symbol, e.note)), errors


def events_to_csv(events: list[Event]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "type", "note", "symbol"])
    for e in events:
        w.writerow([e.date.isoformat(), e.type, e.note, e.symbol])
    return buf.getvalue()


# ---------------- holidays ----------------
def parse_holidays(text: str) -> tuple[dict[date, str], list[str]]:
    rows = _rows(text)
    if not rows:
        return {}, []
    head, body = rows[0], rows[1:]
    di = _find(head, "date")
    ni = _find(head, "note", "description", "holiday")
    if di is None:
        return {}, ["holidays header must contain 'date' (optional 'note' or 'description')"]
    out, errors = {}, []
    for n, r in enumerate(body, start=2):
        try:
            d = parse_date(_cell(r, di))
            if d.weekday() >= 5:
                log.info("holiday %s falls on a weekend; kept for the record", d)
            out[d] = _cell(r, ni)
        except ValueError as e:
            errors.append(f"line {n}: {e}")
    return dict(sorted(out.items())), errors


def holidays_to_csv(holidays: dict[date, str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "note"])
    for d, note in sorted(holidays.items()):
        w.writerow([d.isoformat(), note])
    return buf.getvalue()


# ---------------- FII / DII ----------------
def parse_fii_dii(text: str) -> tuple[dict[date, FiiDii], list[str]]:
    """Two accepted layouts:
    1. Our own:   date,fii_net_cr,dii_net_cr
    2. NSE paste: Category,Date,Buy Value,Sell Value,Net Value   (one row per FII/FPI and DII)
    Values are in Rs crore. Net selling is negative."""
    rows = _rows(text)
    if not rows:
        return {}, []
    head, body = rows[0], rows[1:]
    errors: list[str] = []
    ci = _find(head, "category")
    if ci is None:
        di, fi, dii = _find(head, "date"), _find(head, "fiinet", "fii"), _find(head, "diinet", "dii")
        if di is None or fi is None:
            return {}, ["fii_dii header must be 'date,fii_net_cr,dii_net_cr' or the NSE "
                        "'Category,Date,Buy Value,Sell Value,Net Value' layout"]
        out: dict[date, FiiDii] = {}
        for n, r in enumerate(body, start=2):
            try:
                d = parse_date(_cell(r, di))
                dv = _cell(r, dii).strip()
                out[d] = FiiDii(d, parse_amount(_cell(r, fi)), parse_amount(dv) if dv else None)
            except ValueError as e:
                errors.append(f"line {n}: {e}")
        return dict(sorted(out.items())), errors

    di, ni = _find(head, "date"), _find(head, "net")
    if di is None or ni is None:
        return {}, ["NSE layout needs 'Date' and 'Net Value' columns"]
    fii: dict[date, float] = {}
    dii_v: dict[date, float] = {}
    for n, r in enumerate(body, start=2):
        try:
            cat = _norm(_cell(r, ci))
            d = parse_date(_cell(r, di))
            v = parse_amount(_cell(r, ni))
            if cat.startswith(("fii", "fpi")):
                fii[d] = v
            elif cat.startswith("dii"):
                dii_v[d] = v
            else:
                raise ValueError(f"category '{_cell(r, ci)}' is neither FII/FPI nor DII")
        except ValueError as e:
            errors.append(f"line {n}: {e}")
    for d in sorted(set(dii_v) - set(fii)):
        errors.append(f"{d}: DII row has no matching FII/FPI row")
    out = {d: FiiDii(d, fii[d], dii_v.get(d)) for d in sorted(fii)}
    return out, errors


def fii_dii_to_csv(rows: dict[date, FiiDii]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "fii_net_cr", "dii_net_cr"])
    for d in sorted(rows):
        r = rows[d]
        w.writerow([d.isoformat(), r.fii_net_cr, "" if r.dii_net_cr is None else r.dii_net_cr])
    return buf.getvalue()


# ---------------- manager ----------------
class ManualData:
    """Loads the CSV files into the store and calendar; handles pasted imports."""

    def __init__(self, settings: Settings, store: Store, calendar: MarketCalendar):
        self.settings = settings
        self.store = store
        self.calendar = calendar
        self._lock = threading.Lock()
        self.errors: dict[str, list[str]] = {k: [] for k in KINDS}

    def _path(self, kind: str) -> Path:
        c = self.settings.calendar
        rel = {"events": c.events_csv, "holidays": c.holidays_csv, "fii_dii": c.fii_dii_csv}[kind]
        return self.settings.path(rel)

    def load_all(self) -> None:
        """Load every CSV. A file that fails to parse is NOT applied: the last good
        copy already in SQLite stays in force (it is never replaced by an empty set),
        and problems() reports the file and row so the regime stays UNKNOWN.
        A missing file, or one with no header row at all, is treated the same way;
        only a file with a header and no data rows means "the list is empty"."""
        for kind in KINDS:
            p = self._path(kind)
            try:
                if not p.exists():
                    parsed, errors = None, ["file not found"]
                else:
                    text = p.read_text(encoding="utf-8-sig")
                    if not _rows(text):
                        parsed, errors = None, ["file is empty (no header row)"]
                    else:
                        parsed, errors = self._parse(kind, text)
            except (OSError, UnicodeDecodeError) as e:
                parsed, errors = None, [f"cannot read file: {type(e).__name__}"]
            with self._lock:
                self.errors[kind] = errors
            if errors:
                log.error("%s: %d invalid row(s); file NOT loaded, last good copy kept: %s",
                          p.name, len(errors), errors[:5])
                if kind == "holidays":
                    self.calendar.set_holidays(self.store.holidays())
                continue
            self._apply(kind, parsed)

    def display_path(self, kind: str) -> str:
        c = self.settings.calendar
        return {"events": c.events_csv, "holidays": c.holidays_csv, "fii_dii": c.fii_dii_csv}[kind]

    def problems(self) -> list[str]:
        """One plain-language line per invalid file, naming the file and the bad row(s)."""
        out = []
        with self._lock:
            errors = {k: list(v) for k, v in self.errors.items() if v}
        for kind, errs in errors.items():
            shown = "; ".join(errs[:3]) + (f"; and {len(errs) - 3} more" if len(errs) > 3 else "")
            out.append(f"{self.display_path(kind)} could not be loaded ({shown}); "
                       f"the last good copy is in use until it is fixed or re-imported")
        return out

    @staticmethod
    def _parse(kind: str, text: str):
        return {"events": parse_events, "holidays": parse_holidays, "fii_dii": parse_fii_dii}[kind](text)

    def _apply(self, kind: str, parsed) -> None:
        if kind == "events":
            self.store.replace_events([(e.date.isoformat(), e.type, e.symbol, e.note) for e in parsed])
        elif kind == "holidays":
            self.store.replace_holidays([(d.isoformat(), n) for d, n in parsed.items()])
            self.calendar.set_holidays(parsed)
        else:
            self.store.replace_fii_dii([(d.isoformat(), r.fii_net_cr, r.dii_net_cr)
                                        for d, r in parsed.items()])

    def import_text(self, kind: str, text: str) -> dict:
        """events and holidays: the pasted list REPLACES the file.
        fii_dii: pasted days are MERGED into the file (same date is overwritten)."""
        if kind not in KINDS:
            return {"ok": False, "errors": [f"unknown kind '{kind}'; use one of {list(KINDS)}"]}
        if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
            return {"ok": False, "errors": ["pasted text is too large"]}
        parsed, errors = self._parse(kind, text)
        if errors:
            return {"ok": False, "errors": errors, "rows_saved": 0}
        if not parsed:
            return {"ok": False, "errors": ["no data rows found"], "rows_saved": 0}
        with self._lock:
            path = self._path(kind)
            if kind == "fii_dii":
                if path.exists():
                    existing, old_errors = parse_fii_dii(path.read_text(encoding="utf-8-sig"))
                    if old_errors:
                        return {"ok": False, "rows_saved": 0,
                                "errors": [f"existing {path.name} is invalid, fix it first"] + old_errors}
                else:
                    # File missing: merge into the last good copy so no stored day is lost.
                    existing = {r["date"]: FiiDii(r["date"], r["fii_net_cr"], r["dii_net_cr"])
                                for r in self.store.fii_dii_before(date.max, 1_000_000)}
                merged = {**existing, **parsed}
                out_text, final = fii_dii_to_csv(merged), merged
            elif kind == "events":
                out_text, final = events_to_csv(parsed), parsed
            else:
                out_text, final = holidays_to_csv(parsed), parsed
            _atomic_write(path, out_text)
            self.errors[kind] = []
        self._apply(kind, final)
        return {"ok": True, "errors": [], "rows_saved": len(parsed), "rows_total": len(final)}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
