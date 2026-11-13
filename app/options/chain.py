"""Option chains: NIFTY (weekly and monthly), BANKNIFTY (monthly) and watchlist stocks (monthly),
through BreezeFeed.option_chain() (read-only). Each row keeps strike, right, expiry, bid, ask, last,
open interest, its change, volume and the snapshot time, in `option_chain`.

Quality gate for option quotes: a row with a missing, zero or negative bid or ask, or with the ask
below the bid, is quarantined (quality_events, reason BAD_OPTION_QUOTE) and never stored, so no
strategy can use it. A stored snapshot older than options.chain.max_age_seconds is stale.

Which expiries are loaded comes from the instrument list (app/options/instruments.py): for an
underlying with weekly contracts the two nearest expiries, plus the two nearest monthly expiries.
Index chains are refreshed in the background; stock chains on request:

    python -m app.options.chain                # index underlyings
    python -m app.options.chain RELIANCE TCS   # these symbols

Sim mode builds a FAKE chain from the simulated price (Black-Scholes), stored with source 'sim'.
"""
from __future__ import annotations

import logging
import math
import time as _time
from collections.abc import Callable
from datetime import date, datetime

from ..config import Instrument, Settings
from ..history import _num
from ..models import utcnow
from ..quality import IST
from ..store import Store
from .base import OptionQuote
from .greeks import bs_price, years_to_expiry
from .instruments import ContractSpec, InstrumentBook

log = logging.getLogger("option_chain")

BAD_OPTION_QUOTE = "BAD_OPTION_QUOTE"
BREEZE_RIGHT = {"CE": "call", "PE": "put"}


def parse_chain_rows(rows: list[dict], right: str) -> tuple[list[dict], list[tuple[str, dict]]]:
    """Breeze option-chain rows for one right -> (usable rows, quarantined rows with the reason)."""
    good: dict[float, dict] = {}
    bad: list[tuple[str, dict]] = []
    for raw in rows or []:
        strike = _num(raw.get("strike_price"))
        if strike is None or strike <= 0:
            bad.append(("no strike", raw))
            continue
        bid, ask = _num(raw.get("best_bid_price")), _num(raw.get("best_offer_price"))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            bad.append(("missing bid/ask", raw))
            continue
        if ask < bid:
            bad.append(("crossed bid/ask", raw))
            continue
        last, oi = _num(raw.get("ltp")), _num(raw.get("open_interest"))
        if oi is not None and oi < 0:
            bad.append(("negative open interest", raw))
            continue
        good[strike] = {"strike": strike, "right": right, "bid": bid, "ask": ask,
                        "last": last if last is not None and last > 0 else None, "oi": oi,
                        "oi_change": _num(raw.get("chnge_oi")), "volume": _num(raw.get("total_quantity_traded")),
                        "spot": _num(raw.get("spot_price"))}
    return [good[k] for k in sorted(good)], bad


class BreezeChainSource:
    name = "breeze"
    remote = True

    def __init__(self, feed):
        self._feed = feed          # BreezeFeed; the SDK stays inside breeze_feed.py

    def fetch(self, inst: Instrument, spec: ContractSpec, right: str) -> list[dict]:
        return self._feed.option_chain(inst.breeze_code, spec.expiry.isoformat() + "T06:00:00.000Z", BREEZE_RIGHT[right])


