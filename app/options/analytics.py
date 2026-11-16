"""Option intelligence from a stored chain snapshot, computed by code:
- implied volatility and delta per strike (Black-Scholes on the bid/ask mid price)
- IV rank against the past year (index: India VIX; stock: stored at-the-money implied volatility)
- put-call ratio (put open interest / call open interest)
- change in open interest (the exchange's day change, summed per side)
- the highest-OI call strike (resistance) and put strike (support)

`OptionDesk` puts these together with the instrument list, margins and the three option strategies
for the Options page and GET /api/options/{underlying}. It only reads; nothing is sent to the broker.
"""
from __future__ import annotations

from datetime import date, datetime, time

from ..config import Settings
from ..costs import CostModel, MissingCostRates
from ..history import bar_date
from ..models import utcnow
from ..quality import IST
from ..store import Store
from . import build_option_strategies
from .base import ChainQuotes, ExpiryInfo, OptionContext, OptionQuote, index_option_budget
from .chain import stored_quotes, wanted_specs
from .greeks import days_to_expiry, iv_and_delta, iv_rank, years_to_expiry
from .instruments import InstrumentBook
from .margin import MarginBook
from .option_history import OptionHistory, stock_iv_series


def put_call_ratio(quotes: list[OptionQuote]) -> float | None:
    """Put open interest / call open interest. None if either side is unknown or zero."""
    calls = [q.oi for q in quotes if q.right == "CE" and q.oi is not None]
    puts = [q.oi for q in quotes if q.right == "PE" and q.oi is not None]
    if not calls or not puts or sum(calls) <= 0:
        return None
    return round(sum(puts) / sum(calls), 3)


def highest_oi_strike(quotes: list[OptionQuote], right: str) -> float | None:
    rows = [q for q in quotes if q.right == right and q.oi is not None and q.oi > 0]
    return max(rows, key=lambda q: q.oi).strike if rows else None


def oi_change(quotes: list[OptionQuote]) -> dict:
    """Day change in open interest per side, and the strike with the largest build-up on each side."""
    out: dict = {}
    for right, name in (("CE", "calls"), ("PE", "puts")):
        rows = [q for q in quotes if q.right == right and q.oi_change is not None]
        out[name] = round(sum(q.oi_change for q in rows)) if rows else None
        top = max(rows, key=lambda q: q.oi_change, default=None)
        out[f"largest_increase_{name}"] = top.strike if top is not None and top.oi_change > 0 else None
    return out


def atm_iv_pct(quotes: list[OptionQuote], spot: float, t: float | None, rate: float, dividend: float) -> float | None:
    """Implied volatility (%) at the money: the mean of the call and put at the strike nearest spot."""
    strikes = sorted({q.strike for q in quotes}, key=lambda k: abs(k - spot))
    for k in strikes[:3]:
        ivs = []
        for q in quotes:
            if q.strike == k:
                iv, _ = iv_and_delta(q.mid, spot, k, t, rate, dividend, q.right)
                if iv is not None:
                    ivs.append(iv)
        if len(ivs) == 2:
            return round(sum(ivs) / 2 * 100, 2)
    return None


def chain_table(quotes: list[OptionQuote], spot: float | None, t: float | None, rate: float, dividend: float,
                around: int = 12) -> list[dict]:
    """Rows per strike near the money with bid, ask, last, OI, IV and delta for the call and the put."""
    by: dict[float, dict] = {}
    for q in quotes:
        iv, delta = iv_and_delta(q.mid, spot, q.strike, t, rate, dividend, q.right) if spot else (None, None)
        by.setdefault(q.strike, {"strike": q.strike})[q.right.lower()] = {
            "bid": q.bid, "ask": q.ask, "last": q.last, "oi": q.oi, "oi_change": q.oi_change,
            "iv_pct": None if iv is None else round(iv * 100, 2), "delta": None if delta is None else round(delta, 3)}
    strikes = sorted(by)
    if spot and strikes:
        i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
        strikes = strikes[max(0, i - around):i + around + 1]
    return [by[k] for k in strikes]


