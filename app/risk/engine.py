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
- one approval per underlying, exposure (BULLISH/BEARISH/NEUTRAL) and session (SAME_SYMBOL_ALREADY_APPROVED,
  EXPOSURE_UNKNOWN; risk.block_same_symbol_same_session);
- sizing (ZERO_SIZE); capital pool and selling-spread count (RISK_BUDGET_USED); sector (SECTOR_LIMIT);
  correlation above risk.correlation_threshold = one position (CORRELATED_LIMIT, CORRELATION_UNKNOWN).

Stock option spreads (phase 9) follow the phase 9 specification: every rupee figure of their decision is a
percentage of the separate stock-option capital `stock_option_selling.backtest.capital_inr` (Rs 10,00,000; the
capital of every Phase 9 output, `stock_selling.phase9_settings`), never of account.capital_inr; for my real book the
capital fit BLOCKS since 18-Nov-2026 (the user's policy: RESERVE_BREACH when the 25% reserve would be committed,
MARGIN_EXCEEDS_CAPITAL above the capital; paper books keep their own rules), size within stock_option_selling.max_risk_per_position_pct, never
more lots than signalled, and the phase 9 portfolio limits (`app.options.portfolio.portfolio_check`).
Unknown data, unknown margin, a missing hedge, the kill switch, the loss stops and the drawdown pause
still block them: each of these only reduces risk.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ..costs import MissingCostRates
from ..models import APPROVED, NO_TRADE, RiskDecision, Signal, unhedged_short_legs
from ..options.portfolio import Holding, portfolio_check
from ..quality import IST, is_market_hours, TICK_REASONS
from ..session import DERIVATIVES, segment_for
from ..session import hours as session_hours
from .limits import (CORRELATED_LIMIT, DATA_MISSING, KILL_SWITCH, correlated_group, drawdown_state,
                     kill_switch, loss_stops, sector_check)
from .portfolio import (CAPITAL_UNKNOWN, FITS, NEEDS_MORE_CAPITAL, PORTFOLIO_UNKNOWN, STOCK_SPREAD, TIGHT,
                        EXPOSURE_UNKNOWN, SAME_SYMBOL_ALREADY_APPROVED, AccountState, capital_fit, committed_inr,
                        exposure_of, known, paise, pool_check, position_problems, same_symbol_check,
                        selling_spread_check)
from .sizing import ZERO_SIZE, cash_quantity, option_lots

DATA_STALE, SIGNAL_EXPIRED, OUTSIDE_REGIME = "DATA_STALE", "SIGNAL_EXPIRED", "OUTSIDE_REGIME"
MARGIN_UNKNOWN, LOT_SIZE_UNKNOWN, UNHEDGED_SHORT = "MARGIN_UNKNOWN", "LOT_SIZE_UNKNOWN", "UNHEDGED_SHORT"
INCONSISTENT_SIGNAL = "INCONSISTENT_SIGNAL"
SIGNAL_NOT_YET_VALID, CONTRACT_EXPIRED = "SIGNAL_NOT_YET_VALID", "CONTRACT_EXPIRED"
RESERVE_BREACH, MARGIN_EXCEEDS_CAPITAL = "RESERVE_BREACH", "MARGIN_EXCEEDS_CAPITAL"
# real-book capital and exposure blocks a paper trade does not inherit (paper books keep their own rules)
REAL_BOOK_ONLY_BLOCKS = frozenset({"SAME_SYMBOL_ALREADY_APPROVED", RESERVE_BREACH, MARGIN_EXCEEDS_CAPITAL})
SIZE_HALVED = "SIZE_HALVED"

