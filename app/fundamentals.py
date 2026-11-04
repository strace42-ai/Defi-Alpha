"""Фундаментальный модуль: связь токен↔протокол (DefiLlama) и метрики протокола и сети.

Связь подтверждается ТОЛЬКО проверяемым признаком:
  1) поле address протокола в DefiLlama совпадает с chain:address токена;
  2) gecko_id протокола совпадает с coingecko_id токена (CoinGecko указывает этот контракт);
  3) ручная запись с доказательством (manual_facts: protocol_link).
Совпадение названия/тикера — только «возможный кандидат», связь НЕ устанавливается.
"""
from __future__ import annotations

import logging
from typing import Any

from . import db
from .chains import CHAINS
from .discovery import parse_llama_address
from .enrich import get_facts, save_facts
from .http import SourceError
from .sources import defillama
from .util import dumps, fnum, from_ts, iso, loads, now_iso, now_utc, pct_change, sha1

log = logging.getLogger("das.fundamentals")
DEFI_CATEGORIES = {"Dexs", "Lending", "Yield", "Yield Aggregator", "CDP", "Derivatives", "Liquid Staking",
                   "Bridge", "Options", "Synthetics", "RWA", "RWA Lending", "Launchpad", "Algo-Stables",
                   "Indexes", "Liquidity manager", "Leveraged Farming", "Insurance", "Restaking",
                   "Liquid Restaking", "Basis Trading", "Prediction Market", "Dexs Aggregator", "DEX Aggregator",
                   "Farm", "Services", "Uncollateralized Lending", "NFT Lending", "Onchain Capital Allocator",
                   "Perps", "Stablecoin Issuer", "Interface", "Reserve Currency", "Privacy", "Payments",
                   "Staking Pool", "Decentralized Stablecoin", "Treasury Manager", "Managed Token Pools"}


def link_protocol(uid: str) -> dict:
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    if not tok:
        return {}
    chain = CHAINS[tok["chain"]]
    addr = tok["address"]
    res: dict[str, Any] = {"linked": None, "possible": []}
    rows = db.q("SELECT slug,name,category,address,gecko_id,symbol,url,listed_at,chains FROM protocols "
                "WHERE address IS NOT NULL AND (address LIKE ? OR address LIKE ?)", (f"%{addr}%", f"%{tok['address_display'] or addr}%"))
    for r in rows:
        ch, a = parse_llama_address(r["address"])
        if ch and ch.key == chain.key and a == addr:
            res["linked"] = {"slug": r["slug"], "name": r["name"], "category": r["category"], "method": "defillama_address",
                             "evidence": f"DefiLlama /protocols: address = {r['address']}",
                             "url": f"https://defillama.com/protocol/{r['slug']}"}
            break
    if not res["linked"] and tok["coingecko_id"]:
        r = db.q1("SELECT slug,name,category,address FROM protocols WHERE gecko_id=?", (tok["coingecko_id"],))
        if r:
            res["linked"] = {"slug": r["slug"], "name": r["name"], "category": r["category"], "method": "gecko_id",
                             "evidence": f"gecko_id протокола = coingecko_id токена ({tok['coingecko_id']}); "
                                         f"адрес в DefiLlama: {r['address'] or 'не указан'}",
                             "url": f"https://defillama.com/protocol/{r['slug']}"}
    if not res["linked"]:
        m = db.q1("SELECT * FROM manual_facts WHERE token_uid=? AND fact_key='protocol_link' AND active=1 ORDER BY id DESC LIMIT 1", (uid,))
        if m:
            r = db.q1("SELECT slug,name,category FROM protocols WHERE slug=?", (m["value"],))
            res["linked"] = {"slug": m["value"], "name": r["name"] if r else m["value"], "category": r["category"] if r else None,
                             "method": "manual", "evidence": f"ручная запись: {m['note'] or ''} {m['evidence_url'] or ''}".strip(),
                             "url": m["evidence_url"]}
    # возможные кандидаты по совпадению тикера — только для ручной проверки
    if not res["linked"] and tok["symbol"]:
        for r in db.q("SELECT slug,name,category,address FROM protocols WHERE UPPER(symbol)=UPPER(?) LIMIT 5", (tok["symbol"],)):
            res["possible"].append({"slug": r["slug"], "name": r["name"], "category": r["category"],
                                    "address": r["address"],
                                    "note": "совпадение ТОЛЬКО по тикеру — связь не подтверждена"})
    old = tok["protocol_slug"]
    new = (res["linked"] or {}).get("slug")
    db.ex("UPDATE tokens SET protocol_slug=?, protocol_link_evidence=? WHERE uid=?",
          (new, dumps(res["linked"]) if res["linked"] else None, uid))
    db.record_status_change(uid, "protocol_slug", old, new, (res["linked"] or {}).get("evidence", ""))
    return res


def _series_change(series: list[tuple[int, float]], days: int) -> float | None:
    if not series:
        return None
    last_ts, last_v = series[-1]
    target = last_ts - days * 86400
    prev = [v for t, v in series if t <= target]
    if not prev:
        return None
    return pct_change(last_v, prev[-1])


