"""API аналитических модулей 2.1. Подключается к основному приложению одной строкой; при выключенных модулях
только чтение состояния — база не изменяется (таблицы аналитики не создаются)."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from . import analytics, db
from .chains import CHAINS
from .util import loads, normalize_address, valid_address

router = APIRouter(prefix="/api/analytics")


@router.get("/status")
def status():
    from .jobs import runner
    st = analytics.status()
    l = runner.lanes.get("analytics")
    st["lane"] = {"queued": l.size() if l else 0, "current": (l.current or {}).get("kw") if l else None}
    st["roles"] = analytics.ROLES
    st["confidence"] = analytics.CONFIDENCE
    return st


@router.post("/settings")
def settings(payload: dict = Body(...)):
    m = payload.get("module")
    if m not in analytics.MODULES:
        raise HTTPException(400, "модуль: momentum | emission")
    on = bool(payload.get("enabled"))
    s = analytics.set_enabled(m, on)
    if on:
        from .jobs import runner
        runner.submit("analytics", 3, module=m)  # первый расчёт сразу (в своей очереди, без сетевых запросов)
    return {"settings": s, "status": analytics.status()}


def _latest_rows(module: str) -> list[dict]:
    if not analytics.schema_present():
        return []
    rows = db.q("SELECT r.*, t.symbol, t.name, t.chain, t.level, t.status AS token_status, s.last_status, s.last_error, "
                "(SELECT 1 FROM hidden_tokens h WHERE h.token_uid=t.uid AND h.active=1) AS hidden "
                "FROM an_state s JOIN an_results r ON r.id=s.last_result_id JOIN tokens t ON t.uid=s.token_uid WHERE s.module=?", (module,))
    out = []
    for r in rows:
        d = {k: r[k] for k in ("token_uid", "symbol", "name", "chain", "level", "token_status", "computed_at", "method_version", "verdict",
                               "confidence", "last_status", "last_error", "hidden")}
        d["summary"] = loads(r["summary"], {})
        out.append(d)
    err = db.q("SELECT s.token_uid, s.last_error, s.last_run_at, t.symbol FROM an_state s JOIN tokens t ON t.uid=s.token_uid "
               "WHERE s.module=? AND s.last_status='error' AND s.last_result_id IS NULL", (module,))
    out += [{"token_uid": e["token_uid"], "symbol": e["symbol"], "last_status": "error", "last_error": e["last_error"],
             "computed_at": e["last_run_at"], "summary": {}} for e in err]
    return out


@router.get("/momentum")
def momentum_list():
    rows = _latest_rows("momentum")
    order = {"improving": 0, "stable": 1, "deteriorating": 2, "insufficient": 3}
    rows.sort(key=lambda r: (not (r["summary"] or {}).get("divergence"), order.get(r.get("verdict"), 4),
                             -((r["summary"] or {}).get("fees_30d") or 0)))
    return {"enabled": analytics.enabled("momentum"), "rows": rows, "verdicts": __import__("app.momentum", fromlist=["VERDICTS"]).VERDICTS}


@router.get("/momentum/{uid:path}")
def momentum_one(uid: str):
    r = analytics.latest("momentum", uid)
    tok = db.q1("SELECT chain, facts FROM tokens WHERE uid=?", (uid,))
    series = {}
    if tok:  # ряды для графиков читаются из уже сохранённых данных по запросу (в историю результатов не копируются)
        p = (loads(tok["facts"], {}) or {}).get("protocol") or {}
        ch = loads(db.get_progress(f"chain_health:{tok['chain']}"), {}) or {}
        series = {"tvl": (p.get("tvl_series") or [])[-120:], "chain_tvl": (ch.get("tvl_series") or [])[-120:],
                  **{k: ((p.get(k) or {}).get("chart") or [])[-120:] for k in ("fees", "revenue", "holders_revenue", "dex_volume")}}
    return {"enabled": analytics.enabled("momentum"), "latest": r, "history": analytics.history("momentum", uid), "series": series}


@router.get("/emission")
def emission_list():
    return {"enabled": analytics.enabled("emission"), "rows": _latest_rows("emission")}


@router.get("/emission/{uid:path}/events.csv")
def emission_csv(uid: str):
    """Выгрузка классификации каждого события (вычисляется по запросу; в карточке не используется)."""
    from . import emission_pressure
    if not db.q1("SELECT 1 FROM tokens WHERE uid=?", (uid,)):
        raise HTTPException(404, "токен не найден")
    with analytics.no_network():
        rows = emission_pressure.classified_events(uid)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["block", "ts", "ts_source", "tx_hash", "log_index", "kind", "counterparty", "amount_raw", "category", "evidence", "role", "method_version"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) for c in cols])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=emission-{uid.replace(':', '_')}.csv"})


@router.get("/emission/{uid:path}")
def emission_one(uid: str):
    r = analytics.latest("emission", uid)
    tok = db.q1("SELECT chain FROM tokens WHERE uid=?", (uid,))
    ch = CHAINS.get(tok["chain"]) if tok else None
    return {"enabled": analytics.enabled("emission"), "latest": r, "history": analytics.history("emission", uid, 20),
            "explorer_addr": ch.explorer_address("{address}") if ch else None, "explorer_tx": ch.explorer_tx("{tx}") if ch else None}


@router.get("/token/{uid:path}")
def token_summary(uid: str):
    """Компактная сводка для карточки токена (только чтение сохранённых результатов)."""
    out = {}
    for m in analytics.MODULES:
        r = analytics.latest(m, uid) if analytics.enabled(m) else None
        out[m] = {"enabled": analytics.enabled(m), "computed_at": r["computed_at"] if r else None,
                  "summary": loads(r["summary"], {}) if r else None, "verdict": r["verdict"] if r else None,
                  "confidence": r["confidence"] if r else None, "state": r["state"] if r else None}
    return out


@router.get("/roles")
def roles(chain: str, token_uid: str | None = None):
    if chain not in CHAINS:
        raise HTTPException(400)
    reg = []
    if analytics.schema_present():
        reg = db.rows_to_dicts(db.q("SELECT * FROM address_roles WHERE chain=? AND (token_uid=? OR token_uid IS NULL) ORDER BY id DESC",
                                    (chain, token_uid)))
    return {"effective": list(analytics.roles_for(chain, token_uid).values()), "registry": reg, "roles": analytics.ROLES}


@router.post("/roles")
def add_role(payload: dict = Body(...)):
    ck = payload.get("chain")
    if ck not in CHAINS:
        raise HTTPException(400, "неизвестная сеть")
    ch = CHAINS[ck]
    addr = (payload.get("address") or "").strip()
    if not valid_address(ch.kind, addr):
        raise HTTPException(400, "некорректный адрес")
    uid = payload.get("token_uid") or None
    if uid and not db.q1("SELECT 1 FROM tokens WHERE uid=? AND chain=?", (uid, ck)):
        raise HTTPException(400, "токен не найден в этой сети — роль не переносится между сетями")
    try:
        rid = analytics.add_role(ck, uid, normalize_address(ch.kind, addr), payload.get("role"),
                                 source_url=payload.get("source_url"), source_type=payload.get("source_type"),
                                 evidence=payload.get("evidence"), confidence=payload.get("confidence") or "claimed", note=payload.get("note"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if analytics.enabled("emission") and uid:
        from .jobs import runner
        runner.submit("analytics", 2, module="emission", uid=uid)  # пересчёт с новой меткой
    return {"ok": True, "id": rid}


@router.post("/roles/{rid}/deactivate")
def deactivate_role(rid: int):
    if not analytics.deactivate_role(rid):
        raise HTTPException(404)
    return {"ok": True}


@router.post("/recompute")
def recompute(payload: dict = Body(...)):
    m = payload.get("module")
    if m not in analytics.MODULES:
        raise HTTPException(400)
    if not analytics.enabled(m):
        raise HTTPException(409, "модуль выключен — включите его, чтобы выполнить расчёт")
    from .jobs import runner
    jid = runner.submit("analytics", 2, module=m, uid=payload.get("uid"), force=True)
    return {"queued": jid is not None}


@router.get("/fables")
def fables_view():
    """Этап 2 (релиз A): атрибуция эмиссии FABLES — последний расчёт и состояние индексатора (только чтение)."""
    from . import fables as F, fables_indexer
    r = analytics.latest("fables", F.TOKEN_UID) if analytics.schema_present() else None
    return {"enabled": analytics.enabled("fables"), "latest": r, "indexer": fables_indexer.status(),
            "history": analytics.history("fables", F.TOKEN_UID, 20), "token_uid": F.TOKEN_UID,
            "explorer_addr": "https://robinhoodchain.blockscout.com/address/{address}", "explorer_tx": "https://robinhoodchain.blockscout.com/tx/{tx}"}
