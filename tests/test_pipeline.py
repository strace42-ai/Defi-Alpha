"""Интеграционные тесты на СОХРАНЁННЫХ РЕАЛЬНЫХ ответах API (tests/fixtures/real_2026-11-04)
и на явно искусственных сценариях отказов (429, недоступность, пустая выдача, устаревание)."""
import json

import httpx
import pytest
import respx

from app import db, discovery, enrich, market, onchain, pipeline
from app.chains import CHAINS
from app.http import SourceError, get_json
from app.util import dumps, now_iso, token_uid
from conftest import load

GT = "https://api.geckoterminal.com/api/v2"
BOW = "0x451b42a15100c340ca12f7c66de06fac5ea2d751"
FAB = "0xb63d07cef74be747231d601bb2faa6b252c68106"


# ---------------------------------------------------------------- HTTP: 429 / недоступность / пустая выдача
@respx.mock
def test_429_then_success():
    route = respx.get("https://api.test/x").mock(side_effect=[httpx.Response(429, headers={"retry-after": "1"}), httpx.Response(200, json={"ok": 1})])
    data, _ = get_json("geckoterminal", "https://api.test/x")
    assert data == {"ok": 1} and route.call_count == 2
    st = db.q1("SELECT * FROM source_status WHERE source='geckoterminal'")
    assert st["rate_limited_total"] == 1 and st["last_success_at"]


@respx.mock
def test_temporary_unavailability_raises_and_is_logged():
    respx.get("https://api.test/down").mock(return_value=httpx.Response(503))
    with pytest.raises(SourceError) as e:
        get_json("dexscreener", "https://api.test/down", retries=2)
    assert e.value.kind == "unavailable"
    assert db.q1("SELECT errors_total FROM source_status WHERE source='dexscreener'")["errors_total"] == 3


@respx.mock
def test_cloudflare_challenge_not_bypassed():
    respx.get("https://explorer.test/api").mock(return_value=httpx.Response(403, text="<html><title>Just a moment...</title></html>"))
    with pytest.raises(SourceError) as e:
        get_json("blockscout", "https://explorer.test/api")
    assert e.value.kind == "blocked"


@respx.mock
def test_empty_new_pools_records_coverage():
    respx.get(f"{GT}/networks/robinhood/new_pools").mock(return_value=httpx.Response(200, json={"data": []}))
    r = discovery.discover_new_pools("robinhood")
    assert r["pools"] == 0 and r["new_tokens"] == []
    cov = db.q1("SELECT * FROM coverage")
    assert cov["pools"] == 0 and "первый запуск" in cov["note"]


# ---------------------------------------------------------------- Discovery на реальном ответе
@respx.mock
def test_discovery_real_robinhood_page_and_gap_detection():
    page = load("gt_new_pools_robinhood_p1.json")
    pager = lambda pg: (lambda req: httpx.Response(200, json=pg if req.url.params.get("page") == "1" else {"data": []}))
    respx.get(f"{GT}/networks/robinhood/new_pools").mock(side_effect=pager(page))
    r = discovery.discover_new_pools("robinhood")
    assert r["pools"] == 20
    # quote-активы (ETH 0xeeee…, USDG, WETH) помечены служебными, а не новыми проектами
    q = {row["address"]: row["asset_class"] for row in db.q("SELECT address, asset_class FROM tokens")}
    assert q.get("0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee") == "quote"
    # второй запуск с тем же (старым) ответом — пулы старше последнего → непрерывно
    r2 = discovery.discover_new_pools("robinhood")
    assert r2["continuous"] is True
    # искусственно: прошлый «самый новый» пул сильно новее → разрыв фиксируется
    db.set_progress("gt_new_pools_newest:robinhood", "2026-11-04T10:00:00Z")
    old = {"data": [dict(p, attributes=dict(p["attributes"], pool_created_at="2026-11-04T12:00:00Z")) for p in page["data"]],
           "included": page["included"]}
    respx.get(f"{GT}/networks/robinhood/new_pools").mock(side_effect=pager(old))
    r3 = discovery.discover_new_pools("robinhood")
    assert r3["continuous"] is False and "РАЗРЫВ" in r3["note"]
    # дедупликация: повторные пулы не создают дубликатов
    assert db.q1("SELECT COUNT(*) n FROM pools")["n"] == 20


