"""MCAP Intelligence Engine — многоступенчатое определение circulating MCAP (чистые функции, без сети).

Уровни данных:
  1. Прямые рыночные источники (CoinGecko с проверкой контракта, GeckoTerminal/DEX Screener market cap).
  2. Circulating supply × устойчивая цена (официальный эндпоинт проекта, ручная запись с доказательством, CoinGecko).
  3. Ончейн-восстановление: totalSupply() − балансы адресов, доказательно НЕ находящихся в обращении
     (метка circulation=excluded с доказательством). Даёт ВЕРХНЮЮ границу обращения при предположении,
     что прочие адреса в обращении; нижнюю границу так установить нельзя.
  4. Оценка: только из подтверждённых частей; недостающая доля обращения не выдумывается.

Результат: тип CONFIRMED / ESTIMATED / UNKNOWN, значение или диапазон, все шаги с источниками и формулами.
Стоимость выпущенного предложения (цена × totalSupply) показывается отдельно и НЕ выдаётся за circulating MCAP.
"""
from __future__ import annotations

from typing import Any

from .util import fmt_usd, median

CONFIRMED, ESTIMATED, UNKNOWN = "CONFIRMED", "ESTIMATED", "UNKNOWN"
CONFLICT_PCT = 15.0


def _pt(source: str, kind: str, mcap: float, confidence: str, *, supply: float | None = None, price: float | None = None,
        formula: str = "", url: str | None = None, at: str | None = None, note: str = "") -> dict:
    return {"source": source, "kind": kind, "mcap": mcap, "confidence": confidence, "supply": supply, "price": price,
            "formula": formula, "url": url, "at": at, "note": note}


