"""2.1 (этап 1): Fundamental Momentum и Emission Pressure — правильность и ИЗОЛЯЦИЯ от сканера 2.0.1.
Номера test_NN соответствуют обязательным проверкам 1–16 из задания."""
from __future__ import annotations

import datetime as _dt
import hashlib
import sqlite3
import threading
import time as _real_time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import analytics, config, db, discovery, emission_pressure as ep, http, jobs, momentum
from app.chains import CHAINS
from app.util import dumps, iso, loads, now_iso, now_utc

RH = CHAINS["robinhood"]
DEC = 10 ** 18
SEEDER, MINTER, GAUGE, USER_A, USER_B = ("0x" + c * 40 for c in "abcde")
_REAL_SLEEP = threading.Event().wait  # time.sleep в тестах отключён conftest'ом


def _tok(addr="0x" + "f" * 40, symbol="FAB", facts=None):
    uid, _ = discovery.upsert_token(RH, {"address": addr, "symbol": symbol}, source="test", reason="synthetic")
    db.ex("UPDATE tokens SET decimals=18, facts=? WHERE uid=?", (dumps(facts or {}), uid))
    return uid


def _enable(*mods):
    for m in mods:
        analytics.set_enabled(m, True)


def _hash_tables(*tables) -> dict:
    c = db.connect()
    out = {}
    for t in tables:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        h = hashlib.sha256()
        for row in c.execute(f"SELECT * FROM {t} ORDER BY {', '.join(cols)}"):
            h.update(repr(tuple(row)).encode())
        out[t] = h.hexdigest()
    c.close()
    return out


# ------------------------------------------------------------------ синтетический «FABLES»
def fables_like(with_gauge_role=False, circ_points=1):
    now = now_utc().replace(minute=0, second=0, microsecond=0)
    s1 = now - _dt.timedelta(days=16)
    starts = [s1 + _dt.timedelta(days=7 * i) for i in range(4)]  # нед.1, нед.2 завершены; нед.3 идёт; нед.4 в будущем
    uid = _tok(facts={"onchain": {}})
    db.ex("INSERT INTO address_labels(chain,address,label,kind,source_url,source_type,token_uid) VALUES(?,?,?,?,?,?,?)",
          ("robinhood", SEEDER, "Seeder Safe", "treasury", "https://docs.example/token", "official_docs", uid))
    db.ex("INSERT INTO address_labels(chain,address,label,kind,source_url,source_type,token_uid) VALUES(?,?,?,?,?,?,?)",
          ("robinhood", MINTER, "minter()", "minter", None, "onchain", uid))
    ev = [("mint", 500, SEEDER, 15_000_000), ("mint", 600, MINTER, 40_000_000), ("mint", 700, USER_A, 1_000),
          ("mint", 1500, GAUGE, 5_000_000), ("mint", 1600, USER_B, 2_000_000), ("mint", 2500, GAUGE, 4_000_000),
          ("burn", 2600, USER_B, 100), ("mint", 3500, GAUGE, 1_000_000)]
    db.exmany("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,ts,ts_source,counterparty,amount_raw,amount) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)",
              [(uid, k, f"0x{b:064x}", 0, b, None, None, a, str(v * DEC), float(v)) for k, b, a, v in ev])
    total = sum(v if k == "mint" else -v for k, _, _, v in ev) * DEC
    f = loads(db.q1("SELECT facts FROM tokens WHERE uid=?", (uid,))["facts"], {})
    f["onchain"] = {"reconciliation": {"matches": True, "at_block": 4000, "total_supply_raw": str(total), "diff_raw": "0"}}
    db.ex("UPDATE tokens SET facts=? WHERE uid=?", (dumps(f), uid))
    db.set_progress(f"mint_scan_from:{uid}", "1")
    db.set_progress(f"mint_scan_to:{uid}", "4000")
    db.set_progress(f"week_blocks:{uid}", dumps({iso(starts[0]): {"block": 1000}, iso(starts[1]): {"block": 2000},
                                                 iso(starts[2]): {"block": 3000}}))
    db.ex("INSERT INTO manual_facts(token_uid,fact_key,value,source_type,entered_at,active) VALUES(?,?,?,?,?,1)",
          (uid, "tge_date", iso(starts[0]), "official_docs", now_iso()))
    params = {"weekly_budgets": [{"week": i + 1, "start": iso(starts[i]), "amount": a} for i, a in enumerate([10_000_000, 9_800_000, 9_604_000, 9_411_920])],
              "emission_recipient_kinds": ["gauge"], "seeder_liquidity": 15_000_000, "team": "40M постоянный лок"}
    db.ex("INSERT INTO tokenomics_versions(token_uid,version_label,published_at,source_type,is_current,params,entered_at) VALUES(?,?,?,?,1,?,?)",
          (uid, "v-test", iso(starts[0] - _dt.timedelta(days=2)), "official_docs", dumps(params), now_iso()))
    for i in range(circ_points):
        db.observe("token", uid, "circulating_supply", 46_000_000 + i * 1_000_000, source="project_endpoint", unit="tokens")
    if with_gauge_role:
        analytics.add_role("robinhood", uid, GAUGE, "gauge", source_url="https://docs.example/contracts", source_type="official_docs",
                           evidence="адрес gauge в документации", confidence="confirmed")
    return uid