REASON_CODES = (CAPITAL_UNKNOWN, PORTFOLIO_UNKNOWN, DATA_MISSING, DATA_STALE, SIGNAL_EXPIRED, OUTSIDE_REGIME,
                KILL_SWITCH, "DAILY_STOP", "MONTHLY_PAUSE", "DRAWDOWN_PAUSE", ZERO_SIZE, "RISK_BUDGET_USED",
                "SECTOR_LIMIT", CORRELATED_LIMIT, "CORRELATION_UNKNOWN", MARGIN_UNKNOWN, LOT_SIZE_UNKNOWN,
                UNHEDGED_SHORT, SAME_SYMBOL_ALREADY_APPROVED, EXPOSURE_UNKNOWN, INCONSISTENT_SIGNAL,
                SIGNAL_NOT_YET_VALID, CONTRACT_EXPIRED, RESERVE_BREACH, MARGIN_EXCEEDS_CAPITAL)


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
    """A stock option with any sold leg: the journal's STOCK_OPTION_SELLING trade type (`journal.trade_type_of`),
    which puts it in the STOCK_OPTIONS pool. One leg-based classification picks both the pool and the risk path
    (the phase 9 checks, the stock-option capital and the lot cap); never the signal's direction label."""
    from ..journal import signal_trade_type
    try:
        return signal_trade_type(signal) == "STOCK_OPTION_SELLING"
    except ValueError:
        return False


def signal_inconsistencies(signal: Signal) -> list[str]:
    """Why a signal's asset class, legs and direction disagree (empty = consistent). Checked before sizing: an
    inconsistent signal is never sized (INCONSISTENT_SIGNAL). Cash: no legs, LONG or SHORT. Options: legs, every leg
    priced; all legs bought = LONG; with a sold leg the net premium decides - a credit is a SPREAD, a debit (a bull call
    spread) is LONG. A hedged put spread labelled LONG is therefore inconsistent."""
    legs = signal.legs or ()
    if signal.asset_class == "CASH_EQUITY":
        out = ["a cash signal with option legs"] if legs else []
        if signal.direction not in ("LONG", "SHORT"):
            out.append(f"a cash signal must be LONG or SHORT, not {signal.direction}")
        return out
    if signal.asset_class not in ("INDEX_OPTION", "STOCK_OPTION"):
        return [f"unknown asset class {signal.asset_class!r}"]
    if not legs:
        return []                                   # no legs: DATA_MISSING (`_legs_known`)
    lots = [getattr(x, "lots", None) for x in legs]
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in lots):
        return []                                   # invalid lots: DATA_MISSING (`_legs_known`)
    if not all(known(getattr(x, "price", None)) and x.price >= 0 for x in legs):
        return ["a leg without a known premium: credit or debit cannot be told"]
    if signal.direction not in ("LONG", "SPREAD"):
        return [f"an option signal must be LONG or SPREAD, not {signal.direction}"]
    if all(x.action == "BUY" for x in legs):
        return [] if signal.direction == "LONG" else ["every leg is bought: the direction must be LONG, not SPREAD"]
    net = sum(Decimal(str(x.price)) * x.lots * (1 if x.action == "SELL" else -1) for x in legs)
    if net > 0 and signal.direction != "SPREAD":
        return [f"a net credit ({net:g} per share-lot) with a sold leg is a SPREAD, not {signal.direction}"]
    if net < 0 and signal.direction != "LONG":
        return [f"a net debit ({-net:g} per share-lot) is LONG, not {signal.direction}"]
    if net == 0:
        return ["sold and bought premiums are equal: neither a credit nor a debit"]
    return []


def time_problems(signal: Signal, now: datetime) -> list[tuple[str, str]]:
    """The signal is valid only for created_at <= now < valid_until, and every option leg's contract must still be
    open for a new entry: before the end of continuous derivatives trading on its expiry day (app/session.py). The
    data time is not checked here: a morning swing signal is built on the previous session's close (data_as_of and
    created_at earlier than now) and stays valid until its valid_until."""
    out = []
    if now < signal.created_at:
        out.append((SIGNAL_NOT_YET_VALID, f"created at {signal.created_at.isoformat()}, after the decision time "
                                          f"{now.isoformat()}"))
    if now >= signal.valid_until:
        out.append((SIGNAL_EXPIRED, f"valid until {signal.valid_until.isoformat()}"))
    for x in signal.legs or ():
        exp = getattr(x, "expiry", None)
        if not isinstance(exp, date):
            out.append((CONTRACT_EXPIRED, f"{x.action} {x.right} {x.strike:g}: expiry not known"))
            continue
        cutoff = session_hours(DERIVATIVES, exp).continuous_end_utc
        if now >= cutoff:
            out.append((CONTRACT_EXPIRED, f"{x.action} {x.right} {x.strike:g} expiring {exp:%d-%b-%Y} stopped trading "
                                          f"at {cutoff.astimezone(IST):%d-%b-%Y %H:%M} IST"))
    return out


