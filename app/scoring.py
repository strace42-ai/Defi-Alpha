"""100-балльная матрица. Каждый балл объясним; при отсутствии данных балл НЕ присваивается,
а пункт помечается «не проверено» и уменьшает «проверяемый максимум».

confirmed     — сумма баллов за подтверждённые пункты (сопоставимо между проектами только при равном покрытии);
max_verifiable— максимум по пунктам, для которых были данные;
preliminary   — confirmed / max_verifiable × 100 (ПРЕДВАРИТЕЛЬНО, не сопоставимый рейтинг);
coverage_pct  — доля проверенных пунктов в 100 баллах.
Скоринг — не рекомендация к покупке.
"""
from __future__ import annotations

from typing import Any

CRITERIA = [
    ("activity", "Реальная активность и экономика", 20),
    ("value_capture", "Захват ценности токеном", 15),
    ("tokenomics", "Токеномика и размывание", 20),
    ("security", "Безопасность и управление", 15),
    ("competition", "Конкурентоспособность и положение сети", 10),
    ("distribution", "Ончейн-распределение и ликвидность", 10),
    ("team", "Команда и катализаторы", 5),
    ("market", "Рыночная структура", 5),
]


class _C:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, label: str, max_pts: float, cond: bool | None, reason: str):
        """cond=None → данных нет (не проверено)."""
        self.items.append({"label": label, "max": max_pts,
                           "points": (max_pts if cond else 0.0) if cond is not None else 0.0,
                           "status": "unverified" if cond is None else ("awarded" if cond else "not_awarded"),
                           "reason": reason})


def _g(d: Any, *ks, default=None):
    for k in ks:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if d is not None else default


