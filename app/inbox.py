"""Inbox importers for files you download by hand.

Put the files in data/inbox/ and press "Import inbox files" on the Data status page (or run
`python -m app.inbox`). Each file is recognised by its columns:

- NSE board meetings CSV (columns SYMBOL, PURPOSE and a meeting-date column): a RESULTS event for each
  meeting whose purpose mentions results, and a COVERAGE row for every watchlist stock (symbol, source,
  retrieved_at = the file's date, covered_through = the export's END date that you declare). A file
  proves nothing about a stock it does not list unless it is the whole list, so a board-meetings file
  is imported only with your declaration (form fields or --board-from / --board-through /
  --all-companies): the export's date range, and your confirmation that it is an unfiltered
  all-companies export. The file is then sanity-checked (at least `board_meetings_min_symbols`
  distinct companies; every meeting date inside the declared range; meetings within
  `board_meetings_edge_days` of both ends) and rejected otherwise. Only then does "no row for a stock"
  mean "no meeting announced in the range".
- NSE corporate actions CSV (SYMBOL, PURPOSE, EX-DATE): new corporate actions (source "nse_inbox").
- NSE FII/DII activity CSV (Category, Date, Buy Value, Sell Value, Net Value) or our own
  date,fii_net_cr,dii_net_cr: new days added to data/fii_dii.csv; a day already there is kept.
- RBI decisions CSV (date, decision): RBI_POLICY rows in data/events.csv. A decision of "scheduled"
  records a planned meeting date (the Data status page then knows when a decision is due).

A processed file moves to data/inbox/done/, a rejected one to data/inbox/rejected/ with a .reason.txt
file saying why. Every row is tagged with its source file and the import date; duplicates are skipped.
Nothing in a file can change a row you entered by hand.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import Settings, load_settings
from .corporate_actions import stated_factor
from .manual_data import Event, ManualData, _cell, _find, _norm, _rows, merge_into_file, parse_date, parse_fii_dii
from .models import utcnow
from .quality import IST
from .store import Store

MAX_FILE_BYTES = 5_000_000
INBOX_SOURCE = "nse_inbox"


class Rejected(ValueError):
    pass


def detect(header: list[str]) -> str | None:
    h = [_norm(x) for x in header]
    has = lambda *names: any(any(x.startswith(n) for n in names) for x in h)
    if has("category") and has("net"):
        return "fii_dii"
    if has("date") and has("fiinet", "fii") and not has("symbol"):
        return "fii_dii"
    if has("symbol") and has("purpose") and has("exdate"):
        return "corporate_actions"
    if has("symbol") and has("purpose") and has("meetingdate", "boardmeetingdate", "date"):
        return "board_meetings"
    if has("date") and has("decision"):
        return "rbi"
    return None


def _today(now: datetime) -> date:
    return now.astimezone(IST).date()


# ---------------- board meetings ----------------
@dataclass(frozen=True)
class BoardMeetingsDeclaration:
    """What you state about a downloaded NSE board-meetings export: its date range and that it is the
    unfiltered all-companies list (no symbol, index or purpose filter)."""
    covered_from: date
    covered_through: date
    all_companies: bool


NO_DECLARATION = ("a board-meetings file needs your declaration before it can give event coverage: the export's "
                  "date range (from, through) and your confirmation that it is an unfiltered all-companies export "
                  "(Data status page fields, or python -m app.inbox --board-from YYYY-MM-DD --board-through "
                  "YYYY-MM-DD --all-companies)")


def check_board_meetings(dates: list[date], symbols: set[str], decl: BoardMeetingsDeclaration | None, retrieved: date,
                         min_symbols: int, edge_days: int) -> None:
    """Raise Rejected unless the file can be the whole list for the declared range."""
    if decl is None:
        raise Rejected(NO_DECLARATION)
    if not decl.all_companies:
        raise Rejected("you did not confirm that the file is an unfiltered all-companies export: a filtered list "
                       "proves nothing about the stocks it leaves out, so no coverage is given")
    if decl.covered_from > decl.covered_through:
        raise Rejected(f"declared range {decl.covered_from} to {decl.covered_through} ends before it starts")
    if decl.covered_from > retrieved:
        raise Rejected(f"declared range starts {decl.covered_from}, after the file was downloaded ({retrieved}): "
                       f"the days in between would not be covered")
    if not dates:
        raise Rejected("no meeting dates in the file: coverage cannot be stated")
    if len(symbols) < min_symbols:
        raise Rejected(f"only {len(symbols)} distinct companies in the file (at least {min_symbols} expected in an "
                       f"all-companies export): it looks filtered")
    outside = sorted(d for d in dates if not decl.covered_from <= d <= decl.covered_through)
    if outside:
        raise Rejected(f"{len(outside)} meeting dates lie outside the declared range {decl.covered_from} to "
                       f"{decl.covered_through} (first {outside[0]}): the declared range does not match the file")
    lo, hi = min(dates), max(dates)
    if (lo - decl.covered_from).days > edge_days or (decl.covered_through - hi).days > edge_days:
        raise Rejected(f"meetings run from {lo} to {hi}, but the declared range is {decl.covered_from} to "
                       f"{decl.covered_through} (more than {edge_days} days without a meeting at an end): the file "
                       f"does not span the declared range")


def board_meeting_events(text: str, name: str, retrieved: date, watchlist: list[str], now: datetime,
                         declaration: BoardMeetingsDeclaration | None = None, min_symbols: int = 100,
                         edge_days: int = 7) -> tuple[list[Event], dict]:
    rows = _rows(text)
    head, body = rows[0], rows[1:]
    si, pi = _find(head, "symbol"), _find(head, "purpose")
    di = _find(head, "boardmeetingdate", "meetingdate", "date")
    ni = _find(head, "details", "description")
    if si is None or pi is None or di is None:
        raise Rejected("board meetings file needs SYMBOL, PURPOSE and a meeting date column; found " + ", ".join(head))
    out, dates, symbols, bad = [], [], set(), []
    for n, r in enumerate(body, start=2):
        try:
            d = parse_date(_cell(r, di).split(" ")[0] if len(_cell(r, di)) > 11 else _cell(r, di))
        except ValueError as e:
            bad.append(f"line {n}: {e}")
            continue
        dates.append(d)
        if _cell(r, si):
            symbols.add(_cell(r, si).upper())
        purpose = (_cell(r, pi) + " " + _cell(r, ni)).strip()
        if re.search(r"result", purpose, re.I):
            note = (f"Board meeting: {_cell(r, pi)[:120]} ; source={INBOX_SOURCE} file {name} ; "
                    f"imported {_today(now).isoformat()}").replace("=", ":").replace(";", ",")
            out.append(Event(d, "RESULTS", note, _cell(r, si).upper()))
    if bad:
        raise Rejected("; ".join(bad[:5]))
    check_board_meetings(dates, symbols, declaration, retrieved, min_symbols, edge_days)
    frm, through = declaration.covered_from, declaration.covered_through
    for sym in watchlist:          # the whole list for the declared range, so no row = no meeting announced
        out.append(Event(retrieved, "COVERAGE", f"source=NSE board meetings file {name.replace(';', ',').replace('=', '-')} "
                                                f"(all companies, confirmed by you); covered_from={frm.isoformat()}; "
                                                f"covered_through={through.isoformat()}", sym))
    return out, {"results_events": sum(1 for e in out if e.type == "RESULTS"), "coverage_rows": len(watchlist),
                 "covered_from": frm.isoformat(), "covered_through": through.isoformat(),
                 "distinct_companies": len(symbols)}


# ---------------- corporate actions ----------------
BONUS = re.compile(r"bonus\D*?(\d+)\s*:\s*(\d+)", re.I)
SPLIT = re.compile(r"(?:split|sub-?division).*?(?:rs|re)\.?\s*(\d+(?:\.\d+)?).*?\bto\b\s*(?:rs|re)\.?\s*(\d+(?:\.\d+)?)", re.I)
RIGHTS = re.compile(r"rights\D*?(\d+)\s*:\s*(\d+)", re.I)


def classify(purpose: str) -> tuple[str, str]:
    """(action type, ratio) from an NSE PURPOSE text."""
    if m := SPLIT.search(purpose):
        return "SPLIT", f"{m.group(1)}:{m.group(2)}"
    if m := BONUS.search(purpose):
        return "BONUS", f"{m.group(1)}:{m.group(2)}"
    if re.search(r"demerger", purpose, re.I):
        return "DEMERGER", ""
    if m := RIGHTS.search(purpose):
        return "RIGHTS", f"{m.group(1)}:{m.group(2)}"
    if re.search(r"dividend", purpose, re.I):
        return "DIVIDEND", ""
    return "OTHER", ""


def corporate_action_rows(text: str, name: str, now: datetime) -> list[dict]:
    rows = _rows(text)
    head, body = rows[0], rows[1:]
    si, pi, ei = _find(head, "symbol"), _find(head, "purpose"), _find(head, "exdate")
    ri, se = _find(head, "recorddate"), _find(head, "series")
    if si is None or pi is None or ei is None:
        raise Rejected("corporate actions file needs SYMBOL, PURPOSE and EX-DATE; found " + ", ".join(head))
    out, bad = [], []
    for n, r in enumerate(body, start=2):
        if _cell(r, se) and _cell(r, se).upper() not in ("EQ", "BE"):
            continue
        ex_text = _cell(r, ei)
        if ex_text in ("", "-"):
            continue
        try:
            ex = parse_date(ex_text)
        except ValueError as e:
            bad.append(f"line {n}: {e}")
            continue
        purpose = _cell(r, pi)
        t, ratio = classify(purpose)
        if t == "OTHER":
            continue
        factor, basis = stated_factor(t, ratio, purpose)
        rec = _cell(r, ri)
        out.append({"symbol": _cell(r, si).upper(), "ex_date": ex.isoformat(), "action_type": t, "purpose": purpose or t,
                    "record_date": parse_date(rec).isoformat() if rec not in ("", "-") else None, "announced_on": None,
                    "ratio": ratio or None, "price_factor": factor, "factor_basis": basis, "source": INBOX_SOURCE,
                    "source_ref": name, "imported_utc": now.isoformat()})
    if bad:
        raise Rejected("; ".join(bad[:5]))
    return out


# ---------------- RBI decisions ----------------
def rbi_events(text: str, name: str, now: datetime) -> list[Event]:
    rows = _rows(text)
    head, body = rows[0], rows[1:]
    di, ci = _find(head, "date"), _find(head, "decision")
    if di is None or ci is None:
        raise Rejected("RBI file needs 'date' and 'decision' columns; found " + ", ".join(head))
    out, bad = [], []
    for n, r in enumerate(body, start=2):
        try:
            out.append(rbi_event(parse_date(_cell(r, di)), _cell(r, ci), f"file {name}", now))
        except ValueError as e:
            bad.append(f"line {n}: {e}")
    if bad:
        raise Rejected("; ".join(bad[:5]))
    return out


def rbi_event(d: date, decision: str, origin: str, now: datetime) -> Event:
    text = (decision or "").strip().replace(";", ",")
    if not text:
        raise ValueError("the decision is empty (write e.g. 'repo unchanged at 5.25%', or 'scheduled')")
    if text.lower() == "scheduled":
        return Event(d, "RBI_POLICY", f"SCHEDULED MPC decision (entered by you, {origin}, {_today(now).isoformat()})", "")
    return Event(d, "RBI_POLICY", f"RBI decision: {text} (entered by you, {origin}, {_today(now).isoformat()})", "")


# ---------------- the run ----------------
def import_file(settings: Settings, store: Store, manual: ManualData, path: Path, now: datetime | None = None,
                board_meetings: BoardMeetingsDeclaration | None = None) -> dict:
    now = now or utcnow()
    if path.stat().st_size > MAX_FILE_BYTES:
        raise Rejected(f"file is larger than {MAX_FILE_BYTES // 1_000_000} MB")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    rows = _rows(text)
    if len(rows) < 2:
        raise Rejected("no data rows")
    kind = detect(rows[0])
    if kind is None:
        raise Rejected("columns not recognised: " + ", ".join(rows[0]))
    name = path.name
    retrieved = datetime.fromtimestamp(path.stat().st_mtime, IST).date()
    if kind == "fii_dii":
        parsed, errors = parse_fii_dii(text)
        if errors:
            raise Rejected("; ".join(errors[:5]))
        res = merge_into_file(manual, "fii_dii", parsed)
    elif kind == "board_meetings":
        stocks = [i.symbol for i in settings.watchlist if i.kind == "stock"]
        lim = settings.data_status
        events, info = board_meeting_events(text, name, retrieved, stocks, now, board_meetings,
                                            lim.board_meetings_min_symbols, lim.board_meetings_edge_days)
        res = {**merge_into_file(manual, "events", events), **info}
    elif kind == "corporate_actions":
        acts = corporate_action_rows(text, name, now)
        added = store.save_corporate_actions(acts, replace_source=False)
        store.log_import("corporate_actions", INBOX_SOURCE, name, now, added, "inbox file")
        res = {"ok": True, "added": added, "duplicates": len(acts) - added, "errors": []}
    else:
        res = merge_into_file(manual, "events", rbi_events(text, name, now))
    if not res.get("ok", True):
        raise Rejected("; ".join(res.get("errors") or ["not saved"]))
    if kind != "corporate_actions":
        store.log_import(kind, INBOX_SOURCE, name, now, res.get("added", 0), "inbox file")
    return {"file": name, "kind": kind, **res}


def run_inbox(settings: Settings, store: Store, manual: ManualData, now: datetime | None = None,
              board_meetings: BoardMeetingsDeclaration | None = None) -> list[dict]:
    now = now or utcnow()
    inbox = settings.path(settings.data_status.inbox_dir)
    done, rejected = inbox / "done", inbox / "rejected"
    for p in (inbox, done, rejected):
        p.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(IST).strftime("%Y%m%d-%H%M%S")
    out = []
    for path in sorted(x for x in inbox.iterdir() if x.is_file() and x.suffix.lower() in (".csv", ".txt")):
        try:
            res = import_file(settings, store, manual, path, now, board_meetings)
            shutil.move(str(path), str(done / f"{stamp}_{path.name}"))
            out.append({**res, "moved_to": f"done/{stamp}_{path.name}"})
        except (Rejected, OSError, ValueError) as e:
            target = rejected / f"{stamp}_{path.name}"
            shutil.move(str(path), str(target))
            target.with_name(target.name + ".reason.txt").write_text(f"{e}\n", encoding="utf-8")
            out.append({"file": path.name, "ok": False, "errors": [str(e)], "moved_to": f"rejected/{target.name}"})
    return out


def declaration(start, through, all_companies) -> BoardMeetingsDeclaration | None:
    """The declaration from form or command-line values; None when no date is given. A bad date raises ValueError."""
    start, through = (start or "").strip(), (through or "").strip()
    if not start and not through:
        return None
    if not start or not through:
        raise ValueError("give both dates of the board-meetings export (from and through)")
    return BoardMeetingsDeclaration(parse_date(start), parse_date(through), all_companies is True)


def main(argv: list[str] | None = None) -> int:
    from .calendar import MarketCalendar
    ap = argparse.ArgumentParser(description="Import the files in data/inbox/ (NSE board meetings, corporate actions, "
                                             "FII/DII, RBI decisions)")
    ap.add_argument("--board-from", help="board meetings: the first date of the downloaded export (YYYY-MM-DD)")
    ap.add_argument("--board-through", help="board meetings: the last date of the downloaded export (YYYY-MM-DD)")
    ap.add_argument("--all-companies", action="store_true",
                    help="board meetings: I confirm the file is an unfiltered all-companies export")
    args = ap.parse_args(argv)
    settings = load_settings()
    store = Store(settings.db_path(), live=settings.is_live_feed)
    manual = ManualData(settings, store, MarketCalendar())
    manual.load_all()
    res = run_inbox(settings, store, manual, board_meetings=declaration(args.board_from, args.board_through,
                                                                        args.all_companies))
    print(json.dumps(res, indent=2) if res else f"no files in {settings.data_status.inbox_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
