"""Trading Lab, этап B: свечи, индикаторы, структура, дивергенции, отсутствие look-ahead."""
import datetime as dt
import json
import math
from pathlib import Path

import pytest

from app.tradinglab import analysis, candles, divergence, indicators as ind, structure
from app.tradinglab.candles import Candle, Series

FIX = Path(__file__).resolve().parent / "fixtures" / "trading_lab"
NOW = int(dt.datetime(2026, 11, 5, 13, 27, tzinfo=dt.UTC).timestamp())  # момент записи фикстур


def real(tf):
    name = {"1h": "h1", "4h": "h4", "1d": "d1"}[tf]
    return candles.from_gt_payload(json.loads((FIX / f"gt_ohlcv_{name}.json").read_text()), tf, NOW)


def approx(a, b, eps=1e-9):
    return a is not None and abs(a - b) <= eps


# ------------------------------------------------------------------ свечи
def test_real_fixture_drops_unclosed_and_has_no_gaps():
    s = real("1h")
    assert s.dropped_unclosed == 1                      # свеча 13:00 ещё идёт
    assert s.candles[-1].ts + 3600 + candles.GRACE_SEC <= NOW
    assert s.gaps == [] and s.invalid == [] and len(s) == 376
    assert len(real("4h")) == 94 and len(real("1d")) == 16


def test_normalize_dedupe_conflict_gap_invalid():
    H = 3600
    rows = [[0, 1, 2, 0.5, 1.5, 10], [H, 1.5, 2, 1, 1.2, 5], [H, 9, 9, 9, 9, 9],   # дубликат с конфликтом
            [3 * H, 1, 1.1, 0.9, 1, 1],                                           # пропуск 2H
            [4 * H, 1, 0.5, 0.9, 1, 1],                                           # high < open → invalid
            [5 * H, 1, 1, 1, 1, 1]]                                               # не закрыта к now
    s = candles.normalize(rows, "1h", now=6 * H)
    assert [k.ts for k in s.candles] == [0, H, 3 * H]
    assert s.candles[1].c == 1.2                       # первая запись не перезаписывается
    assert s.duplicates == 1 and len(s.conflicts) == 1
    assert s.gaps == [{"after_ts": H, "missing": 1}]
    assert len(s.invalid) == 1 and s.dropped_unclosed == 1
    assert s.quality()["ok"] is False


def test_upto_excludes_unclosed_htf():
    s = real("4h")
    t = s.candles[10].ts + 3 * 3600                    # середина 11-й 4H-свечи
    assert s.upto(t).candles[-1].ts == s.candles[9].ts


# ------------------------------------------------------------------ индикаторы (ручной расчёт)
def test_sma_ema():
    assert ind.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert ind.ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_rsi_hand_and_edges():
    r = ind.rsi([1, 2, 1, 2, 3], 2)
    assert r[:2] == [None, None] and approx(r[2], 50.0) and approx(r[3], 75.0) and approx(r[4], 87.5)
    assert ind.rsi(list(range(1, 30)), 14)[-1] == 100.0
    assert ind.rsi([5.0] * 20, 14)[-1] == 50.0
    assert ind.rsi([1.0] * 14, 14) == [None] * 14       # 14 баров < 15 → недоступно


def test_macd_warmup_and_constant():
    m = ind.macd([2.0] * 40)
    assert m["macd"][24] is None and m["macd"][25] == 0.0
    assert m["hist"][32] is None and m["hist"][33] == 0.0
    assert ind.MIN_BARS("macd") == 34
    assert all(x is None for x in ind.macd([1.0] * 33)["hist"])


def test_macd_matches_independent_ema_difference():
    c = [1 + 0.1 * math.sin(i / 3) + i * 0.01 for i in range(60)]
    m = ind.macd(c)
    e12, e26 = ind.ema(c, 12), ind.ema(c, 26)
    assert approx(m["macd"][59], e12[59] - e26[59])
    sig = ind.ema([x for x in m["macd"] if x is not None], 9)
    assert approx(m["signal"][59], sig[-1]) and approx(m["hist"][59], m["macd"][59] - sig[-1])


def test_atr_hand():
    h, l, c = [2, 3, 4, 6], [1, 1, 2, 3], [1.5, 2, 3, 4]
    a = ind.atr(h, l, c, 2)
    assert a[:2] == [None, None] and approx(a[2], 2.0) and approx(a[3], 2.5)


def test_adx_properties():
    n = 14
    up = [1 + i * 0.01 for i in range(40)]
    d = ind.adx([x + 0.005 for x in up], [x - 0.005 for x in up], up, n)
    assert d["adx"][2 * n - 2] is None and d["adx"][2 * n - 1] is not None
    assert d["pdi"][-1] > d["ndi"][-1] and d["adx"][-1] > 50


def test_bollinger_hand():
    b = ind.bollinger([1, 2, 3], 3, 2)
    sd = math.sqrt(2 / 3)
    assert approx(b["mid"][2], 2) and approx(b["upper"][2], 2 + 2 * sd) and approx(b["lower"][2], 2 - 2 * sd)
    assert approx(b["width"][2], 4 * sd / 2)
    assert ind.bollinger([1.0] * 25)["width"][-1] == 0.0