class SimChainSource:
    """FAKE option chain for sim mode: Black-Scholes around the simulated price. NOT market data."""
    name = "sim"
    remote = False

    def __init__(self, spot: Callable[[str], float | None], iv_pct: Callable[[], float | None], rate: float,
                 now: Callable[[], datetime] = utcnow):
        self._spot, self._iv, self._rate, self._now = spot, iv_pct, rate, now

    def fetch(self, inst: Instrument, spec: ContractSpec, right: str) -> list[dict]:
        s = self._spot(inst.symbol)
        t = years_to_expiry(self._now(), spec.expiry)
        if not s or not t:
            return []
        iv = (self._iv() or 15.0) / 100.0 * (1.0 if inst.kind == "index" else 1.6)
        rows = []
        for k in spec.strikes:
            p = bs_price(s, k, t, self._rate, 0.0, iv, right) or 0.0
            half = max(0.05, round(p * 0.01, 2))
            near = math.exp(-(((k - s) / (0.03 * s)) ** 2))
            oi = round(spec.lot_size * (300 + 4000 * near + (1500 if (k / (spec.strikes[1] - spec.strikes[0])) % 5 == 0 else 0)))
            rows.append({"strike_price": k, "best_bid_price": round(p - half, 2), "best_offer_price": round(p + half, 2),
                         "ltp": round(p, 2), "open_interest": oi, "chnge_oi": round(oi * 0.04 * (1 if right == "PE" else -1)),
                         "total_quantity_traded": oi // 3, "spot_price": round(s, 2)})
        return rows


def wanted_specs(book: InstrumentBook, settings: Settings, symbol: str, today: date,
                 now: datetime | None = None) -> list[ContractSpec]:
    """The expiries whose chain is loaded for a symbol: the two nearest of any kind when the symbol
    trades weekly contracts, and the two nearest monthly ones."""
    cfg = settings.option_underlying(symbol)
    if cfg is None:
        return []
    specs = book.specs(symbol, today, now)
    out: dict[date, ContractSpec] = {}
    if "weekly" in cfg.expiries:
        for s in specs[:2]:
            out[s.expiry] = s
    if "monthly" in cfg.expiries:
        for s in [x for x in specs if x.kind == "monthly"][:2]:
            out[s.expiry] = s
    return [out[e] for e in sorted(out)]


class ChainLoader:
    def __init__(self, settings: Settings, store: Store, source, book: InstrumentBook,
                 sleep: Callable[[float], None] = _time.sleep):
        self.settings, self.store, self.source, self.book = settings, store, source, book
        self._sleep = sleep
        self._requests = 0

    def default_symbols(self) -> list[str]:
        return list(self.settings.options.underlyings)

    def load(self, symbols: list[str] | None = None, now: datetime | None = None) -> dict:
        """Fetch and store the chains of `symbols` (default: the index underlyings). One failing
        symbol does not stop the others."""
        now = now or utcnow()
        today = now.astimezone(IST).date()
        by_symbol = {i.symbol: i for i in self.settings.watchlist}
        summary: dict = {}
        for sym in symbols or self.default_symbols():
            inst = by_symbol.get(sym)
            specs = wanted_specs(self.book, self.settings, sym, today, now) if inst else []
            if not specs:
                summary[sym] = {"error": "no contracts in the instrument list (lot size and expiries unknown)"}
                continue
            for spec in specs:
                key = f"{sym}:{spec.expiry.isoformat()}"
                try:
                    saved = rejected = 0
                    for right in ("CE", "PE"):
                        if getattr(self.source, "remote", False) and self._requests:
                            self._sleep(self.settings.options.chain.request_pause_seconds)
                        self._requests += 1
                        good, bad = parse_chain_rows(self.source.fetch(inst, spec, right), right)
                        for reason, raw in bad:
                            self.store.save_quality_event(
                                now, sym, BAD_OPTION_QUOTE, _num(raw.get("ltp")), _num(raw.get("best_bid_price")),
                                _num(raw.get("best_offer_price")),
                                f"{self.source.name}:chain:{spec.expiry}:{raw.get('strike_price')}{right}:{reason}")
                        saved += self.store.save_option_chain(self.source.name, [
                            (sym, spec.expiry.isoformat(), g["strike"], right, now.isoformat(), g["bid"], g["ask"],
                             g["last"], g["oi"], g["oi_change"], g["volume"], g["spot"]) for g in good])
                        rejected += len(bad)
                    summary[key] = {"saved": saved, "quarantined": rejected}
                except Exception as e:      # noqa: BLE001 - network or SDK errors must not kill the app
                    log.exception("option chain failed for %s", key)
                    summary[key] = {"error": f"{type(e).__name__}: {e}"}
        return summary


def stored_quotes(store: Store, source: str, underlying: str, expiry: date) -> tuple[list[OptionQuote], datetime | None]:
    """(quotes of the newest stored snapshot, its time)."""
    rows = store.latest_option_chain(source, underlying, expiry)
    quotes = [OptionQuote(r["strike"], r["right"], r["bid"], r["ask"], r["last"], r["oi"], r["oi_change"], r["volume"])
              for r in rows]
    return quotes, (rows[0]["as_of"] if rows else None)


def main(argv: list[str] | None = None) -> int:
    import sys

    from dotenv import load_dotenv

    from ..config import ROOT, load_settings
    from ..feeds.breeze_feed import BreezeFeed
    from .instruments import refresh_from_broker

    args = [a.upper() for a in (sys.argv[1:] if argv is None else argv)]
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    unknown = sorted(set(args) - {i.symbol for i in settings.watchlist})
    if unknown:
        raise SystemExit(f"not in the watchlist: {unknown}")
    store = Store(settings.path(settings.storage.db_path_live), live=True)
    print(f"instrument list: {refresh_from_broker(store, settings.watchlist)} contract expiries stored")
    loader = ChainLoader(settings, store, BreezeChainSource(BreezeFeed(settings.watchlist)), InstrumentBook(store, "breeze"))
    for key, res in loader.load(args or None).items():
        print(f"{key:28s} {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
