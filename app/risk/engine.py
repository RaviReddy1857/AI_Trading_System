"""Phase 5 risk engine: the only component allowed to approve a signal. It can always say NO TRADE.

`decide(signal, account, market, ...)` returns a RiskDecision: APPROVED with the quantity or lots and
the rupee risk, or NO_TRADE with every reason code that applies. It is a pure function of its inputs:
no clock reads, no network, same input -> same output. Every rupee limit is a percentage of the
capital passed in, so changing account.capital_inr rescales everything.

Checks (all of them are evaluated; each failing one adds its code):
- inputs: capital (CAPITAL_UNKNOWN), positions and P&L (PORTFOLIO_UNKNOWN), cost rates, lot size,
  sector (DATA_MISSING / LOT_SIZE_UNKNOWN), margin of anything with a sold leg (MARGIN_UNKNOWN);
- kill switch (KILL_SWITCH): index price not LIVE, outside market hours, quarantined-tick spike;
- the signal's own price LIVE (DATA_STALE), not expired (SIGNAL_EXPIRED), regime allowed (OUTSIDE_REGIME);
- loss stops on realised + open P&L (DAILY_STOP, MONTHLY_PAUSE); drawdown (sizes halved; DRAWDOWN_PAUSE);
- sizing (ZERO_SIZE); capital pool and selling-spread count (RISK_BUDGET_USED); sector (SECTOR_LIMIT);
  correlation above risk.correlation_threshold = one position (CORRELATED_LIMIT, CORRELATION_UNKNOWN).

Stock option spreads (phase 9) follow the phase 9 specification: capital is advisory (a capital-fit
label and a warning, never a block), size within stock_option_selling.max_risk_per_position_pct, never
more lots than signalled, and the phase 9 portfolio limits (`app.options.portfolio.portfolio_check`).
Unknown data, unknown margin, a missing hedge, the kill switch, the loss stops and the drawdown pause
still block them: each of these only reduces risk.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..costs import MissingCostRates
from ..models import APPROVED, NO_TRADE, RiskDecision, Signal, unhedged_short_legs
from ..options.portfolio import Holding, portfolio_check
from ..quality import is_market_hours
from .limits import (CORRELATED_LIMIT, DATA_MISSING, KILL_SWITCH, correlated_group, drawdown_state,
                     kill_switch, loss_stops, sector_check)
from .portfolio import (CAPITAL_UNKNOWN, FITS, NEEDS_MORE_CAPITAL, PORTFOLIO_UNKNOWN, STOCK_SPREAD, TIGHT,
                        AccountState, capital_fit, committed_inr, known, paise, pool_check, position_problems,
                        selling_spread_check)
from .sizing import ZERO_SIZE, cash_quantity, option_lots

DATA_STALE, SIGNAL_EXPIRED, OUTSIDE_REGIME = "DATA_STALE", "SIGNAL_EXPIRED", "OUTSIDE_REGIME"
MARGIN_UNKNOWN, LOT_SIZE_UNKNOWN, UNHEDGED_SHORT = "MARGIN_UNKNOWN", "LOT_SIZE_UNKNOWN", "UNHEDGED_SHORT"
SIZE_HALVED = "SIZE_HALVED"

REASON_CODES = (CAPITAL_UNKNOWN, PORTFOLIO_UNKNOWN, DATA_MISSING, DATA_STALE, SIGNAL_EXPIRED, OUTSIDE_REGIME,
                KILL_SWITCH, "DAILY_STOP", "MONTHLY_PAUSE", "DRAWDOWN_PAUSE", ZERO_SIZE, "RISK_BUDGET_USED",
                "SECTOR_LIMIT", CORRELATED_LIMIT, "CORRELATION_UNKNOWN", MARGIN_UNKNOWN, LOT_SIZE_UNKNOWN,
                UNHEDGED_SHORT)


@dataclass(frozen=True)
class MarketState:
    """The market facts a decision needs at one moment. None = not known (blocks)."""
    now: datetime | None                 # UTC
    price_status: str | None             # the signal symbol's price status; only LIVE is usable
    regime: str | None
    market_open: bool | None
    index_status: str | None             # the index (regime.index_symbol) price status
    quarantined_recent: int | None       # quarantined ticks in the kill-switch window


def is_stock_spread(signal: Signal) -> bool:
    return signal.asset_class == "STOCK_OPTION" and signal.direction == "SPREAD"


def _f(x) -> float | None:
    return None if x is None else float(round(Decimal(str(x)), 2))


def decide(signal: Signal, account: AccountState, market: MarketState, settings, costs,
           corr: Callable[[str, str], float | None] | None, sectors: dict,
           allowed_regimes=None, corr_stock: Callable[[str, str], float | None] | None = None,
           universe=()) -> RiskDecision:
    """The risk decision for one signal. `costs`: app.costs.CostModel (None = rates not known).
    `corr(a, b)`: the 60-session return correlation known now (phase 5 rule); `corr_stock` the one the
    phase 9 portfolio limits use (default `corr`). `sectors`: symbol -> sector (watchlist tags).
    `allowed_regimes`: the strategy's regimes (None = the strategy already applied them). `universe`:
    the stocks the phase 9 correlated groups are built from."""
    r = settings.risk
    block: list[tuple[str, str]] = []
    warnings: list[str] = []
    facts: dict = {}

    def no(code: str, text: str) -> None:
        block.append((code, text))

    cap = account.capital_inr
    cap_ok = known(cap) and cap > 0
    if not cap_ok:
        no(CAPITAL_UNKNOWN, f"capital not known ({cap!r}): no limit can be computed")

    for text in kill_switch(settings.regime.index_symbol, market.index_status, market.quarantined_recent,
                            market.market_open, r.kill_switch):
        no(KILL_SWITCH, text)
    if market.price_status != "LIVE":
        no(DATA_STALE, f"{signal.symbol} price is {market.price_status or 'not known'}, not LIVE")
    if not isinstance(market.now, datetime) or market.now.tzinfo is None:
        no(DATA_MISSING, "decision time not known")
    elif market.now >= signal.valid_until:
        no(SIGNAL_EXPIRED, f"valid until {signal.valid_until.isoformat()}")
    if allowed_regimes is not None and market.regime not in allowed_regimes:
        no(OUTSIDE_REGIME, f"regime {market.regime or 'UNKNOWN'} is not in {sorted(allowed_regimes)}")
    if costs is None:
        no(DATA_MISSING, "cost rates not known: the cost-inclusive risk cannot be computed")

    problems = position_problems(account.positions)
    for text in problems[:3]:
        no(PORTFOLIO_UNKNOWN, text)
    positions = list(account.positions) if not problems else []

    if cap_ok and not problems:
        stops, f = loss_stops(cap, account.realised_today_inr, account.realised_month_inr, account.positions,
                              r.daily_loss_stop_pct, r.monthly_loss_pause_pct)
        facts["loss_stops"] = f
        for code, text in stops:
            no(code, text)
    dd, factor, dd_code, f = drawdown_state(account.equity_curve, account.flows, r.drawdown_halve_pct,
                                            r.drawdown_pause_pct)
    facts["drawdown"] = f
    if dd_code == PORTFOLIO_UNKNOWN:
        no(PORTFOLIO_UNKNOWN, f["why"])
    elif dd_code:
        no(dd_code, f"drawdown {f['drawdown_pct']:.2f}% from peak equity (deposits excluded) reached "
                    f"{r.drawdown_pause_pct:g}%: new signals paused")
    if factor < 1:
        warnings.append(f"{SIZE_HALVED}: drawdown {f['drawdown_pct']:.2f}% reached {r.drawdown_halve_pct:g}%, "
                        "every size is halved")

    out = {"quantity": None, "lots": None, "risk": None, "profit": None, "loss": None, "costs": None,
           "margin": None, "fit": None}
    if cap_ok and costs is not None:
        try:
            if signal.asset_class == "CASH_EQUITY":
                _cash(signal, settings, costs, cap, factor, positions, problems, corr, sectors, no, out, facts)
            elif is_stock_spread(signal):
                _stock_spread(signal, settings, costs, cap, factor, positions, problems, corr_stock or corr, sectors,
                              account.history_verified, universe, no, warnings, out, facts)
            else:
                _option(signal, settings, costs, cap, factor, positions, problems, corr, sectors, no, out, facts)
        except MissingCostRates as e:
            no(DATA_MISSING, f"cost rates missing: {e}")

    codes = tuple(dict.fromkeys(c for c, _ in block))
    reasons = tuple(f"{c}: {t}" for c, t in block)
    qty = out["quantity"]
    if not codes and (qty is None or qty <= 0):
        codes, reasons = (ZERO_SIZE,), (f"{ZERO_SIZE}: no whole size fits the risk budget",)
    # last guard: whatever happened above, an approval never carries more risk than the budget
    if not codes:
        limit = _budget(signal, settings, cap, factor)
        if out["risk"] is None or Decimal(str(out["risk"])) > limit:
            codes, reasons = ("RISK_BUDGET_USED",), (f"RISK_BUDGET_USED: risk {out['risk']} above Rs {limit}",)
    status = NO_TRADE if codes else APPROVED
    risk = out["risk"]
    return RiskDecision(
        status=status, reason_codes=codes,
        quantity=qty if status == APPROVED else None, lots=out["lots"] if status == APPROVED else None,
        risk_inr=_f(risk) if status == APPROVED else None,
        risk_pct=round(float(risk) / cap * 100, 3) if status == APPROVED else None,
        est_profit_inr=_f(out["profit"]) if status == APPROVED else None,
        est_loss_inr=_f(out["loss"]) if status == APPROVED else None,
        costs_inr=_f(out["costs"]) if status == APPROVED else None,
        margin_inr=_f(out["margin"]), capital_fit=out["fit"], warnings=tuple(warnings), reasons=reasons,
        size_factor=factor, facts={**facts, "would_be": {k: (float(v) if isinstance(v, Decimal) else v)
                                                         for k, v in out.items()}})


def _budget(signal: Signal, settings, cap: float, factor: float) -> Decimal:
    """The per-position risk budget in rupees for this kind of signal, after the drawdown factor."""
    if signal.asset_class == "CASH_EQUITY":
        pct = settings.risk.max_loss_per_trade_pct
    elif is_stock_spread(signal):
        pct = settings.stock_option_selling.max_risk_per_position_pct
    else:
        pct = settings.risk.max_loss_per_option_pct
    return paise(Decimal(str(cap)) * Decimal(str(pct)) / 100 * Decimal(str(factor)))


def _group(signal, positions, problems, corr, settings, no) -> Decimal:
    """Risk already held in the proposed symbol's correlated group (0 when the positions are unknown:
    the decision is blocked anyway)."""
    if problems:
        return Decimal(0)
    c = corr if corr is not None else (lambda a, b: None)
    code, text, group, held = correlated_group(signal.symbol, positions, c, settings.risk.correlation_threshold)
    if code:
        no(code, text)
    return held


def _cash(signal, settings, costs, cap, factor, positions, problems, corr, sectors, no, out, facts) -> None:
    r = settings.risk
    seg = "intraday_equity" if signal.horizon == "INTRADAY" else "delivery_equity"
    costs.require(seg)
    long = signal.direction == "LONG"
    entry = Decimal(str(signal.entry))

    def round_trip(q: int, exit_px: float) -> Decimal:
        if q <= 0:
            return Decimal(0)
        ev, xv = entry * q, Decimal(str(exit_px)) * q
        buy, sell = (ev, xv) if long else (xv, ev)
        return costs.equity(seg, buy, sell).total + paise((ev + xv) * costs.slip_pct(seg))

    held = _group(signal, positions, problems, corr, settings, no)
    q, f = cash_quantity(cap, r.max_loss_per_trade_pct, settings.backtest.position_cap_pct, signal.entry, signal.stop,
                         lambda k: round_trip(k, signal.stop), factor, float(held))
    facts["sizing"] = {**f, "held_in_correlated_group_inr": float(held)}
    if q <= 0:
        alone, _ = cash_quantity(cap, r.max_loss_per_trade_pct, settings.backtest.position_cap_pct, signal.entry,
                                 signal.stop, lambda k: round_trip(k, signal.stop), factor)
        if held > 0 and alone > 0:
            no(CORRELATED_LIMIT, f"Rs {held:,.2f} of risk is already held in correlated positions; together they "
                                 f"count as one position and the budget is used")
        else:
            no(ZERO_SIZE, f"not one share fits: budget Rs {f['budget_inr']:,.2f}, Rs {f['risk_per_share']:g} a share "
                          f"to the stop, position cap Rs {f['cap_value_inr']:,.2f}")
        return
    per_share = abs(entry - Decimal(str(signal.stop)))
    c_stop = round_trip(q, signal.stop)
    out.update(quantity=q, risk=per_share * q + c_stop, loss=per_share * q + c_stop, costs=c_stop,
               profit=abs(Decimal(str(signal.target)) - entry) * q - round_trip(q, signal.target),
               margin=entry * q)
    if problems:
        return
    code, text, pf = pool_check(cap, positions, out["margin"], r.uncommitted_cash_pct)
    out["fit"] = pf["capital_fit"]
    facts["pool"] = pf
    if code:
        no(code, text)
    code, text = sector_check(signal.symbol, positions, sectors, r.max_trades_per_sector)
    if code:
        no(code, text)


def _legs_known(signal, no) -> bool:
    legs = signal.legs or ()
    if not legs:
        no(DATA_MISSING, "an option signal without legs")
        return False
    if not all(getattr(x, "lot_size", None) and x.lot_size > 0 for x in legs):
        no(LOT_SIZE_UNKNOWN, "lot size not known for every leg")
        return False
    if unhedged_short_legs(legs):
        no(UNHEDGED_SHORT, "a sold option has no bought option of the same right and expiry")
        return False
    return True


def _margin(signal, structure, pay, n, no) -> Decimal | None:
    """Cash or margin needed for n lots: a bought option needs its premium; anything with a sold leg
    needs the user's margin entry per lot (never assumed)."""
    if structure == "LONG_OPTION":
        return -pay.net_premium_inr
    per_lot = (signal.details or {}).get("margin_per_lot_inr")
    if not known(per_lot) or per_lot <= 0:
        no(MARGIN_UNKNOWN, "no current margin entry for one lot of this structure (enter it from ICICI's margin "
                           "calculator)")
        return None
    return paise(Decimal(str(per_lot)) * n)


