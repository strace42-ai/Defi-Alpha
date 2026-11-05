"""Настройки пользователя и хранение секретов.

- Настройки: data/settings.json (создаётся из значений по умолчанию; правятся через интерфейс).
- Секреты (API-ключи): data/secrets.bin. На Windows шифруются DPAPI (привязка к учётной
  записи пользователя), на других ОС — файл с правами 0600. Секреты никогда не попадают
  в экспорт, логи и ответы API (возвращается только признак «задан/не задан»).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DAS_DATA_DIR", ROOT / "data"))
SETTINGS_PATH = DATA_DIR / "settings.json"
SECRETS_PATH = DATA_DIR / "secrets.bin"

DEFAULTS: dict[str, Any] = {
    "filters": {
        "max_age_days": 90,
        "max_circ_mcap_usd": 5_000_000,
        "max_drawdown_pct": 86.0,
        "ath_wick_factor": 2.0,          # хвост свечи > N× max(open, close) считается аномалией
        "min_pool_liquidity_usd": 5_000,  # пул ниже — не используется для цены/ATH
        "price_outlier_pct": 30.0,       # пул с ценой, отклоняющейся от медианы > N%, исключается
        "mcap_conflict_pct": 15.0,       # расхождение источников MCAP > N% → событие конфликта
        "vc_fail_categories": ["vc_fund"],
        "vc_warn_categories": ["angel", "strategic", "ecosystem_fund"],
        "chain_min_tvl_usd": 20_000_000,
        "chain_max_tvl_decline_90d_pct": -60.0,
        "chain_min_dex_volume_30d_usd": 10_000_000,
        "chain_max_stable_decline_90d_pct": -50.0,
    },
    "market_minimum": {  # уровень B: минимальные рыночные параметры
        "min_liquidity_usd": 10_000,
        "min_volume_24h_usd": 1_000,
    },
    "scoring": {"watchlist_min_preliminary": 55},
    "intervals_min": {
        "discovery": 20,
        "candidates_refresh": 240,
        "fundamentals": 1440,
        "weekly_snapshot": 10080,
        "backup": 1440,
    },
    "settings_version": 2,
    "scheduler_enabled": True,   # v2: фоновый мониторинг включён по умолчанию (можно поставить на паузу в интерфейсе)
    "refresh_sec": {             # частота обновления по уровню приоритета (0 = автоматически не обновлять)
        "quote": {"0": 60, "1": 120, "2": 300, "3": 1800, "4": 21600, "5": 0},
        "deep": {"0": 900, "1": 1800, "2": 7200, "3": 43200, "4": 0, "5": 0},
        "onchain": {"0": 300, "1": 900, "2": 3600, "3": 0, "4": 0, "5": 0},
        "fundamentals": {"0": 3600, "1": 10800, "2": 21600, "3": 86400, "4": 0, "5": 0},
        "coingecko": {"0": 3600, "1": 10800, "2": 21600, "3": 86400, "4": 0, "5": 0},
    },
    "top_candidates_n": 30,
    "queue_max_per_lane": 300,
    "task_max_seconds": 90,
    "reorg_overlap_blocks": 2000,
    "focus_ttl_sec": 90,
    "chains_enabled": None,      # None → default_enabled из реестра сетей
    "discovery": {
        "max_pages_per_chain": 4,
        "historical_top_pools_pages": 2,
        "defillama_backfill": True,
        "dexscreener_profiles": False,  # промо-источник, нерепрезентативен; выключен по умолчанию
    },
    "rate_limits_per_min": {
        "geckoterminal": 9,    # измерено: ~10/мин без 429 при равномерных запросах
        "dexscreener": 240,    # документировано 300/мин для pairs/tokens
        "defillama": 60,
        "rpc": 60,
        "project": 20,
        "coingecko": 4,      # публичный API без ключа: ~5/мин, при 429 — пауза
        "llama_coins": 30,
    },
    "alerts": {
        "tvl_outperform_pp": 15.0,
        "fees_growth_pct": 30.0,
        "tvl_flat_pct": 5.0,
        "liquidity_drop_pct": 30.0,
        "emission_spike_x": 2.0,
        "streak_periods": 3,
        "stale_hours": 30,
        "unlock_lookahead_days": 14,
    },
    "backup_keep": 14,
    "raw_keep_days": 30,
    "ui_port": 8765,
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(force: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return copy.deepcopy(_cache)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        user: dict = {}
        if SETTINGS_PATH.exists():
            try:
                user = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                user = {}
        if user and int(user.get("settings_version", 1)) < 2:
            # v1 → v2: включить фоновый мониторинг один раз (дальше пользователь управляет сам); остальное сохраняется
            user["settings_version"] = 2
            user["scheduler_enabled"] = True
            user.setdefault("_migrations", []).append("v2: scheduler_enabled=true; No-VC больше не фильтр")
            try:
                tmp = SETTINGS_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(_merge(DEFAULTS, user), ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, SETTINGS_PATH)
            except OSError:
                pass
        _cache = _merge(DEFAULTS, user)
        return copy.deepcopy(_cache)


def save_settings(new: dict[str, Any]) -> dict[str, Any]:
    global _cache
    merged = _merge(DEFAULTS, new)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)
        _cache = merged
    return copy.deepcopy(merged)


def enabled_chains(settings: dict | None = None) -> list[str]:
    from .chains import CHAINS
    s = settings or load_settings()
    if s.get("chains_enabled") is None:
        return [k for k, c in CHAINS.items() if c.default_enabled]
    return [k for k in s["chains_enabled"] if k in CHAINS]


# ---------------------------------------------------------------- secrets
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), buf)
    blob_out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI error")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _read_secrets() -> dict[str, str]:
    if not SECRETS_PATH.exists():
        return {}
    raw = SECRETS_PATH.read_bytes()
    try:
        if sys.platform == "win32" and raw[:1] != b"{":
            raw = _dpapi(raw, protect=False)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def get_secret(name: str) -> str | None:
    env = os.environ.get(f"DAS_{name.upper()}")
    if env:
        return env
    return _read_secrets().get(name) or None


def set_secret(name: str, value: str | None) -> None:
    s = _read_secrets()
    if value:
        s[name] = value
    else:
        s.pop(name, None)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(s).encode("utf-8")
    if sys.platform == "win32":
        raw = _dpapi(raw, protect=True)
    SECRETS_PATH.write_bytes(raw)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass


def secrets_status() -> dict[str, bool]:
    names = ["coingecko_demo_key", "etherscan_key", "dune_key"]
    s = _read_secrets()
    return {n: bool(s.get(n) or os.environ.get(f"DAS_{n.upper()}")) for n in names}
