"""Ценовая структура: подтверждённые pivots, BOS/CHoCH, liquidity sweeps, equal highs/lows, FVG, уровни, log-Fibonacci.

Ключевое правило против перерисовки: pivot на баре i становится известен только на баре i + right (confirmed_idx).
Любое событие, использующее pivot, может произойти не раньше бара confirmed_idx + 1.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .candles import Series


@dataclass(frozen=True)
class Pivot:
    kind: str           # "H" | "L"
    idx: int            # бар экстремума
    ts: int
    price: float
    confirmed_idx: int  # бар, на закрытии которого pivot подтверждён

    def d(self) -> dict:
        return asdict(self)


def pivots(s: Series, left: int = 3, right: int = 3) -> list[Pivot]:
    h, l, ts = s.col("h"), s.col("l"), s.ts
    out: list[Pivot] = []
    for i in range(left, len(h) - right):
        if h[i] > max(h[i - left:i]) and h[i] >= max(h[i + 1:i + right + 1]):
            out.append(Pivot("H", i, ts[i], h[i], i + right))
        if l[i] < min(l[i - left:i]) and l[i] <= min(l[i + 1:i + right + 1]):
            out.append(Pivot("L", i, ts[i], l[i], i + right))
    return out


def known_at(pvs: list[Pivot], i: int) -> list[Pivot]:
    """Pivots, известные на закрытии бара i."""
    return [p for p in pvs if p.confirmed_idx <= i]


def structure_events(s: Series, pvs: list[Pivot]) -> list[dict]:
    """Пробои структуры по закрытию: close выше последнего известного pivot high → BOS_UP (если тренд не был вниз)
    или CHoCH_UP (смена с нисходящего). Симметрично вниз. Каждый pivot пробивается не более одного раза."""
    c = s.col("c")
    events: list[dict] = []
    trend = "none"
    broken: set[tuple[str, int]] = set()
    for i in range(len(c)):
        kn = [p for p in pvs if p.confirmed_idx < i]
        hs = [p for p in kn if p.kind == "H"]
        ls_ = [p for p in kn if p.kind == "L"]
        if hs:
            ph = hs[-1]
            if ("H", ph.idx) not in broken and c[i] > ph.price:
                broken.add(("H", ph.idx))
                typ = "CHoCH_UP" if trend == "down" else "BOS_UP"
                trend = "up"
                events.append({"type": typ, "idx": i, "ts": s.ts[i], "level": ph.price, "pivot_idx": ph.idx})
        if ls_:
            pl = ls_[-1]
            if ("L", pl.idx) not in broken and c[i] < pl.price:
                broken.add(("L", pl.idx))
                typ = "CHoCH_DOWN" if trend == "up" else "BOS_DOWN"
                trend = "down"
                events.append({"type": typ, "idx": i, "ts": s.ts[i], "level": pl.price, "pivot_idx": pl.idx})
    return events


def swing_labels(pvs: list[Pivot]) -> list[dict]:
    """HH/LH для highs и HL/LL для lows относительно предыдущего pivot того же типа."""
    out = []
    prev: dict[str, Pivot] = {}
    for p in pvs:
        q = prev.get(p.kind)
        if q is not None:
            if p.kind == "H":
                lab = "HH" if p.price > q.price else "LH"
            else:
                lab = "HL" if p.price > q.price else "LL"
            out.append({"label": lab, **p.d()})
        prev[p.kind] = p
    return out


def liquidity_sweep_low(s: Series, i: int, pvs: list[Pivot], atr_i: float | None, tol_atr: float = 0.1) -> dict | None:
    """Бар i прокалывает последний известный swing low (минимум ниже уровня − tol·ATR) и закрывается выше уровня."""
    if atr_i is None:
        return None
    kn = [p for p in pvs if p.kind == "L" and p.confirmed_idx < i]
    if not kn:
        return None
    pl = kn[-1]
    k = s.candles[i]
    if k.l < pl.price - tol_atr * atr_i and k.c > pl.price:
        return {"idx": i, "ts": k.ts, "level": pl.price, "pivot_idx": pl.idx, "sweep_low": k.l,
                "depth_atr": (pl.price - k.l) / atr_i}
    return None


def equal_levels(pvs: list[Pivot], kind: str, tol_pct: float = 0.4) -> list[dict]:
    ps = [p for p in pvs if p.kind == kind]
    out = []
    for a, b in zip(ps, ps[1:]):
        if abs(math.log(b.price / a.price)) * 100 <= tol_pct:
            out.append({"kind": "EQH" if kind == "H" else "EQL", "a_idx": a.idx, "b_idx": b.idx,
                        "level": (a.price + b.price) / 2, "confirmed_idx": b.confirmed_idx})
    return out


def fair_value_gaps(s: Series) -> list[dict]:
    """FVG на трёх свечах: бычий — low[i] > high[i-2], медвежий — high[i] < low[i-2]. Известен на закрытии i."""
    out = []
    k = s.candles
    for i in range(2, len(k)):
        if k[i].l > k[i - 2].h:
            out.append({"type": "FVG_UP", "idx": i, "ts": k[i].ts, "bottom": k[i - 2].h, "top": k[i].l})
        elif k[i].h < k[i - 2].l:
            out.append({"type": "FVG_DOWN", "idx": i, "ts": k[i].ts, "bottom": k[i].h, "top": k[i - 2].l})
    return out


def sr_levels(pvs: list[Pivot], tol_pct: float = 1.5, max_levels: int = 8) -> list[dict]:
    """Кластеры цен подтверждённых pivots (лог-расстояние ≤ tol). Сила = число касаний."""
    clusters: list[list[Pivot]] = []
    for p in sorted(pvs, key=lambda x: x.price):
        if clusters and abs(math.log(p.price / clusters[-1][-1].price)) * 100 <= tol_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    out = []
    for cl in clusters:
        lvl = math.exp(sum(math.log(p.price) for p in cl) / len(cl))
        out.append({"price": lvl, "touches": len(cl), "last_idx": max(p.idx for p in cl),
                    "kinds": "".join(sorted({p.kind for p in cl}))})
    out.sort(key=lambda d: (-d["touches"], -d["last_idx"]))
    return sorted(out[:max_levels], key=lambda d: d["price"])


RETR = (0.236, 0.382, 0.5, 0.618, 0.786)
EXT = (1.0, 1.272, 1.618, 2.0)


def log_fib(low: float, high: float, anchor: float | None = None) -> dict:
    """Логарифмические уровни: retracement r → exp(ln H − r·(ln H − ln L));
    extension от anchor (по умолчанию L) → exp(ln anchor + r·(ln H − ln L))."""
    a, b = math.log(low), math.log(high)
    rng = b - a
    base = math.log(anchor) if anchor else a
    return {"low": low, "high": high,
            "retracement": {str(r): math.exp(b - r * rng) for r in RETR},
            "extension": {str(r): math.exp(base + r * rng) for r in EXT}}


def last_impulse_up(pvs: list[Pivot], i: int) -> tuple[Pivot, Pivot] | None:
    """Последняя пара (известный swing low → более поздний известный swing high) на момент бара i."""
    kn = [p for p in pvs if p.confirmed_idx <= i]
    hs = [p for p in kn if p.kind == "H"]
    if not hs:
        return None
    ph = hs[-1]
    lows = [p for p in kn if p.kind == "L" and p.idx < ph.idx]
    if not lows:
        return None
    return lows[-1], ph
