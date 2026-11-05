"""Конкурентный стресс-тест SQLite: настоящие потоки, настоящие соединения, реальные задержки (time.sleep не отключается).

    python scripts/stress_sqlite.py [--seconds 180] [--fault-rate 0.003] [--code-dir .] [--report out.md]

Одновременно работают: 4 очереди фонового мониторинга + планировщик, запись CoinGecko, массовая запись mint-событий и их
timestamp, обновление каталога DefiLlama (~9000 протоколов), сохранение фундаментальных метрик, скрытие/восстановление
токенов, наблюдение, пауза/возобновление мониторинга и чтение интерфейса (через HTTP API приложения).
Сеть подменяется на уровне httpx-транспорта (весь сетевой код приложения выполняется) с задержками 20–150 мс.
Ошибки записи инъецируются с вероятностью --fault-rate на DML-запрос (OperationalError, как при SQLITE_BUSY).

База — синтетическая, сопоставимого с реальной размера: сотни токенов, ≥ 20 000 ончейн-событий, ~9000 протоколов.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=int, default=180)
ap.add_argument("--fault-rate", type=float, default=0.003)
ap.add_argument("--code-dir", default=str(Path(__file__).resolve().parent.parent))
ap.add_argument("--report", default="")
ap.add_argument("--tokens", type=int, default=800)
ap.add_argument("--events", type=int, default=20000)
ap.add_argument("--label", default="")
args = ap.parse_args()

CODE = Path(args.code_dir).resolve()
sys.path.insert(0, str(CODE))
DATA = Path(tempfile.mkdtemp(prefix="das-stress-"))
os.environ["DAS_DATA_DIR"] = str(DATA)

import httpx  # noqa: E402

from app import config, db, http, jobs, refresh  # noqa: E402
from app.util import dumps, now_iso  # noqa: E402

M = Counter()           # метрики
LAT = defaultdict(list)  # задержки пользовательских операций, мс
UNHANDLED = []
LOCK = threading.Lock()
rnd = random.Random(42)
FAB = "0x" + "f" * 40
EVENT_EVERY = 997        # mint-событие в каждом блоке, кратном 997 (детерминированно — для проверки потерь/дублей)
BLOCK_TIME = 0.1
GENESIS = 1_790_000_000
HEAD0 = 106_000_000
START_WALL = time.time()


def head_now() -> int:
    return HEAD0 + int((time.time() - START_WALL) / BLOCK_TIME)


# ------------------------------------------------------------------ инъекция ошибок и учёт SQLITE_BUSY
DML = ("INSERT", "UPDATE", "DELETE", "REPLACE")


class FaultConn(sqlite3.Connection):
    def execute(self, sql, *a, **kw):
        s = sql.lstrip()[:7].upper()
        if s.startswith(DML) and rnd.random() < args.fault_rate:
            with LOCK:
                M["injected_write_faults"] += 1
            raise sqlite3.OperationalError("database is locked (injected)")
        try:
            return super().execute(sql, *a, **kw)
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                with LOCK:
                    M["sqlite_busy_errors"] += 1
            raise

    def commit(self):
        try:
            return super().commit()
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                with LOCK:
                    M["sqlite_busy_errors"] += 1
            raise


_orig_connect = sqlite3.connect


def _connect(*a, **kw):
    kw.setdefault("factory", FaultConn)
    return _orig_connect(*a, **kw)


db.sqlite3.connect = _connect  # тот же вызов, что делает приложение


# ------------------------------------------------------------------ фиктивная сеть (httpx-транспорт)
def addr(i: int) -> str:
    return "0x" + f"{i:040x}"


def pool_obj(net, base, quote, i):
    return ({"id": f"{net}_0x{i:064x}", "type": "pool",
             "attributes": {"address": f"0x{i:064x}", "name": "T / WETH", "pool_created_at": now_iso(), "base_token_price_usd": "0.04",
                            "quote_token_price_usd": "2300", "reserve_in_usd": "250000", "fdv_usd": "4000000", "market_cap_usd": None,
                            "volume_usd": {"h24": "50000"}, "transactions": {"h24": {"buys": 40, "sells": 30}}},
             "relationships": {"base_token": {"data": {"id": f"{net}_{base}"}}, "quote_token": {"data": {"id": f"{net}_{quote}"}},
                               "dex": {"data": {"id": "uni"}}}},
            [{"id": f"{net}_{base}", "type": "token", "attributes": {"address": base, "symbol": "T", "name": "Tok", "decimals": 18}},
             {"id": f"{net}_{quote}", "type": "token", "attributes": {"address": quote, "symbol": "WETH", "name": "WETH", "decimals": 18}}])


PROTOCOLS = [{"slug": f"p{i}", "name": f"P{i}", "category": "Dexs", "chains": ["Robinhood Chain"], "address": f"robinhood:{addr(i + 1)}",
              "gecko_id": None, "symbol": "P", "url": "https://x", "listedAt": 1790000000, "tvl": 1e6 + i, "mcap": None,
              "chainTvls": {"Robinhood Chain": 1e6 + i}} for i in range(9000)]
NEW_TOKEN_SEQ = [10_000_000]


def route(req: httpx.Request) -> httpx.Response:
    time.sleep(rnd.uniform(0.02, 0.15))
    u = str(req.url)
    with LOCK:
        M["http_requests"] += 1
    if rnd.random() < 0.01:
        return httpx.Response(503)
    if rnd.random() < 0.005:
        return httpx.Response(429, headers={"retry-after": "1"})
    if "geckoterminal" in u:
        if "/new_pools" in u or "/pools?" in u or u.endswith("/pools") or "/pools?page" in u:
            data, inc = [], []
            for _ in range(20):
                NEW_TOKEN_SEQ[0] += 1
                p, t = pool_obj("robinhood", addr(NEW_TOKEN_SEQ[0]), "0x0bd7d308f8e1639fab988df18a8011f41eacad73", NEW_TOKEN_SEQ[0])
                data.append(p)
                inc += t
            return httpx.Response(200, json={"data": data, "included": inc})
        if "/tokens/multi/" in u:
            a = u.split("/tokens/multi/")[1].split("?")[0].split(",")
            return httpx.Response(200, json={"data": [{"attributes": {"address": x, "symbol": "T", "name": "Tok", "decimals": 18,
                                                                       "price_usd": "0.04", "fdv_usd": "4000000", "market_cap_usd": None,
                                                                       "total_supply": "1e26", "normalized_total_supply": "100000000",
                                                                       "total_reserve_in_usd": "250000", "volume_usd": {"h24": "50000"}}} for x in a]})
        if "/ohlcv/" in u:
            now = int(time.time()) // 86400 * 86400
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [[now - d * 86400, 0.04, 0.05, 0.03, 0.04, 1000] for d in range(30)]}}, "meta": {}})
        if "/info" in u:
            return httpx.Response(200, json={"data": {"attributes": {"websites": ["https://x"], "holders": {"count": 100}}}})
        if "/tokens/" in u and "/pools" in u:
            a = u.split("/tokens/")[1].split("/")[0]
            p, t = pool_obj("robinhood", a, "0x0bd7d308f8e1639fab988df18a8011f41eacad73", int(a, 16) % 10 ** 9)
            return httpx.Response(200, json={"data": [p], "included": t})
        return httpx.Response(200, json={"data": []})
    if "dexscreener" in u:
        a = u.rstrip("/").split("/")[-1].split(",")[0]
        return httpx.Response(200, json=[{"chainId": "robinhood", "dexId": "uni", "pairAddress": "0x" + "9" * 40, "priceUsd": str(0.04 * rnd.uniform(0.98, 1.02)),
                                          "baseToken": {"address": a, "symbol": "T"}, "quoteToken": {"address": "0x0bd7d308f8e1639fab988df18a8011f41eacad73", "symbol": "WETH"},
                                          "liquidity": {"usd": 300000}, "volume": {"h24": 40000}, "txns": {"h24": {"buys": 30, "sells": 20}},
                                          "fdv": 4000000, "marketCap": 4000000}])
    if "stablecoins.llama.fi" in u:  # проверять ДО coins.llama.fi (подстрока)
        return httpx.Response(200, json=[{"date": str(1790000000 + d * 86400), "totalCirculatingUSD": {"peggedUSD": 1e9}} for d in range(100)])
    if "coins.llama.fi" in u:
        return httpx.Response(200, json={"coins": {}})
    if "api.llama.fi" in u:
        if u.endswith("/protocols"):
            return httpx.Response(200, json=PROTOCOLS)
        if "/protocol/" in u:
            return httpx.Response(200, json={"name": "P", "category": "Dexs", "tvl": [{"date": 1790000000 + d * 86400, "totalLiquidityUSD": 1e6 + d} for d in range(100)],
                                             "currentChainTvls": {"Robinhood Chain": 1e6}, "raises": []})
        if "/summary/" in u:
            return httpx.Response(200, json={"total24h": 1000, "total7d": 7000, "total30d": 30000, "totalDataChart": [[1790000000 + d * 86400, 1000] for d in range(60)], "methodology": {}})
        if "historicalChainTvl" in u:
            return httpx.Response(200, json=[{"date": 1790000000 + d * 86400, "tvl": 1e9} for d in range(120)])
        if "/overview/" in u:
            return httpx.Response(200, json={"total30d": 1e9, "change_30dover30d": 1})
        if "/v2/chains" in u:
            return httpx.Response(200, json=[{"name": "Robinhood Chain", "tvl": 1e9}])
        return httpx.Response(200, json={})
    if "coingecko" in u:
        return httpx.Response(200, json={"id": "x", "platforms": {"robinhood": FAB}, "market_data": {"current_price": {"usd": 0.04},
                                         "circulating_supply": 40e6, "total_supply": 1e8, "market_cap": {"usd": 1.6e6}}})
    if req.method == "POST":  # JSON-RPC
        body = json.loads(req.content)
        m, p = body["method"], body.get("params") or []
        if m == "eth_blockNumber":
            res = hex(head_now())
        elif m == "eth_getBlockByNumber":
            b = int(p[0], 16)
            res = {"timestamp": hex(GENESIS + int(b * BLOCK_TIME))}
        elif m == "eth_getLogs":
            a, b = int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16)
            first = (a + EVENT_EVERY - 1) // EVENT_EVERY * EVENT_EVERY
            res = [{"blockNumber": hex(x), "transactionHash": "0x" + f"{x:064x}", "logIndex": "0x0", "data": "0x" + f"{10 ** 18:064x}",
                    "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", "0x" + "0" * 64, "0x" + "0" * 24 + "ab" * 20]}
                   for x in range(first, b + 1, EVENT_EVERY)] if p[0]["topics"][1:2] == ["0x" + "0" * 64] else []
        elif m == "eth_call":
            res = "0x" + f"{12:064x}" if p[0]["data"] == "0x313ce567" else "0x" + "0" * 64
        elif m == "eth_getCode":
            res = "0x6080604052"
        else:
            res = "0x" + "0" * 64
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": res})
    return httpx.Response(404)


http.set_client(httpx.Client(transport=httpx.MockTransport(route), timeout=10))


# ------------------------------------------------------------------ синтетическая база
def build_db():
    db.init_db()
    s = config.load_settings()
    s["rate_limits_per_min"] = {k: 1200 for k in s["rate_limits_per_min"]}
    s["chains_enabled"] = ["robinhood"]
    s["intervals_min"]["discovery"] = 1
    config.save_settings(s)
    c = sqlite3.connect(str(db.DB_PATH))
    big = dumps({"gt": {"price_usd": 0.04}, "pools": {"items": [{"address": "0x1", "price_usd": 0.04, "liquidity_usd": 1e5}] * 20},
                 "price_history": {"0x1": [[1790000000 + d * 86400, 0.04, 100] for d in range(90)]}})
    rows = [(f"robinhood:{addr(i)}", "robinhood", addr(i), addr(i), "T", f"Token{i}", 18, now_iso(), "test", "synthetic", "token",
             "C" if i < 30 else "B" if i < 200 else "A", "MANUAL REVIEW" if i < 200 else "FAIL", big, now_iso(), 50.0 + (i % 40)) for i in range(args.tokens)]
    rows.append((f"robinhood:{FAB}", "robinhood", FAB, FAB, "FAB", "Fables-like", 18, now_iso(), "test", "synthetic", "token", "E",
                 "MANUAL REVIEW", big, now_iso(), 80.0))
    c.executemany("INSERT INTO tokens(uid,chain,address,address_display,symbol,name,decimals,first_seen_at,discovery_source,discovery_reason,"
                  "asset_class,level,status,facts,last_market_at,score_preliminary) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    first = HEAD0 - args.events * EVENT_EVERY
    first = first // EVENT_EVERY * EVENT_EVERY
    ev = [(f"robinhood:{FAB}", "mint", "0x" + f"{b:064x}", 0, b, None, "0x" + "ab" * 20, str(10 ** 18), 1.0)
          for b in range(first, HEAD0, EVENT_EVERY)]
    c.executemany("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,ts,counterparty,amount_raw,amount) VALUES(?,?,?,?,?,?,?,?,?)", ev)
    c.execute("INSERT INTO scan_progress(key,value,updated_at) VALUES(?,?,?)", (f"mint_scan_from:robinhood:{FAB}", str(first), now_iso()))
    c.execute("INSERT INTO scan_progress(key,value,updated_at) VALUES(?,?,?)", (f"mint_scan_to:robinhood:{FAB}", str(HEAD0), now_iso()))
    c.executemany("INSERT INTO protocols(slug,name,category,chains,address,raw,fetched_at) VALUES(?,?,?,?,?,?,?)",
                  [(p["slug"], p["name"], p["category"], "[]", p["address"], dumps(p), now_iso()) for p in PROTOCOLS])
    c.executemany("INSERT INTO observations(entity_type,entity_id,metric,value,source,fetched_at) VALUES(?,?,?,?,?,?)",
                  [("token", f"robinhood:{addr(i % args.tokens)}", "price_usd", 0.04, "gt", now_iso()) for i in range(7000)])
    for i in range(10):
        c.execute("INSERT INTO watchlist(token_uid,added_at,status) VALUES(?,?,?)", (f"robinhood:{addr(i)}", now_iso(), "watching"))
    c.execute("INSERT INTO watchlist(token_uid,added_at,status) VALUES(?,?,?)", (f"robinhood:{FAB}", now_iso(), "position"))
    c.execute("UPDATE tokens SET coingecko_id='x' WHERE level IN ('B','C','E')")
    c.commit()
    c.close()
    return first, len(ev)


# ------------------------------------------------------------------ нагрузка
STOP = threading.Event()


def guarded(name, fn):
    def run():
        while not STOP.is_set():
            try:
                fn()
            except Exception as e:  # ошибка нагрузочного потока — учитывается, но поток продолжает
                with LOCK:
                    M[f"driver_error:{name}:{type(e).__name__}"] += 1
            STOP.wait(rnd.uniform(0.2, 1.0))
    return threading.Thread(target=run, name=f"driver-{name}", daemon=True)


def driver_tasks(r):
    def f():
        uids = [f"robinhood:{addr(rnd.randrange(200))}" for _ in range(3)]
        r.submit("quotes", 1, uids=uids)
        r.submit("deep", 2, uid=uids[0])
        r.submit("cg", 2, uid=uids[1])
        r.submit("fund", 2, uid=uids[2])
        r.submit("onchain", 1, uid=f"robinhood:{FAB}")
        if rnd.random() < 0.08:
            r.submit("protocols", 2)
        if rnd.random() < 0.05:
            r.submit("backup", 4)
    return f


def user_ops():
    from fastapi.testclient import TestClient
    from app.api import app
    cl = TestClient(app)
    checks = Counter()

    def timed(name, fn):
        t0 = time.perf_counter()
        try:
            resp = fn()
            ok = resp.status_code < 400
        except Exception as e:  # noqa: BLE001
            ok = False
            resp = None
            with LOCK:
                M[f"user_exception:{name}:{type(e).__name__}"] += 1
        with LOCK:
            LAT[name].append((time.perf_counter() - t0) * 1000)
            M[f"user_{'ok' if ok else 'fail'}:{name}"] += 1
        return resp

    def f():
        u = f"robinhood:{addr(rnd.randrange(30))}"
        op = rnd.random()
        if op < 0.25:
            r = timed("hide", lambda: cl.post(f"/api/hide/{u}", json={"reason": "stress"}))
            if r is not None and r.status_code < 400:
                best = timed("read_best", lambda: cl.get("/api/best"))
                if best is not None and best.status_code < 400 and any(x["uid"] == u for x in best.json()["rows"]):
                    with LOCK:
                        M["hidden_still_in_best"] += 1
            timed("unhide", lambda: cl.post(f"/api/unhide/{u}", json={}))
        elif op < 0.45:
            timed("watch_add", lambda: cl.post(f"/api/watchlist/{u}", json={"status": "studying"}))
        elif op < 0.5:
            timed("monitoring_toggle", lambda: cl.post("/api/monitoring", json={"enabled": rnd.random() < 0.7}))
        else:
            timed("read_tokens", lambda: cl.get("/api/tokens?limit=200"))
            timed("read_token", lambda: cl.get(f"/api/token/{u}"))
            timed("read_live", lambda: cl.get("/api/live"))
    return f


def lock_probe():
    """Отдельное соединение раз в секунду пытается сделать короткую запись: выявляет долгоживущие блокировки."""
    c = _orig_connect(str(db.DB_PATH), timeout=10, isolation_level=None)
    c.execute("CREATE TABLE IF NOT EXISTS _probe(x)")
    while not STOP.is_set():
        t0 = time.perf_counter()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO _probe VALUES(1)")
            c.execute("COMMIT")
            w = (time.perf_counter() - t0) * 1000
            with LOCK:
                LAT["probe_write_wait"].append(w)
        except sqlite3.OperationalError:
            with LOCK:
                M["probe_write_failed_10s"] += 1
            try:
                c.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        STOP.wait(1.0)


def main():
    first, n_ev = build_db()
    threading.excepthook = lambda a: UNHANDLED.append(f"{a.thread.name}: {a.exc_type.__name__}: {a.exc_value}")
    r = jobs.runner
    t0 = time.time()
    r.start(scheduler=True)
    drivers = [guarded("tasks", driver_tasks(r)), threading.Thread(target=lock_probe, name="probe", daemon=True)]
    drivers += [guarded(f"user{i}", user_ops()) for i in range(3)]
    for d in drivers:
        d.start()
    while time.time() - t0 < args.seconds:
        time.sleep(5)
    STOP.set()
    lanes_alive = {n: l.thread.is_alive() for n, l in r.lanes.items()}
    sched_alive = r.sched.is_alive()
    r.stop()
    # корректная остановка: дождаться завершения задач, выполнявшихся в момент остановки (как при закрытии программы),
    # иначе замер попадает между вставкой событий и проставлением их времени внутри одной задачи
    inflight = {n: (l.current or {}).get("job") for n, l in r.lanes.items() if l.current}
    t_wait = time.time()
    while time.time() - t_wait < 90 and any(l.current and l.thread.is_alive() for l in r.lanes.values()):
        time.sleep(0.5)
    still = {n: (l.current or {}).get("job") for n, l in r.lanes.items() if l.current}
    time.sleep(1.0)
    dur = time.time() - t0
    c = _orig_connect(str(db.DB_PATH), timeout=30)
    integ = c.execute("PRAGMA integrity_check").fetchone()[0]
    to_block = int(c.execute("SELECT value FROM scan_progress WHERE key=?", (f"mint_scan_to:robinhood:{FAB}",)).fetchone()[0])
    blocks = [r_[0] for r_ in c.execute("SELECT block FROM supply_events WHERE token_uid=? AND COALESCE(orphaned,0)=0 AND kind='mint'",
                                         (f"robinhood:{FAB}",)).fetchall()] if "orphaned" in [x[1] for x in c.execute("PRAGMA table_info(supply_events)")] else \
        [r_[0] for r_ in c.execute("SELECT block FROM supply_events WHERE token_uid=? AND kind='mint'", (f"robinhood:{FAB}",)).fetchall()]
    expected = set(range(first, to_block, EVENT_EVERY))
    got = Counter(blocks)
    lost = len(expected - set(got))
    dup = sum(v - 1 for v in got.values() if v > 1)
    null_ts = c.execute("SELECT COUNT(*) FROM supply_events WHERE ts IS NULL").fetchone()[0]
    jobs_err = dict(c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    errlog = c.execute("SELECT COUNT(*) FROM error_log").fetchone()[0]
    locked_in_log = c.execute("SELECT COUNT(*) FROM error_log WHERE message LIKE '%locked%'").fetchone()[0]
    done = {n: l.done for n, l in r.lanes.items()}
    pct = lambda xs, q: round(statistics.quantiles(xs, n=100)[q - 1], 1) if len(xs) >= 2 else (round(xs[0], 1) if xs else None)
    lat = {k: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "max": round(max(v), 1)} for k, v in LAT.items() if v}
    res = {"label": args.label, "code": str(CODE), "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
           "duration_s": round(dur, 1), "tokens": args.tokens + 1, "events_initial": n_ev, "fault_rate": args.fault_rate,
           "metrics": dict(M), "unhandled_thread_exceptions": UNHANDLED[:20], "unhandled_count": len(UNHANDLED),
           "lanes_alive": lanes_alive, "scheduler_alive": sched_alive, "tasks_done": done, "integrity_check": integ,
           "events_expected_in_scanned_range": len(expected), "events_lost": lost, "events_duplicated": dup,
           "events_without_ts": null_ts, "inflight_at_stop": inflight, "unfinished_after_wait": still, "scanned_to_block": to_block, "jobs_by_status": jobs_err, "error_log_rows": errlog,
           "error_log_locked": locked_in_log, "latency_ms": lat,
           "tx_stats": dict(getattr(db, "tx_stats", {}) or {}) or "нет (версия без транзакционного слоя)",
           "task_errors": dict(Counter((j or "?") for (j,) in c.execute("SELECT job FROM jobs WHERE status='error'").fetchall()))}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if args.report:
        Path(args.report).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    os._exit(0)


if __name__ == "__main__":
    main()
