"""Регрессионные тесты по замечаниям независимого аудита v0.1.0 (2026-11-05).
Данные в тестах — ИСКУССТВЕННЫЕ (синтетические), сконструированы для воспроизведения конкретных ошибок."""
import datetime as dt

import pytest

from app import db, discovery, filters as F, onchain
from app.chains import CHAINS
from app.util import dumps, now_iso

NOW = dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc)
ECO = {"chain_min_tvl_usd": 20e6, "chain_max_tvl_decline_90d_pct": -60, "chain_min_dex_volume_30d_usd": 10e6,
       "chain_max_stable_decline_90d_pct": -50}


def cand(kind, value, conf):
    return {"kind": kind, "value": value, "confidence": conf, "label": kind, "source": kind}


# ---------------------------------------------------------------- 1. f_mcap: конфликт через порог
def test_audit1_confirmed_4m_secondary_8m_is_not_pass():
    f = {"mcap_candidates": [cand("circulating_official", 4e6, "confirmed"), cand("coingecko_via_gt", 8e6, "reported")],
         "mcap_conflict": {"spread_pct": 100}}
    r = F.f_mcap(f, 5e6)
    assert r["status"] == "UNKNOWN"
    assert "оба ниже лимита" not in r["reason"]
    assert "по разные стороны" in r["reason"]
    assert r["needs"]


def test_audit1_all_sources_below_limit_pass_and_wording():
    f = {"mcap_candidates": [cand("circulating_official", 2.9e6, "confirmed"), cand("coingecko_via_gt", 2.2e6, "reported")],
         "mcap_conflict": {"spread_pct": 33}}
    r = F.f_mcap(f, 5e6)
    assert r["status"] == "PASS" and r["confidence"] == "confirmed"
    assert "все источники ниже лимита" in r["reason"]


def test_audit1_all_sources_above_limit_fail():
    f = {"mcap_candidates": [cand("circulating_official", 6e6, "confirmed"), cand("ds_reported", 9e6, "reported")]}
    assert F.f_mcap(f, 5e6)["status"] == "FAIL"


def test_audit1_not_evidence_sources_ignored():
    # MCAP=FDV у DEX Screener не является доказательством и не создаёт «конфликт через порог»
    f = {"mcap_candidates": [cand("circulating_official", 4e6, "confirmed"), cand("ds_equals_fdv", 8e6, "not_evidence")]}
    assert F.f_mcap(f, 5e6)["status"] == "PASS"


# ---------------------------------------------------------------- 2. f_utility: нет сайта ≠ FAIL
def test_audit2_no_site_no_socials_no_llama_is_unknown():
    r = F.f_utility({}, [], "token")
    assert r["status"] == "UNKNOWN"
    assert r["needs"]


def test_audit2_tokenized_stock_still_fails():
    assert F.f_utility({}, [], "tokenized_stock")["status"] == "FAIL"


def test_audit2_manual_fail_still_fails():
    r = F.f_utility({}, [{"fact_key": "utility", "value": "FAIL", "note": "проверено: продукта нет"}], "token")
    assert r["status"] == "FAIL"


# ---------------------------------------------------------------- 3. f_ecosystem: нет PASS без метрик активности
def test_audit3_tvl_only_is_not_pass():
    r = F.f_ecosystem({"tvl_usd": 25e6}, ECO)
    assert r["status"] == "UNKNOWN"


def test_audit3_tvl_plus_one_metric_not_confirmed():
    r = F.f_ecosystem({"tvl_usd": 25e6, "dex_volume_30d": 50e6}, ECO)
    assert not (r["status"] == "PASS" and r["confidence"] == "confirmed")


def test_audit3_full_metrics_confirmed_pass():
    h = {"tvl_usd": 1e9, "tvl_change_30d": 4, "tvl_change_90d": 150, "stables_usd": 1e9, "stables_change_30d": -4,
         "stables_change_90d": 75, "dex_volume_30d": 2.3e10, "fees_30d": 7.5e7}
    r = F.f_ecosystem(h, ECO)
    assert r["status"] == "PASS" and r["confidence"] == "confirmed"


# ---------------------------------------------------------------- 4. onchain: целые единицы и подтверждённые timestamps
def _tok():
    uid, _ = discovery.upsert_token(CHAINS["robinhood"], {"address": "0x" + "1" * 40, "symbol": "TST", "decimals": 18},
                                    source="test", reason="synthetic")
    db.ex("UPDATE tokens SET decimals=18 WHERE uid=?", (uid,))
    return uid


