import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).resolve().parent / "fixtures" / "real_2026-11-04"


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Каждый тест — с чистой БД и настройками во временной папке."""
    from app import config, db, http
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "SECRETS_PATH", tmp_path / "secrets.bin")
    config._cache = None
    db.set_db_path(tmp_path / "test.db")
    db.init_db()
    http.reset_limiters()
    # без пауз лимитера в тестах
    s = config.load_settings()
    s["rate_limits_per_min"] = {k: 100000 for k in s["rate_limits_per_min"]}
    config.save_settings(s)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    from app import refresh
    refresh._focus.clear()
    yield
    refresh._focus.clear()
    http.reset_limiters()


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))
