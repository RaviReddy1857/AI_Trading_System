"""Inbox importers for files you download by hand.

Put the files in data/inbox/ and press "Import inbox files" on the Data status page (or run
`python -m app.inbox`). Each file is recognised by its columns:

- NSE board meetings CSV (columns SYMBOL, PURPOSE and a meeting-date column): a RESULTS event for each
  meeting whose purpose mentions results, and a COVERAGE row for every watchlist stock (symbol, source,
  retrieved_at = the file's date, covered_through = the latest meeting date in the file, or the date
  you give). The board-meetings list covers every company for its period, so a stock without a row had
  no announced meeting in it.
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
def board_meeting_events(text: str, name: str, retrieved: date, watchlist: list[str], now: datetime,
                         covered_through: date | None = None) -> tuple[list[Event], dict]:
    rows = _rows(text)
    head, body = rows[0], rows[1:]
    si, pi = _find(head, "symbol"), _find(head, "purpose")
    di = _find(head, "boardmeetingdate", "meetingdate", "date")
    ni = _find(head, "details", "description")
    if si is None or pi is None or di is None:
        raise Rejected("board meetings file needs SYMBOL, PURPOSE and a meeting date column; found " + ", ".join(head))
    out, last, bad = [], None, []
    for n, r in enumerate(body, start=2):
        try:
            d = parse_date(_cell(r, di).split(" ")[0] if len(_cell(r, di)) > 11 else _cell(r, di))
        except ValueError as e:
            bad.append(f"line {n}: {e}")
            continue
        last = d if last is None or d > last else last
        purpose = (_cell(r, pi) + " " + _cell(r, ni)).strip()
        if re.search(r"result", purpose, re.I):
            note = (f"Board meeting: {_cell(r, pi)[:120]} ; source={INBOX_SOURCE} file {name} ; "
                    f"imported {_today(now).isoformat()}").replace("=", ":").replace(";", ",")
            out.append(Event(d, "RESULTS", note, _cell(r, si).upper()))
    if bad:
        raise Rejected("; ".join(bad[:5]))
    through = covered_through or last
    if through is None:
        raise Rejected("no meeting dates in the file: coverage cannot be stated")
    for sym in watchlist:          # every company is in the export, so no row = no meeting announced
        out.append(Event(retrieved, "COVERAGE", f"source=NSE board meetings file {name.replace(';', ',').replace('=', '-')}; "
                                                f"covered_through={through.isoformat()}", sym))
    return out, {"results_events": sum(1 for e in out if e.type == "RESULTS"), "coverage_rows": len(watchlist),
                 "covered_through": through.isoformat()}


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
                covered_through: date | None = None) -> dict:
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
        events, info = board_meeting_events(text, name, retrieved, stocks, now, covered_through)
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
              covered_through: date | None = None) -> list[dict]:
    now = now or utcnow()
    inbox = settings.path(settings.data_status.inbox_dir)
    done, rejected = inbox / "done", inbox / "rejected"
    for p in (inbox, done, rejected):
        p.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(IST).strftime("%Y%m%d-%H%M%S")
    out = []
    for path in sorted(x for x in inbox.iterdir() if x.is_file() and x.suffix.lower() in (".csv", ".txt")):
        try:
            res = import_file(settings, store, manual, path, now, covered_through)
            shutil.move(str(path), str(done / f"{stamp}_{path.name}"))
            out.append({**res, "moved_to": f"done/{stamp}_{path.name}"})
        except (Rejected, OSError, ValueError) as e:
            target = rejected / f"{stamp}_{path.name}"
            shutil.move(str(path), str(target))
            target.with_name(target.name + ".reason.txt").write_text(f"{e}\n", encoding="utf-8")
            out.append({"file": path.name, "ok": False, "errors": [str(e)], "moved_to": f"rejected/{target.name}"})
    return out


def main(argv: list[str] | None = None) -> int:
    from .calendar import MarketCalendar
    ap = argparse.ArgumentParser(description="Import the files in data/inbox/ (NSE board meetings, corporate actions, "
                                             "FII/DII, RBI decisions)")
    ap.add_argument("--covered-through", help="board meetings: the last date the downloaded list covers (YYYY-MM-DD); "
                                              "default: the latest meeting date in the file")
    args = ap.parse_args(argv)
    settings = load_settings()
    store = Store(settings.db_path(), live=settings.is_live_feed)
    manual = ManualData(settings, store, MarketCalendar())
    manual.load_all()
    res = run_inbox(settings, store, manual, covered_through=parse_date(args.covered_through) if args.covered_through else None)
    print(json.dumps(res, indent=2) if res else f"no files in {settings.data_status.inbox_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
