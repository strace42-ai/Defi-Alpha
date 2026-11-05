"""Аналитический слой: все индикаторы, структура, дивергенции и режим рынка для одного ряда.

Все ряды каузальны: значение на баре i зависит только от баров ≤ i (pivots — через confirmed_idx). Поэтому массивы
считаются один раз на весь ряд, а `snapshot(i)` возвращает ровно то, что было известно на закрытии бара i
(это проверяется тестом: расчёт на обрезанном ряду даёт те же значения).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import divergence, indicators as ind, structure
from .candles import Series

INSUFFICIENT = "INSUFFICIENT_HISTORY"

DEFAULT_PARAMS = {
    "ema_fast": 20, "ema_slow": 50, "rsi": 14, "atr": 14, "adx": 14, "bb": 20, "bb_k": 2.0,
    "mfi": 14, "cmf": 20, "relvol": 20, "macd": (12, 26, 9), "pivot_left": 3, "pivot_right": 3,
}


@dataclass
class Analysis:
    s: Series
    p: dict
    arr: dict = field(default_factory=dict)
    pivots: list = field(default_factory=list)
    divs: list = field(default_factory=list)
    events: list = field(default_factory=list)
    fvgs: list = field(default_factory=list)

    def v(self, name: str, i: int):
        a = self.arr.get(name)
        return a[i] if a is not None and 0 <= i < len(a) else None

    def regime(self, i: int) -> dict:
        c = self.s.candles[i].c
        ef, es, ax = self.v("ema_fast", i), self.v("ema_slow", i), self.v("adx", i)
        pdi, ndi, bw = self.v("pdi", i), self.v("ndi", i), self.v("bb_width", i)
        missing = [n for n, x in (("EMA%d" % self.p["ema_fast"], ef), ("EMA%d" % self.p["ema_slow"], es), ("ADX", ax)) if x is None]
        if missing:
            return {"regime": INSUFFICIENT, "missing": missing,
                    "why": f"недостаточно истории для {', '.join(missing)} ({len(self.s.candles[:i + 1])} баров)"}
        bws = [x for x in self.arr["bb_width"][max(0, i - 100):i + 1] if x is not None]
        bw_med = sorted(bws)[len(bws) // 2] if bws else None
        if c > ef > es and ax >= 20 and pdi > ndi:
            r, why = "TREND_UP", f"close>EMA{self.p['ema_fast']}>EMA{self.p['ema_slow']}, ADX {ax:.1f} ≥ 20, +DI>−DI"
        elif c < ef < es and ax >= 20 and ndi > pdi:
            r, why = "TREND_DOWN", f"close<EMA{self.p['ema_fast']}<EMA{self.p['ema_slow']}, ADX {ax:.1f} ≥ 20, −DI>+DI"
        elif ax < 18 and bw is not None and bw_med is not None and bw <= bw_med:
            r, why = "RANGE", f"ADX {ax:.1f} < 18, ширина BB не выше медианы"
        else:
            r, why = "TRANSITION", f"смешанные признаки: ADX {ax:.1f}, EMA {'выше' if ef > es else 'ниже'} медленной"
        return {"regime": r, "why": why, "adx": ax, "pdi": pdi, "ndi": ndi, "bb_width": bw, "bb_width_median": bw_med}

    def snapshot(self, i: int | None = None) -> dict:
        """Состояние на закрытии бара i (по умолчанию — последний закрытый)."""
        i = len(self.s) - 1 if i is None else i
        k = self.s.candles[i]
        n = i + 1
        vals = {}
        for name, mb in self.min_bars().items():
            vals[name] = {"value": self.v(name, i), "min_bars": mb,
                          "status": "OK" if self.v(name, i) is not None else INSUFFICIENT}
        pv = structure.known_at(self.pivots, i)
        return {
            "tf": self.s.tf, "idx": i, "ts": k.ts, "close_ts": k.close_time(self.s.tf), "bars": n,
            "candle": {"o": k.o, "h": k.h, "l": k.l, "c": k.c, "v": k.v},
            "indicators": vals,
            "regime": self.regime(i),
            "pivots": [p.d() for p in pv[-12:]],
            "swings": structure.swing_labels(pv)[-8:],
            "structure_events": [e for e in self.events if e["idx"] <= i][-6:],
            "divergences": [d for d in divergence.known_at(self.divs, i) if i - d["confirmed_idx"] <= 30][-10:],
            "levels": structure.sr_levels(pv),
            "fvg": [f for f in self.fvgs if f["idx"] <= i][-5:],
            "quality": self.s.quality(),
        }

    def min_bars(self) -> dict:
        p = self.p
        f, sl, sg = p["macd"]
        return {"ema_fast": p["ema_fast"], "ema_slow": p["ema_slow"], "rsi": p["rsi"] + 1, "atr": p["atr"] + 1,
                "adx": 2 * p["adx"], "pdi": p["adx"] + 1, "ndi": p["adx"] + 1, "macd": sl, "macd_signal": sl + sg - 1,
                "macd_hist": sl + sg - 1, "bb_mid": p["bb"], "bb_upper": p["bb"], "bb_lower": p["bb"], "bb_width": p["bb"],
                "mfi": p["mfi"] + 1, "cmf": p["cmf"], "obv": 2, "relvol": p["relvol"] + 1, "vol_sma": p["relvol"]}


def analyze(s: Series, params: dict | None = None) -> Analysis:
    p = {**DEFAULT_PARAMS, **(params or {})}
    h, l, c, v = s.col("h"), s.col("l"), s.col("c"), s.col("v")
    a = Analysis(s, p)
    m = ind.macd(c, *p["macd"])
    bb = ind.bollinger(c, p["bb"], p["bb_k"])
    dmi = ind.adx(h, l, c, p["adx"])
    a.arr = {
        "ema_fast": ind.ema(c, p["ema_fast"]), "ema_slow": ind.ema(c, p["ema_slow"]),
        "rsi": ind.rsi(c, p["rsi"]), "atr": ind.atr(h, l, c, p["atr"]),
        "adx": dmi["adx"], "pdi": dmi["pdi"], "ndi": dmi["ndi"],
        "macd": m["macd"], "macd_signal": m["signal"], "macd_hist": m["hist"],
        "bb_mid": bb["mid"], "bb_upper": bb["upper"], "bb_lower": bb["lower"], "bb_width": bb["width"],
        "mfi": ind.mfi(h, l, c, v, p["mfi"]), "cmf": ind.cmf(h, l, c, v, p["cmf"]), "obv": ind.obv(c, v),
        "relvol": ind.rel_volume(v, p["relvol"]), "vol_sma": ind.sma(v, p["relvol"]),
    }
    a.pivots = structure.pivots(s, p["pivot_left"], p["pivot_right"])
    a.events = structure.structure_events(s, a.pivots)
    a.fvgs = structure.fair_value_gaps(s)
    for name, key in (("RSI", "rsi"), ("MFI", "mfi"), ("MACD_HIST", "macd_hist"), ("CMF", "cmf"), ("OBV", "obv")):
        a.divs += divergence.find(a.pivots, a.arr[key], name)
    a.divs.sort(key=lambda d: d["confirmed_idx"])
    return a
