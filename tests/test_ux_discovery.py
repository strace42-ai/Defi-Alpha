"""Discovery & UX, этап A: фиксированная дата TGE, сортировка (сервер и интерфейс), рейтинг с фильтрами и лучшие по сетям."""
from __future__ import annotations

import datetime as _dt
import json
import socket
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from app import config, db, discovery, filters, pipeline
from app.api import app, sort_rows
from app.chains import CHAINS
from app.util import dumps, now_iso

UTC = _dt.timezone.utc
cl = TestClient(app)


def _manual(tge: str | None):
    return [{"fact_key": "tge_date", "value": tge, "source_type": "official_docs"}] if tge else []


def _facts(first_trade: str | None):
    return {"ath": {"first_trade_at": first_trade}} if first_trade else {}


# ---------------------------------------------------------------- 1–3, 5: фильтр TGE
def test_01_fixed_date_keeps_token_after_90_days():
    far = _dt.datetime(2027, 6, 1, tzinfo=UTC)  # почти 300 дней после TGE
    r = filters.f_age(_facts("2026-08-10T00:00:00Z"), _manual("2026-08-10T00:00:00Z"), far, 90, "fixed_date", "2026-08-01")
    assert r["status"] == "PASS" and r["confidence"] == "confirmed"
    r_old = filters.f_age(_facts("2026-07-20T00:00:00Z"), _manual("2026-07-20T00:00:00Z"), far, 90, "fixed_date", "2026-08-01")
    assert r_old["status"] == "FAIL"


def test_02_rolling_mode_b_still_works():
    now = _dt.datetime(2026, 11, 5, tzinfo=UTC)
    assert filters.f_age({}, _manual("2026-10-20T00:00:00Z"), now, 90, "max_age", None)["status"] == "PASS"
    assert filters.f_age({}, _manual("2026-07-01T00:00:00Z"), now, 90, "max_age", None)["status"] == "FAIL"


def test_unconfirmed_tge_is_unknown_not_pass_and_pool_date_is_only_a_reference():
    r = filters.f_age(_facts("2026-09-01T00:00:00Z"), [], _dt.datetime(2027, 3, 1, tzinfo=UTC), 90, "fixed_date", "2026-08-01")
    assert r["status"] == "UNKNOWN" and r["values"]["preliminary_start"].startswith("2026-09-01")
    # торговля раньше даты доказывает, что токен старше — это FAIL и без TGE
    r2 = filters.f_age(_facts("2026-06-01T00:00:00Z"), [], _dt.datetime(2026, 11, 1, tzinfo=UTC), 90, "fixed_date", "2026-08-01")
    assert r2["status"] == "FAIL"


def test_05_bow_not_dropped_by_age_in_fixed_mode():
    """BOW: TGE не подтверждён, первая сделка 2026-08-08. В скользящем режиме через 90 дн. — FAIL; в фиксированном — нет."""
    later = _dt.datetime(2026, 12, 31, tzinfo=UTC)
    f = _facts("2026-08-08T00:00:00Z")
    assert filters.f_age(f, [], later, 90, "max_age", None)["status"] == "FAIL"           # причина исчезновения в 2.1.1
    assert filters.f_age(f, [], later, 90, "fixed_date", "2026-08-07")["status"] == "UNKNOWN"  # остаётся в выборке


def test_tge_date_migrates_once_and_does_not_move(monkeypatch, tmp_path):
    p = config.SETTINGS_PATH
    s = json.loads(p.read_text(encoding="utf-8"))
    s["filters"].pop("tge_from_date", None)
    p.write_text(json.dumps(s), encoding="utf-8")
    config._cache = None
    d1 = config.load_settings()["filters"]["tge_from_date"]
    expect = (_dt.datetime.now(UTC) - _dt.timedelta(days=90)).date().isoformat()
    assert d1 == expect and json.loads(p.read_text(encoding="utf-8"))["filters"]["tge_from_date"] == d1  # записано в файл
    config._cache = None
    assert config.load_settings()["filters"]["tge_from_date"] == d1  # повторная загрузка не пересчитывает


def _token(i: int, chain="robinhood", level="C", score=50.0, price: float | None = 1.0, status="PASS"):
    uid, _ = discovery.upsert_token(CHAINS[chain], {"address": "0x" + f"{i:040x}", "symbol": f"T{i:03d}", "name": f"Token {i}"},
                                    source="test", reason="synthetic")
    f = {"price": {"value": price}} if price is not None else {}
    db.ex("UPDATE tokens SET level=?, status=?, score_preliminary=?, asset_class='token', facts=?, last_market_at=? WHERE uid=?",
          (level, status, score, dumps(f), now_iso(), uid))
    return uid


