"""EVM JSON-RPC (только чтение): totalSupply, события Transfer, байткод, слоты прокси.

Проверено на Robinhood Chain (официальный RPC) 2026-11-04:
- eth_getLogs до 10 млн блоков за запрос; при частых запросах — 429 (соблюдаем паузы);
- архивного состояния нет («historical state … is not available») → исторический totalSupply
  через eth_call на старом блоке недоступен; история восстанавливается из событий.
"""
from __future__ import annotations

from typing import Any

from ..chains import Chain
from ..http import SourceError, request_json

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC = "0x" + "0" * 64
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"

# 4-байтовые селекторы функций — индикаторы (не доказательство) прав в контракте
SELECTORS = {
    "mint(address,uint256)": "40c10f19",
    "mint(uint256)": "a0712d68",
    "burn(uint256)": "42966c68",
    "owner()": "8da5cb5b",
    "transferOwnership(address)": "f2fde38b",
    "renounceOwnership()": "715018a6",
    "pause()": "8456cb59",
    "unpause()": "3f4ba83a",
    "blacklist(address)": "f9f92be4",
    "addBlackList(address)": "0ecb93c0",
    "setBlacklist(address,bool)": "153b0d1e",
    "upgradeTo(address)": "3659cfe6",
    "upgradeToAndCall(address,bytes)": "4f1ef286",
    "grantRole(bytes32,address)": "2f2ff15d",
    "minter()": "07546172",
    "setMinter(address)": "fca3b5aa",
    "setFee/setTax(uint256)": "69fe0e2d",
}


class Rpc:
    def __init__(self, chain: Chain):
        if not chain.rpc:
            raise SourceError(f"rpc:{chain.key}", "unavailable", "RPC не задан")
        self.chain = chain
        self.url = chain.rpc[0]
        self.source = f"rpc:{chain.key}"

    def call(self, method: str, params: list[Any]) -> Any:
        data, _ = request_json(self.source, "POST", self.url,
                               json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                               save_raw=False, retries=3)
        if not isinstance(data, dict):
            raise SourceError(self.source, "bad_response", "некорректный JSON-RPC ответ")
        if "error" in data:
            err = data["error"] or {}
            raise SourceError(self.source, "rpc_error", f"{err.get('code')}: {err.get('message')}")
        return data.get("result")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_timestamp(self, block: int) -> int | None:
        b = self.call("eth_getBlockByNumber", [hex(block), False])
        return int(b["timestamp"], 16) if b else None

    def eth_call(self, to: str, data: str, block: str = "latest") -> str | None:
        try:
            return self.call("eth_call", [{"to": to, "data": data}, block])
        except SourceError as e:
            if e.kind == "rpc_error" and ("revert" in e.message.lower() or "execution" in e.message.lower()):
                return None
            raise

    def total_supply_raw(self, token: str) -> int | None:
        r = self.eth_call(token, "0x18160ddd")
        return int(r, 16) if r and r != "0x" else None

    def decimals(self, token: str) -> int | None:
        r = self.eth_call(token, "0x313ce567")
        return int(r, 16) if r and r != "0x" else None

    def address_call(self, token: str, selector: str) -> str | None:
        r = self.eth_call(token, "0x" + selector)
        if not r or r == "0x" or len(r) < 66:
            return None
        return "0x" + r[-40:]

    def code(self, address: str) -> str:
        return self.call("eth_getCode", [address, "latest"]) or "0x"

    def storage(self, address: str, slot: str) -> str | None:
        return self.call("eth_getStorageAt", [address, slot, "latest"])

    def transfer_logs(self, token: str, from_block: int, to_block: int, *, mint: bool | None) -> list[dict]:
        """mint=True: from=0x0; mint=False: to=0x0 (burn); None: все Transfer."""
        topics: list[Any] = [TRANSFER_TOPIC]
        if mint is True:
            topics.append(ZERO_TOPIC)
        elif mint is False:
            topics += [None, ZERO_TOPIC]
        return self.call("eth_getLogs", [{"address": token, "fromBlock": hex(from_block),
                                          "toBlock": hex(to_block), "topics": topics}]) or []


def bytecode_flags(code: str) -> dict[str, bool]:
    """Наличие селектора в байткоде (PUSH4 <selector>). Эвристика: в прокси функции живут в implementation."""
    c = (code or "").lower()
    return {name: ("63" + sel) in c for name, sel in SELECTORS.items()}


def is_minimal_proxy(code: str) -> str | None:
    c = (code or "").lower()
    if c.startswith("0x363d3d373d3d3d363d73") and len(c) >= 2 + 2 * 45:
        return "0x" + c[22:62]
    return None
