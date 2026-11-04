"""Общие утилиты: время (всегда UTC), нормализация адресов, числа."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Any

UTC = _dt.timezone.utc

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_EVM_POOL_RE = re.compile(r"^0x[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")  # v4 pool ids = 32 bytes
_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(UTC).replace(microsecond=0)


def iso(dt: _dt.datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now_utc())  # type: ignore[return-value]


def parse_iso(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def from_ts(ts: float | int | None, ms: bool = False) -> _dt.datetime | None:
    if ts is None:
        return None
    try:
        v = float(ts) / (1000.0 if ms else 1.0)
        return _dt.datetime.fromtimestamp(v, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def days_between(a: _dt.datetime, b: _dt.datetime) -> float:
    return (b - a).total_seconds() / 86400.0


def is_evm_address(addr: str) -> bool:
    return bool(addr) and bool(_EVM_RE.match(addr))


def normalize_address(chain_kind: str, addr: str) -> str:
    """EVM-адреса регистронезависимы → нижний регистр.
    Solana mint-адреса (base58) РЕГИСТРОЗАВИСИМЫ → сохраняются как есть."""
    addr = (addr or "").strip()
    if chain_kind == "evm":
        return addr.lower()
    return addr


def token_uid(chain_key: str, chain_kind: str, address: str) -> str:
    """Единица учёта: конкретный токен на конкретной сети (не тикер и не пара)."""
    return f"{chain_key}:{normalize_address(chain_kind, address)}"


def split_uid(uid: str) -> tuple[str, str]:
    chain, _, addr = uid.partition(":")
    return chain, addr


def valid_address(chain_kind: str, addr: str) -> bool:
    if chain_kind == "evm":
        return is_evm_address(addr)
    return bool(_B58_RE.match(addr or ""))


def fnum(v: Any) -> float | None:
    """Безопасное преобразование в число. Отсутствие ≠ 0: возвращает None."""
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


def dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, default=str, sort_keys=True)


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def median(xs: list[float]) -> float | None:
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "н/д"
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.2f} млрд"
    if a >= 1e6:
        return f"${v/1e6:.2f} млн"
    if a >= 1e3:
        return f"${v/1e3:.1f} тыс."
    if a >= 1:
        return f"${v:.2f}"
    return f"${v:.6g}"
