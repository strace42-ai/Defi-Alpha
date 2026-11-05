"""Регрессионные тесты релиза 2.0 (пункты 14.1–14.16 и 8.1–8.12 ТЗ).
Все данные здесь ИСКУССТВЕННЫЕ (синтетические) и сконструированы для проверки логики; сеть не используется
(HTTP перехватывается respx)."""
import json
import sqlite3
import threading
import time as _real_time
from pathlib import Path

import httpx
import pytest
import respx

from app import config, db, discovery, enrich, filters as F, http, jobs, mcap as M, pipeline, refresh
from app.chains import CHAINS
from app.util import dumps, now_iso

FIX = Path(__file__).resolve().parent / "fixtures"
RH = CHAINS["robinhood"]
DS = "https://api.dexscreener.com"


def tok(addr="0x" + "3" * 40, sym="TST", facts=None, level=None):
    uid, _ = discovery.upsert_token(RH, {"address": addr, "symbol": sym}, source="test", reason="synthetic")
    if facts is not None:
        db.ex("UPDATE tokens SET facts=?, last_market_at=? WHERE uid=?", (dumps(facts), now_iso(), uid))
    if level:
        db.ex("UPDATE tokens SET level=?, status='MANUAL REVIEW', score_preliminary=70 WHERE uid=?", (level, uid))
    return uid


def healthy_chain():
    db.set_progress("chain_health:robinhood", json.dumps({"tvl_usd": 1e9, "tvl_change_30d": 5, "tvl_change_90d": 50,
                                                         "stables_usd": 1e9, "dex_volume_30d": 1e10, "fees_30d": 1e7, "at": now_iso()}))


GOOD = {"price": {"value": 0.04, "confidence": "derived"}, "earliest_pool": {"at": now_iso()},
        "ath": {"ath_usd": 0.05, "first_trade_at": now_iso(), "first_candle_at": now_iso()},
        "gt": {"liquidity_usd": 5e5, "volume_h24": 1e5},
        "circulating_official": {"value": 50e6, "source_type": "official_docs", "url": "https://docs.example/supply"},
        "protocol_link": {"linked": {"slug": "x", "name": "X", "category": "Dexs", "method": "defillama_address"}},
        "protocol": {"tvl_usd": 2e6, "fees": {"available": True, "total30d": 5e4}, "holders_revenue": {"available": True, "total30d": 1e4}}}


# ================================================================ 14.1 / 14.2 — пять фильтров, VC не исключает
def test_14_1_confirmed_vc_not_excluded():
    healthy_chain()
    uid = tok(facts=dict(GOOD))
    db.ex("INSERT INTO manual_facts(token_uid,fact_key,value,category,amount_usd,entered_at) VALUES(?,?,?,?,?,?)",
          (uid, "funding", "Fund X Seed", "vc_fund", 10e6, now_iso()))
    r = pipeline.evaluate(uid)
    assert "vc" not in r["filters"]
    assert r["status"] != "FAIL"
    f = enrich.get_facts(uid)
    assert f["funding"]["status"] == "vc_present" and f["funding"]["risks"]  # учтено как риск, не исключение


def test_14_2_exactly_five_filters_and_level_e_without_vc():
    healthy_chain()
    uid = tok(facts=dict(GOOD))
    r = pipeline.evaluate(uid)
    assert set(r["filters"]) == {"age", "mcap", "drawdown", "utility", "ecosystem"}
    assert r["filters"]["mcap"]["status"] == "PASS" and r["filters"]["mcap"]["confidence"] == "confirmed"  # сценарий A: 50M × 0.04 = 2M
    assert "6" not in r["reason"]


