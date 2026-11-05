"""DISCOVERY ENGINE — обнаружение новых токенов.

Источники (каждый со своей семантикой покрытия):
1. GeckoTerminal new_pools — новые пулы почти в реальном времени. Адаптивная пагинация:
   страницы запрашиваются, пока не будет достигнут самый новый пул прошлого запуска
   (непрерывное покрытие) или лимит страниц. Разрывы записываются в таблицу coverage.
2. GeckoTerminal top pools (по объёму) — исторический добор активных пулов за 90 дней.
3. DefiLlama /protocols — недавно добавленные протоколы с адресом токена (DeFi-ориентированный добор).
4. (выключено по умолчанию) DEX Screener token-profiles — ПРОМО-источник, нерепрезентативен.

Единица учёта — токен на конкретной сети (chain + нормализованный адрес).
"""
from __future__ import annotations

import logging
import re
from typing import Callable

from . import config, db
from .chains import CHAINS, Chain, by_llama, by_ds
from .http import SourceError
from .sources import defillama, dexscreener, geckoterminal
from .util import dumps, loads, from_ts, iso, normalize_address, now_iso, now_utc, parse_iso, token_uid, valid_address, days_between

log = logging.getLogger("das.discovery")
Progress = Callable[[str], None]


_ISSUER_RE = re.compile(r"(\b(Inc\.?|Corporation|Corp\.?|ETF|Trust|plc|N\.V\.|Holdings|Technologies Corp)\s*$)|(\bInc\.?\b)", re.I)


def classify_asset(chain: Chain, address: str, symbol: str | None, name: str | None) -> str:
    if address in chain.quote_assets:
        return "quote"
    n = (name or "")
    if "Robinhood Token" in n:  # токенизированные акции на Robinhood Chain
        return "tokenized_stock"
    if _ISSUER_RE.search(n):  # «Apple Inc.», «Meta Platforms Inc.», «… ETF Trust» — токенизированные бумаги эмитентов
        return "tokenized_stock"
    return "token"


def upsert_token(chain: Chain, tok: dict, *, source: str, reason: str) -> tuple[str | None, bool]:
    raw_addr = (tok.get("address") or "").strip()
    addr = normalize_address(chain.kind, raw_addr)
    if not valid_address(chain.kind, addr):
        return None, False
    uid = token_uid(chain.key, chain.kind, addr)
    with db.tx():  # проверка существования и вставка — атомарно (параллельные очереди не создают дублей/ошибок PK)
        return _upsert_token_tx(chain, tok, addr, raw_addr, uid, source, reason)


def _upsert_token_tx(chain, tok, addr, raw_addr, uid, source, reason):
    row = db.q1("SELECT uid, symbol, name, decimals, coingecko_id FROM tokens WHERE uid=?", (uid,))
    now = now_iso()
    aclass = classify_asset(chain, addr, tok.get("symbol"), tok.get("name"))
    if row is None:
        db.ex("INSERT INTO tokens(uid,chain,address,address_display,symbol,name,decimals,first_seen_at,"
              "discovery_source,discovery_reason,is_quote_asset,asset_class,coingecko_id,updated_at) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, chain.key, addr, raw_addr, tok.get("symbol"), tok.get("name"), tok.get("decimals"), now,
               source, reason, 1 if aclass == "quote" else 0, aclass, tok.get("coingecko_id"), now))
        return uid, True
    # метаданные дополняются, но не затираются пустыми значениями
    db.ex("UPDATE tokens SET symbol=COALESCE(symbol,?), name=COALESCE(name,?), decimals=COALESCE(decimals,?), "
          "coingecko_id=COALESCE(coingecko_id,?), updated_at=? WHERE uid=?",
          (tok.get("symbol"), tok.get("name"), tok.get("decimals"), tok.get("coingecko_id"), now, uid))
    if row["name"] is None and tok.get("name"):
        ac = classify_asset(chain, addr, tok.get("symbol"), tok.get("name"))
        if ac != "token":
            db.ex("UPDATE tokens SET asset_class=?, is_quote_asset=? WHERE uid=?", (ac, 1 if ac == "quote" else 0, uid))
    return uid, False


