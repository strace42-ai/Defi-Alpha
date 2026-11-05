"""Fundamental Momentum (2.1, этап 1): улучшается ли экономика протокола быстрее его рыночной оценки.

Источники — только уже накопленные данные (сетевых запросов нет):
- дневные ряды DefiLlama из сводки протокола (facts.protocol): TVL, комиссии, выручка, выручка держателей, объём DEX;
- дневной ряд TVL сети (chain_health) и изменения комиссий/объёмов сети за 30 дн. по DefiLlama;
- дневные свечи самого ликвидного пула токена (GeckoTerminal OHLCV) и история оценок MCAP (mcap_assessments).

Правила:
- динамика считается только между двумя ТОЧНО совпадающими дневными точками ряда источника (без интерполяции);
  суммы за окно — только при наличии всех дней обоих окон; иначе — «недостаточно данных»;
- рост долларового TVL не трактуется как чистый приток капитала (TVL меняется и от цен активов внутри);
- MCAP сравнивается только между двумя подтверждёнными оценками одного типа; иначе используется изменение цены с оговоркой;
- результат — событие для исследования, а не рекомендация.
"""
from __future__ import annotations

import statistics
from typing import Any

from . import config, db
from .chains import CHAINS
from .util import loads

VERSION = "momentum-1.2"
DAY = 86400
WINDOWS = (7, 30, 90)
VERDICTS = {"improving": "улучшается", "stable": "стабильно", "deteriorating": "ухудшается", "insufficient": "недостаточно данных"}


def candidates() -> list[str]:
    return [r["uid"] for r in db.q("SELECT uid FROM tokens WHERE protocol_slug IS NOT NULL")]


def load(uid: str) -> tuple[dict, dict]:
    tok = db.q1("SELECT uid, chain, symbol, protocol_slug, facts FROM tokens WHERE uid=?", (uid,))
    if not tok:
        raise ValueError(f"токен не найден: {uid}")
    f = loads(tok["facts"], {}) or {}
    p = f.get("protocol") or {}
    ch = loads(db.get_progress(f"chain_health:{tok['chain']}"), {}) or {}
    mc = db.rows_to_dicts(db.q("SELECT id, assessed_at, mcap_type, value, method FROM mcap_assessments WHERE token_uid=? ORDER BY id", (uid,)))
    meth = db.rows_to_dicts(db.q("SELECT metric, value_text, fetched_at FROM observations WHERE entity_type='protocol' AND entity_id=? "
                                 "AND metric LIKE '%_methodology' ORDER BY id", (p.get("slug") or "",))) if p.get("slug") else []
    ph = f.get("price_history") or {}
    pool = _main_pool(f)
    data = {"tok": dict(tok) if tok else {}, "p": p, "chain": ch, "mcap_hist": mc, "meth": meth, "mcap": f.get("mcap") or {},
            "price": ph.get(pool) if pool else None, "pool": pool}

    def last(series):
        return series[-1] if series else None
    snap = {"slug": p.get("slug"), "protocol_at": p.get("at"), "stale": p.get("stale"),
            "tvl_last": last(p.get("tvl_series")), "tvl_n": len(p.get("tvl_series") or []),
            **{f"{k}_last": last((p.get(k) or {}).get("chart")) for k in ("fees", "revenue", "holders_revenue", "dex_volume")},
            "chain_at": ch.get("at"), "chain_tvl_last": last(ch.get("tvl_series")),
            "mcap_ids": [m["id"] for m in mc][-3:], "meth_n": len(meth), "pool": pool, "price_last": last(data["price"])}
    return snap, data


def _main_pool(f: dict) -> str | None:
    """Пул с наибольшей ликвидностью среди пулов, по которым есть дневные свечи."""
    ph = f.get("price_history") or {}
    items = [x for x in ((f.get("pools") or {}).get("items") or []) if x.get("address") in ph]
    if not items:
        return next(iter(ph), None)
    return max(items, key=lambda x: x.get("liquidity_usd") or 0)["address"]


