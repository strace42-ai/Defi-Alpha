"""Запуск: python -m app  (start.bat делает это автоматически)."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import threading
import webbrowser

from . import config, db


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


def main() -> None:
    ap = argparse.ArgumentParser(description="DeFi Alpha Scanner")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--scan", action="store_true", help="поставить сканирование в очередь при старте")
    args = ap.parse_args()
    setup_logging()
    db.init_db()
    # прерванные задания прошлого запуска помечаются, данные и позиции сканирования сохраняются
    db.ex("UPDATE jobs SET status='interrupted', finished_at=started_at WHERE status IN ('running','queued')")
    from . import research
    from .jobs import runner
    seeded = research.load_seed()
    # добавленные вручную/исследовательские токены без анализа (в т.ч. после прерывания) — в очередь
    seeded = list(dict.fromkeys(seeded + [r["uid"] for r in db.q(
        "SELECT uid FROM tokens WHERE discovery_source='manual' AND last_deep_at IS NULL")]))
    for uid in seeded:
        runner.submit("token", priority=5, uid=uid)  # фоном, после пользовательских заданий
    if args.scan:
        runner.submit("scan")
    runner.start()  # после постановки в очередь: приоритеты применяются с первого задания
    port = _free_port(args.port or int(config.load_settings().get("ui_port", 8765)))
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  DeFi Alpha Scanner запущен: {url}\n  Данные: {config.DATA_DIR}\n  Для остановки закройте это окно (Ctrl+C).\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    import uvicorn
    from .api import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