# ------------------------------------------------------------------ синтетический протокол для Momentum
def momentum_token(tvl_now=145.0, fees_cur=19.0, fees_prev=10.0, mcap_old=1e6, mcap_new=1.1e6, days=100):
    t1 = int(now_utc().timestamp()) // 86400 * 86400 - 86400
    ts = [t1 - i * 86400 for i in range(days)][::-1]
    tvl = [[t, 100.0] for t in ts]
    tvl[-1][1] = tvl_now
    fees = [[t, fees_cur if t > t1 - 30 * 86400 else fees_prev] for t in ts]
    p = {"slug": "proto", "name": "Proto", "category": "Dexs", "chains": [RH.llama], "at": now_iso(), "tvl_usd": tvl_now,
         "tvl_series": tvl, "fees": {"available": True, "chart": fees}, "revenue": {"available": False},
         "holders_revenue": {"available": False}, "dex_volume": {"available": False}}
    uid = _tok(addr="0x" + "9" * 40, symbol="MOM", facts={"protocol": p, "mcap": {"type": "CONFIRMED", "value": mcap_new}})
    db.ex("UPDATE tokens SET protocol_slug='proto' WHERE uid=?", (uid,))
    db.set_progress("chain_health:robinhood", dumps({"at": now_iso(), "tvl_series": [[t, 1000.0] for t in ts], "tvl_usd": 1000.0}))
    old = iso(now_utc() - _dt.timedelta(days=31))
    db.ex("INSERT INTO mcap_assessments(token_uid,assessed_at,mcap_type,value,method) VALUES(?,?,?,?,?)", (uid, old, "CONFIRMED", mcap_old, "m"))
    db.ex("INSERT INTO mcap_assessments(token_uid,assessed_at,mcap_type,value,method) VALUES(?,?,?,?,?)", (uid, now_iso(), "CONFIRMED", mcap_new, "m"))
    return uid


