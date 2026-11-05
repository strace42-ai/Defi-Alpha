"""Адресное обновление токенов по приоритетам (v2).

Уровни приоритета:
  0 — открытая в интерфейсе карточка (фокус, с ограниченным сроком);
  1 — личный список наблюдения (кроме статуса «Архив»), независимо от фильтра возраста;
  2 — лучшие автоматические кандидаты (уровни C–E, не скрытые);
  3 — предварительно интересные (уровень B, ручная проверка);
  4 — прочие обнаруженные;
  5 — отклонённые, скрытые пользователем, служебные активы.
Частота каждого вида обновления задаётся в settings.refresh_sec[вид][уровень]; 0 — не обновлять автоматически.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import config, db, market, pipeline
from .chains import CHAINS
from .enrich import get_facts, save_facts, trusted_quotes
from .http import SourceError
from .sources import coingecko, defillama, dexscreener
from .sources.evm_rpc import Rpc
from .util import dumps, loads, normalize_address, now_iso, now_utc, parse_iso

_focus: dict[str, float] = {}
_focus_lock = threading.Lock()
DEAD = "0x000000000000000000000000000000000000dead"


# ------------------------------------------------------------------ фокус (открытая карточка)
def set_focus(uid: str) -> None:
    with _focus_lock:
        _focus[uid] = time.monotonic()


def focused() -> list[str]:
    ttl = float(config.load_settings().get("focus_ttl_sec", 90))
    now = time.monotonic()
    with _focus_lock:
        for k in [k for k, t in _focus.items() if now - t > ttl]:
            _focus.pop(k, None)
        return list(_focus)


# ------------------------------------------------------------------ уровни приоритета
def tiers(top_n: int | None = None) -> dict[str, int]:
    s = config.load_settings()
    top = set(pipeline.best_candidates(top_n or int(s.get("top_candidates_n", 30))))
    hidden = {r["token_uid"] for r in db.q("SELECT token_uid FROM hidden_tokens WHERE active=1")}
    watch = {r["token_uid"]: r["status"] for r in db.q("SELECT token_uid, status FROM watchlist")}
    out: dict[str, int] = {}
    for r in db.q("SELECT uid, level, status, asset_class FROM tokens"):
        u = r["uid"]
        if u in watch and watch[u] != "archive":
            t = 1
        elif r["asset_class"] != "token" or u in hidden or r["status"] == "FAIL":
            t = 5
        elif u in top:
            t = 2
        elif r["level"] in ("B", "C", "D", "E") or r["status"] == "MANUAL REVIEW":
            t = 3
        else:
            t = 4
        if u in watch and watch[u] == "archive":
            t = min(t, 3)
        out[u] = t
    for u in focused():
        if u in out:
            out[u] = 0
    return out


def due(kind: str, last: str | None, tier: int) -> bool:
    iv = int((config.load_settings()["refresh_sec"].get(kind) or {}).get(str(tier), 0))
    if iv <= 0:
        return False
    lt = parse_iso(last)
    return lt is None or (now_utc() - lt).total_seconds() >= iv


def _state(uid: str) -> dict:
    r = db.q1("SELECT * FROM refresh_state WHERE token_uid=?", (uid,))
    return dict(r) if r else {}


def _set_state(uid: str, **kw) -> None:
    db.ex("INSERT OR IGNORE INTO refresh_state(token_uid) VALUES(?)", (uid,))
    sets = ", ".join(f"{k}=?" for k in kw)
    db.ex(f"UPDATE refresh_state SET {sets} WHERE token_uid=?", (*kw.values(), uid))


def due_tokens(kind: str, limit: int = 30) -> list[tuple[int, str]]:
    """Токены, которым пора обновиться, по возрастанию уровня (0 — первыми)."""
    t = tiers()
    col = {"quote": "last_quote_at", "coingecko": "last_cg_at", "fundamentals": "last_fund_at"}.get(kind)
    rows = {r["token_uid"]: dict(r) for r in db.q("SELECT * FROM refresh_state")}
    tok = {r["uid"]: dict(r) for r in db.q("SELECT uid, last_deep_at, last_onchain_at, protocol_slug, coingecko_id, chain FROM tokens")}
    mstat = {r["token_uid"]: ("UNKNOWN" if r["status"] == "UNKNOWN" else r["confidence"]) for r in db.q(
        "SELECT token_uid, status, confidence FROM filter_current WHERE filter_key='mcap'")} if kind == "coingecko" else {}
    out = []
    for uid, tier in t.items():
        if kind == "deep":
            last = (tok.get(uid) or {}).get("last_deep_at")
        elif kind == "onchain":
            last = (tok.get(uid) or {}).get("last_onchain_at")
            ch = CHAINS.get((tok.get(uid) or {}).get("chain"))
            if not ch or ch.kind != "evm" or not ch.logs_ok:
                continue
        else:
            last = (rows.get(uid) or {}).get(col)
            if kind == "coingecko" and not (tok.get(uid) or {}).get("coingecko_id"):
                continue
            if kind == "fundamentals" and not (tok.get(uid) or {}).get("protocol_slug"):
                continue
        if due(kind, last, tier):
            # при равном приоритете сначала токены, где MCAP не установлен (CoinGecko может дать circulating)
            pri2 = (0 if mstat.get(uid) == "UNKNOWN" else 1 if mstat.get(uid) == "preliminary" else 2) if kind == "coingecko" else 0
            out.append((tier, pri2, last or "", uid))
    out.sort()
    return [(t_, u) for t_, _, _, u in out[:limit]]


# ------------------------------------------------------------------ котировки (быстрый канал)
def refresh_quotes(uids: list[str]) -> dict:
    """Цена и ликвидность: DEX Screener (все пары токена) + DefiLlama coins как независимая проверка.
    last_market_at обновляется ТОЛЬКО при успешном получении цены (иначе данные помечаются устаревшими)."""
    stats = {"ok": 0, "failed": 0}
    by_chain: dict[str, list[str]] = {}
    for u in uids:
        by_chain.setdefault(u.split(":")[0], []).append(u)
    s = config.load_settings()["filters"]
    for ck, us in by_chain.items():
        ch = CHAINS.get(ck)
        if not ch:
            continue
        llama = {}
        if ch.llama_coins:
            try:
                llama, _ = defillama.coin_prices([f"{ch.llama_coins}:{u.split(':', 1)[1]}" for u in us])
            except SourceError:
                llama = {}
        for u in us:
            addr = u.split(":", 1)[1]
            f = get_facts(u)
            old = loads(dumps(f), {})
            try:
                pairs, raw = dexscreener.token_pairs(ch.ds, addr)
            except SourceError as e:
                _set_state(u, last_quote_at=now_iso(), last_quote_error=str(e)[:300])
                stats["failed"] += 1
                continue
            views = [market.pool_view_from_ds(p, addr, trusted_quotes(ch)) for p in pairs]
            pools, robust = market.assess_pools(market.dedupe_pools(views), float(s["min_pool_liquidity_usd"]),
                                                float(s["price_outlier_pct"]))
            good = [p for p in pools if p["quality"]]
            lp = llama.get(f"{ch.llama_coins}:{addr}") or {}
            gt = f.get("gt") or {}
            prev = db.q1("SELECT value, COALESCE(observed_at, fetched_at) at FROM observations WHERE entity_type='token' AND entity_id=? "
                         "AND metric='price_usd_robust' ORDER BY id DESC LIMIT 1", (u,))
            prev_d = None
            if prev and prev["value"]:
                pt = parse_iso(prev["at"])
                prev_d = {"value": prev["value"], "at_ts": pt.timestamp() if pt else None, "now_ts": now_utc().timestamp()}
            gt_age_ok = parse_iso(gt.get("at")) and (now_utc() - parse_iso(gt.get("at"))).total_seconds() < 3600
            refs = {"defillama": lp.get("price") if (lp.get("confidence") or 0) >= 0.8 else None,
                    "geckoterminal": gt.get("price_usd") if gt_age_ok else None}
            pc = market.price_check(good, robust, refs, prev_d)
            pc["at"] = now_iso()
            if pc.get("value") is None:
                _set_state(u, last_quote_at=now_iso(), last_quote_error="цена не установлена: нет качественных пар/провайдеров")
                stats["failed"] += 1
                continue
            f["price_check"] = pc
            f["price"] = {"value": pc["value"], "source": "derived:quality_pools" if robust else "providers", "at": now_iso(),
                          "method": pc["method"], "confidence": pc["confidence"]}
            if pairs:
                top = max(views, key=lambda v: v.get("liquidity_usd") or 0)
                f["ds"] = {**(f.get("ds") or {}), "top_pair": top.get("address"), "side": top.get("side"),
                           "price_usd": top.get("price_usd"), "liquidity_usd": top.get("liquidity_usd"),
                           "volume_h24": sum(v.get("volume_h24") or 0 for v in views if v.get("side") == "base") or top.get("volume_h24"),
                           "fdv": top.get("ds_fdv"), "market_cap": top.get("ds_market_cap"), "at": now_iso(),
                           "url": f"https://dexscreener.com/{ch.ds}/{addr}"}
                pl = f.get("pools") or {}
                pl.update({"quality_count": len(good), "liquidity_quality_usd": sum(p.get("liquidity_usd") or 0 for p in good),
                           "quotes_at": now_iso()})
                f["pools"] = pl
            db.observe("token", u, "price_usd_robust", pc["value"], source="derived", unit="USD", confidence="derived",
                       method=pc["method"], raw_id=raw, commit=False)
            db.observe("token", u, "liquidity_usd_quality", (f.get("pools") or {}).get("liquidity_quality_usd"), source="dexscreener",
                       unit="USD", confidence="reported", method="сумма ликвидности качественных пар", commit=False)
            db.get_conn().commit()
            save_facts(u, f, "last_market_at")
            _set_state(u, last_quote_at=now_iso(), last_quote_ok_at=now_iso(), last_quote_error=None)
            pipeline.evaluate(u, old_facts=old)  # пересчёт MCAP, фильтров и сигналов по новой цене (без сетевых запросов)
            stats["ok"] += 1
    return stats


# ------------------------------------------------------------------ CoinGecko (circulating supply)
def refresh_coingecko(uid: str) -> dict:
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    f = get_facts(uid)
    ch = CHAINS[tok["chain"]]
    try:
        info, _ = coingecko.coin(tok["coingecko_id"])
    except SourceError as e:
        _set_state(uid, last_cg_at=now_iso())
        cg = f.get("coingecko") or {}
        cg["error"] = str(e)[:200]
        cg["error_at"] = now_iso()
        f["coingecko"] = cg  # прежнее значение сохраняется, отмечается ошибка
        save_facts(uid, f, "updated_at")
        return {"error": str(e)}
    if not info:
        _set_state(uid, last_cg_at=now_iso())
        return {"error": "не найдено"}
    info["verified_contract"] = coingecko.contract_matches(info, set(ch.cg_platforms), tok["address"])
    info["at"] = now_iso()
    f["coingecko"] = info
    for metric, v in (("circulating_supply_cg", info.get("circulating_supply")), ("market_cap_cg", info.get("market_cap_usd"))):
        db.observe("token", uid, metric, v, source="coingecko", confidence="reported", method="CoinGecko /coins/{id}",
                   verify_url=info.get("url"), observed_at=info.get("last_updated"))
    save_facts(uid, f, "updated_at")
    _set_state(uid, last_cg_at=now_iso())
    pipeline.evaluate(uid, old_facts=None)
    return {"verified": info["verified_contract"], "circulating": info.get("circulating_supply")}


# ------------------------------------------------------------------ ончейн: необращающиеся адреса
def refresh_circulation(uid: str) -> dict:
    """Балансы адресов с меткой circulation=excluded (с доказательством) и burn-адреса 0x…dEaD.
    Адреса уникальны → двойное вычитание невозможно; staking/казначейство без доказательства НЕ вычитаются."""
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    ch = CHAINS[tok["chain"]]
    if ch.kind != "evm":
        return {"error": "только EVM"}
    labels = db.rows_to_dicts(db.q("SELECT * FROM address_labels WHERE chain=? AND (token_uid=? OR token_uid IS NULL)", (ch.key, uid)))
    targets = {l["address"]: l for l in labels if l.get("circulation") == "excluded"}
    targets.setdefault(DEAD, {"address": DEAD, "label": "burn-адрес 0x…dEaD", "circulation": "excluded",
                              "circulation_evidence": "стандартный адрес сжигания (ключ недоступен)"})
    rpc = Rpc(ch)
    dec = tok["decimals"] if tok["decimals"] is not None else 18
    excluded, uncertain = [], []
    for a, l in targets.items():
        try:
            r = rpc.eth_call(tok["address"], "0x70a08231" + "0" * 24 + a[2:].lower())
            bal = int(r, 16) if r and r != "0x" else 0
        except SourceError as e:
            return {"error": str(e)}
        if bal:
            excluded.append({"address": a, "label": l.get("label"), "balance_raw": str(bal), "balance": bal / 10 ** dec,
                             "evidence": l.get("circulation_evidence") or l.get("source_url")})
    for l in labels:
        if l.get("circulation") in (None, "uncertain") and l["address"] not in targets:
            uncertain.append({"address": l["address"], "label": l.get("label"), "kind": l.get("kind"),
                              "note": "не вычитается: нет доказательства, что токены не обращаются"})
    f = get_facts(uid)
    oc = f.get("onchain") or {}
    oc["circulation"] = {"excluded": excluded, "uncertain": uncertain, "at": now_iso(),
                         "note": "вычитаются только адреса с доказанной блокировкой; адреса уникальны — без двойного учёта"}
    f["onchain"] = oc
    save_facts(uid, f, "updated_at")
    return {"excluded": len(excluded), "uncertain": len(uncertain)}
