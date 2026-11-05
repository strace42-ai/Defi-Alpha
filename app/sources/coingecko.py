"""CoinGecko public API v3 (без ключа; ~5 запросов/мин, проверено 2026-11-05).

/coins/{id}: market_data.circulating_supply / total_supply / max_supply / market_cap и platforms
(сеть → адрес контракта). Монета принимается для токена ТОЛЬКО если platforms содержит этот адрес —
идентичность по сети + адресу, а не по тикеру.
"""
from __future__ import annotations

from ..config import get_secret
from ..http import SourceError, get_json
from ..util import fnum

SRC = "coingecko"
BASE = "https://api.coingecko.com/api/v3"


def coin(cg_id: str) -> tuple[dict | None, int | None]:
    key = get_secret("coingecko_demo_key")
    headers = {"x-cg-demo-api-key": key} if key else None
    try:
        data, raw = get_json(SRC, f"{BASE}/coins/{cg_id}", headers=headers, retries=1, allow_404=True,
                             params={"localization": "false", "tickers": "false", "market_data": "true",
                                     "community_data": "false", "developer_data": "false", "sparkline": "false"})
    except SourceError:
        raise
    if not data or not isinstance(data, dict):
        return None, raw
    m = data.get("market_data") or {}
    usd = lambda k: fnum((m.get(k) or {}).get("usd")) if isinstance(m.get(k), dict) else fnum(m.get(k))
    plats = {k: (v or "") for k, v in (data.get("platforms") or {}).items()}
    detail = {k: (v or {}).get("contract_address") for k, v in (data.get("detail_platforms") or {}).items()}
    return {
        "id": data.get("id"), "symbol": data.get("symbol"), "name": data.get("name"),
        "platforms": {**detail, **plats},
        "price_usd": usd("current_price"), "market_cap_usd": usd("market_cap"), "fdv_usd": usd("fully_diluted_valuation"),
        "circulating_supply": fnum(m.get("circulating_supply")), "total_supply": fnum(m.get("total_supply")),
        "max_supply": fnum(m.get("max_supply")), "last_updated": m.get("last_updated"),
        "url": f"https://www.coingecko.com/en/coins/{data.get('id')}",
    }, raw


def contract_matches(info: dict, chain_keys: set[str], address: str) -> bool:
    a = address.lower() if address.startswith("0x") else address
    for plat, addr in (info.get("platforms") or {}).items():
        if not addr:
            continue
        b = addr.lower() if addr.startswith("0x") else addr
        if b == a and (plat in chain_keys or not chain_keys):
            return True
    return False