# ================================================================== 1–16
def test_01_disabled_modules_change_nothing():
    """Оба модуля выключены: планировщик не ставит аналитику, таблицы не создаются, API 2.0.1 работает как прежде."""
    uid = fables_like()
    assert not analytics.enabled("momentum") and not analytics.enabled("emission")
    r = jobs.Runner()
    put = r.plan()
    assert "analytics" not in put and r.lanes["analytics"].size() == 0
    cl = TestClient(__import__("app.api", fromlist=["app"]).app)
    assert cl.get("/api/analytics/status").json()["schema_present"] is False
    assert cl.get("/api/best").status_code == 200 and cl.get(f"/api/token/{uid}").status_code == 200
    assert cl.get(f"/api/analytics/token/{uid}").json()["emission"]["summary"] is None
    names = {x["name"] for x in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any(n.startswith("an_") for n in names) and "address_roles" not in names


def test_02_momentum_enabled_quotes_and_discovery_keep_running(monkeypatch):
    """Включён Momentum (и он долгий): задачи котировок и обнаружения выполняются в своих очередях без ожидания."""
    momentum_token()
    _enable("momentum")
    slow = threading.Event()
    orig = momentum.compute

    def slow_compute(uid, data):
        slow.set()
        _REAL_SLEEP(1.5)
        return orig(uid, data)
    monkeypatch.setattr(momentum, "compute", slow_compute)
    done = {}
    monkeypatch.setitem(jobs.TASKS, "quotes", lambda prog, uids: done.setdefault("quotes", _real_time.monotonic()))
    monkeypatch.setitem(jobs.TASKS, "discover", lambda prog, chain: done.setdefault("discover", _real_time.monotonic()))
    r = jobs.Runner()
    r.start(scheduler=False)
    try:
        r.submit("analytics", 6, module="momentum")
        assert slow.wait(3)
        t0 = _real_time.monotonic()
        r.submit("quotes", 0, uids=["x"])
        r.submit("discover", 1, chain="robinhood")
        for _ in range(40):
            if len(done) == 2:
                break
            _REAL_SLEEP(0.05)
        assert set(done) == {"quotes", "discover"}
        assert max(done.values()) - t0 < 1.0, "котировки/обнаружение ждали аналитику"
        assert r.lanes["analytics"].current is not None  # аналитика ещё идёт — в своей очереди
    finally:
        r.stop()


def test_03_emission_enabled_keeps_fables_data():
    uid = fables_like()
    before = _hash_tables("supply_events", "tokenomics_versions", "observations", "address_labels", "manual_facts")
    facts_before = db.q1("SELECT facts, level, status FROM tokens WHERE uid=?", (uid,))
    _enable("emission")
    st = analytics.run_module("emission", deadline_s=30)
    assert st["recomputed"] == 1 and st["errors"] == 0
    assert _hash_tables("supply_events", "tokenomics_versions", "observations", "address_labels", "manual_facts") == before
    assert tuple(db.q1("SELECT facts, level, status FROM tokens WHERE uid=?", (uid,))) == tuple(facts_before)


def test_04_analytics_error_does_not_stop_queues(monkeypatch):
    a = momentum_token()
    b = _tok(addr="0x" + "8" * 40, symbol="B2", facts={"protocol": {"slug": "p2"}})
    db.ex("UPDATE tokens SET protocol_slug='p2' WHERE uid=?", (b,))
    _enable("momentum")
    orig = momentum.compute
    monkeypatch.setattr(momentum, "compute", lambda uid, data: (_ for _ in ()).throw(ValueError("сбой расчёта")) if uid == a else orig(uid, data))
    st = analytics.run_module("momentum", deadline_s=30)
    assert st["errors"] == 1 and st["recomputed"] == 1  # второй токен посчитан
    assert db.q1("SELECT last_status FROM an_state WHERE module='momentum' AND token_uid=?", (a,))["last_status"] == "error"
    # то же в очереди: задание падает целиком — поток жив и выполняет следующие задачи
    monkeypatch.setattr(analytics, "run_module", lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("модуль упал")))
    ran = threading.Event()
    monkeypatch.setitem(jobs.TASKS, "quotes", lambda prog, uids: ran.set())
    r = jobs.Runner()
    r.start(scheduler=False)
    try:
        r.submit("analytics", 6, module="momentum")
        _REAL_SLEEP(0.5)
        r.submit("quotes", 0, uids=["x"])
        assert ran.wait(3)
        assert all(l.thread.is_alive() for l in r.lanes.values())
        r.submit("analytics", 6, module="momentum", uid="again")
        _REAL_SLEEP(0.5)
        assert r.lanes["analytics"].thread.is_alive()
    finally:
        r.stop()


