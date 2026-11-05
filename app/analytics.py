"""Аналитические модули 2.1 (Fundamental Momentum, Emission Pressure): общая инфраструктура.

Принципы изоляции от сканера 2.0.1:
- модули ВЫКЛЮЧЕНЫ по умолчанию; пока ни один не включался, база не изменяется (таблицы создаются при первом включении);
- работают в отдельной очереди «analytics» с низким приоритетом и бюджетом времени, откладываются при перегрузке основных очередей;
- этап 1 НЕ выполняет сетевых запросов (запрет проверяется в http.request_json) — лимиты источников не расходуются;
- читают накопленные данные; пишут ТОЛЬКО в собственные таблицы an_* и address_roles, короткими транзакциями db.tx();
- первичные данные (supply_events, observations, tokenomics_versions, facts) не изменяются;
- каждый результат хранит версию метода, снимок входных данных (и его хеш), время расчёта и источники;
  пересчёт выполняется, только если изменились входные данные, версия метода или метки адресов.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

from . import config, db
from .util import dumps, loads, now_iso

log = logging.getLogger("das.analytics")

MODULES = {"momentum": "Фундаментальный импульс", "emission": "Эмиссионное давление"}
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS an_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module TEXT NOT NULL, token_uid TEXT NOT NULL, method_version TEXT NOT NULL,
    computed_at TEXT NOT NULL, input_hash TEXT NOT NULL, input_snapshot TEXT,
    status TEXT, verdict TEXT, confidence TEXT,
    block_from INTEGER, block_to INTEGER, coverage TEXT,
    summary TEXT, result TEXT, sources TEXT
);
CREATE INDEX IF NOT EXISTS ix_an_results ON an_results(module, token_uid, id);
CREATE TABLE IF NOT EXISTS an_state (
    module TEXT NOT NULL, token_uid TEXT NOT NULL, input_hash TEXT, last_result_id INTEGER,
    last_run_at TEXT, last_status TEXT, last_error TEXT, PRIMARY KEY(module, token_uid)
);
CREATE TABLE IF NOT EXISTS an_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT, started_at TEXT, finished_at TEXT,
    processed INTEGER, recomputed INTEGER, unchanged INTEGER, errors INTEGER, stopped_reason TEXT
);
-- реестр подтверждённых ролей адресов (отдельно от address_labels 2.0: её смысл не меняется, она читается как источник)
CREATE TABLE IF NOT EXISTS address_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL, token_uid TEXT, address TEXT NOT NULL, role TEXT NOT NULL,
    source_url TEXT, source_type TEXT, evidence TEXT, confirmed_at TEXT, confidence TEXT,
    entered_by TEXT, entered_at TEXT, active INTEGER DEFAULT 1, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_roles_addr ON address_roles(chain, address);
"""
_TABLES = ("an_results", "an_state", "an_runs", "address_roles")

# ------------------------------------------------------------------ роли адресов
ROLES = {
    "minter": "Minter (контракт выпуска)", "gauge": "Gauge (LP-награды)", "lp_reward_distributor": "Распределитель LP-наград",
    "emission_contract": "Эмиссионный контракт (назначение не уточнено)", "rebase_distributor": "Распределитель ребейсов",
    "ve_escrow": "ve-escrow (блокировки)", "treasury": "Казначейство", "team": "Команда", "team_vesting": "Вестинг команды",
    "seeder": "Seeder (стартовая ликвидность)", "community": "Сообщество", "airdrop": "Points/airdrop",
    "liquidity": "Ликвидность", "strategic_reserve": "Стратегический резерв", "liquidity_pool": "Пул ликвидности (DEX)",
    "dex_router": "DEX-роутер", "cex_deposit": "Депозит CEX", "burn": "Адрес сжигания", "unknown": "Неизвестный адрес",
}
GENESIS_ROLES = {"team", "treasury", "seeder", "community", "airdrop", "liquidity", "strategic_reserve", "team_vesting", "ve_escrow"}
LP_ROLES = {"gauge", "lp_reward_distributor"}
OTHER_EMISSION_ROLES = {"emission_contract", "rebase_distributor"}
TRADING_ROLES = {"liquidity_pool", "dex_router", "cex_deposit"}
CONFIDENCE = {"confirmed": "подтверждено", "probable": "вероятно", "claimed": "заявлено, не проверено"}
# address_labels 2.0 (kind) → роль реестра; метка читается, но не изменяется
LEGACY_KIND = {"escrow": "ve_escrow", "treasury": "treasury", "team": "team", "minter": "minter", "gauge": "gauge",
               "emissions": "emission_contract", "vesting": "team_vesting", "pool": "liquidity_pool", "lp": "liquidity_pool",
               "cex": "cex_deposit", "burn": "burn", "seeder": "seeder", "distributor": "lp_reward_distributor"}