def _sum_last(chart: list, days: int, offset: int = 0) -> float | None:
    if not chart or len(chart) < days + offset:
        return None
    seg = chart[len(chart) - days - offset: len(chart) - offset]
    return float(sum(fnum(v) or 0 for _, v in seg))


def protocol_metrics(slug: str) -> dict:
    out: dict[str, Any] = {"slug": slug, "at": now_iso(), "errors": []}
    try:
        p, _ = defillama.protocol(slug)
    except SourceError as e:
        out["errors"].append(str(e))
        p = None
    if p:
        tvl = [(int(x["date"]), float(x["totalLiquidityUSD"])) for x in p.get("tvl") or [] if x.get("totalLiquidityUSD") is not None]
        cur = p.get("currentChainTvls") or {}
        tvl_now = tvl[-1][1] if tvl else None
        borrowed = sum(v for k, v in cur.items() if k.endswith("borrowed") and "-" not in k) or None
        staking = sum(v for k, v in cur.items() if k == "staking") or None
        out.update({
            "name": p.get("name"), "category": p.get("category"), "chains": p.get("chains"),
            "description": (p.get("description") or "")[:800], "url": p.get("url"),
            "listed_at": iso(from_ts(p.get("listedAt"))), "first_tvl_at": iso(from_ts(tvl[0][0])) if tvl else None,
            "tvl_usd": tvl_now, "tvl_change_7d": _series_change(tvl, 7), "tvl_change_30d": _series_change(tvl, 30),
            "tvl_change_90d": _series_change(tvl, 90), "borrowed_usd": borrowed, "staking_usd": staking,
            "raises": p.get("raises") or [], "audits": p.get("audits"), "audit_links": p.get("audit_links") or [],
            "forked_from": p.get("forkedFromIds") or [], "tags": p.get("tags") or [],
            "tvl_series": [[t, v] for t, v in tvl[-200:]],
            "hacks": p.get("hacks") or [],
            "verify_url": f"https://defillama.com/protocol/{slug}",
        })
        if borrowed and tvl_now:
            out["deposits_usd"] = tvl_now + borrowed
            out["utilization_pct"] = borrowed / (tvl_now + borrowed) * 100
        db.observe("protocol", slug, "tvl_usd", tvl_now, source="defillama", unit="USD", confidence="reported",
                   method="protocol.tvl[-1]", verify_url=out["verify_url"])
        if borrowed:
            db.observe("protocol", slug, "borrowed_usd", borrowed, source="defillama", unit="USD", confidence="reported",
                       method="currentChainTvls.borrowed", verify_url=out["verify_url"])
    for dt, key in (("dailyFees", "fees"), ("dailyRevenue", "revenue"), ("dailyHoldersRevenue", "holders_revenue")):
        try:
            d, _ = defillama.fees_summary(slug, dt)
        except SourceError as e:
            out["errors"].append(str(e))
            d = None
        if not d:
            out[key] = {"available": False, "note": "у DefiLlama нет данных этого типа (это НЕ ноль)"}
            continue
        chart = d.get("totalDataChart") or []
        meth = d.get("methodology") or {}
        out[key] = {"available": True, "total24h": fnum(d.get("total24h")), "total7d": fnum(d.get("total7d")),
                    "total30d": fnum(d.get("total30d")), "all_time": fnum(d.get("totalAllTime")),
                    "prev7d": _sum_last(chart, 7, 7), "chart": chart[-120:],
                    "methodology_hash": sha1(dumps(meth)), "methodology": meth}
        db.observe("protocol", slug, f"{key}_30d", fnum(d.get("total30d")), source="defillama", unit="USD",
                   confidence="reported", method=f"summary/fees dataType={dt} total30d",
                   verify_url=f"https://defillama.com/protocol/{slug}")
        db.observe("protocol", slug, f"{key}_methodology", None, value_text=sha1(dumps(meth)), source="defillama",
                   method="sha1(methodology)")
    try:
        d, _ = defillama.dex_summary(slug)
        if d:
            out["dex_volume"] = {"available": True, "total24h": fnum(d.get("total24h")), "total7d": fnum(d.get("total7d")),
                                 "total30d": fnum(d.get("total30d")), "chart": (d.get("totalDataChart") or [])[-120:]}
        else:
            out["dex_volume"] = {"available": False}
    except SourceError as e:
        out["errors"].append(str(e))
    # производные показатели
    tvl_now = out.get("tvl_usd")
    f30 = (out.get("fees") or {}).get("total30d")
    v30 = (out.get("dex_volume") or {}).get("total30d")
    if tvl_now and f30 is not None:
        out["fees_to_tvl_annual_pct"] = f30 * 365 / 30 / tvl_now * 100
    if tvl_now and v30 is not None:
        out["volume_to_tvl_30d"] = v30 / tvl_now
    if f30 and v30:
        out["effective_fee_rate_pct"] = f30 / v30 * 100
    return out