def _estimates(signal, structure, pay, n, out) -> None:
    shares = Decimal(n * signal.legs[0].lot_size)
    entry, target, stop = (Decimal(str(x)) for x in (signal.entry, signal.target, signal.stop))
    out["risk"] = pay.max_loss_inr
    out["costs"] = pay.costs_inr
    if structure in ("CREDIT_SPREAD", "IRON_CONDOR"):
        out["profit"] = (entry - target) * shares - pay.costs_inr
        out["loss"] = pay.max_loss_inr
    elif structure == "LONG_OPTION":
        out["profit"] = (target - entry) * shares - pay.costs_inr
        out["loss"] = (entry - stop) * shares + pay.costs_inr
    else:                                         # debit spread: the most it can lose
        out["profit"] = None
        out["loss"] = pay.max_loss_inr


def _option(signal, settings, costs, cap, factor, positions, problems, corr, sectors, no, out, facts) -> None:
    r = settings.risk
    if not _legs_known(signal, no):
        return
    costs.require("options")
    held = _group(signal, positions, problems, corr, settings, no)
    budget = _budget(signal, settings, cap, factor)
    try:
        n, pay, structure = option_lots(signal.legs, costs, float(budget - held))
    except ValueError as e:
        no(DATA_MISSING, f"option structure not sized: {e}")
        return
    facts["sizing"] = {"budget_inr": float(budget), "held_in_correlated_group_inr": float(held), "lots": n,
                       "structure": structure, "max_loss_one_lot_inr": None if n else float(pay.max_loss_inr)}
    if n <= 0:
        alone, _, _ = option_lots(signal.legs, costs, float(budget))
        if held > 0 and alone > 0:
            no(CORRELATED_LIMIT, f"Rs {held:,.2f} of risk is already held in correlated positions; together they "
                                 f"count as one position and the budget is used")
        else:
            no(ZERO_SIZE, f"one lot's max loss Rs {pay.max_loss_inr:,.2f} is above the budget Rs {budget:,.2f}")
        return
    out.update(lots=n, quantity=n * signal.legs[0].lot_size)
    _estimates(signal, structure, pay, n, out)
    out["margin"] = _margin(signal, structure, pay, n, no)
    if problems or out["margin"] is None:
        return
    code, text, pf = pool_check(cap, positions, out["margin"], r.uncommitted_cash_pct)
    out["fit"] = pf["capital_fit"]
    facts["pool"] = pf
    if code:
        no(code, text)
    selling = signal.asset_class == "INDEX_OPTION" and structure in ("CREDIT_SPREAD", "IRON_CONDOR")
    code, text = selling_spread_check(positions, selling, r.max_open_selling_spreads)
    if code:
        no(code, text)
    code, text = sector_check(signal.symbol, positions, sectors, r.max_trades_per_sector)
    if code:
        no(code, text)


