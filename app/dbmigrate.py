"""Безопасное обновление: поиск прежней установки и перенос её базы.

Почему это нужно: ZIP релиза распаковывается в НОВУЮ папку (DefiAlpha-vX.Y.Z), и в ней нет папки data.
Раньше программа молча создавала там новую пустую базу. Теперь при первом запуске без базы:
  1) ищутся прежние установки (соседние папки, Загрузки/Рабочий стол/Документы);
  2) если найдены — программа НЕ создаёт базу и предлагает перенос (интерфейс #/setup или --import-db);
  3) перенос: проверка целостности исходной базы (только чтение — оригинал не изменяется), копия через SQLite backup API,
     проверка целостности копии, сверка числа строк по всем таблицам, резервная копия текущей базы, отчёт, возможность отката.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from . import config
from .util import now_iso, now_utc

COUNT_TABLES = ["tokens", "pools", "observations", "supply_events", "filter_results", "filter_current", "manual_facts",
                "tokenomics_versions", "address_labels", "watchlist", "alerts", "status_history", "hidden_tokens", "user_actions",
                "mcap_assessments", "protocols", "coverage", "scan_progress"]


def default_search_roots() -> list[Path]:
    roots = [config.ROOT.parent, config.ROOT.parent.parent]
    home = Path.home()
    for n in ("Downloads", "Desktop", "Documents", "Загрузки", "Рабочий стол", "Документы"):
        roots.append(home / n)
    out = []
    for r in roots:
        try:
            if r.exists() and r.is_dir() and r not in out:
                out.append(r)
        except OSError:
            pass
    return out


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5)


def db_info(path: Path) -> dict:
    """Сведения о базе без изменения файла (режим только чтения)."""
    p = Path(path)
    info = {"path": str(p), "size_mb": round(p.stat().st_size / 1e6, 1) if p.exists() else 0,
            "modified": None, "schema_version": None, "counts": {}, "fables": False, "error": None}
    try:
        info["modified"] = now_utc().fromtimestamp(p.stat().st_mtime, tz=now_utc().tzinfo).strftime("%Y-%m-%dT%H:%M:%SZ")
        c = _ro(p)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in COUNT_TABLES:
            if t in tables:
                info["counts"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if "meta" in tables:
            r = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            info["schema_version"] = int(r[0]) if r else 1
        if "tokens" in tables:
            info["fables"] = c.execute("SELECT COUNT(*) FROM tokens WHERE symbol='FABLES'").fetchone()[0] > 0
        c.close()
    except (sqlite3.Error, OSError, ValueError) as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def find_previous_installations(current_db: Path, search_roots: list[Path] | None = None) -> list[dict]:
    cur = Path(current_db).resolve()
    found: dict[str, dict] = {}
    for root in (search_roots if search_roots is not None else default_search_roots()):
        for pat in ("*/data/scanner.db", "*/*/data/scanner.db", "data/scanner.db"):
            try:
                for p in Path(root).glob(pat):
                    rp = p.resolve()
                    if rp == cur or str(rp) in found:
                        continue
                    inf = db_info(rp)
                    if inf["error"] is None and inf["counts"].get("tokens", 0) > 0:
                        found[str(rp)] = inf
            except OSError:
                continue
    return sorted(found.values(), key=lambda x: (-(x["counts"].get("supply_events", 0)), x["path"]))


def startup_state(db_path: Path, search_roots: list[Path] | None = None) -> dict:
    """existing — база на месте; setup_required — базы нет, но найдена прежняя установка (база НЕ создаётся);
    new_empty — базы нет и прежних установок не найдено (будет создана новая, с явным сообщением пользователю)."""
    p = Path(db_path)
    if p.exists() and p.stat().st_size > 0:
        return {"mode": "existing", "db": str(p), "candidates": []}
    cands = find_previous_installations(p, search_roots)
    return {"mode": "setup_required" if cands else "new_empty", "db": str(p), "candidates": cands}


def integrity(path: Path, ro: bool = True) -> str:
    c = _ro(path) if ro else sqlite3.connect(str(path))
    try:
        return c.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        c.close()


def import_db(src: Path, dst: Path) -> dict:
    """Перенос прежней базы. Оригинал открывается только на чтение и не изменяется."""
    src, dst = Path(src).resolve(), Path(dst)
    rep: dict = {"ok": False, "source": str(src), "dest": str(dst), "started_at": now_iso()}
    if not src.exists():
        rep["error"] = "исходная база не найдена"
        return rep
    st0 = src.stat()
    rep["integrity_source"] = integrity(src)
    if rep["integrity_source"] != "ok":
        rep["error"] = f"исходная база повреждена: {rep['integrity_source']} — перенос остановлен, оригинал не тронут"
        return rep
    rep["counts_source"] = db_info(src)["counts"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    bdir = dst.parent / "backups"
    bdir.mkdir(exist_ok=True)
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    if dst.exists() and dst.stat().st_size > 0:
        keep = bdir / f"scanner-{stamp}-before-import.db"
        s_ = sqlite3.connect(str(dst))
        o_ = sqlite3.connect(str(keep))
        with o_:
            s_.backup(o_)
        o_.close()
        s_.close()
        rep["backup_of_replaced_db"] = str(keep)
    else:
        (bdir / f"scanner-{stamp}-before-import.none").write_text("до переноса базы не было", encoding="utf-8")
    tmp = dst.with_name(dst.name + ".importing")
    if tmp.exists():
        tmp.unlink()
    s_ = _ro(src)
    o_ = sqlite3.connect(str(tmp))
    with o_:
        s_.backup(o_)
    o_.close()
    s_.close()
    rep["integrity_dest"] = integrity(tmp, ro=False)
    rep["counts_dest"] = db_info(tmp)["counts"]
    mism = {t: (rep["counts_source"].get(t), rep["counts_dest"].get(t)) for t in rep["counts_source"]
            if rep["counts_source"].get(t) != rep["counts_dest"].get(t)}
    if rep["integrity_dest"] != "ok" or mism:
        tmp.unlink()
        rep["error"] = f"копия не прошла проверку: integrity={rep['integrity_dest']}, расхождения строк={mism}"
        return rep
    # копия переводится в обычный журнал (вся информация — в одном файле), служебные -wal/-shm копии удаляются
    c = sqlite3.connect(str(tmp))
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()
    for ext in ("-wal", "-shm"):
        for base in (tmp, dst):
            q = Path(str(base) + ext)
            if q.exists():
                q.unlink()
    os.replace(tmp, dst)
    c = sqlite3.connect(str(dst))
    c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('imported_from',?)", (str(src),))
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('imported_at',?)", (now_iso(),))
    c.commit()
    c.close()
    # настройки прежней установки переносятся, только если в новой папке их ещё нет
    for name in ("settings.json", "secrets.bin"):
        a, b = src.parent / name, dst.parent / name
        if a.exists() and not b.exists():
            shutil.copy2(a, b)
            rep.setdefault("copied_files", []).append(name)
    st1 = src.stat()
    rep["source_untouched"] = (st0.st_size, st0.st_mtime) == (st1.st_size, st1.st_mtime)
    rep["ok"] = True
    rep["finished_at"] = now_iso()
    (dst.parent / f"import-report-{stamp}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep


def rollback_import(dst: Path) -> Path | None:
    """Отменить перенос: вернуть базу, которая была до переноса (или убрать перенесённую, если базы не было)."""
    dst = Path(dst)
    bdir = dst.parent / "backups"
    marks = sorted(list(bdir.glob("scanner-*-before-import.db")) + list(bdir.glob("scanner-*-before-import.none")))
    if not marks:
        return None
    last = marks[-1]
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    if dst.exists():
        shutil.copy2(dst, bdir / f"scanner-{stamp}-rolled-back-import.db")
    for ext in ("-wal", "-shm"):
        q = Path(str(dst) + ext)
        if q.exists():
            q.unlink()
    if last.suffix == ".none":
        if dst.exists():
            dst.unlink()
    else:
        shutil.copy2(last, dst)
    last.rename(last.with_name(last.name + ".used"))
    return last


def pending_import_path() -> Path:
    return config.DATA_DIR / "pending_import.json"