def upsert_pool(chain: Chain, *, address: str, dex: str | None, name: str | None, base_uid: str | None,
                quote_uid: str | None, created_at: str | None, source: str) -> str:
    puid = f"{chain.key}:{normalize_address(chain.kind, address)}"
    with db.tx():
        _upsert_pool_tx(chain, puid, address, dex, name, base_uid, quote_uid, created_at, source)
    return puid


def _upsert_pool_tx(chain, puid, address, dex, name, base_uid, quote_uid, created_at, source):
    now = now_iso()
    row = db.q1("SELECT uid, sources, pool_created_at FROM pools WHERE uid=?", (puid,))
    if row is None:
        db.ex("INSERT INTO pools(uid,chain,address,dex,name,base_uid,quote_uid,pool_created_at,first_seen_at,"
              "last_seen_at,sources) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (puid, chain.key, normalize_address(chain.kind, address), dex, name, base_uid, quote_uid,
               created_at, now, now, dumps([source])))
    else:
        srcs = set(loads(row["sources"], []) or [])
        srcs.add(source)
        created = row["pool_created_at"]
        if created_at and (not created or created_at < created):
            created = created_at  # самая ранняя из известных дат создания пула
        db.ex("UPDATE pools SET last_seen_at=?, sources=?, pool_created_at=?, dex=COALESCE(dex,?), name=COALESCE(name,?) "
              "WHERE uid=?", (now, dumps(sorted(srcs)), created, dex, name, puid))


def _ingest_gt_pools(chain: Chain, pools: list[dict], source: str, reason_tpl: str, raw_id: int | None,
                     max_age_days: float | None = None) -> tuple[int, list[str]]:
    new_tokens: list[str] = []
    n = 0
    for p in pools:
        if not p.get("pool_address"):
            continue
        created = p.get("pool_created_at")
        if max_age_days is not None:
            dt = parse_iso(created)
            if not dt or days_between(dt, now_utc()) > max_age_days:
                continue
        reason = reason_tpl.format(pool=p.get("name"), dex=p.get("dex"), created=created)
        with db.tx():  # один пул — одна короткая транзакция (только SQL, без сети)
            res = _ingest_one(chain, p, source, reason, raw_id, created)
        n += 1
        for uid, isnew in res:
            if uid and isnew:
                new_tokens.append(uid)
    return n, new_tokens


def _ingest_one(chain, p, source, reason, raw_id, created):
    buid, bnew = upsert_token(chain, p["base"], source=source, reason=reason)
    quid, qnew = upsert_token(chain, p["quote"], source=source, reason=reason)
    puid = upsert_pool(chain, address=p["pool_address"], dex=p.get("dex"), name=p.get("name"),
                       base_uid=buid, quote_uid=quid, created_at=created, source=source)
    if buid:
        db.observe("pool", puid, "reserve_usd", p.get("reserve_usd"), source=source, unit="USD",
                   confidence="reported", method="pool.reserve_in_usd", raw_id=raw_id)
        db.observe("pool", puid, "base_price_usd", p.get("base_price_usd"), source=source, unit="USD",
                   confidence="reported", method="pool.base_token_price_usd", raw_id=raw_id)
    return ((buid, bnew), (quid, qnew))