def _stock_spread(signal, settings, costs, cap, factor, positions, problems, corr, sectors, history_verified,
                  universe, no, warnings, out, facts) -> None:
    if not _legs_known(signal, no):
        return
    costs.require("options")
    budget = _budget(signal, settings, cap, factor)
    signalled = max(x.lots for x in signal.legs)
    try:
        n, pay, structure = option_lots(signal.legs, costs, float(budget), max_lots=signalled)
    except ValueError as e:
        no(DATA_MISSING, f"option structure not sized: {e}")
        return
    facts["sizing"] = {"budget_inr": float(budget), "lots": n, "signalled_lots": signalled, "structure": structure}
    if n <= 0:
        no(ZERO_SIZE, f"one lot's max loss Rs {pay.max_loss_inr:,.2f} is above the budget Rs {budget:,.2f}")
        return
    out.update(lots=n, quantity=n * signal.legs[0].lot_size)
    _estimates(signal, structure, pay, n, out)
    out["margin"] = _margin(signal, structure, pay, n, no)
    if problems:
        return
    if out["margin"] is not None:
        label, shortfall, pf = capital_fit(cap, committed_inr(positions), out["margin"],
                                           settings.risk.uncommitted_cash_pct)
        out["fit"] = label
        facts["capital_fit"] = {**pf, "shortfall_inr": shortfall,
                                "recommended_capital_inr": settings.stock_option_selling.backtest.capital_inr}
        if label == NEEDS_MORE_CAPITAL:
            warnings.append(f"{NEEDS_MORE_CAPITAL}: margin Rs {out['margin']:,.2f} is above the free capital; "
                            f"shortfall Rs {shortfall:,.2f} (capital is advisory for stock option selling)")
        elif label == TIGHT:
            warnings.append("TIGHT_CAPITAL: the margin fits but breaks the uncommitted-cash rule "
                            f"({settings.risk.uncommitted_cash_pct:g}% kept free); advisory only")
    holdings = [Holding(p.symbol, float(p.risk_inr)) for p in positions if p.kind == STOCK_SPREAD]
    code, text, pf = portfolio_check(signal.symbol, float(pay.max_loss_inr), holdings, sectors,
                                     corr if corr is not None else (lambda a, b: None),
                                     settings.stock_option_selling.portfolio, cap, history_verified=history_verified,
                                     universe=universe)
    facts["phase9_portfolio"] = pf
    if code:
        no(code, text)


