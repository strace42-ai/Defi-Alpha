"""Шесть инвестиционных фильтров. Чистые функции: вход — факты с источниками, выход — решение
с причиной и доказательствами. Все решения воспроизводимы (одинаковые факты → одинаковый результат).

Статусы: PASS / FAIL / UNKNOWN. confidence: confirmed | preliminary | estimated.
UNKNOWN ≠ FAIL: такие кандидаты попадают в очередь ручной проверки.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from .util import days_between, fmt_usd, parse_iso

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"

# v2: пять обязательных фильтров. Финансирование (VC) — аналитический показатель риска, НЕ фильтр (см. funding_analysis).
FILTER_NAMES = {
    "age": "Возраст",
    "mcap": "Капитализация (circulating)",
    "drawdown": "Просадка от ATH",
    "utility": "Реальная утилити",
    "ecosystem": "Живая экосистема",
}

FUNDING_CATEGORIES = {
    "vc_fund": "Венчурный фонд (VC)",
    "angel": "Ангельский инвестор",
    "grant": "Грант",
    "public_sale": "Публичная продажа",
    "strategic": "Стратегический партнёр",
    "market_purchase": "Рыночная покупка токенов",
    "ecosystem_fund": "Экосистемное финансирование",
    "none_documented": "Отсутствие внешнего финансирования задокументировано",
    "checked_no_mention": "Проверено: упоминаний нет (НЕ доказательство отсутствия VC)",
}


def R(key: str, status: str, reason: str, *, confidence: str | None = None, evidence: list | None = None,
      needs: list | None = None, values: dict | None = None) -> dict[str, Any]:
    return {"key": key, "name": FILTER_NAMES[key], "status": status, "confidence": confidence,
            "reason": reason, "evidence": evidence or [], "needs": needs or [], "values": values or {}}


# ---------------------------------------------------------------- 1. Возраст
def f_age(facts: dict, manual: list[dict], now: _dt.datetime, max_days: float) -> dict:
    ev = []
    tge = next((m for m in manual if m["fact_key"] == "tge_date" and m.get("value")), None)
    first_trade = (facts.get("ath") or {}).get("first_trade_at")
    earliest = facts.get("earliest_pool") or {}
    first_mint = ((facts.get("onchain") or {}).get("first_mint") or {}).get("ts")
    disc_pool = facts.get("discovery_pool_created_at")
    dates = {
        "tge": parse_iso(tge["value"]) if tge else None,
        "first_trade": parse_iso(first_trade),
        "earliest_pool": parse_iso(earliest.get("at")),
        "first_onchain_mint": parse_iso(first_mint),
    }
    if tge:
        ev.append({"label": "Официальный TGE", "value": tge["value"], "source": tge.get("source_type"), "url": tge.get("evidence_url")})
    if earliest.get("at"):
        ev.append({"label": "Создание самого раннего известного пула", "value": earliest["at"],
                   "source": ",".join(earliest.get("sources") or []), "note": earliest.get("dex")})
    if first_trade:
        ev.append({"label": "Первая сделка (дневная свеча с объёмом)", "value": first_trade, "source": "GeckoTerminal OHLCV",
                   "note": "точность — сутки"})
    if first_mint:
        ev.append({"label": "Первый mint (ончейн)", "value": first_mint, "source": "RPC eth_getLogs"})
    # старейшая из подтверждённых публичных дат = момент первого публичного обращения
    public = [d for k, d in dates.items() if d and k in ("first_trade", "earliest_pool")]
    if dates["tge"]:
        start, basis, conf = dates["tge"], "официальный TGE", "confirmed"
        # если торговля началась раньше TGE — берём более раннюю дату (первое публичное обращение)
        if public and min(public) < start - _dt.timedelta(days=1):
            start, basis, conf = min(public), "первая торговля раньше заявленного TGE", "preliminary"
    elif public:
        start = min(public)
        basis = "первая сделка" if dates["first_trade"] and start == dates["first_trade"] else "создание самого раннего известного пула"
        conf = "preliminary"
    else:
        return R("age", UNKNOWN, "Нет ни даты TGE, ни данных о пулах/сделках", evidence=ev,
                 needs=["Найти дату TGE или первой сделки"])
    age = days_between(start, now)
    vals = {"age_days": round(age, 1), "start": start.isoformat(), "basis": basis}
    # ончейн-mint намного раньше торгов → контракт старше пулов; возможно, ранние пулы вне покрытия
    warn = None
    if dates["first_onchain_mint"] and dates["first_onchain_mint"] < start - _dt.timedelta(days=30):
        warn = (f"Первый mint ({dates['first_onchain_mint'].date()}) на {days_between(dates['first_onchain_mint'], start):.0f} дн. "
                "раньше первых известных торгов — проверьте, нет ли ранних пулов вне покрытия")
    if disc_pool and earliest.get("at") and disc_pool > earliest["at"] and days_between(parse_iso(earliest["at"]), parse_iso(disc_pool)) > 7:
        ev.append({"label": "Пул, через который обнаружен токен", "value": disc_pool,
                   "note": "это НОВЫЙ ПУЛ уже торгующегося токена — возраст считается по самому раннему пулу"})
    proto = facts.get("protocol") or {}
    p_dates = [d for d in (parse_iso(proto.get("first_tvl_at")), parse_iso(proto.get("listed_at")),
                           parse_iso(proto.get("family_first_listed_at"))) if d]
    p_start = min(p_dates) if p_dates else None
    if p_start:
        vals["protocol_age_days"] = round(days_between(p_start, now), 1)
        ev.append({"label": "Возраст протокола (первый TVL/листинг DefiLlama)", "value": p_start.date().isoformat(),
                   "source": "defillama", "note": f"{vals['protocol_age_days']:.0f} дн."})
    young_token_old_project = bool(p_start and age <= max_days and days_between(p_start, start) > 180)
    if age > max_days:
        return R("age", FAIL, f"Возраст {age:.0f} дн. > {max_days:.0f} дн. (основа: {basis}, {start.date()})",
                 confidence="confirmed" if conf == "confirmed" else "preliminary", evidence=ev, values=vals)
    if warn:
        return R("age", UNKNOWN, f"Возраст по торгам {age:.0f} дн., но {warn}", evidence=ev, values=vals,
                 needs=["Проверить дату TGE и ранние пулы"])
    note = "" if conf == "confirmed" else " — ПРЕДВАРИТЕЛЬНО: TGE не подтверждён"
    if young_token_old_project:
        note += (f". ВНИМАНИЕ: токен новый, но протокол работает с {p_start.date()} — возможна миграция/новый токен "
                 "зрелого проекта")
    return R("age", PASS, f"Возраст {age:.0f} дн. ≤ {max_days:.0f} (основа: {basis}, {start.date()}){note}",
             confidence=conf, evidence=ev, values=vals,
             needs=([] if conf == "confirmed" else ["Подтвердить дату TGE по официальным источникам"])
             + (["Проверить, не является ли токен миграцией/заменой токена зрелого проекта"] if young_token_old_project else []))


# ---------------------------------------------------------------- 2. MCAP
def f_mcap(facts: dict, limit: float, manual: list[dict] | None = None) -> dict:
    """Решение по оценке MCAP Intelligence Engine (app/mcap.py)."""
    from . import mcap as M
    a = facts.get("mcap") if isinstance(facts.get("mcap"), dict) and "points" in facts.get("mcap", {}) else M.assess(facts, manual)
    status, conf, reason, needs = M.filter_decision(a, limit)
    ev = []
    for p in a["points"]:
        ev.append({"label": f"{p['kind']}: {p['formula']}", "value": p["mcap"], "source": p["source"], "url": p.get("url"),
                   "confidence": p["confidence"], "at": p.get("at"), "note": p.get("note")})
    for u in a["upper_bounds"]:
        ev.append({"label": f"верхняя граница ({u['source']}): {u['formula']}", "value": u["mcap"], "note": u["note"]})
    for c in a["conflicts"]:
        ev.append({"label": f"КОНФЛИКТ ({c['kind']})", "value": f"{c.get('spread_pct') or 0:.0f}%", "note": c.get("note") or ""})
    vals = {"limit": limit, "mcap_type": a["type"], "value": a["value"], "low": a["low"], "high": a["high"],
            "issued_value": a["issued_value"], "fdv_total": a["issued_value"], "fdv_max": (facts.get("fdv_max") or {}).get("value"),
            "mcap_low": a["low"], "mcap_high": a["high"]}
    return R("mcap", status, reason, confidence=conf, evidence=ev, values=vals, needs=needs)


def funding_analysis(facts: dict, manual: list[dict], mcap_value: float | None) -> dict:
    """Финансирование — аналитический показатель риска (не фильтр): раунды, оценки, аллокации, разлоки, давление продаж."""
    rounds = []
    for m in manual:
        if m.get("fact_key") != "funding":
            continue
        rounds.append({"category": m.get("category"), "category_name": FUNDING_CATEGORIES.get(m.get("category"), m.get("category")),
                       "value": m.get("value"), "amount_usd": m.get("amount_usd"), "valuation_usd": m.get("valuation_usd"),
                       "source": m.get("source_type"), "url": m.get("evidence_url"), "note": m.get("note")})
    for r in ((facts.get("protocol") or {}).get("raises")) or []:
        amt = r.get("amount")
        rounds.append({"category": "vc_fund" if (r.get("leadInvestors") or r.get("otherInvestors")) else "unknown",
                       "category_name": f"Раунд {r.get('round') or ''} (DefiLlama)",
                       "value": ", ".join((r.get("leadInvestors") or []) + (r.get("otherInvestors") or [])),
                       "amount_usd": float(amt) * 1e6 if amt else None, "valuation_usd": (float(r["valuation"]) * 1e6) if r.get("valuation") else None,
                       "source": "defillama", "url": r.get("source"), "date": r.get("date")})
    raised = sum(r["amount_usd"] or 0 for r in rounds if r["category"] in ("vc_fund", "angel", "strategic"))
    tn_unlocks = []
    from .enrich import current_tokenomics  # noqa: F401  (параметры токеномики приходят через facts)
    for u in (facts.get("tokenomics_unlocks") or []):
        tn_unlocks.append(u)
    risks = []
    if raised and mcap_value:
        ratio = raised / mcap_value
        if ratio >= 1:
            risks.append(f"Привлечено ${raised:,.0f} при MCAP ${mcap_value:,.0f} ({ratio:.1f}×): инвесторы могут быть в убытке "
                         "или иметь стимул фиксировать прибыль — серьёзный фактор давления продаж")
        elif ratio >= 0.3:
            risks.append(f"Привлечено ${raised:,.0f} — {ratio*100:.0f}% от MCAP: заметное потенциальное давление продаж при разлоках")
    inv_unlocks = [u for u in tn_unlocks if "invest" in str(u.get("category") or u.get("note") or "").lower()]
    if inv_unlocks:
        risks.append(f"Запланированы разлоки инвесторов: {len(inv_unlocks)}")
    status = "no_data" if not rounds else ("vc_present" if any(r["category"] == "vc_fund" for r in rounds) else "non_vc_funding")
    return {"status": status, "rounds": rounds, "raised_usd": raised or None, "risks": risks, "investor_unlocks": inv_unlocks,
            "note": "Финансирование не исключает проект: это аналитика рисков. Отсутствие данных ≠ отсутствие VC."}


# ---------------------------------------------------------------- 3. Просадка
def f_drawdown(facts: dict, max_dd: float) -> dict:
    ath = facts.get("ath") or {}
    price = (facts.get("price") or {}).get("value")
    a = ath.get("ath_usd")
    if a is None or price is None:
        return R("drawdown", UNKNOWN, "Нет истории цен (OHLCV) или текущей цены", needs=["Получить OHLCV качественных пулов"])
    dd = max(0.0, (1 - price / a) * 100)
    ev = [{"label": "ATH (без аномальных хвостов)", "value": a, "at": ath.get("ath_at"), "source": "GeckoTerminal OHLCV (день)",
           "note": f"пул {ath.get('ath_pool')}"},
          {"label": "Текущая цена", "value": price, "source": (facts.get("price") or {}).get("source"),
           "note": (facts.get("price") or {}).get("method")}]
    if ath.get("ath_raw_high_usd") and ath["ath_raw_high_usd"] != a:
        ev.append({"label": "Максимум свечи с учётом аномального хвоста (не используется)", "value": ath["ath_raw_high_usd"],
                   "at": ath.get("ath_raw_high_at")})
    if ath.get("ath_close_usd"):
        ev.append({"label": "Максимальное дневное закрытие", "value": ath["ath_close_usd"], "at": ath.get("ath_close_at")})
    # полнота истории: первые свечи против самого раннего известного пула
    earliest = parse_iso((facts.get("earliest_pool") or {}).get("at"))
    first_c = parse_iso(ath.get("first_candle_at"))
    complete = bool(earliest and first_c and (first_c - earliest).total_seconds() <= 2 * 86400)
    vals = {"drawdown_pct": round(dd, 2), "limit": max_dd, "ath": a, "price": price, "history_complete": complete}
    warn = "" if complete else " История свечей может не покрывать начало торгов: истинный ATH может быть выше."
    if facts.get("ath_pool_quality_warning"):
        warn += " " + facts["ath_pool_quality_warning"] + "."
    if ath.get("stale"):
        warn += f" ATH из прежнего расчёта: последнее обновление свечей не удалось ({ath.get('last_attempt_at')}: {ath.get('last_error')})."
    if ath.get("retained_reason"):
        warn += " " + ath["retained_reason"] + "."
    if dd > max_dd:
        # наблюдаемая просадка — нижняя граница истинной, поэтому FAIL надёжен даже при неполной истории
        return R("drawdown", FAIL, f"Просадка {dd:.1f}% > {max_dd:.0f}%", confidence="confirmed" if not facts.get("ath_pool_quality_warning") else "preliminary",
                 evidence=ev, values=vals)
    return R("drawdown", PASS, f"Просадка {dd:.1f}% ≤ {max_dd:.0f}%." + warn,
             confidence="confirmed" if complete and not facts.get("ath_pool_quality_warning") else "preliminary",
             evidence=ev, values=vals, needs=[] if complete else ["Проверить ранние пулы и ATH вручную"])


# ---------------------------------------------------------------- 5. Утилити
def f_utility(facts: dict, manual: list[dict], asset_class: str | None) -> dict:
    link = (facts.get("protocol_link") or {}).get("linked")
    proto = facts.get("protocol") or {}
    links = facts.get("links") or {}
    gi = facts.get("gt_info") or {}
    websites = sorted(set((links.get("websites") or []) + (gi.get("websites") or [])))
    socials = links.get("socials") or []
    ev = []
    if asset_class == "tokenized_stock":
        return R("utility", FAIL, "Токенизированная акция/фонд (актив эмитента, определено по названию), а не токен DeFi-протокола",
                 confidence="preliminary")
    man = next((m for m in manual if m["fact_key"] == "utility"), None)
    if link:
        ev.append({"label": "Связь с протоколом", "value": f"{link['name']} ({link.get('category')})",
                   "source": link["method"], "url": link.get("url"), "note": link.get("evidence")})
    if websites:
        ev.append({"label": "Сайты (недоверенные данные профиля)", "value": ", ".join(websites[:5])})
    if man:
        ev.append({"label": "Ручная оценка утилити", "value": man.get("value"), "url": man.get("evidence_url"), "note": man.get("note")})
        if man.get("value") in ("FAIL", "PASS"):
            return R("utility", man["value"], f"Ручная оценка: {man.get('note') or ''}", confidence="confirmed", evidence=ev)
    risks = []
    if link and proto:
        tvl = proto.get("tvl_usd")
        fees = (proto.get("fees") or {})
        cat = proto.get("category") or link.get("category")
        active = (tvl or 0) > 0 or (fees.get("total30d") or 0) > 0
        hr = proto.get("holders_revenue") or {}
        if not hr.get("available"):
            risks.append("нет данных о выплатах держателям: захват ценности токеном не подтверждён")
        elif not hr.get("total30d"):
            risks.append("holders revenue за 30 дн. = 0: токен может не захватывать ценность продукта")
        if active:
            vals = {"tvl": tvl, "category": cat, "value_capture_risks": risks}
            return R("utility", PASS, f"Подтверждённая связь с работающим протоколом «{link['name']}» ({cat}): "
                     f"TVL {fmt_usd(tvl)}, комиссии 30д {fmt_usd(fees.get('total30d')) if fees.get('available') else 'н/д'}",
                     confidence="confirmed" if link["method"] in ("defillama_address", "manual") else "preliminary",
                     evidence=ev, values=vals,
                     needs=["Проверить, захватывает ли токен экономическую ценность"] if risks else [])
        return R("utility", UNKNOWN, f"Протокол «{link['name']}» найден, но активности не видно (TVL/комиссии = 0 или н/д)",
                 evidence=ev, needs=["Проверить работу продукта"])
    if link and not proto:
        return R("utility", UNKNOWN, "Связь с протоколом установлена, метрики ещё не получены", evidence=ev)
    if not websites and not socials:
        # Отсутствие сайта/соцсетей/листинга — НЕ доказательство отсутствия продукта (новый проект может быть ещё
        # не проиндексирован). FAIL — только при независимом доказательстве (ручная оценка, токенизированная акция).
        return R("utility", UNKNOWN, "Нет листинга DeFi-протокола, сайта и соцсетей в профилях DEX Screener/GeckoTerminal. "
                 "Это не доказывает отсутствие продукта (проект может быть ещё не проиндексирован) — нужна ручная проверка",
                 evidence=ev,
                 values={"no_public_traces": True,
                         "checked": ["DefiLlama address/gecko_id", "DEX Screener info", "GeckoTerminal info" if gi else "GT info не запрашивался"]},
                 needs=["Найти официальный сайт/документацию/контракты продукта по адресу токена; при отсутствии продукта — "
                        "внести ручную оценку утилити FAIL с доказательством"])
    possible = (facts.get("protocol_link") or {}).get("possible") or []
    for p in possible:
        ev.append({"label": "Возможный протокол (только совпадение тикера!)", "value": p["name"], "note": p["note"]})
    return R("utility", UNKNOWN, "Есть сайт/соцсети, но связь токена с работающим DeFi-продуктом не подтверждена "
             "(сайт сам по себе не доказывает продукт)", evidence=ev,
             needs=["Проверить на официальном сайте/в документации адрес токена и наличие работающего продукта"])


# ---------------------------------------------------------------- 6. Экосистема
def f_ecosystem(health: dict | None, s: dict) -> dict:
    if not health or health.get("tvl_usd") is None:
        return R("ecosystem", UNKNOWN, "Нет данных DefiLlama о сети", needs=["Обновить данные сети"])
    tvl = health.get("tvl_usd")
    ch90 = health.get("tvl_change_90d")
    st90 = health.get("stables_change_90d")
    vol = health.get("dex_volume_30d")
    ev = [{"label": "TVL сети", "value": tvl, "source": "defillama"},
          {"label": "TVL 30д / 90д, %", "value": f"{_p(health.get('tvl_change_30d'))} / {_p(ch90)}", "source": "defillama"},
          {"label": "Стейблкоины, $ (30д/90д)", "value": f"{fmt_usd(health.get('stables_usd'))} ({_p(health.get('stables_change_30d'))} / {_p(st90)})",
           "source": "defillama stablecoins"},
          {"label": "DEX-объём 30д", "value": vol, "source": "defillama", "note": f"изм. {_p(health.get('dex_volume_change_30d'))}"},
          {"label": "Комиссии сети 30д", "value": health.get("fees_30d"), "source": "defillama"}]
    neg = []
    if tvl < s["chain_min_tvl_usd"]:
        neg.append(f"TVL {fmt_usd(tvl)} < {fmt_usd(s['chain_min_tvl_usd'])}")
    if ch90 is not None and ch90 < s["chain_max_tvl_decline_90d_pct"]:
        neg.append(f"TVL за 90д {ch90:.0f}%")
    if st90 is not None and st90 < s["chain_max_stable_decline_90d_pct"]:
        neg.append(f"стейблкоины за 90д {st90:.0f}%")
    if vol is not None and vol < s["chain_min_dex_volume_30d_usd"]:
        neg.append(f"DEX-объём 30д {fmt_usd(vol)}")
    growth = (health.get("tvl_change_30d") or 0) > 10 or (health.get("stables_change_30d") or 0) > 10
    # проверенность активности: три независимые группы метрик
    groups = {
        "динамика TVL 30/90д": health.get("tvl_change_30d") is not None or ch90 is not None,
        "стейблкоины": health.get("stables_usd") is not None or st90 is not None or health.get("stables_change_30d") is not None,
        "экономическая активность (DEX-объём/комиссии)": vol is not None or health.get("fees_30d") is not None,
    }
    have = [k for k, v in groups.items() if v]
    missing = [k for k, v in groups.items() if not v]
    vals = {"negatives": neg, "growth": growth, "metric_groups_checked": have, "metric_groups_missing": missing}
    if len(neg) >= 2 and not growth:
        return R("ecosystem", FAIL, "Признаки заброшенной сети: " + "; ".join(neg), confidence="confirmed", evidence=ev, values=vals)
    activity_ok = groups["экономическая активность (DEX-объём/комиссии)"]
    trend_ok = groups["динамика TVL 30/90д"]
    if not (activity_ok and trend_ok):
        return R("ecosystem", UNKNOWN, f"Недостаточно метрик для оценки активности сети (TVL {fmt_usd(tvl)}); "
                 f"нет данных: {', '.join(missing)}", evidence=ev, values=vals,
                 needs=["Получить динамику TVL и DEX-объём/комиссии сети или оценить сеть вручную"])
    if neg:
        return R("ecosystem", PASS if growth else UNKNOWN,
                 ("Небольшая сеть с подтверждённым ростом: " if growth else "Смешанные сигналы: ") + "; ".join(neg),
                 confidence="preliminary", evidence=ev, values=vals,
                 needs=[] if growth else ["Оценить динамику сети вручную"])
    conf = "confirmed" if not missing else "preliminary"
    return R("ecosystem", PASS, f"Сеть активна: TVL {fmt_usd(tvl)}, DEX-объём 30д {fmt_usd(vol)}"
             + (f" (не проверено: {', '.join(missing)})" if missing else ""), confidence=conf, evidence=ev, values=vals)


def _p(v: float | None) -> str:
    return "н/д" if v is None else f"{v:+.0f}%"


def evaluate_all(facts: dict, manual: list[dict], health: dict | None, settings: dict, now: _dt.datetime,
                 asset_class: str | None) -> dict[str, dict]:
    fs = settings["filters"]
    return {
        "age": f_age(facts, manual, now, float(fs["max_age_days"])),
        "mcap": f_mcap(facts, float(fs["max_circ_mcap_usd"]), manual),
        "drawdown": f_drawdown(facts, float(fs["max_drawdown_pct"])),
        "utility": f_utility(facts, manual, asset_class),
        "ecosystem": f_ecosystem(health, fs),
    }
