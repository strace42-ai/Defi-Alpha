"""Независимая проверка экспорта истории выпуска (без базы и без сети):

    python scripts/verify_supply_export.py evidence/fables

Проверяет SHA-256 CSV, пересчитывает Σmint − Σburn целыми числами и сравнивает с totalSupply из summary.json.
"""
import csv
import hashlib
import json
import sys
from pathlib import Path


def main(d: str) -> int:
    p = Path(d)
    summary = json.loads((p / "summary.json").read_text(encoding="utf-8"))
    data = (p / "supply_events.csv").read_bytes()
    sha_ok = hashlib.sha256(data).hexdigest() == summary["csv"]["sha256"]
    minted = burned = n = 0
    seen = set()
    with (p / "supply_events.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["tx_hash"], row["log_index"])
            if key in seen:
                print("ДУБЛИКАТ события:", key)
                return 1
            seen.add(key)
            n += 1
            v = int(row["amount_raw"])
            if row["kind"] == "mint":
                minted += v
            else:
                burned += v
    total = int(summary["reconciliation"]["total_supply_raw"])
    ok = sha_ok and n == summary["csv"]["rows"] and minted - burned == total
    print(f"строк: {n}; SHA-256 {'совпадает' if sha_ok else 'НЕ совпадает'}")
    print(f"Σmint − Σburn = {minted - burned}")
    print(f"totalSupply   = {total} (блок {summary['reconciliation'].get('total_supply_read_at_block')})")
    print("РЕЗУЛЬТАТ:", "точное совпадение" if ok else "НЕ СОВПАДАЕТ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