def test_mfi_hand():
    h, l, c, v = [10, 11, 10.5], [10, 11, 10.5], [10, 11, 10.5], [50, 100, 200]
    assert approx(ind.mfi(h, l, c, v, 2)[2], 100 * 1100 / 3200)


def test_cmf_hand():
    assert approx(ind.cmf([10, 10], [0, 0], [10, 0], [100, 300], 2)[1], -0.5)
    assert ind.cmf([1, 1], [1, 1], [1, 1], [5, 5], 2)[1] == 0.0          # h == l → множитель 0


def test_obv_and_relvol():
    assert ind.obv([1, 2, 2, 1], [10, 20, 30, 40]) == [0.0, 20.0, 20.0, -20.0]
    rv = ind.rel_volume([1, 1, 1, 3], 3)
    assert rv[:3] == [None, None, None] and rv[3] == 3.0


# ------------------------------------------------------------------ структура и дивергенции
def zigzag(points, step=4):
    """Ряд свечей, линейно соединяющий точки (для детерминированных pivots)."""
    closes = []
    for a, b in zip(points, points[1:]):
        closes += [a + (b - a) * k / step for k in range(step)]
    closes.append(points[-1])
    ks = [Candle(i * 3600, c, c * 1.001, c * 0.999, c, 100.0) for i, c in enumerate(closes)]
    return Series("1h", ks)


def test_pivots_confirmed_after_right_bars():
    s = zigzag([10, 12, 9, 11])
    pv = structure.pivots(s, 2, 2)
    hi = [p for p in pv if p.kind == "H"][0]
    assert hi.idx == 4 and hi.confirmed_idx == 6
    assert structure.known_at(pv, 5) == [] and hi in structure.known_at(pv, 6)


def test_choch_after_downtrend():
    s = zigzag([12, 10, 11, 9, 10, 8, 10.5, 9.5, 12])
    ev = structure.structure_events(s, structure.pivots(s, 2, 2))
    types = [e["type"] for e in ev]
    assert "BOS_DOWN" in types and "CHoCH_UP" in types
    assert types.index("CHoCH_UP") > types.index("BOS_DOWN")
    for e in ev:  # пробитый уровень известен до бара пробоя
        assert e["idx"] > e["pivot_idx"]


def test_regular_bullish_divergence_numbers():
    s = zigzag([10, 8, 10, 7.5, 10])
    pv = structure.pivots(s, 2, 2)
    lows = [p for p in pv if p.kind == "L"]
    fake = [None] * len(s)
    fake[lows[0].idx], fake[lows[1].idx] = 25.0, 35.0      # индикатор выше на более низком минимуме
    d = divergence.find(pv, fake, "RSI")
    assert len(d) == 1 and d[0]["type"] == "regular_bullish"
    assert d[0]["p1"]["price"] == lows[0].price and d[0]["p2"]["ind"] == 35.0
    assert d[0]["confirmed_idx"] == lows[1].idx + 2 and d[0]["price_change_pct"] < 0
    fake[lows[1].idx] = 26.0                               # разница < порога шума RSI (2 п.) → нет
    assert divergence.find(pv, fake, "RSI") == []


def test_log_fib():
    f = structure.log_fib(0.05, 0.2)
    assert approx(f["retracement"]["0.5"], math.sqrt(0.05 * 0.2))
    assert approx(f["extension"]["1.0"], 0.2)


# ------------------------------------------------------------------ warmup и look-ahead
def test_daily_warmup_is_explicit():
    a = analysis.analyze(real("1d"))
    snap = a.snapshot()
    assert snap["bars"] == 16
    assert snap["indicators"]["macd_hist"]["status"] == analysis.INSUFFICIENT
    assert snap["indicators"]["adx"]["status"] == analysis.INSUFFICIENT
    assert snap["indicators"]["rsi"]["status"] == "OK"
    assert snap["regime"]["regime"] == analysis.INSUFFICIENT


@pytest.mark.parametrize("tf", ["1h", "4h"])
def test_no_lookahead_truncated_equals_full(tf):
    """Добавление будущих свечей не меняет ни одного значения, pivot, события или дивергенции в прошлом."""
    s = real(tf)
    full = analysis.analyze(s)
    for i in (40, 60, len(s) // 2, len(s) - 5):
        part = analysis.analyze(Series(tf, s.candles[:i + 1]))
        a, b = full.snapshot(i), part.snapshot(i)
        for k in a["indicators"]:
            va, vb = a["indicators"][k]["value"], b["indicators"][k]["value"]
            assert (va is None and vb is None) or abs(va - vb) <= 1e-12 * max(1, abs(va)), (tf, i, k)
        assert a["pivots"] == b["pivots"]
        assert a["structure_events"] == b["structure_events"]
        assert a["divergences"] == b["divergences"]
        assert a["regime"]["regime"] == b["regime"]["regime"]
