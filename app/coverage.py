"""Покрытие сетей: что сканер реально видит в каждой сети (только сохранённые данные, без сетевых запросов).

Различаются три ситуации: токен не обнаружен (по сохранённым данным неизмеримо — показываются только пропуски покрытия
new_pools), обнаружен, но не прошёл фильтры (FAIL по каждому фильтру), прошёл фильтры, но не попал в лучшие кандидаты
(уровень B: DeFi-связь не подтверждена; уровни C–E вне первой страницы глобального рейтинга).
Процент покрытия не вычисляется: общее число токенов/страниц сети неизвестно.
"""
from __future__ import annotations

from . import config, db
from .chains import CHAINS


def chain_report(runner=None) -> dict:
    s = config.load_settings()
    enabled = set(config.enabled_chains(s))
    from .pipeline import best_candidates
    best = best_candidates(1_000_000)
    top_n = int(s.get("top_candidates_n", 30))
    top = set(best[:top_n])
    rank_by_chain: dict[str, int] = {}
    for u in best:
        rank_by_chain[u.split(":")[0]] = rank_by_chain.get(u.split(":")[0], 0) + 1
    tok = {r["chain"]: dict(r) for r in db.q(
        "SELECT chain, COUNT(*) n, SUM(asset_class='token') tokens, SUM(last_market_at IS NOT NULL) quoted, SUM(last_deep_at IS NOT NULL) deep, "
        "SUM(last_onchain_at IS NOT NULL) onchain, SUM(protocol_slug IS NOT NULL) defi_linked, "
        "SUM(level='B') lvl_b, SUM(level IN ('C','D','E')) lvl_cde, SUM(level='E') lvl_e, SUM(status='PASS') pass, "
        "SUM(status IN ('UNKNOWN','MANUAL REVIEW')) unknown, SUM(status='FAIL') fail, MAX(first_seen_at) last_new_token "
        "FROM tokens GROUP BY chain")}
    fails = {}
    for r in db.q("SELECT t.chain, f.filter_key, COUNT(*) n FROM filter_current f JOIN tokens t ON t.uid=f.token_uid "
                  "WHERE f.status='FAIL' GROUP BY t.chain, f.filter_key"):
        fails.setdefault(r["chain"], {})[r["filter_key"]] = r["n"]
    unk = {}
    for r in db.q("SELECT t.chain, f.filter_key, COUNT(*) n FROM filter_current f JOIN tokens t ON t.uid=f.token_uid "
                  "WHERE f.status='UNKNOWN' GROUP BY t.chain, f.filter_key"):
        unk.setdefault(r["chain"], {})[r["filter_key"]] = r["n"]
    cov = {}
    for r in db.q("SELECT chain, MAX(run_at) last_run, COUNT(*) runs, SUM(continuous=0) gaps, SUM(pools) pools, "
                  "MAX(CASE WHEN continuous=0 THEN gap_start END) last_gap_start, MAX(CASE WHEN continuous=0 THEN gap_end END) last_gap_end "
                  "FROM coverage WHERE source='geckoterminal:new_pools' GROUP BY chain"):
        cov[r["chain"]] = dict(r)
    pending: dict[str, int] = {}
    if runner is not None:
        for l in runner.lanes.values():
            with l.lock:
                for j in l.keys.values():
                    kw = j.get("kw") or {}
                    ck = kw.get("chain") or (kw.get("uid") or "").split(":")[0] or None
                    for u in kw.get("uids") or []:
                        pending[u.split(":")[0]] = pending.get(u.split(":")[0], 0) + 1
                    if ck:
                        pending[ck] = pending.get(ck, 0) + 1
    src = {r["source"]: dict(r) for r in db.q("SELECT * FROM source_status")}
    rows = []
    for ck, ch in CHAINS.items():
        t = tok.get(ck, {})
        c = cov.get(ck, {})
        on = ck in enabled
        cde = t.get("lvl_cde") or 0
        in_top = sum(1 for u in top if u.startswith(ck + ":"))
        rpc = src.get(f"rpc:{ck}") or {}
        rows.append({
            "chain": ck, "name": ch.name, "support": ch.support, "enabled": on,
            "onchain_logs": bool(getattr(ch, "logs_ok", True)) and ch.kind == "evm",
            "onchain_note": getattr(ch, "logs_note", None),
            "tokens": t.get("tokens") or 0, "quoted": t.get("quoted") or 0, "deep_analyzed": t.get("deep") or 0,
            "onchain_scanned": t.get("onchain") or 0, "defi_linked": t.get("defi_linked") or 0,
            "level_b": t.get("lvl_b") or 0, "level_cde": cde, "level_e": t.get("lvl_e") or 0, "pass": t.get("pass") or 0,
            "unknown": t.get("unknown") or 0, "fail": t.get("fail") or 0, "fail_by_filter": fails.get(ck, {}), "unknown_by_filter": unk.get(ck, {}),
            "in_ranking": rank_by_chain.get(ck, 0), "in_global_top": in_top, "ranked_outside_top": rank_by_chain.get(ck, 0) - in_top,
            "last_discovery": c.get("last_run"), "discovery_runs": c.get("runs") or 0, "pools_seen": c.get("pools") or 0,
            "gaps": c.get("gaps") or 0,
            "last_gap": [c.get("last_gap_start"), c.get("last_gap_end")] if c.get("gaps") and (c.get("last_gap_start") or c.get("last_gap_end")) else None,
            "last_new_token": t.get("last_new_token"), "pending_jobs": pending.get(ck, 0),
            "rpc_errors": rpc.get("errors_total"), "rpc_last_error": rpc.get("last_error"),
            "coverage_pct": None,
            "coverage_note": ("сеть выключена" if not on else
                              "нет ни одного успешного прохода обнаружения" if not c.get("runs") else
                              f"пропусков покрытия new_pools: {c.get('gaps') or 0}; общее число токенов сети неизвестно — процент не вычисляется"),
        })
    sources = [{"source": k, "calls": v.get("calls_total"), "errors": v.get("errors_total"), "rate_limited": v.get("rate_limited_total"),
                "last_success_at": v.get("last_success_at"), "last_error_at": v.get("last_error_at"), "last_error": v.get("last_error"),
                "limit_per_min": (s.get("rate_limits_per_min") or {}).get(k.split(":")[0])} for k, v in src.items()]
    return {"chains": rows, "sources": sources, "enabled": sorted(enabled), "top_n": top_n,
            "limits": s.get("rate_limits_per_min"), "discovery_interval_min": s["intervals_min"]["discovery"],
            "max_pages_per_chain": s["discovery"]["max_pages_per_chain"],
            "notes": ["Уровень D требует ончейн-проверки эмиссии: в сетях без бесплатного eth_getLogs (см. «ончейн») токены не достигают D/E — "
                      "это техническое ограничение, а не оценка качества проектов сети.",
                      "Уровень C требует подтверждённой связи с протоколом DefiLlama по адресу контракта.",
                      "Выбор токенов для анализа чередуется по сетям (round-robin) внутри каждого уровня приоритета."]}
