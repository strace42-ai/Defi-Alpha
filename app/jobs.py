"""Фоновые задания. Один рабочий поток (последовательно — чтобы соблюдать лимиты API).
Прогресс пишется в таблицу jobs; после перезапуска сбор продолжается с сохранённых позиций
(scan_progress: последний пул new_pools, блоки сканирования mint и т.д.).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from typing import Any, Callable

from . import backup, config, db, discovery, enrich, fundamentals, onchain, pipeline
from .alerts import check_sources
from .http import SourceError
from .util import dumps, loads, now_iso, now_utc, parse_iso

log = logging.getLogger("das.jobs")

JOB_NAMES = {"scan": "Сканирование", "refresh": "Обновление кандидатов", "fundamentals": "Фундаментал",
             "token": "Анализ токена", "backup": "Резервная копия", "weekly": "Недельный снимок",
             "reevaluate": "Пересчёт фильтров"}


class Runner:
    def __init__(self):
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self.current: dict | None = None
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.worker = threading.Thread(target=self._loop, name="das-worker", daemon=True)
        self.sched = threading.Thread(target=self._sched_loop, name="das-scheduler", daemon=True)
        self.queued: list[dict] = []

    def start(self):
        self.worker.start()
        self.sched.start()

    PRIORITY = {"scan": 0, "token": 1, "reevaluate": 2, "refresh": 3, "fundamentals": 3, "weekly": 4, "backup": 4}

    def submit(self, job: str, priority: int | None = None, **kw) -> int:
        with self.lock:
            for j in self.queued + ([self.current] if self.current else []):
                if j and j["job"] == job and j.get("kw") == kw:
                    return j["id"]
            cur = db.ex("INSERT INTO jobs(job,started_at,status,progress) VALUES(?,?,?,?)", (job, None, "queued", "в очереди"))
            j = {"id": cur.lastrowid, "job": job, "kw": kw}
            self.queued.append(j)
        self._seq += 1
        self.q.put((self.PRIORITY.get(job, 3) if priority is None else priority, self._seq, j))
        return j["id"]

    def status(self) -> dict:
        with self.lock:
            return {"current": self.current, "queued": [{"id": j["id"], "job": j["job"], "kw": j["kw"]} for j in self.queued]}

    def _progress(self, jid: int, msg: str):
        with self.lock:
            if self.current:
                self.current["progress"] = msg
        try:
            db.ex("UPDATE jobs SET progress=? WHERE id=?", (msg[:500], jid))
        except Exception:
            pass

    def _loop(self):
        db.init_db()
        while not self.stop_flag.is_set():
            try:
                _, _, j = self.q.get(timeout=1)
            except queue.Empty:
                continue
            with self.lock:
                self.queued = [x for x in self.queued if x["id"] != j["id"]]
                self.current = {"id": j["id"], "job": j["job"], "kw": j["kw"], "progress": "старт", "started_at": now_iso()}
            db.ex("UPDATE jobs SET started_at=?, status='running' WHERE id=?", (now_iso(), j["id"]))
            prog = lambda m, jid=j["id"]: self._progress(jid, m)
            try:
                stats = JOBS[j["job"]](prog, **j["kw"])
                db.ex("UPDATE jobs SET finished_at=?, status='done', stats=?, progress='готово' WHERE id=?",
                      (now_iso(), dumps(stats), j["id"]))
            except Exception as e:  # задание не должно ронять приложение
                tb = traceback.format_exc()
                log.error("job %s failed: %s", j["job"], tb)
                db.log_error(f"job:{j['job']}", tb)
                db.ex("UPDATE jobs SET finished_at=?, status='error', error=? WHERE id=?", (now_iso(), str(e)[:1000], j["id"]))
            finally:
                with self.lock:
                    self.current = None

    def _sched_loop(self):
        db.init_db()
        while not self.stop_flag.is_set():
            time.sleep(20)
            s = config.load_settings()
            if not s.get("scheduler_enabled"):
                continue
            iv = s["intervals_min"]
            for job, key in (("scan", "discovery"), ("refresh", "candidates_refresh"), ("fundamentals", "fundamentals"),
                             ("weekly", "weekly_snapshot"), ("backup", "backup")):
                last = db.q1("SELECT MAX(started_at) m FROM jobs WHERE job=? AND status IN ('done','running','error')", (job,))
                lt = parse_iso(last["m"]) if last and last["m"] else None
                if lt is None or (now_utc() - lt).total_seconds() >= iv[key] * 60:
                    pend = db.q1("SELECT id FROM jobs WHERE job=? AND status IN ('queued','running')", (job,))
                    if not pend:
                        self.submit(job)


# ------------------------------------------------------------------ задания
def _snapshot(uid: str) -> dict:
    return enrich.get_facts(uid)


def _evaluate(uid: str, old: dict | None) -> dict:
    return pipeline.evaluate(uid, old_facts=old)


def deep_analyze(uid: str, prog: Callable[[str], None], onchain_calls: int = 40, onchain_seconds: float = 240) -> dict:
    old = _snapshot(uid)
    tok0 = db.q1("SELECT chain FROM tokens WHERE uid=?", (uid,))
    enrich.quick_enrich(tok0["chain"], [uid], prog)
    enrich.deep_enrich(uid, prog)
    fundamentals.update_token_fundamentals(uid)
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    res = _evaluate(uid, old)
    if res["level"] in ("C", "D", "E") or db.q1("SELECT 1 FROM watchlist WHERE token_uid=?", (uid,)):
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


def job_scan(prog, chains: list[str] | None = None, deep_limit: int = 8) -> dict:
    """Порядок выбран так, чтобы первые объяснённые результаты появлялись через 1–2 минуты:
    по каждой сети — новые пулы → рыночные данные → фильтры; затем исторический добор; затем углублённый анализ."""
    s = config.load_settings()
    chains = chains or config.enabled_chains(s)
    stats: dict[str, Any] = {"chains": {}, "new_tokens": 0, "deep": 0}
    prog("DefiLlama: список протоколов и метрики сетей")
    fundamentals.ensure_protocols()
    for ck in chains:
        h = fundamentals.get_chain_health(ck)
        if not h or (now_utc() - (parse_iso(h.get("at")) or now_utc())).total_seconds() > 20 * 3600:
            fundamentals.chain_health(ck)
    for ck in chains:
        prog(f"{ck}: новые пулы GeckoTerminal")
        r = discovery.discover_new_pools(ck, prog)
        stats["chains"][ck] = {k: r[k] for k in ("pages", "pools", "continuous", "note", "error")}
        stats["new_tokens"] += _quick_and_evaluate(ck, r["new_tokens"], prog)
    # исторический добор — не чаще раза в 6 часов на сеть
    backfill: dict[str, list[str]] = {}
    for ck in chains:
        lk = f"top_pools_at:{ck}"
        last = parse_iso(db.get_progress(lk))
        if not last or (now_utc() - last).total_seconds() > 6 * 3600:
            r = discovery.discover_top_pools(ck, prog)
            backfill.setdefault(ck, []).extend(r["new_tokens"])
            db.set_progress(lk, now_iso())
    if s["discovery"]["defillama_backfill"]:
        last = parse_iso(db.get_progress("defillama_backfill_at"))
        if not last or (now_utc() - last).total_seconds() > 12 * 3600:
            prog("DefiLlama: недавние протоколы с токенами")
            r = discovery.discover_defillama(chains, prog)
            for uid in r.get("new_tokens", []):
                backfill.setdefault(uid.split(":")[0], []).append(uid)
            db.set_progress("defillama_backfill_at", now_iso())
    if s["discovery"].get("dexscreener_profiles"):
        r = discovery.discover_ds_profiles(chains)
        for uid in r.get("new_tokens", []):
            backfill.setdefault(uid.split(":")[0], []).append(uid)
    for ck, uids in backfill.items():
        stats["new_tokens"] += _quick_and_evaluate(ck, uids, prog)
    # углублённый анализ: уровень B+ (возраст/MCAP/просадка не FAIL, рыночный минимум выполнен)
    cands = db.q("SELECT uid, facts FROM tokens WHERE chain IN (%s) AND asset_class='token' AND last_deep_at IS NULL "
                 "AND level IN ('B','C','D','E') ORDER BY first_seen_at DESC LIMIT 500" % ",".join("?" * len(chains)), chains)
    ranked = []
    for r in cands:
        f = loads(r["facts"], {})
        liq = max([x for x in ((f.get("gt") or {}).get("liquidity_usd"), (f.get("ds") or {}).get("liquidity_usd")) if x is not None] or [0])
        # проекты со связью с DefiLlama — в приоритете
        ranked.append((1 if (f.get("protocol_link") or {}).get("linked") else 0, liq, r["uid"]))
    ranked.sort(reverse=True)
    for _, liq, uid in ranked[:deep_limit]:
        prog(f"углублённый анализ ({stats['deep'] + 1}/{min(deep_limit, len(ranked))})")
        try:
            deep_analyze(uid, prog, onchain_calls=20, onchain_seconds=60)
            stats["deep"] += 1
        except SourceError as e:
            db.log_error("deep", f"{uid}: {e}")
    stats["deep_pending"] = max(0, len(ranked) - deep_limit)
    check_sources(s["alerts"]["stale_hours"])
    return stats


def job_refresh(prog, limit: int = 30) -> dict:
    rows = db.q("SELECT uid FROM tokens WHERE level IN ('B','C','D','E') OR uid IN (SELECT token_uid FROM watchlist) "
                "ORDER BY COALESCE(last_deep_at,'') ASC LIMIT ?", (limit,))
    n = 0
    for r in rows:
        prog(f"обновление кандидата {n + 1}/{len(rows)}")
        try:
            deep_analyze(r["uid"], prog)
            n += 1
        except SourceError as e:
            db.log_error("refresh", f"{r['uid']}: {e}")
    return {"refreshed": n}


def job_fundamentals(prog) -> dict:
    s = config.load_settings()
    for ck in config.enabled_chains(s):
        prog(f"{ck}: метрики сети")
        fundamentals.chain_health(ck)
    try:
        discovery.refresh_protocols(prog)
    except SourceError as e:
        db.log_error("protocols", str(e))
    rows = db.q("SELECT uid FROM tokens WHERE level IN ('C','D','E') OR protocol_slug IS NOT NULL OR uid IN (SELECT token_uid FROM watchlist)")
    for i, r in enumerate(rows):
        prog(f"фундаментал {i + 1}/{len(rows)}")
        old = _snapshot(r["uid"])
        fundamentals.update_token_fundamentals(r["uid"])
        _evaluate(r["uid"], old)
    return {"tokens": len(rows)}


def job_weekly(prog) -> dict:
    st = job_refresh(prog, limit=200)
    backup.make_backup(label="weekly")
    return st


def job_token(prog, uid: str) -> dict:
    return deep_analyze(uid, prog, onchain_calls=60)


def job_backup(prog) -> dict:
    p = backup.make_backup()
    return {"path": str(p)}


def job_reevaluate(prog) -> dict:
    rows = db.q("SELECT uid FROM tokens")
    for i, r in enumerate(rows):
        if i % 200 == 0:
            prog(f"пересчёт {i}/{len(rows)}")
        pipeline.evaluate(r["uid"], old_facts=None)
    return {"tokens": len(rows)}


JOBS: dict[str, Callable] = {"scan": job_scan, "refresh": job_refresh, "fundamentals": job_fundamentals, "weekly": job_weekly,
                             "token": job_token, "backup": job_backup, "reevaluate": job_reevaluate}

runner = Runner()
