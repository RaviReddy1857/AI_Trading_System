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

from .calendar import MarketCalendar, coverage_through, is_coverage_row
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
    source: str = ""          # blank = entered by the user; otherwise where the row came from


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
    src_i, ret_i, cov_i = _find(head, "source"), _find(head, "retrievedat"), _find(head, "coveredthrough")
    if di is None or ti is None:
        return [], ["events header must contain 'date' and 'type' (optional 'note', 'symbol', and for COVERAGE rows "
                    "'source', 'retrieved_at', 'covered_through')"]
    out, errors = [], []
    for n, r in enumerate(body, start=2):
        try:
            t = _cell(r, ti).upper()
            # a COVERAGE row may give its date in retrieved_at instead of the date column
            d = parse_date(_cell(r, ret_i) if t == "COVERAGE" and not _cell(r, di).strip() else _cell(r, di))
            if t not in EVENT_TYPES:
                raise ValueError(f"type '{t}' is not one of {list(EVENT_TYPES)}")
            note, sym = _cell(r, ni), _cell(r, si).upper()
            if t == "COVERAGE":
                # symbol, source, retrieved_at (or the date column), covered_through: the stock's announced events
                # were checked at `source` on `retrieved_at` and are known through `covered_through`
                src, through = _cell(r, src_i).strip(), _cell(r, cov_i).strip()
                if _cell(r, ret_i).strip():
                    d = parse_date(_cell(r, ret_i))
                if not (src and through):
                    got = coverage_fields(note)
                    if got is None:
                        raise ValueError("a COVERAGE row needs symbol, source, retrieved_at and covered_through")
                    src, through = got[0], got[1].isoformat()
                if not sym:
                    raise ValueError("a COVERAGE row needs the stock's symbol")
                note = f"source={src}; covered_through={parse_date(through).isoformat()}"
            out.append(Event(d, t, note, sym))
        except ValueError as e:
            errors.append(f"line {n}: {e}")
    return sorted(set(out), key=lambda e: (e.date, e.type, e.symbol, e.note)), errors


def coverage_fields(note: str) -> tuple[str, date] | None:
    """(source, covered_through) of a COVERAGE row's note, or None if it cannot be read."""
    parts = dict(x.strip().split("=", 1) for x in (note or "").split(";") if "=" in x)
    try:
        src = parts["source"].strip()
        return (src, parse_date(parts["covered_through"])) if src else None
    except (KeyError, ValueError):
        return None


EVENT_COVERAGE_MISSING = "EVENT COVERAGE MISSING: results dates unknown"


@dataclass(frozen=True)
class WeekCoverage:
    """Whether the stocks' announced events (results, board meetings) are known for a week: a stock is covered when
    events.csv holds a COVERAGE row for it retrieved on or before today, at most `max_age_days` ago, that runs to the
    end of the week (`through`). Without it, "no event" only means none was entered, not that there is none."""
    today: date
    through: date
    stocks: tuple[str, ...]          # the watchlist stocks (an index card depends on all of them)
    why_not: dict                    # watchlist stock -> why it is not covered (missing, stale or too short)
    covered_extra: frozenset = frozenset()     # covered symbols outside the watchlist (stock option underlyings)

    def covered(self, symbol: str) -> bool:
        return symbol not in self.why_not if symbol in self.stocks else symbol in self.covered_extra

    def gaps(self, symbols=None) -> list[str]:
        """The stocks among `symbols` (default: every watchlist stock) not covered for the week."""
        return [x for x in (self.stocks if symbols is None else symbols) if not self.covered(x)]

    def text(self, symbols=None) -> str | None:
        """None when every stock is covered, else the missing-coverage line (with how many and which)."""
        g = self.gaps(symbols)
        if not g:
            return None
        n = len(self.stocks if symbols is None else symbols)
        return (f"{EVENT_COVERAGE_MISSING} (no current coverage through {self.through:%d-%b} for {len(g)} of {n} "
                f"stock(s): {', '.join(g[:8])}{', ...' if len(g) > 8 else ''})")


