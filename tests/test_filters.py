"""Модульные тесты фильтров. ВНИМАНИЕ: здесь используются ИСКУССТВЕННЫЕ факты (синтетические данные),
явно сконструированные для проверки логики. В пользовательские результаты они не попадают."""
import datetime as dt

from app import filters as F

NOW = dt.datetime(2026, 11, 4, 12, tzinfo=dt.timezone.utc)
S = {"chain_min_tvl_usd": 20e6, "chain_max_tvl_decline_90d_pct": -60, "chain_min_dex_volume_30d_usd": 10e6,
     "chain_max_stable_decline_90d_pct": -50}


def facts(**kw):
    base = {
        "price": {"value": 0.2},
        "earliest_pool": {"at": "2026-10-01T00:00:00Z", "sources": ["geckoterminal"]},
        "ath": {"ath_usd": 1.0, "first_trade_at": "2026-10-01T00:00:00Z", "first_candle_at": "2026-10-01T00:00:00Z"},
        "mcap_candidates": [{"kind": "coingecko_via_gt", "value": 2e6, "confidence": "reported", "label": "x", "source": "gt"}],
        "fdv_total": {"value": 8e6},
    }
    base.update(kw)
    return base


def test_suitable_token_passes_age_mcap_drawdown():
    f = facts()
    assert F.f_age(f, [], NOW, 90)["status"] == "PASS"
    assert F.f_mcap(f, 5e6)["status"] == "PASS"
    d = F.f_drawdown(f, 86)
    assert d["status"] == "PASS" and round(d["values"]["drawdown_pct"]) == 80  # пример из ТЗ: ATH $1 → $0.20


def test_age_preliminary_without_tge_and_confirmed_with_tge():
    r = F.f_age(facts(), [], NOW, 90)
    assert r["confidence"] == "preliminary"
    r2 = F.f_age(facts(), [{"fact_key": "tge_date", "value": "2026-10-01T00:00:00Z"}], NOW, 90)
    assert r2["status"] == "PASS" and r2["confidence"] == "confirmed"


def test_token_older_than_90_days_fails():
    f = facts(earliest_pool={"at": "2026-06-01T00:00:00Z"}, ath={"ath_usd": 1, "first_trade_at": "2026-06-01T00:00:00Z"})
    r = F.f_age(f, [], NOW, 90)
    assert r["status"] == "FAIL" and "> 90" in r["reason"]


def test_new_pool_of_old_token_uses_earliest_pool():
    # обнаружен через новый пул (вчера), но у токена есть пул годичной давности
    f = facts(discovery_pool_created_at="2026-11-03T00:00:00Z", earliest_pool={"at": "2025-11-01T00:00:00Z"},
              ath={"ath_usd": 1, "first_trade_at": "2025-11-01T00:00:00Z"})
    r = F.f_age(f, [], NOW, 90)
    assert r["status"] == "FAIL"
    assert any("НОВЫЙ ПУЛ" in (e.get("note") or "") for e in r["evidence"])


def test_old_first_mint_makes_age_unknown():
    f = facts(onchain={"first_mint": {"ts": "2025-01-01T00:00:00Z"}})
    assert F.f_age(f, [], NOW, 90)["status"] == "UNKNOWN"


def test_mcap_above_limit_fails():
    f = facts(mcap_candidates=[{"kind": "coingecko_via_gt", "value": 7e6, "confidence": "reported", "label": "x", "source": "gt"}])
    assert F.f_mcap(f, 5e6)["status"] == "FAIL"


def test_mcap_not_equated_to_fdv():
    # только DS marketCap == FDV (не доказательство) и FDV выше лимита → UNKNOWN, не FAIL
    f = facts(mcap_candidates=[{"kind": "ds_equals_fdv", "value": 8e6, "confidence": "not_evidence", "label": "x", "source": "ds"}],
              fdv_total={"value": 8e6})
    r = F.f_mcap(f, 5e6)
    assert r["status"] == "UNKNOWN" and "НЕ приравнивается" in r["reason"]


def test_mcap_upper_bound_by_fdv_is_preliminary_pass():
    f = facts(mcap_candidates=[], fdv_total={"value": 1e6})
    r = F.f_mcap(f, 5e6)
    assert r["status"] == "PASS" and r["confidence"] == "preliminary"


def test_mcap_sources_straddle_limit_unknown():
    f = facts(mcap_candidates=[
        {"kind": "circulating_official", "value": 4e6, "confidence": "confirmed", "label": "a", "source": "p"},
        {"kind": "coingecko_via_gt", "value": 6e6, "confidence": "reported", "label": "b", "source": "gt"}])
    # подтверждённый источник имеет приоритет над reported
    assert F.f_mcap(f, 5e6)["status"] == "PASS"
    f2 = facts(mcap_candidates=[
        {"kind": "coingecko_via_gt", "value": 4e6, "confidence": "reported", "label": "a", "source": "gt"},
        {"kind": "ds_reported", "value": 6e6, "confidence": "reported", "label": "b", "source": "ds"}])
    assert F.f_mcap(f2, 5e6)["status"] == "UNKNOWN"


def test_drawdown_70_82_passes_with_86_limit():
    for price in (0.30, 0.18):  # просадка 70% и 82%
        assert F.f_drawdown(facts(price={"value": price}), 86)["status"] == "PASS"
    assert F.f_drawdown(facts(price={"value": 0.10}), 86)["status"] == "FAIL"