def _legacy_confidence(source_type: str | None) -> str:
    return "confirmed" if source_type in ("official_docs", "onchain") else "claimed"


def roles_for(chain: str, token_uid: str | None) -> dict[str, dict]:
    """Действующие роли адресов для токена в сети. Роль, указанная для одного токена, к другим токенам не переносится;
    роль без токена (token_uid пусто — например, депозит CEX) действует только в этой сети.
    Ручное уточнение в реестре имеет приоритет над меткой 2.0; разные активные роли в реестре — конфликт (показывается)."""
    out: dict[str, dict] = {}
    for r in db.q("SELECT * FROM address_labels WHERE chain=? AND (token_uid=? OR token_uid IS NULL)", (chain, token_uid)):
        role = LEGACY_KIND.get((r["kind"] or "").lower())
        if not role:
            continue
        out[r["address"]] = {"address": r["address"], "role": role, "label": r["label"], "confidence": _legacy_confidence(r["source_type"]),
                             "source_url": r["source_url"], "source_type": r["source_type"], "origin": "address_labels (2.0)",
                             "token_specific": r["token_uid"] is not None, "conflict": None, "entries": []}
    if not schema_present():
        return out
    reg: dict[str, list[dict]] = {}
    for r in db.q("SELECT * FROM address_roles WHERE chain=? AND active=1 AND (token_uid=? OR token_uid IS NULL) ORDER BY id",
                  (chain, token_uid)):
        reg.setdefault(r["address"], []).append(dict(r))
    for a, ents in reg.items():
        # роль конкретно для этого токена важнее роли «для всей сети»
        spec = [e for e in ents if e["token_uid"]] or ents
        roles = sorted({e["role"] for e in spec})
        last = spec[-1]
        prev = out.get(a)
        out[a] = {"address": a, "role": last["role"] if len(roles) == 1 else "unknown",
                  "label": last.get("note") or ROLES.get(last["role"]), "confidence": last.get("confidence") or "claimed",
                  "source_url": last.get("source_url"), "source_type": last.get("source_type"), "origin": "address_roles",
                  "token_specific": bool(last["token_uid"]), "entries": spec,
                  "conflict": (f"разные роли в реестре: {', '.join(roles)} — роль не применяется до уточнения" if len(roles) > 1 else None),
                  "refines_legacy": ({"role": prev["role"], "label": prev["label"]} if prev and prev["role"] != last["role"] else None)}
    return out


def roles_hash(chain: str, token_uid: str | None) -> str:
    rs = roles_for(chain, token_uid)
    return sha({a: [r["role"], r["confidence"], bool(r["conflict"])] for a, r in sorted(rs.items())})


def add_role(chain: str, token_uid: str | None, address: str, role: str, *, source_url: str | None, source_type: str | None,
             evidence: str | None, confidence: str, note: str | None = None, entered_by: str = "user") -> int:
    if role not in ROLES:
        raise ValueError(f"неизвестная роль: {role}")
    if confidence not in CONFIDENCE:
        raise ValueError("достоверность: confirmed | probable | claimed")
    if confidence == "confirmed" and not (source_url or evidence):
        raise ValueError("для подтверждённой роли нужна ссылка или описание доказательства")
    ensure_schema()
    with db.tx() as c:
        cur = c.execute("INSERT INTO address_roles(chain,token_uid,address,role,source_url,source_type,evidence,confirmed_at,confidence,"
                        "entered_by,entered_at,active,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (chain, token_uid, address, role, source_url, source_type or "user", evidence,
                         now_iso() if confidence == "confirmed" else None, confidence, entered_by, now_iso(), note))
        c.execute("INSERT INTO user_actions(token_uid,action,details,at) VALUES(?,?,?,?)",
                  (token_uid, "address_role_add", dumps({"chain": chain, "address": address, "role": role, "confidence": confidence}), now_iso()))
        return cur.lastrowid


