"""Торговый автомат одного портфеля.

Состояния: WARMUP → FLAT → ENTRY_PENDING → LONG → EXIT_PENDING → FLAT; а также PAUSED, RISK_OFF, DATA_ERROR, KILLED.

Последовательность на закрытой свече i (backtest; forward использует те же методы с живой котировкой):
 1) execute_pending(open_i): решение, принятое на закрытии i-1, исполняется по ПЕРВОЙ цене после решения;
 2) intrabar(h_i, l_i): стоп (исполнение по min(open, stop) с доп. проскальзыванием) проверяется раньше цели;
 3) on_close(i): trailing/time stop/MFE/MAE, анализ стратегии → новое намерение (pending), оценка equity.
Повторная обработка той же свечи — no-op (идемпотентность по bar_ts); fill_id детерминирован.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from . import risk as R
from .analysis import Analysis
from .execution import ZERO, CostModel, dec, liquidation_value, q_fables, quote_buy, quote_sell
from .portfolio import Portfolio
from .strategies import STRATEGIES, Ctx


@dataclass
class Position:
    position_id: str
    entry_ts: int
    entry_idx: int
    stop: float
    initial_stop: float
    t1: float | None
    t1_frac: float
    t1_done: bool
    trail_atr: float
    trail_ref: str
    trail_after_t1: bool
    time_stop: int
    decision_ref: str
    entry_mid: float
    entry_usd: str
    risk_usd: str
    qty_initial: str
    high_water: float
    max_high: float
    min_low: float
    gross: str = "0"
    spent: str = "0"     # USDG + газ на покупку
    got: str = "0"       # USDG − газ от продаж
    costs: str = "0"     # fee + impact + slippage + gas по позиции
    exits: list = field(default_factory=list)


class Trader:
    def __init__(self, tf: str, mode: str, experiment: str, cost: CostModel, initial="1000", risk_cfg: R.RiskConfig | None = None):
        self.tf, self.mode, self.experiment, self.cost = tf, mode, experiment, cost
        self.strategy, self.fn = STRATEGIES[tf]
        self.rcfg = risk_cfg or R.RISK[tf]
        self.name = f"Trader {tf.upper()}"
        self.portfolio = Portfolio.new(self.name, initial)
        self.risk = R.RiskState(peak_equity=dec(initial))
        self.state = "WARMUP"
        self.position: Position | None = None
        self.pending: dict | None = None
        self.last_bar_ts = -1
        self.paused = False
        self.ops: list[dict] = []        # торговые операции (fills)
        self.trades: list[dict] = []     # завершённые сделки
        self.decisions: list[dict] = []  # все решения (в т.ч. WAIT)
        self.equity_curve: list[dict] = []
        self.events: list[dict] = []     # отмены, риск-события
        self._new: dict = {"ops": [], "trades": [], "decisions": [], "equity": [], "events": []}

    # ------------------------------------------------------------------ helpers
    def _id(self, ts: int, kind: str) -> str:
        return f"{self.experiment}:{self.tf}:{ts}:{kind}"

    def _push(self, key: str, rec: dict) -> None:
        getattr(self, "equity_curve" if key == "equity" else key).append(rec)
        self._new[key].append(rec)

    def drain(self) -> dict:
        out, self._new = self._new, {"ops": [], "trades": [], "decisions": [], "equity": [], "events": []}
        return out

    def equity_at(self, mid: float) -> Decimal:
        return self.portfolio.equity(liquidation_value(self.portfolio.qty, mid, self.cost))

    def _op(self, fid: str, ts: int, f, reason: str, signals: list, pnl: Decimal | None = None) -> None:
        self._push("ops", {"op_id": fid, "mode": self.mode, "trader": self.tf, "ts": ts, "side": f.side, "qty": str(f.qty),
                           "price": str(f.exec_price), "mid": str(f.mid), "usd": str(f.usd), "fee_usd": str(f.fee_usd),
                           "impact_usd": str(f.impact_usd), "slippage_usd": str(f.slippage_usd), "gas_usd": str(f.gas_usd),
                           "slip_bps": float((f.exec_price / f.mid - 1) * 10000) * (1 if f.side == "BUY" else -1),
                           "reason": reason, "signals": signals,
                           "position_id": self.position.position_id if self.position else None,
                           "realized_pnl": None if pnl is None else str(pnl), "cost_model": self.cost.name})

    # ------------------------------------------------------------------ 1) исполнение намерения
    def execute_pending(self, mid: float, ts: int, idx: int) -> None:
        p, self.pending = self.pending, None
        if not p:
            return
        if p["kind"] == "ENTER":
            plan = p["plan"]
            if mid <= plan["stop"]:
                self._push("events", {"ts": ts, "type": "ENTRY_CANCELLED", "why": f"цена исполнения {mid:.6g} ≤ стопа {plan['stop']:.6g}"})
                self.state = "FLAT"
                return
            from .strategies import rr_net
            rr = rr_net(mid, plan["stop"], plan.get("t2") or plan["t1"], self.cost) if plan.get("t1") else None
            if rr is None or rr < self.rcfg.min_rr * 0.8:
                self._push("events", {"ts": ts, "type": "ENTRY_CANCELLED",
                                      "why": f"цена ушла: R:R по цене исполнения {rr if rr is None else round(rr, 2)} < {self.rcfg.min_rr * 0.8:.2f}"})
                self.state = "FLAT"
                return
            usd = min(dec(p["usd"]), self.portfolio.cash - self.cost.gas_usd * 2)
            if usd < self.rcfg.min_trade_usd:
                self._push("events", {"ts": ts, "type": "ENTRY_CANCELLED", "why": "недостаточно денег"})
                self.state = "FLAT"
                return
            f = quote_buy(usd, mid, self.cost)
            fid = self._id(ts, "BUY")
            pid = f"{self.experiment}:{self.tf}:P{ts}"
            self.portfolio.apply_buy(fid, f, pid, ts)
            risk_usd = (f.usd + f.gas_usd) - f.qty * dec(plan["stop"]) * (1 - self.cost.sell_fee - self.cost.slippage - self.cost.stop_slippage)
            self.position = Position(pid, ts, idx, plan["stop"], plan["stop"], plan.get("t1"), plan["t1_frac"], False, plan["trail_atr"],
                                     plan["trail_ref"], p["strategy_tf"] != "1d", plan["time_stop"], p["decision_id"], mid,
                                     str(f.usd + f.gas_usd), str(risk_usd), str(f.qty), mid, mid, mid,
                                     spent=str(f.usd + f.gas_usd), costs=str(f.cost_usd))
            self._op(fid, ts, f, p["reason"], p["signals"])
            self.state = "LONG"
        else:
            self._sell(p["frac"], mid, ts, idx, p["reason"], extra_slip=ZERO, kind=p["kind"])

    def _sell(self, frac: float, mid: float, ts: int, idx: int, reason: str, extra_slip: Decimal, kind: str) -> None:
        pos = self.position
        have = self.portfolio.qty
        qty = have if frac >= 1 else min(have, q_fables(dec(pos.qty_initial) * dec(str(frac))))
        if qty <= 0:
            return
        f = quote_sell(qty, mid, self.cost, extra_slip)
        fid = self._id(ts, kind)
        _, pnl = self.portfolio.apply_sell(fid, f, pos.position_id)
        pos.gross = str(dec(pos.gross) + qty * (dec(mid) - dec(pos.entry_mid)))
        pos.got = str(dec(pos.got) + f.usd - f.gas_usd)
        pos.costs = str(dec(pos.costs) + f.cost_usd)
        pos.exits.append({"ts": ts, "qty": str(qty), "mid": mid, "price": str(f.exec_price), "pnl": str(pnl), "reason": reason})
        self._op(fid, ts, f, reason, [], pnl)
        if self.portfolio.qty <= 0:
            self._close_trade(ts, idx, reason)
        self.state = "LONG" if self.position else "FLAT"

    def _close_trade(self, ts: int, idx: int, reason: str) -> None:
        pos = self.position
        pnl = sum((dec(e["pnl"]) for e in pos.exits), ZERO)
        spent, got, costs = dec(pos.spent), dec(pos.got), dec(pos.costs)
        net = got - spent
        r_usd = dec(pos.risk_usd)
        qty = dec(pos.qty_initial)
        by_stop = any("стоп" in e["reason"].lower() for e in pos.exits)
        tr = {"trade_id": pos.position_id, "mode": self.mode, "trader": self.tf, "strategy": self.strategy["id"],
              "version": self.strategy["version"], "entry_ts": pos.entry_ts, "exit_ts": ts, "bars_held": idx - pos.entry_idx,
              "qty": str(qty), "entry_price": str(spent / qty), "exit_price": str(got / qty), "net_pnl": str(net),
              "gross_pnl": pos.gross, "costs": str(costs), "return_pct": float(net / spent * 100),
              "r_multiple": float(net / r_usd) if r_usd > 0 else None,
              "mfe_pct": (pos.max_high / pos.entry_mid - 1) * 100, "mae_pct": (pos.min_low / pos.entry_mid - 1) * 100,
              "exit_reason": reason, "exits": pos.exits, "decision_ref": pos.decision_ref, "lots_pnl_check": str(pnl)}
        self._push("trades", tr)
        R.on_trade_closed(self.risk, self.rcfg, net, by_stop, idx)
        self.position = None

    # ------------------------------------------------------------------ 2) внутри свечи
    def intrabar(self, o: float, h: float, l: float, ts: int, idx: int) -> None:
        pos = self.position
        if not pos:
            return
        pos.max_high, pos.min_low = max(pos.max_high, h), min(pos.min_low, l)
        if l <= pos.stop:
            px = min(o, pos.stop)
            self._sell(1.0, px, ts, idx, f"стоп {pos.stop:.6g} (исполнено по {px:.6g} + доп. проскальзывание)",
                       extra_slip=self.cost.stop_slippage, kind="STOP")
            return
        if pos.t1 and not pos.t1_done and h >= pos.t1:
            px = max(o, pos.t1)
            pos.t1_done = True
            self._sell(pos.t1_frac, px, ts, idx, f"цель 1 {pos.t1:.6g}: продажа {pos.t1_frac * 100:.0f}%", extra_slip=ZERO, kind="T1")

    # ------------------------------------------------------------------ 3) закрытие свечи
    def on_close(self, a: Analysis, i: int, htf: Analysis | None, j: int | None, market=None, fund=None,
                 global_kill: bool = False) -> dict:
        k = a.s.candles[i]
        pos = self.position
        notes = []
        if pos:
            atr = a.v("atr", i)
            ref = k.c if pos.trail_ref == "close" else k.h
            pos.high_water = max(pos.high_water, ref)
            if atr and (pos.t1_done or not pos.trail_after_t1):
                trail = pos.high_water - pos.trail_atr * atr
                if trail > pos.stop:
                    notes.append(f"trailing stop {pos.stop:.6g} → {trail:.6g}")
                    pos.stop = trail
            if i - pos.entry_idx >= pos.time_stop:
                self.pending = {"kind": "EXIT", "frac": 1.0, "reason": f"time stop: {i - pos.entry_idx} баров", "decision_id": self._id(k.ts, "D")}
                self.state = "EXIT_PENDING"
        tr_prev_atr = a.v("atr", i - 1) if i > 0 else None
        if tr_prev_atr and i > 0:
            tr = max(k.h, a.s.candles[i - 1].c) - min(k.l, a.s.candles[i - 1].c)
            if tr > self.rcfg.anomaly_tr_atr * tr_prev_atr:
                R.on_anomaly(self.risk, self.rcfg, i, tr / tr_prev_atr)
        ctx = Ctx(a, i, htf, j, self.cost, pos is not None, market, fund)
        d = self.fn(ctx)
        d["decision_id"] = self._id(k.ts, "D")
        d["mode"], d["trader"], d["notes"] = self.mode, self.tf, notes
        eq = self.equity_at(k.c)
        R.on_bar(self.risk, self.rcfg, eq, d["decided_at"])
        quality_ok = a.s.quality()["ok"]
        if pos is None and self.pending is None:
            if not quality_ok:
                self.state = "DATA_ERROR"
                d["action_final"] = "NO_TRADE"
                d["risk"] = [{"name": "Качество данных", "ok": False, "detail": f"{a.s.quality()}"}]
            elif self.paused:
                self.state = "PAUSED"
                d["action_final"] = "NO_TRADE"
            elif d["action"] == "WARMUP":
                self.state = "WARMUP"
                d["action_final"] = "WARMUP"
            elif d["action"] == "ENTER":
                checks = R.entry_checks(self.risk, self.rcfg, eq, i, (d["plan"] or {}).get("rr_net"), global_kill)
                d["risk"] = checks
                plan = d["plan"]
                buy = 1 + float(self.cost.buy_fee + self.cost.slippage)
                sell = 1 - float(self.cost.sell_fee + self.cost.slippage + self.cost.stop_slippage)
                usd, binding = R.position_size(self.rcfg, self.cost, eq, self.portfolio.cash,
                                               dec(plan["entry_ref"] * buy), dec(plan["stop"] * sell))
                checks.append({"name": "Размер позиции", "ok": usd > 0, "detail": f"${usd:.2f} (ограничение: {binding})"})
                if all(c["ok"] for c in checks):
                    self.pending = {"kind": "ENTER", "plan": plan, "usd": str(usd), "decision_id": d["decision_id"],
                                    "reason": "; ".join(d["for"]), "signals": d["for"], "strategy_tf": self.tf}
                    self.state = "ENTRY_PENDING"
                    d["action_final"] = "ENTER"
                else:
                    self.state = "RISK_OFF" if self.risk.killed or global_kill else "FLAT"
                    d["action_final"] = "WAIT"
                    d["explanation"] += "\nРиск-фильтр отклонил вход: " + "; ".join(f"{c['name']} ({c['detail']})" for c in checks if not c["ok"])
            else:
                self.state = "RISK_OFF" if self.risk.killed else "FLAT"
                d["action_final"] = d["action"]
        elif pos is not None and d["action"] == "EXIT" and self.pending is None:
            self.pending = {"kind": "EXIT", "frac": 1.0, "reason": "сигнал стратегии: " + "; ".join(d["for"]), "decision_id": d["decision_id"]}
            self.state = "EXIT_PENDING"
            d["action_final"] = "EXIT"
        else:
            d["action_final"] = "HOLD" if pos else d["action"]
        if self.risk.killed and pos is not None and self.pending is None:
            self.pending = {"kind": "EXIT", "frac": 1.0, "reason": "kill switch: " + self.risk.kill_reason, "decision_id": d["decision_id"]}
            self.state = "EXIT_PENDING"
        d["state"] = self.state
        self._push("decisions", d)
        liq = liquidation_value(self.portfolio.qty, k.c, self.cost)
        self._push("equity", {"ts": d["decided_at"], "trader": self.tf, "mode": self.mode, "equity": str(self.portfolio.equity(liq)),
                              "cash": str(self.portfolio.cash), "qty": str(self.portfolio.qty), "mid": k.c, "liq_value": str(liq)})
        return d

    # ------------------------------------------------------------------ одна закрытая свеча (backtest)
    def process_bar(self, a: Analysis, i: int, htf: Analysis | None = None, j: int | None = None, **kw) -> dict | None:
        k = a.s.candles[i]
        if k.ts <= self.last_bar_ts:
            return None  # уже обработана: идемпотентность
        self.execute_pending(k.o, k.ts, i)
        self.intrabar(k.o, k.h, k.l, k.ts, i)
        d = self.on_close(a, i, htf, j, **kw)
        self.last_bar_ts = k.ts
        return d

    # ------------------------------------------------------------------ сохранение состояния
    def to_state(self) -> dict:
        return {"tf": self.tf, "mode": self.mode, "experiment": self.experiment, "state": self.state, "last_bar_ts": self.last_bar_ts,
                "paused": self.paused, "portfolio": self.portfolio.to_state(), "risk": self.risk.to_state(),
                "position": asdict(self.position) if self.position else None, "pending": self.pending,
                "strategy_version": self.strategy["version"], "risk_version": R.RISK_VERSION, "cost_model": self.cost.name}

    def load_state(self, s: dict) -> None:
        assert s["tf"] == self.tf and s["experiment"] == self.experiment and s["mode"] == self.mode
        self.state, self.last_bar_ts, self.paused = s["state"], s["last_bar_ts"], s["paused"]
        self.portfolio = Portfolio.from_state(s["portfolio"])
        self.risk = R.RiskState.from_state(s["risk"])
        self.position = Position(**s["position"]) if s["position"] else None
        self.pending = s["pending"]