def assess(facts: dict, manual: list[dict] | None = None) -> dict:
    manual = manual or []
    steps: list[dict] = []
    pc = facts.get("price_check") or {}
    price = pc.get("value") if pc else None
    if price is None:
        price = (facts.get("price") or {}).get("value")
    # достоверность цены: проверка цены v2 (high/medium/low) либо пометка источника v1
    legacy = {"derived": "medium", "reported": "medium", "estimated": "low", "confirmed": "high"}
    price_conf = pc.get("confidence") or legacy.get((facts.get("price") or {}).get("confidence"), "low")
    price_lo, price_hi = pc.get("low") or price, pc.get("high") or price
    points: list[dict] = []
    uppers: list[dict] = []
    issued = None

    # ---------- уровень 2 (самый сильный): circulating supply × цена
    co = facts.get("circulating_official") or {}
    if co.get("value") is not None and price:
        conf = "confirmed" if co.get("source_type") == "official_docs" else "reported"
        points.append(_pt("project_endpoint", "circulating_official", co["value"] * price, conf, supply=co["value"], price=price,
                          formula=f"{co['value']:,.0f} × ${price:.6g}", url=co.get("url"), at=co.get("at"),
                          note="официальный эндпоинт circulating, документирован проектом" if conf == "confirmed" else "эндпоинт проекта"))
        steps.append({"level": 2, "name": "официальный circulating supply", "result": "получен"})
    else:
        steps.append({"level": 2, "name": "официальный circulating supply", "result": "нет эндпоинта/записи" if price else "нет цены"})
    man = next((m for m in reversed(manual) if m.get("fact_key") == "circulating_supply" and m.get("value")), None)
    if man and price:
        try:
            v = float(str(man["value"]).replace(" ", "").replace(",", ""))
            conf = "confirmed" if man.get("source_type") == "official_docs" and man.get("evidence_url") else "reported"
            points.append(_pt("manual", "circulating_manual", v * price, conf, supply=v, price=price,
                              formula=f"{v:,.0f} × ${price:.6g}", url=man.get("evidence_url"), note=man.get("note") or "ручная запись"))
        except ValueError:
            pass
    # ---------- уровень 1: агрегаторы (CoinGecko — один исходный источник, не учитывается дважды)
    cg = facts.get("coingecko") or {}
    cg_used = False
    if cg.get("verified_contract") and cg.get("circulating_supply") and price:
        cs, ts = cg["circulating_supply"], cg.get("total_supply")
        note = "CoinGecko (контракт подтверждён через platforms)"
        if ts and abs(cs - ts) <= 0.005 * ts:
            note += "; CoinGecko считает всё предложение обращающимся — проектом не подтверждено"
        points.append(_pt("coingecko", "coingecko_circulating", cs * price, "reported", supply=cs, price=price,
                          formula=f"{cs:,.0f} (CoinGecko) × ${price:.6g}", url=cg.get("url"), at=cg.get("last_updated"), note=note))
        cg_used = True
        steps.append({"level": 1, "name": "CoinGecko", "result": "circulating получен"})
    elif cg.get("id") and not cg.get("verified_contract"):
        steps.append({"level": 1, "name": "CoinGecko", "result": "монета найдена, но контракт не совпадает — не используется"})
    else:
        steps.append({"level": 1, "name": "CoinGecko", "result": "нет данных" if not cg.get("error") else f"ошибка: {cg['error']}"})
    gt = facts.get("gt") or {}
    if gt.get("market_cap_usd") and not cg_used:
        if gt.get("fdv_usd") and abs(gt["market_cap_usd"] - gt["fdv_usd"]) <= 0.005 * gt["fdv_usd"]:
            steps.append({"level": 1, "name": "GeckoTerminal market_cap", "result": "равен FDV — не доказательство обращения"})
        else:
            points.append(_pt("geckoterminal", "coingecko_via_gt", gt["market_cap_usd"], "reported", url=gt.get("url"),
                              at=gt.get("at"), formula="market_cap_usd GeckoTerminal", note="circulating по данным CoinGecko"))
            steps.append({"level": 1, "name": "GeckoTerminal market_cap", "result": "получен"})
    ds = facts.get("ds") or {}
    if ds.get("market_cap"):
        if ds.get("fdv") and abs(ds["market_cap"] - ds["fdv"]) <= 0.005 * ds["fdv"]:
            steps.append({"level": 1, "name": "DEX Screener marketCap", "result": "равен FDV — не доказательство обращения"})
        else:
            points.append(_pt("dexscreener", "ds_reported", ds["market_cap"], "reported", url=ds.get("url"), at=ds.get("at"),
                              formula="marketCap DEX Screener"))
    # совместимость: кандидаты, рассчитанные ранее (enrich.compute_caps), если более богатых данных нет
    if not points:
        for c in facts.get("mcap_candidates") or []:
            if c.get("confidence") in ("confirmed", "reported") and c.get("value"):
                points.append(_pt(c.get("source") or c["kind"], c["kind"], c["value"], c["confidence"], url=c.get("url"),
                                  formula=c.get("label") or c["kind"]))
    if price is None and points and not pc:
        price_conf = "medium"  # готовые значения кандидатов рассчитаны upstream по устойчивой цене (enrich.compute_caps)
    # ---------- уровень 3: ончейн — верхние границы
    oc = facts.get("onchain") or {}
    total = oc.get("total_supply") or (facts.get("total_supply") or {}).get("value") or gt.get("total_supply")
    if total and price:
        issued = total * price
        uppers.append({"source": "totalSupply", "mcap": issued, "supply": total,
                       "formula": f"{total:,.0f} (totalSupply) × ${price:.6g}",
                       "note": "стоимость ВЫПУЩЕННОГО предложения: circulating ≤ totalSupply (на этой сети)"})
    elif (facts.get("fdv_total") or {}).get("value"):
        issued = facts["fdv_total"]["value"]
        uppers.append({"source": "fdv_total", "mcap": issued, "formula": "FDV по totalSupply",
                       "note": "стоимость выпущенного предложения — верхняя граница circulating MCAP"})
    circ = oc.get("circulation") or {}
    if circ.get("excluded") and total and price:
        ex = sum(float(e.get("balance") or 0) for e in circ["excluded"])
        up = max(0.0, total - ex)
        uppers.append({"source": "onchain_excluded", "mcap": up * price, "supply": up,
                       "formula": f"({total:,.0f} − {ex:,.0f} в {len(circ['excluded'])} доказательно не обращающихся адресах) × ${price:.6g}",
                       "note": "верхняя граница: прочие адреса считаются обращающимися (не доказано)",
                       "excluded": circ["excluded"]})
        steps.append({"level": 3, "name": "ончейн: totalSupply − не обращающиеся адреса", "result": f"исключено {ex:,.0f}"})
    else:
        steps.append({"level": 3, "name": "ончейн-восстановление", "result": "нет totalSupply" if not total else
                      "нет адресов с доказанной блокировкой (метки circulation=excluded)"})
    upper = min(uppers, key=lambda u: u["mcap"]) if uppers else None

    # ---------- классификация
    conf_pts = [p for p in points if p["confidence"] == "confirmed"]
    rep_pts = [p for p in points if p["confidence"] == "reported"]
    conflicts = []
    if len(points) >= 2:
        lo, hi = min(p["mcap"] for p in points), max(p["mcap"] for p in points)
        if lo > 0 and (hi / lo - 1) * 100 > CONFLICT_PCT:
            conflicts.append({"kind": "sources", "spread_pct": (hi / lo - 1) * 100,
                              "values": [{"source": p["source"], "mcap": p["mcap"], "confidence": p["confidence"]} for p in points]})
    if upper:
        over = [p for p in points if p["mcap"] > upper["mcap"] * 1.005]
        if over:
            conflicts.append({"kind": "exceeds_upper_bound", "upper": upper["mcap"],
                              "values": [{"source": p["source"], "mcap": p["mcap"]} for p in over],
                              "note": "источник указывает обращение больше доказуемой верхней границы — источник ошибочен или устарел"})
    if pc.get("conflict"):
        conflicts.append({"kind": "price", "spread_pct": pc.get("spread_pct"), "note": "цены провайдеров расходятся"})
    if price is None and not points:
        mtype, value, low, high = UNKNOWN, None, None, None
        reason = "цена не установлена"
    elif conf_pts:
        mtype = CONFIRMED if price_conf in ("high", "medium") and not pc.get("conflict") else ESTIMATED
        vs = [p["mcap"] for p in conf_pts]
        value, low, high = median(vs), min(vs), max(vs)
        reason = "circulating подтверждён первоисточником" if mtype == CONFIRMED else \
            "circulating подтверждён, но цена недостаточно надёжна"
    elif rep_pts:
        mtype = ESTIMATED
        vs = [p["mcap"] for p in rep_pts]
        value, low, high = median(vs), min(vs), max(vs)
        reason = "circulating по данным агрегаторов (не подтверждён проектом)"
    elif upper:
        mtype, value, low, high = ESTIMATED, None, None, upper["mcap"]
        reason = "известна только верхняя граница; circulating не установлен — значение не подставляется"
    else:
        mtype, value, low, high = UNKNOWN, None, None, None
        reason = "нет данных о предложении"
    if high is not None and upper and upper["mcap"] < high:
        high = upper["mcap"]
    # с учётом разброса цены провайдеров
    if value is not None and price and pc.get("conflict"):
        low, high = low * (price_lo / price), high * (price_hi / price)
    return {"type": mtype, "value": value, "low": low, "high": high, "reason": reason,
            "price": price, "price_confidence": price_conf, "price_check": pc or None,
            "points": points, "upper_bounds": uppers, "upper": upper, "issued_value": issued,
            "conflicts": conflicts, "steps": steps,
            "circulating_supply": (conf_pts or rep_pts or [{}])[0].get("supply") if (conf_pts or rep_pts) else None,
            "total_supply": total,
            "limitations": _limits(mtype, upper, points)}