def discover_new_pools(chain_key: str, progress: Progress | None = None) -> dict:
    chain = CHAINS[chain_key]
    s = config.load_settings()
    max_pages = int(s["discovery"]["max_pages_per_chain"])
    pkey = f"gt_new_pools_newest:{chain_key}"
    prev_newest = db.get_progress(pkey)
    oldest = newest = None
    pages = pools_total = 0
    new_tokens: list[str] = []
    continuous = False
    error = None
    for page in range(1, max_pages + 1):
        try:
            pools, raw = geckoterminal.new_pools(chain.gt, page)
        except SourceError as e:
            error = str(e)
            break
        pages += 1
        if not pools:
            break
        ts = [p["pool_created_at"] for p in pools if p.get("pool_created_at")]
        if ts:
            oldest = min([oldest] + ts) if oldest else min(ts)
            newest = max([newest] + ts) if newest else max(ts)
        n, nt = _ingest_gt_pools(chain, pools, "geckoterminal:new_pools",
                                 "новый пул {pool} ({dex}), создан {created}", raw)
        pools_total += n
        new_tokens += nt
        if progress:
            progress(f"{chain.name}: страница {page}, пулов {pools_total}, новых токенов {len(new_tokens)}")
        if prev_newest and oldest and oldest <= prev_newest:
            continuous = True
            break
    gap_start = gap_end = None
    note = ""
    if prev_newest is None:
        note = "первый запуск: более ранние пулы этим источником не покрыты"
    elif not continuous and oldest:
        gap_start, gap_end = prev_newest, oldest
        note = f"РАЗРЫВ покрытия: пулы между {prev_newest} и {oldest} не получены (лимит {max_pages} стр.)"
    if error:
        note = (note + "; " if note else "") + f"ошибка: {error}"
    if newest and (not prev_newest or newest > prev_newest):
        db.set_progress(pkey, newest)
    db.ex("INSERT INTO coverage(chain,source,run_at,pages,pools,window_start,window_end,continuous,gap_start,gap_end,note) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (chain_key, "geckoterminal:new_pools", now_iso(), pages, pools_total, oldest, newest,
           1 if continuous else 0, gap_start, gap_end, note))
    return {"chain": chain_key, "pages": pages, "pools": pools_total, "new_tokens": new_tokens,
            "window": [oldest, newest], "continuous": continuous, "note": note, "error": error}


def discover_top_pools(chain_key: str, progress: Progress | None = None) -> dict:
    """Исторический добор: активные пулы (по объёму 24ч), созданные не позднее max_age_days."""
    chain = CHAINS[chain_key]
    s = config.load_settings()
    pages = int(s["discovery"]["historical_top_pools_pages"])
    max_age = float(s["filters"]["max_age_days"])
    if s["filters"].get("tge_mode") == "fixed_date" and s["filters"].get("tge_from_date"):
        # исторический добор — пулы не старше фиксированной даты (а не скользящих N дней)
        fd = parse_iso(f"{s['filters']['tge_from_date']}T00:00:00Z")
        if fd:
            max_age = max(1.0, days_between(fd, now_utc()))
    new_tokens: list[str] = []
    got = 0
    err = None
    for page in range(1, pages + 1):
        try:
            pools, raw = geckoterminal.top_pools(chain.gt, page)
        except SourceError as e:
            err = str(e)
            break
        if not pools:
            break
        n, nt = _ingest_gt_pools(chain, pools, "geckoterminal:top_pools",
                                 "активный пул {pool} ({dex}) в топе по объёму, создан {created}", raw,
                                 max_age_days=max_age)
        got += n
        new_tokens += nt
    if progress:
        progress(f"{chain.name}: топ-пулы — {got} пулов моложе {max_age:.0f} дн.")
    db.ex("INSERT INTO coverage(chain,source,run_at,pages,pools,continuous,note) VALUES(?,?,?,?,?,?,?)",
          (chain_key, "geckoterminal:top_pools", now_iso(), pages, got, 0,
           f"только топ-{pages*20} пулов по объёму 24ч; неактивные пулы не покрываются" + (f"; ошибка: {err}" if err else "")))
    return {"chain": chain_key, "pools": got, "new_tokens": new_tokens, "error": err}


def parse_llama_address(addr: str | None) -> tuple[Chain | None, str | None]:
    if not addr or not isinstance(addr, str):
        return None, None
    if ":" in addr:
        prefix, a = addr.split(":", 1)
        ch = by_llama(prefix)
    else:
        prefix, a = "ethereum", addr
        ch = CHAINS["eth"] if addr.startswith("0x") else None
    if not ch:
        return None, None
    a = normalize_address(ch.kind, a)
    return (ch, a) if valid_address(ch.kind, a) else (None, None)