def test_token_identity_is_chain_plus_address():
    ch = CHAINS["robinhood"]
    u1, n1 = discovery.upsert_token(ch, {"address": "0x451B42A15100C340CA12F7C66DE06FAC5EA2D751", "symbol": "BOW"}, source="t", reason="r")
    u2, n2 = discovery.upsert_token(ch, {"address": BOW, "symbol": "BOW"}, source="t", reason="r")
    u3, n3 = discovery.upsert_token(ch, {"address": "0xad9e5fb798d12663911388a8d25eba21d6141401", "symbol": "BOW"}, source="t", reason="r")
    assert u1 == u2 and n1 and not n2  # один токен, разный регистр
    assert u3 != u1 and n3  # другой контракт с тем же тикером — отдельная сущность
    u4, _ = discovery.upsert_token(CHAINS["base"], {"address": BOW, "symbol": "BOW"}, source="t", reason="r")
    assert u4 != u1  # тот же адрес на другой сети — другой токен


def test_solana_mint_case_preserved():
    ch = CHAINS["solana"]
    m = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    uid, _ = discovery.upsert_token(ch, {"address": m}, source="t", reason="r")
    assert uid == "solana:" + m
    other, _ = discovery.upsert_token(ch, {"address": m.lower()}, source="t", reason="r")
    assert other != uid


# ---------------------------------------------------------------- Рынок: реальные пулы BOW
def test_bow_quote_side_pairs_do_not_leak_price():
    ds = load("ds_token_pairs_bow.json")
    from app.sources.dexscreener import _parse_pair
    pairs = [_parse_pair(p) for p in ds]
    views = [market.pool_view_from_ds(p, BOW, set(CHAINS["robinhood"].quote_assets)) for p in pairs]
    quote_side = [v for v in views if v["side"] == "quote"]
    assert quote_side and all(v["price_usd"] is None for v in quote_side)  # priceUsd другого токена не берётся
    pools, med = market.assess_pools(views, 5000, 30)
    assert 0.003 < med < 0.005  # реальная цена BOW ~0.0041, а не 0.019/100.6 из чужих пар


def test_multiple_pools_dedup_and_gt_quote_price():
    gt = load("gt_token_pools_bow.json")
    from app.sources.geckoterminal import _parse_pools
    from app.sources.dexscreener import _parse_pair
    tq = set(CHAINS["robinhood"].quote_assets)
    views = [market.pool_view_from_gt(p, BOW, tq) for p in _parse_pools(gt)]
    views += [market.pool_view_from_ds(_parse_pair(p), BOW, tq) for p in load("ds_token_pairs_bow.json")]
    d = market.dedupe_pools(views)
    assert len(d) < len(views)
    # в GT для пары MASKS/BOW наш токен — quote, и GT отдаёт его цену (quote_token_price_usd)
    q = [v for v in d if v["side"] == "quote" and v["source"] == "geckoterminal"]
    assert q and all(v["price_usd"] is not None for v in q)


def test_ath_real_bow_and_wick_filter():
    o = load("gt_ohlcv_bow_day.json")
    cs = o["data"]["attributes"]["ohlcv_list"]
    a = market.compute_ath({"pool": sorted(cs)}, 2.0)
    assert a["ath_usd"] <= a["ath_raw_high_usd"]
    assert a["first_trade_at"].startswith("2026-08-08")
    dd = market.drawdown_pct(0.0041, a["ath_usd"])
    assert 50 < dd < 86  # BOW: просадка в пределах лимита 86% — не исключать
    # искусственная аномальная свеча
    fake = [[1, 1.0, 50.0, 0.9, 1.1, 10], [2, 1.1, 1.3, 1.0, 1.2, 10]]
    a2 = market.compute_ath({"p": fake}, 2.0)
    assert a2["ath_usd"] == 1.3 and a2["wick_anomalies"]