def _limits(mtype: str, upper: dict | None, points: list[dict]) -> list[str]:
    out = []
    if mtype != CONFIRMED:
        out.append("circulating supply не подтверждён первоисточником")
    if upper and upper["source"] == "totalSupply":
        out.append("totalSupply на этой сети: для мультичейн-токенов глобальное обращение может отличаться")
    if any(p["source"] in ("dexscreener", "geckoterminal") for p in points):
        out.append("методика агрегатора по circulating неизвестна")
    return out


def filter_decision(a: dict, limit: float) -> tuple[str, str | None, str, list[str]]:
    """(status, confidence, reason, needs) для фильтра MCAP по оценке assess()."""
    pts = [p for p in a["points"] if p["confidence"] in ("confirmed", "reported")]
    up = (a.get("upper") or {}).get("mcap")
    price_conflict = any(c["kind"] == "price" for c in a["conflicts"])
    bad_source = any(c["kind"] == "exceeds_upper_bound" for c in a["conflicts"])
    need_check = ["Сверить circulating supply и цену по первоисточникам; выяснить причину расхождения"]
    if a["price"] is None and not pts:
        return "UNKNOWN", None, "Цена не установлена", ["Получить цену из ликвидных пулов"]
    if pts:
        mlo = min(p["mcap"] for p in pts)
        mhi = max(p["mcap"] for p in pts)
        if price_conflict and a.get("low") is not None:
            mlo, mhi = min(mlo, a["low"]), max(mhi, a["high"])
        below = [p for p in pts if p["mcap"] <= limit]
        above = [p for p in pts if p["mcap"] > limit]
        lab = lambda cs: ", ".join(f"{p['source']} {fmt_usd(p['mcap'])} [{p['confidence']}]" for p in cs)
        if bad_source and up is not None and up <= limit:
            return ("PASS", "preliminary", f"Источник(и) завышают обращение выше доказуемой границы; по верхней границе "
                    f"{fmt_usd(up)} ≤ {fmt_usd(limit)}", need_check)
        if above and below:
            return "UNKNOWN", None, f"Источники по разные стороны лимита {fmt_usd(limit)}: ниже — {lab(below)}; выше — {lab(above)}", need_check
        if price_conflict and mlo <= limit < mhi:
            return "UNKNOWN", None, f"Из-за расхождения цен диапазон MCAP {fmt_usd(mlo)}–{fmt_usd(mhi)} пересекает лимит", need_check
        if not above:
            conf = "confirmed" if (a["type"] == CONFIRMED and any(p["confidence"] == "confirmed" for p in pts)
                                   and not price_conflict) else "preliminary"
            multi = f" (все источники ниже лимита: {len(pts)})" if len(pts) > 1 else ""
            return "PASS", conf, f"{a['type']} MCAP {fmt_usd(mlo)}–{fmt_usd(mhi)} ≤ {fmt_usd(limit)}{multi}", \
                ([] if conf == "confirmed" else ["Подтвердить circulating supply первоисточником"])
        conf = "confirmed" if any(p["confidence"] == "confirmed" for p in pts) else "preliminary"
        multi = f" (все источники выше лимита: {len(pts)})" if len(pts) > 1 else ""
        return "FAIL", conf, f"{a['type']} MCAP {fmt_usd(mlo)}–{fmt_usd(mhi)} > {fmt_usd(limit)}{multi}", []
    if up is not None and up <= limit:
        return ("PASS", "preliminary", f"Circulating не установлен; доказуемая верхняя граница {fmt_usd(up)} ≤ {fmt_usd(limit)} "
                f"({a['upper']['note']})", ["Установить circulating supply"])
    if up is not None:
        return ("UNKNOWN", None, f"Circulating не установлен; стоимость выпущенного предложения {fmt_usd(a['issued_value'])} "
                f"(верхняя граница {fmt_usd(up)}) > лимита, но MCAP НЕ приравнивается к ней",
                ["Найти circulating supply: официальный эндпоинт, токеномика, адреса блокировок"])
    return "UNKNOWN", None, "Нет данных о предложении", ["Получить totalSupply и circulating supply"]
