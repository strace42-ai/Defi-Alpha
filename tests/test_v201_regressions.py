"""Регрессионные тесты аварийного исправления 2.0.1 (по аудиту релиза 2.0.0).
Данные — ИСКУССТВЕННЫЕ. Каждый тест воспроизводит конкретный дефект; до исправления они падали
(см. docs/audit/v2.0.1_regressions_before_fix.txt)."""
import sqlite3
import threading
import time as _real_time
from pathlib import Path

import pytest

from app import config, db, discovery, enrich, fundamentals, http, jobs
from app.chains import CHAINS
from app.http import SourceError
from app.util import dumps, loads, now_iso

RH = CHAINS["robinhood"]



def _bulk_insert(sql, rows):
    """Вставка тестовых данных отдельным соединением (не зависит от API слоя записи — тест запускается и на 2.0.0)."""
    c_ = sqlite3.connect(str(db.DB_PATH), timeout=10)
    with c_:
        c_.executemany(sql, rows)
    c_.close()

def tok(addr="0x" + "4" * 40, facts=None, **cols):
    uid, _ = discovery.upsert_token(RH, {"address": addr, "symbol": "TST"}, source="test", reason="synthetic")
    if facts is not None:
        db.ex("UPDATE tokens SET facts=? WHERE uid=?", (dumps(facts), uid))
    for k, v in cols.items():
        db.ex(f"UPDATE tokens SET {k}=? WHERE uid=?", (v, uid))
    return uid