def test_05_empty_source_does_not_erase_previous_data():
    """Аналитика не перезаписывает подтверждённые данные; пустые входные данные дают «нет данных», а не нули,
    и не удаляют прежний результат."""
    uid = momentum_token()
    _enable("momentum")
    analytics.run_module("momentum", deadline_s=30)
    first = analytics.latest("momentum", uid)
    assert first["verdict"] == "improving"
    f = loads(db.q1("SELECT facts FROM tokens WHERE uid=?", (uid,))["facts"], {})
    f["protocol"]["tvl_series"] = []
    f["protocol"]["fees"] = {"available": None, "error": "HTTP 503"}
    db.ex("UPDATE tokens SET facts=? WHERE uid=?", (dumps(f), uid))
    facts_raw = db.q1("SELECT facts FROM tokens WHERE uid=?", (uid,))["facts"]
    analytics.run_module("momentum", deadline_s=30)
    last = analytics.latest("momentum", uid)
    assert last["verdict"] == "insufficient"
    m = last["result"]["metrics"]
    assert m["fees"]["status"] == "unknown" and m["tvl_30d"]["value"] is None  # не 0
    assert db.q1("SELECT COUNT(*) n FROM an_results WHERE id=?", (first["id"],))["n"] == 1  # прежний результат в истории
    assert db.q1("SELECT facts FROM tokens WHERE uid=?", (uid,))["facts"] == facts_raw  # facts не тронуты


def test_06_genesis_not_counted_as_lp_without_evidence():
    uid = fables_like()
    _, data = ep.load(uid)
    r = ep.compute(uid, data)
    items = {g["address"]: g for g in r["genesis"]["items"]}
    assert items[SEEDER]["category"] == "genesis" and items[SEEDER]["evidence"] == "confirmed"
    assert items[MINTER]["category"] == "pre_tge_unclassified"  # крупный ранний mint без роли аллокации — не Genesis
    assert items[MINTER]["doc_hint"]  # совпадение суммы — только подсказка
    assert items[USER_A]["category"] == "pre_tge_unclassified"
    assert r["genesis"]["confirmed"]["raw"] == str(15_000_000 * DEC)
    assert r["lp_emission"]["amount"] is None and r["lp_emission"]["status"] == "unknown"  # gauge не помечен
    cats = {c["key"] for c in r["categories"]}
    assert "lp_emission" not in cats and "unknown" in cats


def test_07_reward_path_counted_once():
    recs = [{"stage": "mint", "tx": "0x1", "log": 0, "amount_raw": 100},
            {"stage": "transfer", "tx": "0x2", "log": 0, "amount_raw": 100},   # minter → distributor
            {"stage": "transfer", "tx": "0x3", "log": 0, "amount_raw": 100},   # distributor → gauge
            {"stage": "claim", "tx": "0x4", "log": 1, "amount_raw": 100},      # gauge → LP
            {"stage": "claim", "tx": "0x4", "log": 1, "amount_raw": 100}]      # повторное чтение того же события
    t = ep.stage_totals(recs)
    assert t["issued_raw"] == 100 and t["claimed_raw"] == 100 and t["duplicates_ignored"] == 1 and t["internal_transfers"] == 2


def test_08_mint_claim_sale_are_independent_stages():
    t = ep.stage_totals([{"stage": "mint", "tx": "a", "log": 0, "amount_raw": 100},
                         {"stage": "claim", "tx": "b", "log": 0, "amount_raw": 80},
                         {"stage": "sale", "tx": "c", "log": 0, "amount_raw": 30}])
    assert t["new_supply_raw"] == 100  # не 210
    assert (t["issued_raw"], t["claimed_raw"], t["sold_raw"]) == (100, 80, 30)


