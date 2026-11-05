"""Метрики эффективности. Сделка = вход + все выходы одной позиции (не путать с торговой операцией).

Пока завершённых сделок < MIN_SAMPLE, статистика помечается INSUFFICIENT_SAMPLE; любой результат на истории FABLES
помечается NOT VALIDATED (одна короткая фаза рынка, нет нескольких циклов).
"""
from __future__ import annotations

import statistics
from decimal import Decimal

from .execution import dec

MIN_SAMPLE = 30


def max_drawdown(curve: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for x in curve:
        peak = max(peak, x)
        if peak > 0:
            mdd = max(mdd, (peak - x) / peak)
    return mdd * 100


def summarize(trader, bh_curve: list[dict] | None = None, bar_sec: int = 3600) -> dict:
    p = trader.portfolio
    eq = [float(e["equity"]) for e in trader.equity_curve]
    initial = float(p.initial)
    final = eq[-1] if eq else initial
    last = trader.equity_curve[-1] if trader.equity_curve else None
    liq = dec(last["liq_value"]) if last else Decimal(0)
    trades = trader.trades
    pnl = [float(t["net_pnl"]) for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    streak = mx = 0
    for x in pnl:
        streak = streak + 1 if x <= 0 else 0
        mx = max(mx, streak)
    slips = [o["slip_bps"] for o in trader.ops]
    exposure = sum(1 for e in trader.equity_curve if float(e["qty"]) > 0) / len(eq) * 100 if eq else 0.0
    out = {
        "trader": trader.tf, "mode": trader.mode, "strategy": trader.strategy["id"], "version": trader.strategy["version"],
        "state": trader.state, "initial": initial, "equity": final, "cash": float(p.cash), "fables_qty": float(p.qty),
        "fables_value": float(liq), "net_pnl": final - initial, "net_return_pct": (final / initial - 1) * 100,
        "gross_return_pct": (final - initial + float(p.costs)) / initial * 100,
        "realized_pnl": float(p.realized), "unrealized_pnl": float(p.unrealized(liq)), "fees": float(p.fees),
        "costs_total": float(p.costs), "closed_trades": len(trades), "operations": len(trader.ops),
        "open_positions": 1 if trader.position else 0,
        "win_rate": len(wins) / len(pnl) * 100 if pnl else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "avg_win": statistics.mean(wins) if wins else None, "avg_loss": statistics.mean(losses) if losses else None,
        "expectancy_usd": statistics.mean(pnl) if pnl else None, "expectancy_r": statistics.mean(rs) if rs else None,
        "max_drawdown_pct": max_drawdown(eq), "max_loss_streak": mx,
        "avg_holding_h": statistics.mean(t["bars_held"] for t in trades) * bar_sec / 3600 if trades else None,
        "exposure_pct": exposure, "turnover_x": float(p.turnover) / initial,
        "slippage_bps_mean": statistics.mean(slips) if slips else None,
        "slippage_bps_median": statistics.median(slips) if slips else None,
        "sample": "INSUFFICIENT_SAMPLE" if len(trades) < MIN_SAMPLE else "OK",
        "validation": "NOT VALIDATED", "cost_model": trader.cost.name,
        "vs_cash_usd": final - initial,
    }
    if bh_curve and last:
        bh_at = [b for b in bh_curve if b["ts"] <= last["ts"]]
        if bh_at:
            b = float(bh_at[-1]["equity"])
            out["bh_equity_same_time"] = b
            out["vs_bh_usd"] = final - b
            out["vs_bh_pp"] = (final - b) / initial * 100
    return out