def week_coverage(events, stocks, today: date, through: date, max_age_days: int, extra=()) -> WeekCoverage:
    """WeekCoverage of `stocks` (and of any `extra` symbols, such as a stock option underlying outside the
    watchlist) from the stored events (dicts with date, type, symbol, note). Reuses the stock option selling rule
    (`options.stock_selling.event_coverage`) with the week's end in place of the expiry."""
    from types import SimpleNamespace

    from .options.stock_selling import event_coverage
    rows = [SimpleNamespace(**e) for e in events if e.get("type") == "COVERAGE"]
    why, ok_extra = {}, set()
    for sym in dict.fromkeys([*stocks, *extra]):
        ok, text = event_coverage(rows, sym, today, through, max_age_days)
        if not ok and sym in stocks:
            why[sym] = text
        if ok and sym not in stocks:
            ok_extra.add(sym)
    return WeekCoverage(today, through, tuple(stocks), why, frozenset(ok_extra))


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
            note = _cell(r, ni)
            if is_coverage_row(note):
                # not a holiday: the list is complete from this date through the one in the note
                end = coverage_through(note)
                if end is None or end < d:
                    raise ValueError("a COVERAGE row needs 'COVERAGE through YYYY-MM-DD' (on or after its date) in "
                                     "its description")
                if d in out and not is_coverage_row(out[d]):
                    raise ValueError(f"{d} is both a holiday and the start of a COVERAGE row: date the COVERAGE row "
                                     "on a working day")
            elif d in out and is_coverage_row(out[d]):
                raise ValueError(f"{d} is both a holiday and the start of a COVERAGE row")
            elif d.weekday() >= 5:
                log.info("holiday %s falls on a weekend; kept for the record", d)
            out[d] = note
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
        si = _find(head, "source")
        if di is None or fi is None:
            return {}, ["fii_dii header must be 'date,fii_net_cr,dii_net_cr' or the NSE "
                        "'Category,Date,Buy Value,Sell Value,Net Value' layout"]
        out: dict[date, FiiDii] = {}
        for n, r in enumerate(body, start=2):
            try:
                d = parse_date(_cell(r, di))
                dv = _cell(r, dii).strip()
                out[d] = FiiDii(d, parse_amount(_cell(r, fi)), parse_amount(dv) if dv else None, _cell(r, si))
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
    tagged = any(r.source for r in rows.values())
    w.writerow(["date", "fii_net_cr", "dii_net_cr"] + (["source"] if tagged else []))
    for d in sorted(rows):
        r = rows[d]
        w.writerow([d.isoformat(), r.fii_net_cr, "" if r.dii_net_cr is None else r.dii_net_cr] + ([r.source] if tagged else []))
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
        self.overlay_errors: dict[str, list[str]] = {}

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
        for kind, errs in dict(self.overlay_errors).items():
            out.append(f"{self.overlay_path(kind)} (imported rows) could not be read ({'; '.join(errs[:3])}); "
                       f"those rows are not used")
        return out

    @staticmethod
    def _parse(kind: str, text: str):
        return {"events": parse_events, "holidays": parse_holidays, "fii_dii": parse_fii_dii}[kind](text)

    def overlay_path(self, kind: str) -> Path | None:
        if kind not in ("events", "fii_dii"):
            return None
        return self.settings.path(self.settings.calendar.overlay(kind))

    def _overlay(self, kind: str):
        """Rows imported from the external repo (a gitignored file next to the user's own). Unreadable or
        invalid: not used at all, and reported by problems(); the user's own file is never affected."""
        p = self.overlay_path(kind)
        self.overlay_errors.pop(kind, None)
        if p is None or not p.exists():
            return [] if kind == "events" else {}
        try:
            parsed, errors = self._parse(kind, p.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError) as e:
            parsed, errors = None, [f"cannot read file: {type(e).__name__}"]
        if errors:
            self.overlay_errors[kind] = errors
            log.error("%s: %d invalid row(s); the imported rows are NOT used: %s", p.name, len(errors), errors[:5])
            return [] if kind == "events" else {}
        return parsed

    def _apply(self, kind: str, parsed) -> None:
        if kind == "events":
            own = list(parsed)
            mine = {(e.date, e.type, e.symbol) for e in own}
            extra = [e for e in self._overlay("events") if (e.date, e.type, e.symbol) not in mine]   # the user's row wins
            self.store.replace_events([(e.date.isoformat(), e.type, e.symbol, e.note) for e in own + extra])
        elif kind == "holidays":
            self.store.replace_holidays([(d.isoformat(), n) for d, n in parsed.items()])
            self.calendar.set_holidays(parsed)
        else:
            merged = {**self._overlay("fii_dii"), **parsed}            # a day the user entered is never replaced
            self.store.replace_fii_dii([(d.isoformat(), r.fii_net_cr, r.dii_net_cr)
                                        for d, r in sorted(merged.items())])

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


def event_key(e: Event) -> tuple:
    """What makes two events the same, whatever file they came from: date, type and symbol; for a
    COVERAGE row also its covered_through."""
    through = coverage_fields(e.note)[1] if e.type == "COVERAGE" and coverage_fields(e.note) else None
    return (e.date, e.type, e.symbol, through)


def merge_into_file(manual: "ManualData", kind: str, new) -> dict:
    """Add rows to the user's own file WITHOUT replacing any row already there (inbox imports).
    events: a row with the same event_key as one in the file is a duplicate; fii_dii: a date already in
    the file is kept as it is. The comment lines at the top of the file are kept."""
    path = manual._path(kind)
    with manual._lock:
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        old, errors = manual._parse(kind, text) if _rows(text) else (([] if kind == "events" else {}), [])
        if errors:
            return {"ok": False, "added": 0, "duplicates": 0,
                    "errors": [f"{path.name} is invalid, fix it first"] + errors[:5]}
        if kind == "events":
            have = {event_key(e) for e in old}
            add = []
            for e in new:
                if event_key(e) not in have:
                    have.add(event_key(e))
                    add.append(e)
            final = sorted(set(old) | set(add), key=lambda e: (e.date, e.type, e.symbol, e.note))
            body = events_to_csv(final)
        else:
            add = [r for d, r in new.items() if d not in old]
            final = {**{r.date: r for r in add}, **old}
            body = fii_dii_to_csv(final)
        head = "".join(ln + "\n" for ln in text.splitlines() if ln.lstrip().startswith("#"))
        if add:
            _atomic_write(path, head + body)
        manual.errors[kind] = []
    manual._apply(kind, final)
    return {"ok": True, "added": len(add), "duplicates": len(new) - len(add), "errors": []}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
