"""Phase 6 step 4: the paper trader. Every approved signal is paper-traded automatically, whatever its trade type's
alert switch says, and tracked to its stop, target or time exit, with costs. Nothing is sent to the broker.

Fill: at the next bar's open, never on the signal bar: the first LIVE quote received at or after the planned fill
time (the close of the signal bar). Cash: a purchase at the ask and a sale at the bid when the feed sends them,
else the last trade moved against us by the configured slippage. Options: every leg at its touch (buy at the ask,
sell at the bid) from a chain snapshot taken at or after the planned fill. No LIVE quote before the signal's
valid-until: NOT_FILLED. A fill at or beyond the stop or target: NOT_FILLED (FILL_BEYOND_STOP_OR_TARGET), as in
the backtests.

Exits: cash on each closed 5-minute bar built from the stored 1-minute bars (stop first when one bar touches both,
`app.backtest.Engine._hit`), the stop or target price moved by the slippage; options on LIVE leg quotes at the
touch (a long option at its bid; a spread at the cost to buy it back). Time exits by `positions.time_exit_at`
(stock option spreads: the phase 9 time and forced exits). A stale quote never moves a paper trade.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .execution import PAPER_FILL, PLANNED_FILL, legs_of
from .journal import Leg, Position, ist_day, pnl, pool_of_trade_type, signal_trade_type
from .models import Bar

FIVE = timedelta(minutes=5)


def bar_of(bars_1m: list[Bar], symbol: str, start: datetime) -> Bar | None:
    """The 5-minute bar [start, start+5) from 1-minute bars (None when none was stored)."""
    xs = [b for b in bars_1m if start <= b.start < start + FIVE]
    if not xs:
        return None
    return Bar(symbol, start, xs[0].open, max(b.high for b in xs), min(b.low for b in xs), xs[-1].close,
               sum((b.volume or 0) for b in xs), sum(b.ticks for b in xs))


class PaperTrader:
    def __init__(self, settings, journal, marker, store, execbook, calendar, costs):
        self.s, self.j, self.m, self.store, self.x, self.cal, self.costs = (settings, journal, marker, store, execbook,
                                                                           calendar, costs)

    # ---- open ----
    def open(self, sig, decision, now: datetime) -> dict | None:
        """A pending paper trade for an approved signal (planned fill: the next bar's open = the signal bar's close)."""
        if decision.status != "APPROVED" or self.j.paper_trade(sig.signal_id) is not None:
            return None
        tt = signal_trade_type(sig)
        units = decision.lots if sig.legs else decision.quantity
        legs = [Leg(x.action, x.right, x.strike, x.expiry, units, x.lot_size, x.price).to_dict() for x in sig.legs] \
            if sig.legs else None
        row = {"signal_id": sig.signal_id, "trade_type": tt, "pool": pool_of_trade_type(tt), "symbol": sig.symbol,
               "direction": sig.direction, "legs_json": json.dumps(legs) if legs else None, "quantity": units,
               "status": "PENDING", "planned_fill_utc": sig.created_at.isoformat(), "stop": sig.stop,
               "target": sig.target, "risk_inr": decision.risk_inr, "margin_inr": decision.margin_inr,
               "horizon": sig.horizon, "strategy_id": sig.strategy_id,
               "note": json.dumps({"valid_until_utc": sig.valid_until.isoformat()})}
        self.j.upsert_paper(row)
        return row

    # ---- fills ----
    def fill_pending(self, now: datetime, signals: dict) -> list[str]:
        """Fill every pending paper trade whose next-bar quote has arrived. `signals`: signal_id -> Signal."""
        done = []
        for row in self.j.paper_trades(status="PENDING"):
            sig = signals.get(row["signal_id"])
            planned = datetime.fromisoformat(row["planned_fill_utc"])
            valid_until = datetime.fromisoformat(json.loads(row["note"])["valid_until_utc"])
            if sig is None:
                continue
            qs = self._quotes(sig, now)
            fresh = all(q.usable and q.quote_time is not None and q.quote_time >= planned for q in qs)
            if not fresh:
                if now >= valid_until:
                    self.x.record_quotes(sig, PLANNED_FILL, qs, now, row["quantity"], self._spot(sig, now))
                    self._finish(row, "NOT_FILLED", now, "no LIVE quote at or after the planned fill before valid-until")
                    done.append(row["signal_id"])
                continue
            spot = self._spot(sig, now)
            self.x.record_quotes(sig, PLANNED_FILL, qs, now, row["quantity"], spot)
            prices, source = self._fill_prices(sig, qs)
            if prices is None:
                self._finish(row, "NOT_FILLED", now, source)
                done.append(row["signal_id"])
                continue
            entry = self._entry_price(sig, prices)
            beyond = self._beyond(sig, entry)
            self.x.record_fill(legs_of(sig), sig.signal_id, PAPER_FILL, prices, now, row["quantity"], source, spot)
            if beyond:
                self._finish(row, "NOT_FILLED", now, f"FILL_BEYOND_STOP_OR_TARGET: fill {entry:.2f} {beyond}")
                done.append(row["signal_id"])
                continue
            from .positions import time_exit_at
            row.update(status="OPEN", entry_utc=now.isoformat(), entry_price=round(entry, 4),
                       entry_prices_json=json.dumps(prices) if sig.legs else None, entry_source=source)
            due, _ = time_exit_at(self.j.paper_position(row), self.s, self.cal)
            row["time_exit_utc"] = due.isoformat() if due else None
            self.j.upsert_paper(row)
            done.append(row["signal_id"])
        return done

    def _quotes(self, sig, now):
        if not sig.legs:
            return [self.m.cash(sig.symbol, now)]
        return [self.m.option(sig.symbol, x.expiry, x.strike, x.right, now) for x in sig.legs]

    def _spot(self, sig, now):
        return self.m.spot(sig.symbol, now)

    def _fill_prices(self, sig, qs) -> tuple[list[float] | None, str]:
        if not sig.legs:
            side = "BUY" if sig.direction == "LONG" else "SELL"
            q = qs[0]
            t = q.touch(side)
            if t is not None:
                return [t], f"first quote of the next bar ({'ask' if side == 'BUY' else 'bid'})"
            seg = "intraday_equity" if sig.horizon == "INTRADAY" else "delivery_equity"
            if q.last is None:
                return None, "no price in the next bar's first quote"
            return [float(self.costs.fill_price(seg, q.last, side))], "first trade of the next bar plus slippage"
        out = []
        for leg, q in zip(sig.legs, qs):
            t = q.touch(leg.action)
            if t is None:
                return None, f"no {'ask' if leg.action == 'BUY' else 'bid'} for {leg.strike:g} {leg.right}"
            out.append(t)
        return out, "option chain at the next bar (buy at the ask, sell at the bid)"

    @staticmethod
    def _entry_price(sig, prices) -> float:
        if not sig.legs:
            return prices[0]
        net = sum(p if x.action == "SELL" else -p for x, p in zip(sig.legs, prices))
        return -net if sig.direction == "LONG" else net

    @staticmethod
    def _beyond(sig, entry) -> str | None:
        if sig.legs and sig.direction == "SPREAD":
            # a spread's stop and target are costs to buy it back: the credit must sit between them
            return None if sig.target < entry < sig.stop else f"outside target {sig.target} and stop {sig.stop}"
        long = sig.direction == "LONG"
        ok = (sig.stop < entry < sig.target) if long else (sig.target < entry < sig.stop)
        return None if ok else f"at or beyond stop {sig.stop} / target {sig.target}"

    # ---- exits ----
    def manage(self, now: datetime, bar_start: datetime | None = None) -> list[dict]:
        """Check every open paper trade: cash on the 5-minute bar [bar_start, bar_start+5), options on quotes now."""
        out = []
        for row in self.j.paper_trades(status="OPEN"):
            pos = self.j.paper_position(row)
            res = self._cash_exit(pos, row, now, bar_start) if not pos.is_option else self._option_exit(pos, row, now)
            if res is not None:
                reason, prices = res
                out.append(self._close(row, pos, reason, prices, now))
        return out

    def _cash_exit(self, pos: Position, row, now, bar_start):
        from .backtest import Engine
        if bar_start is None:
            return None
        bars = self.store.bars_1m_between(pos.symbol, bar_start, bar_start + FIVE)
        bar = bar_of(bars, pos.symbol, bar_start)
        entry_t = datetime.fromisoformat(row["entry_utc"])
        if bar is not None and bar_start + FIVE > entry_t:
            is_entry = bar_start <= entry_t < bar_start + FIVE
            if is_entry:                               # only the part of the bar after the fill
                after = [b for b in bars if b.start >= entry_t.replace(second=0, microsecond=0)]
                bar = bar_of(after, pos.symbol, bar_start) or bar
            hit = Engine._hit(pos.direction, bar, pos.stop, pos.target, is_entry)
            if hit:
                return hit[0], [self._slip(pos, hit[1])]
        due = row.get("time_exit_utc")
        if due and bar is not None and bar_start + FIVE >= datetime.fromisoformat(due):
            return "TIME", [self._slip(pos, bar.close)]
        return None

    def _slip(self, pos, raw: float) -> float:
        seg = "intraday_equity" if pos.trade_type == "INTRADAY_STOCKS" else "delivery_equity"
        side = "SELL" if pos.direction == "LONG" else "BUY"
        return float(self.costs.fill_price(seg, raw, side))

    def _option_exit(self, pos: Position, row, now):
        prices, _ = self.m.marks(pos, now)
        due = row.get("time_exit_utc")
        if prices is None:
            return None                                # a stale or missing quote never moves a paper trade
        from .positions import own_price
        px = own_price(pos, prices)
        spread = pos.selling
        if spread:
            if px >= pos.stop:
                return "STOP", prices
            if px <= pos.target:
                return "TARGET", prices
        else:
            if px <= pos.stop:
                return "STOP", prices
            if px >= pos.target:
                return "TARGET", prices
        if pos.trade_type == "STOCK_OPTION_SELLING":
            from .options.stock_selling import trading_days_to
            expiry = min(x.expiry for x in pos.legs)
            p9 = self.s.stock_option_selling
            if (trading_days_to(ist_day(now), expiry, self.cal.is_trading_day) <= p9.forced_exit_trading_days_before_expiry
                    or (expiry - ist_day(now)).days <= p9.time_exit_dte):
                return "TIME", prices
        if due and now >= datetime.fromisoformat(due):
            return "TIME", prices
        return None

    def _close(self, row, pos: Position, reason: str, prices, now) -> dict:
        calc = pos
        if pos.trade_type == "SWING_STOCKS" and ist_day(now) == ist_day(pos.entry_utc):
            calc = replace(pos, trade_type="INTRADAY_STOCKS")       # same-day exit: intraday charges
        gross, charges, net = pnl(calc, prices, pos.quantity, self.costs)
        row.update(status="CLOSED", exit_utc=now.isoformat(), exit_price=round(prices[0], 4) if not pos.is_option else None,
                   exit_prices_json=json.dumps(prices), exit_reason=reason, gross_inr=float(gross),
                   costs_inr=None if charges is None else float(charges), net_pnl_inr=None if net is None else float(net))
        self.j.upsert_paper(row)
        return {"signal_id": row["signal_id"], "reason": reason, "net_pnl_inr": row["net_pnl_inr"]}

    def _finish(self, row, status, now, why) -> None:
        note = json.loads(row["note"] or "{}")
        note["why"] = why
        row.update(status=status, exit_utc=now.isoformat(), exit_reason=why, note=json.dumps(note))
        self.j.upsert_paper(row)