def test_03_changing_date_reevaluates_without_deleting_history():
    uid = _token(1)
    db.ex("INSERT INTO manual_facts(token_uid,fact_key,value,source_type,entered_at,active) VALUES(?,?,?,?,?,1)",
          (uid, "tge_date", "2026-09-15T00:00:00Z", "official_docs", now_iso()))
    s = config.load_settings()
    s["filters"].update(tge_mode="fixed_date", tge_from_date="2026-09-01")
    config.save_settings(s)
    pipeline.evaluate(uid, old_facts=None)
    assert db.q1("SELECT status FROM filter_current WHERE token_uid=? AND filter_key='age'", (uid,))["status"] == "PASS"
    s["filters"]["tge_from_date"] = "2026-10-01"
    config.save_settings(s)
    pipeline.evaluate(uid, old_facts=None)
    assert db.q1("SELECT status FROM filter_current WHERE token_uid=? AND filter_key='age'", (uid,))["status"] == "FAIL"
    hist = [r["status"] for r in db.q("SELECT status FROM filter_results WHERE token_uid=? AND filter_key='age' ORDER BY id", (uid,))]
    assert hist == ["PASS", "FAIL"]  # прежнее решение не удалено
    assert db.q1("SELECT 1 FROM tokens WHERE uid=?", (uid,))  # токен и история на месте


def test_04_hidden_tokens_stay_hidden_after_reevaluation():
    a, b = _token(2, score=90), _token(3, score=80)
    assert cl.post(f"/api/hide/{a}", json={"reason": "t"}).status_code == 200
    s = config.load_settings()
    s["filters"]["tge_from_date"] = "2026-01-01"
    config.save_settings(s)
    for u in (a, b):
        pipeline.evaluate(u, old_facts=None)
    db.ex("UPDATE tokens SET level='C', status='PASS' WHERE uid IN (?,?)", (a, b))
    uids = [r["uid"] for r in cl.get("/api/best?limit=100").json()["rows"]]
    assert a not in uids and b in uids
    assert db.q1("SELECT active FROM hidden_tokens WHERE token_uid=?", (a,))["active"] == 1


# ---------------------------------------------------------------- 9, 10, 12, 13: сортировка и рейтинг
def test_09_10_sort_numbers_dates_strings_nulls_last():
    rows = [{"uid": "a", "price": 3.0, "symbol": "beta", "first_trade": "2026-09-01", "level": "C"},
            {"uid": "b", "price": None, "symbol": "Alpha", "first_trade": None, "level": "E"},
            {"uid": "c", "price": 10.0, "symbol": "gamma", "first_trade": "2026-08-01", "level": "A"},
            {"uid": "d", "price": 2.5, "symbol": None, "first_trade": "2026-10-01", "level": None}]
    ids = lambda rs: [r["uid"] for r in rs]
    assert ids(sort_rows(rows, "price", "asc")) == ["d", "a", "c", "b"]
    assert ids(sort_rows(rows, "price", "desc")) == ["c", "a", "d", "b"]          # пусто — внизу и при убывании
    assert ids(sort_rows(rows, "symbol", "asc")) == ["b", "a", "c", "d"]          # без учёта регистра, пусто внизу
    assert ids(sort_rows(rows, "first_trade", "asc")) == ["c", "a", "d", "b"]     # хронологически
    assert ids(sort_rows(rows, "level", "desc")) == ["b", "a", "c", "d"]          # E > C > A, пусто внизу
    same = [{"uid": "z", "price": 1.0}, {"uid": "y", "price": 1.0}]
    assert ids(sort_rows(same, "price", "asc")) == ["y", "z"]                     # стабильный дополнительный ключ


def test_12_server_sort_covers_all_pages():
    for i in range(45):
        _token(100 + i, price=None if i % 9 == 0 else float((i * 37) % 45 + 1), score=float(i))
    p1 = cl.get("/api/best?limit=20&offset=0&sort=price&order=asc").json()
    p2 = cl.get("/api/best?limit=20&offset=20&sort=price&order=asc").json()
    p3 = cl.get("/api/best?limit=20&offset=40&sort=price&order=asc").json()
    allrows = p1["rows"] + p2["rows"] + p3["rows"]
    assert p1["total"] == 45 and len(allrows) == 45
    prices = [r["price"] for r in allrows]
    known = [x for x in prices if x is not None]
    assert known == sorted(known) and prices[len(known):] == [None] * (45 - len(known))  # глобальный порядок, пусто в конце
    t = cl.get("/api/tokens?limit=10&offset=10&sort=price&order=desc").json()
    assert t["total"] >= 45 and len(t["rows"]) == 10