# ================================================================ 14.3 — миграция без потери данных
def _make_v1_db(path: Path) -> None:
    c = sqlite3.connect(str(path))
    c.executescript((FIX / "schema_v1.sql").read_text(encoding="utf-8"))
    c.execute("INSERT INTO meta VALUES('schema_version','1')")
    c.execute("INSERT INTO tokens(uid,chain,address,first_seen_at,symbol,level,status) VALUES('robinhood:0xa','robinhood','0xa','2026-10-01T00:00:00Z','A','C','MANUAL REVIEW')")
    c.execute("INSERT INTO observations(entity_type,entity_id,metric,value,source,fetched_at) VALUES('token','robinhood:0xa','price_usd',1.5,'gt','2026-10-01T00:00:00Z')")
    c.execute("INSERT INTO tokenomics_versions(token_uid,version_label,params,entered_at) VALUES('robinhood:0xa','v1','{}','x')")
    c.execute("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,amount_raw,amount,ts) VALUES('robinhood:0xa','mint','0x1',0,5,'100',1e-16,'2026-10-01T00:00:00Z')")
    c.execute("INSERT INTO address_labels(chain,address,label,kind) VALUES('robinhood','0xb','Escrow','escrow')")
    c.execute("INSERT INTO watchlist(token_uid,added_at,note) VALUES('robinhood:0xa','x','моя заметка')")
    c.execute("INSERT INTO filter_results(token_uid,filter_key,status,evaluated_at) VALUES('robinhood:0xa','vc','UNKNOWN','x')")
    c.execute("INSERT INTO filter_current(token_uid,filter_key,status,evaluated_at) VALUES('robinhood:0xa','vc','UNKNOWN','x')")
    c.execute("INSERT INTO manual_facts(token_uid,fact_key,value,entered_at) VALUES('robinhood:0xa','funding','x','x')")
    c.commit()
    c.close()


def test_14_3_migration_preserves_data_and_can_roll_back(tmp_path):
    p = config.DATA_DIR / "scanner.db"
    db.set_db_path(p)
    _make_v1_db(p)
    tables = ["tokens", "observations", "tokenomics_versions", "supply_events", "address_labels", "watchlist", "filter_results", "manual_facts"]
    before = {t: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in tables}
    info = db.init_db()
    assert info["from_version"] == 1 and info["backup"] and Path(info["backup"]).exists()
    after = {t: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in tables}
    assert before == after  # ничего не потеряно
    assert db.q1("SELECT note, status FROM watchlist")["note"] == "моя заметка"
    assert db.q1("SELECT COUNT(*) n FROM filter_results WHERE filter_key='vc'")["n"] == 1  # история решений сохранена
    assert db.q1("SELECT COUNT(*) n FROM filter_current WHERE filter_key='vc'")["n"] == 0  # снят только текущий кеш
    assert db.q1("SELECT value FROM meta WHERE key='schema_version'")["value"] == "2"
    assert db.init_db()["backup"] is None  # повторный запуск — без повторной миграции
    restored = db.rollback_migration()
    assert restored is not None
    db.set_db_path(p)
    assert db.q1("SELECT value FROM meta WHERE key='schema_version'")["value"] == "1"
    assert db.q1("SELECT COUNT(*) n FROM supply_events")["n"] == 1


def test_14_3_settings_migration_keeps_user_values():
    s = config.load_settings()
    s["settings_version"] = 1
    s["scheduler_enabled"] = False
    s["filters"]["max_age_days"] = 60
    config.SETTINGS_PATH.write_text(json.dumps(s), encoding="utf-8")
    config._cache = None
    n = config.load_settings()
    assert n["scheduler_enabled"] is True and n["filters"]["max_age_days"] == 60 and n["settings_version"] == 2
    n["scheduler_enabled"] = False  # дальше пользователь управляет сам
    config.save_settings(n)
    config._cache = None
    assert config.load_settings()["scheduler_enabled"] is False


# ================================================================ 14.4–14.8 — планировщик
def test_14_4_scheduler_plans_discovery_automatically():
    r = jobs.Runner()
    assert config.load_settings()["scheduler_enabled"] is True
    put = r.plan()
    assert put.get("discover") == len(config.enabled_chains())


def test_14_5_duplicates_and_bounded_queue():
    r = jobs.Runner()
    a = r.submit("discover", 1, chain="robinhood")
    b = r.submit("discover", 1, chain="robinhood")
    assert a == b and r.lanes["main"].size() == 1
    s = config.load_settings()
    s["queue_max_per_lane"] = 3
    config.save_settings(s)
    for i in range(5):
        r.submit("deep", 4, uid=f"x{i}")
    assert r.lanes["main"].size() == 3  # очередь не растёт бесконечно


