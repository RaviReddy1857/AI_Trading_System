"""Phase 6 quotes for open positions, paper fills and execution records.

Cash prices come from the quality-gated hub; option prices from the newest stored option chain snapshot,
usable only while it is younger than `options.chain.max_age_seconds` and the row has a bid and an ask.
A quote that is missing, stale or crossed is reported with that status: it is never turned into zero and
never filled in from another source. Only a LIVE quote may move a paper trade, trigger an exit call or mark
a position.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

LIVE, STALE, MISSING, CROSSED = "LIVE", "STALE", "MISSING", "CROSSED"


def _pos(x) -> bool:
    import math
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


@dataclass(frozen=True)
class LegQuote:
    """One instrument's quote at one moment, with where it came from and how usable it is."""
    status: str                      # LIVE | STALE | MISSING | CROSSED | NO_DATA | CLOSED
    bid: float | None
    ask: float | None
    last: float | None
    quote_time: datetime | None      # the quote's own timestamp (tick receive time, or chain snapshot time)
    source: str                      # "feed tick" | "option chain"
    depth: dict | None = None        # best five bids and asks with quantities, when the feed sends them
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.status == LIVE

    @property
    def mid(self) -> float | None:
        if _pos(self.bid) and _pos(self.ask) and self.ask >= self.bid:
            return (self.bid + self.ask) / 2
        return None

    def touch(self, side: str) -> float | None:
        """The price a market order would meet: the ask to buy, the bid to sell (None when not quoted)."""
        v = self.ask if side == "BUY" else self.bid
        return v if _pos(v) else None

    def fill_price(self, side: str, cash: bool) -> float | None:
        """Price for a paper fill or a mark: options at the touch (sell at the bid, buy at the ask); cash at the
        touch when the feed sends bid and ask, else the last trade. None unless the quote is LIVE."""
        if not self.usable:
            return None
        t = self.touch(side)
        if t is not None:
            return t
        return self.last if cash and _pos(self.last) else None

    def to_dict(self) -> dict:
        return {"status": self.status, "bid": self.bid, "ask": self.ask, "last": self.last,
                "quote_time_utc": self.quote_time.isoformat() if self.quote_time else None, "source": self.source,
                "depth": self.depth, "note": self.note}


class Marker:
    """Quotes for cash symbols (hub) and option contracts (stored chain)."""

    def __init__(self, settings, store, hub, source: str):
        self.s, self.store, self.hub, self.source = settings, store, hub, source

    def cash(self, symbol: str, now: datetime) -> LegQuote:
        q = self.hub.quote(symbol, now)
        if q["at"] is None:
            return LegQuote(MISSING if q["status"] != "CLOSED" else "CLOSED", None, None, None, None, "feed tick",
                            note="no tick received" + ("" if q["status"] != "CLOSED" else " (market closed)"))
        status = q["status"]
        if status == "LIVE" and q["bid"] is not None and q["ask"] is not None and q["bid"] > q["ask"] > 0:
            status = CROSSED
        return LegQuote(status if status != "NO_DATA" else MISSING, q["bid"], q["ask"], q["last"], q["at"], "feed tick",
                        note="the feed does not send market depth")

    def spot(self, symbol: str, now: datetime) -> float | None:
        q = self.cash(symbol, now)
        return q.last if q.usable and _pos(q.last) else None

    def option(self, underlying: str, expiry: date, strike: float, right: str, now: datetime) -> LegQuote:
        from .options.chain import stored_quotes
        quotes, as_of = stored_quotes(self.store, self.source, underlying, expiry)
        q = next((x for x in quotes if abs(x.strike - strike) < 1e-6 and x.right == right), None)
        if q is None:
            return LegQuote(MISSING, None, None, None, as_of, "option chain",
                            note="contract not in the stored chain" if quotes else "no chain stored for this expiry")
        age = (now - as_of).total_seconds() if as_of else None
        if age is None or age > self.s.options.chain.max_age_seconds:
            return LegQuote(STALE, q.bid, q.ask, q.last, as_of, "option chain",
                            note=f"chain snapshot {'age unknown' if age is None else f'{age / 60:.0f} minutes old'}")
        if not (_pos(q.bid) and _pos(q.ask)):
            return LegQuote(MISSING, q.bid, q.ask, q.last, as_of, "option chain", note="no bid and ask")
        if q.bid > q.ask:
            return LegQuote(CROSSED, q.bid, q.ask, q.last, as_of, "option chain", note="bid above ask")
        return LegQuote(LIVE, q.bid, q.ask, q.last, as_of, "option chain", note="the chain does not carry depth")

    def leg_quotes(self, pos, now: datetime) -> list[LegQuote]:
        if not pos.is_option:
            return [self.cash(pos.symbol, now)]
        return [self.option(pos.symbol, x.expiry, x.strike, x.right, now) for x in pos.legs]

    def marks(self, pos, now: datetime) -> tuple[list[float] | None, str]:
        """Closing prices of a position now (each leg at the price closing it would meet), or (None, why)."""
        qs = self.leg_quotes(pos, now)
        if not pos.is_option:
            side = "SELL" if pos.direction == "LONG" else "BUY"
            p = qs[0].fill_price(side, cash=True)
            return ([p], "") if p is not None else (None, f"{pos.symbol} price {qs[0].status}")
        out = []
        for leg, q in zip(pos.legs, qs):
            p = q.fill_price("SELL" if leg.action == "BUY" else "BUY", cash=False)
            if p is None:
                return None, f"{leg.strike:g} {leg.right} {leg.expiry:%d-%b}: quote {q.status}"
            out.append(p)
        return out, ""