def test_insufficient_ath_history_is_preliminary():
    f = facts(earliest_pool={"at": "2026-09-01T00:00:00Z"},
              ath={"ath_usd": 1.0, "first_candle_at": "2026-10-01T00:00:00Z", "first_trade_at": "2026-10-01T00:00:00Z"})
    r = F.f_drawdown(f, 86)
    assert r["status"] == "PASS" and r["confidence"] == "preliminary" and "истинный ATH может быть выше" in r["reason"]
    # FAIL надёжен даже при неполной истории (наблюдаемая просадка — нижняя граница)
    f["price"] = {"value": 0.05}
    assert F.f_drawdown(f, 86)["status"] == "FAIL"


def test_vc_known_fails_unknown_stays_unknown():
    assert F.f_vc({}, [{"fact_key": "funding", "category": "vc_fund", "value": "Seed $3M"}], ["vc_fund"], ["angel"])["status"] == "FAIL"
    r = F.f_vc({"protocol": {"raises": []}}, [], ["vc_fund"], ["angel"])
    assert r["status"] == "UNKNOWN" and "НЕ доказывает" in r["reason"]


def test_vc_angel_and_grant_not_equal_vc():
    m = [{"fact_key": "funding", "category": "angel", "value": "angel"}, {"fact_key": "funding", "category": "grant", "value": "grant"}]
    assert F.f_vc({}, m, ["vc_fund"], ["angel"])["status"] == "UNKNOWN"


def test_vc_pass_requires_documented_evidence():
    m = [{"fact_key": "funding", "category": "none_documented", "value": "no VC", "evidence_url": "https://docs.example/allocations"}]
    assert F.f_vc({}, m, ["vc_fund"], [])["status"] == "PASS"
    m2 = [{"fact_key": "funding", "category": "none_documented", "value": "no VC", "evidence_url": None}]
    assert F.f_vc({}, m2, ["vc_fund"], [])["status"] == "UNKNOWN"


def test_vc_defillama_raises_fail():
    f = {"protocol": {"raises": [{"round": "Seed", "amount": 3, "leadInvestors": ["Fund X"]}]}}
    assert F.f_vc(f, [], ["vc_fund"], [])["status"] == "FAIL"


def test_utility():
    linked = {"protocol_link": {"linked": {"slug": "x", "name": "X", "category": "Dexs", "method": "defillama_address"}},
              "protocol": {"tvl_usd": 1e6, "fees": {"available": True, "total30d": 1e4}, "holders_revenue": {"available": False}}}
    r = F.f_utility(linked, [], "token")
    assert r["status"] == "PASS" and r["values"]["value_capture_risks"]
    empty = F.f_utility({}, [], "token")
    assert empty["status"] == "FAIL"
    site_only = F.f_utility({"links": {"websites": ["https://x.io"], "socials": []}}, [], "token")
    assert site_only["status"] == "UNKNOWN"  # сайт сам по себе не доказывает продукт
    assert F.f_utility({}, [], "tokenized_stock")["status"] == "FAIL"


def test_ecosystem():
    good = {"tvl_usd": 1e9, "tvl_change_90d": 50, "stables_change_90d": 20, "dex_volume_30d": 1e9}
    assert F.f_ecosystem(good, S)["status"] == "PASS"
    dead = {"tvl_usd": 2e6, "tvl_change_90d": -80, "stables_change_90d": -70, "dex_volume_30d": 1e5}
    assert F.f_ecosystem(dead, S)["status"] == "FAIL"
    small_growing = {"tvl_usd": 8e6, "tvl_change_30d": 40, "tvl_change_90d": 120, "dex_volume_30d": 5e7}
    assert F.f_ecosystem(small_growing, S)["status"] == "PASS"  # маленькие растущие сети не исключаются
    assert F.f_ecosystem(None, S)["status"] == "UNKNOWN"


def test_determinism():
    f = facts()
    a = F.evaluate_all(f, [], {"tvl_usd": 1e9, "dex_volume_30d": 1e9}, {"filters": {**S, "max_age_days": 90, "max_circ_mcap_usd": 5e6,
                       "max_drawdown_pct": 86, "vc_fail_categories": ["vc_fund"], "vc_warn_categories": []}}, NOW, "token")
    b = F.evaluate_all(f, [], {"tvl_usd": 1e9, "dex_volume_30d": 1e9}, {"filters": {**S, "max_age_days": 90, "max_circ_mcap_usd": 5e6,
                       "max_drawdown_pct": 86, "vc_fail_categories": ["vc_fund"], "vc_warn_categories": []}}, NOW, "token")
    assert a == b


def test_new_token_of_mature_protocol_flagged():
    f = facts(protocol={"first_tvl_at": "2023-08-28T00:00:00Z"})
    r = F.f_age(f, [], NOW, 90)
    assert r["status"] == "PASS" and "зрелого проекта" in r["reason"] and r["values"]["protocol_age_days"] > 365


def test_mature_family_listing_used():
    f = facts(protocol={"first_tvl_at": "2026-10-22T00:00:00Z", "family_first_listed_at": "2023-08-28T00:00:00Z"})
    assert "зрелого проекта" in F.f_age(f, [], NOW, 90)["reason"]