def _wait(cond, timeout=10.0):
    t0 = _real_time.monotonic()
    while _real_time.monotonic() - t0 < timeout:
        if cond():
            return True
        threading.Event().wait(0.05)
    return False


def test_14_6_network_failure_does_not_stop_lanes(monkeypatch):
    r = jobs.Runner()
    done = []

    def boom(prog, **kw):
        raise http.SourceError("geckoterminal", "unavailable", "нет сети")
    monkeypatch.setitem(jobs.TASKS, "discover", boom)
    monkeypatch.setitem(jobs.TASKS, "deep", lambda prog, **kw: done.append(kw["uid"]) or {})
    r.start(scheduler=False)
    r.submit("discover", 1, chain="robinhood")
    r.submit("deep", 2, uid="u1")
    assert _wait(lambda: done == ["u1"])
    assert db.q1("SELECT status FROM jobs WHERE job='discover'")["status"] == "error"
    r.stop()


def test_14_7_open_token_refreshes_without_user_action():
    uid = tok(level="B")
    refresh.set_focus(uid)
    assert refresh.tiers()[uid] == 0
    r = jobs.Runner()
    r.plan()
    queued = [j for j in r.lanes["fast"].keys.values() if j["job"] == "quotes"]
    assert any(uid in j["kw"]["uids"] for j in queued) and min(j["priority"] for j in queued) == 0


def test_14_8_long_onchain_scan_does_not_block_quotes(monkeypatch):
    r = jobs.Runner()
    gate = threading.Event()
    seen = []
    monkeypatch.setitem(jobs.TASKS, "onchain", lambda prog, **kw: gate.wait(5) or {})
    monkeypatch.setitem(jobs.TASKS, "quotes", lambda prog, **kw: seen.append("quotes") or {})
    r.start(scheduler=False)
    r.submit("onchain", 0, uid="slow")
    assert _wait(lambda: r.lanes["chain"].current is not None)
    r.submit("quotes", 0, uids=["a"])
    assert _wait(lambda: seen == ["quotes"], 3)  # котировки получены, пока ончейн-скан ещё идёт
    assert r.lanes["chain"].current is not None
    gate.set()
    r.stop()


# ================================================================ 14.9–14.11 — скрытие, восстановление, наблюдение
def test_14_9_10_hidden_not_returned_and_restore():
    from fastapi.testclient import TestClient
    from app.api import app
    uid = tok(level="C")
    assert uid in pipeline.best_candidates()
    c = TestClient(app)
    c.post(f"/api/hide/{uid}", json={"reason": "не интересен"})
    assert uid not in pipeline.best_candidates()
    db.ex("UPDATE tokens SET level='C', status='MANUAL REVIEW' WHERE uid=?", (uid,))  # «пересчёт» снова сделал его кандидатом
    assert uid not in pipeline.best_candidates()
    assert db.q1("SELECT 1 FROM tokens WHERE uid=?", (uid,))  # в общей базе остаётся
    assert c.get("/api/hidden").json()["rows"][0]["hidden_reason"] == "не интересен"
    c.post(f"/api/unhide/{uid}", json={})
    assert uid in pipeline.best_candidates()
    acts = [r["action"] for r in db.q("SELECT action FROM user_actions WHERE token_uid=? ORDER BY id", (uid,))]
    assert acts == ["hide", "restore"]  # история действий сохраняется


def test_14_9_temporary_hide_expires():
    from fastapi.testclient import TestClient
    from app.api import app
    uid = tok(level="C")
    TestClient(app).post(f"/api/hide/{uid}", json={"days": 7})
    assert uid not in pipeline.best_candidates()
    db.ex("UPDATE hidden_tokens SET until='2000-01-01T00:00:00Z' WHERE token_uid=?", (uid,))
    assert uid in pipeline.best_candidates()