def test_09_incomplete_epoch_is_not_final():
    uid = fables_like(with_gauge_role=True)
    _, data = ep.load(uid)
    r = ep.compute(uid, data)
    w = {e["week"]: e for e in r["epochs"]}
    assert w[1]["utilization"]["status"] == "final" and w[1]["utilization"]["value_pct"] == pytest.approx(50.0)
    assert w[1]["utilization"]["remaining"]["raw"] == str(5_000_000 * DEC)
    assert w[2]["utilization"]["value_pct"] == pytest.approx(4_000_000 / 9_800_000 * 100)
    assert w[3]["state"] == "in_progress" and w[3]["utilization"]["status"] == "partial" and w[3]["utilization"]["final"] is False
    assert w[4]["state"] == "unconfirmed" and w[4]["utilization"]["status"] == "unknown"
    assert w[1]["minted_all"]["raw"] == str(7_000_000 * DEC)  # все mint ≠ LP-эмиссия
    assert w[1]["lp_emission"]["raw"] == str(5_000_000 * DEC)


def test_10_without_address_labels_utilization_unknown():
    uid = fables_like(with_gauge_role=False)
    _, data = ep.load(uid)
    r = ep.compute(uid, data)
    w1 = next(e for e in r["epochs"] if e["week"] == 1)
    assert w1["state"] == "complete" and w1["utilization"]["status"] == "unknown"
    assert "не проверено" in w1["utilization"]["reason"] and w1.get("lp_emission") is None


def test_11_unknown_circulating_is_not_estimated():
    uid = fables_like(circ_points=1)
    _, data = ep.load(uid)
    r = ep.compute(uid, data)
    assert r["circulating"]["status"] == "unknown" and r["circulating"].get("change") is None
    assert r["net_circulating_growth"]["status"] == "unknown"
    assert r["net_circulating_growth"]["components"]["new_locks"] is None


def test_11b_two_points_give_calculated_change():
    uid = fables_like(circ_points=2)
    ids = [r["id"] for r in db.q("SELECT id FROM observations WHERE metric='circulating_supply' ORDER BY id")]
    db.ex("UPDATE observations SET fetched_at=? WHERE id=?", (iso(now_utc() - _dt.timedelta(days=3)), ids[0]))
    _, data = ep.load(uid)
    r = ep.compute(uid, data)
    assert r["circulating"]["status"] == "calculated" and r["circulating"]["change"] == pytest.approx(1_000_000)


def test_12_rescan_does_not_duplicate_events_or_results():
    uid = fables_like()
    _enable("emission")
    analytics.run_module("emission", deadline_s=30)
    n_res = db.q1("SELECT COUNT(*) n FROM an_results")["n"]
    rows = db.q("SELECT * FROM supply_events WHERE token_uid=?", (uid,))
    # повторный проход ончейн-сканера по тем же блокам (INSERT OR IGNORE по блокчейн-идентичности tx+log)
    db.exmany("INSERT OR IGNORE INTO supply_events(token_uid,kind,tx_hash,log_index,block,counterparty,amount_raw,amount) VALUES(?,?,?,?,?,?,?,?)",
              [(r["token_uid"], r["kind"], r["tx_hash"], r["log_index"], r["block"], r["counterparty"], r["amount_raw"], r["amount"]) for r in rows])
    assert db.q1("SELECT COUNT(*) n FROM supply_events WHERE token_uid=?", (uid,))["n"] == len(rows)
    st = analytics.run_module("emission", deadline_s=30)
    assert st["unchanged"] == 1 and db.q1("SELECT COUNT(*) n FROM an_results")["n"] == n_res


def test_13_recompute_does_not_touch_history():
    uid = fables_like()
    momentum_token()
    _enable("emission", "momentum")
    tables = ("supply_events", "observations", "tokenomics_versions", "mcap_assessments", "manual_facts", "address_labels", "tokens")
    before = _hash_tables(*tables)
    analytics.run_module("emission", deadline_s=30, force=True)
    analytics.run_module("momentum", deadline_s=30, force=True)
    rid = analytics.add_role("robinhood", uid, GAUGE, "gauge", source_url="https://x", source_type="user", evidence="e", confidence="confirmed")
    analytics.run_module("emission", deadline_s=30)  # пересчёт после исправления меток
    analytics.deactivate_role(rid)
    analytics.run_module("emission", deadline_s=30)
    after = _hash_tables(*tables)
    assert after == before


