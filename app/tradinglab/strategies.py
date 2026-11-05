"""Три детерминированные стратегии (v1). Спецификация и обоснование: docs/trading_lab/STAGE_A_ARCHITECTURE.md §7.

Стратегия получает только данные, известные на закрытии бара i (Analysis каузален; HTF — индекс последней ЗАКРЫТОЙ
старшей свечи). Результат — решение с условиями ✓/✗, планом и объяснением, построенным из этих же условий.
Стратегия НЕ исполняет сделки: она формирует намерение, исполнение — engine по следующей доступной цене.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from . import structure
from .analysis import INSUFFICIENT, Analysis
from .execution import CostModel


@dataclass
class Ctx:
    a: Analysis
    i: int
    htf: Analysis | None
    j: int | None                 # индекс последней закрытой HTF-свечи (или None)
    cost: CostModel
    in_position: bool
    market: dict | None = None    # контекст рынка (ETH и т.п.), если доступен
    fund: dict | None = None      # ончейн/фундаментальные метрики, если доступны


def C(name: str, ok: bool | None, detail: str) -> dict:
    """ok=None — условие не проверялось (нет данных); такое условие не блокирует, только если это явно разрешено."""
    return {"name": name, "ok": ok, "detail": detail}


def _fmt(x, nd=5):
    return "н/д" if x is None else f"{x:.{nd}g}"


def rr_net(entry: float, stop: float, target: float, m: CostModel) -> float | None:
    buy = float(m.buy_fee + m.slippage)
    sell = float(m.sell_fee + m.slippage)
    e = entry * (1 + buy)
    t = target * (1 - sell)
    s = stop * (1 - sell - float(m.stop_slippage))
    if e <= s:
        return None
    return (t - e) / (e - s)


def roundtrip(m: CostModel) -> float:
    return float(m.buy_fee + m.sell_fee + 2 * m.slippage)


def decision(strategy: dict, ctx: Ctx, action: str, conds: list[dict], plan: dict | None, inputs: dict, nxt: str) -> dict:
    pos = [c for c in conds if c["ok"]]
    neg = [c for c in conds if c["ok"] is False]
    na = [c for c in conds if c["ok"] is None]
    head = {"ENTER": "ВХОД (исполнение по следующей доступной цене)", "WAIT": "WAIT", "HOLD": "HOLD", "EXIT": "ВЫХОД",
            "WARMUP": "WARMUP / NO TRADE"}[action]
    lines = [f"Решение: {head}"]
    lines += [f"✓ {c['name']}: {c['detail']}" for c in pos]
    lines += [f"✗ {c['name']}: {c['detail']}" for c in neg]
    lines += [f"— {c['name']}: {c['detail']} (не проверено)" for c in na]
    if nxt:
        lines.append(f"Следующая проверка: {nxt}")
    k = ctx.a.s.candles[ctx.i]
    return {"strategy": strategy["id"], "version": strategy["version"], "tf": ctx.a.s.tf, "bar_ts": k.ts,
            "decided_at": k.close_time(ctx.a.s.tf), "action": action, "conditions": conds, "plan": plan,
            "for": [c["name"] for c in pos], "against": [c["name"] for c in neg], "next": nxt, "inputs": inputs,
            "explanation": "\n".join(lines)}


def _warm(strategy, ctx, need: int, what: str) -> dict | None:
    have = ctx.i + 1
    if have < need:
        return decision(strategy, ctx, "WARMUP", [C("История", False, f"{have} из {need} закрытых баров ({what})")], None,
                        {}, f"после накопления {need - have} баров")
    return None


def _htf_ok(ctx: Ctx, max_adx_down: float, min_rsi: float) -> dict:
    if ctx.htf is None or ctx.j is None:
        return C("Контекст старшего ТФ", False, "нет закрытых свечей старшего ТФ")
    r = ctx.htf.regime(ctx.j)
    rsi = ctx.htf.v("rsi", ctx.j)
    if r["regime"] == INSUFFICIENT or rsi is None:
        return C("Контекст старшего ТФ", False, f"{ctx.htf.s.tf}: {INSUFFICIENT}")
    bad = r["regime"] == "TREND_DOWN" and (r.get("adx") or 0) > max_adx_down
    ok = not bad and rsi > min_rsi
    return C("Контекст старшего ТФ", ok, f"{ctx.htf.s.tf}: {r['regime']}, ADX {_fmt(r.get('adx'), 3)}, RSI {rsi:.1f} "
             f"(нужно: не сильный нисходящий тренд, RSI > {min_rsi:g})")


# ===================================================================== S1 — 1H liquidity sweep reversal
S1 = {"id": "S1-1H-sweep-reversal", "version": "1.0.0", "tf": "1h", "htf": "4h",
      "params": {"sweep_tol_atr": 0.1, "sweep_relvol": 1.3, "confirm_bars": 3, "stop_atr": 0.25, "min_target_atr": 2.0,
                 "min_gross_vs_roundtrip": 3.0, "rsi_max": 70, "bbw_min_pct": 10, "htf_max_adx_down": 30, "htf_min_rsi": 35,
                 "t1_frac": 0.5, "trail_atr": 2.0, "time_stop": 24}}


def s1(ctx: Ctx) -> dict:
    P, a, i = S1["params"], ctx.a, ctx.i
    w = _warm(S1, ctx, max(a.min_bars()[x] for x in ("atr", "rsi", "relvol", "bb_width", "mfi", "cmf")) + P["confirm_bars"],
              "ATR/RSI/BB/MFI/CMF/объём")
    if w:
        return w
    k = a.s.candles
    atr = a.v("atr", i)
    inputs = {x: a.v(x, i) for x in ("rsi", "mfi", "cmf", "obv", "atr", "bb_width", "relvol", "macd_hist")}
    if ctx.in_position:
        return decision(S1, ctx, "HOLD", [C("Позиция", True, "сопровождение: стоп/цель/trailing/time stop в engine")], None,
                        inputs, "на закрытии следующего бара")
    conds: list[dict] = []
    sweep = None
    for kk in range(i - 1, i - 1 - P["confirm_bars"], -1):
        sw = structure.liquidity_sweep_low(a.s, kk, a.pivots, a.v("atr", kk - 1), P["sweep_tol_atr"])
        if sw and (a.v("relvol", kk) or 0) >= P["sweep_relvol"]:
            sweep = sw
            break
    conds.append(C("Liquidity sweep swing low", sweep is not None,
                   f"прокол уровня {_fmt(sweep['level'])} до {_fmt(sweep['sweep_low'])} ({sweep['depth_atr']:.2f} ATR) на баре {sweep['idx']}, "
                   f"объём ≥ {P['sweep_relvol']}× SMA20" if sweep else
                   f"за {P['confirm_bars']} бара нет прокола подтверждённого минимума с возвратом и объёмом ≥ {P['sweep_relvol']}×"))
    if sweep:
        kk = sweep["idx"]
        conf = k[i].c > k[kk].h
        conds.append(C("Подтверждение: закрытие выше максимума sweep-свечи", conf, f"close {_fmt(k[i].c)} vs high {_fmt(k[kk].h)}"))
        divs = [d for d in a.divs if d["confirmed_idx"] <= i and d["type"] == "regular_bullish" and d["indicator"] in ("RSI", "MFI", "OBV")
                and d["p2"]["idx"] >= kk - 6]
        cmf, obv_now, obv_5 = a.v("cmf", i), a.v("obv", i), a.v("obv", i - 5)
        flows = []
        if divs:
            flows.append(f"бычья дивергенция {divs[-1]['indicator']} (pivots {divs[-1]['p1']['idx']}→{divs[-1]['p2']['idx']})")
        if cmf is not None and cmf > 0:
            flows.append(f"CMF {cmf:.3f} > 0")
        if obv_now is not None and obv_5 is not None and obv_now > obv_5:
            flows.append("OBV выше, чем 5 баров назад")
        conds.append(C("Подтверждение денежным потоком", bool(flows), "; ".join(flows) if flows else
                       f"нет дивергенции RSI/MFI/OBV, CMF {_fmt(cmf, 3)} ≤ 0, OBV не растёт"))
    conds.append(_htf_ok(ctx, P["htf_max_adx_down"], P["htf_min_rsi"]))
    rsi = a.v("rsi", i)
    conds.append(C(f"RSI ≤ {P['rsi_max']}", rsi is not None and rsi <= P["rsi_max"], f"RSI {_fmt(rsi, 3)}"))
    bws = sorted(x for x in a.arr["bb_width"][max(0, i - 100):i + 1] if x is not None)
    bw = a.v("bb_width", i)
    pct = 100 * sum(1 for x in bws if x < bw) / len(bws) if bws and bw is not None else None
    conds.append(C("Ширина BB не в нижних 10%", pct is not None and pct >= P["bbw_min_pct"], f"перцентиль {_fmt(pct, 3)}%"))
    plan = None
    if sweep:
        entry = k[i].c
        stop = sweep["sweep_low"] - P["stop_atr"] * atr
        cands = [p.price for p in structure.known_at(a.pivots, i) if p.kind == "H"] + [lv["price"] for lv in structure.sr_levels(structure.known_at(a.pivots, i))]
        bu = a.v("bb_upper", i)
        if bu:
            cands.append(bu)
        cands = sorted(x for x in cands if x - entry >= P["min_target_atr"] * atr)
        t1 = cands[0] if cands else None
        rr = rr_net(entry, stop, t1, ctx.cost) if t1 else None
        gross_ok = t1 is not None and (t1 / entry - 1) >= P["min_gross_vs_roundtrip"] * roundtrip(ctx.cost)
        conds.append(C("Цель ≥ 2 ATR и ≥ 3× круг издержек", gross_ok,
                       f"цель {_fmt(t1)} (+{(t1 / entry - 1) * 100:.2f}%), круг издержек {roundtrip(ctx.cost) * 100:.2f}%" if t1 else
                       "нет сопротивления выше на ≥ 2 ATR"))
        plan = {"entry_ref": entry, "stop": stop, "t1": t1, "t1_frac": P["t1_frac"], "trail_atr": P["trail_atr"],
                "trail_ref": "close", "time_stop": P["time_stop"], "rr_net": rr}
    ok = all(c["ok"] for c in conds)
    first_bad = next((c["name"] for c in conds if not c["ok"]), "")
    return decision(S1, ctx, "ENTER" if ok else "WAIT", conds, plan, inputs,
                    "" if ok else f"на закрытии следующего 1H-бара; блокирует: {first_bad}")


# ===================================================================== S2 — 4H structure shift + pullback
S2 = {"id": "S2-4H-structure-pullback", "version": "1.0.0", "tf": "4h", "htf": "1d",
      "params": {"choch_lookback": 40, "fib_shallow": "0.382", "fib_deep": "0.618", "fib_invalid": "0.786", "ema_touch": 1.005,
                 "adx_min": 18, "cmf_min": -0.05, "stop_atr": 0.3, "min_rr_t1": 1.0, "t1_frac": 0.5, "trail_atr": 2.5,
                 "time_stop": 30}}


def s2(ctx: Ctx) -> dict:
    P, a, i = S2["params"], ctx.a, ctx.i
    w = _warm(S2, ctx, max(a.min_bars()[x] for x in ("ema_fast", "adx", "macd_hist", "cmf", "atr")), "EMA20/ADX/MACD/CMF")
    if w:
        return w
    k = a.s.candles
    atr = a.v("atr", i)
    inputs = {x: a.v(x, i) for x in ("ema_fast", "adx", "pdi", "ndi", "macd_hist", "cmf", "atr", "rsi")}
    ev = [e for e in a.events if e["idx"] <= i]
    if ctx.in_position:
        brk = [e for e in ev if e["idx"] == i and e["type"] in ("BOS_DOWN", "CHoCH_DOWN")]
        c = C("Медвежий слом структуры", bool(brk), f"{brk[0]['type']} уровня {_fmt(brk[0]['level'])}" if brk else "нет")
        return decision(S2, ctx, "EXIT" if brk else "HOLD", [c], None, inputs, "на закрытии следующего 4H-бара")
    conds: list[dict] = []
    last = ev[-1] if ev else None
    choch = [e for e in ev if e["type"] == "CHoCH_UP" and i - e["idx"] <= P["choch_lookback"]]
    st_ok = bool(last and last["type"] in ("BOS_UP", "CHoCH_UP") and choch)
    conds.append(C("Структура вверх (CHoCH_UP, затем без слома вниз)", st_ok,
                   f"последнее событие {last['type'] if last else 'нет'}; CHoCH_UP за {P['choch_lookback']} баров: {'да' if choch else 'нет'}"))
    imp = structure.last_impulse_up(a.pivots, i)
    plan = None
    if imp:
        L, H = imp
        fib = structure.log_fib(L.price, H.price)
        r_sh, r_dp, r_inv = (fib["retracement"][P[x]] for x in ("fib_shallow", "fib_deep", "fib_invalid"))
        pull = [x.l for x in k[H.idx + 1:i + 1]]
        pull_low = min(pull) if pull else None
        ema = a.v("ema_fast", i)
        in_zone = pull_low is not None and (r_dp * 0.99 <= pull_low <= r_sh or (ema is not None and pull_low <= ema * P["ema_touch"] and pull_low >= r_inv))
        conds.append(C("Откат в зону log-Fib 0,382–0,618 или к EMA20", in_zone,
                       f"импульс {_fmt(L.price)}→{_fmt(H.price)}; минимум отката {_fmt(pull_low)}; зона {_fmt(r_dp)}–{_fmt(r_sh)}; EMA20 {_fmt(ema)}"))
        conds.append(C("Структура отката не сломана (close > 0,786)", k[i].c > r_inv, f"close {_fmt(k[i].c)} vs {_fmt(r_inv)}"))
        if pull_low is not None:
            stop = pull_low - P["stop_atr"] * atr
            t1 = H.price
            t2 = pull_low * math.exp(1.618 * math.log(H.price / L.price))
            entry = k[i].c
            rr1, rr2 = rr_net(entry, stop, t1, ctx.cost) if t1 > entry else None, rr_net(entry, stop, t2, ctx.cost)
            conds.append(C(f"Чистый R:R до T1 ≥ {P['min_rr_t1']}", rr1 is not None and rr1 >= P["min_rr_t1"], f"T1 {_fmt(t1)}, R:R {_fmt(rr1, 3)}"))
            plan = {"entry_ref": entry, "stop": stop, "t1": t1, "t2": t2, "t1_frac": P["t1_frac"], "trail_atr": P["trail_atr"],
                    "trail_ref": "high", "time_stop": P["time_stop"], "rr_net": rr2}
    else:
        conds.append(C("Импульс (swing low → swing high)", False, "нет подтверждённой пары pivots"))
    hist, hist_prev = a.v("macd_hist", i), a.v("macd_hist", i - 1)
    trig = k[i].c > k[i].o and k[i].c > k[i - 1].h and hist is not None and hist_prev is not None and hist > hist_prev
    conds.append(C("Триггер: бычье закрытие выше high[-1] и разворот MACD hist", trig,
                   f"close {_fmt(k[i].c)}, open {_fmt(k[i].o)}, high[-1] {_fmt(k[i - 1].h)}, hist {_fmt(hist_prev, 3)}→{_fmt(hist, 3)}"))
    adx_, pdi, ndi = a.v("adx", i), a.v("pdi", i), a.v("ndi", i)
    conds.append(C(f"ADX ≥ {P['adx_min']} и +DI > −DI", adx_ is not None and adx_ >= P["adx_min"] and pdi > ndi,
                   f"ADX {_fmt(adx_, 3)}, +DI {_fmt(pdi, 3)}, −DI {_fmt(ndi, 3)}"))
    cmf = a.v("cmf", i)
    conds.append(C(f"CMF ≥ {P['cmf_min']}", cmf is not None and cmf >= P["cmf_min"], f"CMF {_fmt(cmf, 3)}"))
    mk = ctx.market or {}
    if "eth_above_ema20" in mk:
        conds.append(C("Рынок: ETH 1D выше EMA20 или FABLES сильнее ETH за 7д",
                       bool(mk["eth_above_ema20"] or mk.get("rs_vs_eth_7d", 0) > 0), f"{mk}"))
    else:
        conds.append(C("Рынок: ETH 1D", None, "данные ETH не подключены"))
    ok = all(c["ok"] is not False for c in conds) and any(c["ok"] for c in conds) and plan is not None
    first_bad = next((c["name"] for c in conds if c["ok"] is False), "")
    return decision(S2, ctx, "ENTER" if ok else "WAIT", conds, plan, inputs,
                    "" if ok else f"на закрытии следующего 4H-бара; блокирует: {first_bad}")


# ===================================================================== S3 — 1D range accumulation breakout
S3 = {"id": "S3-1D-range-breakout", "version": "1.0.0", "tf": "1d", "htf": None,
      "params": {"min_bars": 30, "range_min": 10, "range_max": 30, "range_max_width": 1.45, "touch_pct": 0.03, "min_touches": 2,
                 "break_atr": 0.5, "vol_x": 1.5, "vol_n": 10, "obv_n": 20, "stop_atr_max": 2.5, "t1_frac": 0.5,
                 "trail_atr": 3.0, "time_stop": 45, "liq_drop_max": -0.25, "sell_buy_max": 4.0}}


def s3(ctx: Ctx) -> dict:
    P, a, i = S3["params"], ctx.a, ctx.i
    w = _warm(S3, ctx, P["min_bars"], "дневной тренд и диапазон")
    if w:
        return w
    k = a.s.candles
    atr = a.v("atr", i)
    inputs = {x: a.v(x, i) for x in ("atr", "cmf", "obv", "rsi", "mfi")}
    if ctx.in_position:
        return decision(S3, ctx, "HOLD", [C("Позиция", True, "сопровождение: trailing 3 ATR / time stop в engine")], None,
                        inputs, "на закрытии следующей дневной свечи")
    conds: list[dict] = []
    rng = None
    for W in range(min(P["range_max"], i), P["range_min"] - 1, -1):
        win = k[i - W:i]
        hi, lo = max(x.h for x in win), min(x.l for x in win)
        th = sum(1 for x in win if x.h >= hi * (1 - P["touch_pct"]))
        tl = sum(1 for x in win if x.l <= lo * (1 + P["touch_pct"]))
        if hi / lo <= P["range_max_width"] and th >= P["min_touches"] and tl >= P["min_touches"]:
            rng = {"bars": W, "hi": hi, "lo": lo, "touch_hi": th, "touch_lo": tl}
            break
    conds.append(C("Диапазон накопления ≥ 10 дней, ширина ≤ 45%, ≥ 2 касаний", rng is not None,
                   f"{rng['bars']} дн., {_fmt(rng['lo'])}–{_fmt(rng['hi'])}, касаний {rng['touch_lo']}/{rng['touch_hi']}" if rng else "не найден"))
    plan = None
    if rng:
        conds.append(C("Пробой: close > верх + 0,5 ATR", k[i].c > rng["hi"] + P["break_atr"] * atr,
                       f"close {_fmt(k[i].c)} vs {_fmt(rng['hi'] + P['break_atr'] * atr)}"))
        mid = math.sqrt(rng["hi"] * rng["lo"])
        stop = max(mid, k[i].c - P["stop_atr_max"] * atr)
        t1 = rng["hi"] * (rng["hi"] / rng["lo"])
        plan = {"entry_ref": k[i].c, "stop": stop, "t1": t1, "t1_frac": P["t1_frac"], "trail_atr": P["trail_atr"],
                "trail_ref": "close", "time_stop": P["time_stop"], "rr_net": rr_net(k[i].c, stop, t1, ctx.cost)}
    base = sum(x.v for x in k[i - P["vol_n"]:i]) / P["vol_n"]
    conds.append(C(f"Объём ≥ {P['vol_x']}× SMA{P['vol_n']}", base > 0 and k[i].v >= P["vol_x"] * base, f"{k[i].v / base:.2f}×" if base else "н/д"))
    cmf = a.v("cmf", i)
    conds.append(C("CMF20 > 0", cmf is not None and cmf > 0, f"CMF {_fmt(cmf, 3)}"))
    obv = a.arr["obv"]
    conds.append(C("OBV на 20-дневном максимуме", obv[i] is not None and obv[i] >= max(x for x in obv[i - P["obv_n"] + 1:i + 1] if x is not None),
                   f"OBV {_fmt(obv[i], 4)}"))
    f = ctx.fund or {}
    conds.append(C("Ликвидность не упала > 25% за 7д", None if "liq_change_7d" not in f else f["liq_change_7d"] > P["liq_drop_max"],
                   "нет истории снимков ликвидности" if "liq_change_7d" not in f else f"{f['liq_change_7d'] * 100:.1f}%"))
    conds.append(C("Продажи/покупки h24 < 4", None if "sell_buy_h24" not in f else f["sell_buy_h24"] < P["sell_buy_max"],
                   "нет снимка DexScreener" if "sell_buy_h24" not in f else f"{f['sell_buy_h24']:.2f}"))
    ok = all(c["ok"] is not False for c in conds) and plan is not None
    first_bad = next((c["name"] for c in conds if c["ok"] is False), "")
    return decision(S3, ctx, "ENTER" if ok else "WAIT", conds, plan, inputs,
                    "" if ok else f"на закрытии следующей дневной свечи; блокирует: {first_bad}")


STRATEGIES: dict[str, tuple[dict, Callable[[Ctx], dict]]] = {"1h": (S1, s1), "4h": (S2, s2), "1d": (S3, s3)}
ANALYSIS_PARAMS = {"1h": {}, "4h": {}, "1d": {"ema_fast": 10, "ema_slow": 20, "adx": 10, "pivot_left": 2, "pivot_right": 2}}