def test_13_chain_view_not_limited_by_global_top():
    s = config.load_settings()
    s["top_candidates_n"] = 3
    config.save_settings(s)
    for i in range(10):
        _token(300 + i, chain="robinhood", score=100.0 - i)
    for i in range(4):
        _token(400 + i, chain="base", score=10.0 - i)  # хуже всех RH — в глобальную первую страницу не попадают
    g = cl.get("/api/best").json()
    assert len(g["rows"]) == 3 and all(r["chain"] == "robinhood" for r in g["rows"]) and g["total"] == 14
    b = cl.get("/api/best?chain=base").json()
    assert [r["chain_rank"] for r in b["rows"]] == [1, 2, 3] and b["total"] == 4 and b["rows"][0]["rank"] == 11
    assert g["by_chain"] == {"robinhood": 10, "base": 4}


def test_best_filters_search_mcap_and_start_date():
    a = _token(500, price=1.0)
    db.ex("UPDATE tokens SET symbol='FINDME', facts=? WHERE uid=?", (dumps({"price": {"value": 1.0}, "mcap": {"value": 2e6, "type": "CONFIRMED", "points": []},
                                                                         "ath": {"first_trade_at": "2026-09-10T00:00:00Z"}}), a))
    _token(501, price=1.0)
    assert [r["uid"] for r in cl.get("/api/best?search=FINDME").json()["rows"]] == [a]
    assert a in [r["uid"] for r in cl.get("/api/best?mcap_max=3000000").json()["rows"]]
    assert a not in [r["uid"] for r in cl.get("/api/best?mcap_max=1000000").json()["rows"]]
    assert a not in [r["uid"] for r in cl.get("/api/best?start_from=2026-10-01").json()["rows"]]


def test_14_15_analytics_unaffected_and_integrity():
    from app import analytics, momentum
    uid = _token(600)
    db.ex("UPDATE tokens SET protocol_slug='p' WHERE uid=?", (uid,))
    _, data = momentum.load(uid)
    before = momentum.compute(uid, data)
    s = config.load_settings()
    s["filters"].update(tge_mode="fixed_date", tge_from_date="2026-05-01")
    config.save_settings(s)
    pipeline.evaluate(uid, old_facts=None)
    _, data = momentum.load(uid)
    after = momentum.compute(uid, data)
    assert before["summary"] == after["summary"]
    assert db.q1("PRAGMA integrity_check")[0] == "ok"
    _ = analytics


# ---------------------------------------------------------------- 11: интерфейс (настоящий браузер)
UI = r"""
const { chromium } = require('playwright');
(async () => {
  const base = process.argv[2];
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  const syms = async () => p.$$eval('#tw table tbody tr', rs => rs.map(r => r.querySelector('td b').textContent));
  await p.goto(base + '/#/scanner'); await p.waitForSelector('#tw table');
  const out = { def: await syms() };
  const th = 'th[data-k="price"]';
  await p.click(th); out.asc = await syms(); out.arrow1 = await p.getAttribute(th, 'data-arrow');
  await p.click(th); out.desc = await syms();
  await p.evaluate(async () => { await render(true); });   // автообновление
  out.afterRefresh = await syms(); out.arrow2 = await p.getAttribute(th, 'data-arrow');
  await p.click(th); out.reset = await syms();
  out.parse = await p.evaluate(() => { const mk = t => { const d = document.createElement('td'); d.textContent = t; return cellValue(d); };
    return [mk('$1,5 млн'), mk('−48.5%'), mk('89 дн.'), mk('05.11.2026, 08:40'), mk('н/д'), mk('1INCH')]; });
  out.errors = errs; console.log(JSON.stringify(out)); await b.close();
})();
"""


