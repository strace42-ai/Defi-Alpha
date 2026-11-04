"""Solana JSON-RPC — отдельный адаптер (SPL-токены устроены иначе, чем ERC-20).

Поддерживается: getTokenSupply (текущее предложение), getAccountInfo(jsonParsed) —
mintAuthority / freezeAuthority, программа токена (Token / Token-2022).
История mint-событий для Solana в этой версии НЕ реализована (нужен индексатор).
"""
from __future__ import annotations

from ..chains import Chain
from ..http import SourceError, request_json


class SolanaRpc:
    def __init__(self, chain: Chain):
        self.chain = chain
        self.url = chain.rpc[0]
        self.source = f"rpc:{chain.key}"

    def call(self, method: str, params: list) -> dict:
        data, _ = request_json(self.source, "POST", self.url,
                               json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                               save_raw=False, retries=3)
        if "error" in (data or {}):
            raise SourceError(self.source, "rpc_error", str(data["error"]))
        return (data or {}).get("result") or {}

    def token_supply(self, mint: str) -> dict | None:
        r = self.call("getTokenSupply", [mint])
        v = r.get("value")
        if not v:
            return None
        return {"amount_raw": v.get("amount"), "decimals": v.get("decimals"), "ui": float(v.get("uiAmountString") or 0)}

    def mint_info(self, mint: str) -> dict | None:
        r = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        v = r.get("value")
        if not v:
            return None
        info = ((v.get("data") or {}).get("parsed") or {}).get("info") or {}
        return {"program": v.get("owner"), "mint_authority": info.get("mintAuthority"),
                "freeze_authority": info.get("freezeAuthority"), "decimals": info.get("decimals"),
                "supply_raw": info.get("supply")}