def test_14_11_watchlist_keeps_updating_after_age_fail():
    uid = tok()
    db.ex("UPDATE tokens SET status='FAIL', status_reason='Возраст 120 дн. > 90' WHERE uid=?", (uid,))
    assert refresh.tiers()[uid] == 5
    db.ex("INSERT INTO watchlist(token_uid,added_at,status) VALUES(?,?,?)", (uid, now_iso(), "position"))
    assert refresh.tiers()[uid] == 1
    assert uid in [u for _, u in refresh.due_tokens("quote")]


# ================================================================ 14.12 / 8.10 — нет ложной актуальности
@respx.mock
def test_14_12_failed_quotes_do_not_look_fresh():
    uid = tok(facts={"price": {"value": 1.0}})
    db.ex("UPDATE tokens SET last_market_at='2026-01-01T00:00:00Z' WHERE uid=?", (uid,))
    respx.get(url__startswith=DS).mock(return_value=httpx.Response(503))
    respx.get(url__startswith="https://coins.llama.fi").mock(return_value=httpx.Response(503))
    st = refresh.refresh_quotes([uid])
    assert st["failed"] == 1
    t = db.q1("SELECT last_market_at FROM tokens WHERE uid=?", (uid,))
    assert t["last_market_at"] == "2026-01-01T00:00:00Z"  # отметка свежести не сдвинулась
    assert db.q1("SELECT last_quote_error FROM refresh_state WHERE token_uid=?", (uid,))["last_quote_error"]
    r = pipeline.evaluate(uid)
    assert r["status"] in ("STALE DATA", "FAIL", "UNKNOWN", "DATA ERROR")


@respx.mock
def test_8_10_api_down_keeps_previous_value_marked():
    uid = tok(facts={"coingecko": {"id": "x", "verified_contract": True, "circulating_supply": 1e6, "at": "2026-11-01T00:00:00Z"}})
    db.ex("UPDATE tokens SET coingecko_id='x' WHERE uid=?", (uid,))
    respx.get(url__startswith="https://api.coingecko.com").mock(return_value=httpx.Response(503))
    refresh.refresh_coingecko(uid)
    cg = enrich.get_facts(uid)["coingecko"]
    assert cg["circulating_supply"] == 1e6 and cg.get("error") and cg["at"] == "2026-11-01T00:00:00Z"


# ================================================================ 14.14 — 429 и backoff
@respx.mock
def test_14_14_429_backoff_and_circuit_breaker():
    respx.get("https://api.test/r").mock(side_effect=[httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, json={})])
    http.get_json("geckoterminal", "https://api.test/r")
    lim = http.limiter("geckoterminal")
    assert lim.penalty_until - _real_time.monotonic() > 5  # следующая попытка — не раньше Retry-After
    route = respx.get("https://api.test/down").mock(return_value=httpx.Response(503))
    for _ in range(http.BREAKER_FAILS):
        with pytest.raises(http.SourceError):
            http.get_json("dexscreener", "https://api.test/down", retries=0)
    calls = route.call_count
    with pytest.raises(http.SourceError) as e:
        http.get_json("dexscreener", "https://api.test/down", retries=0)
    assert e.value.kind == "circuit_open" and route.call_count == calls  # запрос не отправлен — бюджет не сжигается
    assert http.source_available("dexscreener")[0] is False


# ================================================================ 14.15 / 14.16
def test_14_15_crossing_threshold_no_false_confirmed_pass():
    f = {"circulating_official": {"value": 1e8, "source_type": "official_docs"}, "price": {"value": 0.04, "confidence": "derived"},
         "coingecko": {"verified_contract": True, "circulating_supply": 2e8}}
    r = F.f_mcap(f, 5e6)  # $4 млн (официально) и $8 млн (CoinGecko)
    assert r["status"] == "UNKNOWN"


def test_14_16_missing_info_not_auto_fail_or_pass():
    assert F.f_utility({}, [], "token")["status"] == "UNKNOWN"
    assert F.f_ecosystem({"tvl_usd": 25e6}, config.load_settings()["filters"])["status"] == "UNKNOWN"


# ================================================================ MCAP Intelligence Engine (8.1–8.12)
P = {"value": 0.04, "confidence": "derived"}