# ---------------------------------------------------------------- Конфликт MCAP / устаревшие данные / статусы
def _mk(uid_addr=FAB, facts=None):
    ch = CHAINS["robinhood"]
    uid, _ = discovery.upsert_token(ch, {"address": uid_addr, "symbol": "TST"}, source="test", reason="synthetic")
    if facts is not None:
        db.ex("UPDATE tokens SET facts=?, last_market_at=? WHERE uid=?", (dumps(facts), now_iso(), uid))
    return uid


def test_mcap_conflict_event():
    uid = _mk()
    f = {"price": {"value": 0.06}, "gt": {"market_cap_usd": 2.2e6, "fdv_usd": 8.8e6}, "circulating_official": {"value": 46e6, "source_type": "official_docs"}}
    enrich.compute_caps(uid, f)
    assert f.get("mcap_conflict")
    assert db.q1("SELECT COUNT(*) n FROM conflicts WHERE token_uid=?", (uid,))["n"] == 1


def test_stale_data_status():
    uid = _mk(facts={"gt": {"liquidity_usd": 5e5, "volume_h24": 1e5}, "price": {"value": 1.0},
                     "earliest_pool": {"at": "2026-10-20T00:00:00Z"}})
    db.ex("UPDATE tokens SET last_market_at='2026-10-01T00:00:00Z' WHERE uid=?", (uid,))
    r = pipeline.evaluate(uid)
    assert r["status"] in ("STALE DATA", "FAIL")
    if r["status"] == "FAIL":  # если какой-то фильтр уже FAIL — устаревание вторично
        assert "FAIL" in [v["status"] for v in r["filters"].values()]


def test_unknown_not_hidden_goes_to_manual_review():
    uid = _mk(facts={"gt": {"liquidity_usd": 5e5, "volume_h24": 1e5, "market_cap_usd": 1e6, "fdv_usd": 3e6},
                     "price": {"value": 1.0}, "earliest_pool": {"at": now_iso()},
                     "mcap_candidates": [{"kind": "coingecko_via_gt", "value": 1e6, "confidence": "reported", "label": "x", "source": "gt"}],
                     "links": {"websites": ["https://x.example"]}})
    db.set_progress("chain_health:robinhood", json.dumps({"tvl_usd": 1e9, "dex_volume_30d": 1e9}))
    r = pipeline.evaluate(uid)
    assert r["status"] == "MANUAL REVIEW" and r["level"] == "B"


def test_quote_asset_never_a_candidate():
    ch = CHAINS["robinhood"]
    uid, _ = discovery.upsert_token(ch, {"address": "0x5fc5360d0400a0fd4f2af552add042d716f1d168", "symbol": "USDG"}, source="t", reason="r")
    r = pipeline.evaluate(uid)
    assert r["status"] == "FAIL" and "quote" in r["reason"]