def score(facts: dict, filters: dict[str, dict], manual: list[dict]) -> dict:
    p = facts.get("protocol") or {}
    mk = lambda k: [m for m in manual if m["fact_key"] == k]
    out = {}
    # 1. Активность
    c = _C()
    tvl = p.get("tvl_usd")
    c.add("TVL ≥ $1 млн", 4, None if tvl is None else tvl >= 1e6, f"TVL {tvl:,.0f}" if tvl is not None else "нет данных TVL")
    t30 = p.get("tvl_change_30d")
    c.add("TVL за 30 дн. растёт", 4, None if t30 is None else t30 > 0, f"{t30:+.1f}%" if t30 is not None else "нет истории TVL 30д")
    f30 = _g(p, "fees", "total30d") if _g(p, "fees", "available") else None
    c.add("Комиссии за 30 дн. > 0", 4, None if f30 is None else f30 > 0, f"${f30:,.0f}" if f30 is not None else "нет данных о комиссиях")
    r30 = _g(p, "revenue", "total30d") if _g(p, "revenue", "available") else None
    c.add("Протокольная выручка > 0", 4, None if r30 is None else r30 > 0, f"${r30:,.0f}" if r30 is not None else "нет данных о выручке")
    ftv = p.get("fees_to_tvl_annual_pct")
    util = p.get("utilization_pct")
    if ftv is not None:
        c.add("Эффективность капитала: комиссии/TVL годовых ≥ 10%", 4, ftv >= 10, f"{ftv:.1f}%")
    elif util is not None:
        c.add("Эффективность капитала: утилизация lending ≥ 40%", 4, util >= 40, f"{util:.1f}%")
    else:
        c.add("Эффективность капитала", 4, None, "нет комиссий/утилизации")
    out["activity"] = c.items
    # 2. Захват ценности
    c = _C()
    hr = _g(p, "holders_revenue", "total30d") if _g(p, "holders_revenue", "available") else None
    c.add("Выплаты держателям/локерам > 0", 6, None if hr is None else hr > 0, f"${hr:,.0f} за 30д" if hr is not None else "нет данных holders revenue")
    hm = p.get("holders_revenue_annual_to_mcap_pct")
    c.add("Holders revenue годовых / MCAP ≥ 10%", 5, None if hm is None else hm >= 10, f"{hm:.1f}%" if hm is not None else "нужны holders revenue и circulating MCAP")
    vc_free = mk("value_capture")
    c.add("Ценность для свободного (не заблокированного) держателя", 4,
          None if not vc_free else vc_free[-1].get("value") == "yes",
          (vc_free[-1].get("note") or vc_free[-1].get("value")) if vc_free else "ручная проверка: buyback/burn, выплаты без лока и т.п.")
    out["value_capture"] = c.items
    # 3. Токеномика
    c = _C()
    cands = facts.get("mcap_candidates") or []
    has_circ = any(x["kind"] in ("circulating_official", "circulating_manual", "coingecko_via_gt") for x in cands)
    c.add("Circulating supply установлен из источника", 4, has_circ, "есть" if has_circ else "только FDV/нет данных")
    fm = _g(facts, "fdv_max", "value")
    circ = next((x["value"] for x in cands if x["kind"] in ("circulating_official", "circulating_manual", "coingecko_via_gt")), None)
    ratio = fm / circ if fm and circ else None
    c.add("FDV(max) / MCAP ≤ 5 (умеренное будущее размывание)", 4, None if ratio is None else ratio <= 5,
          f"{ratio:.1f}×" if ratio is not None else "нужны max supply и circulating")
    oc = facts.get("onchain") or {}
    rec = oc.get("reconciliation") or {}
    c.add("История выпуска сходится с totalSupply()", 4, None if not rec or rec.get("matches") is None else bool(rec.get("matches")),
          rec.get("note") or "ончейн-сканирование не выполнялось")
    bc = _g(oc, "analytics", "budget_comparison") or []
    done = [w for w in bc if not w.get("in_progress") and w.get("over_budget") is not None]
    c.add("Фактическая эмиссия не превышает заявленный бюджет", 4, None if not done else not any(w["over_budget"] for w in done),
          f"проверено недель: {len(done)}" if done else "нет версии токеномики с недельными бюджетами или завершённых недель")
    vest = mk("vesting_documented")
    c.add("Вестинг команды документирован", 4, None if not vest else vest[-1].get("value") == "yes",
          (vest[-1].get("note") or "") if vest else "ручная проверка")
    out["tokenomics"] = c.items
    # 4. Безопасность
    c = _C()
    sec = oc.get("security") or {}
    inds = {i["key"]: i for i in sec.get("indicators") or []}
    if sec and not sec.get("error"):
        c.add("Контракт не обновляемый (нет прокси)", 4, "proxy" not in inds and "upgrade" not in inds,
              inds.get("proxy", {}).get("text", "прокси не обнаружен (эвристика)"))
        c.add("Нет pause/blacklist/настраиваемых комиссий", 3, not any(k in inds for k in ("pause", "blacklist", "tax")),
              ", ".join(inds[k]["text"] for k in ("pause", "blacklist", "tax") if k in inds) or "не обнаружено (эвристика)")
        own = inds.get("owner") or inds.get("minter")
        c.add("Права владельца/минтера не у EOA", 4, None if not own else own["status"] != "risk",
              own["text"] if own else "owner()/minter() отсутствуют или не читаются")
    else:
        c.add("Контракт не обновляемый", 4, None, "проверка безопасности не выполнялась")
        c.add("Нет pause/blacklist", 3, None, "проверка безопасности не выполнялась")
        c.add("Права владельца не у EOA", 4, None, "проверка безопасности не выполнялась")
    aud = p.get("audits")
    audm = mk("audit")
    c.add("Аудит подтверждён", 4, (True if audm and audm[-1].get("value") == "yes" else None) if not aud or str(aud) == "0" else True,
          f"DefiLlama audits={aud}" if aud and str(aud) != "0" else ("ручная запись" if audm else "аудит не подтверждён источником"))
    out["security"] = c.items
    # 5. Конкурентоспособность
    c = _C()
    eco = filters.get("ecosystem") or {}
    c.add("Сеть проходит фильтр «живая экосистема»", 4, None if eco.get("status") == "UNKNOWN" else eco.get("status") == "PASS", eco.get("reason", ""))
    cs = p.get("category_share")
    c.add("Доля в категории на сети ≥ 10%", 3, None if not cs else cs["share_pct"] >= 10,
          f"{cs['share_pct']:.1f}% (место {cs['rank']} из {cs['of']})" if cs else "нет данных о категории")
    if t30 is not None and _g(facts, "chain_health", "tvl_change_30d") is not None:
        rel = t30 - facts["chain_health"]["tvl_change_30d"]
        c.add("TVL 30д растёт быстрее сети", 3, rel > 0, f"{rel:+.1f} п.п. к сети")
    else:
        c.add("TVL 30д растёт быстрее сети", 3, None, "нет данных")
    out["competition"] = c.items
    # 6. Распределение и ликвидность
    c = _C()
    liq = _g(facts, "pools", "liquidity_quality_usd")
    mc = circ or next((x["value"] for x in cands if x["confidence"] in ("reported", "confirmed")), None)
    lr = liq / mc * 100 if liq and mc else None
    c.add("Ликвидность качественных пулов ≥ 5% MCAP", 4, None if lr is None else lr >= 5, f"{lr:.1f}%" if lr is not None else "нет MCAP или ликвидности")
    h = _g(facts, "gt_info", "holders") or {}
    top10 = h.get("distribution_percentage", {}).get("top_10") if isinstance(h.get("distribution_percentage"), dict) else None
    try:
        top10 = float(top10) if top10 is not None else None
    except (TypeError, ValueError):
        top10 = None
    c.add("Топ-10 адресов ≤ 50% (с оговоркой: включает контракты локов/пулов)", 3, None if top10 is None else top10 <= 50,
          f"{top10:.1f}% (GeckoTerminal)" if top10 is not None else "нет данных о держателях")
    hc = h.get("count")
    c.add("Держателей ≥ 1000", 3, None if hc is None else int(hc) >= 1000, f"{hc}" if hc is not None else "нет данных")
    out["distribution"] = c.items
    # 7. Команда и катализаторы
    c = _C()
    tm = mk("team")
    c.add("Команда/репутация проверены", 3, None if not tm else tm[-1].get("value") == "yes", (tm[-1].get("note") or "") if tm else "ручная проверка")
    ct = mk("catalyst")
    c.add("Подтверждённые катализаторы", 2, None if not ct else True, (ct[-1].get("note") or "") if ct else "ручная проверка")
    out["team"] = c.items
    # 8. Рыночная структура
    c = _C()
    dd = (filters.get("drawdown") or {}).get("values", {}).get("drawdown_pct")
    c.add("Просадка от ATH 40–86% (не на пике и не уничтожен)", 2, None if dd is None else 40 <= dd <= 86, f"{dd:.0f}%" if dd is not None else "нет ATH")
    vol = _g(facts, "gt", "volume_h24")
    vr = vol / liq if vol is not None and liq else None
    c.add("Объём 24ч / ликвидность 0.05–3 (живой, без аномалий)", 1.5, None if vr is None else 0.05 <= vr <= 3, f"{vr:.2f}" if vr is not None else "нет данных")
    qc = _g(facts, "pools", "quality_count")
    c.add("≥ 2 качественных пула", 1.5, None if qc is None else qc >= 2, f"{qc}" if qc is not None else "пулы не анализировались")
    out["market"] = c.items

    criteria = []
    confirmed = maxv = 0.0
    for key, name, mx in CRITERIA:
        items = out[key]
        aw = sum(i["points"] for i in items)
        chk = sum(i["max"] for i in items if i["status"] != "unverified")
        criteria.append({"key": key, "name": name, "max": mx, "awarded": round(aw, 2), "checked_max": chk,
                         "unverified": [i["label"] for i in items if i["status"] == "unverified"], "items": items})
        confirmed += aw
        maxv += chk
    blocking = []
    for k, fr in filters.items():
        if fr["status"] == "FAIL":
            blocking.append(f"Фильтр «{fr['name']}»: {fr['reason']}")
    for i in (oc.get("security") or {}).get("indicators") or []:
        if i["key"] in ("no_code", "blacklist") and i["status"] == "risk":
            blocking.append(f"Безопасность: {i['text']}")
    if rec and rec.get("matches") is False:  # None = сканирование не завершено
        blocking.append("Предложение не сходится с событиями — модель эмиссии требует ручной проверки")
    coverage = maxv
    prelim = confirmed / maxv * 100 if maxv else None
    unc = "высокая" if coverage < 40 else "средняя" if coverage < 70 else "низкая"
    return {"criteria": criteria, "confirmed": round(confirmed, 1), "max_verifiable": round(maxv, 1),
            "coverage_pct": round(coverage, 1), "preliminary": round(prelim, 1) if prelim is not None else None,
            "uncertainty": unc, "blocking_risks": blocking,
            "disclaimer": "Предварительная оценка с пропусками; не сопоставимый рейтинг и не рекомендация к покупке."}