def stock_capital(settings) -> float:
    """The separate stock-option capital every Phase 9 figure uses (not account.capital_inr)."""
    from ..options.stock_selling import phase9_settings
    return phase9_settings(settings).account.capital_inr


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

    cap = stock_capital(settings) if is_stock_spread(signal) else account.capital_inr
    facts["capital_inr"] = cap
    facts["capital_source"] = ("stock_option_selling.backtest.capital_inr" if is_stock_spread(signal)
                               else "account.capital_inr")
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
    else:
        for code, text in time_problems(signal, market.now):
            no(code, text)
    if allowed_regimes is not None and market.regime not in allowed_regimes:
        no(OUTSIDE_REGIME, f"regime {market.regime or 'UNKNOWN'} is not in {sorted(allowed_regimes)}")
    if costs is None:
        no(DATA_MISSING, "cost rates not known: the cost-inclusive risk cannot be computed")

    problems = position_problems(account.positions)
    for text in problems[:3]:
        no(PORTFOLIO_UNKNOWN, text)
    positions = list(account.positions) if not problems else []
    # one approval per underlying, exposure and session, on the live book passed in (never a stored snapshot). The
    # session is the decision's own (IST day of market.now), stored in the facts; never the signal's data time (a
    # morning swing signal is built on the previous session's close)
    exposure = exposure_of(signal.direction, signal.legs)
    facts["exposure"] = exposure
    if isinstance(market.now, datetime) and market.now.tzinfo is not None:
        session = market.now.astimezone(IST).date()
        facts["decision_session"] = session.isoformat()
        if r.block_same_symbol_same_session and not problems:
            code, text = same_symbol_check(signal.symbol, exposure, positions, session)
            if code:
                no(code, text)

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
    inconsistent = signal_inconsistencies(signal)
    for text in inconsistent:
        no(INCONSISTENT_SIGNAL, text)
    facts["risk_path"] = ("cash" if signal.asset_class == "CASH_EQUITY" else
                          "stock_option_selling" if is_stock_spread(signal) else "option")
    if cap_ok and costs is not None and not inconsistent:
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
        except ValueError as e:                  # an invalid number (negative, NaN, infinity) never becomes a size
            no(DATA_MISSING, f"invalid input: {e}")

    codes = tuple(dict.fromkeys(c for c, _ in block))
    reasons = tuple(f"{c}: {t}" for c, t in block)
    qty = out["quantity"]
    if not codes and (qty is None or qty <= 0):
        codes, reasons = (ZERO_SIZE,), (f"{ZERO_SIZE}: no whole size fits the risk budget",)
    # last guard: whatever happened above, an approval never carries more risk than the budget
    if not codes:
        limit = _budget(signal, settings, cap, factor)
        if out["risk"] is None or not Decimal(str(out["risk"])).is_finite() or Decimal(str(out["risk"])) > limit:
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
    if not all(isinstance(x.lots, int) and not isinstance(x.lots, bool) and x.lots >= 1 for x in legs):
        no(DATA_MISSING, "every leg needs a whole number of lots, at least 1")
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
                                "capital_source": "stock_option_selling.backtest.capital_inr"}
        # BLOCKING for my real stock-option book (the user's policy of 18-Nov-2026; it was advisory before). Paper
        # books keep their own rules: a signal blocked only by these is still paper-traded (`PaperTrader.pending_row`)
        if label == NEEDS_MORE_CAPITAL:
            no(MARGIN_EXCEEDS_CAPITAL, f"margin Rs {out['margin']:,.2f} with Rs {pf['committed_inr']:,.2f} already "
                                       f"committed is above the stock-option capital Rs {pf['capital_inr']:,.2f}; "
                                       f"shortfall Rs {shortfall:,.2f}")
        elif label == TIGHT:
            no(RESERVE_BREACH, f"capital committed would be Rs {pf['after_inr']:,.2f}, above Rs {pf['usable_inr']:,.2f}: "
                               f"the {settings.risk.uncommitted_cash_pct:g}% reserve of the stock-option capital must "
                               "stay uncommitted")
    holdings = [Holding(p.symbol, float(p.risk_inr)) for p in positions if p.kind == STOCK_SPREAD]
    code, text, pf = portfolio_check(signal.symbol, float(pay.max_loss_inr), holdings, sectors,
                                     corr if corr is not None else (lambda a, b: None),
                                     settings.stock_option_selling.portfolio, cap, history_verified=history_verified,
                                     universe=universe)
    facts["phase9_portfolio"] = pf
    if code:
        no(code, text)


