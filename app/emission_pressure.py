"""Emission Pressure (2.1, этап 1): фактический выпуск токена и его структура — из уже собранных событий mint/burn,
сохранённых версий токеномики и реестра ролей адресов. Сетевых запросов нет; новых ончейн-сканеров нет.

Что считается на этапе 1:
- Genesis: mint ДО запуска (граница — точный блок времени TGE) на адрес с ролью стартовой аллокации.
  Крупный ранний mint на адрес без такой роли — «до запуска, категория не подтверждена» (UNKNOWN), а не Genesis;
- LP-эмиссия: mint после запуска на адрес с ролью gauge / распределитель LP-наград. Mint с неизвестным назначением — UNKNOWN;
- чистый выпуск (Σmint − Σburn) и его сверка с totalSupply; разбивка по эпохам (недельным бюджетам токеномики);
- использование бюджета = LP-эмиссия эпохи / бюджет эпохи — только для завершённой эпохи с точными границами и
  идентифицированными эмиссионными адресами. Без меток — «не проверено» (UNKNOWN); незавершённая эпоха — частичный результат;
- стоимость эмиссии — по цене СВОЕЙ эпохи (VWAP дневных свечей пула) с указанием метода; общие комиссии DefiLlama
  показываются как gross fees и НЕ считаются eligible fees.
Этап 2/3 (claims, ve-блокировки, продажи наград) — явный статус UNKNOWN с причиной; приблизительные оценки не подставляются.
Все суммы токенов — целые минимальные единицы (в JSON — строки); USD — Decimal.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from . import analytics, db
from .analytics import GENESIS_ROLES, LP_ROLES, OTHER_EMISSION_ROLES, ROLES
from .util import loads, now_utc, parse_iso

VERSION = "emission-1.1"  # 2.1.1: полнота классификации для FINAL; точные уровни доказательности
DAY = 86400
CATEGORY_NAMES = {
    "genesis": "Genesis (стартовая аллокация)", "pre_tge_unclassified": "До запуска, категория не подтверждена",
    "lp_emission": "LP-эмиссия (gauge / распределитель наград)", "emission_other": "Прочая эмиссия (назначение не уточнено)",
    "allocation_after_tge": "Аллокация после запуска (вестинг/казначейство)", "mint_to_escrow": "Mint сразу в ve-escrow",
    "system_minter": "Mint на контракт minter (назначение не установлено)", "unknown": "Неизвестное назначение",
}
EVIDENCE_RANK = {"confirmed": 3, "probable": 2, "claimed": 1, "unknown": 0}


def weakest(levels) -> str:
    """Итоговая доказательность группы — по самому слабому элементу."""
    levels = list(levels)
    return min(levels, key=lambda x: EVIDENCE_RANK.get(x, 0)) if levels else "unknown"


STAGE2 = "этап 2: ограниченный индексатор событий claim / ve-escrow для выбранных протоколов ещё не реализован"
STAGE3 = "этап 3: трассировка наград и подтверждённых swap-событий выполняется только после проверки этапов 1–2"


def candidates() -> list[str]:
    return [r["token_uid"] for r in db.q("SELECT DISTINCT token_uid FROM supply_events")]


def load(uid: str) -> tuple[dict, dict]:
    tok = db.q1("SELECT uid, chain, symbol, decimals, facts FROM tokens WHERE uid=?", (uid,))
    if not tok:
        raise ValueError(f"токен не найден: {uid}")
    f = loads(tok["facts"], {}) or {}
    agg = db.q1("SELECT COUNT(*) n, MIN(block) lo, MAX(block) hi, SUM(COALESCE(orphaned,0)) orph FROM supply_events WHERE token_uid=?", (uid,))
    tn = [dict(r) for r in db.q("SELECT id, version_label, published_at, is_current, params, source_url, superseded_by FROM tokenomics_versions "
                                "WHERE token_uid=? ORDER BY id", (uid,))]
    for v in tn:
        v["params"] = loads(v["params"], {}) or {}
    wb = loads(db.get_progress(f"week_blocks:{uid}"), {}) or {}
    tge = db.q1("SELECT value, evidence_url, note FROM manual_facts WHERE token_uid=? AND fact_key='tge_date' AND active=1 ORDER BY id DESC LIMIT 1", (uid,))
    circ = db.rows_to_dicts(db.q("SELECT id, value, source, fetched_at, method FROM observations WHERE entity_type='token' AND entity_id=? "
                                 "AND metric='circulating_supply' ORDER BY id", (uid,)))
    roles = analytics.roles_for(tok["chain"], uid)
    oc = f.get("onchain") or {}
    ph = f.get("price_history") or {}
    from .momentum import _main_pool
    pool = _main_pool(f)
    fees = {k: ((f.get("protocol") or {}).get(k) or {}).get("chart") for k in ("fees", "revenue", "holders_revenue")}
    data = {"tok": dict(tok), "max_block": agg["hi"], "tokenomics": tn, "week_blocks": wb, "tge": dict(tge) if tge else None,
            "circ": circ, "roles": roles, "onchain": oc, "price": ph.get(pool) if pool else None, "pool": pool, "fees": fees,
            "protocol_slug": (f.get("protocol") or {}).get("slug"),
            "scan_from": db.get_progress(f"mint_scan_from:{uid}"), "scan_to": db.get_progress(f"mint_scan_to:{uid}")}
    rec = oc.get("reconciliation") or {}
    snap = {"events": agg["n"], "min_block": agg["lo"], "max_block": agg["hi"], "orphaned": agg["orph"] or 0,
            "scan_from": data["scan_from"], "scan_to": data["scan_to"], "decimals": tok["decimals"],
            "reconciliation": {k: rec.get(k) for k in ("matches", "at_block", "diff_raw")},
            "tokenomics": [[v["id"], v["is_current"], analytics.sha(v["params"])[:16]] for v in tn],
            "week_blocks": analytics.sha(wb)[:16], "tge": data["tge"]["value"] if data["tge"] else None,
            "roles": analytics.sha({a: [r["role"], r["confidence"], bool(r["conflict"])] for a, r in sorted(roles.items())})[:16],
            "circ_ids": [c["id"] for c in circ][-5:], "pool": pool, "price_last": (data["price"] or [None])[-1],
            "fees_last": {k: (v or [None])[-1] for k, v in fees.items()}}
    return snap, data


# ------------------------------------------------------------------ вспомогательное
def _tok(raw: int, dec: int) -> float:
    return float(Decimal(int(raw)) / (Decimal(10) ** dec))


def _raw(amount: Any, dec: int) -> int | None:
    try:
        return int(Decimal(str(amount)) * (Decimal(10) ** dec))
    except Exception:  # noqa: BLE001
        return None


def _amt(raw: int | None, dec: int) -> dict | None:
    return None if raw is None else {"raw": str(int(raw)), "tokens": _tok(raw, dec)}


def _doc_amounts(params: dict) -> list[tuple[str, Decimal]]:
    """Числа из опубликованной токеномики (для подсказок о совпадении сумм; классификацией НЕ являются)."""
    out = []
    for k, v in params.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out.append((k, Decimal(str(v))))
        elif isinstance(v, str):
            for mm in re.finditer(r"(\d+(?:[.,]\d+)?)\s*M\b", v):
                out.append((k, Decimal(mm.group(1).replace(",", ".")) * Decimal(1_000_000)))
    return out


def stage_totals(records: list[dict]) -> dict:
    """Стадии движения наград — НЕЗАВИСИМЫЕ показатели; новый выпуск = только mint.
    records: {"stage": mint|transfer|claim|sale, "tx": .., "log": .., "amount_raw": ..}.
    Один токен, прошедший mint → distributor → gauge → пользователь, учитывается в выпуске один раз (как mint);
    внутренние переводы между контрактами наград не являются ни выпуском, ни получением; claim и sale не прибавляются к выпуску."""
    seen: set[tuple] = set()
    out = {"issued_raw": 0, "claimed_raw": 0, "sold_raw": 0, "internal_transfers": 0, "duplicates_ignored": 0}
    for r in records:
        key = (r["stage"], r["tx"], int(r["log"]))
        if key in seen:
            out["duplicates_ignored"] += 1
            continue
        seen.add(key)
        v = int(r["amount_raw"])
        if r["stage"] == "mint":
            out["issued_raw"] += v
        elif r["stage"] == "claim":
            out["claimed_raw"] += v
        elif r["stage"] == "sale":
            out["sold_raw"] += v
        else:
            out["internal_transfers"] += 1
    out["new_supply_raw"] = out["issued_raw"]  # claim/sale — не новый выпуск
    return out


def classify(ev: dict, role: dict | None, tge_block: int | None) -> tuple[str, str, str | None]:
    """→ (категория, доказательность, роль). Роль с конфликтом в реестре не применяется."""
    r = role["role"] if role and not role.get("conflict") else None
    conf = (role or {}).get("confidence")
    # уровень доказательности роли переносится КАК ЕСТЬ (без повышения claimed → probable)
    ev_level = conf if conf in EVIDENCE_RANK and conf != "unknown" else "unknown"
    pre = tge_block is not None and ev["block"] < tge_block
    if pre:
        if r in GENESIS_ROLES:
            return "genesis", ev_level, r
        return "pre_tge_unclassified", "unknown", r
    if r in LP_ROLES:
        return "lp_emission", ev_level, r
    if r in OTHER_EMISSION_ROLES:
        return "emission_other", ev_level, r
    if r == "ve_escrow":
        return "mint_to_escrow", ev_level, r
    if r in GENESIS_ROLES:
        return "allocation_after_tge", ev_level, r
    if r == "minter":
        return "system_minter", "unknown", r
    return "unknown", "unknown", r


# ------------------------------------------------------------------ расчёт
def compute(uid: str, data: dict) -> dict:
    tok = data["tok"]
    dec = int(tok["decimals"]) if tok["decimals"] is not None else 18
    A = lambda raw: _amt(raw, dec)
    roles = data["roles"]
    rows = db.rows_to_dicts(db.q("SELECT kind, tx_hash, log_index, block, ts, ts_source, counterparty, amount_raw FROM supply_events "
                                 "WHERE token_uid=? AND COALESCE(orphaned,0)=0 AND block<=? ORDER BY block, log_index",
                                 (uid, data["max_block"] or 0)))
    res: dict[str, Any] = {"method_version": VERSION, "symbol": tok["symbol"], "decimals": dec, "limitations": [], "sources": []}
    lim = res["limitations"].append
    cur_tn = next((v for v in reversed(data["tokenomics"]) if v["is_current"]), None)
    params = (cur_tn or {}).get("params") or {}
    wb = data["week_blocks"]
    # ---- покрытие
    oc = data["onchain"]
    rec = oc.get("reconciliation") or {}
    scan_from, scan_to = int(data["scan_from"] or 0) or None, int(data["scan_to"] or 0) or None
    complete = rec.get("matches") is True
    res["coverage"] = {"scan_from_block": scan_from, "scan_to_block": scan_to, "events": len(rows),
                       "reconciled": complete, "reconciliation": rec,
                       "status": "complete" if complete else "partial",
                       "note": ("Σmint − Σburn точно совпадает с totalSupply() — история выпуска полная до блока сверки"
                                if complete else "история выпуска не сверена с totalSupply() — итоги частичные")}
    res["block_from"], res["block_to"] = scan_from, scan_to
    if not complete:
        lim("история mint/burn не сверена с totalSupply: все итоги — по просканированному диапазону")
    # ---- граница запуска (TGE)
    tge_iso = (data["tge"] or {}).get("value")
    tge_block, tge_src = None, None
    if tge_iso and parse_iso(tge_iso):
        key = tge_iso if tge_iso in wb else next((k for k in wb if parse_iso(k) == parse_iso(tge_iso)), None)
        if key:
            tge_block, tge_src = wb[key]["block"], f"точный первый блок с timestamp ≥ {key} (ручной факт tge_date + время блоков)"
    res["tge"] = {"at": tge_iso, "block": tge_block, "source": tge_src,
                  "status": "confirmed" if tge_block else "unknown",
                  "note": None if tge_block else "граница запуска не подтверждена точным блоком — Genesis не выделяется"}
    if not tge_block:
        lim("без точного блока TGE стартовые аллокации не отделяются от эмиссии")
    # ---- классификация
    cat_raw: dict[str, int] = defaultdict(int)
    cat_n: dict[str, int] = defaultdict(int)
    cat_ev: dict[str, set] = defaultdict(set)
    cat_recips: dict[str, set] = defaultdict(set)
    minted = burned = 0
    n_mint = n_burn = 0
    genesis_items: list[dict] = []
    unknown_by: dict[str, dict] = {}
    docs = _doc_amounts(params)
    classified = []
    for e in rows:
        v = int(e["amount_raw"])
        if e["kind"] == "burn":
            burned += v
            n_burn += 1
            classified.append((e, "burn", "confirmed", None))
            continue
        minted += v
        n_mint += 1
        role = roles.get(e["counterparty"])
        cat, lvl, rname = classify(e, role, tge_block)
        cat_raw[cat] += v
        cat_n[cat] += 1
        cat_ev[cat].add(lvl)
        cat_recips[cat].add(e["counterparty"])
        classified.append((e, cat, lvl, rname))
        if cat in ("genesis", "pre_tge_unclassified"):
            tokens = Decimal(v) / (Decimal(10) ** dec)
            hint = list(dict.fromkeys(f"сумма совпадает с параметром токеномики «{k}» ({d.normalize():f})" for k, d in docs if d == tokens))
            genesis_items.append({"tx": e["tx_hash"], "log_index": e["log_index"], "block": e["block"], "ts": e["ts"], "address": e["counterparty"],
                                  "role": rname, "role_label": (role or {}).get("label"), "role_source": (role or {}).get("source_url"),
                                  "category": cat, "evidence": lvl, "amount": A(v), "doc_hint": hint or None,
                                  "note": None if cat == "genesis" else
                                  "mint до запуска на адрес без подтверждённой роли стартовой аллокации — категория не установлена"
                                  + ("; совпадение суммы с документацией — подсказка, а не доказательство" if hint else "")})
        if cat in ("unknown", "system_minter"):
            u = unknown_by.setdefault(e["counterparty"], {"address": e["counterparty"], "raw": 0, "events": 0, "first_block": e["block"],
                                                          "last_block": e["block"], "sample_tx": e["tx_hash"], "role": rname})
            u["raw"] += v
            u["events"] += 1
            u["last_block"] = e["block"]
    # независимая проверка: Σmint − Σburn до блока сверки должна ТОЧНО равняться totalSupply() на этом блоке
    at_b = rec.get("at_block")
    net_at = (sum(int(e["amount_raw"]) * (1 if e["kind"] == "mint" else -1) for e in rows if e["block"] <= at_b) if at_b else None)
    exact_now = bool(at_b and rec.get("total_supply_raw") and net_at == int(rec["total_supply_raw"]))
    res["totals"] = {"minted": A(minted), "burned": A(burned), "net": A(minted - burned), "mint_events": n_mint, "burn_events": n_burn,
                     "total_supply_rpc": A(int(rec["total_supply_raw"])) if rec.get("total_supply_raw") else None,
                     "reconciled_at_block": at_b, "net_at_reconciled_block": A(net_at) if net_at is not None else None,
                     "reconciled_exactly": exact_now,
                     "after_reconciled_block": A((minted - burned) - net_at) if net_at is not None else None,
                     "status": "confirmed" if exact_now else "partial",
                     "note": ("Σmint − Σburn по событиям Transfer (from/to = 0x0), целые минимальные единицы; до блока сверки — "
                              "точное совпадение с totalSupply(); события после блока сверки будут сверены при следующем ончейн-обновлении"
                              if exact_now else "Σmint − Σburn по событиям; точная сверка с totalSupply() не подтверждена")}
    if complete and not exact_now:
        lim("сверка ончейн-модуля не воспроизводится на текущем наборе событий — итоги помечены как частичные")
    complete = exact_now
    res["coverage"]["reconciled"] = exact_now
    res["coverage"]["status"] = "complete" if exact_now else "partial"
    res["categories"] = [{"key": k, "name": CATEGORY_NAMES[k], "amount": A(cat_raw[k]), "events": cat_n[k], "recipients": len(cat_recips[k]),
                          "share_of_minted_pct": float(Decimal(cat_raw[k]) * 100 / Decimal(minted)) if minted else None,
                          "evidence": weakest(cat_ev[k])}
                         for k in CATEGORY_NAMES if cat_n.get(k)]
    gen_conf = sum(int(g["amount"]["raw"]) for g in genesis_items if g["category"] == "genesis" and g["evidence"] == "confirmed")
    gen_prob = sum(int(g["amount"]["raw"]) for g in genesis_items if g["category"] == "genesis" and g["evidence"] == "probable")
    gen_claim = sum(int(g["amount"]["raw"]) for g in genesis_items if g["category"] == "genesis" and g["evidence"] in ("claimed", "unknown"))
    gen_unk = sum(int(g["amount"]["raw"]) for g in genesis_items if g["category"] == "pre_tge_unclassified")
    res["genesis"] = {"confirmed": A(gen_conf), "probable": A(gen_prob), "claimed": A(gen_claim), "pre_tge_unclassified": A(gen_unk),
                      "items": genesis_items[:50],
                      "status": "unknown" if not tge_block else ("confirmed" if gen_conf and not gen_unk and not gen_prob and not gen_claim else "partial"),
                      "note": "Genesis — только mint до блока TGE на адрес с ролью стартовой аллокации; остальное до запуска — не подтверждено"}
    # ---- LP-эмиссия: какие роли считаются подходящими (из версии токеномики, иначе gauge/распределитель)
    eligible_roles = set()
    for k in (params.get("emission_recipient_kinds") or []):
        eligible_roles.add(analytics.LEGACY_KIND.get(k, k))
    eligible_roles = (eligible_roles & (LP_ROLES | OTHER_EMISSION_ROLES)) or set(LP_ROLES)
    labeled = [a for a, r in roles.items() if r["role"] in eligible_roles and not r.get("conflict")]
    lp_cats = {"lp_emission"} | ({"emission_other"} if "emission_contract" in eligible_roles else set())

    def split_lp(items) -> dict:
        """Разделить mint на: подтверждённую LP-эмиссию; LP по роли без подтверждения; не классифицированные
        (могут оказаться LP-эмиссией); доказанно не-LP (подтверждённая роль другой категории)."""
        o = {"confirmed_lp": 0, "unconfirmed_lp": 0, "unclassified": 0, "proven_non_lp": 0}
        for e, cat, lvl, _ in items:
            if cat == "burn":
                continue
            v = int(e["amount_raw"])
            if cat in lp_cats:
                o["confirmed_lp" if lvl == "confirmed" else "unconfirmed_lp"] += v
            elif cat in ("genesis", "allocation_after_tge", "mint_to_escrow") and lvl == "confirmed":
                o["proven_non_lp"] += v
            else:
                o["unclassified"] += v
        o["complete"] = o["unconfirmed_lp"] == 0 and o["unclassified"] == 0
        return o
    post = [x for x in classified if tge_block is None or x[0]["block"] >= tge_block]
    tot = split_lp(post)
    lp_levels = {lvl for e, cat, lvl, _ in post if cat in lp_cats}
    res["lp_emission"] = {
        "amount": A(tot["confirmed_lp"]) if labeled else None,
        "unconfirmed": A(tot["unconfirmed_lp"]) if labeled else None,
        "unclassified_post_tge": A(tot["unclassified"]),
        "complete": bool(labeled and tot["complete"] and complete),
        "eligible_roles": sorted(eligible_roles), "labeled_addresses": labeled,
        "status": ("unknown" if not labeled else
                   "confirmed" if tot["complete"] and complete and lp_levels <= {"confirmed"} else
                   "partial" if tot["confirmed_lp"] else weakest(lp_levels or {"unknown"})),
        "note": ("адреса эмиссионных контрактов (gauge / распределитель) не идентифицированы — LP-эмиссия не выделяется; "
                 "mint с неизвестным назначением остаются UNKNOWN" if not labeled else
                 None if tot["complete"] else
                 "amount — только подтверждённая LP-эмиссия; часть выпуска после запуска не классифицирована и может также "
                 "относиться к LP-эмиссии — полнота охвата эмиссионных контрактов не доказана")}
    # ---- эпохи
    epochs = []
    prices = {int(x[0]): (Decimal(str(x[1])), Decimal(str(x[2] or 0))) for x in (data.get("price") or []) if x and x[1] is not None}
    feeser = {k: {int(x[0]): Decimal(str(x[1])) for x in (v or []) if x and x[1] is not None} for k, v in data["fees"].items()}
    now = now_utc()
    for w in params.get("weekly_budgets") or []:
        st, en = parse_iso(w.get("start")), None
        if not st:
            continue
        en = st + _dt.timedelta(days=7)
        b0 = (wb.get(w["start"]) or wb.get(st.strftime("%Y-%m-%dT%H:%M:%SZ")) or {}).get("block")
        b1 = (wb.get(en.strftime("%Y-%m-%dT%H:%M:%SZ")) or {}).get("block")
        in_progress = en > now
        state = "unconfirmed" if b0 is None or (b1 is None and not in_progress) else ("in_progress" if in_progress else "complete")
        ep: dict[str, Any] = {"week": w.get("week"), "start": w.get("start"), "end": en.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "start_block": b0, "end_block": b1, "state": state, "budget": A(_raw(w.get("amount"), dec)) if w.get("amount") is not None else None,
                              "tokenomics_version": (cur_tn or {}).get("version_label")}
        if state != "unconfirmed":
            sel = [x for x in classified if x[0]["block"] >= b0 and (b1 is None or x[0]["block"] < b1)]
            by = defaultdict(int)
            for e, cat, _, _ in sel:
                by[cat] += int(e["amount_raw"])
            m_all = sum(v for k, v in by.items() if k != "burn")
            ep.update({"minted_all": A(m_all), "burned": A(by.get("burn", 0)), "net": A(m_all - by.get("burn", 0)),
                       "by_category": {k: A(v) for k, v in by.items() if k != "burn"},
                       "unknown": A(by.get("unknown", 0) + by.get("system_minter", 0)),
                       "covered_to_block": scan_to, "coverage_complete": bool(scan_to and (b1 is None or scan_to >= b1))})
            sp = split_lp(sel)
            lp = sp["confirmed_lp"] + sp["unconfirmed_lp"]
            budget = int(ep["budget"]["raw"]) if ep["budget"] else None
            pct = (lambda raw: float(Decimal(raw) * 100 / Decimal(budget))) if budget else (lambda raw: None)
            bounds = {"confirmed_lp": A(sp["confirmed_lp"]), "unconfirmed_lp": A(sp["unconfirmed_lp"]), "unclassified": A(sp["unclassified"]),
                      "classification_complete": sp["complete"], "confirmed_pct": pct(sp["confirmed_lp"]),
                      "upper_bound_pct": pct(sp["confirmed_lp"] + sp["unconfirmed_lp"] + sp["unclassified"])}
            if not labeled:
                util = {"status": "unknown", "classification_complete": False,
                        "reason": "не проверено: эмиссионные адреса не идентифицированы (нужны роли gauge / распределитель)"}
            elif budget is None:
                util = {"status": "unknown", "reason": "бюджет эпохи не указан в версии токеномики", **bounds}
            elif not ep["coverage_complete"]:
                util = {"status": "partial", "final": False, "reason": "события эпохи просканированы не полностью", **bounds}
            elif in_progress:
                util = {"status": "partial", "final": False, **bounds,
                        "reason": "эпоха не завершена — частичный результат, не сравнивать с завершёнными эпохами"}
            elif not sp["complete"]:
                util = {"status": "partial", "final": False, **bounds,
                        "reason": "полнота классификации не доказана: подтверждено не менее confirmed_pct; часть mint эпохи "
                                  "не классифицирована или LP-роль не подтверждена — полная доля бюджета неизвестна"}
            else:
                util = {"status": "final", "final": True, **bounds, "value_pct": pct(sp["confirmed_lp"]),
                        "remaining": A(budget - sp["confirmed_lp"]),
                        "basis": "каждый mint эпохи доказанно отнесён к LP-эмиссии или к другой подтверждённой категории"}
            ep["lp_emission"] = A(lp) if labeled else None
            ep["utilization"] = util
            ep["all_mints_to_budget_pct"] = float(Decimal(m_all) * 100 / Decimal(budget)) if budget else None
            ep["all_mints_note"] = "все mint эпохи (включая неизвестные категории) — НЕ использование бюджета"
            # цена эпохи
            d0 = int(st.timestamp()) // DAY * DAY
            days = [d for d in prices if d0 <= d < int(en.timestamp())]
            vol = sum(prices[d][1] for d in days)
            if len(days) >= 5 and vol > 0:
                vwap = sum(prices[d][0] * prices[d][1] for d in days) / vol
                ep["price"] = {"value": float(vwap), "status": "estimated", "days": len(days), "pool": data.get("pool"),
                               "method": "VWAP дневных цен закрытия самого ликвидного пула (GeckoTerminal OHLCV) за дни эпохи; "
                                         "границы эпохи 22:00 UTC не совпадают с дневными свечами"}
                post = sum(v for k, v in by.items() if k not in ("burn", "genesis", "pre_tge_unclassified"))
                ep["emission_usd"] = {"lp": float(Decimal(lp) / (Decimal(10) ** dec) * vwap) if labeled else None,
                                      "all_post_tge_mints": float(Decimal(post) / (Decimal(10) ** dec) * vwap),
                                      "status": "estimated",
                                      "note": "по цене своей эпохи; all_post_tge_mints включает mint с неизвестным назначением (верхняя граница)"}
            else:
                ep["price"] = {"value": None, "status": "unknown", "days": len(days),
                               "reason": "нет дневных свечей минимум за 5 дней эпохи — стоимость эмиссии не вычисляется (показано количество токенов)"}
            # комиссии DefiLlama за UTC-сутки эпохи
            fdays = [d0 + DAY * (i + 1) for i in range(7)]
            fe = {}
            for k, ser in feeser.items():
                have = [d for d in fdays if d in ser]
                if ser and len(have) == 7:
                    fe[k] = float(sum(ser[d] for d in fdays))
                elif ser and have:
                    fe[k] = None
                    fe[f"{k}_partial_days"] = len(have)
            ep["fees"] = {**fe, "status": "reported" if fe.get("fees") is not None else "unknown",
                          "note": "DefiLlama, сутки UTC со дня после начала эпохи (сдвиг 2 ч от границ); gross fees ≠ eligible fees"}
            if ep.get("emission_usd") and fe.get("fees"):
                ep["incentives_to_gross_fees"] = {
                    "lp": (ep["emission_usd"]["lp"] / fe["fees"]) if ep["emission_usd"]["lp"] is not None else None,
                    "all_post_tge_mints": ep["emission_usd"]["all_post_tge_mints"] / fe["fees"],
                    "eligible_fees": None, "eligible_status": "unknown",
                    "note": "отношение к ОБЩИМ комиссиям DefiLlama; какие комиссии учитывает контрактное ограничение эмиссии — не установлено"}
        else:
            ep["utilization"] = {"status": "unknown", "reason": "границы эпохи не подтверждены временем блоков"}
        if cur_tn and cur_tn.get("published_at") and parse_iso(cur_tn["published_at"]) and parse_iso(cur_tn["published_at"]) > st + _dt.timedelta(days=1):
            ep["version_note"] = "версия токеномики опубликована позже начала эпохи — бюджет эпохи мог быть другим"
        epochs.append(ep)
    res["epochs"] = epochs
    if not epochs:
        lim("в текущей версии токеномики нет недельных бюджетов — использование бюджета не вычисляется")
    # ---- обращение
    circ = data["circ"]
    by_src: dict[str, list] = defaultdict(list)
    for c in circ:
        by_src[c["source"]].append(c)
    best = max(by_src.values(), key=len) if by_src else []
    if len(best) >= 2 and best[0]["fetched_at"] != best[-1]["fetched_at"]:
        res["circulating"] = {"status": "calculated", "source": best[0]["source"], "from": {"at": best[0]["fetched_at"], "value": best[0]["value"]},
                              "to": {"at": best[-1]["fetched_at"], "value": best[-1]["value"]},
                              "change": best[-1]["value"] - best[0]["value"], "points": len(best),
                              "note": "по наблюдениям одного источника; методика источника — в карточке токена"}
    else:
        res["circulating"] = {"status": "unknown", "points": len(best),
                              "latest": {"at": best[-1]["fetched_at"], "value": best[-1]["value"], "source": best[-1]["source"]} if best else None,
                              "reason": "нужно минимум два наблюдения circulating supply одного источника — изменение не вычисляется"}
    res["net_circulating_growth"] = {
        "status": "unknown" if res["circulating"]["status"] == "unknown" else "calculated",
        "components": {"total_supply_change": res["totals"]["net"], "total_supply_status": res["totals"]["status"],
                       "minted_into_escrow": A(cat_raw.get("mint_to_escrow", 0) + sum(int(g["amount"]["raw"]) for g in genesis_items
                                                                                     if g.get("role") == "ve_escrow")),
                       "new_locks": None, "new_unlocks": None, "locks_status": "unknown", "locks_reason": STAGE2},
        "note": "итоговое изменение обращения не выводится формулой mint − locks + unlocks: блокировки/разблокировки ещё не индексируются"}
    res["claims"] = {"status": "unknown", "reason": STAGE2}
    res["ve_locks"] = {"status": "unknown", "reason": STAGE2}
    res["reward_sales"] = {"status": "unknown", "reason": STAGE3,
                           "note": "продажи не подменяются процентом от эмиссии; переводы на DEX/CEX не считаются доказанными продажами"}
    ub = sorted(unknown_by.values(), key=lambda x: -x["raw"])
    unk_total = sum(x["raw"] for x in ub)
    res["unknown_recipients"] = {"total": A(unk_total), "addresses": len(ub),
                                 "top": [{**{k: v for k, v in x.items() if k != "raw"}, "amount": A(x["raw"]),
                                          "share_of_unknown_pct": float(Decimal(x["raw"]) * 100 / Decimal(unk_total)) if unk_total else None}
                                         for x in ub[:25]]}
    res["roles_used"] = [{"address": a, "role": r["role"], "role_name": ROLES.get(r["role"]), "label": r.get("label"), "confidence": r["confidence"],
                          "origin": r["origin"], "source_url": r.get("source_url"), "conflict": r.get("conflict"),
                          "refines_legacy": r.get("refines_legacy")} for a, r in roles.items()]
    res["conflicts"] = [x for x in res["roles_used"] if x["conflict"]]
    res["sources"] = [{"source": "события Transfer (mint/burn) RPC", "blocks": [scan_from, scan_to]},
                      {"source": "токеномика", "version": (cur_tn or {}).get("version_label"), "url": (cur_tn or {}).get("source_url")},
                      {"source": "реестр ролей адресов", "count": len(roles)}]
    if data.get("pool"):
        res["sources"].append({"source": "GeckoTerminal OHLCV", "pool": data["pool"]})
    if data.get("protocol_slug"):
        res["sources"].append({"source": "DefiLlama fees", "url": f"https://defillama.com/protocol/{data['protocol_slug']}"})
    res["answers"] = _answers(res)
    res["status"] = "ok"
    res["confidence"] = "high" if complete and labeled and tge_block else "medium" if complete else "low"
    res["summary"] = {"minted": res["totals"]["minted"], "burned": res["totals"]["burned"], "net": res["totals"]["net"],
                      "reconciled": complete, "genesis_confirmed": res["genesis"]["confirmed"], "pre_tge_unclassified": res["genesis"]["pre_tge_unclassified"],
                      "lp_emission": res["lp_emission"]["amount"], "lp_status": res["lp_emission"]["status"],
                      "unknown": res["unknown_recipients"]["total"], "unknown_share_pct":
                          float(Decimal(unk_total) * 100 / Decimal(minted)) if minted else None,
                      "epochs": [{"week": e["week"], "state": e["state"], "util": e["utilization"].get("status"),
                                  "util_pct": e["utilization"].get("value_pct"),
                                  "util_confirmed_pct": e["utilization"].get("confirmed_pct")} for e in epochs],
                      "conflicts": len(res["conflicts"])}
    return res


def _answers(r: dict) -> list[dict]:
    """Проверяемые ответы на вопросы исследования (FABLES и любой токен с той же структурой данных)."""
    g = r["genesis"]
    lp = r["lp_emission"]
    out = [
        {"q": "Сколько токенов выпущено первоначально (Genesis)?", "status": g["status"],
         "value": g["confirmed"], "extra": {"не подтверждено (до запуска)": g["pre_tge_unclassified"], "вероятно": g["probable"],
                                           "заявлено, не проверено": g["claimed"]},
         "basis": g["note"]},
        {"q": "Сколько выпущено именно на LP-вознаграждения?", "status": lp["status"], "value": lp["amount"],
         "basis": lp["note"] or f"mint после запуска на адреса с ролями: {', '.join(lp['eligible_roles'])}"},
        {"q": "Сколько наград реально получено LP?", "status": "unknown", "value": None, "basis": STAGE2},
        {"q": "Сколько новых токенов заблокировано?", "status": "partial" if r["net_circulating_growth"]["components"]["minted_into_escrow"] and
         int(r["net_circulating_growth"]["components"]["minted_into_escrow"]["raw"]) else "unknown",
         "value": r["net_circulating_growth"]["components"]["minted_into_escrow"],
         "basis": "подтверждён только mint сразу в ve-escrow; новые блокировки ранее выпущенных токенов — " + STAGE2},
        {"q": "Сколько разблокировано и фактически вернулось в обращение?", "status": "unknown", "value": None, "basis": STAGE2},
        {"q": "Как менялось circulating supply?", "status": r["circulating"]["status"],
         "value": r["circulating"].get("change"), "basis": r["circulating"].get("note") or r["circulating"].get("reason")},
        {"q": "Стоимость эмиссионных стимулов относительно подходящих (eligible) комиссий?", "status": "unknown", "value": None,
         "basis": "eligible fees не установлены; по эпохам показано отношение к ОБЩИМ комиссиям DefiLlama (справочно)"},
        {"q": "Какая доля бюджета использована по эпохам?",
         "status": "unknown" if all(e["utilization"]["status"] == "unknown" for e in r["epochs"]) else "partial",
         "value": [{"week": e["week"], "status": e["utilization"]["status"], "pct": e["utilization"].get("value_pct"),
                    "min_pct": e["utilization"].get("confirmed_pct")} for e in r["epochs"]],
         "basis": (r["epochs"][0]["utilization"].get("reason") if r["epochs"] else "нет недельных бюджетов в токеномике")},
        {"q": "Есть ли подтверждённые продажи полученных вознаграждений?", "status": "unknown", "value": None, "basis": STAGE3},
        {"q": "Улучшается ли экономика относительно роста эмиссии?", "status": "see_momentum", "value": None,
         "basis": "сопоставьте чистый выпуск по эпохам с выводом модуля «Фундаментальный импульс»; совместная оценка — после этапа 2"},
    ]
    return out


def classified_events(uid: str) -> list[dict]:
    """Классификация каждого события (по запросу пользователя — выгрузка; в карточке не вызывается)."""
    snap, data = load(uid)
    rows = db.rows_to_dicts(db.q("SELECT kind, tx_hash, log_index, block, ts, ts_source, counterparty, amount_raw FROM supply_events "
                                 "WHERE token_uid=? AND COALESCE(orphaned,0)=0 ORDER BY block, log_index", (uid,)))
    tge_iso = (data["tge"] or {}).get("value")
    wb = data["week_blocks"]
    tge_block = None
    if tge_iso:
        key = tge_iso if tge_iso in wb else next((k for k in wb if parse_iso(k) == parse_iso(tge_iso)), None)
        tge_block = wb[key]["block"] if key else None
    out = []
    for e in rows:
        if e["kind"] == "burn":
            cat, lvl, rn = "burn", "confirmed", None
        else:
            cat, lvl, rn = classify(e, data["roles"].get(e["counterparty"]), tge_block)
        out.append({**e, "category": cat, "evidence": lvl, "role": rn, "method_version": VERSION})
    return out