# ------------------------------------------------------------------ ряды
def _daily(series) -> dict[int, float]:
    """Только дневные точки источника (00:00 UTC); внутридневная «текущая» точка не используется для сравнения."""
    out = {}
    for x in series or []:
        try:
            t, v = int(x[0]), x[1]
        except (TypeError, ValueError, IndexError):
            continue
        if v is None or t % DAY:
            continue
        out[t] = float(v)
    return out


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b <= 0:
        return None
    return (a / b - 1) * 100


def _point_change(d: dict[int, float], w: int, end: int | None = None) -> dict:
    if not d:
        return {"value": None, "status": "unknown", "reason": "ряд отсутствует"}
    t1 = end if end is not None else max(d)
    t0 = t1 - w * DAY
    if t1 not in d or t0 not in d:
        return {"value": None, "status": "unknown", "reason": f"нет точки ряда на {w} дн. раньше — недостаточно истории"}
    return {"value": _pct(d[t1], d[t0]), "status": "reported", "from": t0, "to": t1, "v0": d[t0], "v1": d[t1]}


def _window_sums(d: dict[int, float], w: int) -> dict:
    """Сумма за последние w дней и за предыдущие w дней — только при наличии ВСЕХ дней обоих окон."""
    if not d:
        return {"cur": None, "prev": None, "change": None, "status": "unknown", "reason": "ряд отсутствует"}
    t1 = max(d)
    cur_days = [t1 - i * DAY for i in range(w)]
    prev_days = [t1 - (w + i) * DAY for i in range(w)]
    out: dict[str, Any] = {"window": w, "to": t1}
    out["cur"] = sum(d[t] for t in cur_days) if all(t in d for t in cur_days) else None
    out["prev"] = sum(d[t] for t in prev_days) if all(t in d for t in prev_days) else None
    out["change"] = _pct(out["cur"], out["prev"])
    out["status"] = "reported" if out["change"] is not None else "unknown"
    if out["change"] is None:
        out["reason"] = "нет двух полных сопоставимых периодов — недостаточно данных"
    return out


def _avg(d: dict[int, float], t1: int, w: int) -> float | None:
    days = [t1 - i * DAY for i in range(w)]
    vals = [d[t] for t in days if t in d]
    return sum(vals) / len(vals) if len(vals) == w else None


