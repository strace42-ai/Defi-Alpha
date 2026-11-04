"""Многоступенчатый отбор A→E, итоговый статус, скоринг, сигналы. Все переходы журналируются."""
from __future__ import annotations

import datetime as _dt
from typing import Any

from . import alerts, config, db, filters, scoring
from .enrich import get_facts, save_facts
from .fundamentals import get_chain_health
from .util import dumps, loads, now_iso, now_utc, parse_iso

LEVELS = {"A": "Обнаружен", "B": "Предварительно подходящий", "C": "DeFi-кандидат", "D": "Фундаментальная проверка",
          "E": "Список наблюдения"}


def manual_facts(uid: str) -> list[dict]:
    return db.rows_to_dicts(db.q("SELECT * FROM manual_facts WHERE token_uid=? AND active=1 ORDER BY id", (uid,)))


def market_minimum(facts: dict, s: dict) -> tuple[bool | None, str]:
    mm = s["market_minimum"]
    liqs = [x for x in ((facts.get("pools") or {}).get("liquidity_quality_usd"),
                        (facts.get("gt") or {}).get("liquidity_usd"),
                        (facts.get("ds") or {}).get("liquidity_usd")) if x is not None]
    pool_vol = sum((p.get("volume_h24") or 0) for p in (facts.get("pools") or {}).get("items") or [] if p.get("quality")) or None
    vols = [x for x in ((facts.get("gt") or {}).get("volume_h24"), (facts.get("ds") or {}).get("volume_h24"), pool_vol) if x is not None]
    if not liqs and not vols:
        return None, "нет рыночных данных"
    liq = (facts.get("pools") or {}).get("liquidity_quality_usd")
    if liq is None:
        liq = max(liqs) if liqs else None
    vol = max(vols) if vols else None
    bad = []
    if liq is None or liq < mm["min_liquidity_usd"]:
        bad.append(f"ликвидность {liq if liq is not None else 'н/д'} < {mm['min_liquidity_usd']}")
    if vol is None or vol < mm["min_volume_24h_usd"]:
        bad.append(f"объём 24ч {vol if vol is not None else 'н/д'} < {mm['min_volume_24h_usd']}")
    return (not bad), ("; ".join(bad) if bad else f"ликвидность ≈ ${liq:,.0f}, объём 24ч ≈ ${vol:,.0f}")


