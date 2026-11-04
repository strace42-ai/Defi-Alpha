"""Резервное копирование SQLite (онлайн-копия через sqlite3 backup API) с ротацией."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config, db
from .util import now_utc

BACKUP_DIR = config.DATA_DIR / "backups"


def make_backup(label: str = "auto") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"scanner-{now_utc().strftime('%Y%m%d-%H%M%S')}-{label}.db"
    src = db.connect(db.DB_PATH)
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    out.close()
    src.close()
    keep = int(config.load_settings().get("backup_keep", 14))
    files = sorted(BACKUP_DIR.glob("scanner-*.db"))
    for f in files[:-keep]:
        try:
            f.unlink()
        except OSError:
            pass
    return dst


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    return [{"name": f.name, "size": f.stat().st_size} for f in sorted(BACKUP_DIR.glob("scanner-*.db"), reverse=True)]
