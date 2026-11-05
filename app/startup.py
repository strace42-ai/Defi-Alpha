"""Порядок запуска: выбор базы (без незаметного создания новой), миграция, затем фоновый мониторинг."""
from __future__ import annotations

import threading

from . import config, db, dbmigrate
from .util import now_iso

SETUP: dict = {"active": False, "mode": None, "candidates": [], "db": None}
_lock = threading.Lock()
_started = {"done": False, "scan": False}


def prepare(import_db: str | None = None, new_db: bool = False, scan: bool = False) -> dict:
    """Вызывается до запуска веб-сервера. Возвращает состояние; при setup_required база НЕ создаётся."""
    _started["scan"] = scan
    p = db.DB_PATH
    pend = dbmigrate.pending_import_path()
    if import_db is None and pend.exists():  # перенос, запрошенный из интерфейса работающей программы
        import json
        try:
            import_db = json.loads(pend.read_text(encoding="utf-8")).get("path")
        finally:
            pend.unlink()
    if import_db:
        rep = dbmigrate.import_db(import_db, p)
        if not rep["ok"]:
            raise SystemExit(f"Перенос базы не выполнен: {rep.get('error')}. Исходная база не изменена.")
        print(f"  Перенесена прежняя база: {import_db}\n  Строк: {rep['counts_dest']}")
    st = dbmigrate.startup_state(p)
    if st["mode"] == "setup_required" and not new_db:
        SETUP.update(active=True, **st)
        return st
    if st["mode"] == "existing":
        # другая установка с БОЛЬШИМ объёмом данных (например, 2.0.0 запущена в новой папке и создала новую базу)
        cur = dbmigrate.db_info(p)["counts"]
        richer = [c for c in dbmigrate.find_previous_installations(p)
                  if c["counts"].get("supply_events", 0) > cur.get("supply_events", 0)
                  or c["counts"].get("tokens", 0) > cur.get("tokens", 0)]
        SETUP["richer_elsewhere"] = richer[:5]
        SETUP["current_counts"] = cur
        st["richer_elsewhere"] = richer[:5]
    complete(new_empty=(st["mode"] == "new_empty"))
    return st


def complete(new_empty: bool = False) -> dict:
    """Инициализация/миграция базы и запуск мониторинга (однократно)."""
    with _lock:
        if _started["done"]:
            return {"already": True}
        info = db.init_db()
        if new_empty:
            db.ex("INSERT OR IGNORE INTO meta(key,value) VALUES('db_origin',?)", (f"new_empty:{now_iso()}",))
        db.ex("UPDATE jobs SET status='interrupted', finished_at=started_at WHERE status IN ('running','queued')")
        from . import research
        from .jobs import runner
        config.load_settings()  # перенос настроек v1 → v2
        seeded = research.load_seed()
        seeded = list(dict.fromkeys(seeded + [r["uid"] for r in db.q(
            "SELECT uid FROM tokens WHERE discovery_source='manual' AND last_deep_at IS NULL")]))
        for uid in seeded:
            runner.submit("token", priority=5, uid=uid)
        if _started["scan"]:
            runner.submit("scan", priority=0)
        if info.get("from_version") == 1:
            runner.submit("reevaluate", priority=1)
        runner.start(scheduler=True)
        SETUP.update(active=False)
        _started["done"] = True
        return info
