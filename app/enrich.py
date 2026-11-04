"""Обогащение рыночными данными.

quick_enrich — пакетно (дёшево): DEX Screener tokens/v1 и GeckoTerminal tokens/multi по 30 адресов.
deep_enrich  — по одному кандидату (дорого): все пулы токена, OHLCV, info, официальные эндпоинты supply.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import config, db, market
from .chains import CHAINS, Chain
from .discovery import upsert_pool, upsert_token
from .http import SourceError, get_text
from .sources import dexscreener, geckoterminal
from .util import dumps, fnum, loads, normalize_address, now_iso, token_uid

log = logging.getLogger("das.enrich")
Progress = Callable[[str], None]


def get_facts(uid: str) -> dict:
    r = db.q1("SELECT facts FROM tokens WHERE uid=?", (uid,))
    return loads(r["facts"], {}) if r else {}


def save_facts(uid: str, facts: dict, field: str = "last_market_at") -> None:
    db.ex(f"UPDATE tokens SET facts=?, {field}=?, updated_at=? WHERE uid=?", (dumps(facts), now_iso(), now_iso(), uid))


def _v(value: Any, source: str, *, at: str | None = None, method: str | None = None, confidence: str = "reported",
       url: str | None = None, **extra) -> dict:
    d = {"value": value, "source": source, "at": at or now_iso(), "method": method, "confidence": confidence, "url": url}
    d.update(extra)
    return d


def trusted_quotes(chain: Chain) -> set[str]:
    return set(chain.quote_assets.keys())


def _gt_url(chain: Chain, addr: str) -> str:
    return f"https://www.geckoterminal.com/{chain.gt}/tokens/{addr}"


def _ds_url(chain: Chain, addr: str) -> str:
    return f"https://dexscreener.com/{chain.ds}/{addr}"


# ------------------------------------------------------------------ quick
def quick_enrich(chain_key: str, uids: list[str], progress: Progress | None = None) -> dict:
    chain = CHAINS[chain_key]
    rows = {r["uid"]: dict(r) for r in db.q(
        f"SELECT uid,address,address_display,is_quote_asset,asset_class FROM tokens WHERE uid IN ({','.join('?'*len(uids))})", uids)} if uids else {}
    targets = [u for u in uids if u in rows and not rows[u]["is_quote_asset"]]
    stats = {"ds": 0, "gt": 0, "errors": []}
    for i in range(0, len(targets), 30):
        batch = targets[i:i + 30]
        addrs = [rows[u]["address"] for u in batch]
        # --- DEX Screener
        ds_by: dict[str, list[dict]] = {}
        try:
            pairs, raw = dexscreener.tokens(chain.ds, addrs)
            stats["ds"] += 1
            for p in pairs:
                for side in ("base", "quote"):
                    a = normalize_address(chain.kind, (p.get(side) or {}).get("address") or "")
                    u = token_uid(chain.key, chain.kind, a)
                    if u in batch:
                        ds_by.setdefault(u, []).append(p)
        except SourceError as e:
            stats["errors"].append(str(e))
            raw = None
        # --- GeckoTerminal
        gt_by: dict[str, dict] = {}
        try:
            toks, graw = geckoterminal.tokens_multi(chain.gt, addrs)
            stats["gt"] += 1
            for t in toks:
                u = token_uid(chain.key, chain.kind, t.get("address") or "")
                gt_by[u] = t
        except SourceError as e:
            stats["errors"].append(str(e))
            graw = None
        for u in batch:
            f = get_facts(u)
            addr = rows[u]["address"]
            t = gt_by.get(u)
            if t:
                if t.get("coingecko_id"):
                    db.ex("UPDATE tokens SET coingecko_id=? WHERE uid=?", (t["coingecko_id"], u))
                if t.get("decimals") is not None:
                    db.ex("UPDATE tokens SET decimals=COALESCE(decimals,?), symbol=COALESCE(symbol,?), name=COALESCE(name,?) WHERE uid=?",
                          (t["decimals"], t.get("symbol"), t.get("name"), u))
                url = _gt_url(chain, addr)
                for metric, val, unit, meth in (
                        ("price_usd", t.get("price_usd"), "USD", "GT token.price_usd (цена топ-пула)"),
                        ("fdv_usd_gt", t.get("fdv_usd"), "USD", "GT fdv_usd = цена × total_supply"),
                        ("mcap_usd_gt", t.get("market_cap_usd"), "USD", "GT market_cap_usd (circulating по данным CoinGecko)"),
                        ("total_supply", t.get("normalized_total_supply"), "tokens", "GT normalized_total_supply"),
                        ("liquidity_usd_gt", t.get("total_reserve_usd"), "USD", "GT total_reserve_in_usd по топ-пулам"),
                        ("volume_h24_gt", t.get("volume_h24"), "USD", "GT volume_usd.h24")):
                    db.observe("token", u, metric, val, source="geckoterminal", unit=unit, method=meth,
                               verify_url=url, raw_id=graw, commit=False)
                f["gt"] = {"price_usd": t.get("price_usd"), "fdv_usd": t.get("fdv_usd"),
                           "market_cap_usd": t.get("market_cap_usd"), "total_supply": t.get("normalized_total_supply"),
                           "liquidity_usd": t.get("total_reserve_usd"), "volume_h24": t.get("volume_h24"),
                           "coingecko_id": t.get("coingecko_id"), "at": now_iso(), "url": url}
            ps = ds_by.get(u) or []
            if ps:
                views = [market.pool_view_from_ds(p, addr, trusted_quotes(chain)) for p in ps]
                top = max(views, key=lambda v: v.get("liquidity_usd") or 0)
                websites = sorted({w for p in ps for w in p.get("websites") or []})
                socials = [s for p in ps[:1] for s in p.get("socials") or []]
                f["ds"] = {"top_pair": top.get("address"), "side": top.get("side"),
                           "price_usd": top.get("price_usd"),
                           "liquidity_usd": top.get("liquidity_usd"), "volume_h24": top.get("volume_h24"),
                           "fdv": top.get("ds_fdv"), "market_cap": top.get("ds_market_cap"),
                           "pair_created_at": top.get("created_at"), "at": now_iso(), "url": _ds_url(chain, addr)}
                if websites or socials:
                    f["links"] = {"websites": websites, "socials": socials, "source": "dexscreener", "at": now_iso(),
                                  "note": "ссылки из профиля DEX Screener — недоверенные данные, требуют проверки"}
                for metric, val in (("price_usd", top.get("price_usd")), ("liquidity_usd_ds", top.get("liquidity_usd")),
                                    ("fdv_usd_ds", top.get("ds_fdv")), ("mcap_usd_ds", top.get("ds_market_cap"))):
                    db.observe("token", u, metric, val, source="dexscreener", unit="USD",
                               method="DS top pair" + (" (токен — base)" if top.get("side") == "base" else ""),
                               verify_url=_ds_url(chain, addr), raw_id=raw, commit=False)
                for v, p in zip(views, ps):
                    base_uid, _ = upsert_token(chain, p.get("base") or {}, source="dexscreener:tokens", reason="пара токена-кандидата")
                    quote_uid, _ = upsert_token(chain, p.get("quote") or {}, source="dexscreener:tokens", reason="пара токена-кандидата")
                    if v.get("address"):
                        upsert_pool(chain, address=v["address"], dex=v.get("dex"), name=v.get("name"),
                                    base_uid=base_uid, quote_uid=quote_uid, created_at=v.get("created_at"),
                                    source="dexscreener")
            elif "ds" not in f:
                f["ds"] = {"note": "DEX Screener не вернул пар для токена", "at": now_iso()}
            db.get_conn().commit()
            save_facts(u, f)
        if progress:
            progress(f"{chain.name}: рыночные данные {min(i + 30, len(targets))}/{len(targets)}")
    return stats


# ------------------------------------------------------------------ deep
def _project_supply(uid: str) -> dict | None:
    """Официальный эндпоинт circulating supply, если задан фактом supply_endpoint_circulating."""
    r = db.q1("SELECT * FROM manual_facts WHERE token_uid=? AND fact_key='supply_endpoint_circulating' AND active=1 "
              "ORDER BY id DESC LIMIT 1", (uid,))
    if not r:
        return None
    url = r["value"]
    try:
        txt = get_text("project", url).strip()
        val = fnum(txt.split()[0] if txt else None)
    except (SourceError, IndexError) as e:
        return {"error": str(e), "url": url}
    if val is None:
        return {"error": "ответ не является числом (возможно, формат изменился)", "url": url}
    db.observe("token", uid, "circulating_supply", val, source="project_endpoint", unit="tokens",
               confidence="confirmed" if r["source_type"] == "official_docs" else "reported",
               method="официальный эндпоинт проекта", verify_url=url)
    return {"value": val, "url": url, "source_type": r["source_type"], "doc": r["evidence_url"], "at": now_iso()}


def _manual_value(uid: str, key: str) -> dict | None:
    r = db.q1("SELECT * FROM manual_facts WHERE token_uid=? AND fact_key=? AND active=1 ORDER BY id DESC LIMIT 1", (uid, key))
    return dict(r) if r else None


def current_tokenomics(uid: str) -> dict | None:
    r = db.q1("SELECT * FROM tokenomics_versions WHERE token_uid=? AND is_current=1 ORDER BY id DESC LIMIT 1", (uid,))
    if not r:
        return None
    d = dict(r)
    d["params"] = loads(d["params"], {})
    return d


def deep_enrich(uid: str, progress: Progress | None = None) -> dict:
    s = config.load_settings()
    fs = s["filters"]
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    if not tok:
        return {"error": "нет токена"}
    chain = CHAINS[tok["chain"]]
    addr = tok["address"]
    f = get_facts(uid)
    errors = []
    views: list[dict] = []
    tq = trusted_quotes(chain)
    # ---- пулы из GeckoTerminal
    try:
        gpools, _ = geckoterminal.token_pools(chain.gt, addr)
        for p in gpools:
            buid, _ = upsert_token(chain, p["base"], source="geckoterminal:token_pools", reason="пул токена-кандидата")
            quid, _ = upsert_token(chain, p["quote"], source="geckoterminal:token_pools", reason="пул токена-кандидата")
            upsert_pool(chain, address=p["pool_address"], dex=p.get("dex"), name=p.get("name"), base_uid=buid,
                        quote_uid=quid, created_at=p.get("pool_created_at"), source="geckoterminal")
            views.append(market.pool_view_from_gt(p, addr, tq))
    except SourceError as e:
        errors.append(str(e))
    # ---- пулы из DEX Screener
    try:
        dpairs, _ = dexscreener.token_pairs(chain.ds, addr)
        for p in dpairs:
            buid, _ = upsert_token(chain, p.get("base") or {}, source="dexscreener:token_pairs", reason="пара токена-кандидата")
            quid, _ = upsert_token(chain, p.get("quote") or {}, source="dexscreener:token_pairs", reason="пара токена-кандидата")
            if p.get("pair_address"):
                upsert_pool(chain, address=p["pair_address"], dex=p.get("dex"), name=None, base_uid=buid,
                            quote_uid=quid, created_at=p.get("pair_created_at"), source="dexscreener")
            views.append(market.pool_view_from_ds(p, addr, tq))
        if dpairs:
            websites = sorted({w for p in dpairs for w in p.get("websites") or []})
            socials = next((p.get("socials") for p in dpairs if p.get("socials")), [])
            if websites or socials:
                f["links"] = {"websites": websites, "socials": socials, "source": "dexscreener", "at": now_iso(),
                              "note": "ссылки из профиля DEX Screener — недоверенные данные, требуют проверки"}
    except SourceError as e:
        errors.append(str(e))
    pools = market.dedupe_pools(views)
    pools, med_price = market.assess_pools(pools, float(fs["min_pool_liquidity_usd"]), float(fs["price_outlier_pct"]))
    pools.sort(key=lambda p: -(p.get("liquidity_usd") or 0))
    good = [p for p in pools if p["quality"]]
    total_liq = sum(p.get("liquidity_usd") or 0 for p in pools if p.get("side"))
    good_liq = sum(p.get("liquidity_usd") or 0 for p in good)
    f["pools"] = {"count": len(pools), "quality_count": len(good), "liquidity_total_usd": total_liq,
                  "liquidity_quality_usd": good_liq, "items": pools[:25], "at": now_iso(),
                  "note": "GT /tokens/{addr}/pools (1 стр.) + DS /token-pairs; список может быть неполным"}
    if med_price is not None:
        f["price"] = _v(med_price, "derived:median_quality_pools", method=f"медиана цены по {len(good)} качественным пулам",
                        confidence="derived", pools=[p["address"] for p in good])
        db.observe("token", uid, "price_usd_robust", med_price, source="derived", unit="USD",
                   confidence="derived", method="медиана качественных пулов")
    else:
        gp = (f.get("gt") or {}).get("price_usd")
        f["price"] = _v(gp, "geckoterminal", method="цена топ-пула GT (качественных пулов нет)",
                        confidence="estimated") if gp else {"value": None, "note": "цена не установлена"}
    db.observe("token", uid, "liquidity_usd_quality", good_liq, source="derived", unit="USD", confidence="derived",
               method="сумма ликвидности качественных пулов")
    # ---- самый ранний известный пул (по ВСЕМ сохранённым пулам токена)
    er = db.q1("SELECT uid, dex, name, pool_created_at, sources FROM pools WHERE (base_uid=? OR quote_uid=?) "
               "AND pool_created_at IS NOT NULL ORDER BY pool_created_at ASC LIMIT 1", (uid, uid))
    if er:
        f["earliest_pool"] = {"at": er["pool_created_at"], "pool": er["uid"], "dex": er["dex"], "name": er["name"],
                              "sources": loads(er["sources"], []),
                              "note": "дата создания самого раннего ИЗВЕСТНОГО пула; более ранние пулы вне покрытия возможны"}
    # ---- OHLCV (дневные) по 3 качественным пулам из GT
    ohlcv_pools = [p for p in good if p["source"] == "geckoterminal" or "geckoterminal" in (p.get("also_in") or [])][:3]
    if not ohlcv_pools:
        ohlcv_pools = [p for p in pools if p.get("price_usd") and p.get("side")][:1]
        f["ath_pool_quality_warning"] = "нет качественных пулов: ATH рассчитан по лучшему доступному пулу"
    else:
        f.pop("ath_pool_quality_warning", None)
    candles: dict[str, list] = {}
    for p in ohlcv_pools:
        try:
            cs, meta, _ = geckoterminal.ohlcv(chain.gt, p["address"], addr, "day")
            if cs:
                candles[p["address"]] = cs
        except SourceError as e:
            errors.append(str(e))
    if candles:
        ath = market.compute_ath(candles, float(fs["ath_wick_factor"]))
        ath["candles_days"] = {k: len(v) for k, v in candles.items()}
        f["ath"] = ath
        f["price_history"] = {k: [[c[0], c[4], c[5]] for c in v] for k, v in candles.items()}
        db.observe("token", uid, "ath_usd", ath["ath_usd"], source="derived:geckoterminal_ohlcv", unit="USD",
                   confidence="derived", method=ath["method"], observed_at=ath["ath_at"])
    else:
        f["ath"] = {"ath_usd": None, "note": "OHLCV недоступен", "errors": errors[-1:]}
    # ---- GT info
    try:
        info, _ = geckoterminal.token_info(chain.gt, addr)
        if info:
            f["gt_info"] = {k: info.get(k) for k in ("websites", "twitter", "telegram", "discord", "gt_score",
                                                       "gt_verified", "categories", "holders", "mint_authority",
                                                       "freeze_authority", "is_honeypot")}
            f["gt_info"]["description_untrusted"] = (info.get("description") or "")[:1500]
            f["gt_info"]["at"] = now_iso()
            if info.get("holders"):
                db.observe("token", uid, "holders_count", fnum((info["holders"] or {}).get("count")),
                           source="geckoterminal", unit="addresses", method="GT holders.count")
    except SourceError as e:
        errors.append(str(e))
    # ---- предложение и капитализации
    proj = _project_supply(uid)
    if proj:
        f["circulating_official"] = proj
    tn = current_tokenomics(uid)
    if tn and tn["params"].get("max_supply"):
        f["max_supply"] = {"value": fnum(tn["params"]["max_supply"]), "source": "tokenomics_version",
                           "version": tn.get("version_label"), "url": tn.get("source_url"),
                           "source_type": tn.get("source_type")}
    f["errors"] = errors[-5:]
    compute_caps(uid, f)
    save_facts(uid, f, "last_deep_at")
    db.ex("UPDATE tokens SET last_market_at=? WHERE uid=?", (now_iso(), uid))
    if progress:
        progress(f"углублённые данные: {tok['symbol'] or addr[:10]}")
    return {"pools": len(pools), "quality": len(good), "errors": errors}


def compute_caps(uid: str, f: dict) -> None:
    """Сформировать кандидаты MCAP/FDV с явной маркировкой происхождения. Конфликты → журнал."""
    s = config.load_settings()["filters"]
    price = (f.get("price") or {}).get("value")
    gt = f.get("gt") or {}
    ds = f.get("ds") or {}
    onchain = f.get("onchain") or {}
    total = onchain.get("total_supply") or gt.get("total_supply")
    total_src = "rpc totalSupply()" if onchain.get("total_supply") else ("GeckoTerminal" if gt.get("total_supply") else None)
    cands = []
    co = f.get("circulating_official") or {}
    if co.get("value") is not None and price:
        cands.append({"kind": "circulating_official", "value": co["value"] * price,
                      "supply": co["value"], "source": "project_endpoint", "url": co.get("url"),
                      "confidence": "confirmed" if co.get("source_type") == "official_docs" else "reported",
                      "label": "официальный эндпоинт circulating × цена"})
    man = _manual_value(uid, "circulating_supply")
    if man and price and fnum(man["value"]):
        cands.append({"kind": "circulating_manual", "value": fnum(man["value"]) * price, "supply": fnum(man["value"]),
                      "source": "manual", "url": man.get("evidence_url"), "confidence": "reported",
                      "label": "circulating, внесён вручную × цена"})
    if gt.get("market_cap_usd"):
        if gt.get("fdv_usd") and abs(gt["market_cap_usd"] - gt["fdv_usd"]) <= 0.005 * gt["fdv_usd"]:
            cands.append({"kind": "gt_equals_fdv", "value": gt["market_cap_usd"], "source": "geckoterminal", "url": gt.get("url"),
                          "confidence": "not_evidence",
                          "label": "GeckoTerminal market_cap = FDV → circulating = totalSupply не подтверждён, НЕ доказательство MCAP"})
        else:
            cands.append({"kind": "coingecko_via_gt", "value": gt["market_cap_usd"], "source": "geckoterminal",
                          "url": gt.get("url"), "confidence": "reported",
                          "label": "market_cap_usd GeckoTerminal (circulating по CoinGecko)"})
    if ds.get("market_cap"):
        if ds.get("fdv") and abs(ds["market_cap"] - ds["fdv"]) <= 0.005 * ds["fdv"]:
            cands.append({"kind": "ds_equals_fdv", "value": ds["market_cap"], "source": "dexscreener", "url": ds.get("url"),
                          "confidence": "not_evidence",
                          "label": "DEX Screener marketCap = FDV → circulating неизвестен, НЕ доказательство MCAP"})
        else:
            cands.append({"kind": "ds_reported", "value": ds["market_cap"], "source": "dexscreener", "url": ds.get("url"),
                          "confidence": "reported", "label": "marketCap DEX Screener"})
    f["mcap_candidates"] = cands
    f["total_supply"] = {"value": total, "source": total_src}
    f["fdv_total"] = {"value": (price * total) if price and total else None,
                      "label": "FDV по выпущенному предложению (totalSupply × цена)", "supply_source": total_src}
    ms = (f.get("max_supply") or {}).get("value")
    f["fdv_max"] = {"value": (price * ms) if price and ms else None,
                    "label": "FDV по максимальному предложению", "supply_source": (f.get("max_supply") or {}).get("source")}
    evid = [c for c in cands if c["confidence"] in ("confirmed", "reported")]
    if len(evid) >= 2:
        vals = [c["value"] for c in evid]
        lo, hi = min(vals), max(vals)
        if lo > 0 and (hi / lo - 1) * 100 > float(s["mcap_conflict_pct"]):
            note = "; ".join(f"{c['label']}: {c['value']:,.0f}" for c in evid)
            db.add_conflict(uid, "circulating_mcap", evid, f"Расхождение MCAP {((hi/lo-1)*100):.0f}%: {note}")
            f["mcap_conflict"] = {"spread_pct": (hi / lo - 1) * 100, "values": evid, "at": now_iso()}
        else:
            f.pop("mcap_conflict", None)
