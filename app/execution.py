"""Phase 6 step 13: execution realism.

For every signal (live or paper) an execution record per leg (`execution_snapshots`: signal_id, leg, moment,
source, time):
- SIGNAL and PLANNED_FILL (the next bar's open): the bid, ask, last price and market depth of each leg, with the
  quote's own timestamp and price status. A missing, stale or crossed quote is stored with that status and its
  fields as they were (None where nothing was quoted): never as zero and never filled in. Depth is stored when the
  feed sends it; today it does not (the Breeze quote stream is subscribed without depth, the chain has none).
- PAPER_FILL: the paper fill and the price it was taken from.
- ACTUAL_FILL: my fill and fill time per leg (from 'I took this trade'; phase 7 adds the contract-note price).

Slippage report: per leg, the paper fill and my fill against the mid and against the touch (the ask for a purchase,
the bid for a sale) at the planned fill, in rupees (x quantity) and as % of the reference price (the premium for an
option); grouped by stock and by strike distance from spot (`execution.strike_distance_bands_pct`), with the
spread width and the depth at the touch beside each group, the count, and "low sample" under `backtest.min_trades`.
Positive slippage = worse for me. Every figure comes from the stored records; a leg without a usable quote at the
planned fill is counted as "no quote", never estimated.
"""
from __future__ import annotations

import csv
import io
import json
import math
import statistics
from datetime import datetime

SIGNAL, PLANNED_FILL, PAPER_FILL, ACTUAL_FILL = "SIGNAL", "PLANNED_FILL", "PAPER_FILL", "ACTUAL_FILL"
MOMENTS = (SIGNAL, PLANNED_FILL, PAPER_FILL, ACTUAL_FILL)

SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_snapshots (
    signal_id TEXT NOT NULL, leg INTEGER NOT NULL, moment TEXT NOT NULL, source TEXT NOT NULL, at_utc TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER, strike REAL, right TEXT, expiry TEXT,
    status TEXT, bid REAL, ask REAL, last REAL, quote_time_utc TEXT, depth_json TEXT, spot REAL,
    price REAL, note TEXT, PRIMARY KEY (signal_id, leg, moment)
);
"""
_COLS = ["signal_id", "leg", "moment", "source", "at_utc", "symbol", "side", "quantity", "strike", "right", "expiry",
         "status", "bid", "ask", "last", "quote_time_utc", "depth_json", "spot", "price", "note"]


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def legs_of(sig) -> list[dict]:
    """leg index, side and identity of every leg of a signal (cash: one leg, index 0)."""
    if not sig.legs:
        return [{"leg": 0, "side": "BUY" if sig.direction == "LONG" else "SELL", "symbol": sig.symbol,
                 "strike": None, "right": None, "expiry": None}]
    return [{"leg": i, "side": x.action, "symbol": sig.symbol, "strike": x.strike, "right": x.right,
             "expiry": x.expiry.isoformat()} for i, x in enumerate(sig.legs)]


class ExecutionBook:
    def __init__(self, store, settings):
        self.s = settings
        self._db, self._lock = store._db, store._lock
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _put(self, row: dict) -> bool:
        """Insert once (a moment of a leg is recorded once: a restart never overwrites it)."""
        with self._lock:
            cur = self._db.execute(f"INSERT OR IGNORE INTO execution_snapshots ({','.join(_COLS)}) VALUES "
                                   f"({','.join('?' * len(_COLS))})", [row.get(c) for c in _COLS])
            self._db.commit()
            return cur.rowcount == 1

    def has(self, signal_id: str, moment: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM execution_snapshots WHERE signal_id=? AND moment=? LIMIT 1",
                                    (signal_id, moment)).fetchone() is not None

    def record_quotes(self, sig, moment: str, quotes, at: datetime, quantity: int | None, spot: float | None) -> None:
        """One row per leg with the quote as it was (app.pricing.LegQuote), whatever its status."""
        for leg, q in zip(legs_of(sig), quotes):
            self._put({**leg, "signal_id": sig.signal_id, "moment": moment, "source": q.source, "at_utc": at.isoformat(),
                       "quantity": quantity, "status": q.status, "bid": q.bid, "ask": q.ask, "last": q.last,
                       "quote_time_utc": q.quote_time.isoformat() if q.quote_time else None,
                       "depth_json": json.dumps(q.depth) if q.depth else None, "spot": spot, "note": q.note})

    def record_fill(self, sig_legs: list[dict], signal_id: str, moment: str, prices, at: datetime, quantity: int | None,
                    source: str, spot: float | None = None) -> None:
        for leg, p in zip(sig_legs, prices):
            self._put({**leg, "signal_id": signal_id, "moment": moment, "source": source, "at_utc": at.isoformat(),
                       "quantity": quantity, "price": p, "spot": spot})

    def rows(self, signal_id: str | None = None) -> list[dict]:
        q = f"SELECT {','.join(_COLS)} FROM execution_snapshots"
        with self._lock:
            rs = self._db.execute(q + (" WHERE signal_id=?" if signal_id else "") + " ORDER BY signal_id, leg, moment",
                                  (signal_id,) if signal_id else ()).fetchall()
        out = [dict(zip(_COLS, r)) for r in rs]
        for d in out:
            d["depth"] = json.loads(d.pop("depth_json")) if d.get("depth_json") else None
        return out

    # ---------------- slippage report ----------------
    def leg_rows(self) -> list[dict]:
        """One row per (signal, leg) that has a planned-fill record: the references and each fill's slippage."""
        by: dict[tuple, dict] = {}
        for r in self.rows():
            by.setdefault((r["signal_id"], r["leg"]), {})[r["moment"]] = r
        out = []
        for (sid, leg), m in sorted(by.items()):
            pf = m.get(PLANNED_FILL)
            if pf is None:
                continue
            side = pf["side"]
            usable = pf["status"] == "LIVE" and _ok(pf["bid"]) and _ok(pf["ask"]) and pf["ask"] >= pf["bid"]
            mid = (pf["bid"] + pf["ask"]) / 2 if usable else None
            touch = (pf["ask"] if side == "BUY" else pf["bid"]) if usable else None
            row = {"signal_id": sid, "leg": leg, "symbol": pf["symbol"], "side": side, "strike": pf["strike"],
                   "right": pf["right"], "quantity": pf["quantity"], "planned_fill_status": pf["status"],
                   "bid": pf["bid"], "ask": pf["ask"], "mid": mid, "touch": touch,
                   "spread": round(pf["ask"] - pf["bid"], 4) if usable else None,
                   "spread_pct_of_mid": round((pf["ask"] - pf["bid"]) / mid * 100, 4) if usable and mid else None,
                   "depth_at_touch": _depth_at_touch(pf.get("depth"), side), "spot": pf["spot"],
                   "band": band_of(pf["strike"], pf["spot"], self.s.execution.strike_distance_bands_pct),
                   "no_quote": not usable}
            for name, moment in (("paper", PAPER_FILL), ("actual", ACTUAL_FILL)):
                f = m.get(moment)
                price = f["price"] if f else None
                row[f"{name}_fill"] = price
                for ref_name, ref in (("mid", mid), ("touch", touch)):
                    if price is None or ref is None:
                        row[f"{name}_vs_{ref_name}"] = row[f"{name}_vs_{ref_name}_inr"] = row[f"{name}_vs_{ref_name}_pct"] = None
                        continue
                    slip = (price - ref) if side == "BUY" else (ref - price)
                    qty = (f.get("quantity") or pf["quantity"] or 0)
                    row[f"{name}_vs_{ref_name}"] = round(slip, 4)
                    row[f"{name}_vs_{ref_name}_inr"] = round(slip * qty, 2)
                    row[f"{name}_vs_{ref_name}_pct"] = round(slip / ref * 100, 4)
            out.append(row)
        return out

    def report(self) -> dict:
        rows = self.leg_rows()
        m = self.s.backtest.min_trades

        def group(key) -> list[dict]:
            groups: dict[str, list[dict]] = {}
            for r in rows:
                groups.setdefault(key(r), []).append(r)
            res = []
            for k in sorted(groups):
                g = groups[k]
                n_sig = len({r["signal_id"] for r in g})

                def mean(field):
                    vals = [r[field] for r in g if r[field] is not None]
                    return round(statistics.fmean(vals), 4) if vals else None

                def total(field):
                    vals = [r[field] for r in g if r[field] is not None]
                    return round(sum(vals), 2) if vals else None
                res.append({"group": k, "legs": len(g), "trades": n_sig, "no_quote": sum(1 for r in g if r["no_quote"]),
                            "low_sample": n_sig < m,
                            "paper_vs_mid_pct": mean("paper_vs_mid_pct"), "paper_vs_touch_pct": mean("paper_vs_touch_pct"),
                            "paper_vs_mid_inr": total("paper_vs_mid_inr"), "paper_vs_touch_inr": total("paper_vs_touch_inr"),
                            "actual_vs_mid_pct": mean("actual_vs_mid_pct"), "actual_vs_touch_pct": mean("actual_vs_touch_pct"),
                            "actual_vs_mid_inr": total("actual_vs_mid_inr"), "actual_vs_touch_inr": total("actual_vs_touch_inr"),
                            "actual_fills": sum(1 for r in g if r["actual_fill"] is not None),
                            "avg_spread": mean("spread"), "avg_spread_pct_of_mid": mean("spread_pct_of_mid"),
                            "avg_depth_at_touch": mean("depth_at_touch"),
                            "depth_note": None if any(r["depth_at_touch"] is not None for r in g)
                            else "depth not sent by the feed"})
            return res
        return {"by_stock": group(lambda r: r["symbol"]), "by_strike_band": group(lambda r: r["band"]),
                "legs": len(rows), "bands_pct": list(self.s.execution.strike_distance_bands_pct),
                "note": "Slippage = worse for you when positive (purchase: fill - reference; sale: reference - fill). "
                        "Reference = mid or touch (ask for a purchase, bid for a sale) at the planned fill. Computed from "
                        "the stored execution records; a leg without a usable quote is counted under no_quote."}

    def csv(self) -> str:
        rows = self.leg_rows()
        buf = io.StringIO()
        if not rows:
            return "no planned-fill records yet\n"
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()


def _depth_at_touch(depth: dict | None, side: str) -> float | None:
    """Quantity at the best price on the side a fill would meet (asks for a purchase, bids for a sale)."""
    if not depth:
        return None
    levels = depth.get("asks" if side == "BUY" else "bids") or []
    if not levels:
        return None
    q = levels[0].get("quantity") if isinstance(levels[0], dict) else None
    return float(q) if _ok(q) else None


def band_of(strike, spot, bands) -> str:
    """Strike distance from spot in % of spot, in the configured bands ("cash" for a share)."""
    if strike is None:
        return "cash"
    if not (_ok(spot) and _ok(strike)):
        return "spot unknown"
    d = abs(strike - spot) / spot * 100
    lo = 0.0
    for b in bands:
        if d < b:
            return f"{lo:g}-{b:g}%"
        lo = b
    return f"{lo:g}%+"