def evaluate(uid: str, old_facts: dict | None = None) -> dict:
    s = config.load_settings()
    tok = dict(db.q1("SELECT * FROM tokens WHERE uid=?", (uid,)))
    facts = get_facts(uid)
    health = get_chain_health(tok["chain"])
    if health:
        facts["chain_health"] = {k: health.get(k) for k in ("tvl_usd", "tvl_change_30d", "tvl_change_90d", "stables_usd",
                                                             "stables_change_30d", "dex_volume_30d", "at")}
    if tok["first_seen_at"] and "discovery_pool_created_at" not in facts:
        r = db.q1("SELECT MIN(pool_created_at) m FROM pools WHERE (base_uid=? OR quote_uid=?) AND sources LIKE '%new_pools%'", (uid, uid))
        if r and r["m"]:
            facts["discovery_pool_created_at"] = r["m"]
    man = manual_facts(uid)
    now = now_utc()
    if tok["asset_class"] in ("quote",):
        res = {"level": "A", "status": "FAIL", "reason": "стандартный quote-/обёрнутый нативный актив — не новый проект", "filters": {}}
        _apply(uid, tok, res, facts, {}, None)
        return res
    fr = filters.evaluate_all(facts, man, health, s, now, tok["asset_class"])
    # история решений фильтров
    c = db.get_conn()
    prev = {r["filter_key"]: dict(r) for r in db.q("SELECT * FROM filter_current WHERE token_uid=?", (uid,))}
    for k, r in fr.items():
        p = prev.get(k)
        if not p or p["status"] != r["status"] or p["reason"] != r["reason"]:
            c.execute("INSERT INTO filter_results(token_uid,filter_key,status,confidence,reason,evidence,params,evaluated_at) "
                      "VALUES(?,?,?,?,?,?,?,?)", (uid, k, r["status"], r["confidence"], r["reason"], dumps(r["evidence"]),
                                                  dumps(r.get("values")), now_iso()))
        c.execute("INSERT INTO filter_current(token_uid,filter_key,status,confidence,reason,evidence,evaluated_at) VALUES(?,?,?,?,?,?,?) "
                  "ON CONFLICT(token_uid,filter_key) DO UPDATE SET status=excluded.status, confidence=excluded.confidence, "
                  "reason=excluded.reason, evidence=excluded.evidence, evaluated_at=excluded.evaluated_at",
                  (uid, k, r["status"], r["confidence"], r["reason"], dumps(r["evidence"]), now_iso()))
    c.commit()
    mm_ok, mm_reason = market_minimum(facts, s)
    st = {k: v["status"] for k, v in fr.items()}
    # уровни
    level = "A"
    if mm_ok and all(st[k] != "FAIL" for k in ("age", "mcap", "drawdown")):
        level = "B"
        if st["utility"] == "PASS":
            level = "C"
            if facts.get("protocol") and tok["last_onchain_at"]:
                level = "D"
    sc = scoring.score(facts, fr, man)
    if level == "D" and not any(v == "FAIL" for v in st.values()) and (sc["preliminary"] or 0) >= s["scoring"]["watchlist_min_preliminary"]:
        level = "E"
    # итоговый статус
    fails = [f"{fr[k]['name']}: {fr[k]['reason']}" for k, v in st.items() if v == "FAIL"]
    unknown = [fr[k]["name"] for k, v in st.items() if v == "UNKNOWN"]
    prelim = [fr[k]["name"] for k, v in fr.items() if v["status"] == "PASS" and v.get("confidence") == "preliminary"]
    stale_h = s["alerts"]["stale_hours"]
    lm = parse_iso(tok["last_market_at"])
    if fails:
        status, reason = "FAIL", "; ".join(fails)
    elif mm_ok is False:
        status, reason = "FAIL", f"Рыночный минимум уровня B не выполнен: {mm_reason}"
    elif lm and now - lm > _dt.timedelta(hours=stale_h):
        status, reason = "STALE DATA", f"Рыночные данные старше {stale_h} ч ({tok['last_market_at']})"
    elif mm_ok is None and facts.get("errors"):
        status, reason = "DATA ERROR", "Ошибки получения данных: " + "; ".join(facts["errors"][:2])
    elif all(v == "PASS" for v in st.values()):
        status = "PASS"
        reason = "Все 6 фильтров пройдены" + (f" (предварительно: {', '.join(prelim)})" if prelim else "")
    elif unknown and level in ("B", "C", "D", "E"):
        status, reason = "MANUAL REVIEW", "Требуют ручной проверки: " + ", ".join(unknown)
    else:
        status, reason = "UNKNOWN", ("Недостаточно данных: " + ", ".join(unknown)) if unknown else "Недостаточно данных"
    res = {"level": level, "status": status, "reason": reason, "filters": fr, "score": sc, "market_minimum": [mm_ok, mm_reason]}
    old_filters = {k: {"status": v["status"], "confidence": v["confidence"]} for k, v in prev.items()}
    _apply(uid, tok, res, facts, old_filters, old_facts)
    return res


def _apply(uid: str, tok: dict, res: dict, facts: dict, old_filters: dict, old_facts: dict | None) -> None:
    sc = res.get("score") or {}
    conf = None
    if res.get("filters"):
        cs = [v.get("confidence") for v in res["filters"].values() if v["status"] == "PASS"]
        conf = "confirmed" if cs and all(c == "confirmed" for c in cs) and len(cs) == 6 else ("preliminary" if cs else "low")
    db.record_status_change(uid, "level", tok["level"], res["level"], res["reason"])
    db.record_status_change(uid, "status", tok["status"], res["status"], res["reason"])
    facts["evaluation"] = {"level": res["level"], "status": res["status"], "reason": res["reason"],
                           "market_minimum": res.get("market_minimum"), "at": now_iso()}
    facts["score"] = sc
    db.ex("UPDATE tokens SET level=?, status=?, status_reason=?, score_confirmed=?, score_max_verifiable=?, score_preliminary=?, "
          "confidence=?, facts=?, updated_at=? WHERE uid=?",
          (res["level"], res["status"], res["reason"], sc.get("confirmed"), sc.get("max_verifiable"), sc.get("preliminary"),
           conf, dumps(facts), now_iso(), uid))
    if res.get("filters") and old_facts is not None:
        new_f = {k: {"status": v["status"], "confidence": v["confidence"], "reason": v["reason"], "values": v.get("values"),
                     "evidence": v.get("evidence")} for k, v in res["filters"].items()}
        alerts.check_token(uid, tok["symbol"] or tok["address"][:10], old_facts, facts, old_filters, new_f, tok["level"],
                           res["level"], config.load_settings()["alerts"])
