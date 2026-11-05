"""ALERT ENGINE. Сигналы формируются сравнением старой и новой сводки фактов.

Каждый сигнал: тип, время, условие срабатывания, исходные значения, источники, ограничения, важность.
Защита от ложных сигналов: при смене методологии DefiLlama сигналы роста/падения комиссий подавляются.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from . import db
from .util import dumps, now_iso, now_utc, parse_iso, pct_change

SEV = {"high": "высокая", "medium": "средняя", "low": "низкая", "info": "инфо"}


def _weekly(chart: list, n: int) -> list[float]:
    """Суммы по последним n полным 7-дневным окнам (старые → новые)."""
    vals = [float(v or 0) for _, v in chart]
    out = []
    for i in range(n, 0, -1):
        seg = vals[len(vals) - i * 7: len(vals) - (i - 1) * 7 or None]
        if len(seg) == 7:
            out.append(sum(seg))
    return out


def _streak(xs: list[float], up: bool) -> int:
    s = 0
    for a, b in zip(xs, xs[1:]):
        if (b > a) if up else (b < a):
            s += 1
        else:
            s = 0
    return s


def emit(uid: str | None, atype: str, sev: str, title: str, condition: str, values: dict, sources: list,
         limitations: str = "", dedup: str | None = None) -> bool:
    key = dedup or f"{uid}:{atype}:{now_iso()[:10]}"
    try:
        cur = db.ex("INSERT OR IGNORE INTO alerts(token_uid,alert_type,severity,title,condition,values_json,sources_json,limitations,"
                    "dedup_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (uid, atype, sev, title, condition, dumps(values), dumps(sources), limitations, key, now_iso()))
        return cur.rowcount == 1  # 0 — такой сигнал уже есть (дедупликация)
    except Exception as e:  # noqa: BLE001 — сигнал не записан; причина — в журнале, вызывающий код продолжает
        db.log_error("alerts.emit", f"{atype} {uid}: {type(e).__name__}: {e}")
        return False


def check_token(uid: str, sym: str, old: dict, new: dict, old_f: dict, new_f: dict, old_level: str | None,
                new_level: str, a: dict) -> int:
    n = 0
    week = now_utc().strftime("%G-W%V")
    # --- обнаружение
    if new_level in ("C", "D", "E") and (old_level in (None, "A", "B")):
        n += emit(uid, "new_candidate", "medium", f"{sym}: новый DeFi-кандидат уровня {new_level}",
                  "токен прошёл предварительный отбор и имеет подтверждённую связь с DeFi-протоколом",
                  {"level": new_level}, ["pipeline"], dedup=f"{uid}:new_candidate")
    oa, na = old_f.get("age") or {}, new_f.get("age") or {}
    if oa.get("confidence") == "preliminary" and na.get("confidence") == "confirmed":
        n += emit(uid, "tge_confirmed", "medium", f"{sym}: подтверждена дата TGE", na.get("reason", ""), na.get("values", {}),
                  ["manual_facts"], dedup=f"{uid}:tge_confirmed")
    om, nm = old_f.get("mcap") or {}, new_f.get("mcap") or {}
    if om.get("status") == "UNKNOWN" and nm.get("status") in ("PASS", "FAIL"):
        n += emit(uid, "mcap_resolved", "medium", f"{sym}: появились данные о капитализации ({nm['status']})", nm.get("reason", ""),
                  nm.get("values", {}), [e.get("source") for e in nm.get("evidence", [])], dedup=f"{uid}:mcap_resolved:{week}")
    # --- фундаментал
    p_old, p_new = old.get("protocol") or {}, new.get("protocol") or {}
    meth_changed = False
    for k in ("fees", "revenue", "holders_revenue"):
        ho, hn = (p_old.get(k) or {}).get("methodology_hash"), (p_new.get(k) or {}).get("methodology_hash")
        if ho and hn and ho != hn:
            meth_changed = True
    if meth_changed:
        n += emit(uid, "methodology_changed", "info", f"{sym}: DefiLlama изменил методологию расчёта комиссий/выручки",
                  "хэш methodology изменился", {}, ["defillama"],
                  "Сигналы роста/падения комиссий за этот период подавлены: изменение может быть методологическим",
                  dedup=f"{uid}:meth:{week}")
    ch = new.get("chain_health") or {}
    t30, c30 = p_new.get("tvl_change_30d"), ch.get("tvl_change_30d")
    if t30 is not None and c30 is not None:
        rel = t30 - c30
        if rel >= a["tvl_outperform_pp"]:
            n += emit(uid, "tvl_outperform", "medium", f"{sym}: TVL растёт быстрее сети (+{rel:.0f} п.п. за 30д)",
                      f"TVL протокола 30д − TVL сети 30д ≥ {a['tvl_outperform_pp']} п.п.", {"protocol_30d": t30, "chain_30d": c30},
                      ["defillama"], dedup=f"{uid}:tvl_out:{week}")
        if rel <= -a["tvl_outperform_pp"]:
            n += emit(uid, "tvl_underperform", "medium", f"{sym}: TVL падает быстрее сети ({rel:.0f} п.п. за 30д)",
                      f"TVL протокола 30д − TVL сети 30д ≤ −{a['tvl_outperform_pp']} п.п.", {"protocol_30d": t30, "chain_30d": c30},
                      ["defillama"], dedup=f"{uid}:tvl_under:{week}")
    if not meth_changed:
        fees = p_new.get("fees") or {}
        t7, p7 = fees.get("total7d"), fees.get("prev7d")
        g = pct_change(t7, p7)
        tv7 = p_new.get("tvl_change_7d")
        if g is not None and g >= a["fees_growth_pct"] and tv7 is not None and tv7 <= a["tvl_flat_pct"]:
            n += emit(uid, "fees_up_tvl_flat", "medium", f"{sym}: комиссии +{g:.0f}% при TVL {tv7:+.0f}% (7д)",
                      f"комиссии 7д к пред. 7д ≥ +{a['fees_growth_pct']}% и TVL 7д ≤ +{a['tvl_flat_pct']}%",
                      {"fees_7d": t7, "fees_prev_7d": p7, "tvl_7d": tv7}, ["defillama"],
                      "Комиссии могут включать стимулированные объёмы", dedup=f"{uid}:fees_up:{week}")
        k = int(a["streak_periods"])
        rev = _weekly((p_new.get("revenue") or {}).get("chart") or [], k + 1)
        if len(rev) == k + 1 and _streak(rev, True) >= k:
            n += emit(uid, "revenue_streak_up", "medium", f"{sym}: выручка растёт {k} недели подряд", "недельные суммы revenue строго растут",
                      {"weekly": rev}, ["defillama"], dedup=f"{uid}:rev_up:{week}")
        fw = _weekly(fees.get("chart") or [], k + 1)
        if len(fw) == k + 1 and _streak(fw, False) >= k:
            n += emit(uid, "fees_decline_streak", "high", f"{sym}: комиссии падают {k} недели подряд", "недельные суммы fees строго падают",
                      {"weekly": fw}, ["defillama"], dedup=f"{uid}:fees_down:{week}")
    # --- эмиссия
    an = ((new.get("onchain") or {}).get("analytics") or {})
    daily = an.get("daily") or []
    if len(daily) >= 14:
        today = now_utc().date()
        def s(d0, d1):
            return sum(x["mint"] for x in daily if d0 <= _dt.date.fromisoformat(x["date"]) < d1)
        last7 = s(today - _dt.timedelta(days=7), today + _dt.timedelta(days=1))
        prev = s(today - _dt.timedelta(days=28), today - _dt.timedelta(days=7)) / 3
        if prev > 0 and last7 > a["emission_spike_x"] * prev:
            n += emit(uid, "emission_spike", "high", f"{sym}: фактическая эмиссия за 7д в {last7/prev:.1f}× выше среднего",
                      f"mint за 7д > {a['emission_spike_x']}× среднего за 3 пред. недели", {"last7": last7, "prev_avg": prev},
                      ["rpc eth_getLogs"], dedup=f"{uid}:emis:{week}")
    for w in an.get("budget_comparison") or []:
        if w.get("over_budget"):
            n += emit(uid, "mint_over_budget", "high", f"{sym}: неделя {w['week']} — mint в контракты эмиссии выше бюджета",
                      "mint на адреса с метками эмиссии > 110% заявленного недельного бюджета", w,
                      ["rpc eth_getLogs", "tokenomics_versions"], "Зависит от полноты меток адресов", dedup=f"{uid}:budget:{w['week']}")
    # --- ликвидность
    lo, ln = (old.get("pools") or {}).get("liquidity_quality_usd"), (new.get("pools") or {}).get("liquidity_quality_usd")
    d = pct_change(ln, lo)
    if d is not None and d <= -a["liquidity_drop_pct"] and (lo or 0) > 5000:
        n += emit(uid, "liquidity_drop", "high", f"{sym}: ликвидность снизилась на {abs(d):.0f}%",
                  f"ликвидность качественных пулов упала ≥ {a['liquidity_drop_pct']}% с прошлого снимка", {"old": lo, "new": ln},
                  ["geckoterminal", "dexscreener"], dedup=f"{uid}:liq:{now_iso()[:10]}")
    # --- конфликт circulating
    if new.get("mcap_conflict") and not old.get("mcap_conflict"):
        n += emit(uid, "supply_conflict", "medium", f"{sym}: расхождение источников MCAP {new['mcap_conflict']['spread_pct']:.0f}%",
                  "разница между источниками circulating MCAP выше порога", new["mcap_conflict"], ["several"],
                  dedup=f"{uid}:conflict:{week}")
    # --- административные роли
    so, sn = ((old.get("onchain") or {}).get("security") or {}), ((new.get("onchain") or {}).get("security") or {})
    for k in ("owner", "minter", "implementation"):
        if so.get(k) and sn.get(k) and so[k] != sn[k]:
            n += emit(uid, "admin_changed", "high", f"{sym}: изменился {k}: {so[k]} → {sn[k]}", f"значение {k} изменилось между снимками",
                      {"old": so[k], "new": sn[k]}, ["rpc"], dedup=f"{uid}:admin:{k}:{sn[k]}")
    # --- unlock
    from .enrich import current_tokenomics
    tn = current_tokenomics(uid)
    for u in (tn or {}).get("params", {}).get("unlocks") or []:
        dt = parse_iso(u.get("date"))
        if dt and now_utc() <= dt <= now_utc() + _dt.timedelta(days=a["unlock_lookahead_days"]):
            n += emit(uid, "unlock_upcoming", "high", f"{sym}: предстоящий unlock {u.get('amount')} ({dt.date()})",
                      f"unlock в ближайшие {a['unlock_lookahead_days']} дн. по версии токеномики {tn.get('version_label')}", u,
                      [tn.get("source_url")], "Дата/объём из документации, не из ончейн-данных", dedup=f"{uid}:unlock:{u.get('date')}")
    return n


def check_sources(stale_hours: float) -> int:
    n = 0
    cutoff = now_utc() - _dt.timedelta(hours=stale_hours)
    for r in db.q("SELECT * FROM source_status"):
        ls = parse_iso(r["last_success_at"])
        if r["last_error_at"] and (not ls or ls < cutoff):
            n += emit(None, "source_stale", "high", f"Источник {r['source']} не обновлялся > {stale_hours:.0f} ч",
                      "нет успешных ответов за период", {"last_success": r["last_success_at"], "last_error": r["last_error"]},
                      [r["source"]], dedup=f"src:{r['source']}:{now_iso()[:10]}")
    return n