def deactivate_role(role_id: int) -> bool:
    ensure_schema()
    with db.tx() as c:
        r = c.execute("SELECT token_uid, address, role, active FROM address_roles WHERE id=?", (role_id,)).fetchone()
        if not r:
            return False
        if r["active"]:
            c.execute("UPDATE address_roles SET active=0 WHERE id=?", (role_id,))
            c.execute("INSERT INTO user_actions(token_uid,action,details,at) VALUES(?,?,?,?)",
                      (r["token_uid"], "address_role_deactivate", dumps({"id": role_id, "address": r["address"], "role": r["role"]}), now_iso()))
        return True


# ------------------------------------------------------------------ схема (создаётся лениво)
_schema_ok: dict[str, bool] = {}
_schema_lock = threading.Lock()


def schema_present() -> bool:
    key = str(db.DB_PATH)
    if _schema_ok.get(key):
        return True
    r = db.q1("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name IN (%s)" % ",".join("?" * len(_TABLES)), _TABLES)
    ok = bool(r and r["n"] == len(_TABLES))
    if ok:
        _schema_ok[key] = True
    return ok


def ensure_schema() -> None:
    """Создать аналитические таблицы (идемпотентно). Вызывается только при включении модуля или ручном вводе роли."""
    if schema_present():
        return
    with _schema_lock:
        if schema_present():
            return
        with db.tx() as c:
            for chunk in SCHEMA.split(";"):
                stmt = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith("--")).strip()
                if stmt:
                    c.execute(stmt)
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('analytics_schema',?)", (str(SCHEMA_VERSION),))
        _schema_ok[str(db.DB_PATH)] = True


# ------------------------------------------------------------------ настройки
def settings() -> dict:
    return config.load_settings().get("analytics") or {}


def enabled(module: str) -> bool:
    return bool(settings().get(f"{module}_enabled"))


def set_enabled(module: str, on: bool) -> dict:
    if module not in MODULES:
        raise ValueError(module)
    if on:
        ensure_schema()
    s = config.load_settings()
    s.setdefault("analytics", {})[f"{module}_enabled"] = bool(on)
    config.save_settings(s)
    if not on and not any(enabled(m) for m in MODULES):
        from .jobs import runner
        runner.drop_queued("analytics")
    return settings()


# ------------------------------------------------------------------ запрет сети в аналитике этапа 1
_net = threading.local()


@contextmanager
def no_network():
    prev = getattr(_net, "forbid", False)
    _net.forbid = True
    try:
        yield
    finally:
        _net.forbid = prev


def network_forbidden() -> bool:
    return getattr(_net, "forbid", False)


# ------------------------------------------------------------------ выполнение
def sha(obj: Any) -> str:
    return hashlib.sha256(dumps(obj).encode()).hexdigest()


def _impl(module: str):
    if module == "momentum":
        from . import momentum as m
    elif module == "emission":
        from . import emission_pressure as m
    else:
        raise ValueError(module)
    return m