class OptionDesk:
    def __init__(self, settings: Settings, store: Store, source: str, spot_fn, regime_fn, now_fn=utcnow):
        """spot_fn(symbol) -> price if its status is LIVE, else None. regime_fn() -> regime name."""
        self.s, self.store, self.source = settings, store, source
        self.spot_fn, self.regime_fn, self.now_fn = spot_fn, regime_fn, now_fn
        self.book = InstrumentBook(store, source)
        self.strategies = build_option_strategies(settings)
        self.kinds = {i.symbol: i.kind for i in settings.watchlist}

    def underlyings(self) -> list[str]:
        return [i.symbol for i in self.s.watchlist if self.s.option_underlying(i.symbol) is not None]

    # ---- implied volatility measure and its rank ----
    def iv_measure(self, sym: str, today: date, current_atm_iv: float | None) -> dict:
        o = self.s.options
        if self.kinds.get(sym) == "index":
            ref = o.iv_reference_symbol
            hist = [b.close for b in self.store.history_bars("bars_1d", ref, self.source, limit=o.iv_rank_lookback_days + 5)
                    if bar_date(b) < today][-o.iv_rank_lookback_days:]
            now_value = self.spot_fn(ref)
            basis = f"{ref} (India VIX) against its last {len(hist)} daily closes"
        else:
            closes = {bar_date(b): b.close for b in self.store.history_bars("bars_1d", sym, self.source, limit=400)}
            cfg = self.s.option_underlying(sym)
            hist_src = OptionHistory(self.store, self.source)
            monthly = [e for e, k in hist_src.expiries(self.s.regime.index_symbol) if k == "monthly"]
            series = stock_iv_series(hist_src, sym, monthly, closes, cfg.backtest_strike_step if cfg else None,
                                     o.risk_free_rate_pct / 100, (cfg.dividend_yield_pct if cfg else 0) / 100)
            hist = [v for d, v in series.items() if d < today][-o.iv_rank_lookback_days:]
            now_value = current_atm_iv
            basis = (f"at-the-money implied volatility against {len(hist)} stored days "
                     f"(python -m app.options.option_history --iv-history {sym} downloads them)")
        rank = iv_rank(now_value, hist, o.iv_rank_min_history_days)
        return {"value_pct": now_value, "rank": None if rank is None else round(rank, 1), "history_days": len(hist),
                "basis": basis, "note": "" if rank is not None else
                f"IV rank unknown: needs a current value and at least {o.iv_rank_min_history_days} days of history"}

    # ---- everything for one underlying ----
    def view(self, sym: str, now: datetime | None = None) -> dict:
        now = now or self.now_fn()
        today = now.astimezone(IST).date()
        o = self.s.options
        cfg = self.s.option_underlying(sym)
        kind = self.kinds.get(sym)
        rate, div = o.risk_free_rate_pct / 100, (cfg.dividend_yield_pct if cfg else 0) / 100
        spot = self.spot_fn(sym)
        specs = self.book.specs(sym, today, now)
        loaded = wanted_specs(self.book, self.s, sym, today, now)
        chains, quotes_by_expiry, problems = [], {}, []
        if not specs:
            problems.append("LOT_SIZE_UNKNOWN: no current contracts for this symbol in the stored instrument list"
                            + ("" if self.book.as_of() else " (the list has not been loaded yet)"))
        atm_now = None
        for spec in loaded:
            quotes, as_of = stored_quotes(self.store, self.source, sym, spec.expiry)
            age = (now - as_of).total_seconds() if as_of else None
            stale = age is None or age > o.chain.max_age_seconds
            t = years_to_expiry(now, spec.expiry)
            if quotes and not stale:
                quotes_by_expiry[spec.expiry] = quotes
            iv_here = atm_iv_pct(quotes, spot, t, rate, div) if quotes and spot else None
            if atm_now is None and iv_here is not None and not stale and days_to_expiry(now, spec.expiry) >= 5:
                atm_now = iv_here
            chains.append({
                "expiry": spec.expiry.isoformat(), "kind": spec.kind, "lot_size": spec.lot_size,
                "days_to_expiry": days_to_expiry(now, spec.expiry),
                "as_of_utc": as_of.isoformat() if as_of else None, "age_seconds": None if age is None else round(age),
                "stale": stale, "rows_stored": len(quotes),
                "put_call_ratio": put_call_ratio(quotes),
                "resistance_highest_oi_call": highest_oi_strike(quotes, "CE"),
                "support_highest_oi_put": highest_oi_strike(quotes, "PE"),
                "oi_change": oi_change(quotes), "atm_iv_pct": iv_here,
                "table": chain_table(quotes, spot, t, rate, div)})
        iv = self.iv_measure(sym, today, atm_now)
        margins = MarginBook.from_store(self.store, o.margin.max_age_days)
        ideas = []
        try:
            costs = CostModel(self.s.costs)
            costs.require("options")
        except MissingCostRates as e:
            costs = None
            problems.append(f"cost rates missing: {e}")
        regime = self.regime_fn()
        if specs and not quotes_by_expiry:
            problems.append(f"DATA_STALE: no option chain stored in the last {o.chain.max_age_seconds} s for {sym} "
                            f"(index chains refresh in the background during market hours; stocks: "
                            f"python -m app.options.chain {sym})")
        if costs is not None and cfg is not None and not quotes_by_expiry:
            ideas = [{"strategy_id": st.id, "strategy_version": st.version, "label": st.status_label,
                      "alerts": st.alerts_enabled, "status": "NO_TRADE", "signals": [],
                      "reasons": ["DATA_MISSING: no fresh option chain (bid/ask unknown)" if specs
                                  else "LOT_SIZE_UNKNOWN: lot size and expiries not in the instrument list"]}
                     for st in self.strategies]
        elif costs is not None and cfg is not None:
            ctx = OptionContext(
                underlying=sym, kind=kind, asof=now, regime=regime, spot=spot,
                expiries=tuple(ExpiryInfo(s.expiry, s.kind, s.lot_size) for s in specs if s.expiry in quotes_by_expiry),
                quotes=ChainQuotes(spot or 0.0, quotes_by_expiry), costs=costs, capital_inr=self.s.account.capital_inr,
                rate=rate, dividend=div, status="LIVE" if spot else "STALE", iv_reference_pct=iv["value_pct"],
                iv_rank=iv["rank"], trigger=None, allowed_kinds=tuple(cfg.expiries),
                margin=lambda structure, width: margins.lookup(sym, structure, width, today), require_margin=True,
                historical=False, max_spread_pct=o.chain.max_spread_pct, min_oi_lots=o.chain.min_open_interest_lots,
                search_steps=o.backtest.strike_search_steps,
                option_risk_budget_inr=index_option_budget(self.s) if kind == "index" else None)
            for st in self.strategies:
                sigs = st.evaluate(ctx)
                reasons = list(st.last_no_trade)
                if st.id == "long_option" and not sigs and any(r.startswith("NO_TRIGGER") for r in reasons):
                    reasons = ["NO_TRIGGER: option buying needs a live intraday or swing signal on the underlying; "
                               "the live signal loop arrives in phase 6"]
                ideas.append({"strategy_id": st.id, "strategy_version": st.version, "label": st.status_label,
                              "alerts": st.alerts_enabled, "status": "SIGNAL" if sigs else "NO_TRADE",
                              "reasons": reasons, "signals": [s.to_dict() for s in sigs]})
        return {
            "underlying": sym, "kind": kind, "feed": self.source, "as_of_utc": now.isoformat(),
            "spot": spot, "spot_status": "LIVE" if spot else "not LIVE (no option signal)", "regime": regime,
            "instruments_as_of_utc": self.book.as_of().isoformat() if self.book.as_of() else None,
            "instruments_fresh": self.book.is_fresh(now),
            "expiries": [{"expiry": s.expiry.isoformat(), "kind": s.kind, "lot_size": s.lot_size,
                          "days_to_expiry": days_to_expiry(now, s.expiry)} for s in specs],
            "iv": iv, "chains": chains, "ideas": ideas, "problems": problems,
            "margins": [{"underlying": m.underlying, "structure": m.structure, "width": m.width,
                         "margin_per_lot_inr": m.margin_per_lot_inr, "as_of": m.as_of.isoformat(),
                         "usable": margins.lookup(m.underlying, m.structure, m.width, today) is not None}
                        for m in margins.entries() if m.underlying == sym],
            "notes": ["Numbers are for ONE lot; quantity is decided by the risk engine (phase 5).",
                      "Implied volatility and delta: Black-Scholes on the bid/ask mid price, risk-free rate "
                      f"{o.risk_free_rate_pct:g}% (an assumption in config.yaml).",
                      "Research only: this app cannot place orders."],
        }


def session_close(d: date) -> datetime:
    return datetime.combine(d, time(15, 30), IST)
