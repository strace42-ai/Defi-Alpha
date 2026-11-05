"""Прогон MCAP Intelligence Engine по накопленной базе с отчётом «до/после».

    python scripts/mcap_engine_run.py [--cg-limit 60] [--quick-limit 300] [--report docs/MCAP_ENGINE_REPORT.md]

Этапы: (0) статистика до; (1) миграция схемы и пересчёт по уже собранным данным (без сети);
(2) добыча данных: быстрые рыночные данные, CoinGecko (circulating с проверкой контракта), котировки с проверкой цены,
ончейн-адреса блокировок — в пределах бюджета запросов; (3) статистика после, изменения статусов, примеры расчётов.
Никаких подставных чисел: значения берутся только из источников, указанных в каждом примере.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, enrich, pipeline, refresh  # noqa: E402
from app.http import SourceError  # noqa: E402
from app.util import fmt_usd, now_iso  # noqa: E402


def stats() -> dict:
    rows = db.q("SELECT t.uid, t.level, t.status, t.facts, fc.status fs, fc.confidence fcf FROM tokens t "
                "LEFT JOIN filter_current fc ON fc.token_uid=t.uid AND fc.filter_key='mcap' WHERE t.asset_class='token'")
    out = {"tokens": len(rows), "filter": Counter(), "type": Counter(), "status": Counter(), "per": {}}
    for r in rows:
        out["filter"][f"{r['fs']}/{r['fcf'] or '-'}"] += 1
        f = json.loads(r["facts"] or "{}")
        m = f.get("mcap") if isinstance(f.get("mcap"), dict) and "points" in f.get("mcap", {}) else None
        out["type"][(m or {}).get("type") or "нет оценки (v1)"] += 1
        out["status"][r["status"]] += 1
        out["per"][r["uid"]] = {"fs": r["fs"], "status": r["status"], "type": (m or {}).get("type"), "level": r["level"],
                                "conflicts": len((m or {}).get("conflicts") or [])}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cg-limit", type=int, default=60)
    ap.add_argument("--quick-limit", type=int, default=300)
    ap.add_argument("--quotes-limit", type=int, default=60)
    ap.add_argument("--report", default=str(ROOT / "docs" / "MCAP_ENGINE_REPORT.md"))
    a = ap.parse_args()
    before = stats()
    info = db.init_db()
    print("миграция:", info, flush=True)
    for r in db.q("SELECT uid FROM tokens"):
        pipeline.evaluate(r["uid"], old_facts=None)
    offline = stats()
    print("после пересчёта без сети:", dict(offline["filter"]), flush=True)
    # ---- добыча данных: сначала токены, близкие к прохождению остальных фильтров
    def need_rows():
        return db.q("SELECT t.uid, t.chain, t.level, t.coingecko_id FROM tokens t JOIN filter_current fc ON fc.token_uid=t.uid "
                    "AND fc.filter_key='mcap' WHERE t.asset_class='token' AND (fc.status='UNKNOWN' OR fc.confidence='preliminary') "
                    "ORDER BY CASE WHEN fc.status='UNKNOWN' THEN 0 ELSE 1 END, "
                    "CASE t.level WHEN 'E' THEN 0 WHEN 'D' THEN 1 WHEN 'C' THEN 2 WHEN 'B' THEN 3 ELSE 4 END")
    need = need_rows()
    by_chain: dict[str, list[str]] = {}
    for r in need[:a.quick_limit]:
        by_chain.setdefault(r["chain"], []).append(r["uid"])
    net = Counter()
    for ck, uids in by_chain.items():
        try:
            enrich.quick_enrich(ck, uids)
            net["quick_tokens"] += len(uids)
        except SourceError as e:
            net["quick_errors"] += 1
            print("quick error", e)
    for r in db.q("SELECT uid FROM tokens"):
        pipeline.evaluate(r["uid"], old_facts=None)
    cg_list = [r for r in need_rows() if r["coingecko_id"]][:a.cg_limit]
    for i, r in enumerate(cg_list):
        res = refresh.refresh_coingecko(r["uid"])
        net["cg_calls"] += 1
        net["cg_verified" if res.get("verified") else ("cg_error" if res.get("error") else "cg_contract_mismatch")] += 1
        if i % 10 == 0:
            print(f"CoinGecko {i + 1}/{len(cg_list)}", flush=True)
    q_list = [r["uid"] for r in need_rows() if r["level"] in ("B", "C", "D", "E")][:a.quotes_limit]
    for i in range(0, len(q_list), 10):
        st = refresh.refresh_quotes(q_list[i:i + 10])
        net["quotes_ok"] += st["ok"]
        net["quotes_failed"] += st["failed"]
    for r in db.q("SELECT DISTINCT token_uid FROM address_labels WHERE circulation='excluded' AND token_uid IS NOT NULL"):
        try:
            refresh.refresh_circulation(r["token_uid"])
            net["circulation"] += 1
        except SourceError:
            pass
    for r in db.q("SELECT uid FROM tokens"):
        pipeline.evaluate(r["uid"], old_facts=None)
    after = stats()
    write_report(a.report, before, offline, after, net)
    print("готово:", dict(after["filter"]), flush=True)


def example(uid: str) -> str:
    t = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    f = json.loads(t["facts"] or "{}")
    m = f.get("mcap") or {}
    fc = db.q1("SELECT * FROM filter_current WHERE token_uid=? AND filter_key='mcap'", (uid,))
    lines = [f"### {t['symbol']} — `{uid}`", "",
             f"- Тип: **{m.get('type')}**; значение {fmt_usd(m.get('value'))}; диапазон {fmt_usd(m.get('low'))} – {fmt_usd(m.get('high'))}",
             f"- Цена: ${m.get('price'):.8g} (достоверность {m.get('price_confidence')})" if m.get("price") else "- Цена: н/д",
             f"- Решение фильтра: **{fc['status'] if fc else '—'}** {('(' + fc['confidence'] + ')') if fc and fc['confidence'] else ''} — {fc['reason'] if fc else ''}"]
    for p in m.get("points") or []:
        lines.append(f"  - {p['source']} [{p['confidence']}]: `{p['formula']}` = {fmt_usd(p['mcap'])}" + (f" — {p['note']}" if p.get("note") else ""))
    for u in m.get("upper_bounds") or []:
        lines.append(f"  - верхняя граница ({u['source']}): `{u['formula']}` = {fmt_usd(u['mcap'])}")
    for c in m.get("conflicts") or []:
        lines.append(f"  - КОНФЛИКТ {c['kind']}: {round(c.get('spread_pct') or 0)}% {c.get('note') or ''}")
    return "\n".join(lines) + "\n"


def write_report(path: str, before: dict, offline: dict, after: dict, net: Counter) -> None:
    changed = Counter()
    status_changed = 0
    for uid, b in before["per"].items():
        a = after["per"].get(uid)
        if not a:
            continue
        if b["fs"] != a["fs"]:
            changed[f"{b['fs']} → {a['fs']}"] += 1
        if b["status"] != a["status"]:
            status_changed += 1
    conflicts = sum(1 for v in after["per"].values() if v["conflicts"])
    ex = ["robinhood:0xb63d07cef74be747231d601bb2faa6b252c68106", "robinhood:0x451b42a15100c340ca12f7c66de06fac5ea2d751"]
    for typ in ("CONFIRMED", "ESTIMATED"):
        for uid, v in after["per"].items():
            if v["type"] == typ and uid not in ex and v["level"] in ("B", "C", "D", "E"):
                ex.append(uid)
                break
    for uid, v in after["per"].items():
        if v["fs"] == "UNKNOWN" and uid not in ex and v["level"] in ("B", "C"):
            ex.append(uid)
            break
    fmtc = lambda c: "\n".join(f"| {k} | {v} |" for k, v in sorted(c.items(), key=lambda x: -x[1]))
    txt = f"""# MCAP Intelligence Engine — прогон по накопленной базе

