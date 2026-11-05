"""Индикаторы. Чистые функции над списками float; результат выровнен по индексам входа, None — значение недоступно.

Минимальное число свечей (MIN_BARS) задаётся явно: до него индикатор возвращает None, а analysis помечает его
INSUFFICIENT_HISTORY. Значения никогда не «достраиваются».
Соглашения (как у Wilder / TradingView): RSI, ATR, ADX — сглаживание Уайлдера (RMA), засев средним первых n;
EMA засевается SMA первых n; Bollinger — стандартное отклонение генеральной совокупности.
Объём — в USD (GeckoTerminal). Для MFI/CMF/OBV важна относительная динамика, масштаб объёма не влияет на знак сигналов.
"""
from __future__ import annotations

import math
from typing import Optional

Num = Optional[float]


def MIN_BARS(name: str, **p) -> int:
    n = p.get("n", 14)
    return {
        "sma": n, "ema": n, "rsi": n + 1, "atr": n + 1, "adx": 2 * n, "bb": p.get("n", 20),
        "macd": p.get("slow", 26) + p.get("signal", 9) - 1, "mfi": n + 1, "cmf": p.get("n", 20), "obv": 2,
        "relvol": p.get("n", 20) + 1,
    }[name]


def sma(x: list[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x: list[Num], n: int) -> list[Num]:
    """EMA с засевом SMA первых n доступных (не-None) значений."""
    out: list[Num] = [None] * len(x)
    k = 2.0 / (n + 1)
    buf: list[float] = []
    prev: Num = None
    for i, v in enumerate(x):
        if v is None:
            continue
        if prev is None:
            buf.append(v)
            if len(buf) == n:
                prev = sum(buf) / n
                out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def _rma_seeded(x: list[Num], n: int, start: int) -> list[Num]:
    """Wilder: первое значение — среднее x[start..start+n-1], далее (prev*(n-1)+v)/n."""
    out: list[Num] = [None] * len(x)
    if len(x) < start + n:
        return out
    prev = sum(x[start:start + n]) / n  # type: ignore[arg-type]
    out[start + n - 1] = prev
    for i in range(start + n, len(x)):
        prev = (prev * (n - 1) + x[i]) / n  # type: ignore[operator]
        out[i] = prev
    return out


def rsi(c: list[float], n: int = 14) -> list[Num]:
    out: list[Num] = [None] * len(c)
    if len(c) < n + 1:
        return out
    gains = [0.0] + [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    losses = [0.0] + [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]
    ag, al = _rma_seeded(gains, n, 1), _rma_seeded(losses, n, 1)
    for i in range(n, len(c)):
        g, lo = ag[i], al[i]
        if g is None or lo is None:
            continue
        out[i] = 100.0 if lo == 0 and g > 0 else 50.0 if lo == 0 else 100.0 - 100.0 / (1.0 + g / lo)
    return out


def macd(c: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list[Num]]:
    ef, es = ema(c, fast), ema(c, slow)
    line: list[Num] = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist: list[Num] = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return {"macd": line, "signal": sig, "hist": hist}


def true_range(h: list[float], l: list[float], c: list[float]) -> list[Num]:
    tr: list[Num] = [None]
    for i in range(1, len(c)):
        tr.append(max(h[i], c[i - 1]) - min(l[i], c[i - 1]))
    return tr


def atr(h: list[float], l: list[float], c: list[float], n: int = 14) -> list[Num]:
    return _rma_seeded(true_range(h, l, c), n, 1)


def adx(h: list[float], l: list[float], c: list[float], n: int = 14) -> dict[str, list[Num]]:
    L = len(c)
    pdm: list[Num] = [None] + [0.0] * (L - 1)
    ndm: list[Num] = [None] + [0.0] * (L - 1)
    for i in range(1, L):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if up > dn and up > 0 else 0.0
        ndm[i] = dn if dn > up and dn > 0 else 0.0
    tr = true_range(h, l, c)
    s_tr, s_p, s_n = _rma_seeded(tr, n, 1), _rma_seeded(pdm, n, 1), _rma_seeded(ndm, n, 1)
    pdi: list[Num] = [None] * L
    ndi: list[Num] = [None] * L
    dx: list[Num] = [None] * L
    for i in range(L):
        if s_tr[i] is None or not s_tr[i]:
            continue
        pdi[i] = 100.0 * s_p[i] / s_tr[i]  # type: ignore[operator]
        ndi[i] = 100.0 * s_n[i] / s_tr[i]  # type: ignore[operator]
        tot = pdi[i] + ndi[i]  # type: ignore[operator]
        dx[i] = 0.0 if tot == 0 else 100.0 * abs(pdi[i] - ndi[i]) / tot  # type: ignore[operator]
    adx_ = _rma_seeded(dx, n, n)  # первый DX на индексе n → первый ADX на 2n-1
    return {"adx": adx_, "pdi": pdi, "ndi": ndi}


def bollinger(c: list[float], n: int = 20, k: float = 2.0) -> dict[str, list[Num]]:
    mid = sma(c, n)
    up: list[Num] = [None] * len(c)
    lo: list[Num] = [None] * len(c)
    bw: list[Num] = [None] * len(c)
    pb: list[Num] = [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((v - m) ** 2 for v in w) / n)  # type: ignore[operator]
        up[i], lo[i] = m + k * sd, m - k * sd  # type: ignore[operator]
        bw[i] = (up[i] - lo[i]) / m if m else None  # type: ignore[operator]
        pb[i] = (c[i] - lo[i]) / (up[i] - lo[i]) if up[i] != lo[i] else 0.5  # type: ignore[operator]
    return {"mid": mid, "upper": up, "lower": lo, "width": bw, "pctb": pb}


def mfi(h: list[float], l: list[float], c: list[float], v: list[float], n: int = 14) -> list[Num]:
    tp = [(a + b + d) / 3.0 for a, b, d in zip(h, l, c)]
    out: list[Num] = [None] * len(c)
    for i in range(n, len(c)):
        pos = neg = 0.0
        for j in range(i - n + 1, i + 1):
            mf = tp[j] * v[j]
            if tp[j] > tp[j - 1]:
                pos += mf
            elif tp[j] < tp[j - 1]:
                neg += mf
        out[i] = 50.0 if pos == 0 and neg == 0 else 100.0 if neg == 0 else 100.0 - 100.0 / (1.0 + pos / neg)
    return out


def cmf(h: list[float], l: list[float], c: list[float], v: list[float], n: int = 20) -> list[Num]:
    mfv = [(((cc - ll) - (hh - cc)) / (hh - ll) if hh != ll else 0.0) * vv for hh, ll, cc, vv in zip(h, l, c, v)]
    out: list[Num] = [None] * len(c)
    for i in range(n - 1, len(c)):
        vs = sum(v[i - n + 1:i + 1])
        out[i] = sum(mfv[i - n + 1:i + 1]) / vs if vs else 0.0
    return out


def obv(c: list[float], v: list[float]) -> list[Num]:
    out: list[Num] = [None] * len(c)
    if not c:
        return out
    acc = 0.0
    out[0] = 0.0
    for i in range(1, len(c)):
        acc += v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0.0
        out[i] = acc
    return out


def rel_volume(v: list[float], n: int = 20) -> list[Num]:
    """Объём бара / средний объём n ПРЕДЫДУЩИХ баров (текущий бар не входит в базу)."""
    out: list[Num] = [None] * len(v)
    for i in range(n, len(v)):
        base = sum(v[i - n:i]) / n
        out[i] = v[i] / base if base > 0 else None
    return out


def last(x: list[Num]) -> Num:
    return x[-1] if x else None
