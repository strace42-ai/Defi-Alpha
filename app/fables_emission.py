"""FABLES Emission Intelligence, релиз A: атрибуция каждого выпуска по записи Minter, эпохи по контракту, недельный бюджет.
Без сетевых запросов: читает события, собранные fables_indexer, и supply_events основного сканера (не изменяя их).

Атрибуция: Minter при каждом выпуске сам записывает Minted(allocation, to, amount, by) — категорию распределения и контракт,
который его вызвал. Для распределения Emissions категория уточняется событием Claimed потока в той же транзакции
(kind 0 — LP, kind 1 — rebase). Поэтому не нужны ни traces, ни предположения по адресу получателя.
Сверка: каждый mint из supply_events (Transfer от 0x0) должен иметь ровно одну запись Minted в той же транзакции
с тем же получателем и суммой; Σ Minted должна равняться Minter.totalMinted(). Расхождение — UNKNOWN с перечнем.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from . import analytics, db, fables as F
from .util import loads, now_utc

VERSION = "fables-emission-A1"
DEC = 18


def candidates() -> list[str]:
    from .fables_indexer import schema_present
    if not schema_present() or not db.q1("SELECT 1 FROM tokens WHERE uid=?", (F.TOKEN_UID,)):
        return []
    return [F.TOKEN_UID]


def _events(name: str) -> list[dict]:
    rows = db.q("SELECT tx_hash, log_index, block, block_hash, args FROM an_events WHERE event=? AND orphaned=0 ORDER BY block, log_index", (name,))
    return [{**{k: r[k] for k in ("tx_hash", "log_index", "block", "block_hash")}, **loads(r["args"], {})} for r in rows]


def load(uid: str) -> tuple[dict, dict]:
    from .fables_indexer import checkpoint, latest_evidence
    agg = {r["event"]: [r["n"], r["mx"]] for r in db.q("SELECT event, COUNT(*) n, MAX(block) mx FROM an_events WHERE orphaned=0 GROUP BY event")}
    orph = db.q1("SELECT COUNT(*) n FROM an_events WHERE orphaned=1")["n"]
    se = db.q1("SELECT COUNT(*) n, MAX(block) mx, SUM(COALESCE(orphaned,0)) o FROM supply_events WHERE token_uid=?", (uid,))
    ep = [tuple(r) for r in db.q("SELECT week, start_block FROM an_epochs WHERE chain_id=? ORDER BY week", (F.CHAIN_ID,))]
    ev_ids = [r["id"] for r in db.q("SELECT MAX(id) id FROM an_evidence GROUP BY subject, kind")]
    cps = {k: (checkpoint(k) or {}).get("block") for k in ("head", "logs:minter.Minted", "logs:stream.events", "logs:token.Transfer.forwarders")}
    snap = {"events": agg, "orphaned": orph, "supply": [se["n"], se["mx"], se["o"]], "epochs": ep, "evidence": sorted(ev_ids), "checkpoints": cps}
    data = {"uid": uid, "checkpoints": cps,
            "evidence": {(k["subject"], k["kind"]): latest_evidence(k["subject"], k["kind"])
                         for k in db.q("SELECT DISTINCT subject, kind FROM an_evidence")}}
    return snap, data


def _A(raw: int | None) -> dict | None:
    return None if raw is None else {"raw": str(int(raw)), "tokens": float(Decimal(int(raw)) / (Decimal(10) ** DEC))}


def compute(uid: str, data: dict) -> dict:
    cps = data["checkpoints"]
    ev = data["evidence"]
    minted = _events("Minted")
    claims = _events("StreamClaimed")
    res: dict[str, Any] = {"method_version": VERSION, "symbol": "FABLES", "limitations": [], "sources": [], "status": "ok"}
    lim = res["limitations"].append
    idx_to = cps.get("logs:minter.Minted")
    # ---- атрибуция каждого Minted
    claims_by_tx: dict[str, list[dict]] = defaultdict(list)
    for c in claims:
        claims_by_tx[c["tx_hash"]].append(c)
    mints = []
    for m in minted:
        cands = [c for c in claims_by_tx.get(m["tx_hash"], []) if c["amount"] == m["amount"]]
        claim = cands[0] if len(cands) == 1 else None
        if int(m["allocation"]) == 0 and len(cands) > 1:
            claim = None  # неоднозначно — не угадывается
        cat, lvl, why = F.classify_mint(m, claim)
        mints.append({**m, "category": cat, "evidence": lvl, "basis": why, "claim_kind": claim and claim["kind"]})
    # ---- сверка с supply_events основного сканера (Transfer от 0x0) — до общего обработанного блока
    common_to = min(x for x in (idx_to, db.q1("SELECT MAX(block) b FROM supply_events WHERE token_uid=?", (uid,))["b"]) if x is not None) if idx_to else None
    sup = db.rows_to_dicts(db.q("SELECT tx_hash, log_index, block, counterparty, amount_raw, kind FROM supply_events WHERE token_uid=? "
                                "AND COALESCE(orphaned,0)=0 AND block<=?", (uid, common_to or 0)))
    by_tx_m = defaultdict(list)
    for m in mints:
        if common_to and m["block"] <= common_to:
            by_tx_m[m["tx_hash"]].append(m)
    unmatched, matched = [], 0
    used: set[tuple] = set()
    for s in sup:
        if s["kind"] != "mint":
            continue
        cands = [m for m in by_tx_m.get(s["tx_hash"].lower(), []) if m["to"] == s["counterparty"] and m["amount"] == s["amount_raw"]
                 and (m["tx_hash"], m["log_index"]) not in used]
        if cands:
            used.add((cands[0]["tx_hash"], cands[0]["log_index"]))
            matched += 1
        else:
            unmatched.append({"tx": s["tx_hash"], "log_index": s["log_index"], "block": s["block"], "to": s["counterparty"],
                              "amount": _A(int(s["amount_raw"]))})
    minted_no_transfer = [m for ms in by_tx_m.values() for m in ms if (m["tx_hash"], m["log_index"]) not in used]
    total_minted_ev = sum(int(m["amount"]) for m in mints)
    alloc_ev = (ev.get(("minter", "allocations")) or {}).get("payload") or {}
    alloc_block = (ev.get(("minter", "allocations")) or {}).get("block")
    minted_to_alloc_block = sum(int(m["amount"]) for m in mints if alloc_block and m["block"] <= alloc_block)
    res["reconciliation"] = {
        "minted_events": len(mints), "minted_events_total": _A(total_minted_ev),
        "supply_mints_checked": sum(1 for s in sup if s["kind"] == "mint"), "supply_matched": matched,
        "supply_without_minted": unmatched[:50], "supply_without_minted_n": len(unmatched),
        "minted_without_transfer": [{"tx": m["tx_hash"], "block": m["block"], "amount": _A(int(m["amount"]))} for m in minted_no_transfer[:50]],
        "minted_without_transfer_n": len(minted_no_transfer), "checked_to_block": common_to,
        "contract_totalMinted": _A(int(alloc_ev["totalMinted"])) if alloc_ev.get("totalMinted") not in (None, "None") else None,
        "contract_totalMinted_block": alloc_block,
        "events_total_at_that_block": _A(minted_to_alloc_block) if alloc_block else None,
        "exact": bool(alloc_block and alloc_ev.get("totalMinted") not in (None, "None") and int(alloc_ev["totalMinted"]) == minted_to_alloc_block
                      and not unmatched and not minted_no_transfer),
    }
    if unmatched or minted_no_transfer:
        lim("есть выпуски без однозначной записи Minter или записи Minter без перевода — они показаны и остаются UNKNOWN")
    burns = db.rows_to_dicts(db.q("SELECT amount_raw, block FROM supply_events WHERE token_uid=? AND kind='burn' AND COALESCE(orphaned,0)=0 "
                                  "AND block<=?", (uid, common_to or 0)))
    # ---- итоги по категориям
    cat_raw: dict[str, int] = defaultdict(int)
    cat_n: dict[str, int] = defaultdict(int)
    cat_lvl: dict[str, set] = defaultdict(set)
    cat_to: dict[str, set] = defaultdict(set)
    for m in mints:
        cat_raw[m["category"]] += int(m["amount"])
        cat_n[m["category"]] += 1
        cat_lvl[m["category"]].add(m["evidence"])
        cat_to[m["category"]].add(m["to"])
    unk = sum(int(u["amount"]["raw"]) for u in unmatched)
    if unk:
        cat_raw["UNKNOWN"] += unk
        cat_n["UNKNOWN"] += len(unmatched)
        cat_lvl["UNKNOWN"].add("unknown")
    if burns:
        cat_raw["BURN"] = sum(int(b["amount_raw"]) for b in burns)
        cat_n["BURN"] = len(burns)
        cat_lvl["BURN"].add("confirmed")
    total = sum(v for k, v in cat_raw.items() if k != "BURN")
    res["categories"] = [{"key": k, "name": F.CATEGORIES[k], "amount": _A(cat_raw[k]), "events": cat_n[k],
                          "recipients": len(cat_to.get(k, ())), "share_pct": float(Decimal(cat_raw[k]) * 100 / Decimal(total)) if total and k != "BURN" else None,
                          "evidence": analytics_weakest(cat_lvl[k])} for k in F.CATEGORIES if cat_n.get(k)]
    res["totals"] = {"minted": _A(total), "burned": _A(cat_raw.get("BURN", 0)), "net": _A(total - cat_raw.get("BURN", 0)),
                     "indexed_to_block": idx_to, "classified_pct": float(Decimal(total - cat_raw.get("UNKNOWN", 0)) * 100 / Decimal(total)) if total else None}
    res["allocations"] = [{"index": i, "name": F.ALLOCATIONS[i]["name"], "naming": F.ALLOCATIONS[i]["naming"],
                           "ceiling": _A(int(alloc_ev["allocations"][str(i)]["ceiling"])) if alloc_ev.get("allocations") else None,
                           "minted_contract": _A(int(alloc_ev["allocations"][str(i)]["minted"])) if alloc_ev.get("allocations") else None,
                           "minted_events": _A(sum(int(m["amount"]) for m in mints if int(m["allocation"]) == i and (not alloc_block or m["block"] <= alloc_block)))}
                          for i in range(10)]
    # ---- эпохи (границы — точные блоки по контрактной формуле недели)
    eps = db.rows_to_dicts(db.q("SELECT week, start_ts, start_block, start_evidence FROM an_epochs WHERE chain_id=? ORDER BY week", (F.CHAIN_ID,)))
    budgets = _events("WeeklyBudgetSet")
    closed = _events("WeeksClosed")
    roots = _events("RootPosted")
    now_ts = int(now_utc().timestamp())
    by_week_closed = {}
    prev_total = 0
    for c in sorted(closed, key=lambda x: int(x["throughWeek"])):
        by_week_closed[int(c["throughWeek"])] = {"week_total": int(c["closedTotal"]) - prev_total, "cumulative": int(c["closedTotal"]),
                                                  "block": c["block"], "tx": c["tx_hash"]}
        prev_total = int(c["closedTotal"])
    start_block = {e["week"]: e["start_block"] for e in eps}
    epochs = []
    for e in eps:
        w = e["week"]
        st, en = e["start_ts"], F.week_start(w + 1)
        b0, b1 = e["start_block"], start_block.get(w + 1)
        state = "complete" if en <= now_ts and b1 else ("in_progress" if st <= now_ts < en else "unconfirmed")
        sel = [m for m in mints if b0 is not None and m["block"] >= b0 and (b1 is None or m["block"] < b1)]
        by = defaultdict(int)
        for m in sel:
            by[m["category"]] += int(m["amount"])
        hist = [{"amount": _A(int(b["amount"])), "block": b["block"], "tx": b["tx_hash"]} for b in budgets if int(b["week"]) == w]
        set_before = [b for b in budgets if int(b["week"]) == w and b0 is not None and b["block"] < b0]
        eff = int(hist[-1]["amount"]["raw"]) if hist else None
        at_start = int(set_before[-1]["amount"]) if set_before else None
        cl = by_week_closed.get(w)
        # начисление по корням потока: накопленный итог на конец недели минус на начало (asOf корня — время учёта)
        def cum_at(ts):
            xs = [int(r["cumulativeTotal"]) for r in roots if int(r["asOf"]) <= ts]
            return max(xs) if xs else 0
        accrued = cum_at(en) - cum_at(st) if roots else None
        ep: dict[str, Any] = {"week": w, "start": _iso(st), "end": _iso(en), "start_block": b0, "end_block": b1, "state": state,
                              "first_week_note": ("эмиссия начислялась с 22:00 UTC 20.10 (первый корень потока asOf 23:00) — фактически 6 дн. 2 ч"
                                                  if w == F.FIRST_WEEK else None),
                              "boundary_evidence": loads(e["start_evidence"], {}),
                              "budget_effective": _A(eff), "budget_at_start": _A(at_start), "budget_history": hist,
                              "minted_by_category": {k: _A(v) for k, v in by.items()},
                              "accrued_stream": _A(accrued) if accrued is not None else None,
                              "closed_total": _A(cl["week_total"]) if cl else None, "closed_tx": cl and cl["tx"]}
        # использование бюджета: общий итог потока (контракт) / бюджет потока (контракт); разбиение по видам — не установлено
        if eff is None:
            util = {"status": "UNKNOWN", "reason": "бюджет недели не найден в событиях WeeklyBudgetSet"}
        elif state == "in_progress":
            util = {"status": "INCOMPLETE_EPOCH", "accrued_pct": float(Decimal(accrued) * 100 / Decimal(eff)) if accrued is not None else None,
                    "reason": "эпоха не завершена — начислено на текущий момент"}
        elif cl:
            util = {"status": "CONFIRMED", "value_pct": float(Decimal(cl["week_total"]) * 100 / Decimal(eff)),
                    "basis": "WeeksClosed (закрытый итог недели) / WeeklyBudgetSet (бюджет) — оба значения записаны контрактом потока",
                    "scope": "весь поток наград (LP и rebase вместе): разбиение закрытого итога по видам контракт не публикует"}
            if cl["week_total"] > eff:
                util["warning"] = f"закрытый итог недели больше бюджета: {cl['week_total']} > {eff} (tx {cl['tx']})"
        else:
            util = {"status": "PARTIAL", "reason": "неделя завершилась, но WeeksClosed для неё ещё не записан",
                    "accrued_pct": float(Decimal(accrued) * 100 / Decimal(eff)) if accrued else None}
        ep["utilization_total_stream"] = util
        ep["utilization_lp"] = {"status": "UNKNOWN", "reason": "доля LP в начислении недели не публикуется событиями контракта; "
                                                              "LP claim-ы минтятся с запаздыванием и не равны начислению недели (релиз B: файлы корней потока)"}
        ep["utilization_rebase"] = {"status": "UNKNOWN", "reason": "то же для rebase"}
        ep["utilization_operating"] = {"status": "UNKNOWN", "reason": "операционная доля (1% от LP по документации) отдельными выпусками не наблюдается; механизм не подтверждён"}
        epochs.append(ep)
    res["epochs"] = epochs
    res["budget_structure"] = {
        "total_authorized": "CONFIRMED: Emission stream.weeklyBudget (события WeeklyBudgetSet, с историей изменений)",
        "lp_authorized": "UNKNOWN: разбиение бюджета по видам контрактом не задаётся",
        "rebase_authorized": "UNKNOWN (документация: 8% от заработанного LP при доле блокировок ≤ 30%)",
        "operating_authorized": "UNKNOWN (документация: 1% от полученного LP, из потолка Emissions)",
        "eligible_fees": "UNKNOWN в релизе A: формула бюджета рынка 5 × (eligible staked fees + incentives) — из документации; файлы /api/budget — релиз B",
    }
    # ---- расследования
    res["investigations"] = {"team_lock": _team(ev, mints), "points_locked": _points(mints, ev), "registry": _registry(ev, mints)}
    res["sources"] = [{"source": "Minter.Minted", "to_block": idx_to}, {"source": "Emission stream (Claimed, WeeklyBudgetSet, WeeksClosed, RootPosted)",
                                                                        "to_block": cps.get("logs:stream.events")},
                      {"source": "supply_events основного сканера (сверка)", "to_block": common_to},
                      {"source": "официальная документация fables.fi/docs (таблица контрактов, ABI сайта)"}]
    if not idx_to:
        res["status"] = "no_data"
        lim("индекс событий ещё не собран")
    res["confidence"] = "high" if res["reconciliation"]["exact"] else "medium" if mints else "low"
    res["summary"] = {"minted": res["totals"]["minted"], "classified_pct": res["totals"]["classified_pct"],
                      "reconciled": res["reconciliation"]["exact"],
                      "by_category": {c["key"]: c["amount"]["tokens"] for c in res["categories"]},
                      "epochs": [{"week": e["week"], "state": e["state"], "util": e["utilization_total_stream"]["status"],
                                  "util_pct": e["utilization_total_stream"].get("value_pct")} for e in epochs]}
    res["block_to"] = idx_to
    return res


def analytics_weakest(levels) -> str:
    rank = {"confirmed": 3, "probable": 2, "claimed": 1, "unknown": 0}
    return min(levels, key=lambda x: rank.get(x, 0)) if levels else "unknown"


def _iso(ts: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _team(ev: dict, mints: list[dict]) -> dict:
    m = next((x for x in mints if int(x["allocation"]) == 3), None)
    rec = (ev.get(("team_lock", "creation_receipt")) or {}).get("payload") or {}
    hist = (ev.get(("team_lock", "lock1_events")) or {}).get("payload") or {}
    esc = ev.get(("escrow", "state")) or {}
    es = esc.get("payload") or {}
    from .keccak import topic
    T = topic("Transfer(address,address,uint256)")
    LOCKED = topic("Locked(uint256,address,uint128,uint64)")
    steps, flags = [], {}
    for lg in rec.get("logs") or []:
        t = lg["topics"]
        if lg["address"] == F.TOKEN and t and t[0] == T:
            fr, to = "0x" + t[1][-40:], "0x" + t[2][-40:]
            steps.append({"i": lg["i"], "what": f"FABLES Transfer {fr} → {to}", "amount": _A(int(lg["data"], 16))})
            if fr == F.ADDR["minter"] and to == F.ADDR["escrow"]:
                flags["minter_to_escrow"] = int(lg["data"], 16)
        elif lg["address"] == F.ADDR["escrow"] and t and t[0] == T and len(t) == 4:
            steps.append({"i": lg["i"], "what": f"veNFT #{int(t[3], 16)} выпущен на {'0x' + t[2][-40:]}"})
            if int(t[3], 16) == 1:
                flags["nft1_owner"] = "0x" + t[2][-40:]
        elif lg["address"] == F.ADDR["escrow"] and t and t[0] == LOCKED:
            d = lg["data"][2:]
            steps.append({"i": lg["i"], "what": f"Locked(tokenId={int(t[1], 16)}, owner={'0x' + t[2][-40:]}, amount={int(d[:64], 16)}, unlock={int(d[64:128], 16)})"})
            flags["locked_amount"], flags["locked_unlock"] = int(d[:64], 16), int(d[64:128], 16)
    w = [int(x) for x in es.get("lock1_words") or []]
    perm = int(es["PERMANENT"]) if es.get("PERMANENT") not in (None, "None") else None
    withdrawn = [x for x in hist.get("events") or [] if x["event"] == "Withdrawn"]
    chain_ok = (m is not None and flags.get("minter_to_escrow") == int(m["amount"]) and flags.get("nft1_owner") == F.ADDR["team_safe"]
                and flags.get("locked_amount") == int(m["amount"]))
    return {
        "mint": {"tx": m and m["tx_hash"], "block": m and m["block"], "amount": _A(int(m["amount"])) if m else None, "to": m and m["to"],
                 "by": m and m["by"], "allocation": 3},
        "creation_tx": {"from": rec.get("from"), "to": rec.get("to"), "selector": rec.get("selector"), "steps": steps},
        "chain_confirmed": chain_ok,
        "chain_note": ("в одной транзакции: mint 40M на minter → перевод minter → escrow → veNFT #1 владельцу Team Safe → Locked(1, Team Safe, 40M)"
                       if chain_ok else "цепочка подтверждена не полностью — см. шаги"),
        "current": {"block": esc.get("block"), "owner": es.get("lock1_owner"), "locked": _A(w[0]) if w else None,
                    "unlock_raw": str(w[1]) if len(w) > 1 else None, "permanent": bool(len(w) > 1 and perm is not None and w[1] == perm),
                    "max_duration_days": (es.get("MAX_DURATION_s") or 0) / 86400 if es.get("MAX_DURATION_s") else None},
        "history": hist, "withdrawn": withdrawn,
        "withdrawable_now": False if (len(w) > 1 and perm is not None and w[1] == perm) else None,
        "withdraw_note": ("состояние контракта: unlock = PERMANENT. В ABI escrow есть unlockPermanent(tokenId): владелец может отключить постоянную "
                          "блокировку, после чего (по документации) начинается отсчёт ≈ 2 лет до возможности вывода. Сейчас вывод невозможен; "
                          "это не означает, что токены не могут вернуться в обращение в будущем."),
        "circulation": "заблокированы в escrow (не в обращении) на проверенном блоке; не «навсегда» — см. unlockPermanent",
    }


def _points(mints: list[dict], ev: dict) -> dict:
    out = {}
    tr = _events("Transfer")
    bal = ((ev.get(("token", "balances")) or {}).get("payload") or {})
    for key in ("points_six_months", "points_auto_max"):
        a = F.ADDR[key]
        ms = [m for m in mints if m["by"] == a]
        to_self = sum(int(m["amount"]) for m in ms if m["to"] == a)
        outs = [t for t in tr if t["from"] == a]
        to_escrow = sum(int(t["value"]) for t in outs if t["to"] == F.ADDR["escrow"])
        other = sum(int(t["value"]) for t in outs if t["to"] != F.ADDR["escrow"])
        out[key] = {"address": a, "docs": F.BY_ADDR[a]["docs"], "mints": len(ms), "minted": _A(sum(int(m["amount"]) for m in ms)),
                    "minted_to_itself": _A(to_self), "transferred_to_escrow": _A(to_escrow), "transferred_elsewhere": _A(other),
                    "balance_now": _A(int(bal[key])) if bal.get(key) not in (None, "None") else None,
                    "role_confirmed": bool(ms) and to_escrow == to_self and other == 0,
                    "note": "каждый mint зачисляется на сам контракт и в той же транзакции переводится в veFABLES escrow (новый лок получателю Points)"}
    return out


def _registry(ev: dict, mints: list[dict]) -> list[dict]:
    rel = (ev.get(("minter", "isMinter")) or {}).get("payload") or {}
    tok_m = (ev.get(("minter", "token.minter()")) or {}).get("payload") or {}
    rows = []
    for c in F.CONTRACTS:
        code = (ev.get((c["key"], "code")) or {})
        p = code.get("payload") or {}
        rels, level = [], "claimed"
        if c["key"] == "minter" and tok_m.get("matches"):
            rels.append("FABLES.minter() = этот адрес")
            level = "confirmed"
        allowed = [k.split(":")[0] for k, v in rel.items() if v and k.split(":")[1] == c["key"]]
        if allowed:
            rels.append("Minter.isMinter(" + ", ".join(allowed) + ") = true")
            level = "confirmed"
        n = sum(1 for m in mints if m["by"] == c["address"])
        if n:
            rels.append(f"вызывающий в {n} записях Minted")
        if c["key"] in ("team_safe",) and any(int(m["allocation"]) == 3 for m in mints):
            rels.append("владелец veNFT #1, созданного в транзакции выпуска allocation 3")
            level = "confirmed"
        if c["key"] == "seeder_safe" and any(int(m["allocation"]) == 9 and m["to"] == c["address"] for m in mints):
            rels.append("получатель Minted(allocation=9) при TGE")
            level = "confirmed"
        if c["key"] == "escrow":
            rels.append("получатель FABLES из Points-блокировок, rebase и Lock #1 (события в транзакциях)")
            level = "confirmed"
        rows.append({"key": c["key"], "address": c["address"], "chain_id": F.CHAIN_ID, "docs_name": c["docs"], "role_expected": c["role_expected"],
                     "kind_detected": p.get("kind"), "code_bytes": p.get("code_bytes"), "verified_source": p.get("verified_source"),
                     "relations": rels, "checked_block": code.get("block"), "checked_at": code.get("fetched_at"),
                     "evidence": level if p else "unknown",
                     "note": "наличие кода само по себе роль не подтверждает" if level == "claimed" else None})
    return rows