def effective_option_budget(settings, account: AccountState) -> tuple[float | None, float, bool]:
    """(budget, size factor, drawdown known): the option risk limit an index spread's width search must fit,
    capital x risk.max_loss_per_option_pct x the drawdown factor - the same figure `decide` uses for the
    option budget. A drawdown that is not known gives the full limit (an upper bound: `decide` then blocks
    the signal with PORTFOLIO_UNKNOWN anyway). Capital not known: None (no width search; `decide` blocks)."""
    from ..options.base import index_option_budget
    r = settings.risk
    _, factor, code, _ = drawdown_state(account.equity_curve, account.flows, r.drawdown_halve_pct,
                                        r.drawdown_pause_pct)
    known_dd = code != PORTFOLIO_UNKNOWN
    cap = account.capital_inr
    if not (known(cap) and cap > 0):
        return None, factor, known_dd
    return index_option_budget(settings, factor if known_dd else 1.0, cap), factor, known_dd


# ---------------- live service ----------------
class RiskService:
    """Gathers the live inputs for the risk engine: capital from config.yaml, kill-switch inputs from the
    hub and the store, deposits from capital_log. Open positions and P&L come from the phase 6 journal;
    until it exists they are NOT KNOWN, so every live decision is NO_TRADE (PORTFOLIO_UNKNOWN)."""

    def __init__(self, settings, store, hub, calendar):
        self.s, self.store, self.hub, self.calendar = settings, store, hub, calendar
        self.journal = self.marker = self.costs = None
        self.now_fn = None

    def attach(self, journal, marker, costs, now_fn) -> None:
        """Phase 6: open positions, P&L and equity per pool come from the journal (app/journal.py)."""
        self.journal, self.marker, self.costs, self.now_fn = journal, marker, costs, now_fn

    def _status_of(self, symbol: str, now: datetime) -> str | None:
        return next((row["status"] for row in self.hub.snapshot(now) if row["symbol"] == symbol), None)

    def market_state(self, symbol: str, now: datetime, regime: str | None = None) -> MarketState:
        ks = self.s.risk.kill_switch
        since = (now - timedelta(minutes=ks.quarantine_window_minutes)).astimezone(timezone.utc)
        return MarketState(now=now, price_status=self._status_of(symbol, now), regime=regime,
                           # new signals only in the index's continuous trading (not in the closing auction)
                           market_open=is_market_hours(now, self.calendar.holidays,
                                                       segment_for(self.s, self.s.regime.index_symbol), continuous=True),
                           index_status=self._status_of(self.s.regime.index_symbol, now),
                           # live price ticks only: option chain rows and history bars never count
                           quarantined_recent=self.store.quality_event_count_since(since, TICK_REASONS))

    def option_budget(self) -> float | None:
        """The effective option risk limit for the Options page's index spread width search (drawdown halving
        included once the phase 6 journal records the equity history; until then the full limit)."""
        return effective_option_budget(self.s, self.account_state())[0]

    def correlations(self, source: str):
        """60-session daily-return correlations from the stored daily bars with the phase 5 window
        (`app.options.shortlist.store_correlations`, completed sessions only): call `.at(day, a, b)`."""
        from ..options.shortlist import store_correlations
        r = self.s.risk
        return store_correlations(self.s, self.store, source, r.correlation_window, r.correlation_min_overlap)

    def account_state(self, pool: str = "CASH_INDEX", now: datetime | None = None) -> AccountState:
        """The account state of one capital pool (phase 6, issue 95): CASH_INDEX (account.capital_inr) or
        STOCK_OPTIONS (stock_option_selling.backtest.capital_inr). Without the journal: positions not known."""
        return self.pool_state(pool, now)[0]

    def pool_state(self, pool: str = "CASH_INDEX", now: datetime | None = None) -> tuple[AccountState, dict]:
        flows = tuple(self.store.capital_flows())
        if self.journal is None:
            cap = self.s.account.capital_inr if pool == "CASH_INDEX" else stock_capital(self.s)
            return (AccountState(capital_inr=cap, positions=None, realised_today_inr=None, realised_month_inr=None,
                                 equity_curve=None, flows=flows if pool == "CASH_INDEX" else ()),
                    {"pool": pool, "unknown": ["open positions are not known (no journal)"]})
        from ..journal import pool_account
        now = now or (self.now_fn() if self.now_fn else datetime.now(timezone.utc))
        return pool_account(self.journal, self.s, pool, now, self.marker.marks, self.costs, flows)

    def status(self, now: datetime) -> dict:
        """For GET /api/risk: the limits in rupees, the kill switch, and what is not known yet."""
        s, r = self.s, self.s.risk
        cap = s.account.capital_inr
        stock_cap = stock_capital(s)
        m = self.market_state(s.regime.index_symbol, now)
        ks = kill_switch(s.regime.index_symbol, m.index_status, m.quarantined_recent, m.market_open, r.kill_switch)
        pools = {}
        for pool in ("CASH_INDEX", "STOCK_OPTIONS"):
            acct, f = self.pool_state(pool, now)
            probs = position_problems(acct.positions)
            pools[pool] = {**f, "known": not probs and acct.equity_curve is not None, "problems": probs[:5]}
        acct = self.account_state("CASH_INDEX", now)
        problems = position_problems(acct.positions)
        log = self.store.capital_log()
        return {
            "as_of_utc": now.isoformat(), "capital_inr": cap, "account_mode": s.account.mode,
            "stock_option_capital_inr": stock_cap,     # stock option spreads: every figure is a % of this
            "limits_pct": {k: v for k, v in r.model_dump().items() if k != "kill_switch"},
            "limits_inr": {**s.limits_in_rupees(),
                           "position_value_cap": s.rupees(s.backtest.position_cap_pct),
                           "max_risk_per_stock_spread": round(stock_cap * s.stock_option_selling.max_risk_per_position_pct
                                                              / 100, 2),
                           "capital_pool_usable": s.rupees(100 - r.uncommitted_cash_pct)},
            "kill_switch": {"on": bool(ks), "reasons": ks, "index": s.regime.index_symbol,
                            "index_status": m.index_status, "market_open": m.market_open,
                            "quarantined_recent": m.quarantined_recent, **r.kill_switch.model_dump()},
            "portfolio": {"known": not problems, "problems": problems,
                          "note": "open positions and P&L come from the phase 6 journal, per capital pool"},
            "pools": pools,
            "capital_log": {"entries": len(log),
                            "deposits_inr": round(sum(e["amount"] for e in log if e["type"] == "DEPOSIT"), 2),
                            "withdrawals_inr": round(sum(e["amount"] for e in log if e["type"] == "WITHDRAWAL"), 2),
                            "note": "deposits and withdrawals are never counted as profit or loss"},
            "reason_codes": list(REASON_CODES),
        }
