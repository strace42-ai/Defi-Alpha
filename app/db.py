"""SQLite: схема и вспомогательные функции.

Принципы:
- observations — журнал наблюдений, только добавление (история не перезаписывается);
- каждое значение хранит источник, время наблюдения, время получения, статус достоверности,
  метод получения и ссылку для проверки;
- исходные ответы API сохраняются в raw_responses (gzip) для воспроизводимости.
"""
from __future__ import annotations

import gzip
import logging
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from . import config
from .util import dumps, now_iso

DB_PATH = config.DATA_DIR / "scanner.db"
_local = threading.local()

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tokens (
    uid TEXT PRIMARY KEY,               -- "<chain>:<normalized address>"
    chain TEXT NOT NULL,
    address TEXT NOT NULL,              -- нормализованный (EVM: lower; Solana: как есть)
    address_display TEXT,               -- исходное написание (checksum)
    symbol TEXT, name TEXT, decimals INTEGER,
    first_seen_at TEXT NOT NULL,        -- первое обнаружение НАШЕЙ системой
    discovery_source TEXT, discovery_reason TEXT,
    is_quote_asset INTEGER DEFAULT 0,
    asset_class TEXT,                   -- token | quote | tokenized_stock | launchpad_meme ...
    coingecko_id TEXT,
    protocol_slug TEXT,                 -- подтверждённая связь с DefiLlama
    protocol_link_evidence TEXT,
    level TEXT DEFAULT 'A',             -- A..E
    status TEXT DEFAULT 'UNKNOWN',      -- PASS/FAIL/UNKNOWN/DATA ERROR/STALE DATA/MANUAL REVIEW
    status_reason TEXT,
    score_confirmed REAL, score_max_verifiable REAL, score_preliminary REAL,
    confidence TEXT,
    facts TEXT,                         -- JSON: последняя сводка фактов с источниками
    last_market_at TEXT, last_enriched_at TEXT, last_deep_at TEXT, last_onchain_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tokens_chain ON tokens(chain);
CREATE INDEX IF NOT EXISTS ix_tokens_symbol ON tokens(symbol);
CREATE INDEX IF NOT EXISTS ix_tokens_level ON tokens(level);

CREATE TABLE IF NOT EXISTS pools (
    uid TEXT PRIMARY KEY,               -- "<chain>:<pool address/id lower>"
    chain TEXT NOT NULL, address TEXT NOT NULL,
    dex TEXT, name TEXT,
    base_uid TEXT, quote_uid TEXT,
    pool_created_at TEXT,               -- дата создания ПУЛА (не токена!)
    first_seen_at TEXT, last_seen_at TEXT,
    sources TEXT                        -- json список источников
);
CREATE INDEX IF NOT EXISTS ix_pools_base ON pools(base_uid);
CREATE INDEX IF NOT EXISTS ix_pools_quote ON pools(quote_uid);

CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, url TEXT, status INTEGER, fetched_at TEXT, body BLOB
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,          -- token | pool | protocol | chain
    entity_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL, value_text TEXT, unit TEXT,
    source TEXT NOT NULL,
    observed_at TEXT,                   -- время, к которому относится значение
    fetched_at TEXT NOT NULL,           -- время получения
    confidence TEXT,                    -- confirmed | reported | derived | estimated
    method TEXT,
    verify_url TEXT,
    raw_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_obs_entity ON observations(entity_type, entity_id, metric, fetched_at);

CREATE TABLE IF NOT EXISTS filter_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT NOT NULL, filter_key TEXT NOT NULL,
    status TEXT NOT NULL, confidence TEXT,
    reason TEXT, evidence TEXT, params TEXT,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fr_token ON filter_results(token_uid, filter_key, evaluated_at);

CREATE TABLE IF NOT EXISTS filter_current (
    token_uid TEXT NOT NULL, filter_key TEXT NOT NULL,
    status TEXT NOT NULL, confidence TEXT, reason TEXT, evidence TEXT, evaluated_at TEXT,
    PRIMARY KEY(token_uid, filter_key)
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT, field TEXT, old_value TEXT, new_value TEXT, reason TEXT, at TEXT
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT, metric TEXT, values_json TEXT, detected_at TEXT, note TEXT, resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT, alert_type TEXT, severity TEXT, title TEXT,
    condition TEXT, values_json TEXT, sources_json TEXT, limitations TEXT,
    dedup_key TEXT UNIQUE, created_at TEXT, acknowledged INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watchlist (token_uid TEXT PRIMARY KEY, added_at TEXT, note TEXT);

CREATE TABLE IF NOT EXISTS manual_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT NOT NULL,
    fact_key TEXT NOT NULL,             -- vc_status | funding | tge_date | utility | official_link | team | catalyst | value_capture | audit
    value TEXT, category TEXT,
    evidence_url TEXT, source_type TEXT, -- official_docs | official_social | third_party | onchain | user
    note TEXT, entered_by TEXT, entered_at TEXT, active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_mf_token ON manual_facts(token_uid, fact_key);

CREATE TABLE IF NOT EXISTS tokenomics_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_uid TEXT NOT NULL, version_label TEXT, published_at TEXT,
    source_url TEXT, source_type TEXT, is_current INTEGER DEFAULT 0,
    params TEXT, note TEXT, entered_at TEXT, superseded_by INTEGER
);

CREATE TABLE IF NOT EXISTS supply_events (
    token_uid TEXT NOT NULL, kind TEXT NOT NULL,      -- mint | burn
    tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
    block INTEGER, ts TEXT, ts_source TEXT,            -- ts_source: block (точное время блока) | interpolated
    counterparty TEXT, amount_raw TEXT,                -- amount_raw: целые минимальные единицы (источник истины)
    amount REAL,                                       -- только для отображения; в расчётах не используется
    PRIMARY KEY(token_uid, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS ix_se_token ON supply_events(token_uid, block);

CREATE TABLE IF NOT EXISTS address_labels (
    chain TEXT, address TEXT, label TEXT, kind TEXT, source_url TEXT, source_type TEXT, token_uid TEXT,
    PRIMARY KEY(chain, address)
);

CREATE TABLE IF NOT EXISTS scan_progress (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT, source TEXT, run_at TEXT, pages INTEGER, pools INTEGER,
    window_start TEXT, window_end TEXT, continuous INTEGER, gap_start TEXT, gap_end TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY, last_success_at TEXT, last_error_at TEXT, last_error TEXT,
    calls_total INTEGER DEFAULT 0, errors_total INTEGER DEFAULT 0, rate_limited_total INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, started_at TEXT, finished_at TEXT,
    status TEXT, progress TEXT, stats TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS protocols (
    slug TEXT PRIMARY KEY, name TEXT, category TEXT, chains TEXT, address TEXT, gecko_id TEXT,
    symbol TEXT, url TEXT, listed_at TEXT, raw TEXT, fetched_at TEXT
);

-- v2: пользовательские исключения из рейтинга лучших (не удаление!), журнал действий, состояние обновлений
CREATE TABLE IF NOT EXISTS hidden_tokens (
    token_uid TEXT PRIMARY KEY, hidden_at TEXT NOT NULL, until TEXT, reason TEXT, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, token_uid TEXT, action TEXT, details TEXT, at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ua_token ON user_actions(token_uid, at);
CREATE TABLE IF NOT EXISTS refresh_state (
    token_uid TEXT PRIMARY KEY, last_quote_at TEXT, last_quote_ok_at TEXT, last_quote_error TEXT,
    last_fund_at TEXT, last_mcap_at TEXT, last_cg_at TEXT
);
CREATE TABLE IF NOT EXISTS mcap_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, token_uid TEXT, assessed_at TEXT, mcap_type TEXT,
    value REAL, low REAL, high REAL, price REAL, circ_supply TEXT, method TEXT, filter_status TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS ix_mcap_token ON mcap_assessments(token_uid, assessed_at);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, where_ TEXT, message TEXT
);
"""


BUSY_TIMEOUT_MS = 5000       # только для других процессов (внутри процесса записи сериализованы _WRITE_LOCK)
WRITE_LOCK_TIMEOUT_S = 20    # ожидание общей блокировки писателей процесса
SLOW_TX_MS = 1000            # транзакция дольше — запись в журнал с местом вызова

_WRITE_LOCK = threading.RLock()
_tx_local = threading.local()
_flog = logging.getLogger("das.db")
tx_stats = {"count": 0, "max_ms": 0.0, "max_where": None, "slow": 0, "rollbacks": 0, "lock_timeouts": 0, "busy": 0}
_writer = {"thread": None, "since": None, "where": None}


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Соединение в ЯВНОМ режиме autocommit: драйвер не открывает транзакций сам (не зависит от значения по умолчанию
    в версии Python). Чтение не удерживает снимок; запись — только через tx() (BEGIN IMMEDIATE … COMMIT/ROLLBACK)."""
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    kw: dict[str, Any] = {"timeout": BUSY_TIMEOUT_MS / 1000, "check_same_thread": False}
    if sys.version_info >= (3, 12):
        kw["autocommit"] = True
    else:
        kw["isolation_level"] = None
    conn = sqlite3.connect(str(p), **kw)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_conn() -> sqlite3.Connection:
    """Соединение на поток (только для чтения и для операций внутри tx())."""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != DB_PATH:
        c = connect(DB_PATH)
        _local.conn = c
        _local.path = DB_PATH
    return c


def in_write_tx() -> bool:
    return getattr(_tx_local, "depth", 0) > 0


def _caller() -> str:
    import traceback as _tb
    for fr in reversed(_tb.extract_stack(limit=8)[:-3]):
        if not fr.filename.endswith(("db.py", "contextlib.py")):
            return f"{Path(fr.filename).name}:{fr.lineno} {fr.name}"
    return "?"


@contextmanager
def tx():
    """Транзакция записи. Внутри процесса писатели сериализуются (никаких SQLITE_BUSY между своими потоками);
    любое исключение → ROLLBACK, соединение не остаётся в транзакции. Вложенные tx() присоединяются к внешней.
    Сетевые запросы внутри tx() запрещены (http.request_json проверяет in_write_tx())."""
    if getattr(_tx_local, "depth", 0) > 0:
        _tx_local.depth += 1
        try:
            yield get_conn()
        finally:
            _tx_local.depth -= 1
        return
    if not _WRITE_LOCK.acquire(timeout=WRITE_LOCK_TIMEOUT_S):
        tx_stats["lock_timeouts"] += 1
        w = dict(_writer)
        raise sqlite3.OperationalError(f"write lock timeout {WRITE_LOCK_TIMEOUT_S}s; держит {w.get('thread')} "
                                       f"с {w.get('since')} в {w.get('where')}")
    c = get_conn()
    t0 = time.perf_counter()
    where = _caller()
    _writer.update(thread=threading.current_thread().name, since=now_iso(), where=where)
    _tx_local.depth = 1
    try:
        if c.in_transaction:  # не должно случаться; страховка от внешнего кода
            c.execute("ROLLBACK")
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            tx_stats["busy"] += 1
            raise
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            tx_stats["rollbacks"] += 1
            if c.in_transaction:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
    finally:
        _tx_local.depth = 0
        _writer.update(thread=None, since=None, where=None)
        ms = (time.perf_counter() - t0) * 1000
        tx_stats["count"] += 1
        if ms > tx_stats["max_ms"]:
            tx_stats["max_ms"], tx_stats["max_where"] = round(ms, 1), where
        _WRITE_LOCK.release()
        if ms > SLOW_TX_MS:
            tx_stats["slow"] += 1
            _flog.warning("долгая транзакция %.0f мс: %s", ms, where)


SCHEMA_VERSION = 2


def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_to_v2(c: sqlite3.Connection) -> None:
    """v1 → v2. Только добавление столбцов/таблиц; существующие данные не удаляются.
    filter_current (кеш текущих решений) очищается от фильтра 'vc' — история решений в filter_results сохраняется."""
    if "status" not in _cols(c, "watchlist"):
        c.execute("ALTER TABLE watchlist ADD COLUMN status TEXT DEFAULT 'watching'")
        c.execute("UPDATE watchlist SET status='watching' WHERE status IS NULL")
    if "circulation" not in _cols(c, "address_labels"):
        # excluded — доказательно не в обращении; included — в обращении; NULL/uncertain — не известно
        c.execute("ALTER TABLE address_labels ADD COLUMN circulation TEXT")
        c.execute("ALTER TABLE address_labels ADD COLUMN circulation_evidence TEXT")
    for col, typ in (("amount_usd", "REAL"), ("valuation_usd", "REAL")):
        if col not in _cols(c, "manual_facts"):
            c.execute(f"ALTER TABLE manual_facts ADD COLUMN {col} {typ}")
    if "orphaned" not in _cols(c, "supply_events"):
        c.execute("ALTER TABLE supply_events ADD COLUMN orphaned INTEGER DEFAULT 0")
    c.execute("DELETE FROM filter_current WHERE filter_key='vc'")
    c.execute("INSERT INTO user_actions(token_uid,action,details,at) VALUES(NULL,'migration','schema v1 → v2: No-VC больше не фильтр',?)",
              (now_iso(),))


def init_db(conn: sqlite3.Connection | None = None, backup_before_migration: bool = True) -> dict:
    """Создание/миграция схемы. Перед миграцией существующей базы — резервная копия; миграция в одной транзакции."""
    c = conn or get_conn()
    existed = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tokens'").fetchone() is not None
    ver = 0
    if existed:
        r = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() else None
        ver = int(r[0]) if r else 1
    info = {"existed": existed, "from_version": ver, "to_version": SCHEMA_VERSION, "backup": None}
    if existed and ver < SCHEMA_VERSION and backup_before_migration and conn is None:
        info["backup"] = str(backup_file(f"pre-migration-v{ver}-to-v{SCHEMA_VERSION}"))
    with _WRITE_LOCK:
        if c.in_transaction:
            c.execute("ROLLBACK")
        c.executescript(SCHEMA)  # новые таблицы (IF NOT EXISTS); в режиме autocommit — без висящих транзакций
        try:
            c.execute("BEGIN IMMEDIATE")
            if "ts_source" not in _cols(c, "supply_events"):
                c.execute("ALTER TABLE supply_events ADD COLUMN ts_source TEXT")
                c.execute("UPDATE supply_events SET ts_source='interpolated' WHERE ts IS NOT NULL")
            if ver < 2:
                _migrate_to_v2(c)
                c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
                c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('migrated_v2_at',?)", (now_iso(),))
            c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('created_at',?)", (now_iso(),))
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
    return info


def backup_file(label: str) -> Path:
    """Онлайн-копия базы (sqlite3 backup API)."""
    d = config.DATA_DIR / "backups"
    d.mkdir(parents=True, exist_ok=True)
    from .util import now_utc
    dst = d / f"scanner-{now_utc().strftime('%Y%m%d-%H%M%S')}-{label}.db"
    src = connect(DB_PATH)
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dst


def rollback_migration() -> Path | None:
    """Восстановить последнюю копию, сделанную перед миграцией (текущая база сохраняется рядом)."""
    d = config.DATA_DIR / "backups"
    cands = sorted(d.glob("scanner-*-pre-migration-*.db")) if d.exists() else []
    if not cands:
        return None
    import shutil
    cur = getattr(_local, "conn", None)
    if cur is not None:
        cur.close()
        _local.conn = None
    from .util import now_utc
    keep = DB_PATH.with_name(f"scanner-before-rollback-{now_utc().strftime('%Y%m%d-%H%M%S')}.db")
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, keep)
    for ext in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + ext)
        if p.exists():
            p.unlink()
    shutil.copy2(cands[-1], DB_PATH)
    return cands[-1]


def checkpoint() -> None:
    try:
        get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def set_db_path(p: Path) -> None:
    """Для тестов: переключение БД."""
    global DB_PATH
    DB_PATH = p
    _local.conn = None


# ---------------------------------------------------------------- helpers
def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, tuple(params)).fetchone()


def ex(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with tx() as c:
        return c.execute(sql, tuple(params))


def exmany(sql: str, rows: list, chunk: int = 500, on_chunk=None) -> int:
    """Массовая запись пакетами: каждая порция — отдельная короткая транзакция (между ними пишут другие)."""
    n = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        with tx() as c:
            c.executemany(sql, part)
        n += len(part)
        if on_chunk:
            on_chunk(len(part))
    return n


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def save_raw(source: str, url: str, status: int, body: bytes) -> int:
    body_z = gzip.compress(body or b"")  # сжатие — вне транзакции
    cur = ex("INSERT INTO raw_responses(source,url,status,fetched_at,body) VALUES(?,?,?,?,?)",
             (source, url, status, now_iso(), body_z))
    return int(cur.lastrowid)


def load_raw(raw_id: int) -> bytes | None:
    r = q1("SELECT body FROM raw_responses WHERE id=?", (raw_id,))
    return gzip.decompress(r["body"]) if r else None


def observe(entity_type: str, entity_id: str, metric: str, value: float | None, *,
            source: str, unit: str | None = None, value_text: str | None = None,
            observed_at: str | None = None, confidence: str = "reported",
            method: str | None = None, verify_url: str | None = None, raw_id: int | None = None,
            commit: bool = True) -> None:
    """Записать наблюдение (собственная короткая транзакция или текущая tx()). value=None — только с value_text."""
    if value is None and value_text is None:
        return
    ex("INSERT INTO observations(entity_type,entity_id,metric,value,value_text,unit,source,observed_at,"
       "fetched_at,confidence,method,verify_url,raw_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (entity_type, entity_id, metric, value, value_text, unit, source, observed_at, now_iso(),
        confidence, method, verify_url, raw_id))


def latest_obs(entity_type: str, entity_id: str, metric: str, source: str | None = None) -> dict | None:
    if source:
        r = q1("SELECT * FROM observations WHERE entity_type=? AND entity_id=? AND metric=? AND source=? "
               "ORDER BY fetched_at DESC, id DESC LIMIT 1", (entity_type, entity_id, metric, source))
    else:
        r = q1("SELECT * FROM observations WHERE entity_type=? AND entity_id=? AND metric=? "
               "ORDER BY fetched_at DESC, id DESC LIMIT 1", (entity_type, entity_id, metric))
    return dict(r) if r else None


def latest_obs_all_sources(entity_type: str, entity_id: str, metric: str) -> list[dict]:
    rows = q("SELECT o.* FROM observations o JOIN (SELECT source, MAX(id) mid FROM observations "
             "WHERE entity_type=? AND entity_id=? AND metric=? GROUP BY source) t ON o.id=t.mid "
             "ORDER BY o.fetched_at DESC", (entity_type, entity_id, metric))
    return rows_to_dicts(rows)


def obs_series(entity_type: str, entity_id: str, metric: str, source: str | None = None) -> list[dict]:
    sql = ("SELECT observed_at, fetched_at, value, source, confidence FROM observations WHERE entity_type=? "
           "AND entity_id=? AND metric=?")
    p: list[Any] = [entity_type, entity_id, metric]
    if source:
        sql += " AND source=?"
        p.append(source)
    sql += " ORDER BY COALESCE(observed_at, fetched_at), id"
    return rows_to_dicts(q(sql, p))


def _file_log(where: str, message: str) -> None:
    try:
        d = config.DATA_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} [{where}] {message[:4000]}\n")
    except OSError:
        pass