# ------------------------------------------------------------------ расчёт
def compute(uid: str, data: dict) -> dict:
    s = config.load_settings().get("analytics") or {}
    tok, p, ch = data["tok"], data["p"], data["chain"]
    chain = CHAINS.get(tok.get("chain"))
    res: dict[str, Any] = {"method_version": VERSION, "slug": p.get("slug") or tok.get("protocol_slug"), "name": p.get("name"),
                           "symbol": tok.get("symbol"), "category": p.get("category"), "flags": [], "explanation": [], "sources": []}
    flag = lambda key, text, sev="warn": res["flags"].append({"key": key, "text": text, "severity": sev})
    if not p.get("slug"):
        res.update(status="no_data", verdict="insufficient", confidence="low",
                   summary={"verdict": "insufficient", "note": "метрики протокола ещё не получены"})
        res["explanation"].append("Связь с протоколом подтверждена, но метрики DefiLlama ещё не собраны основным сканером.")
        return res
    res["sources"] += [{"source": "DefiLlama", "url": p.get("verify_url") or f"https://defillama.com/protocol/{p['slug']}",
                        "fetched_at": p.get("at")}]
    if p.get("stale"):
        flag("stale", f"метрики протокола устарели: последняя попытка обновления не удалась ({p.get('last_error')})")
    tvl = _daily(p.get("tvl_series"))
    ctvl = _daily(ch.get("tvl_series"))
    m: dict[str, Any] = {"tvl_usd": {"value": p.get("tvl_usd"), "status": "reported" if p.get("tvl_usd") is not None else "unknown"}}
    single_chain = bool(chain and chain.llama and (p.get("chains") or []) == [chain.llama])
    if not single_chain:
        flag("multichain", "протокол работает в нескольких сетях (или сеть не сопоставлена): ряд TVL — по всем сетям, "
                           "сравнение с одной сетью и доля в сети не вычисляются", "info")
    for w in WINDOWS:
        pc = _point_change(tvl, w)
        cc = _point_change(ctvl, w, end=pc.get("to")) if pc.get("to") else {"value": None, "status": "unknown"}
        rel = None
        if single_chain and pc["value"] is not None and cc["value"] is not None:
            rel = ((1 + pc["value"] / 100) / (1 + cc["value"] / 100) - 1) * 100
        share = None
        if single_chain and pc["value"] is not None and cc["value"] is not None and cc["v0"] and cc["v1"]:
            share = {"from_pct": pc["v0"] / cc["v0"] * 100, "to_pct": pc["v1"] / cc["v1"] * 100}
            share["change_pp"] = share["to_pct"] - share["from_pct"]
        m[f"tvl_{w}d"] = {**pc, "chain_change": cc.get("value"), "relative_to_chain": rel, "share_of_chain": share,
                          "status": pc["status"] if pc["value"] is not None else "unknown"}
    # ускорение: изменение за последние 30 дн. против предыдущих 30 дн.
    if tvl:
        t1 = max(tvl)
        a = _point_change(tvl, 30, end=t1)
        b = _point_change(tvl, 30, end=t1 - 30 * DAY)
        m["tvl_acceleration_30d"] = ({"value": a["value"] - b["value"], "status": "calculated", "recent": a["value"], "previous": b["value"],
                                      "note": "п.п.: изменение за последние 30 дн. минус изменение за предыдущие 30 дн."}
                                     if a["value"] is not None and b["value"] is not None else
                                     {"value": None, "status": "unknown", "reason": "нужно ≥ 60 дней истории TVL"})
    # комиссии, выручка, объём
    series = {}
    for k in ("fees", "revenue", "holders_revenue", "dex_volume"):
        x = p.get(k) or {}
        if x.get("available") is False:
            m[k] = {"available": False, "status": "unknown", "note": "у DefiLlama нет данных этого типа (это не ноль)"}
            continue
        if x.get("available") is None and not x.get("chart"):
            m[k] = {"available": None, "status": "unknown", "note": "данные не получены (ошибка источника)"}
            continue
        d = _daily(x.get("chart"))
        series[k] = d
        m[k] = {"available": True, "stale": bool(x.get("stale")), "w7": _window_sums(d, 7), "w30": _window_sums(d, 30),
                "methodology": (x.get("methodology") or {}) if k != "dex_volume" else None}
    # структура потоков по определениям DefiLlama: fees = всё, что платят пользователи; revenue — протоколу; holders — держателям
    if "fees" in series and "revenue" in series:
        fl = {}
        for w in (7, 30):
            fs, rs = m["fees"][f"w{w}"]["cur"], m["revenue"][f"w{w}"]["cur"]
            hs = (m.get("holders_revenue") or {}).get(f"w{w}", {}).get("cur") if "holders_revenue" in series else None
            if fs is not None and rs is not None:
                consistent = rs <= fs and (hs is None or hs <= rs)
                fl[f"w{w}"] = {"gross_fees": fs, "supply_side_lp": (fs - rs) if rs <= fs else None, "protocol_revenue": rs,
                               "holders_revenue": hs, "treasury_other": (rs - hs) if hs is not None and consistent else None,
                               "consistent": consistent}
                if not consistent and w == 30:
                    flag("flows", f"данные DefiLlama несогласованы за {w} дн. (holders revenue {hs:,.0f} > revenue {rs:,.0f} "
                                  "или revenue > fees): разделение на казначейство/держателей не выводится; вероятно, "
                                  "у протокола разные методики для разных типов данных", "info")
        m["flows"] = {**fl, "status": "calculated", "note": "по определениям DefiLlama: LP/поставщики = fees − revenue; "
                      "казначейство и прочее = revenue − holders revenue. Методика DefiLlama приводится ниже."}
    # эффективность капитала
    if tvl and "fees" in series:
        t1 = max(series["fees"])
        f30 = m["fees"]["w30"]["cur"]
        f30p = m["fees"]["w30"]["prev"]
        a_now, a_prev = _avg(tvl, t1, 30), _avg(tvl, t1 - 30 * DAY, 30)
        cur = f30 * 365 / 30 / a_now * 100 if f30 is not None and a_now else None
        prv = f30p * 365 / 30 / a_prev * 100 if f30p is not None and a_prev else None
        m["fees_to_tvl"] = {"annual_pct": cur, "prev_annual_pct": prv, "change_pp": (cur - prv) if cur is not None and prv is not None else None,
                            "status": "calculated" if cur is not None else "unknown",
                            "note": "комиссии за 30 дн. в годовом выражении к среднему TVL тех же 30 дней"}
    if tvl and "dex_volume" in series:
        t1 = max(series["dex_volume"])
        v30 = m["dex_volume"]["w30"]["cur"]
        a_now = _avg(tvl, t1, 30)
        turn = v30 / 30 / a_now if v30 is not None and a_now else None
        m["volume_to_tvl"] = {"per_day": turn, "status": "calculated" if turn is not None else "unknown"}
        if turn is not None and turn > float(s.get("wash_turnover_per_day", 3.0)):
            flag("wash", f"объём в сутки ≈ {turn:.1f}× TVL — очень высокая оборачиваемость: возможна накрутка объёма "
                         "или торговля ради стимулов; рост объёма не считать доказанным ростом спроса")
    # сеть: комиссии и объём (DefiLlama change_30dover30d)
    chain_fees, chain_vol = ch.get("fees_change_30d"), ch.get("dex_volume_change_30d")
    m["chain"] = {"name": chain.llama if chain else None, "tvl_usd": ch.get("tvl_usd"), "fees_change_30d": chain_fees,
                  "dex_volume_change_30d": chain_vol, "fetched_at": ch.get("at"), "status": "reported" if ch else "unknown"}
    if "dex_volume" in series and ch.get("dex_volume_30d") and chain_vol is not None and single_chain:
        v = m["dex_volume"]["w30"]
        prev_chain = ch["dex_volume_30d"] / (1 + chain_vol / 100) if chain_vol > -100 else None
        if v["cur"] is not None and v["prev"] is not None and prev_chain:
            m["dex_share"] = {"to_pct": v["cur"] / ch["dex_volume_30d"] * 100, "from_pct": v["prev"] / prev_chain * 100,
                              "status": "calculated",
                              "note": "доля в объёме DEX сети; объём сети за прошлый период выведен из изменения DefiLlama"}
            m["dex_share"]["change_pp"] = m["dex_share"]["to_pct"] - m["dex_share"]["from_pct"]
    if p.get("category_share"):
        cs = p["category_share"]
        m["competitors"] = {"share_pct": cs.get("share_pct"), "rank": cs.get("rank"), "of": cs.get("of"), "top": cs.get("top"),
                            "status": "reported", "note": "текущая доля TVL в категории в этой сети; динамика конкурентов не собирается"}
    if chain:
        res["sources"].append({"source": "DefiLlama (сеть)", "url": f"https://defillama.com/chain/{(chain.llama or '').replace(' ', '%20')}",
                               "fetched_at": ch.get("at")})
    # смена методики DefiLlama
    hashes: dict[str, list] = {}
    for o in data["meth"]:
        hashes.setdefault(o["metric"], []).append(o)
    for metric, obs in hashes.items():
        vals = [o["value_text"] for o in obs if o["value_text"]]
        changes = [obs[i]["fetched_at"] for i in range(1, len(obs)) if obs[i]["value_text"] and obs[i]["value_text"] != obs[i - 1]["value_text"]]
        if changes:
            flag("methodology", f"DefiLlama изменил методику ({metric.replace('_methodology', '')}) {changes[-1][:10]}: "
                                "сравнение периодов до и после может быть несопоставимо")
        _ = vals
    # рынок
    price = _daily([[x[0], x[1]] for x in (data.get("price") or [])])
    mk: dict[str, Any] = {"pool": data.get("pool")}
    for w in (7, 30):
        mk[f"price_{w}d"] = _point_change(price, w)
    mc = data["mcap"]
    mk["mcap_now"] = {"value": mc.get("value"), "type": mc.get("type"), "low": mc.get("low"), "high": mc.get("high"),
                      "status": {"CONFIRMED": "confirmed", "ESTIMATED": "estimated"}.get(mc.get("type"), "unknown")}
    hist = [h for h in data["mcap_hist"] if h["mcap_type"] == "CONFIRMED" and h["value"]]
    for w in (7, 30):
        mk[f"mcap_{w}d"] = {"value": None, "status": "unknown",
                            "reason": "нет двух подтверждённых оценок MCAP одного метода с интервалом " + str(w) + " дн."}
        if len(hist) >= 2:
            from .util import parse_iso
            last = hist[-1]
            t_last = parse_iso(last["assessed_at"])
            older = [h for h in hist if parse_iso(h["assessed_at"]) and (t_last - parse_iso(h["assessed_at"])).total_seconds() >= w * DAY
                     and h["method"] == last["method"]]
            if older:
                o = older[-1]
                mk[f"mcap_{w}d"] = {"value": _pct(last["value"], o["value"]), "status": "calculated", "from": o["assessed_at"], "to": last["assessed_at"]}
    if mc.get("type") == "CONFIRMED" and mc.get("value"):
        for k in ("revenue", "holders_revenue", "fees"):
            c30 = ((m.get(k) or {}).get("w30") or {}).get("cur")
            if c30 is not None:
                mk[f"{k}_annual_to_mcap_pct"] = c30 * 365 / 30 / mc["value"] * 100
        mk["ratio_note"] = ("сопоставимы с капитализацией выручка протокола и держателей; gross fees включают долю LP "
                            "и не являются доходом держателей токена — отношение fees/MCAP справочное")
    elif mc.get("value"):
        mk["ratio_note"] = f"MCAP типа {mc.get('type')} — отношения комиссий к MCAP не вычисляются как подтверждённые"
    if data.get("pool"):
        res["sources"].append({"source": "GeckoTerminal OHLCV", "pool": data["pool"]})
    res["metrics"], res["market"] = m, mk
    # ---- вывод по окнам
    def pick(w):
        vals = {}
        t = m.get(f"tvl_{w}d") or {}
        if t.get("relative_to_chain") is not None:
            vals["tvl_vs_chain"] = t["relative_to_chain"]
        elif t.get("value") is not None:
            vals["tvl"] = t["value"]
        for k in ("fees", "revenue", "dex_volume"):
            ws = (m.get(k) or {}).get(f"w{w}") if w in (7, 30) else None
            if ws and ws.get("change") is not None:
                vals[k] = ws["change"]
        return vals
    windows = {}
    for w in (30, 7):
        vals = pick(w)
        if "fees" in vals and "revenue" in vals and abs(vals["fees"] - vals["revenue"]) < 0.5:
            vals.pop("revenue")
        score = sum(1 if v > 10 else -1 if v < -10 else 0 for v in vals.values())
        if len(vals) < 2:
            verdict = "insufficient"
        elif score >= 2 or (score >= 1 and len(vals) == 2 and all(v > 10 for v in vals.values())):
            verdict = "improving"
        elif score <= -2 or (score <= -1 and len(vals) == 2 and all(v < -10 for v in vals.values())):
            verdict = "deteriorating"
        else:
            verdict = "stable"
        mkt = mk.get(f"mcap_{w}d", {}).get("value")
        mkt_kind = "mcap"
        if mkt is None:
            mkt = mk.get(f"price_{w}d", {}).get("value")
            mkt_kind = "price"
        windows[w] = {"values": vals, "verdict": verdict, "market": mkt, "market_kind": mkt_kind}
    main_w = 30 if windows[30]["verdict"] != "insufficient" else 7
    res["windows"] = windows
    res["verdict"] = windows[main_w]["verdict"]
    res["window"] = main_w if res["verdict"] != "insufficient" else None
    # ---- расхождение экономики и оценки
    res["divergence"] = None
    for w in (main_w,):  # только в окне, по которому сделан вывод (иначе ★ за 7 дн. противоречил бы выводу за 30 дн.)
        wv = windows[w]
        ev_ = {k: v for k, v in wv["values"].items() if k in ("fees", "revenue", "tvl_vs_chain", "tvl")}
        if "fees" in ev_ and "revenue" in ev_ and abs(ev_["fees"] - ev_["revenue"]) < 0.5:
            ev_.pop("revenue")  # у части протоколов выручка = комиссии (один ряд) — не считать дважды
        econ_vals = list(ev_.values())
        # событие — только если экономика в целом улучшается (а не один показатель на фоне падения TVL)
        if wv["verdict"] == "improving" and len(econ_vals) >= 2 and wv["market"] is not None:
            econ = statistics.median(econ_vals)
            gap = econ - wv["market"]
            if econ >= float(s.get("divergence_min_econ_pct", 20)) and gap >= float(s.get("divergence_min_gap_pp", 20)):
                res["divergence"] = {
                    "window": w, "econ_median_pct": econ, "market_pct": wv["market"], "market_kind": wv["market_kind"], "gap_pp": gap,
                    "values": {k: round(v, 1) for k, v in wv["values"].items()} | {wv["market_kind"]: round(wv["market"], 1)},
                    "text": (f"за {w} дн.: медиана показателей экономики {econ:+.0f}% против "
                             f"{'MCAP' if wv['market_kind'] == 'mcap' else 'цены токена'} {wv['market']:+.0f}%"
                             + ("" if wv["market_kind"] == "mcap" else " (изменение circulating supply не учтено)"))}
                break
    # ---- достоверность и оговорки
    if res["verdict"] == "insufficient":
        flag("history", "недостаточно истории: нет двух сопоставимых периодов хотя бы для двух показателей", "info")
    if windows[main_w]["market"] is None:
        flag("market", f"изменение рыночной оценки за {main_w} дн. неизвестно (нет свечей/подтверждённых оценок MCAP на начало периода)", "info")
    elif windows[main_w]["market_kind"] == "price":
        flag("circ", "рыночная оценка — по цене токена: изменение circulating supply за период не подтверждено", "info")
    flag("tvl_usd", "TVL в долларах меняется и от цен активов внутри протокола — рост TVL не доказывает чистый приток капитала", "info")
    n_metrics = len(windows[main_w]["values"])
    warn = [x for x in res["flags"] if x["severity"] == "warn"]
    res["confidence"] = ("low" if res["verdict"] == "insufficient" or len(warn) >= 2 or n_metrics < 2 else
                         "high" if n_metrics >= 3 and not warn else "medium")
    res["status"] = "ok"
    ex = res["explanation"]
    w = windows[main_w]
    names = {"tvl_vs_chain": "TVL относительно сети", "tvl": "TVL", "fees": "комиссии", "revenue": "выручка", "dex_volume": "объём DEX"}
    if res["verdict"] == "insufficient":
        ex.append("Вывод не делается: нет двух сопоставимых периодов для минимум двух показателей.")
    else:
        ex.append(f"Окно {main_w} дн.: " + "; ".join(f"{names[k]} {v:+.1f}%" for k, v in w["values"].items()) + ".")
        ex.append(f"Итог: {VERDICTS[res['verdict']]} (каждый показатель > +10% даёт +1, < −10% даёт −1).")
    if res["divergence"]:
        ex.append("Экономика улучшается быстрее рыночной оценки: " + res["divergence"]["text"] + ". Это повод для исследования, не рекомендация.")
    res["summary"] = {
        "slug": res["slug"], "name": res["name"], "verdict": res["verdict"], "window": res["window"], "confidence": res["confidence"],
        "tvl_usd": p.get("tvl_usd"), "tvl_30d": (m.get("tvl_30d") or {}).get("value"), "tvl_vs_chain_30d": (m.get("tvl_30d") or {}).get("relative_to_chain"),
        "tvl_7d": (m.get("tvl_7d") or {}).get("value"),
        "fees_30d": ((m.get("fees") or {}).get("w30") or {}).get("cur"), "fees_30d_change": ((m.get("fees") or {}).get("w30") or {}).get("change"),
        "fees_7d_change": ((m.get("fees") or {}).get("w7") or {}).get("change"),
        "revenue_30d_change": ((m.get("revenue") or {}).get("w30") or {}).get("change"),
        "volume_30d_change": ((m.get("dex_volume") or {}).get("w30") or {}).get("change"),
        "price_7d": mk["price_7d"].get("value"), "price_30d": mk["price_30d"].get("value"),
        "mcap": mc.get("value"), "mcap_type": mc.get("type"), "divergence": bool(res["divergence"]),
        "flags": [x["key"] for x in res["flags"] if x["severity"] == "warn"],
    }
    return res