def run_module(module: str, *, deadline_s: float | None = None, prog: Callable[[str], None] | None = None,
               uids: list[str] | None = None, force: bool = False, stop: threading.Event | None = None) -> dict:
    """Один проход модуля по токенам (старейшие результаты — первыми) в пределах бюджета времени.
    Ошибка расчёта одного токена записывается в его состояние и не прерывает остальные."""
    ensure_schema()
    impl = _impl(module)
    budget = float(deadline_s if deadline_s is not None else settings().get("task_max_seconds", 20))
    t_end = time.monotonic() + budget
    started = now_iso()
    st = {"module": module, "processed": 0, "recomputed": 0, "unchanged": 0, "errors": 0, "stopped_reason": None}
    cand = uids if uids is not None else impl.candidates()
    state = {r["token_uid"]: dict(r) for r in db.q("SELECT * FROM an_state WHERE module=?", (module,))}
    cand = sorted(cand, key=lambda u: (state.get(u) or {}).get("last_run_at") or "")
    pause_ratio = float(settings().get("pause_ratio", 3.0))
    waiter = stop or threading.Event()
    with no_network():
        for uid in cand:
            if time.monotonic() > t_end:
                st["stopped_reason"] = "бюджет времени исчерпан — продолжится в следующем задании"
                break
            if uids is None and not enabled(module):
                st["stopped_reason"] = "модуль выключен пользователем"
                break
            if stop is not None and stop.is_set():
                st["stopped_reason"] = "остановка программы"
                break
            st["processed"] += 1
            try:
                snap, data = impl.load(uid)  # одно чтение: снимок и данные расчёта согласованы
                h = sha({"v": impl.VERSION, "snap": snap})
                prev = state.get(uid) or {}
                if not force and prev.get("input_hash") == h and prev.get("last_status") == "ok":
                    st["unchanged"] += 1
                    continue
                t_c = time.monotonic()
                res = impl.compute(uid, data)  # только чтение; вне транзакции
                _store(module, uid, impl.VERSION, h, snap, res)
                st["recomputed"] += 1
                # ограничение доли процессорного времени: Python исполняет потоки по очереди (GIL), поэтому
                # непрерывный расчёт отнимал бы время у котировок и обнаружения
                spent = time.monotonic() - t_c
                if pause_ratio > 0 and spent > 0.002:
                    waiter.wait(min(5.0, spent * pause_ratio))
            except Exception as e:  # noqa: BLE001 — ошибка аналитики одного токена не влияет на остальные и на сканер
                st["errors"] += 1
                db.log_error(f"analytics:{module}", f"{uid}: {type(e).__name__}: {e}")
                try:
                    with db.tx() as c:
                        c.execute("INSERT INTO an_state(module,token_uid,last_run_at,last_status,last_error) VALUES(?,?,?,?,?) "
                                  "ON CONFLICT(module,token_uid) DO UPDATE SET last_run_at=excluded.last_run_at, "
                                  "last_status='error', last_error=excluded.last_error",
                                  (module, uid, now_iso(), "error", f"{type(e).__name__}: {e}"[:1000]))
                except Exception:  # noqa: BLE001
                    pass
            if prog and st["processed"] % 10 == 0:
                prog(f"{MODULES[module]}: {st['processed']}/{len(cand)}")
    try:
        with db.tx() as c:
            c.execute("INSERT INTO an_runs(module,started_at,finished_at,processed,recomputed,unchanged,errors,stopped_reason) "
                      "VALUES(?,?,?,?,?,?,?,?)", (module, started, now_iso(), st["processed"], st["recomputed"], st["unchanged"],
                                                    st["errors"], st["stopped_reason"]))
    except Exception:  # noqa: BLE001 — журнал прогонов вспомогательный
        pass
    return st


def _store(module: str, uid: str, version: str, h: str, snap: dict, res: dict) -> None:
    keep = int(settings().get("keep_results_per_token", 60))
    with db.tx() as c:
        cur = c.execute("INSERT INTO an_results(module,token_uid,method_version,computed_at,input_hash,input_snapshot,status,verdict,"
                        "confidence,block_from,block_to,coverage,summary,result,sources) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (module, uid, version, now_iso(), h, dumps(snap), res.get("status"), res.get("verdict"), res.get("confidence"),
                         res.get("block_from"), res.get("block_to"), dumps(res.get("coverage")), dumps(res.get("summary")),
                         dumps(res), dumps(res.get("sources"))))
        rid = cur.lastrowid
        c.execute("INSERT INTO an_state(module,token_uid,input_hash,last_result_id,last_run_at,last_status,last_error) "
                  "VALUES(?,?,?,?,?,'ok',NULL) ON CONFLICT(module,token_uid) DO UPDATE SET input_hash=excluded.input_hash, "
                  "last_result_id=excluded.last_result_id, last_run_at=excluded.last_run_at, last_status='ok', last_error=NULL",
                  (module, uid, h, rid, now_iso()))
        # история ограничена: старые производные результаты удаляются (первичные данные не затрагиваются)
        c.execute("DELETE FROM an_results WHERE module=? AND token_uid=? AND id NOT IN "
                  "(SELECT id FROM an_results WHERE module=? AND token_uid=? ORDER BY id DESC LIMIT ?)", (module, uid, module, uid, keep))
    if module == "momentum" and res.get("divergence") and settings().get("alerts", True):
        _divergence_alert(uid, res)