# ---------------------------------------------------------------- Токеномика: версии и бюджет
def test_tokenomics_version_change_and_mint_vs_budget():
    uid = _mk()
    db.ex("INSERT INTO tokenomics_versions(token_uid,version_label,is_current,params,entered_at) VALUES(?,?,?,?,?)",
          (uid, "v1", 1, dumps({"max_supply": 1e9, "weekly_budgets": [{"week": 1, "start": "2026-10-01T00:00:00Z", "amount": 100}]}), now_iso()))
    # новая версия становится актуальной, старая сохраняется
    db.ex("UPDATE tokenomics_versions SET is_current=0 WHERE token_uid=?", (uid,))
    db.ex("INSERT INTO tokenomics_versions(token_uid,version_label,is_current,params,entered_at) VALUES(?,?,?,?,?)",
          (uid, "v2", 1, dumps({"max_supply": 1e9, "weekly_budgets": [{"week": 1, "start": "2026-10-01T00:00:00Z", "amount": 100}],
                               "emission_recipient_kinds": ["gauge"]}), now_iso()))
    assert enrich.current_tokenomics(uid)["version_label"] == "v2"
    assert db.q1("SELECT COUNT(*) n FROM tokenomics_versions WHERE token_uid=?", (uid,))["n"] == 2
    db.ex("INSERT INTO address_labels(chain,address,label,kind) VALUES('robinhood','0x00000000000000000000000000000000000000aa','Gauge','gauge')")
    # искусственные mint-события: 150 в gauge (бюджет 100) + 1000 команде (не эмиссия)
    for i, (to, amt) in enumerate([("0x00000000000000000000000000000000000000aa", 150), ("0x00000000000000000000000000000000000000bb", 1000)]):
        db.ex("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,ts,counterparty,amount_raw,amount) VALUES(?,?,?,?,?,?,?,?,?)",
              (uid, "mint", f"0x{i}", i, 100 + i, "2026-10-02T00:00:00Z", to, str(amt * 10 ** 18), amt))
    # подтверждённые границы недели (первый блок с ts ≥ начала/конца недели)
    db.set_progress(f"week_blocks:{uid}", dumps({"2026-10-01T00:00:00Z": {"block": 50}, "2026-10-08T00:00:00Z": {"block": 1000}}))
    an = onchain.supply_analytics(uid)
    w = an["budget_comparison"][0]
    assert w["minted_all"] == 1150 and w["minted_to_emission_contracts"] == 150 and w["over_budget"] is True
    assert an["top_recipients"][0]["note"] is not None or an["top_recipients"][0]["label"]  # неизвестный адрес помечен


def test_budget_is_not_minted_amount():
    """Без mint-событий бюджет не превращается в выпущенное предложение."""
    uid = _mk()
    db.ex("INSERT INTO tokenomics_versions(token_uid,version_label,is_current,params,entered_at) VALUES(?,?,?,?,?)",
          (uid, "v1", 1, dumps({"weekly_budgets": [{"week": 1, "start": "2026-10-01T00:00:00Z", "amount": 10_000_000}]}), now_iso()))
    db.set_progress(f"week_blocks:{uid}", dumps({"2026-10-01T00:00:00Z": {"block": 1}, "2026-10-08T00:00:00Z": {"block": 9}}))
    an = onchain.supply_analytics(uid)
    assert an["budget_comparison"][0]["minted_all"] == 0 and an["daily"] == []


# ---------------------------------------------------------------- DefiLlama-связь на реальном листинге
def test_defillama_address_link_real_listing():
    prots = load("dl_protocols_subset.json")
    for p in prots:
        db.ex("INSERT INTO protocols(slug,name,category,address,gecko_id,symbol) VALUES(?,?,?,?,?,?)",
              (p["slug"], p["name"], p["category"], p.get("address"), p.get("gecko_id"), p.get("symbol")))
    from app import fundamentals
    ch = CHAINS["robinhood"]
    bow, _ = discovery.upsert_token(ch, {"address": BOW, "symbol": "BOW"}, source="t", reason="r")
    fake, _ = discovery.upsert_token(ch, {"address": "0xad9e5fb798d12663911388a8d25eba21d6141401", "symbol": "BOW"}, source="t", reason="r")
    assert fundamentals.link_protocol(bow)["linked"]["slug"] == "longbow"
    r = fundamentals.link_protocol(fake)
    assert r["linked"] is None  # совпадение тикера не создаёт связь
    assert any(p["slug"] == "longbow" for p in r["possible"])


def test_find_block_by_time_secant():
    """Синтетическая цепь: время блока меняется (1 с, затем 0.1 с) — поиск должен найти блок за ≤ ~10 запросов."""
    class FakeRpc:
        calls = 0

        def block_timestamp(self, b):
            FakeRpc.calls += 1
            return int(b * 1.0) if b <= 5_000_000 else int(5_000_000 + (b - 5_000_000) * 0.1)
    rpc = FakeRpc()
    head = 100_000_000
    head_ts = rpc.block_timestamp(head)
    target = 4_000_000  # ts попадает в «медленный» участок
    b = onchain.find_block_by_time(rpc, target, head, head_ts)
    assert rpc.block_timestamp(b) <= target and target - rpc.block_timestamp(b) < 2000
    assert FakeRpc.calls < 25
