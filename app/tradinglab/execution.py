"""Модель исполнения свопа FABLES/USDG (Uniswap v4, dynamic-fee hook). Только симуляция.

Комиссия измерена 2026-11-05 по событиям Swap (evidence/trading_lab/swap_fee_sample.json): покупка FABLES 0,50%, продажа 1,00%.
Комиссия списывается со входящего актива (как в v4). Импакт — по модели постоянного произведения на ГЛУБИНЕ depth_usd
(виртуальный резерв USDG-стороны). Для backtest глубина берётся консервативно (меньше наблюдаемой), см. CONSERVATIVE_V1.
Slippage — дополнительное ухудшение цены против нас. Газ — фиксированная сумма в USD за своп.
Все суммы — Decimal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal, getcontext

getcontext().prec = 40
D = Decimal
ZERO = D(0)
FABLES_UNIT = D("1e-18")   # decimals 18
USDG_UNIT = D("1e-6")      # decimals 6


def q_fables(x: Decimal) -> Decimal:
    """Количество FABLES в точности токена (как в чейне), округление вниз."""
    return x.quantize(FABLES_UNIT, rounding=ROUND_DOWN)


def q_usdg(x: Decimal) -> Decimal:
    return x.quantize(USDG_UNIT, rounding=ROUND_DOWN)


def dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else D(str(x))


@dataclass(frozen=True)
class CostModel:
    name: str
    buy_fee: Decimal          # доля входящего USDG
    sell_fee: Decimal         # доля входящих FABLES
    slippage: Decimal         # доля, против нас
    stop_slippage: Decimal    # дополнительная доля при исполнении стопа
    gas_usd: Decimal
    depth_usd: Decimal        # виртуальный резерв USDG-стороны для расчёта импакта
    max_trade_frac_liq: Decimal  # предел размера сделки как доля ликвидности пула
    liquidity_usd: Decimal    # ликвидность пула, к которой применяется предел
    usdg_usd: Decimal = D(1)
    label: str = ""

    def d(self) -> dict:
        return {k: str(v) for k, v in asdict(self).items()}


# Консервативная модель для backtest: исторических котировок нужного блока нет → x·y=k с глубиной = 50% наблюдаемого
# реального USDG-резерва ($176,7K → $88K). Импакт сделки $500 ≈ 0,57% против ≈ 0,014% по активной ликвидности
# (StateView, 2026-11-05), т.е. примерно в 40 раз хуже измеренного. Slippage 0,3%, стоп — ещё 0,5%.
CONSERVATIVE_V1 = CostModel(
    name="conservative-v1", buy_fee=D("0.005"), sell_fee=D("0.010"), slippage=D("0.003"), stop_slippage=D("0.005"),
    gas_usd=D("0.05"), depth_usd=D("88000"), max_trade_frac_liq=D("0.005"), liquidity_usd=D("680000"),
    label="BACKTEST: консервативная модель издержек (не точный исторический quote)")


@dataclass(frozen=True)
class Fill:
    side: str             # BUY | SELL
    mid: Decimal          # опорная цена USDG за FABLES
    qty: Decimal          # FABLES получено (BUY) / отдано (SELL)
    usd: Decimal          # USDG отдано (BUY, без газа) / получено (SELL, без газа)
    exec_price: Decimal   # usd / qty
    fee_usd: Decimal
    impact_usd: Decimal
    slippage_usd: Decimal
    gas_usd: Decimal

    @property
    def cost_usd(self) -> Decimal:
        return self.fee_usd + self.impact_usd + self.slippage_usd + self.gas_usd

    def d(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


def _cp_out(amount_in: Decimal, r_in: Decimal, r_out: Decimal) -> Decimal:
    return r_out * amount_in / (r_in + amount_in)


def quote_buy(usd_in, mid, m: CostModel, extra_slip: Decimal = ZERO) -> Fill:
    usd_in, mid = q_usdg(dec(usd_in)), dec(mid)
    if usd_in <= 0 or mid <= 0:
        raise ValueError("некорректная покупка")
    fee = usd_in * m.buy_fee
    net = usd_in - fee
    rq = m.depth_usd
    rb = rq / mid
    out_cp = _cp_out(net, rq, rb)
    slip = m.slippage + extra_slip
    out = q_fables(out_cp * (1 - slip))
    ideal = net / mid
    return Fill("BUY", mid, out, usd_in, usd_in / out, fee, (ideal - out_cp) * mid, (out_cp - out) * mid, m.gas_usd)


def quote_sell(qty, mid, m: CostModel, extra_slip: Decimal = ZERO) -> Fill:
    qty, mid = q_fables(dec(qty)), dec(mid)
    if qty <= 0 or mid <= 0:
        raise ValueError("некорректная продажа")
    fee_t = qty * m.sell_fee
    net = qty - fee_t
    rq = m.depth_usd
    rb = rq / mid
    usd_cp = _cp_out(net, rb, rq)
    slip = m.slippage + extra_slip
    usd = q_usdg(usd_cp * (1 - slip))
    return Fill("SELL", mid, qty, usd, usd / qty, fee_t * mid, net * mid - usd_cp, usd_cp - usd, m.gas_usd)


def liquidation_value(qty, mid, m: CostModel) -> Decimal:
    """Сколько USDG дала бы немедленная продажа qty (за вычетом всех издержек). Для оценки equity."""
    qty = dec(qty)
    if qty <= 0:
        return ZERO
    f = quote_sell(qty, mid, m)
    return f.usd - f.gas_usd


def max_trade_usd(m: CostModel) -> Decimal:
    return m.liquidity_usd * m.max_trade_frac_liq
