"""Phase 9: the daily shortlist for stock option selling, built after the close.

For every watchlist stock with option contracts in the instrument list it applies the rules of
app/options/stock_selling.py to the stock's stored daily bars and its stored option chain of that
session, scores the valid setups (IV rank, trend clarity, room to support/resistance, liquidity)
and stores the ranked list in the `shortlist` table. The page /stock-option-selling and
GET /api/stock-option-selling/shortlist only read that table.

A stock is left off the list, with the reason shown, when anything it needs is missing or stale:
no daily bar for the session, no option chain stored that day (a failed chain request is never
guessed), no lot size, an illiquid strike, or results / a board meeting / a corporate action before
expiry. A shortlist row is a candidate, not a signal: signal cards and alerts come after phase 6.

Universe: the watchlist stocks (the Nifty 50). The broker's other F&O stocks have no price history
or live prices in this system yet, so they cannot be assessed.

Run after the close:  python -m app.options.shortlist            # loads the stock chains (paced), then builds
                      python -m app.options.shortlist --stored   # builds from the chains already stored today
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timezone

from ..config import Settings
from ..costs import CostModel
from ..history import bar_date
from ..indicators import after_last_break
from ..manual_data import Event
from ..models import utcnow
from ..quality import IST
from ..store import Store
from .analytics import OptionDesk, atm_iv_pct
from .base import ChainQuotes, ExpiryInfo, OptionContext
from .chain import stored_quotes, wanted_specs
from .greeks import days_to_expiry, years_to_expiry
from .instruments import InstrumentBook
from .margin import MarginBook
from .stock_selling import StockOptionSelling, build_shortlist, build_stock_selling

log = logging.getLogger("shortlist")


def universe(settings: Settings, book: InstrumentBook, today: date, now: datetime) -> list[str]:
    """Watchlist stocks with unexpired option contracts in a fresh instrument list."""
    return [i.symbol for i in settings.watchlist if i.kind == "stock" and book.specs(i.symbol, today, now)]


def session_date(settings: Settings, store: Store, source: str) -> date | None:
    """The newest session the index has a daily bar for."""
    bars = store.history_bars("bars_1d", settings.regime.index_symbol, source, limit=1)
    return bar_date(bars[-1]) if bars else None


def stock_context(settings: Settings, store: Store, source: str, book: InstrumentBook, desk: OptionDesk,
                  costs: CostModel, margins: MarginBook, sym: str, session: date, now: datetime,
                  regime: str = "UNKNOWN") -> tuple[OptionContext | None, str | None]:
    """(context for one stock at the close of `session`, None) or (None, why it cannot be assessed)."""
    o = settings.options
    cfg = settings.option_underlying(sym)
    bars = store.history_bars("bars_1d", sym, source, limit=400)
    if not bars or bar_date(bars[-1]) != session:
        return None, f"DATA_STALE: no daily bar for {session:%d-%b-%Y} (run python -m app.history)"
    spot = bars[-1].close
    asof = datetime.combine(session, time(15, 30), IST).astimezone(timezone.utc)
    rate, div = o.risk_free_rate_pct / 100.0, (cfg.dividend_yield_pct if cfg else 0.0) / 100.0
    chains, expiries, atm_now = {}, [], None
    for spec in wanted_specs(book, settings, sym, session, now):
        quotes, as_of = stored_quotes(store, source, sym, spec.expiry)
        if not quotes or as_of is None or as_of.astimezone(IST).date() != session:
            continue                                   # no chain from this session: unknown, never guessed
        chains[spec.expiry] = quotes
        expiries.append(ExpiryInfo(spec.expiry, spec.kind, spec.lot_size))
        if atm_now is None and days_to_expiry(asof, spec.expiry) >= 5:
            atm_now = atm_iv_pct(quotes, spot, years_to_expiry(asof, spec.expiry), rate, div)
    if not chains:
        return None, (f"NO_CHAIN: no option chain stored for {session:%d-%b-%Y} (the request failed or was not made); "
                      f"the stock is not eligible today")
    iv = desk.iv_measure(sym, session, atm_now)
    sos = settings.stock_option_selling
    events = tuple(Event(e["date"], e["type"], e["note"] or "", e["symbol"] or "")
                   for e in store.events_between(session, max(chains)))
    return OptionContext(
        underlying=sym, kind="stock", asof=asof, regime=regime, spot=spot, expiries=tuple(expiries),
        quotes=ChainQuotes(spot, chains), costs=costs, capital_inr=settings.account.capital_inr, rate=rate,
        dividend=div, status="LIVE", iv_reference_pct=iv["value_pct"], iv_rank=iv["rank"],
        allowed_kinds=("monthly",), margin=lambda structure, width: margins.lookup(sym, structure, width, session),
        require_margin=True, historical=False, max_spread_pct=sos.max_bid_ask_spread_pct,
        min_oi_lots=sos.min_open_interest, search_steps=o.backtest.strike_search_steps, events=events,
        daily=tuple(after_last_break(bars, settings.history.max_overnight_gap_pct))), None


def run_shortlist(settings: Settings, store: Store, source: str, now: datetime | None = None,
                  strategy: StockOptionSelling | None = None, save: bool = True) -> dict:
    """Assess the universe at the close of the newest stored session and store the ranked list."""
    now = now or utcnow()
    session = session_date(settings, store, source)
    if session is None:
        return {"session_date": None, "rows": [], "error": "no daily bars stored for the index"}
    strategy = strategy or build_stock_selling(settings)
    book = InstrumentBook(store, source)
    costs = CostModel(settings.costs)
    costs.require("options")
    margins = MarginBook.from_store(store, settings.options.margin.max_age_days)
    desk = OptionDesk(settings, store, source, lambda s: None, lambda: "UNKNOWN", lambda: now)
    contexts, left_out = [], []
    stocks = universe(settings, book, session, now)
    for sym in stocks:
        ctx, why = stock_context(settings, store, source, book, desk, costs, margins, sym, session, now)
        if ctx is None:
            left_out.append({"stock": sym, "expiry": None, "side": None, "rank": None, "status": "EXCLUDED",
                             "score": None, "scores": {}, "reasons": [why]})
        else:
            contexts.append(ctx)
    rows = build_shortlist(strategy, contexts) + left_out
    if not stocks:
        rows = [{"stock": "-", "expiry": None, "side": None, "rank": None, "status": "EXCLUDED", "score": None,
                 "scores": {}, "reasons": ["LOT_SIZE_UNKNOWN: the instrument list is missing or older than 5 days"]}]
    if save:
        store.save_shortlist(session, rows, now)
    return {"session_date": session.isoformat(), "rows": rows, "stocks": len(stocks)}


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from ..config import ROOT, load_settings
    ap = argparse.ArgumentParser(description="Daily shortlist for stock option selling (after the close)")
    ap.add_argument("--stored", action="store_true", help="do not ask Breeze: use the option chains already stored today")
    ap.add_argument("--stocks", nargs="*", help="load chains for these stocks only (default: every watchlist stock)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    source = "breeze" if settings.is_live_feed else "sim"
    store = Store(settings.db_path(), live=settings.is_live_feed)
    if not args.stored and settings.is_live_feed:
        from ..feeds.breeze_feed import BreezeFeed
        from .chain import BreezeChainSource, ChainLoader
        from .instruments import refresh_from_broker
        load_dotenv(ROOT / ".env")
        print(f"instrument list: {refresh_from_broker(store, settings.watchlist)} contract expiries stored")
        book = InstrumentBook(store, source)
        now = utcnow()
        want = [s.upper() for s in args.stocks] if args.stocks else universe(settings, book, now.astimezone(IST).date(), now)
        loader = ChainLoader(settings, store, BreezeChainSource(BreezeFeed(settings.watchlist)), book)
        failed = {k: v["error"] for k, v in loader.load(want).items() if "error" in v}
        print(f"option chains loaded for {len(want)} stocks; {len(failed)} failed (those stocks are not eligible today)")
        for k, v in list(failed.items())[:10]:
            print(f"  {k}: {v}")
    out = run_shortlist(settings, store, source)
    if out.get("error"):
        print(out["error"])
        return 1
    top = [r for r in out["rows"] if r["status"] == "SHORTLISTED"]
    print(f"shortlist for {out['session_date']}: {len(top)} candidate(s) of {out['stocks']} stocks")
    for r in top:
        print(f"  {r['rank']:2d} {r['stock']:12s} {'bull put' if r['side'] == 'PE' else 'bear call'}  {r['expiry']}  "
              f"score {r['score']:.2f}  {r['reasons'][-1]}")
    why = {}
    for r in out["rows"]:
        if r["status"] != "SHORTLISTED" and r["reasons"]:
            why.setdefault(r["reasons"][-1].split(":")[0], []).append(r["stock"])
    for k, v in sorted(why.items()):
        print(f"  not listed - {k}: {len(v)} ({', '.join(v[:8])}{' ...' if len(v) > 8 else ''})")
    print("open http://127.0.0.1:8000/stock-option-selling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
