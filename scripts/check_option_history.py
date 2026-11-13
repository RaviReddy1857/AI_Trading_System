"""Phase 4 step 0: does Breeze provide historical option prices?

Read-only. Uses only BreezeFeed.option_historical_raw (get_historical_data_v2 with product type
"options"). Tries past monthly contracts of NIFTY, BANKNIFTY and a few stocks going back year by
year, one recent NIFTY weekly, and 5-minute bars, then prints what came back: rows, first and last
bar, the fields in a row (is open interest there? bid/ask?).

Expiry dates of expired contracts are not in the instrument list, so each month is probed with
candidate dates (last Tuesday / Thursday / Wednesday of the month and the day before each); the
strike is the multiple of a step nearest the stored daily close shortly before expiry.

Run from the project folder:  python scripts/check_option_history.py
Output is also saved to data/diagnostics/option_history_probe.json (not tracked).
"""
from __future__ import annotations

import calendar as _cal
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from app.config import ROOT, load_settings  # noqa: E402
from app.feeds.breeze_feed import BreezeFeed  # noqa: E402
from app.history import bar_date, call_with_timeout  # noqa: E402
from app.store import Store  # noqa: E402

PAUSE = 0.7
MONTHS = [(2026, 10), (2026, 9), (2026, 6), (2026, 1), (2025, 9), (2025, 6), (2024, 12), (2024, 6),
          (2023, 6), (2022, 6), (2021, 6), (2020, 6), (2019, 6), (2018, 6), (2017, 6)]
UNDERLYINGS = [("NIFTY", (100,)), ("BANKNIFTY", (500, 100)), ("RELIANCE", (100, 50, 20, 10)),
               ("HDFCBANK", (100, 50, 20, 10)), ("INFY", (100, 50, 20, 10)), ("SBIN", (50, 20, 10, 5))]


def last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month, _cal.monthrange(year, month)[1])
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def candidates(year: int, month: int) -> list[date]:
    out: list[date] = []
    for wd in (1, 3, 2):                       # Tuesday, Thursday, Wednesday
        d = last_weekday(year, month, wd)
        for x in (d, d - timedelta(days=1)):
            if x not in out and x.weekday() < 5:
                out.append(x)
    return out


def iso(d: date, hh: int = 6) -> str:
    return f"{d.isoformat()}T{hh:02d}:00:00.000Z"


def main() -> int:
    load_dotenv(ROOT / ".env")
    s = load_settings()
    store = Store(s.path(s.storage.db_path_live), live=True)
    feed = BreezeFeed(s.watchlist)
    codes = {i.symbol: i.breeze_code for i in s.watchlist}
    results: list[dict] = []
    calls = 0

    def ask(sym, expiry, strike, right, interval, a: date, b: date) -> dict:
        nonlocal calls
        if calls:
            time.sleep(PAUSE)
        calls += 1
        try:
            resp = call_with_timeout(lambda: feed.option_historical_raw(
                codes[sym], iso(expiry), strike, right, interval, iso(a, 1), iso(b, 12)), 60)
        except Exception as e:      # noqa: BLE001
            resp = {"Success": None, "Error": f"{type(e).__name__}: {e}"}
        rows = resp.get("Success") or []
        out = {"symbol": sym, "expiry": expiry.isoformat(), "strike": strike, "right": right, "interval": interval,
               "from": a.isoformat(), "to": b.isoformat(), "rows": len(rows), "error": resp.get("Error"),
               "status": resp.get("Status")}
        if rows:
            out.update(first=rows[0].get("datetime"), last=rows[-1].get("datetime"), fields=sorted(rows[0]),
                       sample=rows[len(rows) // 2])
            vols = [r.get("volume") for r in rows]
            ois = [r.get("open_interest") for r in rows]
            out["rows_with_volume"] = sum(1 for v in vols if v not in (None, "", 0, "0"))
            out["rows_with_oi"] = sum(1 for v in ois if v not in (None, "", 0, "0"))
        results.append(out)
        print(f"{sym:10s} {expiry} {strike:>8} {right:4s} {interval:8s} rows={out['rows']:5d} "
              f"{out.get('first', '')} .. {out.get('last', '')} {out['error'] or ''}", flush=True)
        return out

    def spot(sym: str, before: date) -> float | None:
        bars = store.history_bars("bars_1d", sym, "breeze",
                                  before=datetime.combine(before, datetime.min.time(), timezone.utc), limit=1)
        return bars[0].close if bars and (before - bar_date(bars[0])).days < 15 else None

    found: dict[str, list[dict]] = {}
    for sym, steps in UNDERLYINGS:
        misses = 0
        for y, m in MONTHS:
            got = None
            for exp in candidates(y, m):
                px = spot(sym, exp - timedelta(days=7))
                if px is None:
                    print(f"{sym:10s} {exp} no stored daily close near expiry; skipped")
                    break
                for step in steps:
                    strike = round(px / step) * step
                    r = ask(sym, exp, strike, "call", "1day", exp - timedelta(days=45), exp)
                    if r["rows"]:
                        got = r
                        break
                    if r["error"] and "session" in str(r["error"]).lower():
                        print("Breeze session not valid: update BREEZE_SESSION_TOKEN in .env and rerun")
                        return 2
                if got:
                    break
            if got:
                found.setdefault(sym, []).append(got)
                misses = 0
            else:
                misses += 1
                if misses >= 3:
                    break

    # the newest contract found per underlying: put side, 5-minute and 1-minute bars
    for sym, items in found.items():
        g = items[0]
        exp = date.fromisoformat(g["expiry"])
        ask(sym, exp, g["strike"], "put", "1day", exp - timedelta(days=45), exp)
        ask(sym, exp, g["strike"], "call", "5minute", exp - timedelta(days=6), exp)
        ask(sym, exp, g["strike"], "call", "1minute", exp - timedelta(days=1), exp)
        # how early in a contract's life do bars start? (far out-of-the-money, long window)
        ask(sym, exp, g["strike"], "call", "1day", exp - timedelta(days=200), exp)
    # oldest contract found per underlying: 5-minute
    for sym, items in found.items():
        g = items[-1]
        exp = date.fromisoformat(g["expiry"])
        ask(sym, exp, g["strike"], "call", "5minute", exp - timedelta(days=6), exp)
    # NIFTY weekly: the two Tuesdays / Thursdays before the newest monthly found
    if "NIFTY" in found:
        base = date.fromisoformat(found["NIFTY"][0]["expiry"])
        for back in (7, 14):
            exp = base - timedelta(days=back)
            px = spot("NIFTY", exp - timedelta(days=3))
            if px:
                ask("NIFTY", exp, round(px / 100) * 100, "call", "1day", exp - timedelta(days=30), exp)
                ask("NIFTY", exp, round(px / 100) * 100, "put", "5minute", exp - timedelta(days=5), exp)

    out = ROOT / "data" / "diagnostics"
    out.mkdir(parents=True, exist_ok=True)
    (out / "option_history_probe.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    print(f"\n{calls} requests. Oldest contract with data per underlying:")
    for sym, items in found.items():
        print(f"  {sym:10s} newest {items[0]['expiry']}  oldest {items[-1]['expiry']}  ({len(items)} of {len(MONTHS)} months tried)")
    any_row = next((r for r in results if r["rows"]), None)
    if any_row:
        print("fields in a row:", any_row["fields"])
        print("sample row:", any_row["sample"])
    else:
        print("NO option history returned for any contract tried.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