def test_13b_reorg_orphaned_events_are_excluded_on_recompute():
    uid = fables_like(with_gauge_role=True)
    _enable("emission")
    analytics.run_module("emission", deadline_s=30)
    w1 = next(e for e in analytics.latest("emission", uid)["result"]["epochs"] if e["week"] == 1)
    assert w1["lp_emission"]["raw"] == str(5_000_000 * DEC)
    db.ex("UPDATE supply_events SET orphaned=1 WHERE token_uid=? AND block=1500", (uid,))  # событие исчезло при реорганизации
    st = analytics.run_module("emission", deadline_s=30)
    assert st["recomputed"] == 1
    w1 = next(e for e in analytics.latest("emission", uid)["result"]["epochs"] if e["week"] == 1)
    assert w1["lp_emission"]["raw"] == "0"
    assert db.q1("SELECT COUNT(*) n FROM supply_events WHERE token_uid=? AND block=1500", (uid,))["n"] == 1  # первичное не удалено


def test_14_no_unhandled_locked_during_concurrent_analytics_and_queues():
    uid = fables_like(with_gauge_role=True)
    momentum_token()
    _enable("emission", "momentum")
    errors, stop = [], threading.Event()
    cl = TestClient(__import__("app.api", fromlist=["app"]).app)

    def analytics_loop():
        while not stop.is_set():
            try:
                analytics.run_module("emission", deadline_s=5, force=True)
                analytics.run_module("momentum", deadline_s=5, force=True)
            except Exception as e:  # noqa: BLE001
                errors.append(f"analytics: {e!r}")

    def writer():
        i = 0
        while not stop.is_set():
            try:
                db.observe("token", uid, "price_usd", 0.01 + i, source="test")
                db.set_progress(f"k{i % 5}", str(i))
                i += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"writer: {e!r}")

    def user():
        while not stop.is_set():
            for path in (f"/api/hide/{uid}", f"/api/unhide/{uid}"):
                r = cl.post(path, json={"reason": "t"})
                if r.status_code >= 400:
                    errors.append(f"user {path}: {r.status_code} {r.text[:200]}")
    th = [threading.Thread(target=f) for f in (analytics_loop, analytics_loop, writer, writer, user)]
    for t in th:
        t.start()
    _REAL_SLEEP(2.5)
    stop.set()
    for t in th:
        t.join(10)
    assert not errors, errors[:5]
    assert db.q1("PRAGMA integrity_check")[0] == "ok"
    assert db.q1("SELECT COUNT(*) n FROM an_state WHERE last_status='error'")["n"] == 0


def test_15_long_analytics_does_not_block_hide_unhide(monkeypatch):
    uid = momentum_token()
    _enable("momentum")
    started = threading.Event()
    orig = momentum.compute

    def slow(u, data):
        started.set()
        _REAL_SLEEP(2.0)  # долгий расчёт — вне транзакции
        return orig(u, data)
    monkeypatch.setattr(momentum, "compute", slow)
    t = threading.Thread(target=lambda: analytics.run_module("momentum", deadline_s=30))
    t.start()
    assert started.wait(3)
    cl = TestClient(__import__("app.api", fromlist=["app"]).app)
    lat = []
    for path in (f"/api/hide/{uid}", f"/api/unhide/{uid}", f"/api/hide/{uid}"):
        t0 = _real_time.perf_counter()
        assert cl.post(path, json={"reason": "t"}).status_code == 200
        lat.append(_real_time.perf_counter() - t0)
    assert t.is_alive(), "расчёт должен ещё идти во время действий пользователя"
    t.join()
    assert max(lat) < 0.5, lat
    assert uid not in [r["uid"] for r in cl.get("/api/best").json()["rows"]]