def refresh_protocols(progress: Progress | None = None) -> list[dict]:
    data, _ = defillama.protocols()  # сеть — до любых транзакций
    now = now_iso()
    rows = []
    for p in data:
        slug = p.get("slug")
        if not slug:
            continue
        slim = {k: p.get(k) for k in ("name", "category", "chains", "address", "gecko_id", "symbol", "url",
                                      "listedAt", "tvl", "mcap", "audits", "parentProtocol", "twitter", "forkedFrom")}
        rows.append((slug, p.get("name"), p.get("category"), dumps(p.get("chains") or []), p.get("address"),
                     p.get("gecko_id"), p.get("symbol"), p.get("url"), iso(from_ts(p.get("listedAt"))), dumps(slim), now))
    # пакетами по 500 (короткие транзакции); старые записи не удаляются — пустой/неполный ответ ничего не стирает
    db.exmany("INSERT INTO protocols(slug,name,category,chains,address,gecko_id,symbol,url,listed_at,raw,fetched_at) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name, "
              "category=excluded.category, chains=excluded.chains, address=excluded.address, gecko_id=excluded.gecko_id, "
              "symbol=excluded.symbol, url=excluded.url, listed_at=excluded.listed_at, raw=excluded.raw, fetched_at=excluded.fetched_at",
              rows, chunk=500)
    if progress:
        progress(f"DefiLlama: обновлено {len(data)} протоколов")
    return data


def discover_defillama(chains: list[str], progress: Progress | None = None, listed_within_days: int = 180) -> dict:
    """Протоколы DefiLlama с адресом токена в включённых сетях, добавленные за последние N дней."""
    try:
        data = refresh_protocols(progress)
    except SourceError as e:
        return {"new_tokens": [], "error": str(e)}
    cutoff = now_utc().timestamp() - listed_within_days * 86400
    new_tokens: list[str] = []
    seen = 0
    for p in data:
        ch, addr = parse_llama_address(p.get("address"))
        if not ch or ch.key not in chains:
            continue
        if (p.get("listedAt") or 0) < cutoff:
            continue
        seen += 1
        uid, isnew = upsert_token(ch, {"address": addr, "symbol": p.get("symbol"), "coingecko_id": p.get("gecko_id")},
                                  source="defillama:protocols",
                                  reason=f"протокол DefiLlama «{p.get('name')}» ({p.get('category')}), "
                                         f"добавлен {iso(from_ts(p.get('listedAt')))}, адрес токена в листинге")
        if uid and isnew:
            new_tokens.append(uid)
    db.ex("INSERT INTO coverage(chain,source,run_at,pools,continuous,note) VALUES(?,?,?,?,?,?)",
          ("*", "defillama:protocols", now_iso(), seen, 0,
           f"протоколы с адресом токена, добавленные в DefiLlama за {listed_within_days} дн.; "
           "проекты без листинга DefiLlama этим источником не находятся"))
    if progress:
        progress(f"DefiLlama: {seen} недавних протоколов с токеном, новых токенов {len(new_tokens)}")
    return {"new_tokens": new_tokens, "protocols": seen}


def discover_ds_profiles(chains: list[str]) -> dict:
    """Промо-источник (платные профили DEX Screener). Помечается как нерепрезентативный."""
    try:
        items, _ = dexscreener.latest_profiles()
    except SourceError as e:
        return {"new_tokens": [], "error": str(e)}
    new_tokens = []
    for it in items:
        ch = by_ds(it.get("chain") or "")
        if not ch or ch.key not in chains:
            continue
        uid, isnew = upsert_token(ch, {"address": it.get("address")}, source="dexscreener:profiles",
                                  reason="ПРОМО: платный профиль DEX Screener (не репрезентативная выборка)")
        if uid and isnew:
            new_tokens.append(uid)
    return {"new_tokens": new_tokens}


def add_token_manually(chain_key: str, address: str, note: str = "") -> tuple[str | None, bool]:
    chain = CHAINS[chain_key]
    return upsert_token(chain, {"address": address}, source="manual",
                        reason=f"добавлен вручную{': ' + note if note else ''}")
