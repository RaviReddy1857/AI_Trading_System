"""ICICI Direct trading costs: brokerage, STT, NSE transaction charge, SEBI fee, IPFT, stamp
duty, GST and the depository charge, for intraday equity, delivery equity and options, plus
slippage.

All rates come from config.yaml -> costs (each with its source and effective date). A rate left
empty (value: null) makes any calculation that needs it raise MissingCostRates, naming every
missing rate. Nothing here guesses a rate.

Arithmetic uses Decimal. Each charge is computed per leg on that leg's turnover and rounded to
the paise (half up); GST is computed on the rounded charges it applies to. Contract notes may
round some items differently (for example per order); compare with a real note before trusting
the paise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .config import Costs, EquityRates, OptionRates, Rate

SEGMENTS = ("intraday_equity", "delivery_equity", "options")
PAISE = Decimal("0.01")
HUNDRED = Decimal(100)


class MissingCostRates(RuntimeError):
    """Raised when a backtest needs a rate that is empty in config.yaml."""


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(PAISE, rounding=ROUND_HALF_UP)


def _pct(rate: Rate) -> Decimal:
    return Decimal(str(rate.value)) / HUNDRED


@dataclass
class CostBreakdown:
    """Charges for one round trip, in rupees (Decimal, paise)."""
    brokerage: Decimal = Decimal("0.00")
    stt: Decimal = Decimal("0.00")
    exchange_txn: Decimal = Decimal("0.00")
    sebi_fee: Decimal = Decimal("0.00")
    ipft: Decimal = Decimal("0.00")
    stamp_duty: Decimal = Decimal("0.00")
    dp_charge: Decimal = Decimal("0.00")
    gst: Decimal = Decimal("0.00")
    lines: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return (self.brokerage + self.stt + self.exchange_txn + self.sebi_fee + self.ipft
                + self.stamp_duty + self.dp_charge + self.gst)

    def as_dict(self) -> dict[str, float]:
        keys = ("brokerage", "stt", "exchange_txn", "sebi_fee", "ipft", "stamp_duty", "dp_charge", "gst")
        return {**{k: float(getattr(self, k)) for k in keys}, "total": float(self.total)}


class CostModel:
    def __init__(self, costs: Costs | None):
        if costs is None:
            raise MissingCostRates("config.yaml has no 'costs' section; backtests cannot run without costs")
        self.c = costs

    @property
    def label(self) -> str:
        return "verified against a contract note" if self.c.verified_against_contract_note else self.c.label

    def info(self) -> dict:
        return {"label": self.label, "plan": self.c.plan, "compiled_on": self.c.compiled_on,
                "verified_against_contract_note": self.c.verified_against_contract_note,
                "slippage_pct": self.c.slippage_pct.model_dump()}

    # ---- completeness ----
    def missing(self, segment: str) -> list[str]:
        """Names of empty rates needed for a segment (plus GST)."""
        if segment not in SEGMENTS:
            raise ValueError(f"unknown cost segment {segment}")
        out = [] if self.c.gst_pct.value is not None else ["costs.gst_pct"]
        rates: EquityRates | OptionRates = getattr(self.c, segment)
        for name, rate in rates:
            if rate is None:
                if name == "dp_charge_per_sell_inr" and segment == "delivery_equity":
                    out.append(f"costs.{segment}.{name}")
                continue
            if rate.value is None:
                out.append(f"costs.{segment}.{name}")
        return out

    def require(self, *segments: str) -> None:
        """Fail loudly, naming every empty rate the given segments need."""
        missing = [m for s in segments for m in self.missing(s)]
        if missing:
            raise MissingCostRates(
                "Cannot run: these cost rates are empty in config.yaml: " + ", ".join(missing)
                + ". Fill them from your ICICI contract note (costs section).")

    # ---- slippage ----
    def slip_pct(self, segment: str) -> Decimal:
        return Decimal(str(getattr(self.c.slippage_pct, segment))) / HUNDRED

    def fill_price(self, segment: str, price: float, side: str) -> Decimal:
        """Price after adverse slippage: buys fill higher, sells lower. Rounded to the paise."""
        s = self.slip_pct(segment)
        p = Decimal(str(price))
        return money(p * (1 + s) if side == "BUY" else p * (1 - s))

    # ---- charges ----
    def _gst(self, b: CostBreakdown) -> Decimal:
        base = Decimal(0)
        for item in self.c.gst_on:
            base += getattr(b, item)
        return money(base * _pct(self.c.gst_pct))

    def equity(self, segment: str, buy_value, sell_value) -> CostBreakdown:
        """Round-trip charges for one cash-equity trade with the given buy and sell turnover."""
        if segment not in ("intraday_equity", "delivery_equity"):
            raise ValueError(f"not an equity segment: {segment}")
        self.require(segment)
        r: EquityRates = getattr(self.c, segment)
        buy, sell = Decimal(str(buy_value)), Decimal(str(sell_value))
        b = CostBreakdown()
        b.brokerage = money(buy * _pct(r.brokerage_pct)) + money(sell * _pct(r.brokerage_pct))
        b.stt = money(buy * _pct(r.stt_buy_pct)) + money(sell * _pct(r.stt_sell_pct))
        b.exchange_txn = money(buy * _pct(r.exchange_txn_pct)) + money(sell * _pct(r.exchange_txn_pct))
        b.sebi_fee = money(buy * _pct(r.sebi_fee_pct)) + money(sell * _pct(r.sebi_fee_pct))
        b.ipft = money(buy * _pct(r.ipft_pct)) + money(sell * _pct(r.ipft_pct))
        b.stamp_duty = money(buy * _pct(r.stamp_duty_buy_pct))
        if segment == "delivery_equity" and r.dp_charge_per_sell_inr is not None and sell > 0:
            b.dp_charge = money(r.dp_charge_per_sell_inr.value)
        b.gst = self._gst(b)
        return b

    def options(self, lots_per_leg: int, buy_premium_value, sell_premium_value) -> CostBreakdown:
        """Round-trip charges for an option position (one buy leg and one sell leg of `lots_per_leg`)."""
        self.require("options")
        r: OptionRates = self.c.options
        buy, sell = Decimal(str(buy_premium_value)), Decimal(str(sell_premium_value))
        b = CostBreakdown()
        b.brokerage = money(Decimal(str(r.brokerage_per_lot_inr.value)) * lots_per_leg * 2)
        b.stt = money(sell * _pct(r.stt_sell_premium_pct))
        b.exchange_txn = money(buy * _pct(r.exchange_txn_premium_pct)) + money(sell * _pct(r.exchange_txn_premium_pct))
        b.sebi_fee = money(buy * _pct(r.sebi_fee_pct)) + money(sell * _pct(r.sebi_fee_pct))
        b.ipft = money(buy * _pct(r.ipft_pct)) + money(sell * _pct(r.ipft_pct))
        b.stamp_duty = money(buy * _pct(r.stamp_duty_buy_pct))
        b.gst = self._gst(b)
        return b