def test_8_1_ds_without_mcap_uses_other_source_circulating():
    f = {"price": P, "ds": {"fdv": 9e6, "market_cap": None}, "coingecko": {"verified_contract": True, "circulating_supply": 50e6, "url": "u"}}
    a = M.assess(f)
    assert a["type"] == "ESTIMATED" and a["value"] == pytest.approx(2e6)
    assert any("50,000,000" in p["formula"] for p in a["points"])


def test_8_2_official_tokenomics_circulating_confirmed():
    f = {"price": P, "circulating_official": {"value": 50e6, "source_type": "official_docs"}}
    a = M.assess(f)
    assert a["type"] == "CONFIRMED" and a["value"] == pytest.approx(2e6)
    assert M.filter_decision(a, 5e6)[:2] == ("PASS", "confirmed")


def test_8_3_and_8_12_total_supply_not_circulating():
    f = {"price": P, "onchain": {"total_supply": 500e6}, "ds": {"fdv": 20e6, "market_cap": 20e6}}
    a = M.assess(f)
    assert a["value"] is None and a["circulating_supply"] is None and a["high"] == pytest.approx(20e6)
    st, conf, reason, _ = M.filter_decision(a, 5e6)
    assert st == "UNKNOWN" and "НЕ приравнивается" in reason
    small = M.assess({"price": P, "onchain": {"total_supply": 50e6}})  # сценарий E при FDV ниже лимита
    assert M.filter_decision(small, 5e6)[:2] == ("PASS", "preliminary") and small["value"] is None


@respx.mock
def test_8_4_venft_lock_subtracted_once_staking_not_subtracted():
    uid = tok()
    a = "0x" + "e" * 40
    db.ex("INSERT INTO address_labels(chain,address,label,kind,token_uid,circulation,circulation_evidence) VALUES(?,?,?,?,?,?,?)",
          ("robinhood", a, "veNFT escrow", "escrow", uid, "excluded", "документация: токены в veNFT заблокированы"))
    db.ex("INSERT INTO address_labels(chain,address,label,kind,token_uid,circulation) VALUES(?,?,?,?,?,?)",
          ("robinhood", "0x" + "5" * 40, "Staking", "staking", uid, None))

    def rpc(req):
        body = json.loads(req.content)
        data = body["params"][0]["data"]
        bal = 300 * 10 ** 18 if a[2:] in data else 0
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(bal)})
    respx.post(RH.rpc[0]).mock(side_effect=rpc)
    r = refresh.refresh_circulation(uid)
    assert r["excluded"] == 1 and r["uncertain"] == 1  # стейкинг без доказательства не вычитается
    f = enrich.get_facts(uid)
    f["onchain"]["total_supply"] = 1000
    f["price"] = {"value": 1.0, "confidence": "derived"}
    m = M.assess(f)
    up = [u for u in m["upper_bounds"] if u["source"] == "onchain_excluded"][0]
    assert up["supply"] == pytest.approx(700)  # 1000 − 300, вычтено ровно один раз


def test_8_5_same_ticker_other_contract_coingecko_rejected():
    from app.sources.coingecko import contract_matches
    info = {"platforms": {"robinhood": "0x451b42a15100c340ca12f7c66de06fac5ea2d751"}}
    assert contract_matches(info, {"robinhood"}, "0x451B42A15100C340CA12F7C66DE06FAC5EA2D751")
    assert not contract_matches(info, {"robinhood"}, "0xad9e5fb798d12663911388a8d25eba21d6141401")
    a = M.assess({"price": P, "coingecko": {"id": "longbow-2", "verified_contract": False, "circulating_supply": 1e9}})
    assert not a["points"]  # данные чужого контракта не используются


def test_8_6_4m_vs_8m_unknown():
    a = M.assess({"price": P, "circulating_official": {"value": 1e8, "source_type": "official_docs"},
                  "coingecko": {"verified_contract": True, "circulating_supply": 2e8}})
    assert M.filter_decision(a, 5e6)[0] == "UNKNOWN" and a["conflicts"]


