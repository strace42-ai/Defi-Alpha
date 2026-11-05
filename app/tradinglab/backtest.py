"""Backtest: пошаговое воспроизведение закрытых свечей тем же кодом стратегий и движка, mode=BACKTEST.

- Исполнение — по open следующей свечи (первая цена после решения), консервативная модель издержек CONSERVATIVE_V1.
- Старший ТФ — только свечи, закрытые к моменту решения.
- Все три трейдера и Buy & Hold стартуют в один момент start_ts с одинаковым капиталом и той же моделью издержек.
- Результат помечен NOT VALIDATED: история FABLES — одна короткая фаза рынка.
"""
from __future__ import annotations

import bisect

from . import performance
from .analysis import analyze
from .candles import TF_SEC, Series
from .engine import Trader
from .execution import CONSERVATIVE_V1, CostModel, dec, liquidation_value, quote_buy
from .risk import RISK, RISK_VERSION, params_hash
from .strategies import ANALYSIS_PARAMS, STRATEGIES


def experiment_params(cost: CostModel, start_ts: int, initial: str) -> dict:
    return {"start_ts": start_ts, "initial_per_trader": initial, "cost_model": cost.d(), "risk_version": RISK_VERSION,
            "risk": {tf: RISK[tf].d() for tf in RISK},
            "strategies": {tf: {"id": s["id"], "version": s["version"], "params": s["params"]} for tf, (s, _) in STRATEGIES.items()},
            "analysis_params": ANALYSIS_PARAMS}


def buy_and_hold(s1h: Series, start_ts: int, initial: str, cost: CostModel) -> list[dict]:
    ks = [k for k in s1h.candles if k.ts >= start_ts]
    if not ks:
        return []
    usd = dec(initial) - cost.gas_usd
    f = quote_buy(usd, ks[0].o, cost)
    out = []
    for k in ks:
        eq = liquidation_value(f.qty, k.c, cost)
        out.append({"ts": k.ts + 3600, "equity": str(eq), "qty": str(f.qty), "entry_price": str(f.exec_price)})
    return out


def run(series: dict[str, Series], start_ts: int | None = None, cost: CostModel = CONSERVATIVE_V1, initial: str = "1000",
        store=None, experiment: str | None = None, tfs=("1h", "4h", "1d")) -> dict:
    an = {tf: analyze(series[tf], ANALYSIS_PARAMS[tf]) for tf in series}
    if start_ts is None:  # день запуска (хаотичный первый час) — только история, торговля с первых суток после него
        start_ts = series["1d"].candles[1].ts if "1d" in series and len(series["1d"]) > 1 else series["1h"].candles[0].ts
    params = experiment_params(cost, start_ts, initial)
    exp = experiment or f"BT-{params_hash(params)}-{start_ts}"
    if store is not None and store.experiment(exp) is None:
        store.create_experiment(exp, "BACKTEST", start_ts, params, params_hash(params), "backtest на закрытых свечах GT")
    traders = {}
    for tf in tfs:
        tr = Trader(tf, "BACKTEST", exp, cost, initial)
        if store is not None and (st := store.trader_state(exp, tf)):
            tr.load_state(st)
        htf_tf = STRATEGIES[tf][0]["htf"]
        htf = an.get(htf_tf) if htf_tf else None
        htf_close = [k.ts + TF_SEC[htf_tf] for k in htf.s.candles] if htf else []
        for i, k in enumerate(an[tf].s.candles):
            if k.ts < start_ts:
                continue
            t_dec = k.ts + TF_SEC[tf]
            j = bisect.bisect_right(htf_close, t_dec) - 1 if htf else None
            d = tr.process_bar(an[tf], i, htf, j if j is not None and j >= 0 else None)
            if d is not None and store is not None:
                store.commit_bar(tr)
        traders[tf] = tr
    bh = buy_and_hold(series["1h"], start_ts, initial, cost) if "1h" in series else []
    summary = {tf: performance.summarize(t, bh, TF_SEC[tf]) for tf, t in traders.items()}
    return {"experiment": exp, "start_ts": start_ts, "params": params, "traders": traders, "analyses": an, "bh": bh,
            "summary": summary}