def _node_ok() -> bool:
    import shutil
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _node_ok(), reason="Playwright/Node недоступен — UI-тест не выполнялся")
def test_11_sort_three_states_nulls_last_and_kept_on_refresh(tmp_path):
    import uvicorn
    for i, pr in enumerate([5.0, None, 1.0, 3.0]):
        _token(700 + i, price=pr, score=float(i))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        threading.Event().wait(0.05)
    js = tmp_path / "ui.js"
    js.write_text(UI, encoding="utf-8")
    r = subprocess.run(["node", str(js), f"http://127.0.0.1:{port}"], capture_output=True, text=True, timeout=120)
    srv.should_exit = True
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["errors"] == []
    assert o["asc"] == ["T702", "T703", "T700", "T701"] and o["arrow1"] == "▲"     # 1 → 3 → 5, н/д внизу
    assert o["desc"] == ["T700", "T703", "T702", "T701"]                          # 5 → 3 → 1, н/д внизу
    assert o["afterRefresh"] == o["desc"] and o["arrow2"] == "▼"                  # сохраняется при автообновлении
    assert o["reset"] == o["def"]                                                 # третье нажатие — порядок по умолчанию
    assert o["parse"] == [1_500_000, -48.5, 89, o["parse"][3], None, "1inch"] and o["parse"][3] > 0


# ---------------------------------------------------------------- этап B: справедливость сетей, лимиты, дубли, покрытие
def test_06_large_rh_batch_does_not_block_base():
    from app import jobs
    items = [(3, f"robinhood:0x{i:040x}") for i in range(200)] + [(3, "base:0x" + "b" * 40), (3, "eth:0x" + "e" * 40)]
    picked = [u for _, u in jobs.fair_pick(items, 3)]
    assert {u.split(":")[0] for u in picked} == {"robinhood", "base", "eth"}  # каждая сеть получает место в первом же такте
    # приоритет уровня важнее чередования: срочные (уровень 0) идут первыми
    items2 = [(0, "robinhood:0x" + "1" * 40)] + items
    assert jobs.fair_pick(items2, 2)[0] == (0, "robinhood:0x" + "1" * 40)
    # стартовая сеть сдвигается от такта к такту — ни одна сеть не ждёт бесконечно
    firsts = {jobs.fair_pick(items, 1)[0][1].split(":")[0] for _ in range(6)}
    assert firsts == {"robinhood", "base", "eth"}


def test_06b_plan_selects_across_chains(monkeypatch):
    from app import jobs, refresh
    rh = [(3, f"robinhood:0x{i:040x}") for i in range(90)]
    other = [(3, "base:0x" + "b" * 40), (3, "bsc:0x" + "c" * 40)]
    monkeypatch.setattr(refresh, "due_tokens", lambda kind, limit=30: (rh + other)[:limit])
    monkeypatch.setattr(jobs.Runner, "_src_ok", lambda self, job: True)
    r = jobs.Runner()
    r.plan()
    deep = [j["kw"]["uid"] for j in r.lanes["main"].keys.values() if j["job"] == "deep"]
    assert {u.split(":")[0] for u in deep} >= {"base", "bsc"}  # 90 токенов RH не заняли все места


def test_07_api_limits_and_queue_bounds_respected(monkeypatch):
    from app import http, jobs
    s = config.load_settings()
    s["rate_limits_per_min"]["geckoterminal"] = 9
    s["queue_max_per_lane"] = 5
    config.save_settings(s)
    http.reset_limiters()
    assert abs(http.limiter("geckoterminal").interval - 60 / 9) < 1e-9  # общий лимит источника задаётся настройкой
    r = jobs.Runner()
    for i in range(20):
        r.submit("deep", 3, uid=f"robinhood:0x{i:040x}")
    assert r.lanes["main"].size() == 5  # очередь ограничена


def test_08_no_duplicate_jobs():
    from app import jobs
    r = jobs.Runner()
    a = r.submit("deep", 3, uid="base:0x" + "1" * 40)
    b = r.submit("deep", 2, uid="base:0x" + "1" * 40)
    assert a == b and r.lanes["main"].size() == 1


def test_coverage_report_from_stored_data_only():
    import httpx
    from app import coverage, http
    calls = []
    http.set_client(httpx.Client(transport=httpx.MockTransport(lambda q: calls.append(1) or httpx.Response(200, json={}))))
    try:
        _token(800, chain="base")
        _token(801, chain="robinhood", level="B")
        rep = cl.get("/api/coverage").json()
        assert calls == []
        rows = {c["chain"]: c for c in rep["chains"]}
        assert rows["base"]["tokens"] >= 1 and rows["base"]["coverage_pct"] is None   # процент не выдумывается
        assert rows["base"]["onchain_logs"] is False and rows["robinhood"]["level_b"] >= 1
        assert rows["base"]["in_ranking"] >= 1
    finally:
        http.set_client(None)
