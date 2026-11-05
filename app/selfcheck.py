"""Проверка на компьютере пользователя (Windows): python -m app.selfcheck  (windows_check.bat).

1) Версии Python/SQLite, режим транзакций драйвера по умолчанию и фактический режим соединений программы.
2) Текущая база (если есть): схема, число строк, PRAGMA integrity_check (только чтение); найденные прежние установки.
3) Конкурентный стресс-тест на ВРЕМЕННОЙ синтетической базе (ваша база не затрагивается): 60 с, реальные потоки.
4) Набор автотестов (если установлен pytest: pip install -r requirements-dev.txt).
Итог печатается и сохраняется в data/selfcheck-<время>.json — этот файл можно передать на проверку.
"""
from __future__ import annotations

import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, config, db, dbmigrate
from .util import now_iso, now_utc


def main() -> int:
    out: dict = {"app_version": __version__, "at": now_iso(), "platform": platform.platform(), "python": sys.version,
                 "sqlite": sqlite3.sqlite_version}
    print(f"DeFi Alpha Scanner {__version__} — проверка окружения\n")
    print(f"ОС: {out['platform']}\nPython: {sys.version.split()[0]}\nSQLite: {sqlite3.sqlite_version}")
    plain = sqlite3.connect(":memory:")
    out["driver_default_autocommit"] = getattr(plain, "autocommit", "нет атрибута (Python < 3.12)")
    print(f"Режим транзакций драйвера по умолчанию: {out['driver_default_autocommit']} "
          f"(-1 = legacy, False = PEP 249 — транзакция открыта всегда)")
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    old = db.DB_PATH
    db.set_db_path(tmp)
    c = db.connect(tmp)
    c.execute("CREATE TABLE t(x)")
    c.execute("SELECT * FROM t").fetchall()
    out["app_connection_in_transaction_after_read"] = c.in_transaction
    out["journal_mode"] = c.execute("PRAGMA journal_mode").fetchone()[0]
    c.close()
    db.set_db_path(old)
    print(f"Соединение программы держит транзакцию после чтения: {out['app_connection_in_transaction_after_read']} (должно быть False)")
    print(f"Режим журнала: {out['journal_mode']} (должно быть wal)\n")
    # ---- база
    p = db.DB_PATH
    if p.exists():
        inf = dbmigrate.db_info(p)
        out["database"] = inf
        try:
            out["database"]["integrity"] = dbmigrate.integrity(p)
        except sqlite3.Error as e:
            out["database"]["integrity"] = f"ошибка: {e}"
        print(f"База: {p}\n  схема v{inf['schema_version']}, строк: {inf['counts']}\n  integrity_check: {out['database']['integrity']}")
    else:
        print(f"База в {p} отсутствует.")
    prev = dbmigrate.find_previous_installations(p)
    out["previous_installations"] = prev
    for x in prev[:5]:
        print(f"  Найдена другая установка: {x['path']} (токенов {x['counts'].get('tokens', 0)}, "
              f"событий эмиссии {x['counts'].get('supply_events', 0)})")
    # ---- стресс-тест
    print("\nКонкурентный стресс-тест (60 с, временная синтетическая база)…")
    script = config.ROOT / "scripts" / "stress_sqlite.py"
    rep_path = Path(tempfile.mkdtemp()) / "stress.json"
    r = subprocess.run([sys.executable, str(script), "--seconds", "60", "--tokens", "300", "--events", "20000",
                        "--fault-rate", "0.003", "--label", "selfcheck", "--report", str(rep_path)],
                       capture_output=True, text=True, timeout=600)
    if rep_path.exists():
        st = json.loads(rep_path.read_text(encoding="utf-8"))
        out["stress"] = st
        m = st["metrics"]
        ok = (st["unhandled_count"] == 0 and all(st["lanes_alive"].values()) and st["scheduler_alive"]
              and st["integrity_check"] == "ok" and st["events_lost"] == 0 and st["events_duplicated"] == 0)
        out["stress_ok"] = ok
        print(f"  длительность {st['duration_s']} с; SQLITE_BUSY: {m.get('sqlite_busy_errors', 0)}; "
              f"инъецировано ошибок записи: {m.get('injected_write_faults', 0)}")
        print(f"  необработанных исключений в потоках: {st['unhandled_count']}; очереди живы: {st['lanes_alive']}; "
              f"планировщик жив: {st['scheduler_alive']}")
        print(f"  integrity_check: {st['integrity_check']}; потеряно событий: {st['events_lost']}; дублей: {st['events_duplicated']}")
        print(f"  задержки пользовательских операций, мс (p95): "
              + ", ".join(f"{k} {v['p95']}" for k, v in st["latency_ms"].items()))
        print("  РЕЗУЛЬТАТ: " + ("ПРОЙДЕН" if ok else "ЕСТЬ ПРОБЛЕМЫ — передайте файл отчёта на проверку"))
    else:
        out["stress_error"] = (r.stdout[-2000:] + r.stderr[-2000:])
        print("  стресс-тест не выполнился:\n" + out["stress_error"])
    # ---- автотесты
    try:
        import pytest  # noqa: F401
        print("\nАвтотесты…")
        t = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(config.ROOT / "tests")],
                           capture_output=True, text=True, timeout=900, cwd=str(config.ROOT))
        tail = (t.stdout.strip().splitlines() or ["нет вывода"])[-1]
        out["pytest"] = {"returncode": t.returncode, "summary": tail, "failures": [l for l in t.stdout.splitlines() if l.startswith("FAILED")][:30]}
        print("  " + tail)
    except ImportError:
        out["pytest"] = "не установлен (pip install -r requirements-dev.txt)"
        print("\nАвтотесты: pytest не установлен — пропущено")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = config.DATA_DIR / f"selfcheck-{now_utc().strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\nОтчёт сохранён: {f}")
    return 0 if out.get("stress_ok") and (not isinstance(out.get("pytest"), dict) or out["pytest"]["returncode"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