def _divergence_alert(uid: str, res: dict) -> None:
    from .alerts import emit
    from .util import now_utc
    d = res["divergence"]
    tok = db.q1("SELECT symbol FROM tokens WHERE uid=?", (uid,))
    emit(uid, "momentum_divergence", "medium",
         f"{(tok['symbol'] if tok else uid)}: экономика протокола улучшается быстрее рыночной оценки ({d['window']} дн.)",
         d["text"], d["values"], ["DefiLlama", "GeckoTerminal OHLCV"],
         limitations="событие для исследования, не рекомендация; долларовый TVL не равен чистому притоку капитала",
         dedup=f"{uid}:momentum_divergence:{d['window']}:{now_utc().strftime('%G-W%V')}")


def latest(module: str, uid: str) -> dict | None:
    if not schema_present():
        return None
    r = db.q1("SELECT * FROM an_results WHERE module=? AND token_uid=? ORDER BY id DESC LIMIT 1", (module, uid))
    if not r:
        return None
    d = dict(r)
    d["result"] = loads(d["result"], {})
    d["input_snapshot"] = loads(d["input_snapshot"], {})
    st = db.q1("SELECT last_status, last_error, last_run_at FROM an_state WHERE module=? AND token_uid=?", (module, uid))
    d["state"] = dict(st) if st else None
    return d


def history(module: str, uid: str, limit: int = 60) -> list[dict]:
    if not schema_present():
        return []
    return db.rows_to_dicts(db.q("SELECT id, computed_at, method_version, status, verdict, confidence, summary, input_hash FROM an_results "
                                 "WHERE module=? AND token_uid=? ORDER BY id DESC LIMIT ?", (module, uid, limit)))


def status() -> dict:
    s = settings()
    out = {"modules": {m: {"name": n, "enabled": bool(s.get(f"{m}_enabled"))} for m, n in MODULES.items()},
           "schema_present": schema_present(), "deferred": dict(_deferred)}
    if out["schema_present"]:
        for m in MODULES:
            r = db.q1("SELECT * FROM an_runs WHERE module=? ORDER BY id DESC LIMIT 1", (m,))
            e = db.q1("SELECT COUNT(*) n FROM an_state WHERE module=? AND last_status='error'", (m,))
            n = db.q1("SELECT COUNT(*) n FROM an_state WHERE module=? AND last_status='ok'", (m,))
            out["modules"][m].update({"last_run": dict(r) if r else None, "tokens_ok": n["n"], "tokens_error": e["n"]})
    return out


# ------------------------------------------------------------------ планирование (вызывается планировщиком jobs)
_deferred: dict[str, Any] = {"count": 0, "last_at": None, "reason": None}
_last_submit: dict[str, float] = {}


def plan(runner, sub: Callable[..., None]) -> None:
    s = settings()
    mods = [m for m in MODULES if s.get(f"{m}_enabled")]
    if not mods:
        runner.drop_queued("analytics")
        return
    backlog = sum(runner.lanes[n].size() for n in ("fast", "main") if n in runner.lanes)
    if backlog > int(s.get("defer_if_backlog", 60)):
        _deferred.update(count=_deferred["count"] + 1, last_at=now_iso(),
                         reason=f"основные очереди загружены ({backlog} задач) — аналитика отложена")
        return
    for m in mods:
        iv = float((s.get("interval_min") or {}).get(m, 60)) * 60
        if time.monotonic() - _last_submit.get(m, -1e12) >= iv:
            _last_submit[m] = time.monotonic()
            sub("analytics", 6, module=m)


def task(prog, module: str, uid: str | None = None, force: bool = False) -> dict:
    """Задание очереди «analytics» (плановое — по всем токенам; по кнопке «Пересчитать» — один токен)."""
    if not enabled(module):
        return {"skipped": "модуль выключен"}
    from .jobs import runner
    return run_module(module, prog=prog, stop=runner.stop_flag, uids=[uid] if uid else None, force=force)