def test_16_disable_stops_planning_and_keeps_db(monkeypatch):
    fables_like()
    _enable("emission")
    analytics._last_submit.clear()
    r = jobs.Runner()
    r.plan()
    assert r.lanes["analytics"].size() == 1
    analytics.set_enabled("emission", False)
    monkeypatch.setattr(jobs, "runner", r)  # set_enabled чистит очередь глобального runner
    analytics.set_enabled("emission", False)
    assert r.lanes["analytics"].size() == 0
    analytics._last_submit.clear()
    r.plan()
    assert r.lanes["analytics"].size() == 0
    assert analytics.task(lambda m: None, "emission") == {"skipped": "модуль выключен"}
    # уже идущий проход останавливается на следующем токене
    _enable("momentum")
    momentum_token()
    _tok(addr="0x" + "7" * 40, symbol="M2", facts={"protocol": {"slug": "x"}})
    db.ex("UPDATE tokens SET protocol_slug='x' WHERE symbol='M2'")
    orig = momentum.compute

    def off_after_first(u, data):
        analytics.set_enabled("momentum", False)
        return orig(u, data)
    monkeypatch.setattr(momentum, "compute", off_after_first)
    st = analytics.run_module("momentum", deadline_s=30)
    assert st["recomputed"] == 1 and st["stopped_reason"] == "модуль выключен пользователем"
    assert db.q1("PRAGMA integrity_check")[0] == "ok"
    assert analytics.schema_present()  # таблицы и результаты сохраняются; повторное включение их использует


# ================================================================== дополнительные проверки
def test_analytics_makes_no_network_requests():
    calls = []
    http.set_client(httpx.Client(transport=httpx.MockTransport(lambda req: calls.append(str(req.url)) or httpx.Response(200, json={}))))
    try:
        fables_like()
        momentum_token()
        _enable("emission", "momentum")
        analytics.run_module("emission", deadline_s=30)
        analytics.run_module("momentum", deadline_s=30)
        assert calls == []
        with analytics.no_network():
            with pytest.raises(RuntimeError, match="запрещён"):
                http.request_json("defillama", "GET", "https://api.llama.fi/protocols", retries=0)
        assert calls == []
    finally:
        http.set_client(None)


def test_momentum_divergence_example_and_alert():
    """Пример из задания: TVL +45%, комиссии +90%, MCAP +10% → событие для исследования (не рекомендация)."""
    uid = momentum_token(tvl_now=145.0, fees_cur=19.0, fees_prev=10.0, mcap_old=1e6, mcap_new=1.1e6)
    _enable("momentum")
    analytics.run_module("momentum", deadline_s=30)
    r = analytics.latest("momentum", uid)["result"]
    assert r["verdict"] == "improving" and r["window"] == 30
    assert r["metrics"]["tvl_30d"]["value"] == pytest.approx(45.0)
    assert r["metrics"]["fees"]["w30"]["change"] == pytest.approx(90.0)
    assert r["market"]["mcap_30d"]["value"] == pytest.approx(10.0)
    d = r["divergence"]
    assert d and d["market_kind"] == "mcap" and d["gap_pp"] > 20
    a = db.q1("SELECT * FROM alerts WHERE token_uid=? AND alert_type='momentum_divergence'", (uid,))
    assert a and "не рекомендация" in a["limitations"]
    analytics.run_module("momentum", deadline_s=30, force=True)
    assert db.q1("SELECT COUNT(*) n FROM alerts WHERE token_uid=? AND alert_type='momentum_divergence'", (uid,))["n"] == 1


def test_momentum_requires_two_comparable_periods():
    uid = momentum_token(days=20)  # меньше 30 дней истории
    _, data = momentum.load(uid)
    r = momentum.compute(uid, data)
    assert r["metrics"]["tvl_30d"]["value"] is None and r["metrics"]["fees"]["w30"]["change"] is None
    assert r["windows"][30]["verdict"] == "insufficient"


