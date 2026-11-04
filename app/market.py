"""Чистые функции рыночного анализа (без сети и БД) — покрываются модульными тестами.

- Качество пулов: только пулы, где известна цена ИМЕННО нашего токена, с достаточной ликвидностью,
  доверенным контр-активом и ценой без аномального отклонения от медианы.
- ATH: по дневным свечам качественных пулов; аномальные хвосты (high > k × max(open, close))
  не используются как ATH, но показываются.
"""
from __future__ import annotations

from typing import Any

from .util import from_ts, iso, median


def pool_view_from_gt(p: dict, token_addr: str, trusted_quotes: set[str]) -> dict:
    base = (p.get("base") or {}).get("address") or ""
    quote = (p.get("quote") or {}).get("address") or ""
    tok = token_addr
    same = (lambda a: a.lower() == tok.lower()) if tok.startswith("0x") else (lambda a: a == tok)
    if same(base):
        side, price, counter = "base", p.get("base_price_usd"), p.get("quote") or {}
    elif same(quote):
        side, price, counter = "quote", p.get("quote_price_usd"), p.get("base") or {}
    else:
        side, price, counter = None, None, {}
    caddr = counter.get("address") or ""
    caddr_n = caddr.lower() if caddr.startswith("0x") else caddr
    return {
        "address": p.get("pool_address"), "dex": p.get("dex"), "name": p.get("name"),
        "side": side, "price_usd": price, "liquidity_usd": p.get("reserve_usd"),
        "volume_h24": p.get("volume_h24"), "created_at": p.get("pool_created_at"),
        "counter_symbol": counter.get("symbol"), "counter_address": caddr_n,
        "counter_trusted": caddr_n in trusted_quotes or bool(counter.get("coingecko_id")),
        "source": "geckoterminal",
    }


def pool_view_from_ds(p: dict, token_addr: str, trusted_quotes: set[str]) -> dict:
    base = (p.get("base") or {}).get("address") or ""
    quote = (p.get("quote") or {}).get("address") or ""
    tok = token_addr
    same = (lambda a: a.lower() == tok.lower()) if tok.startswith("0x") else (lambda a: a == tok)
    if same(base):
        side, price, counter = "base", p.get("price_usd"), p.get("quote") or {}
    elif same(quote):
        # priceUsd у DEX Screener — цена BASE-токена; для нашего токена её использовать НЕЛЬЗЯ
        side, price, counter = "quote", None, p.get("base") or {}
    else:
        side, price, counter = None, None, {}
    caddr = counter.get("address") or ""
    caddr_n = caddr.lower() if caddr.startswith("0x") else caddr
    return {
        "address": p.get("pair_address"), "dex": p.get("dex"), "name": f"{base_sym(p)} / {quote_sym(p)}",
        "side": side, "price_usd": price, "liquidity_usd": p.get("liquidity_usd"),
        "volume_h24": p.get("volume_h24"), "created_at": p.get("pair_created_at"),
        "counter_symbol": counter.get("symbol"), "counter_address": caddr_n,
        "counter_trusted": caddr_n in trusted_quotes, "source": "dexscreener",
        "ds_fdv": p.get("fdv"), "ds_market_cap": p.get("market_cap"),
    }


def base_sym(p: dict) -> str:
    return (p.get("base") or {}).get("symbol") or "?"


def quote_sym(p: dict) -> str:
    return (p.get("quote") or {}).get("symbol") or "?"


