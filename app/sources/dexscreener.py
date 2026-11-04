"""DEX Screener API (бесплатный, без ключа).

Используется для проверки контрактов, пар, цен, ликвидности, сайтов/соцсетей.
ВАЖНО:
- /token-pairs/v1/{chain}/{token} возвращает пары, где токен может быть и base, и QUOTE.
  priceUsd пары — это цена BASE-токена. Для пар, где наш токен quote, цену брать нельзя.
- marketCap DEX Screener часто равен fdv (когда circulating неизвестен) — это не доказательство MCAP.
- token-profiles / token-boosts — промотируемые токены, а не полный перечень новых.
"""
from __future__ import annotations

from ..http import get_json
from ..util import fnum, from_ts, iso

BASE = "https://api.dexscreener.com"
SRC = "dexscreener"


def _parse_pair(p: dict) -> dict:
    return {
        "chain": p.get("chainId"), "dex": p.get("dexId"), "url": p.get("url"),
        "pair_address": p.get("pairAddress"), "labels": p.get("labels") or [],
        "base": p.get("baseToken") or {}, "quote": p.get("quoteToken") or {},
        "price_usd": fnum(p.get("priceUsd")),  # цена BASE-токена
        "liquidity_usd": fnum((p.get("liquidity") or {}).get("usd")),
        "volume_h24": fnum((p.get("volume") or {}).get("h24")),
        "fdv": fnum(p.get("fdv")), "market_cap": fnum(p.get("marketCap")),
        "pair_created_at": iso(from_ts(p.get("pairCreatedAt"), ms=True)),
        "websites": [w.get("url") for w in ((p.get("info") or {}).get("websites") or []) if w.get("url")],
        "socials": [{"type": s.get("type"), "url": s.get("url")} for s in ((p.get("info") or {}).get("socials") or [])],
        "txns_h24": (p.get("txns") or {}).get("h24"),
    }


def token_pairs(chain: str, address: str) -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{BASE}/token-pairs/v1/{chain}/{address}", allow_404=True)
    return [_parse_pair(p) for p in (data or []) if isinstance(p, dict)], raw


def tokens(chain: str, addresses: list[str]) -> tuple[list[dict], int | None]:
    """До 30 адресов. Возвращает по одной (наиболее ликвидной) паре на токен."""
    if not addresses:
        return [], None
    data, raw = get_json(SRC, f"{BASE}/tokens/v1/{chain}/{','.join(addresses[:30])}", allow_404=True)
    return [_parse_pair(p) for p in (data or []) if isinstance(p, dict)], raw


def search(query: str) -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{BASE}/latest/dex/search", params={"q": query})
    return [_parse_pair(p) for p in (data or {}).get("pairs", []) or []], raw


def latest_profiles() -> tuple[list[dict], int | None]:
    """Промо-источник: последние профили токенов (платное размещение). НЕ репрезентативен."""
    data, raw = get_json(SRC, f"{BASE}/token-profiles/latest/v1")
    return [{"chain": d.get("chainId"), "address": d.get("tokenAddress"), "url": d.get("url")}
            for d in (data or []) if isinstance(d, dict)], raw