Сформирован: {now_iso()}. Скрипт: `python scripts/mcap_engine_run.py`. База: накопленная база сканера (копия), {before['tokens']} токенов
(служебные активы исключены). Данные — только из указанных источников; недостающая доля обращения не подставлялась.

## Решение фильтра MCAP

| Статус/достоверность | До (v1) | После пересчёта без сети | После добычи данных |
|---|---|---|---|
""" + "\n".join(f"| {k} | {before['filter'].get(k, 0)} | {offline['filter'].get(k, 0)} | {after['filter'].get(k, 0)} |"
                for k in sorted(set(before['filter']) | set(offline['filter']) | set(after['filter']))) + f"""

## Тип оценки MCAP (после)

| Тип | Токенов |
|---|---|
{fmtc(after['type'])}

- Было MCAP UNKNOWN: **{before['filter'].get('UNKNOWN/-', 0)}**; осталось: **{after['filter'].get('UNKNOWN/-', 0)}**.
- Подтверждённая капитализация (CONFIRMED): **{after['type'].get('CONFIRMED', 0)}**; оценочная (ESTIMATED): **{after['type'].get('ESTIMATED', 0)}**; неизвестна: **{after['type'].get('UNKNOWN', 0)}**.
- Токенов с конфликтом источников: **{conflicts}**.
- Изменилось итоговых статусов отбора: **{status_changed}** (включая эффект отмены фильтра No-VC).

## Переходы решения фильтра MCAP

| Переход | Токенов |
|---|---|
{fmtc(changed)}

## Сетевые запросы прогона

{json.dumps(dict(net), ensure_ascii=False)}

## Примеры с воспроизводимыми расчётами

""" + "\n".join(example(u) for u in ex if db.q1("SELECT 1 FROM tokens WHERE uid=?", (u,))) + """
## Почему часть токенов осталась UNKNOWN
UNKNOWN остаётся только после прохождения всех уровней: нет circulating ни у одного источника (или CoinGecko-монета не
указывает этот контракт), нет доказанных блокировок для ончейн-границы, а стоимость выпущенного предложения выше $5 млн —
поэтому ни PASS, ни FAIL доказать нельзя. Такие токены попадают в очередь «MCAP требует уточнения», и фоновый мониторинг
продолжает попытки (CoinGecko — с ограничением ~5 запросов/мин).
"""
    Path(path).write_text(txt, encoding="utf-8")
    print(txt[:3000])


if __name__ == "__main__":
    main()