def other_writer_ok(timeout=0.5) -> bool:
    c = sqlite3.connect(str(db.DB_PATH), timeout=timeout, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        c.close()


class _Boom(sqlite3.Connection):
    """Соединение, у которого падает заданный DML (как SQLITE_BUSY посреди транзакции)."""
    fail_on = ""

    def execute(self, sql, *a, **kw):
        if _Boom.fail_on and sql.lstrip().upper().startswith(_Boom.fail_on):
            raise sqlite3.OperationalError("database is locked (injected)")
        return super().execute(sql, *a, **kw)


@pytest.fixture
def boom(monkeypatch):
    orig = sqlite3.connect

    def con(*a, **kw):
        kw["factory"] = _Boom
        return orig(*a, **kw)
    monkeypatch.setattr(db.sqlite3, "connect", con)
    db._local.conn = None
    yield _Boom
    _Boom.fail_on = ""
    db._local.conn = None


# ================================================================ P0-1: транзакции, rollback, выживание потоков
def test_p01_failed_status_write_does_not_leave_open_transaction(boom):
    """http._status: INSERT прошёл, UPDATE упал — в 2.0.0 соединение оставалось в транзакции и держало блокировку."""
    boom.fail_on = "UPDATE SOURCE_STATUS"
    http._status("dexscreener", True)
    assert db.get_conn().in_transaction is False
    assert other_writer_ok(), "блокировка записи осталась висеть"


def test_p01_exception_inside_write_rolls_back(boom):
    uid = tok()
    boom.fail_on = "UPDATE TOKENS SET LEVEL"
    with pytest.raises(sqlite3.OperationalError):
        with db.tx() as c:
            c.execute("INSERT INTO watchlist(token_uid,added_at,status) VALUES(?,?,?)", (uid, now_iso(), "watching"))
            c.execute("UPDATE tokens SET level='E' WHERE uid=?", (uid,))
    assert db.q1("SELECT 1 FROM watchlist WHERE token_uid=?", (uid,)) is None  # частичная запись откатана
    assert db.get_conn().in_transaction is False and other_writer_ok()


def test_p01_connections_never_hold_implicit_transactions(monkeypatch):
    """Соединения открываются в явном режиме autocommit: чтение не держит снимок, даже если бы
    Python по умолчанию открывал транзакции (PEP 249, autocommit=False)."""
    orig = sqlite3.connect

    def pep249_default(*a, **kw):
        if "autocommit" not in kw and "isolation_level" not in kw:
            kw["autocommit"] = False
        return orig(*a, **kw)
    monkeypatch.setattr(db.sqlite3, "connect", pep249_default)
    db._local.conn = None
    c = db.get_conn()
    c.execute("SELECT 1").fetchall()
    assert c.in_transaction is False
    db._local.conn = None


def test_p01_worker_survives_failing_job_status_write(boom, monkeypatch):
    done = []
    monkeypatch.setitem(jobs.TASKS, "deep", lambda prog, **kw: done.append(kw["uid"]) or {})
    r = jobs.Runner()
    r.start(scheduler=False)
    boom.fail_on = "UPDATE JOBS"        # статус задачи записать невозможно
    r.submit("deep", 1, uid="a")
    r.submit("deep", 1, uid="b")
    t0 = _real_time.monotonic()
    while len(done) < 2 and _real_time.monotonic() - t0 < 5:
        threading.Event().wait(0.05)
    alive = r.lanes["main"].thread.is_alive()
    r.stop()
    assert done == ["a", "b"] and alive


def test_p01_log_error_never_raises(boom):
    boom.fail_on = "INSERT INTO ERROR_LOG"
    db.log_error("test", "сообщение")  # не должно бросать
    log = config.DATA_DIR / "logs" / "errors.log"
    assert log.exists() and "сообщение" in log.read_text(encoding="utf-8")


def test_p01_bulk_writes_are_chunked(monkeypatch):
    """Массовая запись разбивается на пакеты: между пакетами другие писатели получают доступ."""
    sizes = []
    orig = db.tx

    class Spy:
        def __init__(self):
            self.n = 0

    def run():
        db.exmany("INSERT INTO address_labels(chain,address,label) VALUES(?,?,?)",
                  [("robinhood", f"0x{i:040x}", "x") for i in range(1200)], chunk=500, on_chunk=lambda n: sizes.append(n))
    run()
    assert sizes == [500, 500, 200]
    assert db.q1("SELECT COUNT(*) n FROM address_labels")["n"] == 1200


# ================================================================ P0-2: ATH не уничтожается
PREV_ATH = {"ath_usd": 0.145, "ath_at": "2026-10-20T00:00:00Z", "ath_pool": "0xp", "first_candle_at": "2026-10-20T00:00:00Z",
            "first_trade_at": "2026-10-20T00:00:00Z", "method": "дневные свечи"}


def _deep_with(monkeypatch, *, candles=None, ohlcv_error=False, pools_error=False):
    from app.sources import dexscreener, geckoterminal
    uid = tok(facts={"ath": dict(PREV_ATH), "price_history": {"0xp": [[1, 0.1, 5]]}, "price": {"value": 0.06, "confidence": "derived"}})
    pool = {"pool_address": "0xp", "name": "T/WETH", "dex": "u", "pool_created_at": "2026-10-20T00:00:00Z",
            "base": {"address": uid.split(":")[1]}, "quote": {"address": "0x0bd7d308f8e1639fab988df18a8011f41eacad73"},
            "base_price_usd": 0.06, "quote_price_usd": 2300, "reserve_usd": 500000, "volume_h24": 1e5, "tx_h24": {"buys": 9, "sells": 9}}

    def tp(*a, **k):
        if pools_error:
            raise SourceError("geckoterminal", "unavailable", "нет сети")
        return [pool], None

    def oh(*a, **k):
        if ohlcv_error:
            raise SourceError("geckoterminal", "unavailable", "нет сети")
        return (candles or []), {}, None
    monkeypatch.setattr(geckoterminal, "token_pools", tp)
    monkeypatch.setattr(geckoterminal, "ohlcv", oh)
    monkeypatch.setattr(geckoterminal, "token_info", lambda *a, **k: (None, None))
    monkeypatch.setattr(dexscreener, "token_pairs", lambda *a, **k: ([], None))
    enrich.deep_enrich(uid)
    return enrich.get_facts(uid)


def test_p02_api_down_keeps_ath(monkeypatch):
    f = _deep_with(monkeypatch, ohlcv_error=True)
    assert f["ath"]["ath_usd"] == 0.145 and f["ath"].get("stale") is True and f["ath"].get("last_error")
    assert f["price_history"] == {"0xp": [[1, 0.1, 5]]}


def test_p02_empty_history_keeps_ath(monkeypatch):
    f = _deep_with(monkeypatch, candles=[])
    assert f["ath"]["ath_usd"] == 0.145 and f["ath"].get("stale") is True


def test_p02_pools_unavailable_keeps_price_and_ath(monkeypatch):
    f = _deep_with(monkeypatch, pools_error=True)
    assert f["ath"]["ath_usd"] == 0.145
    assert f["price"]["value"] == 0.06  # прежняя цена не заменяется на «нет цены»


def test_p02_higher_new_ath_replaces(monkeypatch):
    f = _deep_with(monkeypatch, candles=[[1792454400, 0.1, 0.2, 0.09, 0.18, 1000]])  # 2026-10-20
    assert f["ath"]["ath_usd"] == pytest.approx(0.2) and not f["ath"].get("stale")


def test_p02_shorter_history_with_lower_ath_does_not_erase(monkeypatch):
    f = _deep_with(monkeypatch, candles=[[1793404800, 0.06, 0.07, 0.05, 0.06, 1000]])  # только 2026-10-31
    assert f["ath"]["ath_usd"] == 0.145 and f["ath"].get("retained_reason")


def test_p02_more_complete_history_recomputes(monkeypatch):
    f = _deep_with(monkeypatch, candles=[[1792368000, 0.05, 0.09, 0.04, 0.08, 500], [1792454400, 0.08, 0.12, 0.07, 0.1, 900]])
    assert f["ath"]["first_candle_at"].startswith("2026-10-19")  # история длиннее прежней → пересчёт
    assert f["ath"]["ath_usd"] == pytest.approx(0.12)


# ================================================================ P0-3: связь с протоколом не теряется
def _linked():
    db.ex("INSERT INTO protocols(slug,name,category,address,fetched_at) VALUES('fables','Fables','Dexs',?,?)",
          ("robinhood:0x" + "4" * 40, now_iso()))
    uid = tok()
    res = fundamentals.link_protocol(uid)
    assert res["linked"]["slug"] == "fables"
    return uid


def test_p03_link_kept_when_catalog_temporarily_empty():
    uid = _linked()
    db.ex("DELETE FROM protocols")  # DefiLlama временно вернул пустой/неполный каталог
    res = fundamentals.link_protocol(uid)
    assert db.q1("SELECT protocol_slug FROM tokens WHERE uid=?", (uid,))["protocol_slug"] == "fables"
    assert res["linked"]["slug"] == "fables" and res["linked"].get("retained")


def test_p03_link_removed_only_on_evidence_of_change():
    uid = _linked()
    db.ex("UPDATE protocols SET address='robinhood:0x' || ? WHERE slug='fables'", ("5" * 40,))  # протокол указывает ДРУГОЙ токен
    res = fundamentals.link_protocol(uid)
    assert res["linked"] is None and res.get("unlinked_reason")


def test_p03_failed_metrics_keep_previous_protocol(monkeypatch):
    from app.sources import defillama
    uid = _linked()
    f = enrich.get_facts(uid)
    f["protocol"] = {"slug": "fables", "tvl_usd": 23e6, "category": "Dexs"}
    enrich.save_facts(uid, f, "updated_at")

    def down(*a, **k):
        raise SourceError("defillama", "unavailable", "нет сети")
    for fn in ("protocol", "fees_summary", "dex_summary", "protocols"):
        monkeypatch.setattr(defillama, fn, down)
    fundamentals.update_token_fundamentals(uid)
    p = enrich.get_facts(uid)["protocol"]
    assert p["tvl_usd"] == 23e6 and p.get("stale") is True


# ================================================================ P0-5: пользовательские действия
def _client():
    from fastapi.testclient import TestClient
    from app.api import app
    return TestClient(app, raise_server_exceptions=False)


def test_p05_hide_is_atomic_with_user_action(boom):
    uid = tok(level="C", status="MANUAL REVIEW")
    boom.fail_on = "INSERT INTO USER_ACTIONS"
    r = _client().post(f"/api/hide/{uid}", json={"reason": "x"})
    assert r.status_code >= 500 and "detail" in r.json()
    boom.fail_on = ""
    assert db.q1("SELECT COUNT(*) n FROM hidden_tokens WHERE token_uid=? AND active=1", (uid,))["n"] == 0  # без частичного состояния


def test_p05_repeated_actions_do_not_duplicate():
    uid = tok(level="C", status="MANUAL REVIEW")
    c = _client()
    for _ in range(3):
        assert c.post(f"/api/hide/{uid}", json={"reason": "x"}).status_code == 200
        assert c.post(f"/api/watchlist/{uid}", json={"status": "studying"}).status_code == 200
    acts = [r["action"] for r in db.q("SELECT action FROM user_actions WHERE token_uid=?", (uid,))]
    assert acts.count("hide") == 1 and acts.count("watch") == 1


def test_p05_monitoring_toggle_persists_and_errors_are_reported(boom):
    uid = tok()
    c = _client()
    assert c.post("/api/monitoring", json={"enabled": False}).json()["enabled"] is False
    config._cache = None
    assert config.load_settings()["scheduler_enabled"] is False  # сохранено на диск
    boom.fail_on = "INSERT INTO USER_ACTIONS"
    r = c.post(f"/api/watchlist/{uid}", json={"status": "studying"})
    assert r.status_code == 503 and "не выполнено" in r.json()["detail"]
    boom.fail_on = ""
    assert db.q1("SELECT 1 FROM watchlist WHERE token_uid=?", (uid,)) is None  # частичной записи нет


# ================================================================ P0-4: прежняя база не подменяется новой пустой
def _old_install(tmp_path: Path) -> Path:
    old = tmp_path / "DefiAlpha" / "data"
    old.mkdir(parents=True)
    c = sqlite3.connect(str(old / "scanner.db"))
    c.executescript((Path(__file__).parent / "fixtures" / "schema_v1.sql").read_text(encoding="utf-8"))
    c.execute("INSERT INTO meta VALUES('schema_version','1')")
    c.execute("INSERT INTO tokens(uid,chain,address,first_seen_at,symbol) VALUES('robinhood:0xb63d','robinhood','0xb63d','2026-11-04T19:31:00Z','FABLES')")
    c.executemany("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,amount_raw,amount) VALUES(?,?,?,?,?,?,?)",
                  [("robinhood:0xb63d", "mint", f"0x{i}", 0, i, "1", 1.0) for i in range(500)])
    c.commit()
    c.close()
    return old / "scanner.db"


def test_p04_detects_previous_installation_and_does_not_create_db(tmp_path):
    from app import dbmigrate
    old = _old_install(tmp_path)
    new_root = tmp_path / "DefiAlpha-v2.0.1"
    (new_root / "data").mkdir(parents=True)
    st = dbmigrate.startup_state(new_root / "data" / "scanner.db", search_roots=[tmp_path])
    assert st["mode"] == "setup_required"
    assert any(Path(c_["path"]) == old for c_ in st["candidates"])
    assert not (new_root / "data" / "scanner.db").exists()  # новая база не создана незаметно


def test_p04_import_previous_db_with_backup_counts_integrity_and_rollback(tmp_path):
    from app import dbmigrate
    old = _old_install(tmp_path)
    dst = tmp_path / "new" / "data" / "scanner.db"
    dst.parent.mkdir(parents=True)
    rep = dbmigrate.import_db(old, dst)
    assert rep["ok"] and rep["integrity_source"] == "ok" and rep["integrity_dest"] == "ok"
    assert rep["counts_source"]["supply_events"] == rep["counts_dest"]["supply_events"] == 500
    assert rep["source_untouched"] is True and old.exists()
    assert not list(dst.parent.glob("*.importing*"))  # временные файлы переноса не остаются
    db.set_db_path(dst)
    info = db.init_db()
    assert info["from_version"] == 1 and db.q1("SELECT COUNT(*) n FROM supply_events")["n"] == 500
    back = dbmigrate.rollback_import(dst)
    assert back is not None


def test_p04_fresh_install_without_previous_reports_new_empty_db(tmp_path):
    from app import dbmigrate
    st = dbmigrate.startup_state(tmp_path / "x" / "data" / "scanner.db", search_roots=[tmp_path / "nothing"])
    assert st["mode"] == "new_empty" and st["candidates"] == []


def test_p01_malformed_auxiliary_response_does_not_abort_analysis(monkeypatch):
    """Найдено стресс-тестом: неожиданный формат ответа метрик сети обрывал весь анализ токена."""
    from app.sources import defillama
    uid = _linked()
    monkeypatch.setattr(defillama, "chain_stablecoins", lambda *a, **k: ({"coins": {}}, None))  # dict вместо списка
    monkeypatch.setattr(defillama, "chain_tvl_history", lambda *a, **k: ("oops", None))
    monkeypatch.setattr(defillama, "protocol", lambda *a, **k: ({"name": "Fables", "tvl": [{"date": 1, "totalLiquidityUSD": 5.0}]}, None))
    monkeypatch.setattr(defillama, "fees_summary", lambda *a, **k: (None, None))
    monkeypatch.setattr(defillama, "dex_summary", lambda *a, **k: (None, None))
    monkeypatch.setattr(defillama, "chain_dex_overview", lambda *a, **k: (None, None))
    monkeypatch.setattr(defillama, "chain_fees_overview", lambda *a, **k: (None, None))
    f = fundamentals.update_token_fundamentals(uid)  # не должно бросать
    assert f["protocol"]["tvl_usd"] == 5.0
    h = fundamentals.get_chain_health("robinhood")
    assert any("формат" in e for e in h["errors"])


def test_p01_backup_does_not_block_writers():
    """Резервное копирование (sqlite backup API) идёт параллельно с записью: писатели не ждут его окончания."""
    from app import backup
    uid = tok()
    _bulk_insert("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,amount_raw,amount,ts) VALUES(?,?,?,?,?,?,?,?)",
              [(uid, "mint", f"0x{i:064x}", 0, i, "1" * 30, 1.0, now_iso()) for i in range(40000)])
    waits = []
    done = threading.Event()

    def writer():
        while not done.is_set():
            t0 = _real_time.perf_counter()
            db.ex("INSERT INTO error_log(at,where_,message) VALUES(?,?,?)", (now_iso(), "w", "x"))
            waits.append(_real_time.perf_counter() - t0)
            threading.Event().wait(0.005)
    th = threading.Thread(target=writer)
    th.start()
    for _ in range(3):
        backup.make_backup(label="test")
    done.set()
    th.join()
    assert waits and max(waits) < 1.0, f"запись ждала {max(waits):.2f} с во время резервного копирования"


def test_p04_existing_small_db_reports_richer_installation(tmp_path, monkeypatch):
    """Случай пользователя: 2.0.0 уже создала новую базу в новой папке; прежняя (богаче) — рядом."""
    from app import dbmigrate, startup
    old = _old_install(tmp_path)
    tok()  # текущая база: 1 токен, 0 событий
    monkeypatch.setattr(dbmigrate, "default_search_roots", lambda: [tmp_path])
    startup._started["done"] = True  # не запускать фоновый мониторинг в тесте
    try:
        st = startup.prepare()
    finally:
        startup._started["done"] = False
    assert any(Path(c_["path"]) == old for c_ in st["richer_elsewhere"])


def test_p01_interrupted_onchain_job_timestamps_are_filled_on_next_pass():
    """Задача прервана между вставкой событий и проставлением времени: следующий проход дозаполняет время (без потерь)."""
    from app import onchain
    uid = tok()
    rows = [(uid, "mint", "0x%064x" % b, 0, b, None, "0x" + "ab" * 20, str(10 ** 18), 1.0) for b in range(1000, 300_000, 100)]
    _bulk_insert("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,ts,counterparty,amount_raw,amount) "
              "VALUES(?,?,?,?,?,?,?,?,?)", rows)

    class R:
        def block_timestamp(self, b):
            return 1_700_000_000 + b * 2

    onchain._timestamps(R(), uid, 300_000, 1_700_600_000)
    left = db.q1("SELECT COUNT(*) n FROM supply_events WHERE token_uid=? AND ts IS NULL", (uid,))["n"]
    exact = db.q1("SELECT COUNT(*) n FROM supply_events WHERE token_uid=? AND ts_source='block'", (uid,))["n"]
    assert left == 0 and exact >= 2
    assert db.q1("SELECT COUNT(*) n FROM supply_events WHERE token_uid=?", (uid,))["n"] == len(rows)


def test_tests_and_selfcheck_never_write_into_real_data_folder():
    """selfcheck запускает автотесты на компьютере пользователя: тестовые копии не должны попадать в его data\\backups."""
    from app import backup
    p = backup.make_backup(label="test")
    assert Path(p).resolve().is_relative_to(Path(config.DATA_DIR).resolve())
    assert not (Path(__file__).resolve().parent.parent / "data" / "backups" / Path(p).name).exists()