def category_share(slug: str, category: str | None, chain_llama: str | None) -> dict | None:
    if not category or not chain_llama:
        return None
    try:
        data, _ = defillama.protocols()
    except SourceError:
        return None
    total = 0.0
    mine = None
    rank = []
    for p in data:
        if p.get("category") != category:
            continue
        v = (p.get("chainTvls") or {}).get(chain_llama)
        if v:
            total += v
            rank.append((v, p.get("slug")))
            if p.get("slug") == slug:
                mine = v
    rank.sort(reverse=True)
    if not total or mine is None:
        return None
    pos = [s for _, s in rank].index(slug) + 1
    return {"share_pct": mine / total * 100, "rank": pos, "of": len(rank), "category_tvl": total, "chain": chain_llama,
            "top": [{"slug": s, "tvl": v} for v, s in rank[:5]]}


def ensure_protocols() -> None:
    """Список протоколов DefiLlama нужен для проверяемой связи токен↔протокол; загружается, если пуст или старше суток."""
    r = db.q1("SELECT MAX(fetched_at) m FROM protocols")
    from .util import parse_iso
    last = parse_iso(r["m"]) if r and r["m"] else None
    if last is None or (now_utc() - last).total_seconds() > 86400:
        from .discovery import refresh_protocols
        try:
            refresh_protocols()
        except SourceError as e:
            db.log_error("protocols", str(e))


def update_token_fundamentals(uid: str) -> dict:
    ensure_protocols()
    link = link_protocol(uid)
    f = get_facts(uid)
    f["protocol_link"] = link
    if link.get("linked"):
        slug = link["linked"]["slug"]
        m = protocol_metrics(slug)
        tok = db.q1("SELECT chain FROM tokens WHERE uid=?", (uid,))
        m["category_share"] = category_share(slug, m.get("category"), CHAINS[tok["chain"]].llama)
        # отношение выручки к капитализации (только при известном circulating MCAP)
        mc = best_circ_mcap(f)
        if mc:
            for key in ("revenue", "holders_revenue", "fees"):
                t30 = (m.get(key) or {}).get("total30d")
                if t30 is not None:
                    m[f"{key}_annual_to_mcap_pct"] = t30 * 365 / 30 / mc["value"] * 100
        f["protocol"] = m
    else:
        f.pop("protocol", None)
    save_facts(uid, f, "last_enriched_at")
    return f


def best_circ_mcap(f: dict) -> dict | None:
    for kind in ("circulating_official", "circulating_manual", "coingecko_via_gt", "ds_reported"):
        for c in f.get("mcap_candidates") or []:
            if c["kind"] == kind:
                return c
    return None


# ------------------------------------------------------------------ сеть
def chain_health(chain_key: str) -> dict:
    ch = CHAINS[chain_key]
    out: dict[str, Any] = {"chain": chain_key, "llama": ch.llama, "at": now_iso(), "errors": []}
    if not ch.llama:
        out["errors"].append("нет соответствия сети в DefiLlama")
        return out
    try:
        hist, _ = defillama.chain_tvl_history(ch.llama)
        ser = [(int(x["date"]), float(x["tvl"])) for x in hist if x.get("tvl") is not None]
        if ser:
            out.update({"tvl_usd": ser[-1][1], "tvl_change_30d": _series_change(ser, 30),
                        "tvl_change_90d": _series_change(ser, 90), "tvl_series": [[t, v] for t, v in ser[-200:]]})
    except SourceError as e:
        out["errors"].append(str(e))
    try:
        st, _ = defillama.chain_stablecoins(ch.llama)
        ser = []
        for x in st:
            v = ((x.get("totalCirculatingUSD") or {}).get("peggedUSD"))
            if v is not None:
                ser.append((int(x["date"]), float(v)))
        if ser:
            out.update({"stables_usd": ser[-1][1], "stables_change_30d": _series_change(ser, 30),
                        "stables_change_90d": _series_change(ser, 90)})
    except SourceError as e:
        out["errors"].append(str(e))
    try:
        d, _ = defillama.chain_dex_overview(ch.llama)
        if d:
            out.update({"dex_volume_30d": fnum(d.get("total30d")), "dex_volume_change_30d": fnum(d.get("change_30dover30d"))})
    except SourceError as e:
        out["errors"].append(str(e))
    try:
        d, _ = defillama.chain_fees_overview(ch.llama)
        if d:
            out.update({"fees_30d": fnum(d.get("total30d")), "fees_change_30d": fnum(d.get("change_30dover30d"))})
    except SourceError as e:
        out["errors"].append(str(e))
    for k in ("tvl_usd", "stables_usd", "dex_volume_30d", "fees_30d"):
        db.observe("chain", chain_key, k, out.get(k), source="defillama", unit="USD", confidence="reported",
                   method=f"DefiLlama chain {ch.llama}", verify_url=f"https://defillama.com/chain/{ch.llama.replace(' ', '%20')}")
    db.set_progress(f"chain_health:{chain_key}", dumps(out))
    return out


def get_chain_health(chain_key: str) -> dict | None:
    return loads(db.get_progress(f"chain_health:{chain_key}"), None)
