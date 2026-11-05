"""Фоновый мониторинг v2: несколько независимых очередей (lanes) и автоматический планировщик.

Очереди (каждая — свой поток, задачи внутри — по приоритету, FIFO при равном приоритете):
  fast  — котировки: DEX Screener + DefiLlama coins (дёшево, ~1–3 мин для приоритетных токенов);
  main  — обнаружение (GeckoTerminal), углублённый анализ, фундаментал DefiLlama;
  chain — ончейн (RPC): инкрементальный скан mint/burn с перекрытием, необращающиеся адреса;
  aux   — CoinGecko (жёсткий лимит), резервные копии, пересчёты.
Долгий ончейн-скан не блокирует котировки и обнаружение: они в других очередях; лимиты запросов — общие на источник.
Одинаковые задачи не дублируются; длина каждой очереди ограничена; планировщик пропускает источники,
приостановленные circuit breaker'ом после серии отказов.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from typing import Any, Callable

from . import backup, config, db, discovery, enrich, fundamentals, http, onchain, pipeline, refresh
from .alerts import check_sources
from .http import SourceError
from .util import dumps, loads, now_iso, now_utc, parse_iso

log = logging.getLogger("das.jobs")

JOB_NAMES = {"scan": "Сканирование", "discover": "Новые пулы", "backfill": "Исторический добор", "quotes": "Котировки",
             "deep": "Углублённый анализ", "fund": "Фундаментал", "onchain": "Ончейн", "cg": "CoinGecko",
             "chain_health": "Метрики сети", "refresh": "Обновление кандидатов", "fundamentals": "Фундаментал (все)",
             "token": "Анализ токена", "backup": "Резервная копия", "weekly": "Недельный снимок",
             "reevaluate": "Пересчёт фильтров", "protocols": "Список протоколов", "analytics": "Аналитика 2.1", "fables_index": "Индексатор FABLES"}
LANE_OF = {"quotes": "fast", "discover": "main", "backfill": "main", "deep": "main", "fund": "main", "chain_health": "main",
           "protocols": "main", "scan": "main", "refresh": "main", "fundamentals": "main", "weekly": "main", "token": "main",
           "onchain": "chain", "cg": "aux", "backup": "aux", "reevaluate": "aux",
           "analytics": "analytics", "fables_index": "analytics"}  # 2.1: отдельная очередь — аналитика не занимает очереди сбора данных
NO_LOG = {"quotes"}  # частые задачи не засоряют журнал заданий
SOURCES_OF = {"quotes": ["dexscreener"], "discover": ["geckoterminal"], "backfill": ["geckoterminal", "defillama"],
              "deep": ["geckoterminal"], "fund": ["defillama"], "chain_health": ["defillama"], "cg": ["coingecko"],
              "protocols": ["defillama"]}


class Lane:
    def __init__(self, name: str, runner: "Runner"):
        self.name = name
        self.runner = runner
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.keys: dict[str, dict] = {}
        self.current: dict | None = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, name=f"das-{name}", daemon=True)
        self.done = 0

    def size(self) -> int:
        with self.lock:
            return len(self.keys)

    def _loop(self) -> None:
        """Цикл очереди. Никакая ошибка (в т.ч. невозможность записать статус в БД) не завершает поток."""
        while not self.runner.stop_flag.is_set():
            try:
                self._one()
            except BaseException as e:  # noqa: BLE001 — последний рубеж: поток очереди не должен погибнуть
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                db._file_log(f"lane:{self.name}", "".join(traceback.format_exception(e))[-2000:])
                self.runner.stop_flag.wait(1.0)

    def _one(self) -> None:
        try:
            _, _, j = self.q.get(timeout=0.5)
        except queue.Empty:
            return
        with self.lock:
            self.keys.pop(j["key"], None)
            self.current = {**j, "progress": "старт", "started_at": now_iso()}
        log_it = j["job"] not in NO_LOG and j["id"]
        if log_it:
            _safe_job_update("UPDATE jobs SET started_at=?, status='running' WHERE id=?", (now_iso(), j["id"]))

        def prog(m: str, jid=j["id"], li=log_it):
            with self.lock:
                if self.current:
                    self.current["progress"] = m
            if li:
                _safe_job_update("UPDATE jobs SET progress=? WHERE id=?", (m[:500], jid))
        try:
            stats = TASKS[j["job"]](prog, **j["kw"])
            if log_it:
                _safe_job_update("UPDATE jobs SET finished_at=?, status='done', stats=?, progress='готово' WHERE id=?",
                                 (now_iso(), dumps(stats), j["id"]))
        except Exception as e:  # сбой задачи не останавливает очередь и планировщик
            tb = traceback.format_exc()
            log.error("task %s failed: %s", j["job"], tb)
            db.log_error(f"task:{j['job']}", tb[-1500:])  # никогда не бросает
            if log_it:
                _safe_job_update("UPDATE jobs SET finished_at=?, status='error', error=? WHERE id=?",
                                 (now_iso(), str(e)[:1000], j["id"]))
        finally:
            with self.lock:
                self.current = None
                self.done += 1
            self.runner.last_task_end[j["key"]] = time.monotonic()


def _safe_job_update(sql: str, params: tuple) -> None:
    """Статус задачи в журнале — вспомогательный: при недоступности БД пишется в файл, поток продолжает работу."""
    try:
        db.ex(sql, params)
    except Exception as e:  # noqa: BLE001
        db._file_log("jobs.status", f"{type(e).__name__}: {e} :: {sql[:60]}")


class Runner:
    def __init__(self):
        self.stop_flag = threading.Event()
        self.lanes = {n: Lane(n, self) for n in ("fast", "main", "chain", "aux", "analytics")}
        self.sched = threading.Thread(target=self._sched_loop, name="das-scheduler", daemon=True)
        self._seq = 0
        self._seq_lock = threading.Lock()
        self.last_task_end: dict[str, float] = {}
        self.started = False
        self.last_plan_at: str | None = None

    def start(self, scheduler: bool = True) -> None:
        if self.started:
            return
        self.started = True
        for l in self.lanes.values():
            l.thread.start()
        if scheduler:
            self.sched.start()

    def stop(self) -> None:
        self.stop_flag.set()

    def drop_queued(self, lane_name: str) -> int:
        """Убрать из очереди ещё не начатые задачи (выключение аналитики). Выполняемая задача завершится сама."""
        l = self.lanes.get(lane_name)
        if l is None:
            return 0
        n = 0
        with l.lock:
            while True:
                try:
                    l.q.get_nowait()
                    n += 1
                except queue.Empty:
                    break
            ids = [j["id"] for j in l.keys.values() if j.get("id")]
            l.keys.clear()
        for jid in ids:
            _safe_job_update("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'", (now_iso(), jid))
        return n

    @staticmethod
    def key(job: str, kw: dict) -> str:
        return f"{job}:{dumps(kw)}"

    def is_pending(self, job: str, **kw) -> bool:
        k = self.key(job, kw)
        l = self.lanes[LANE_OF.get(job, "main")]
        with l.lock:
            return k in l.keys or (l.current is not None and l.current["key"] == k)

    def submit(self, job: str, priority: int | None = None, **kw) -> int | None:
        """Поставить задачу. Повтор той же задачи не создаёт дубль; при переполнении очереди задача отклоняется."""
        lane = self.lanes[LANE_OF.get(job, "main")]
        k = self.key(job, kw)
        pr = 3 if priority is None else priority
        with lane.lock:
            if k in lane.keys:
                return lane.keys[k]["id"]
            if lane.current is not None and lane.current["key"] == k:
                return lane.current["id"]
            if len(lane.keys) >= int(config.load_settings().get("queue_max_per_lane", 300)) and pr >= 2:
                return None
        jid = 0
        if job not in NO_LOG:
            try:
                jid = db.ex("INSERT INTO jobs(job,started_at,status,progress) VALUES(?,?,?,?)", (job, None, "queued", "в очереди")).lastrowid
            except Exception as e:  # noqa: BLE001 — задача всё равно выполняется; журнал заданий — вспомогательный
                db._file_log("jobs.submit", f"{job}: {type(e).__name__}: {e}")
                jid = 0
        j = {"id": jid, "job": job, "kw": kw, "key": k, "priority": pr, "lane": lane.name}
        with lane.lock:
            lane.keys[k] = j
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        lane.q.put((pr, seq, j))
        return jid

    def status(self) -> dict:
        lanes = {}
        cur = None
        queued = []
        for n, l in self.lanes.items():
            with l.lock:
                c = dict(l.current) if l.current else None
                lanes[n] = {"current": c, "queued": len(l.keys), "done": l.done}
                queued += [{"id": j["id"], "job": j["job"], "kw": j["kw"]} for j in list(l.keys.values())[:20]]
            if c and (cur is None or n == "main"):
                cur = c
        s = config.load_settings()
        return {"current": cur, "queued": queued, "lanes": lanes, "scheduler_enabled": bool(s.get("scheduler_enabled")),
                "scheduler_running": self.sched.is_alive(), "last_plan_at": self.last_plan_at,
                "breaker": http.breaker_status(), "focus": refresh.focused()}

    # -------------------------------------------------------------- планировщик
    def _src_ok(self, job: str) -> bool:
        return all(http.source_available(s)[0] for s in SOURCES_OF.get(job, []))

    def plan(self) -> dict:
        """Один такт планирования (вызывается каждые ~10 с). Возвращает, что поставлено."""
        s = config.load_settings()
        self.last_plan_at = now_iso()
        put: dict[str, int] = {}

        def sub(job, pr, **kw):
            if not self._src_ok(job):
                return
            if self.submit(job, pr, **kw) is not None:
                put[job] = put.get(job, 0) + 1
        chains = config.enabled_chains(s)
        disc_iv = int(s["intervals_min"]["discovery"]) * 60
        for ck in chains:
            last = db.q1("SELECT MAX(run_at) m FROM coverage WHERE chain=? AND source='geckoterminal:new_pools'", (ck,))["m"]
            lt = parse_iso(last)
            if lt is None or (now_utc() - lt).total_seconds() >= disc_iv:
                sub("discover", 1, chain=ck)
            lb = parse_iso(db.get_progress(f"top_pools_at:{ck}"))
            if lb is None or (now_utc() - lb).total_seconds() > 6 * 3600:
                sub("backfill", 4, chain=ck)
            h = fundamentals.get_chain_health(ck)
            if not h or (now_utc() - (parse_iso(h.get("at")) or now_utc())).total_seconds() > 20 * 3600:
                sub("chain_health", 2, chain=ck)
        pr = parse_iso(db.q1("SELECT MAX(fetched_at) m FROM protocols")["m"])
        if pr is None or (now_utc() - pr).total_seconds() > 86400:
            sub("protocols", 2)
        # котировки: пакеты по 10, приоритет = лучший уровень в пакете
        dq = refresh.due_tokens("quote", 40)
        for i in range(0, len(dq), 10):
            chunk = dq[i:i + 10]
            sub("quotes", min(t for t, _ in chunk), uids=sorted(u for _, u in chunk))
        for kind, job, n in (("deep", "deep", 3), ("onchain", "onchain", 2), ("fundamentals", "fund", 3), ("coingecko", "cg", 2)):
            for tier, uid in refresh.due_tokens(kind, n):
                sub(job, tier + (1 if job == "deep" else 0), uid=uid)
        last_b = db.q1("SELECT MAX(started_at) m FROM jobs WHERE job='backup' AND status='done'")["m"]
        lbk = parse_iso(last_b)
        if lbk is None or (now_utc() - lbk).total_seconds() >= int(s["intervals_min"]["backup"]) * 60:
            sub("backup", 5)
        try:  # 2.1: аналитика (выключена по умолчанию); любой её сбой не влияет на планирование сбора данных
            from . import analytics
            analytics.plan(self, sub)
        except Exception:  # noqa: BLE001
            db.log_error("analytics.plan", traceback.format_exc()[-1500:])
        return put

    def _sched_loop(self) -> None:
        tick = 0
        while not self.stop_flag.is_set():
            try:
                if config.load_settings().get("scheduler_enabled"):
                    self.plan()
                    if tick % 30 == 0:
                        check_sources(config.load_settings()["alerts"]["stale_hours"])
            except Exception:  # планировщик не погибает: ошибка такта — в журнал (log_error не бросает)
                db.log_error("scheduler", traceback.format_exc()[-1500:])
            tick += 1
            self.stop_flag.wait(10)


# ------------------------------------------------------------------ задачи
def _snapshot(uid: str) -> dict:
    return enrich.get_facts(uid)


def _evaluate(uid: str, old: dict | None) -> dict:
    return pipeline.evaluate(uid, old_facts=old)


def deep_analyze(uid: str, prog: Callable[[str], None], onchain_calls: int = 40, onchain_seconds: float = 240,
                 with_onchain: bool = True) -> dict:
    old = _snapshot(uid)
    tok0 = db.q1("SELECT chain FROM tokens WHERE uid=?", (uid,))
    enrich.quick_enrich(tok0["chain"], [uid], prog)
    enrich.deep_enrich(uid, prog)
    fundamentals.update_token_fundamentals(uid)
    refresh._set_state(uid, last_fund_at=now_iso())
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    res = _evaluate(uid, old)
    if with_onchain and (res["level"] in ("C", "D", "E") or db.q1("SELECT 1 FROM watchlist WHERE token_uid=?", (uid,))):
        prog(f"ончейн-проверка {tok['symbol']}")
        try:
            onchain.onchain_update(uid, prog, max_calls=onchain_calls, max_seconds=onchain_seconds)
        except SourceError as e:
            db.log_error("onchain", str(e))
        res = _evaluate(uid, old)
    return {"level": res["level"], "status": res["status"]}


def _quick_and_evaluate(ck: str, uids: list[str], prog) -> int:
    uids = [u for u in dict.fromkeys(uids)]
    if not uids:
        return 0
    enrich.quick_enrich(ck, uids, prog)
    for u in uids:
        f = enrich.get_facts(u)  # связь с DefiLlama — только по БД, без запросов
        f["protocol_link"] = fundamentals.link_protocol(u)
        enrich.save_facts(u, f, "last_enriched_at")
        pipeline.evaluate(u, old_facts={})
    return len(uids)


def t_discover(prog, chain: str) -> dict:
    fundamentals.ensure_protocols()
    r = discovery.discover_new_pools(chain, prog)
    n = _quick_and_evaluate(chain, r["new_tokens"], prog)
    return {k: r[k] for k in ("pages", "pools", "continuous", "note", "error")} | {"new_tokens": n}


def t_backfill(prog, chain: str) -> dict:
    s = config.load_settings()
    r = discovery.discover_top_pools(chain, prog)
    db.set_progress(f"top_pools_at:{chain}", now_iso())
    n = _quick_and_evaluate(chain, r["new_tokens"], prog)
    if s["discovery"]["defillama_backfill"]:
        last = parse_iso(db.get_progress("defillama_backfill_at"))
        if not last or (now_utc() - last).total_seconds() > 12 * 3600:
            d = discovery.discover_defillama(config.enabled_chains(s), prog)
            db.set_progress("defillama_backfill_at", now_iso())
            by: dict[str, list[str]] = {}
            for uid in d.get("new_tokens", []):
                by.setdefault(uid.split(":")[0], []).append(uid)
            for ck, us in by.items():
                n += _quick_and_evaluate(ck, us, prog)
    return {"pools": r["pools"], "new_tokens": n}


def t_quotes(prog, uids: list[str]) -> dict:
    return refresh.refresh_quotes(uids)


def t_deep(prog, uid: str) -> dict:
    # без ончейна: он выполняется в отдельной очереди и не задерживает анализ остальных
    return deep_analyze(uid, prog, with_onchain=False)


def t_fund(prog, uid: str) -> dict:
    old = _snapshot(uid)
    fundamentals.update_token_fundamentals(uid)
    refresh._set_state(uid, last_fund_at=now_iso())
    _evaluate(uid, old)
    return {"ok": True}


def t_onchain(prog, uid: str) -> dict:
    old = _snapshot(uid)
    sec = float(config.load_settings().get("task_max_seconds", 90))
    oc = onchain.onchain_update(uid, prog, max_calls=40, max_seconds=sec)
    _evaluate(uid, old)
    return {"to_block": oc.get("scanned_to_block"), "complete": oc.get("complete_to_head")}


def t_cg(prog, uid: str) -> dict:
    return refresh.refresh_coingecko(uid)


def t_chain_health(prog, chain: str) -> dict:
    h = fundamentals.chain_health(chain)
    return {"tvl": h.get("tvl_usd"), "errors": h.get("errors")}


def t_protocols(prog) -> dict:
    discovery.refresh_protocols(prog)
    return {"ok": True}


def job_scan(prog, chains: list[str] | None = None) -> dict:
    """Кнопка «Сканировать»: поставить обнаружение по всем сетям немедленно (обычно не требуется — работает планировщик)."""
    s = config.load_settings()
    chains = chains or config.enabled_chains(s)
    for ck in chains:
        runner.submit("discover", 0, chain=ck)
    return {"queued": chains}


def job_refresh(prog, limit: int = 30) -> dict:
    rows = db.q("SELECT uid FROM tokens WHERE level IN ('B','C','D','E') OR uid IN (SELECT token_uid FROM watchlist) "
                "ORDER BY COALESCE(last_deep_at,'') ASC LIMIT ?", (limit,))
    for r in rows:
        runner.submit("deep", 3, uid=r["uid"])
    return {"queued": len(rows)}


def job_fundamentals(prog) -> dict:
    s = config.load_settings()
    for ck in config.enabled_chains(s):
        runner.submit("chain_health", 2, chain=ck)
    rows = db.q("SELECT uid FROM tokens WHERE level IN ('C','D','E') OR protocol_slug IS NOT NULL OR uid IN (SELECT token_uid FROM watchlist)")
    for r in rows:
        runner.submit("fund", 3, uid=r["uid"])
    return {"queued": len(rows)}


def job_weekly(prog) -> dict:
    job_refresh(prog, limit=200)
    backup.make_backup(label="weekly")
    return {"ok": True}


def job_token(prog, uid: str) -> dict:
    """Кнопка «Обновить данные» в карточке: все виды обновления токена с высоким приоритетом."""
    runner.submit("quotes", 0, uids=[uid])
    runner.submit("onchain", 0, uid=uid)
    r = deep_analyze(uid, prog, with_onchain=False)
    return r


def job_backup(prog) -> dict:
    return {"path": str(backup.make_backup())}


def t_fables_index(prog) -> dict:
    from . import analytics
    return analytics.fables_task(prog)


def t_analytics(prog, module: str, uid: str | None = None, force: bool = False) -> dict:
    from . import analytics
    return analytics.task(prog, module, uid=uid, force=force)


def job_reevaluate(prog) -> dict:
    rows = db.q("SELECT uid FROM tokens")
    for i, r in enumerate(rows):
        if i % 200 == 0:
            prog(f"пересчёт {i}/{len(rows)}")
        pipeline.evaluate(r["uid"], old_facts=None)
    return {"tokens": len(rows)}


TASKS: dict[str, Callable] = {
    "discover": t_discover, "backfill": t_backfill, "quotes": t_quotes, "deep": t_deep, "fund": t_fund, "onchain": t_onchain,
    "cg": t_cg, "chain_health": t_chain_health, "protocols": t_protocols, "scan": job_scan, "refresh": job_refresh,
    "fundamentals": job_fundamentals, "weekly": job_weekly, "token": job_token, "backup": job_backup, "reevaluate": job_reevaluate,
    "analytics": t_analytics, "fables_index": t_fables_index,
}
JOBS = TASKS  # совместимость

runner = Runner()
