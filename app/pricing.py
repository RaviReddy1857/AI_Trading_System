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

from .quality import IST

LIVE, STALE, MISSING, CROSSED = "LIVE", "STALE", "MISSING", "CROSSED"
# the note of a planned-fill quote that is not the first quote (its capture was lost): excluded from slippage
FIRST_QUOTE_UNAVAILABLE = "first quote unavailable"


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
    substitute: bool = False         # not the first quote at or after the planned fill (its capture was lost)

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
                "depth": self.depth, "note": self.note, "substitute": self.substitute}


# no current quote at all (silent before the settlement alerts start), against a quote received and rejected
INVALID = "INVALID"
REJECTED = "REJECTED"               # the quality gate rejected the stock's ticks and no valid one came within the grace
AUCTION = "AUCTION"                 # the closing auction: no current tradable spot (absent, never rejected)
ABSENT = frozenset({MISSING, STALE, "NO_DATA", "CLOSED", AUCTION})


@dataclass(frozen=True)
class SpotReading:
    """The underlying's last price as the live reader saw it: `price` (None unless LIVE and usable), the quote's
    `status` (LIVE, MISSING, STALE, CLOSED, NO_DATA, CROSSED, INVALID) and `why` it was not used."""
    price: float | None
    status: str
    why: str = ""

    @property
    def rejected(self) -> bool:
        """A quote was there but could not be used (crossed, malformed): never silent, unlike an absent one."""
        return self.price is None and self.status not in ABSENT


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

    def first_cash(self, symbol: str, after: datetime, now: datetime) -> LegQuote:
        """The first quote received at or after `after` (the next bar's first quote), as it was then; MISSING until
        one has arrived."""
        q = self.hub.first_quote(symbol, after)
        if q is None:
            return LegQuote(MISSING, None, None, None, None, "feed tick",
                            note="no tick at or after the planned fill yet")
        status = q["status"]
        if status == "LIVE" and q["bid"] is not None and q["ask"] is not None and q["bid"] > q["ask"] > 0:
            status = CROSSED
        sub = bool(q.get("substitute"))
        note = (f"{FIRST_QUOTE_UNAVAILABLE} (not captured: no quotes were being received at the planned fill); "
                f"substitute: the first quote received after, at {q['at'].isoformat()}") if sub             else "first quote at or after the planned fill"
        return LegQuote(status if status != "NO_DATA" else MISSING, q["bid"], q["ask"], q["last"], q["at"],
                        "feed tick", note=note, substitute=sub)

    def first_options(self, underlying: str, legs, after: datetime, until: datetime) -> list[LegQuote]:
        """Every leg's quote from the earliest chain snapshot taken in [after, until] that has a valid bid and ask
        (both positive, not crossed) for EVERY leg: one snapshot for the whole fill. `until`: now or the signal's
        valid-until, whichever is earlier. Until such a snapshot exists every leg is MISSING (with why)."""
        expiries = {x.expiry for x in legs}
        if len(expiries) != 1:
            return [LegQuote(MISSING, None, None, None, None, "option chain",
                             note="legs on different expiries are never filled from one chain snapshot") for _ in legs]
        snaps = self.store.option_chain_contracts_between(self.source, underlying, next(iter(expiries)),
                                                          [(x.strike, x.right) for x in legs], after, until)
        seen = []
        for as_of, rows in snaps:
            quotes = []
            for x in legs:
                r = next((y for y in rows if abs(y["strike"] - x.strike) < 1e-6 and y["right"] == x.right), None)
                if r is None or not (_pos(r["bid"]) and _pos(r["ask"])) or r["bid"] > r["ask"]:
                    quotes = None
                    break
                quotes.append(LegQuote(LIVE, r["bid"], r["ask"], r["last"], as_of, "option chain",
                                       note="earliest chain snapshot at or after the planned fill with every leg quoted"))
            if quotes is not None:
                if seen:
                    quotes = [LegQuote(q.status, q.bid, q.ask, q.last, q.quote_time, q.source,
                                       note=q.note + f" (skipped {len(seen)} earlier snapshot(s) without every leg)")
                              for q in quotes]
                return quotes
            seen.append(as_of)
        why = (f"{len(seen)} chain snapshot(s) at or after the planned fill, none with a bid and ask for every leg"
               if seen else "no chain snapshot at or after the planned fill yet")
        return [LegQuote(MISSING, None, None, None, seen[-1] if seen else None, "option chain", note=why)
                for _ in legs]

    def spot(self, symbol: str, now: datetime) -> float | None:
        return self.spot_reading(symbol, now).price

    def spot_reading(self, symbol: str, now: datetime) -> "SpotReading":
        """The underlying's LIVE last price, or None WITH the quote's status and why it was not used: an absent quote
        (no tick, stale, market closed, the closing auction) is told apart from a quote that was received and rejected
        (crossed, a last price that is not a finite number above 0).

        A tick the hub's quality gate rejected today (crossed, non-finite, quarantined) with no valid tick since makes
        the quote not LIVE, even when the last good tick is recent: STALE while the last valid tick (or, with none
        today, the first rejection) is younger than `stock_option_selling.settlement.rejected_quote_grace_minutes`,
        then REJECTED (the settlement alert's URGENT). Outside continuous trading (CLOSED, AUCTION) it is absent."""
        q = self.cash(symbol, now)
        raw = self.hub.quote(symbol, now)
        rej_at = raw.get("rejected_at") if isinstance(raw, dict) else None
        if rej_at is not None and q.status not in ("CLOSED", AUCTION) \
                and rej_at.astimezone(IST).date() == now.astimezone(IST).date():
            valid_at = raw.get("at")
            if valid_at is None or valid_at.astimezone(IST).date() != now.astimezone(IST).date():
                valid_at = None
            ref = valid_at or raw.get("rejected_since") or rej_at
            waited = (now - ref).total_seconds() / 60.0
            grace = self.s.stock_option_selling.settlement.rejected_quote_grace_minutes
            what = (f"tick rejected by the quality gate ({raw.get('rejected_why') or 'reason not recorded'}) at "
                    f"{rej_at.astimezone(IST):%H:%M:%S} IST; no valid tick "
                    + (f"since {valid_at.astimezone(IST):%H:%M:%S} IST" if valid_at else "today"))
            if waited < grace:
                return SpotReading(None, STALE, f"{what}: STALE within the {grace:g}-minute grace")
            return SpotReading(None, REJECTED, f"{what}: {waited:.0f} minute(s), at or past the {grace:g}-minute grace")
        if q.usable and _pos(q.last):
            return SpotReading(q.last, LIVE, "")
        if q.status in ABSENT:
            return SpotReading(None, q.status, q.note or f"quote {q.status}")
        if q.status == LIVE:
            return SpotReading(None, INVALID, f"last price {q.last!r} is not a finite price above 0")
        return SpotReading(None, q.status, q.note or f"quote {q.status}")

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
