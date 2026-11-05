"""Экспорт ончейн-истории выпуска/сжигания токена из локальной базы (доказательная база анализа).

    python scripts/export_supply_history.py robinhood:0xb63d07cef74be747231d601bb2faa6b252c68106 evidence/fables

Пишет:
  supply_events.csv  — каждое событие Transfer from/to 0x0: блок, время (+ источник времени), транзакция, logIndex,
                       тип, контрагент, метка адреса и её источник, сумма в минимальных единицах и в токенах (точная десятичная);
  summary.json       — диапазон сканирования, сверка с totalSupply(), границы недель (блоки), сравнение с бюджетом,
                       крупнейшие получатели, версия токеномики, SHA-256 CSV;
  README.md          — описание и способ проверки.
Секреты (data/secrets.bin), настройки и API-ключи НЕ читаются и не экспортируются.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from decimal import Decimal, getcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.chains import CHAINS  # noqa: E402
from app.onchain import raw_totals, reconcile, supply_analytics  # noqa: E402
from app.util import loads, now_iso  # noqa: E402

getcontext().prec = 80


def main(uid: str, out_dir: str) -> None:
    db.init_db()
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    if not tok:
        sys.exit(f"токен {uid} не найден в базе {db.DB_PATH}")
    chain = CHAINS[tok["chain"]]
    dec = tok["decimals"] if tok["decimals"] is not None else 18
    q = Decimal(10) ** dec
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    labels = {r["address"]: dict(r) for r in db.q("SELECT * FROM address_labels WHERE chain=?", (tok["chain"],))}
    rows = db.q("SELECT * FROM supply_events WHERE token_uid=? ORDER BY block, log_index", (uid,))
    csv_path = out / "supply_events.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["block", "time_utc", "time_source", "tx_hash", "log_index", "kind", "counterparty",
                    "counterparty_label", "label_kind", "label_source", "amount_raw", "amount_tokens", "explorer_tx"])
        for r in rows:
            lb = labels.get(r["counterparty"]) or {}
            w.writerow([r["block"], r["ts"] or "", r["ts_source"] or "", r["tx_hash"], r["log_index"], r["kind"], r["counterparty"],
                        lb.get("label") or "", lb.get("kind") or "", lb.get("source_url") or "", r["amount_raw"],
                        str(Decimal(int(r["amount_raw"])) / q), chain.explorer_tx(r["tx_hash"]) or ""])
    sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    facts = loads(tok["facts"], {}) or {}
    oc = facts.get("onchain") or {}
    tot = raw_totals(uid)
    recon_saved = oc.get("reconciliation") or {}
    recheck = None
    if recon_saved.get("total_supply_raw"):
        rc = reconcile(int(recon_saved["total_supply_raw"]), tot["minted_raw"] - tot["burned_raw"])
        recheck = {"total_supply_raw": recon_saved["total_supply_raw"], "reconstructed_raw_from_csv_rows": str(tot["minted_raw"] - tot["burned_raw"]),
                   "exact_match": rc["exact"], "diff_raw": str(rc["diff_raw"]), "total_supply_read_at_block": recon_saved.get("at_block"),
                   "method": recon_saved.get("method")}
    an = supply_analytics(uid)
    summary = {
        "token": {"uid": uid, "chain": chain.name, "chain_id": chain.chain_id, "address": tok["address_display"] or tok["address"],
                  "symbol": tok["symbol"], "decimals": dec},
        "exported_at_utc": now_iso(), "source": "локальная база DeFi Alpha Scanner; данные получены через JSON-RPC "
                                               f"{chain.rpc[0] if chain.rpc else ''} (eth_getLogs, eth_call, eth_getBlockByNumber)",
        "scan": {"from_block": oc.get("scanned_from_block"), "to_block_exclusive": oc.get("scanned_to_block"),
                 "events": tot["events"], "minted_raw": str(tot["minted_raw"]), "burned_raw": str(tot["burned_raw"]),
                 "minted_tokens": str(Decimal(tot["minted_raw"]) / q), "burned_tokens": str(Decimal(tot["burned_raw"]) / q)},
        "reconciliation": recheck,
        "first_mint": oc.get("first_mint"),
        "time_sources": {r["ts_source"] or "none": r["n"] for r in db.q(
            "SELECT ts_source, COUNT(*) n FROM supply_events WHERE token_uid=? GROUP BY ts_source", (uid,))},
        "week_boundaries": loads(db.get_progress(f"week_blocks:{uid}"), {}),
        "budget_comparison": [{k: (str(v) if isinstance(v, int) and not isinstance(v, bool) and k.endswith("_raw") else v)
                               for k, v in w_.items()} for w_ in an["budget_comparison"]],
        "top_recipients": an["top_recipients"],
        "tokenomics_version": an["tokenomics_version"],
        "csv": {"file": "supply_events.csv", "rows": len(rows), "sha256": sha},
        "limitations": [
            "time_source=interpolated: время события интерполировано между опорными блоками; используется только для графиков по дням",
            "границы недельных бюджетов определены точно по timestamp блоков (первый блок с ts ≥ границы, проверен соседний блок)",
            "события с нестандартным форматом Transfer (data ≠ 32 байта) не интерпретируются",
            "метки адресов — только из документации проекта и наблюдения minter(); неизвестный адрес ≠ адрес команды",
        ],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    r = recheck or {}
    (out / "README.md").write_text(
        f"# История выпуска {tok['symbol']} ({chain.name})\n\n"
        f"Контракт: `{summary['token']['address']}` · decimals {dec} · экспорт {summary['exported_at_utc']}\n\n"
        f"- Событий: **{tot['events']}** (mint/burn), блоки {oc.get('scanned_from_block')} → {oc.get('scanned_to_block')}\n"
        f"- Σ mint − Σ burn = `{tot['minted_raw'] - tot['burned_raw']}` мин. единиц\n"
        f"- totalSupply() = `{r.get('total_supply_raw')}` (на блоке {r.get('total_supply_read_at_block')})\n"
        f"- Совпадение: **{'точное' if r.get('exact_match') else 'НЕТ'}** (разница `{r.get('diff_raw')}`)\n"
        f"- SHA-256 supply_events.csv: `{sha}`\n\n"
        "Проверка без программы: сложите столбец `amount_raw` для kind=mint и вычтите kind=burn (целыми числами, не float) — "
        "результат должен совпасть с totalSupply из summary.json. Любую транзакцию можно открыть по ссылке explorer_tx.\n"
        "Скрипт проверки: `python scripts/verify_supply_export.py <папка>`.\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "sha256": sha, "reconciliation": recheck}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
