"""Портфельный учёт (Decimal, FIFO). Один портфель — один трейдер; состояния портфелей не пересекаются.

Инварианты (нарушение → AccountingError, операция не применяется):
- денежный баланс не становится отрицательным;
- нельзя продать больше FABLES, чем есть;
- один fill_id применяется ровно один раз (повтор — no-op, возвращает False).
Себестоимость лота включает всю потраченную сумму USDG и газ покупки. Realized PnL = выручка − газ продажи − себестоимость FIFO.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .execution import ZERO, Fill, dec


class AccountingError(Exception):
    pass


@dataclass
class Lot:
    lot_id: str
    position_id: str
    qty: Decimal
    cost: Decimal        # остаточная себестоимость лота, USD
    open_ts: int

    def d(self) -> dict:
        return {"lot_id": self.lot_id, "position_id": self.position_id, "qty": str(self.qty), "cost": str(self.cost),
                "open_ts": self.open_ts}


@dataclass
class Portfolio:
    trader: str
    initial: Decimal
    cash: Decimal
    lots: list[Lot] = field(default_factory=list)
    realized: Decimal = ZERO
    fees: Decimal = ZERO          # swap fee
    costs: Decimal = ZERO         # fee + impact + slippage + gas
    gas: Decimal = ZERO
    turnover: Decimal = ZERO
    applied: set[str] = field(default_factory=set)

    @classmethod
    def new(cls, trader: str, initial) -> "Portfolio":
        x = dec(initial)
        return cls(trader, x, x)

    @property
    def qty(self) -> Decimal:
        return sum((l.qty for l in self.lots), ZERO)

    @property
    def cost_basis(self) -> Decimal:
        return sum((l.cost for l in self.lots), ZERO)

    def apply_buy(self, fill_id: str, f: Fill, position_id: str, ts: int) -> bool:
        if fill_id in self.applied:
            return False
        if f.side != "BUY":
            raise AccountingError("ожидалась покупка")
        spend = f.usd + f.gas_usd
        if spend > self.cash:
            raise AccountingError(f"{self.trader}: недостаточно денег ({self.cash} < {spend})")
        self.cash -= spend
        self.lots.append(Lot(fill_id, position_id, f.qty, spend, ts))
        self._book(fill_id, f)
        return True

    def apply_sell(self, fill_id: str, f: Fill, position_id: str | None = None) -> tuple[bool, Decimal]:
        """→ (применено, realized PnL этой продажи)."""
        if fill_id in self.applied:
            return False, ZERO
        if f.side != "SELL":
            raise AccountingError("ожидалась продажа")
        lots = [l for l in self.lots if position_id is None or l.position_id == position_id]
        have = sum((l.qty for l in lots), ZERO)
        if f.qty > have:
            raise AccountingError(f"{self.trader}: продажа {f.qty} FABLES > имеется {have}")
        if f.usd < f.gas_usd and self.cash + f.usd < f.gas_usd:
            raise AccountingError(f"{self.trader}: нечем оплатить газ")
        left, cost = f.qty, ZERO
        for l in lots:
            if left <= 0:
                break
            take = min(l.qty, left)
            part = l.cost * take / l.qty
            l.qty -= take
            l.cost -= part
            cost += part
            left -= take
        self.lots = [l for l in self.lots if l.qty > 0]
        pnl = f.usd - f.gas_usd - cost
        self.cash += f.usd - f.gas_usd
        self.realized += pnl
        self._book(fill_id, f)
        return True, pnl

    def _book(self, fill_id: str, f: Fill) -> None:
        self.applied.add(fill_id)
        self.fees += f.fee_usd
        self.gas += f.gas_usd
        self.costs += f.cost_usd
        self.turnover += f.usd

    def equity(self, liq_value: Decimal) -> Decimal:
        return self.cash + liq_value

    def unrealized(self, liq_value: Decimal) -> Decimal:
        return liq_value - self.cost_basis

    def to_state(self) -> dict:
        return {"trader": self.trader, "initial": str(self.initial), "cash": str(self.cash), "lots": [l.d() for l in self.lots],
                "realized": str(self.realized), "fees": str(self.fees), "costs": str(self.costs), "gas": str(self.gas),
                "turnover": str(self.turnover), "applied": sorted(self.applied)}

    @classmethod
    def from_state(cls, s: dict) -> "Portfolio":
        p = cls(s["trader"], dec(s["initial"]), dec(s["cash"]))
        p.lots = [Lot(l["lot_id"], l["position_id"], dec(l["qty"]), dec(l["cost"]), int(l["open_ts"])) for l in s["lots"]]
        for k in ("realized", "fees", "costs", "gas", "turnover"):
            setattr(p, k, dec(s[k]))
        p.applied = set(s["applied"])
        return p
