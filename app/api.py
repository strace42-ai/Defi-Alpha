"""Локальный HTTP API для интерфейса. Слушает только 127.0.0.1."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, backup, config, db, diagnostics, export, http, pipeline
from .chains import CHAINS
from .discovery import add_token_manually
from .filters import FILTER_NAMES, FUNDING_CATEGORIES
from .jobs import JOB_NAMES, runner
from .pipeline import LEVELS
from .util import dumps, loads, now_iso, normalize_address, parse_iso, valid_address

WEB = Path(__file__).resolve().parent / "web"
app = FastAPI(title="DeFi Alpha Scanner", version=__version__)


@app.exception_handler(sqlite3.Error)
async def _db_error(request, exc):
    """Ошибка БД → явный ответ: действие НЕ выполнено (транзакция откатана), интерфейс показывает причину."""
    db._file_log("api", f"{request.method} {request.url.path}: {type(exc).__name__}: {exc}")
    return JSONResponse({"detail": f"База данных временно недоступна, действие не выполнено и не сохранено: {exc}",
                         "retry": True}, status_code=503)


@app.exception_handler(Exception)
async def _any_error(request, exc):
    db._file_log("api", f"{request.method} {request.url.path}: {type(exc).__name__}: {exc}")
    return JSONResponse({"detail": f"Внутренняя ошибка, действие не выполнено: {type(exc).__name__}: {exc}"}, status_code=500)

_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only(request, call_next):
    """Защита локального сервера: только Host 127.0.0.1/localhost (против DNS rebinding) и
    запрет изменяющих запросов с чужих сайтов (Origin не локальный)."""
    from . import startup as _st
    if _st.SETUP.get("active") and request.url.path.startswith("/api/") and not request.url.path.startswith(("/api/setup", "/api/meta")):
        return JSONResponse({"detail": "Требуется выбор базы данных: найдена прежняя установка", "setup_required": True}, status_code=409)
    host = (request.headers.get("host") or "").split(":")[0]
    if host and host not in _LOCAL_HOSTS and host != "testserver":
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if (urlparse(origin).hostname or "") not in _LOCAL_HOSTS:
                return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        ctype = request.headers.get("content-type", "")
        if request.headers.get("content-length", "0") not in ("0", "") and "application/json" not in ctype:
            return JSONResponse({"detail": "JSON expected"}, status_code=415)
    return await call_next(request)


def _setup_active() -> bool:
    from . import startup
    return bool(startup.SETUP.get("active"))


def _row(r: dict, fc: dict[str, dict] | None = None) -> dict:
    f = loads(r.get("facts"), {}) or {}
    ch = CHAINS.get(r["chain"])
    fr = fc or {}
    cands = f.get("mcap_candidates") or []
    best = next((c for k in ("circulating_official", "circulating_manual", "coingecko_via_gt", "ds_reported")
                 for c in cands if c["kind"] == k), None)
    ath = f.get("ath") or {}
    price = (f.get("price") or {}).get("value") or (f.get("gt") or {}).get("price_usd") or (f.get("ds") or {}).get("price_usd")
    dd = None
    if price and ath.get("ath_usd"):
        dd = max(0.0, (1 - price / ath["ath_usd"]) * 100)
    age_days = None
    first_trade = ath.get("first_trade_at") or (f.get("earliest_pool") or {}).get("at") or (f.get("ds") or {}).get("pair_created_at")
    if first_trade:
        dt = parse_iso(first_trade)
        if dt:
            age_days = round((_dt.datetime.now(_dt.timezone.utc) - dt).total_seconds() / 86400, 1)
    return {
        "uid": r["uid"], "chain": r["chain"], "chain_name": ch.name if ch else r["chain"], "address": r["address_display"] or r["address"],
        "symbol": r["symbol"], "name": r["name"], "asset_class": r["asset_class"],
        "first_seen_at": r["first_seen_at"], "discovery_source": r["discovery_source"],
        "first_trade": first_trade, "age_days": age_days,
        "age_basis": "первая сделка" if ath.get("first_trade_at") else ("самый ранний пул" if f.get("earliest_pool") else "пул (DS)") if first_trade else None,
        "price": price, **_mcap_fields(f, best),
        "fdv_total": (f.get("fdv_total") or {}).get("value") or (f.get("gt") or {}).get("fdv_usd"),
        "fdv_max": (f.get("fdv_max") or {}).get("value"),
        "liquidity": (f.get("pools") or {}).get("liquidity_quality_usd") if f.get("pools") else ((f.get("gt") or {}).get("liquidity_usd") or (f.get("ds") or {}).get("liquidity_usd")),
        "volume": (f.get("gt") or {}).get("volume_h24") or (f.get("ds") or {}).get("volume_h24"),
        "drawdown": round(dd, 1) if dd is not None else None,
        **{f"f_{k}": (fr.get(k) or {}).get("status") for k in FILTER_NAMES},
        **{f"c_{k}": (fr.get(k) or {}).get("confidence") for k in FILTER_NAMES},
        "level": r["level"], "status": r["status"], "status_reason": r["status_reason"],
        "score_preliminary": r["score_preliminary"], "score_confirmed": r["score_confirmed"],
        "score_max_verifiable": r["score_max_verifiable"], "confidence": r["confidence"],
        "protocol": r["protocol_slug"], "explorer": ch.explorer_token(r["address_display"] or r["address"]) if ch else None,
        "watch": bool(r.get("watch")), "watch_status": r.get("watch_status"), "hidden": bool(r.get("hidden")),
        "last_market_at": r["last_market_at"], "updated_at": r.get("updated_at"),
        "price_conflict": bool((f.get("price_check") or {}).get("conflict")),
    }


def _mcap_fields(f: dict, best: dict | None) -> dict:
    m = f.get("mcap") if isinstance(f.get("mcap"), dict) and "points" in f.get("mcap", {}) else None
    if not m:
        return {"mcap": best["value"] if best else None, "mcap_basis": best["label"] if best else None, "mcap_type": None,
                "mcap_low": None, "mcap_high": None, "circ_supply": None, "total_supply": (f.get("total_supply") or {}).get("value"),
                "mcap_conflict": bool(f.get("mcap_conflict"))}
    return {"mcap": m.get("value"), "mcap_low": m.get("low"), "mcap_high": m.get("high"), "mcap_type": m.get("type"),
            "mcap_basis": m.get("reason"), "circ_supply": m.get("circulating_supply"), "total_supply": m.get("total_supply"),
            "mcap_conflict": bool(m.get("conflicts"))}


def _filters_map(uids: list[str]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    if not uids:
        return out
    for i in range(0, len(uids), 800):
        part = uids[i:i + 800]
        for r in db.q(f"SELECT * FROM filter_current WHERE token_uid IN ({','.join('?' * len(part))})", part):
            out.setdefault(r["token_uid"], {})[r["filter_key"]] = dict(r)
    return out


MODES = {
    "default": "Сначала продвинутые уровни",
    "strongest": "Самые сильные фундаментально",
    "recent": "Недавно обнаружены",
    "active_small": "Высокая активность при небольшой капитализации",
    "warnings": "Наиболее существенные предупреждения",
    "manual": "Требуют ручной проверки",
    "mcap_unclear": "MCAP требует уточнения",
}


def _query_tokens(search: str = "", chain: str = "", status: str = "", level: str = "", show_service: bool = False,
                  only_watch: bool = False, limit: int = 500, offset: int = 0, sort: str = "default", order: str = "desc",
                  mode: str = "default", only_hidden: bool = False, exclude_hidden: bool = False, uids: list[str] | None = None):
    where, p = ["1=1"], []
    if uids is not None:
        if not uids:
            return 0, []
        where.append(f"t.uid IN ({','.join('?' * len(uids))})"); p += uids
    if only_hidden:
        where.append("h.token_uid IS NOT NULL")
    if exclude_hidden:
        where.append("h.token_uid IS NULL")
    if mode == "manual":
        where.append("t.status='MANUAL REVIEW'")
    if mode == "mcap_unclear":
        where.append("t.level IN ('B','C','D','E') AND (json_extract(t.facts,'$.mcap.type') IS NULL OR json_extract(t.facts,'$.mcap.type')!='CONFIRMED') "
                     "AND EXISTS (SELECT 1 FROM filter_current fc WHERE fc.token_uid=t.uid AND fc.filter_key='mcap' AND fc.status!='PASS' OR "
                     "(fc.token_uid=t.uid AND fc.filter_key='mcap' AND fc.confidence='preliminary'))")
    if not show_service:
        where.append("t.asset_class='token'")
    if chain:
        where.append("t.chain=?"); p.append(chain)
    if status:
        where.append("t.status=?"); p.append(status)
    if level:
        lv = [x for x in level.split(",") if x]
        where.append(f"t.level IN ({','.join('?' * len(lv))})"); p += lv
    if search:
        where.append("(t.symbol LIKE ? OR t.name LIKE ? OR t.address LIKE ?)"); p += [f"%{search}%", f"%{search}%", f"%{search.lower()}%"]
    if only_watch:
        where.append("w.token_uid IS NOT NULL")
    cols = {"first_seen_at", "symbol", "chain", "level", "status", "score_preliminary", "last_market_at"}
    order = "ASC" if order.lower() == "asc" else "DESC"
    mode_sql = {
        "strongest": "CASE t.status WHEN 'FAIL' THEN 1 ELSE 0 END, t.score_preliminary DESC NULLS LAST",
        "recent": "t.first_seen_at DESC",
        "active_small": "CASE WHEN COALESCE(json_extract(t.facts,'$.mcap.value'), json_extract(t.facts,'$.mcap.high')) <= 5000000 THEN 0 ELSE 1 END, "
                        "(COALESCE(json_extract(t.facts,'$.ds.volume_h24'),0) * 1.0 / NULLIF(COALESCE(json_extract(t.facts,'$.mcap.value'), "
                        "json_extract(t.facts,'$.mcap.high')),0)) DESC NULLS LAST",
        "warnings": "(SELECT COUNT(*) FROM alerts a WHERE a.token_uid=t.uid AND a.acknowledged=0 AND a.severity='high') DESC, t.score_preliminary DESC NULLS LAST",
        "manual": "t.score_preliminary DESC NULLS LAST",
        "mcap_unclear": "t.score_preliminary DESC NULLS LAST",
    }
    if sort in cols:
        order_sql = f"t.{sort} {order} NULLS LAST"
    elif mode in mode_sql:
        order_sql = mode_sql[mode]
    else:  # по умолчанию: сначала продвинутые уровни, затем оценка, затем свежие
        order_sql = ("CASE t.status WHEN 'FAIL' THEN 1 ELSE 0 END, t.level DESC, t.score_preliminary DESC NULLS LAST, "
                     "t.last_market_at IS NULL, t.first_seen_at DESC")
    joins = ("FROM tokens t LEFT JOIN watchlist w ON w.token_uid=t.uid "
             "LEFT JOIN hidden_tokens h ON h.token_uid=t.uid AND h.active=1 ")
    total = db.q1(f"SELECT COUNT(*) n {joins} WHERE {' AND '.join(where)}", p)["n"]
    rows = db.rows_to_dicts(db.q(f"SELECT t.*, w.token_uid AS watch, w.status AS watch_status, h.token_uid AS hidden, h.hidden_at, "
                                 f"h.until AS hidden_until, h.reason AS hidden_reason {joins} "
                                 f"WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ? OFFSET ?", p + [limit, offset]))
    fm = _filters_map([r["uid"] for r in rows])
    return total, [_row(r, fm.get(r["uid"])) for r in rows]


@app.get("/api/setup")
def setup_state():
    from . import startup
    origin = None
    if not startup.SETUP.get("active"):
        r = db.q1("SELECT value FROM meta WHERE key='db_origin'")
        imp = db.q1("SELECT value FROM meta WHERE key='imported_from'")
        origin = {"db_origin": r["value"] if r else None, "imported_from": imp["value"] if imp else None,
                  "tokens": db.q1("SELECT COUNT(*) n FROM tokens")["n"]}
    return {**startup.SETUP, "db": str(db.DB_PATH), "origin": origin}


@app.post("/api/setup/import")
def setup_import(payload: dict = Body(...)):
    """Перенос прежней базы. На экране выбора — сразу; в работающей программе — при следующем запуске
    (активные соединения фоновых потоков нельзя безопасно подменить на лету)."""
    from pathlib import Path as _P
    from . import dbmigrate, startup
    src = _P(str(payload.get("path") or "").strip().strip('"'))
    if src.is_dir():
        src = src / "scanner.db" if (src / "scanner.db").exists() else src / "data" / "scanner.db"
    info = dbmigrate.db_info(src) if src.exists() else {"error": "файл не найден"}
    if info.get("error"):
        raise HTTPException(400, f"База не подходит: {info['error']}")
    if startup.SETUP.get("active"):
        rep = dbmigrate.import_db(src, db.DB_PATH)
        if not rep["ok"]:
            raise HTTPException(400, f"Перенос не выполнен (оригинал не изменён): {rep.get('error')}")
        startup.complete()
        return {"ok": True, "report": rep}
    dbmigrate.pending_import_path().write_text(dumps({"path": str(src), "requested_at": now_iso()}), encoding="utf-8")
    return {"ok": True, "pending": True, "source": info,
            "message": "Перенос будет выполнен при следующем запуске программы (текущая база сохранится в резервной копии)."}


@app.post("/api/setup/new")
def setup_new():
    from . import startup
    if not startup.SETUP.get("active"):
        return {"ok": True, "unchanged": True}
    startup.complete(new_empty=True)
    return {"ok": True}


@app.get("/api/meta")
def meta():
    return {"version": __version__, "filters": FILTER_NAMES, "levels": LEVELS, "funding_categories": FUNDING_CATEGORIES,
            "watch_statuses": WATCH_STATUSES, "modes": MODES, "setup_required": _setup_active(),
            "chains": {k: {"name": c.name, "kind": c.kind, "support": c.support} for k, c in CHAINS.items()},
            "jobs": JOB_NAMES, "now_utc": now_iso()}


@app.get("/api/dashboard")
def dashboard():
    c = lambda sql, p=(): db.q1(sql, p)["n"]
    s = config.load_settings()
    return {
        "pools": c("SELECT COUNT(*) n FROM pools"),
        "pools_24h": c("SELECT COUNT(*) n FROM pools WHERE first_seen_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')"),
        "tokens": c("SELECT COUNT(*) n FROM tokens WHERE asset_class='token'"),
        "service_assets": c("SELECT COUNT(*) n FROM tokens WHERE asset_class!='token'"),
        "levels": {r["level"]: r["n"] for r in db.q("SELECT level, COUNT(*) n FROM tokens WHERE asset_class='token' GROUP BY level")},
        "statuses": {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM tokens WHERE asset_class='token' GROUP BY status")},
        "watchlist": c("SELECT COUNT(*) n FROM watchlist"),
        "alerts_open": c("SELECT COUNT(*) n FROM alerts WHERE acknowledged=0"),
        "recent_alerts": db.rows_to_dicts(db.q("SELECT a.*, t.symbol FROM alerts a LEFT JOIN tokens t ON t.uid=a.token_uid ORDER BY a.id DESC LIMIT 8")),
        "sources": db.rows_to_dicts(db.q("SELECT * FROM source_status ORDER BY source")),
        "coverage": _coverage_summary(),
        "chains_enabled": config.enabled_chains(s),
        "scheduler_enabled": s.get("scheduler_enabled"),
        "jobs": runner.status(),
        "last_jobs": db.rows_to_dicts(db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 6")),
        "top": best()["rows"][:10],
        "hidden": db.q1("SELECT COUNT(*) n FROM hidden_tokens WHERE active=1")["n"],
        "mcap_types": {r["t"] or "нет оценки": r["n"] for r in db.q("SELECT json_extract(facts,'$.mcap.type') t, COUNT(*) n FROM tokens "
                                                                   "WHERE asset_class='token' GROUP BY t")},
    }


def _coverage_summary() -> list[dict]:
    out = []
    for ck in config.enabled_chains():
        rows = db.rows_to_dicts(db.q("SELECT * FROM coverage WHERE chain=? AND source='geckoterminal:new_pools' ORDER BY id", (ck,)))
        if not rows:
            out.append({"chain": ck, "runs": 0})
            continue
        gaps = [r for r in rows if r["gap_start"]]
        first = min((r["window_start"] for r in rows if r["window_start"]), default=None)
        last = max((r["window_end"] for r in rows if r["window_end"]), default=None)
        # непрерывный участок: от последнего разрыва (или первого запуска) до последнего пула
        cont_from = max([g["gap_end"] for g in gaps], default=first)
        out.append({"chain": ck, "runs": len(rows), "first": first, "last": last, "gaps": len(gaps),
                    "continuous_from": cont_from, "last_run": rows[-1]["run_at"], "last_note": rows[-1]["note"],
                    "gap_list": [{"from": g["gap_start"], "to": g["gap_end"]} for g in gaps[-10:]]})
    return out


@app.get("/api/tokens")
def tokens(search: str = "", chain: str = "", status: str = "", level: str = "", show_service: bool = False,
           only_watch: bool = False, limit: int = Query(500, le=5000), offset: int = 0, sort: str = "default", order: str = "desc",
           mode: str = "default"):
    total, rows = _query_tokens(search, chain, status, level, show_service, only_watch, limit, offset, sort, order, mode=mode)
    return {"total": total, "rows": rows, "modes": MODES}


@app.get("/api/best")
def best():
    """Автоматический рейтинг лучших DeFi-кандидатов (без скрытых пользователем)."""
    from .pipeline import best_candidates
    uids = best_candidates(int(config.load_settings().get("top_candidates_n", 30)))
    _, rows = _query_tokens(uids=uids, limit=len(uids) or 1, show_service=True)
    order = {u: i for i, u in enumerate(uids)}
    rows.sort(key=lambda r: order.get(r["uid"], 999))
    return {"total": len(rows), "rows": rows}


@app.get("/api/hidden")
def hidden_list():
    _, rows = _query_tokens(only_hidden=True, limit=2000, show_service=True, sort="first_seen_at")
    info = {r["token_uid"]: dict(r) for r in db.q("SELECT * FROM hidden_tokens WHERE active=1")}
    for r in rows:
        h = info.get(r["uid"]) or {}
        r.update({"hidden_at": h.get("hidden_at"), "hidden_until": h.get("until"), "hidden_reason": h.get("reason")})
    return {"total": len(rows), "rows": rows}


@app.post("/api/hide/{uid:path}")
def hide(uid: str, payload: dict = Body(default={})):
    """Скрыть из лучших кандидатов (не удаление). Состояние и журнал действия — одна транзакция; повтор — без дублей."""
    days = (payload or {}).get("days")
    until = None
    if days:
        from .util import iso, now_utc as _n
        until = iso(_n() + _dt.timedelta(days=int(days)))
    reason = (payload or {}).get("reason")
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM tokens WHERE uid=?", (uid,)).fetchone():
            raise HTTPException(404, "токен не найден")
        cur = c.execute("SELECT until, reason FROM hidden_tokens WHERE token_uid=? AND active=1", (uid,)).fetchone()
        if cur and cur["until"] == until and (cur["reason"] or None) == (reason or None):
            return {"ok": True, "until": until, "unchanged": True}  # уже скрыт так же — повтор ничего не меняет
        c.execute("INSERT OR REPLACE INTO hidden_tokens(token_uid,hidden_at,until,reason,active) VALUES(?,?,?,?,1)",
                  (uid, now_iso(), until, reason))
        c.execute("INSERT INTO user_actions(token_uid,action,details,at) VALUES(?,?,?,?)",
                  (uid, "hide", dumps({"reason": reason, "until": until}), now_iso()))
    return {"ok": True, "until": until}  # ответ — только после успешного COMMIT


@app.post("/api/unhide/{uid:path}")
def unhide(uid: str):
    with db.tx() as c:
        cur = c.execute("UPDATE hidden_tokens SET active=0 WHERE token_uid=? AND active=1", (uid,))
        if cur.rowcount:
            c.execute("INSERT INTO user_actions(token_uid,action,details,at) VALUES(?,?,?,?)", (uid, "restore", None, now_iso()))
    return {"ok": True, "unchanged": cur.rowcount == 0}


@app.post("/api/focus/{uid:path}")
def focus(uid: str):
    """Открытая карточка: повышенный приоритет обновления (ограничен по времени, не расходует весь бюджет API)."""
    from . import refresh
    refresh.set_focus(uid)
    return {"ok": True}


@app.post("/api/monitoring")
def monitoring(payload: dict = Body(...)):
    s = config.load_settings()
    want = bool(payload.get("enabled"))
    if bool(s.get("scheduler_enabled")) == want:
        return {"enabled": want, "unchanged": True}
    with db.tx() as c:  # журнал действия и сохранение настройки: при ошибке записи настройка не меняется
        c.execute("INSERT INTO user_actions(token_uid,action,details,at) VALUES(NULL,?,NULL,?)",
                  ("monitoring_on" if want else "monitoring_off", now_iso()))
        s["scheduler_enabled"] = want
        config.save_settings(s)  # если запись файла упадёт — транзакция откатится
    return {"enabled": want}


@app.get("/api/live")
def live(since: str = "", uid: str = ""):
    """Лёгкий опрос для живого интерфейса: что изменилось с момента since (UTC ISO)."""
    since = since or "1970-01-01T00:00:00Z"
    out = {"now": now_iso(), "jobs": runner.status(),
           "tokens_changed": db.q1("SELECT COUNT(*) n FROM tokens WHERE updated_at>?", (since,))["n"],
           "new_tokens": db.q1("SELECT COUNT(*) n FROM tokens WHERE first_seen_at>? AND asset_class='token'", (since,))["n"],
           "new_alerts": db.rows_to_dicts(db.q("SELECT a.id,a.title,a.severity,a.token_uid,a.created_at FROM alerts a WHERE a.created_at>? "
                                               "ORDER BY a.id DESC LIMIT 10", (since,))),
           "alerts_open": db.q1("SELECT COUNT(*) n FROM alerts WHERE acknowledged=0")["n"],
           "sources": db.rows_to_dicts(db.q("SELECT source,last_success_at,last_error_at,last_error FROM source_status"))}
    if uid:
        t = db.q1("SELECT updated_at,last_market_at FROM tokens WHERE uid=?", (uid,))
        out["token_updated_at"] = t["updated_at"] if t else None
        out["token_events"] = db.q1("SELECT COUNT(*) n FROM supply_events WHERE token_uid=?", (uid,))["n"]
    return out


@app.get("/api/review")
def review():
    total, rows = _query_tokens(status="MANUAL REVIEW", limit=1000, sort="score_preliminary")
    fm = _filters_map([r["uid"] for r in rows])
    for r in rows:
        r["needs"] = [{"filter": FILTER_NAMES[k], "reason": v["reason"]} for k, v in fm.get(r["uid"], {}).items() if v["status"] == "UNKNOWN"]
    return {"total": total, "rows": rows}


@app.get("/api/token/{uid:path}")
def token(uid: str):
    r = db.q1("SELECT t.*, w.token_uid AS watch, w.note AS watch_note, w.status AS watch_status FROM tokens t LEFT JOIN watchlist w ON w.token_uid=t.uid WHERE t.uid=?", (uid,))
    if not r:
        raise HTTPException(404, "токен не найден")
    r = dict(r)
    f = loads(r["facts"], {}) or {}
    fc = {x["filter_key"]: {**dict(x), "evidence": loads(x["evidence"], [])} for x in db.q("SELECT * FROM filter_current WHERE token_uid=?", (uid,))}
    ch = CHAINS[r["chain"]]
    same_symbol = db.rows_to_dicts(db.q("SELECT uid, chain, address_display, address, name, symbol, protocol_slug, level, status FROM tokens "
                                        "WHERE UPPER(symbol)=UPPER(?) AND uid!=? LIMIT 30", (r["symbol"] or "", uid))) if r["symbol"] else []
    pools = db.rows_to_dicts(db.q("SELECT * FROM pools WHERE base_uid=? OR quote_uid=? ORDER BY pool_created_at", (uid, uid)))
    return {
        "row": _row(r, {k: v for k, v in fc.items()}), "token": {k: v for k, v in r.items() if k != "facts"}, "facts": f,
        "filters": fc, "manual_facts": db.rows_to_dicts(db.q("SELECT * FROM manual_facts WHERE token_uid=? ORDER BY id DESC", (uid,))),
        "tokenomics": [{**dict(x), "params": loads(x["params"], {})} for x in db.q("SELECT * FROM tokenomics_versions WHERE token_uid=? ORDER BY id", (uid,))],
        "labels": db.rows_to_dicts(db.q("SELECT * FROM address_labels WHERE chain=?", (r["chain"],))),
        "filter_history": db.rows_to_dicts(db.q("SELECT filter_key,status,confidence,reason,evaluated_at FROM filter_results WHERE token_uid=? ORDER BY id DESC LIMIT 100", (uid,))),
        "status_history": db.rows_to_dicts(db.q("SELECT * FROM status_history WHERE token_uid=? ORDER BY id DESC LIMIT 100", (uid,))),
        "conflicts": db.rows_to_dicts(db.q("SELECT * FROM conflicts WHERE token_uid=? ORDER BY id DESC LIMIT 20", (uid,))),
        "alerts": db.rows_to_dicts(db.q("SELECT * FROM alerts WHERE token_uid=? ORDER BY id DESC LIMIT 50", (uid,))),
        "pools": pools, "same_symbol": same_symbol,
        "links": {"explorer": ch.explorer_token(r["address_display"] or r["address"]),
                  "geckoterminal": f"https://www.geckoterminal.com/{ch.gt}/tokens/{r['address']}",
                  "dexscreener": f"https://dexscreener.com/{ch.ds}/{r['address']}",
                  "defillama": f"https://defillama.com/protocol/{r['protocol_slug']}" if r["protocol_slug"] else None},
        "supply_events": db.rows_to_dicts(db.q("SELECT * FROM supply_events WHERE token_uid=? ORDER BY block DESC LIMIT 200", (uid,))),
        "series": {m: db.obs_series("token", uid, m) for m in ("price_usd_robust", "liquidity_usd_quality", "total_supply_rpc",
                                                                "circulating_supply", "holders_count")},
        "explorer_tx": ch.explorer_tx("{tx}"), "explorer_addr": ch.explorer_address("{address}"),
        "freshness": _freshness(uid, r, f),
        "hidden": dict(db.q1("SELECT * FROM hidden_tokens WHERE token_uid=? AND active=1", (uid,)) or {}) or None,
        "user_actions": db.rows_to_dicts(db.q("SELECT * FROM user_actions WHERE token_uid=? ORDER BY id DESC LIMIT 50", (uid,))),
        "mcap_history": db.rows_to_dicts(db.q("SELECT assessed_at,mcap_type,value,low,high,price,circ_supply,filter_status,method "
                                              "FROM mcap_assessments WHERE token_uid=? ORDER BY id DESC LIMIT 50", (uid,))),
        "funding_categories": FUNDING_CATEGORIES,
    }


def _freshness(uid: str, r: dict, f: dict) -> list[dict]:
    """Индикаторы актуальности по группам данных: фактическое время последнего успешного получения, задержка, ошибки."""
    from . import refresh
    from .http import source_available
    st = dict(db.q1("SELECT * FROM refresh_state WHERE token_uid=?", (uid,)) or {})
    t = refresh.tiers().get(uid, 4)
    rs = config.load_settings()["refresh_sec"]
    pend = lambda job, **kw: runner.is_pending(job, **kw)
    now = _dt.datetime.now(_dt.timezone.utc)

    def item(name, ok_at, kind, source, err=None, src_time=None, updating=False):
        iv = int((rs.get(kind) or {}).get(str(t), 0))
        dt_ = parse_iso(ok_at)
        age = (now - dt_).total_seconds() if dt_ else None
        avail = source_available(source)[0]
        if updating:
            state = "updating"
        elif not avail or (err and (not dt_ or (age and age > max(iv, 600)))):
            state = "source_down"
        elif dt_ is None:
            state = "none"
        elif iv and age > iv * 3:
            state = "stale"
        elif not iv and age > 86400 * 2:
            state = "stale"
        else:
            state = "fresh"
        return {"name": name, "ok_at": ok_at, "age_sec": age, "interval_sec": iv, "state": state, "error": err, "source": source,
                "source_time": src_time}
    oc = f.get("onchain") or {}
    p = f.get("protocol") or {}
    tvl_last = (p.get("tvl_series") or [[None]])[-1][0] if p.get("tvl_series") else None
    from .util import iso, from_ts
    return [
        item("Цена и ликвидность", st.get("last_quote_ok_at") or r.get("last_market_at"), "quote", "dexscreener",
             st.get("last_quote_error"), updating=any(runner.is_pending("quotes", uids=x) for x in ([[uid]]))),
        item("Пулы, ATH, свечи", r.get("last_deep_at"), "deep", "geckoterminal", updating=pend("deep", uid=uid)),
        item("Фундаментал DefiLlama", st.get("last_fund_at") or r.get("last_enriched_at"), "fundamentals", "defillama",
             src_time=iso(from_ts(tvl_last)) if tvl_last else None, updating=pend("fund", uid=uid)),
        item("Ончейн mint/burn", r.get("last_onchain_at"), "onchain", f"rpc:{r['chain']}", oc.get("error"),
             updating=pend("onchain", uid=uid)),
        item("CoinGecko (circulating)", st.get("last_cg_at") if not (f.get("coingecko") or {}).get("error") else (f.get("coingecko") or {}).get("at"),
             "coingecko", "coingecko", (f.get("coingecko") or {}).get("error"), updating=pend("cg", uid=uid)),
    ]


@app.post("/api/token/{uid:path}/refresh")
def refresh_token(uid: str):
    if not db.q1("SELECT 1 FROM tokens WHERE uid=?", (uid,)):
        raise HTTPException(404)
    return {"job_id": runner.submit("token", uid=uid)}


@app.post("/api/tokens/add")
def add_token(payload: dict = Body(...)):
    ck = payload.get("chain")
    addr = (payload.get("address") or "").strip()
    if ck not in CHAINS:
        raise HTTPException(400, "неизвестная сеть")
    ch = CHAINS[ck]
    if not valid_address(ch.kind, normalize_address(ch.kind, addr)):
        raise HTTPException(400, "некорректный адрес для этой сети")
    uid, new = add_token_manually(ck, addr, payload.get("note", ""))
    db.ex("UPDATE tokens SET address_display=COALESCE(address_display,?) WHERE uid=?", (addr, uid))
    jid = runner.submit("token", uid=uid)
    return {"uid": uid, "new": new, "job_id": jid}


@app.post("/api/token/{uid:path}/facts")
def add_fact(uid: str, payload: dict = Body(...)):
    if not db.q1("SELECT 1 FROM tokens WHERE uid=?", (uid,)):
        raise HTTPException(404)
    key = payload.get("fact_key")
    allowed = {"funding", "tge_date", "utility", "official_link", "team", "catalyst", "value_capture", "audit",
               "circulating_supply", "supply_endpoint_circulating", "protocol_link", "vesting_documented", "note"}
    if key not in allowed:
        raise HTTPException(400, "недопустимый тип факта")
    if key == "funding" and payload.get("category") not in FUNDING_CATEGORIES:
        raise HTTPException(400, "укажите категорию финансирования")
    from .util import fnum
    with db.tx():  # факт и запись в журнал действий — атомарно
        db.ex("INSERT INTO manual_facts(token_uid,fact_key,value,category,evidence_url,source_type,note,entered_by,entered_at,amount_usd,valuation_usd) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (uid, key, payload.get("value"), payload.get("category"), payload.get("evidence_url"), payload.get("source_type") or "user",
               payload.get("note"), "user", now_iso(), fnum(payload.get("amount_usd")), fnum(payload.get("valuation_usd"))))
        db.ex("INSERT INTO user_actions(token_uid,action,details,at) VALUES(?,?,?,?)", (uid, "add_fact", dumps({"fact_key": key}), now_iso()))
    pipeline.evaluate(uid, old_facts=None)
    return {"ok": True}


@app.post("/api/facts/{fid}/deactivate")
def deactivate_fact(fid: int):
    r = db.q1("SELECT token_uid FROM manual_facts WHERE id=?", (fid,))
    if not r:
        raise HTTPException(404)
    db.ex("UPDATE manual_facts SET active=0 WHERE id=?", (fid,))
    pipeline.evaluate(r["token_uid"], old_facts=None)
    return {"ok": True}


@app.post("/api/token/{uid:path}/tokenomics")
def add_tokenomics(uid: str, payload: dict = Body(...)):
    params = payload.get("params") or {}
    if isinstance(params, str):
        params = loads(params, None)
        if params is None:
            raise HTTPException(400, "params должен быть JSON")
    cur = 1 if payload.get("is_current") else 0
    with db.tx():  # снятие флага «актуальная» и вставка новой версии — атомарно
        if cur:
            db.ex("UPDATE tokenomics_versions SET is_current=0 WHERE token_uid=?", (uid,))
        db.ex("INSERT INTO tokenomics_versions(token_uid,version_label,published_at,source_url,source_type,is_current,params,note,entered_at) "
              "VALUES(?,?,?,?,?,?,?,?,?)", (uid, payload.get("version_label"), payload.get("published_at"), payload.get("source_url"),
                                            payload.get("source_type") or "user", cur, dumps(params), payload.get("note"), now_iso()))
    if cur:
        from .alerts import emit
        emit(uid, "tokenomics_changed", "high", f"Новая актуальная версия токеномики: {payload.get('version_label')}",
             "пользователь отметил новую версию токеномики как актуальную", {"published_at": payload.get("published_at")},
             [payload.get("source_url")], dedup=f"{uid}:tn:{payload.get('version_label')}")
    return {"ok": True}


@app.post("/api/tokenomics/{vid}/current")
def set_current_tokenomics(vid: int):
    r = db.q1("SELECT token_uid, version_label FROM tokenomics_versions WHERE id=?", (vid,))
    if not r:
        raise HTTPException(404)
    with db.tx():
        db.ex("UPDATE tokenomics_versions SET is_current=0 WHERE token_uid=?", (r["token_uid"],))
        db.ex("UPDATE tokenomics_versions SET is_current=1 WHERE id=?", (vid,))
    return {"ok": True}


@app.post("/api/labels")
def add_label(payload: dict = Body(...)):
    ck = payload.get("chain")
    if ck not in CHAINS:
        raise HTTPException(400)
    ch = CHAINS[ck]
    circ = payload.get("circulation") if payload.get("circulation") in ("excluded", "included", "uncertain") else None
    if circ == "excluded" and not (payload.get("circulation_evidence") or payload.get("source_url")):
        raise HTTPException(400, "для исключения из обращения нужна ссылка/описание доказательства блокировки")
    db.ex("INSERT OR REPLACE INTO address_labels(chain,address,label,kind,source_url,source_type,token_uid,circulation,circulation_evidence) "
          "VALUES(?,?,?,?,?,?,?,?,?)",
          (ck, normalize_address(ch.kind, payload.get("address") or ""), payload.get("label"), payload.get("kind"),
           payload.get("source_url"), payload.get("source_type") or "user", payload.get("token_uid"), circ, payload.get("circulation_evidence")))
    return {"ok": True}


@app.get("/api/watchlist")
def watchlist():
    total, rows = _query_tokens(only_watch=True, limit=1000, show_service=True)
    notes = {r["token_uid"]: dict(r) for r in db.q("SELECT * FROM watchlist")}
    for r in rows:
        r["watch_note"] = (notes.get(r["uid"]) or {}).get("note")
        r["watch_added"] = (notes.get(r["uid"]) or {}).get("added_at")
        r["watch_status"] = (notes.get(r["uid"]) or {}).get("status")
    return {"total": total, "rows": rows}


WATCH_STATUSES = {"studying": "Изучаю", "watching": "Наблюдаю", "position": "Есть позиция", "questionable": "Гипотеза под вопросом",
                  "archive": "Архив"}


@app.post("/api/watchlist/{uid:path}")
def watch_add(uid: str, payload: dict = Body(default={})):
    st = (payload or {}).get("status") or "watching"
    if st not in WATCH_STATUSES:
        raise HTTPException(400, "неизвестный статус")
    note = (payload or {}).get("note")
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM tokens WHERE uid=?", (uid,)).fetchone():
            raise HTTPException(404, "токен не найден")
        ex = c.execute("SELECT status FROM watchlist WHERE token_uid=?", (uid,)).fetchone()
        if ex and ex["status"] == st and note is None:
            return {"ok": True, "unchanged": True}
        if ex:
            c.execute("UPDATE watchlist SET status=?, note=COALESCE(?, note) WHERE token_uid=?", (st, note, uid))
        else:
            c.execute("INSERT INTO watchlist(token_uid,added_at,note,status) VALUES(?,?,?,?)", (uid, now_iso(), note, st))
        c.execute("INSERT INTO user_actions(token_uid,action,details,at) VALUES(?,?,?,?)", (uid, "watch", st, now_iso()))
    return {"ok": True}


@app.delete("/api/watchlist/{uid:path}")
def watch_del(uid: str):
    with db.tx() as c:
        cur = c.execute("DELETE FROM watchlist WHERE token_uid=?", (uid,))
        if cur.rowcount:
            c.execute("INSERT INTO user_actions(token_uid,action,details,at) VALUES(?,?,?,?)", (uid, "unwatch", None, now_iso()))
    return {"ok": True}


@app.get("/api/alerts")
def get_alerts(limit: int = 300, only_open: bool = False):
    sql = "SELECT a.*, t.symbol, t.chain FROM alerts a LEFT JOIN tokens t ON t.uid=a.token_uid"
    if only_open:
        sql += " WHERE a.acknowledged=0"
    return {"rows": db.rows_to_dicts(db.q(sql + " ORDER BY a.id DESC LIMIT ?", (limit,)))}


@app.post("/api/alerts/{aid}/ack")
def ack(aid: int):
    db.ex("UPDATE alerts SET acknowledged=1 WHERE id=?", (aid,))
    return {"ok": True}


@app.get("/api/tokenomics")
def tokenomics_overview():
    rows = db.rows_to_dicts(db.q(
        "SELECT t.uid, t.symbol, t.chain, t.address_display, t.address, "
        "(SELECT COUNT(*) FROM tokenomics_versions v WHERE v.token_uid=t.uid) versions, "
        "(SELECT COUNT(*) FROM supply_events e WHERE e.token_uid=t.uid AND e.kind='mint') mints, "
        "t.last_onchain_at "
        "FROM tokens t WHERE t.uid IN (SELECT token_uid FROM tokenomics_versions) OR t.uid IN (SELECT token_uid FROM supply_events) "
        "OR t.last_onchain_at IS NOT NULL ORDER BY t.symbol"))
    for r in rows:
        f = loads(db.q1("SELECT facts FROM tokens WHERE uid=?", (r["uid"],))["facts"], {}) or {}
        oc = f.get("onchain") or {}
        r["total_supply"] = oc.get("total_supply")
        r["minted"], r["burned"] = oc.get("minted"), oc.get("burned")  # из целочисленных сумм (onchain.raw_totals)
        r["reconciliation"] = oc.get("reconciliation")
        r["circulating"] = (f.get("circulating_official") or {}).get("value")
        r["max_supply"] = (f.get("max_supply") or {}).get("value")
    return {"rows": rows}


@app.get("/api/sources")
def sources():
    s = config.load_settings()
    chains = []
    for k, c in CHAINS.items():
        chains.append({"key": k, "name": c.name, "kind": c.kind, "support": c.support, "enabled": k in config.enabled_chains(s),
                       "providers": [p for p in ("GeckoTerminal" if c.gt else None, "DEX Screener" if c.ds else None,
                                                 "DefiLlama" if c.llama else None, "RPC" if c.rpc else None) if p],
                       "rpc": c.rpc, "notes": c.notes,
                       "last_success": (db.q1("SELECT MAX(run_at) m FROM coverage WHERE chain=? AND note NOT LIKE '%ошибка%'", (k,)) or {})["m"],
                       "tokens": db.q1("SELECT COUNT(*) n FROM tokens WHERE chain=? AND asset_class='token'", (k,))["n"],
                       "health": loads(db.get_progress(f"chain_health:{k}"), None)})
    return {
        "sources": db.rows_to_dicts(db.q("SELECT * FROM source_status ORDER BY source")),
        "session_budget": http.budget, "rate_limits": s["rate_limits_per_min"],
        "coverage": _coverage_summary(),
        "coverage_log": db.rows_to_dicts(db.q("SELECT * FROM coverage ORDER BY id DESC LIMIT 60")),
        "chains": chains,
        "errors": db.rows_to_dicts(db.q("SELECT * FROM error_log ORDER BY id DESC LIMIT 50")),
        "limitations": LIMITATIONS,
    }


LIMITATIONS = [
    "GeckoTerminal new_pools: 20 пулов/стр., до 10 страниц; бесплатно ~10 запросов/мин. На Solana ~20 пулов за 40 с — покрытие только частичное.",
    "Исторический поиск за 90 дней: только топ пулов по объёму 24ч и протоколы DefiLlama; неактивные старые пулы не покрываются.",
    "DEX Screener не имеет публичного списка всех новых пар; профили/бусты — промо и выключены по умолчанию.",
    "Публичные RPC без архивного состояния: исторический totalSupply восстанавливается из событий Transfer.",
    "Blockscout API (Robinhood) закрыт Cloudflare-проверкой из облачной сети — обход не выполняется; верификация кода = UNKNOWN.",
    "Финансирование (VC) — аналитика рисков, не фильтр; данные о раундах — из DefiLlama raises и ручных записей.",
    "CoinGecko без ключа: ~5 запросов/мин — circulating supply запрашивается только для приоритетных токенов.",
    "Даты в интерфейсе показываются в локальной зоне браузера; в базе — UTC.",
]


@app.post("/api/scan")
def scan(payload: dict = Body(default={})):
    kw = {}
    if payload and payload.get("chains"):
        kw["chains"] = [c for c in payload["chains"] if c in CHAINS]
    return {"job_id": runner.submit("scan", **kw)}


@app.post("/api/jobs/{job}")
def run_job(job: str):
    if job not in ("refresh", "fundamentals", "backup", "reevaluate", "weekly", "scan"):
        raise HTTPException(400)
    return {"job_id": runner.submit(job)}


@app.get("/api/jobs")
def jobs_list():
    return {"status": runner.status(), "recent": db.rows_to_dicts(db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 30"))}


@app.get("/api/settings")
def get_settings():
    return {"settings": config.load_settings(), "defaults": config.DEFAULTS, "secrets": config.secrets_status(),
            "chains": {k: {"name": c.name, "support": c.support} for k, c in CHAINS.items()},
            "enabled_chains": config.enabled_chains(), "backups": backup.list_backups(), "data_dir": str(config.DATA_DIR)}


@app.post("/api/settings")
def post_settings(payload: dict = Body(...)):
    s = config.save_settings(payload)
    http.reset_limiters()
    return {"settings": s}


@app.post("/api/secrets")
def post_secret(payload: dict = Body(...)):
    name = payload.get("name")
    if name not in config.secrets_status():
        raise HTTPException(400)
    config.set_secret(name, payload.get("value") or None)
    return {"secrets": config.secrets_status()}


@app.get("/api/export.{fmt}")
def export_file(fmt: str, search: str = "", chain: str = "", status: str = "", level: str = ""):
    _, rows = _query_tokens(search, chain, status, level, limit=100000)
    if fmt == "csv":
        return Response(export.to_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=defi-alpha-scanner.csv"})
    if fmt == "xlsx":
        return Response(export.to_xlsx(rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=defi-alpha-scanner.xlsx"})
    raise HTTPException(400)


@app.get("/api/diagnostics")
def diag(network: bool = False):
    return diagnostics.run(network=network)


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


from .api_analytics import router as _analytics_router  # noqa: E402 — 2.1: аналитика (выключена по умолчанию)

app.include_router(_analytics_router)


@app.get("/")
def index():
    return FileResponse(str(WEB / "index.html"))