# ---------------- live service ----------------
class RiskService:
    """Gathers the live inputs for the risk engine: capital from config.yaml, kill-switch inputs from the
    hub and the store, deposits from capital_log. Open positions and P&L come from the phase 6 journal;
    until it exists they are NOT KNOWN, so every live decision is NO_TRADE (PORTFOLIO_UNKNOWN)."""

    def __init__(self, settings, store, hub, calendar):
        self.s, self.store, self.hub, self.calendar = settings, store, hub, calendar

    def _status_of(self, symbol: str, now: datetime) -> str | None:
        return next((row["status"] for row in self.hub.snapshot(now) if row["symbol"] == symbol), None)

    def market_state(self, symbol: str, now: datetime, regime: str | None = None) -> MarketState:
        ks = self.s.risk.kill_switch
        since = (now - timedelta(minutes=ks.quarantine_window_minutes)).astimezone(timezone.utc)
        return MarketState(now=now, price_status=self._status_of(symbol, now), regime=regime,
                           market_open=is_market_hours(now, self.calendar.holidays),
                           index_status=self._status_of(self.s.regime.index_symbol, now),
                           quarantined_recent=self.store.quality_event_count_since(since))

    def correlations(self, source: str):
        """60-session daily-return correlations from the stored daily bars with the phase 5 window
        (`app.options.shortlist.store_correlations`, completed sessions only): call `.at(day, a, b)`."""
        from ..options.shortlist import store_correlations
        r = self.s.risk
        return store_correlations(self.s, self.store, source, r.correlation_window, r.correlation_min_overlap)

    def account_state(self) -> AccountState:
        return AccountState(capital_inr=self.s.account.capital_inr, positions=None, realised_today_inr=None,
                            realised_month_inr=None, equity_curve=None, flows=tuple(self.store.capital_flows()))

    def status(self, now: datetime) -> dict:
        """For GET /api/risk: the limits in rupees, the kill switch, and what is not known yet."""
        s, r = self.s, self.s.risk
        cap = s.account.capital_inr
        m = self.market_state(s.regime.index_symbol, now)
        ks = kill_switch(s.regime.index_symbol, m.index_status, m.quarantined_recent, m.market_open, r.kill_switch)
        acct = self.account_state()
        problems = position_problems(acct.positions)
        log = self.store.capital_log()
        return {
            "as_of_utc": now.isoformat(), "capital_inr": cap, "account_mode": s.account.mode,
            "limits_pct": {k: v for k, v in r.model_dump().items() if k != "kill_switch"},
            "limits_inr": {**s.limits_in_rupees(),
                           "position_value_cap": s.rupees(s.backtest.position_cap_pct),
                           "max_risk_per_stock_spread": s.rupees(s.stock_option_selling.max_risk_per_position_pct),
                           "capital_pool_usable": s.rupees(100 - r.uncommitted_cash_pct)},
            "kill_switch": {"on": bool(ks), "reasons": ks, "index": s.regime.index_symbol,
                            "index_status": m.index_status, "market_open": m.market_open,
                            "quarantined_recent": m.quarantined_recent, **r.kill_switch.model_dump()},
            "portfolio": {"known": not problems, "problems": problems,
                          "note": "open positions and P&L come from the phase 6 journal; until then every live "
                                  "signal is NO_TRADE (PORTFOLIO_UNKNOWN)"},
            "capital_log": {"entries": len(log),
                            "deposits_inr": round(sum(e["amount"] for e in log if e["type"] == "DEPOSIT"), 2),
                            "withdrawals_inr": round(sum(e["amount"] for e in log if e["type"] == "WITHDRAWAL"), 2),
                            "note": "deposits and withdrawals are never counted as profit or loss"},
            "reason_codes": list(REASON_CODES),
        }