def assess_pools(pools: list[dict], min_liq: float, outlier_pct: float) -> tuple[list[dict], float | None]:
    """Пометить качество каждого пула. Возвращает (пулы с полями quality/excluded_reason, медианная цена)."""
    for p in pools:
        reasons = []
        if p.get("side") is None:
            reasons.append("токен не участвует в паре")
        if p.get("price_usd") is None:
            reasons.append("цена нашего токена в этом пуле неизвестна" +
                           (" (токен — quote, priceUsd относится к другому токену)" if p.get("side") == "quote" and p.get("source") == "dexscreener" else ""))
        liq = p.get("liquidity_usd")
        if liq is None or liq < min_liq:
            reasons.append(f"ликвидность {liq if liq is not None else 'н/д'} < {min_liq:.0f} USD")
        if not p.get("counter_trusted"):
            reasons.append(f"контр-актив {p.get('counter_symbol')} не в списке доверенных")
        p["excluded_reasons"] = reasons
    candidates = [p for p in pools if not p["excluded_reasons"]]
    med = median([p["price_usd"] for p in candidates])
    if med:
        for p in candidates:
            dev = abs(p["price_usd"] / med - 1) * 100
            if dev > outlier_pct:
                p["excluded_reasons"].append(f"цена отклоняется от медианы на {dev:.0f}% (> {outlier_pct:.0f}%)")
    for p in pools:
        p["quality"] = not p["excluded_reasons"]
    good = [p for p in pools if p["quality"]]
    return pools, median([p["price_usd"] for p in good])


def dedupe_pools(pools: list[dict]) -> list[dict]:
    """Объединить один и тот же пул из разных источников (GT приоритетнее — у него цена для quote-стороны)."""
    out: dict[str, dict] = {}
    for p in pools:
        a = p.get("address") or ""
        k = a.lower() if a.startswith("0x") else a
        if k in out:
            prev = out[k]
            prev.setdefault("also_in", []).append(p["source"])
            if prev.get("price_usd") is None and p.get("price_usd") is not None:
                prev["price_usd"] = p["price_usd"]
            if prev.get("created_at") is None or (p.get("created_at") and p["created_at"] < prev["created_at"]):
                prev["created_at"] = p.get("created_at") or prev.get("created_at")
            for f in ("ds_fdv", "ds_market_cap"):
                if p.get(f) is not None:
                    prev[f] = p[f]
        else:
            out[k] = dict(p)
    return list(out.values())


def compute_ath(candles_by_pool: dict[str, list[list[float]]], wick_factor: float) -> dict[str, Any]:
    """candles: [ts, open, high, low, close, volume]. Возвращает ATH и детали без «молчаливых» допущений."""
    best = {"ath": None, "ath_at": None, "ath_pool": None}
    raw = {"high": None, "at": None, "pool": None}
    close = {"close": None, "at": None}
    anomalies = []
    first_ts = None
    first_trade_ts = None
    for pool, cs in candles_by_pool.items():
        for c in cs:
            if len(c) < 6:
                continue
            ts, o, h, l, cl, vol = c[:6]
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if vol and vol > 0 and (first_trade_ts is None or ts < first_trade_ts):
                first_trade_ts = ts
            body_top = max(o, cl)
            if raw["high"] is None or h > raw["high"]:
                raw = {"high": h, "at": ts, "pool": pool}
            if close["close"] is None or cl > close["close"]:
                close = {"close": cl, "at": ts}
            robust = h
            if body_top > 0 and h > wick_factor * body_top:
                anomalies.append({"pool": pool, "at": iso(from_ts(ts)), "high": h, "body_top": body_top,
                                  "ratio": round(h / body_top, 2)})
                robust = body_top
            if best["ath"] is None or robust > best["ath"]:
                best = {"ath": robust, "ath_at": ts, "ath_pool": pool}
    return {
        "ath_usd": best["ath"], "ath_at": iso(from_ts(best["ath_at"])), "ath_pool": best["ath_pool"],
        "ath_raw_high_usd": raw["high"], "ath_raw_high_at": iso(from_ts(raw["at"])), "ath_raw_pool": raw["pool"],
        "ath_close_usd": close["close"], "ath_close_at": iso(from_ts(close["at"])),
        "wick_anomalies": anomalies[:20], "first_candle_at": iso(from_ts(first_ts)),
        "first_trade_at": iso(from_ts(first_trade_ts)), "method": f"дневные свечи; хвост > {wick_factor}× тела отбрасывается",
        "pools_used": list(candles_by_pool.keys()),
    }


def drawdown_pct(price: float | None, ath: float | None) -> float | None:
    if price is None or ath is None or ath <= 0:
        return None
    return max(0.0, (1 - price / ath) * 100.0)