def log_error(where: str, message: str) -> None:
    """Никогда не бросает: сначала файл журнала, затем (если доступна) таблица error_log."""
    _file_log(where, message)
    if in_write_tx():  # нельзя писать в БД изнутри чужой (возможно, откатываемой) транзакции
        return
    try:
        ex("INSERT INTO error_log(at,where_,message) VALUES(?,?,?)", (now_iso(), where, message[:2000]))
    except Exception:  # noqa: BLE001 — БД недоступна: запись уже есть в файле
        pass


def get_progress(key: str) -> str | None:
    r = q1("SELECT value FROM scan_progress WHERE key=?", (key,))
    return r["value"] if r else None


def set_progress(key: str, value: str) -> None:
    ex("INSERT INTO scan_progress(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
       "value=excluded.value, updated_at=excluded.updated_at", (key, value, now_iso()))


def record_status_change(token_uid: str, field: str, old: Any, new: Any, reason: str) -> None:
    if str(old) == str(new):
        return
    ex("INSERT INTO status_history(token_uid,field,old_value,new_value,reason,at) VALUES(?,?,?,?,?,?)",
       (token_uid, field, None if old is None else str(old), None if new is None else str(new), reason, now_iso()))


def add_conflict(token_uid: str, metric: str, values: Any, note: str) -> None:
    # не дублировать открытый конфликт по той же метрике в течение суток
    with tx() as c:  # проверка и запись — атомарно
        r = c.execute("SELECT id FROM conflicts WHERE token_uid=? AND metric=? AND resolved=0 AND "
                      "detected_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')", (token_uid, metric)).fetchone()
        if r:
            c.execute("UPDATE conflicts SET values_json=?, note=? WHERE id=?", (dumps(values), note, r["id"]))
        else:
            c.execute("INSERT INTO conflicts(token_uid,metric,values_json,detected_at,note) VALUES(?,?,?,?,?)",
                      (token_uid, metric, dumps(values), now_iso(), note))
