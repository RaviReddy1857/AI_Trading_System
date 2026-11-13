"""Margin for option spreads: entered by the user, never guessed.

Breeze's margin calculator is not one of the data calls this project allows itself (login, quotes,
history, option chain), so it is not used. Instead you copy the margin for one lot from ICICI
Direct's own margin calculator into `data/margins.csv` (or paste it on the Options page):

    underlying,structure,width,margin_per_lot_inr,as_of
    NIFTY,CREDIT_SPREAD,200,38500,2026-11-13
    NIFTY,IRON_CONDOR,100,41000,2026-11-13

`width` is the distance between the sold and the bought strike. An entry older than
options.margin.max_age_days, or no entry for that underlying, structure and width, means the margin
is UNKNOWN and the strategy gives no signal (MARGIN_UNKNOWN). Buying an option needs no margin: the
cash needed is the premium, which the strategy computes itself.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date, timedelta

from ..manual_data import MAX_IMPORT_BYTES, _atomic_write, _cell, _find, _rows, parse_amount, parse_date

STRUCTURES = ("CREDIT_SPREAD", "IRON_CONDOR")
MARGIN_UNKNOWN = "MARGIN_UNKNOWN"


@dataclass(frozen=True)
class MarginEntry:
    underlying: str
    structure: str
    width: float
    margin_per_lot_inr: float
    as_of: date


def parse_margins(text: str) -> tuple[dict[tuple, MarginEntry], list[str]]:
    """All-or-nothing parse, like the other manual files. Returns ({key: entry}, errors)."""
    rows = _rows(text)
    if not rows:
        return {}, []
    head, body = rows[0], rows[1:]
    ui, si, wi = _find(head, "underlying", "symbol"), _find(head, "structure"), _find(head, "width")
    mi, di = _find(head, "margin"), _find(head, "asof", "date")
    if None in (ui, si, wi, mi, di):
        return {}, ["margins header must be 'underlying,structure,width,margin_per_lot_inr,as_of'"]
    out: dict[tuple, MarginEntry] = {}
    errors: list[str] = []
    for n, r in enumerate(body, start=2):
        try:
            sym, structure = _cell(r, ui).upper(), _cell(r, si).upper()
            if not sym:
                raise ValueError("underlying is empty")
            if structure not in STRUCTURES:
                raise ValueError(f"structure '{structure}' is not one of {list(STRUCTURES)}")
            width, margin = parse_amount(_cell(r, wi)), parse_amount(_cell(r, mi))
            if not (math.isfinite(width) and width > 0 and math.isfinite(margin) and margin > 0):
                raise ValueError("width and margin must be above zero")
            e = MarginEntry(sym, structure, width, margin, parse_date(_cell(r, di)))
            out[(e.underlying, e.structure, e.width)] = e
        except ValueError as e:
            errors.append(f"line {n}: {e}")
    return out, errors


def margins_to_csv(entries: dict[tuple, MarginEntry]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["underlying", "structure", "width", "margin_per_lot_inr", "as_of"])
    for key in sorted(entries):
        e = entries[key]
        w.writerow([e.underlying, e.structure, f"{e.width:g}", f"{e.margin_per_lot_inr:g}", e.as_of.isoformat()])
    return buf.getvalue()


class MarginBook:
    def __init__(self, entries: list[MarginEntry], max_age_days: int):
        self._by = {(e.underlying, e.structure, round(e.width, 4)): e for e in entries}
        self.max_age = timedelta(days=max_age_days)

    @classmethod
    def from_store(cls, store, max_age_days: int) -> "MarginBook":
        return cls([MarginEntry(**r) for r in store.margins()], max_age_days)

    def lookup(self, underlying: str, structure: str, width: float, today: date) -> float | None:
        """Margin for ONE lot in rupees, or None when unknown or out of date."""
        e = self._by.get((underlying, structure, round(float(width), 4)))
        if e is None or today - e.as_of > self.max_age or e.as_of > today:
            return None
        return e.margin_per_lot_inr

    def entries(self) -> list[MarginEntry]:
        return [self._by[k] for k in sorted(self._by)]


def _to_rows(entries: dict[tuple, MarginEntry]) -> list[tuple]:
    return [(e.underlying, e.structure, e.width, e.margin_per_lot_inr, e.as_of.isoformat()) for e in entries.values()]


def load_margin_file(settings, store) -> list[str]:
    """Copy data/margins.csv into the store at start. A missing file means "no margins entered"
    (every spread is MARGIN_UNKNOWN); an invalid file is not applied and its errors are returned."""
    path = settings.path(settings.options.margin.csv)
    if not path.exists():
        return []
    try:
        parsed, errors = parse_margins(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as e:
        return [f"cannot read {path.name}: {type(e).__name__}"]
    if errors:
        return [f"{path.name} not loaded: " + "; ".join(errors[:3])]
    store.replace_margins(_to_rows(parsed))
    return []


def import_margins(settings, store, text: str) -> dict:
    """Pasted margins are MERGED into data/margins.csv (same underlying, structure and width is
    overwritten). All-or-nothing: any invalid row saves nothing."""
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        return {"ok": False, "errors": ["pasted text is too large"], "rows_saved": 0}
    parsed, errors = parse_margins(text)
    if errors:
        return {"ok": False, "errors": errors, "rows_saved": 0}
    if not parsed:
        return {"ok": False, "errors": ["no data rows found"], "rows_saved": 0}
    path = settings.path(settings.options.margin.csv)
    existing: dict[tuple, MarginEntry] = {}
    if path.exists():
        existing, old_errors = parse_margins(path.read_text(encoding="utf-8-sig"))
        if old_errors:
            return {"ok": False, "rows_saved": 0, "errors": [f"existing {path.name} is invalid, fix it first"] + old_errors}
    merged = {**existing, **parsed}
    _atomic_write(path, margins_to_csv(merged))
    store.replace_margins(_to_rows(merged))
    return {"ok": True, "errors": [], "rows_saved": len(parsed), "rows_total": len(merged)}