def _mint(uid, i, block, raw, ts=None, ts_source=None, to="0x" + "a" * 40):
    db.ex("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,ts,ts_source,counterparty,amount_raw,amount) "
          "VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, "mint", f"0x{i:064x}", i, block, ts, ts_source, to, str(raw), raw / 1e18))


def test_audit4_integer_sums_exact():
    uid = _tok()
    # 0.1 токена 10 раз: в float сумма = 0.9999999999999999, в целых единицах — ровно 10**18
    for i in range(10):
        _mint(uid, i, 100 + i, 10 ** 17)
    tot = onchain.raw_totals(uid)
    assert tot["minted_raw"] == 10 ** 18 and tot["burned_raw"] == 0
    acc = 0.0
    for _ in range(10):
        acc += 0.1
    assert acc != 1.0  # демонстрация проблемы float: 0.9999999999999999
    assert onchain.reconcile(10 ** 18, tot["minted_raw"] - tot["burned_raw"])["exact"] is True
    assert onchain.reconcile(10 ** 18 + 1, tot["minted_raw"])["exact"] is False


def test_audit4_large_values_no_float_loss():
    uid = _tok()
    big = 123_456_789_123_456_789_123_456_789  # > 2**53
    _mint(uid, 0, 1, big)
    _mint(uid, 1, 2, 1)
    assert onchain.raw_totals(uid)["minted_raw"] == big + 1


def test_audit4_week_budget_uses_confirmed_block_boundaries():
    uid = _tok()
    db.ex("INSERT INTO address_labels(chain,address,label,kind) VALUES('robinhood',?,?,?)", ("0x" + "a" * 40, "Gauge", "gauge"))
    db.ex("INSERT INTO tokenomics_versions(token_uid,version_label,is_current,params,entered_at) VALUES(?,?,?,?,?)",
          (uid, "v", 1, dumps({"weekly_budgets": [{"week": 1, "start": "2026-10-01T00:00:00Z", "amount": 100}],
                               "emission_recipient_kinds": ["gauge"]}), now_iso()))
    # интерполированное время ВНУТРИ недели, но блок — ПОСЛЕ подтверждённой границы конца недели
    _mint(uid, 0, 1_000, 60 * 10 ** 18, ts="2026-10-03T00:00:00Z", ts_source="interpolated")
    _mint(uid, 1, 2_500, 70 * 10 ** 18, ts="2026-10-07T23:59:00Z", ts_source="interpolated")
    db.set_progress(f"week_blocks:{uid}", dumps({"2026-10-01T00:00:00Z": {"block": 900, "ts": "2026-10-01T00:00:00Z"},
                                                 "2026-10-08T00:00:00Z": {"block": 2_000, "ts": "2026-10-08T00:00:01Z"}}))
    w = onchain.supply_analytics(uid)["budget_comparison"][0]
    assert w["boundary_source"] == "block_timestamps"
    assert w["minted_to_emission_contracts_raw"] == 60 * 10 ** 18  # событие в блоке 2500 — уже неделя 2
    assert w["over_budget"] is False


def test_audit4_week_without_confirmed_boundaries_unverified():
    uid = _tok()
    db.ex("INSERT INTO address_labels(chain,address,label,kind) VALUES('robinhood',?,?,?)", ("0x" + "a" * 40, "Gauge", "gauge"))
    db.ex("INSERT INTO tokenomics_versions(token_uid,version_label,is_current,params,entered_at) VALUES(?,?,?,?,?)",
          (uid, "v", 1, dumps({"weekly_budgets": [{"week": 1, "start": "2026-10-01T00:00:00Z", "amount": 100}]}), now_iso()))
    _mint(uid, 0, 1_000, 500 * 10 ** 18, ts="2026-10-03T00:00:00Z", ts_source="interpolated")
    w = onchain.supply_analytics(uid)["budget_comparison"][0]
    assert w["over_budget"] is None and w["boundary_source"] is None


def test_audit4_exact_block_at_boundary():
    class Fake:
        def block_timestamp(self, b):
            return 1_000_000 + b // 10  # 10 блоков на секунду
    b = onchain.exact_block_at(Fake(), 1_000_500, head=100_000, head_ts=1_010_000)
    f = Fake()
    assert f.block_timestamp(b) >= 1_000_500 > f.block_timestamp(b - 1)


# ---------------------------------------------------------------- 5. экспорт истории минта: проверяемость, без секретов
def test_audit5_export_and_independent_verify(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("export")  # отдельно от папки данных теста (там лежит secrets.bin)
    import importlib.util
    import sys as _sys
    from app import config
    config.set_secret("etherscan_key", "SECRET-XYZ")
    uid = _tok()
    _mint(uid, 0, 10, 7 * 10 ** 17, ts="2026-10-01T00:00:00Z", ts_source="block")
    _mint(uid, 1, 11, 3 * 10 ** 17, ts="2026-10-01T00:00:01Z", ts_source="interpolated")
    db.ex("UPDATE tokens SET facts=? WHERE uid=?", (dumps({"onchain": {"reconciliation": {"total_supply_raw": str(10 ** 18), "at_block": 11}}}), uid))
    root = config.ROOT / "scripts"

    def load(name):
        spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    load("export_supply_history").main(uid, str(tmp_path))
    ver = load("verify_supply_export")
    assert ver.main(str(tmp_path)) == 0
    for f in tmp_path.iterdir():
        assert "SECRET-XYZ" not in f.read_text(encoding="utf-8")
    # подмена суммы в CSV обнаруживается
    p = tmp_path / "supply_events.csv"
    p.write_text(p.read_text(encoding="utf-8").replace(str(7 * 10 ** 17), str(7 * 10 ** 17 + 1)), encoding="utf-8")
    assert ver.main(str(tmp_path)) == 1
