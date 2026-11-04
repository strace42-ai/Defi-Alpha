"""GeckoTerminal public API v2 (бесплатный, без ключа).

Проверено 2026-11-04 (docs/STAGE0_API_FEASIBILITY.md):
- /networks/{net}/new_pools — 20 пулов на страницу, страницы 1..10; ~10 запросов/мин без 429.
- /networks/{net}/tokens/multi/{a,b,..} — до 30 адресов за запрос.
- /networks/{net}/tokens/{addr}/pools, /tokens/{addr}/info, /pools/{pool}/ohlcv/{tf}.
"""
from __future__ import annotations

from typing import Any

from ..http import get_json
from ..util import fnum

BASE = "https://api.geckoterminal.com/api/v2"
SRC = "geckoterminal"
HDR = {"Accept": "application/json;version=20230302"}


def _addr_from_id(rel_id: str | None) -> str | None:
    # "robinhood_0xabc" → "0xabc"; Solana: "solana_<mint>" (регистр сохраняется)
    if not rel_id:
        return None
    return rel_id.split("_", 1)[1] if "_" in rel_id else rel_id


def _parse_pools(payload: dict) -> list[dict]:
    inc = {i["id"]: i for i in payload.get("included", []) or []}
    out = []
    for p in payload.get("data", []) or []:
        a = p.get("attributes", {})
        rel = p.get("relationships", {})
        bid = (rel.get("base_token", {}).get("data") or {}).get("id")
        qid = (rel.get("quote_token", {}).get("data") or {}).get("id")
        dex = (rel.get("dex", {}).get("data") or {}).get("id")

        def tok(tid):
            t = (inc.get(tid) or {}).get("attributes", {})
            return {"address": t.get("address") or _addr_from_id(tid), "symbol": t.get("symbol"),
                    "name": t.get("name"), "decimals": t.get("decimals"),
                    "coingecko_id": t.get("coingecko_coin_id")}

        out.append({
            "pool_address": a.get("address"),
            "name": a.get("name"),
            "dex": dex,
            "pool_created_at": a.get("pool_created_at"),
            "base": tok(bid), "quote": tok(qid),
            "base_price_usd": fnum(a.get("base_token_price_usd")),
            "quote_price_usd": fnum(a.get("quote_token_price_usd")),
            "reserve_usd": fnum(a.get("reserve_in_usd")),
            "fdv_usd": fnum(a.get("fdv_usd")),
            "market_cap_usd": fnum(a.get("market_cap_usd")),
            "volume_h24": fnum((a.get("volume_usd") or {}).get("h24")),
            "tx_h24": (a.get("transactions") or {}).get("h24"),
        })
    return out


def new_pools(network: str, page: int = 1) -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{BASE}/networks/{network}/new_pools",
                         params={"page": page, "include": "base_token,quote_token,dex"}, headers=HDR)
    return _parse_pools(data or {}), raw


def top_pools(network: str, page: int = 1, sort: str = "h24_volume_usd_desc") -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{BASE}/networks/{network}/pools",
                         params={"page": page, "sort": sort, "include": "base_token,quote_token,dex"}, headers=HDR)
    return _parse_pools(data or {}), raw


def token_pools(network: str, address: str, page: int = 1) -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{BASE}/networks/{network}/tokens/{address}/pools",
                         params={"page": page, "include": "base_token,quote_token,dex"}, headers=HDR,
                         allow_404=True)
    return _parse_pools(data or {}), raw


def tokens_multi(network: str, addresses: list[str]) -> tuple[list[dict], int | None]:
    if not addresses:
        return [], None
    data, raw = get_json(SRC, f"{BASE}/networks/{network}/tokens/multi/{','.join(addresses[:30])}",
                         headers=HDR, allow_404=True)
    out = []
    for t in (data or {}).get("data", []) or []:
        a = t.get("attributes", {})
        out.append({
            "address": a.get("address"), "symbol": a.get("symbol"), "name": a.get("name"),
            "decimals": a.get("decimals"), "coingecko_id": a.get("coingecko_coin_id"),
            "total_supply_raw": a.get("total_supply"),
            "normalized_total_supply": fnum(a.get("normalized_total_supply")),
            "price_usd": fnum(a.get("price_usd")),
            "fdv_usd": fnum(a.get("fdv_usd")),
            "market_cap_usd": fnum(a.get("market_cap_usd")),
            "total_reserve_usd": fnum(a.get("total_reserve_in_usd")),
            "volume_h24": fnum((a.get("volume_usd") or {}).get("h24")),
        })
    return out, raw


def token_info(network: str, address: str) -> tuple[dict | None, int | None]:
    data, raw = get_json(SRC, f"{BASE}/networks/{network}/tokens/{address}/info", headers=HDR, allow_404=True)
    if not data:
        return None, raw
    a = data.get("data", {}).get("attributes", {})
    return {
        "websites": a.get("websites") or [],
        "twitter": a.get("twitter_handle"), "telegram": a.get("telegram_handle"),
        "discord": a.get("discord_url"),
        "description": a.get("description"),  # недоверенный текст — только отображение
        "gt_score": fnum(a.get("gt_score")), "gt_verified": a.get("gt_verified"),
        "categories": a.get("categories") or [],
        "holders": a.get("holders"),
        "mint_authority": a.get("mint_authority"), "freeze_authority": a.get("freeze_authority"),
        "is_honeypot": a.get("is_honeypot"),
        "coingecko_id": a.get("coingecko_coin_id"),
    }, raw


def ohlcv(network: str, pool: str, token_address: str, timeframe: str = "day",
          aggregate: int = 1, limit: int = 1000) -> tuple[list[list[float]], dict, int | None]:
    """Свечи [ts, open, high, low, close, volume] в USD для ЦЕНЫ УКАЗАННОГО ТОКЕНА (параметр token)."""
    data, raw = get_json(SRC, f"{BASE}/networks/{network}/pools/{pool}/ohlcv/{timeframe}",
                         params={"aggregate": aggregate, "limit": limit, "currency": "usd",
                                 "token": token_address}, headers=HDR, allow_404=True)
    if not data:
        return [], {}, raw
    lst = (data.get("data", {}).get("attributes", {}) or {}).get("ohlcv_list") or []
    return sorted(lst, key=lambda r: r[0]), data.get("meta") or {}, raw


def networks_page(page: int = 1) -> tuple[list[dict], Any]:
    data, raw = get_json(SRC, f"{BASE}/networks", params={"page": page}, headers=HDR)
    return [{"id": n["id"], "name": n["attributes"]["name"]} for n in (data or {}).get("data", [])], (data or {}).get("links")
