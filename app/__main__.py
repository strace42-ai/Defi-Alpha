"""Запуск: python -m app  (start.bat делает это автоматически)."""
from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import logging
import logging.handlers
import threading
import webbrowser

from . import __version__, config, db
from .util import now_iso


def setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "logs").mkdir(exist_ok=True)
    h = logging.handlers.RotatingFileHandler(config.DATA_DIR / "logs" / "scanner.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)
    root.addHandler(sh)


def _free_port(start: int) -> int:
    """Если порт занят (например, программа уже запущена) — взять следующий свободный."""
    import socket
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


class InstanceLock:
    """Один экземпляр фонового сборщика на папку данных: файл-блокировка ОС (снимается автоматически при завершении)."""

    def __init__(self, path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True

    def info(self) -> dict:
        try:
            return json.loads((config.DATA_DIR / "instance.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def main() -> None:
    ap = argparse.ArgumentParser(description="DeFi Alpha Scanner")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--scan", action="store_true", help="поставить обнаружение по всем сетям в очередь при старте")
    ap.add_argument("--rollback-migration", action="store_true", help="восстановить базу из копии перед последней миграцией")
    ap.add_argument("--import-db", default=None, help="перенести прежнюю базу (путь к data\\scanner.db); оригинал не изменяется")
    ap.add_argument("--rollback-import", action="store_true", help="отменить последний перенос базы")
    ap.add_argument("--new-db", action="store_true", help="начать с новой пустой базы, даже если найдена прежняя установка")
    args = ap.parse_args()
    setup_logging()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(config.DATA_DIR / "scanner.lock")
    if not lock.acquire():
        inf = lock.info()
        url = inf.get("url") or "http://127.0.0.1:8765/"
        print(f"\n  DeFi Alpha Scanner уже запущен ({url}). Второй экземпляр сборщика не запускается — открываю интерфейс.\n")
        if not args.no_browser:
            webbrowser.open(url)
        return
    if args.rollback_migration:
        p = db.rollback_migration()
        print(f"Восстановлено из {p}" if p else "Копий перед миграцией не найдено")
        return
    if args.rollback_import:
        from . import dbmigrate
        p = dbmigrate.rollback_import(db.DB_PATH)
        print(f"Перенос отменён, восстановлено состояние до переноса ({p})" if p else "Переносов не найдено")
        return
    from . import startup
    from .jobs import runner
    st = startup.prepare(import_db=args.import_db, new_db=args.new_db, scan=args.scan)
    s = config.load_settings()
    if st["mode"] == "setup_required":
        print("\n  ВНИМАНИЕ: в этой папке нет базы, но найдена прежняя установка:")
        for c in st["candidates"][:5]:
            print(f"    {c['path']}  (токенов: {c['counts'].get('tokens', 0)}, событий эмиссии: {c['counts'].get('supply_events', 0)})")
        print("  Новая пустая база НЕ создана. Выберите в открывшемся окне: перенести прежнюю базу или начать с новой.")
    elif st["mode"] == "new_empty":
        print("\n  Создана НОВАЯ пустая база: прежняя установка не найдена. Если она есть — укажите путь к data\\scanner.db"
              " в интерфейсе (Обзор) или запустите: python -m app --import-db ПУТЬ")
    port = _free_port(args.port or int(s.get("ui_port", 8765)))
    url = f"http://127.0.0.1:{port}/"
    (config.DATA_DIR / "instance.json").write_text(json.dumps({"url": url, "pid": os.getpid(), "started_at": now_iso()}), encoding="utf-8")

    def shutdown(*_):
        runner.stop()
        if not startup.SETUP.get("active"):
            db.checkpoint()  # сохранение базы (WAL → основной файл) перед завершением
    atexit.register(shutdown)
    mon = ("не запущен — сначала выберите базу в окне программы" if startup.SETUP.get("active") else
           "включён" if s.get("scheduler_enabled") else "на паузе")
    print(f"\n  DeFi Alpha Scanner {__version__}: {url}\n  Фоновый мониторинг: {mon}"
          f"\n  Данные: {config.DATA_DIR}\n  Мониторинг работает, пока открыто это окно и компьютер не спит.\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    import uvicorn
    from .api import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    shutdown()


if __name__ == "__main__":
    main()