def test_momentum_estimated_mcap_not_treated_as_confirmed():
    uid = momentum_token()
    f = loads(db.q1("SELECT facts FROM tokens WHERE uid=?", (uid,))["facts"], {})
    f["mcap"] = {"type": "ESTIMATED", "value": 5e6}
    db.ex("UPDATE tokens SET facts=? WHERE uid=?", (dumps(f), uid))
    _, data = momentum.load(uid)
    r = momentum.compute(uid, data)
    assert r["market"]["mcap_now"]["status"] == "estimated"
    assert "fees_annual_to_mcap_pct" not in r["market"] and "ratio_note" in r["market"]


def test_role_conflict_and_no_transfer_between_tokens_or_chains():
    uid = fables_like()
    other = _tok(addr="0x" + "1" * 40, symbol="OTH")
    analytics.add_role("robinhood", uid, GAUGE, "gauge", source_url="https://a", source_type="user", evidence="e", confidence="confirmed")
    assert analytics.roles_for("robinhood", other).get(GAUGE) is None      # роль токена не переносится на другой токен
    assert analytics.roles_for("base", uid).get(GAUGE) is None             # и на другую сеть
    analytics.add_role("robinhood", uid, GAUGE, "cex_deposit", source_url="https://b", source_type="user", evidence="e", confidence="probable")
    r = analytics.roles_for("robinhood", uid)[GAUGE]
    assert r["conflict"] and r["role"] == "unknown"
    _, data = ep.load(uid)
    res = ep.compute(uid, data)
    assert res["conflicts"] and res["lp_emission"]["status"] == "unknown"  # конфликтная роль не применяется
    with pytest.raises(ValueError):
        analytics.add_role("robinhood", uid, SEEDER, "team", source_url=None, source_type="user", evidence=None, confidence="confirmed")


def test_totals_reconcile_exactly_in_integer_units():
    uid = fables_like()
    _, data = ep.load(uid)
    r = ep.compute(uid, data)
    t = r["totals"]
    assert t["reconciled_exactly"] is True and t["net_at_reconciled_block"]["raw"] == t["total_supply_rpc"]["raw"]
    assert isinstance(t["minted"]["raw"], str) and int(t["minted"]["raw"]) == (15_000_000 + 40_000_000 + 1_000 + 12_000_000) * DEC


def test_new_tables_do_not_break_migration_or_2_0_1_code_reading():
    """Аналитические таблицы — только дополнительные: init_db повторно проходит, схема 2.0.1 не меняется."""
    fables_like()
    _enable("emission")
    db.init_db()
    v = db.q1("SELECT value FROM meta WHERE key='schema_version'")["value"]
    assert v == "2" and db.q1("SELECT value FROM meta WHERE key='analytics_schema'")["value"] == "1"


def test_analytics_cpu_share_is_limited(monkeypatch):
    """После расчёта токена очередь аналитики делает паузу pause_ratio × время расчёта: в стресс-тесте без этого
    непрерывный пересчёт отнимал время у котировок и обнаружения (общий GIL процесса)."""
    momentum_token()
    for i in range(2):
        u = _tok(addr="0x" + str(i + 2) * 40, symbol=f"T{i}", facts={"protocol": {"slug": f"s{i}"}})
        db.ex("UPDATE tokens SET protocol_slug=? WHERE uid=?", (f"s{i}", u))
    _enable("momentum")
    orig = momentum.compute

    def busy(u, data):
        t = _real_time.perf_counter()
        while _real_time.perf_counter() - t < 0.05:  # 50 мс «вычислений»
            pass
        return orig(u, data)
    monkeypatch.setattr(momentum, "compute", busy)
    t0 = _real_time.perf_counter()
    st = analytics.run_module("momentum", deadline_s=30, force=True)
    took = _real_time.perf_counter() - t0
    assert st["recomputed"] == 3
    assert took >= 3 * 0.05 * (1 + 6.0) * 0.9, f"доля времени аналитики не ограничена: {took:.2f} с"
    stop = threading.Event()
    stop.set()
    assert analytics.run_module("momentum", deadline_s=30, force=True, stop=stop)["stopped_reason"] == "остановка программы"
