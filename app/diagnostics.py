"""Диагностика окружения: версия Python, пакеты, запись в папку данных, доступность API."""
from __future__ import annotations

import importlib
import sys

from . import config


def run(network: bool = False) -> dict:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("Python ≥ 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("httpx", "httpx"), ("openpyxl", "openpyxl")):
        try:
            m = importlib.import_module(mod)
            add(f"Пакет {pkg}", True, getattr(m, "__version__", ""))
        except ImportError:
            add(f"Пакет {pkg}", False, "не установлен — запустите start.bat повторно")
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        t = config.DATA_DIR / ".write_test"
        t.write_text("ok")
        t.unlink()
        add("Запись в папку данных", True, str(config.DATA_DIR))
    except OSError as e:
        add("Запись в папку данных", False, str(e))
    if network:
        import httpx
        targets = [("GeckoTerminal", "https://api.geckoterminal.com/api/v2/networks?page=1"),
                   ("DEX Screener", "https://api.dexscreener.com/token-profiles/latest/v1"),
                   ("DefiLlama", "https://api.llama.fi/v2/chains"),
                   ("Robinhood RPC", None)]
        for name, url in targets:
            try:
                if url:
                    r = httpx.get(url, timeout=15, headers={"User-Agent": "DeFiAlphaScanner/0.1"})
                else:
                    r = httpx.post("https://rpc.mainnet.chain.robinhood.com", timeout=15,
                                   json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                                   headers={"User-Agent": "DeFiAlphaScanner/0.1"})
                add(f"Сеть: {name}", r.status_code == 200, f"HTTP {r.status_code}")
            except Exception as e:
                add(f"Сеть: {name}", False, f"{type(e).__name__}: {e}")
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


if __name__ == "__main__":
    res = run(network=True)
    for c in res["checks"]:
        print(("[OK]  " if c["ok"] else "[ОШИБКА] ") + c["name"] + (f" — {c['detail']}" if c["detail"] else ""))
    sys.exit(0 if res["ok"] else 1)