def test_8_7_illiquid_anomalous_pool_ignored():
    from app import market
    pools = [{"side": "base", "price_usd": 0.04, "liquidity_usd": 400_000, "counter_trusted": True, "trades_h24": 500, "source": "ds"},
             {"side": "base", "price_usd": 0.041, "liquidity_usd": 150_000, "counter_trusted": True, "trades_h24": 120, "source": "ds"},
             {"side": "base", "price_usd": 3.0, "liquidity_usd": 6_000, "counter_trusted": True, "trades_h24": 1, "source": "ds"}]
    _, med = market.assess_pools(pools, 5000, 30)
    assert 0.039 < med < 0.042
    assert pools[2]["quality"] is False
    pc = market.price_check([p for p in pools if p["quality"]], med, {"defillama": 0.0402})
    assert pc["confidence"] == "high" and not pc.get("conflict")
    spike = market.price_check([pools[0]], 0.12, {}, {"value": 0.04, "at_ts": 1000, "now_ts": 1000 + 1800})
    assert spike["confidence"] == "low" and spike.get("spike")


@respx.mock
def test_8_8_price_change_recomputes_mcap_without_onchain_rescan():
    healthy_chain()
    uid = tok(facts={"circulating_official": {"value": 50e6, "source_type": "official_docs"}, "price": {"value": 0.04, "confidence": "derived"}})
    db.ex("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,amount_raw,amount) VALUES(?,?,?,?,?,?,?)", (uid, "mint", "0x1", 0, 1, "1", 0))
    pipeline.evaluate(uid)
    assert enrich.get_facts(uid)["mcap"]["value"] == pytest.approx(2e6)
    addr = uid.split(":")[1]
    pair = {"chainId": "robinhood", "dexId": "u", "pairAddress": "0x" + "9" * 40, "priceUsd": "0.08",
            "baseToken": {"address": addr, "symbol": "TST"}, "quoteToken": {"address": "0x5fc5360d0400a0fd4f2af552add042d716f1d168", "symbol": "USDG"},
            "liquidity": {"usd": 500000}, "txns": {"h24": {"buys": 50, "sells": 40}}, "volume": {"h24": 10000}}
    respx.get(url__startswith=DS).mock(return_value=httpx.Response(200, json=[pair]))
    respx.get(url__startswith="https://coins.llama.fi").mock(return_value=httpx.Response(200, json={"coins": {}}))
    rpc = respx.post(RH.rpc[0]).mock(return_value=httpx.Response(500))
    assert refresh.refresh_quotes([uid])["ok"] == 1
    m = enrich.get_facts(uid)["mcap"]
    assert m["value"] == pytest.approx(4e6)  # цена ×2 → MCAP ×2
    assert rpc.call_count == 0 and db.q1("SELECT COUNT(*) n FROM supply_events WHERE token_uid=?", (uid,))["n"] == 1


def test_8_9_new_verified_supply_upgrades_confidence():
    f = {"price": P, "coingecko": {"verified_contract": True, "circulating_supply": 50e6}}
    assert M.assess(f)["type"] == "ESTIMATED"
    f["circulating_official"] = {"value": 50e6, "source_type": "official_docs"}
    assert M.assess(f)["type"] == "CONFIRMED"


def test_8_11_confirmed_lower_bound_above_limit_fails():
    a = M.assess({"price": P, "circulating_official": {"value": 175e6, "source_type": "official_docs"}})  # $7 млн
    assert M.filter_decision(a, 5e6)[:2] == ("FAIL", "confirmed")


def test_scenario_b_estimated_range_below_limit_is_preliminary():
    a = M.assess({"price": P, "coingecko": {"verified_contract": True, "circulating_supply": 62.5e6},
                  "gt": {}, "ds": {"market_cap": 3.5e6, "fdv": 9e6}})  # 2.5–3.5 млн
    st, conf, _, _ = M.filter_decision(a, 5e6)
    assert a["type"] == "ESTIMATED" and st == "PASS" and conf == "preliminary"


def test_scenario_c_estimated_range_crossing_is_manual_review():
    a = M.assess({"price": P, "coingecko": {"verified_contract": True, "circulating_supply": 75e6},
                  "ds": {"market_cap": 8e6, "fdv": 9e6}})  # 3–8 млн
    assert M.filter_decision(a, 5e6)[0] == "UNKNOWN"
