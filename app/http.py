"""HTTP-клиент с лимитами запросов, повторами, кешем и учётом состояния источников.

- Равномерный лимитер на источник (запросов в минуту из настроек).
- Повторы при 429/5xx/таймаутах с экспоненциальной задержкой; учитывается Retry-After.
- Ответ Cloudflare-проверки (HTML «Just a moment») распознаётся как блокировка — обход НЕ выполняется.
- Каждый ответ сохраняется в raw_responses (gzip) для воспроизводимости.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

from . import config, db
from .util import now_iso

log = logging.getLogger("das.http")
USER_AGENT = "DeFiAlphaScanner/0.1 (local read-only research tool)"


class SourceError(Exception):
    def __init__(self, source: str, kind: str, message: str, status: int | None = None):
        super().__init__(f"[{source}] {kind}: {message}")
        self.source, self.kind, self.status, self.message = source, kind, status, message


class _Limiter:
    def __init__(self, per_min: float):
        self.interval = 60.0 / max(per_min, 0.1)
        self.next_at = 0.0
        self.lock = threading.Lock()
        self.penalty_until = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(self.next_at, self.penalty_until, now)
            self.next_at = t + self.interval
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.penalty_until = max(self.penalty_until, time.monotonic() + seconds)


_limiters: dict[str, _Limiter] = {}
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_client: httpx.Client | None = None
# Счётчики вызовов за текущий процесс (для страницы «Источники данных»)
budget: dict[str, dict[str, int]] = {}


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0),
                               headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    return _client


def set_client(c: httpx.Client | None) -> None:
    """Для тестов."""
    global _client
    _client = c


def reset_limiters() -> None:
    _limiters.clear()
    with _cache_lock:
        _cache.clear()


def limiter(source: str) -> _Limiter:
    if source not in _limiters:
        rl = config.load_settings()["rate_limits_per_min"]
        base = source.split(":")[0]
        _limiters[source] = _Limiter(float(rl.get(base, 30)))
    return _limiters[source]


def _status(source: str, ok: bool, err: str | None = None, rate_limited: bool = False) -> None:
    b = budget.setdefault(source, {"calls": 0, "errors": 0, "rate_limited": 0})
    b["calls"] += 1
    if not ok:
        b["errors"] += 1
    if rate_limited:
        b["rate_limited"] += 1
    try:
        now = now_iso()
        c = db.get_conn()
        c.execute("INSERT OR IGNORE INTO source_status(source) VALUES(?)", (source,))
        if ok:
            c.execute("UPDATE source_status SET last_success_at=?, calls_total=calls_total+1 WHERE source=?",
                      (now, source))
        else:
            c.execute("UPDATE source_status SET last_error_at=?, last_error=?, calls_total=calls_total+1, "
                      "errors_total=errors_total+1, rate_limited_total=rate_limited_total+? WHERE source=?",
                      (now, (err or "")[:500], 1 if rate_limited else 0, source))
        c.commit()
    except Exception:  # статус не должен ломать сбор
        pass


def request_json(source: str, method: str, url: str, *, params: dict | None = None,
                 json_body: Any = None, headers: dict | None = None, retries: int = 4,
                 cache_ttl: float = 0, save_raw: bool = True, allow_404: bool = False) -> tuple[Any, int | None]:
    """Вернуть (json, raw_id). Бросает SourceError при окончательной неудаче."""
    key = f"{method} {url} {json.dumps(params, sort_keys=True)} {json.dumps(json_body, sort_keys=True)}"
    if cache_ttl > 0:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1], None
    lim = limiter(source)
    last_err = "unknown"
    for attempt in range(retries + 1):
        lim.wait()
        try:
            r = client().request(method, url, params=params, json=json_body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"сетевая ошибка: {type(e).__name__}: {e}"
            _status(source, False, last_err)
            time.sleep(min(2 ** attempt, 20))
            continue
        body = r.content
        ctype = r.headers.get("content-type", "")
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            wait = float(ra) if ra and ra.replace(".", "").isdigit() and float(ra) > 0 else 15 * (attempt + 1)
            wait = min(wait, 90)
            lim.penalize(wait)
            last_err = f"HTTP 429 (лимит запросов), пауза {wait:.0f} c"
            _status(source, False, last_err, rate_limited=True)
            continue
        if r.status_code in (403, 503) and b"Just a moment" in body[:3000]:
            _status(source, False, "заблокировано Cloudflare-проверкой (обход не выполняется)")
            raise SourceError(source, "blocked", "Cloudflare-проверка; источник недоступен из этой сети", r.status_code)
        if r.status_code >= 500:
            last_err = f"HTTP {r.status_code}"
            _status(source, False, last_err)
            time.sleep(min(2 ** attempt, 20))
            continue
        if r.status_code == 404 and allow_404:
            _status(source, True)
            return None, None
        if r.status_code >= 400:
            msg = body[:300].decode("utf-8", "replace")
            _status(source, False, f"HTTP {r.status_code}: {msg}")
            raise SourceError(source, "http_error", msg, r.status_code)
        raw_id = db.save_raw(source, str(r.url), r.status_code, body) if save_raw else None
        try:
            data = r.json() if body else None
        except ValueError:
            _status(source, False, f"не JSON ({ctype})")
            raise SourceError(source, "bad_response", f"ответ не JSON ({ctype})", r.status_code)
        _status(source, True)
        if cache_ttl > 0:
            with _cache_lock:
                _cache[key] = (time.monotonic() + cache_ttl, data)
        return data, raw_id
    raise SourceError(source, "unavailable", last_err)


def get_json(source: str, url: str, **kw) -> tuple[Any, int | None]:
    return request_json(source, "GET", url, **kw)


def get_text(source: str, url: str, retries: int = 2) -> str:
    lim = limiter(source)
    last = ""
    for attempt in range(retries + 1):
        lim.wait()
        try:
            r = client().get(url)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = str(e)
            _status(source, False, last)
            continue
        if r.status_code == 200:
            _status(source, True)
            db.save_raw(source, url, 200, r.content)
            return r.text
        last = f"HTTP {r.status_code}"
        _status(source, False, last, rate_limited=r.status_code == 429)
        if r.status_code == 429:
            lim.penalize(20)
    raise SourceError(source, "unavailable", last)
