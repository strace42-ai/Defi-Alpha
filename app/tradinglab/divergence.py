"""Дивергенции по подтверждённым pivots.

Бычьи — по парам swing lows, медвежьи — по парам swing highs (соседние pivots одного типа):
- regular bullish: цена LL (p2 < p1), индикатор выше (i2 > i1);  hidden bullish: цена HL, индикатор ниже;
- regular bearish: цена HH, индикатор ниже;                          hidden bearish: цена LH, индикатор выше.
Двойная — две подряд дивергенции одного типа на трёх pivots.
Дивергенция становится известна на баре confirmed_idx второго pivot. Это информация, а не сигнал сделки.
"""
from __future__ import annotations

import math

from .structure import Pivot

# минимальная разница индикатора, чтобы не считать шум (в единицах индикатора)
MIN_DIFF = {"RSI": 2.0, "MFI": 3.0, "MACD_HIST": 0.0, "CMF": 0.03, "OBV": 0.0}


def find(pvs: list[Pivot], ind: list, name: str, min_bars: int = 4, max_bars: int = 60,
         min_price_pct: float = 0.3) -> list[dict]:
    out: list[dict] = []
    md = MIN_DIFF.get(name, 0.0)
    for kind in ("L", "H"):
        ps = [p for p in pvs if p.kind == kind]
        prev_type = None
        for a, b in zip(ps, ps[1:]):
            dist = b.idx - a.idx
            ia, ib = (ind[a.idx] if a.idx < len(ind) else None), (ind[b.idx] if b.idx < len(ind) else None)
            if ia is None or ib is None or not (min_bars <= dist <= max_bars):
                prev_type = None
                continue
            dp = math.log(b.price / a.price) * 100
            di = ib - ia
            if abs(dp) < min_price_pct or abs(di) <= md:
                prev_type = None
                continue
            typ = None
            if kind == "L":
                if dp < 0 < di:
                    typ = "regular_bullish"
                elif dp > 0 > di:
                    typ = "hidden_bullish"
            else:
                if dp > 0 > di:
                    typ = "regular_bearish"
                elif dp < 0 < di:
                    typ = "hidden_bearish"
            if typ is None:
                prev_type = None
                continue
            out.append({"indicator": name, "type": typ, "multiplicity": 2 if prev_type == typ else 1,
                        "p1": {"idx": a.idx, "ts": a.ts, "price": a.price, "ind": ia},
                        "p2": {"idx": b.idx, "ts": b.ts, "price": b.price, "ind": ib},
                        "bars_between": dist, "price_change_pct": round(dp, 3), "ind_change": di,
                        "confirmed_idx": b.confirmed_idx})
            prev_type = typ
    out.sort(key=lambda d: d["confirmed_idx"])
    return out


def known_at(divs: list[dict], i: int) -> list[dict]:
    return [d for d in divs if d["confirmed_idx"] <= i]
