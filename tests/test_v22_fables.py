"""2.2 релиз A: атрибуция выпуска FABLES по записи Minter, эпохи, индексатор (дедупликация, реорганизации, сбои, лимиты),
изоляция от сканера. Нумерация «сценарий N» — по списку обязательных проверок этапа 2."""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from app import analytics, db, fables as F, fables_emission as FE, fables_indexer as FI, jobs
from app.http import SourceError
from app.keccak import keccak256, selector, topic
from app.util import dumps, now_iso

WALLET = "0x" + "a1" * 20
OTHER = "0x" + "b2" * 20
D = 10 ** 18


def _t(addr: str) -> str:
    return "0x" + addr.replace("0x", "").rjust(64, "0")


def _w(v: int) -> str:
    return format(int(v), "064x")


def log(name: str, block: int, tx: str, li: int, **a) -> dict:
    """Событие в формате ответа eth_getLogs (topics/data как у RPC)."""
    e = F.EVENTS[name]
    topics, data = [e["topic0"]], ""
    for f, typ, idx in e["fields"]:
        v = a[f]
        enc = _t(v) if typ == "address" else (v.replace("0x", "").rjust(64, "0") if typ == "bytes32" else _w(int(v)))
        if idx:
            topics.append("0x" + enc[-64:] if not enc.startswith("0x") else enc)
        else:
            data += enc[-64:] if not enc.startswith("0x") else enc[2:]
    return {"address": F.ADDR[e["contract"]], "topics": topics, "data": "0x" + data, "blockNumber": hex(block),
            "blockHash": "0x" + format(block, "064x"), "transactionHash": tx, "logIndex": hex(li)}


def _tx(n: int) -> str:
    return "0x" + format(n, "064x")


def setup_fables(with_supply=True):
    db.ex("INSERT OR IGNORE INTO tokens(uid,chain,address,address_display,symbol,decimals,first_seen_at) VALUES(?,?,?,?,?,?,?)",
          (F.TOKEN_UID, F.CHAIN, F.TOKEN, F.TOKEN, "FABLES", 18, now_iso()))
    analytics.set_enabled("fables", True)
    W0 = F.week_start(F.FIRST_WEEK)
    # эпохи: 2963 начинается на блоке 1000, 2964 — на 2000, 2965 — на 3000 (точные блоки)
    with db.tx() as c:
        for w, b in ((F.FIRST_WEEK, 1000), (F.FIRST_WEEK + 1, 2000), (F.FIRST_WEEK + 2, 3000)):
            c.execute("INSERT INTO an_epochs(chain_id,week,start_ts,start_block,start_evidence,method_version,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (F.CHAIN_ID, w, F.week_start(w), b, dumps({"block": b}), F.METHOD_VERSION, now_iso()))
    L = [
        log("Minted", 500, _tx(1), 1, allocation=3, to=F.ADDR["minter"], amount=40_000_000 * D, by="0x" + "35" * 20),
        log("Minted", 510, _tx(2), 1, allocation=9, to=F.ADDR["seeder_safe"], amount=15_000_000 * D, by="0x" + "35" * 20),
        log("Minted", 1999, _tx(3), 1, allocation=0, to=WALLET, amount=100 * D, by=F.ADDR["stream"]),    # до границы недели 2964
        log("StreamClaimed", 1999, _tx(3), 2, key="0x" + "11" * 32, kind=0, id=7, amount=100 * D, to=WALLET, asOf=1),
        log("Minted", 2000, _tx(4), 1, allocation=0, to=WALLET, amount=200 * D, by=F.ADDR["stream"]),    # ровно на границе
        log("StreamClaimed", 2000, _tx(4), 2, key="0x" + "12" * 32, kind=0, id=7, amount=200 * D, to=WALLET, asOf=2),
        log("Minted", 2100, _tx(5), 1, allocation=0, to=F.ADDR["stream"], amount=5 * D, by=F.ADDR["stream"]),  # rebase: mint на поток
        log("StreamClaimed", 2100, _tx(5), 3, key="0x" + "13" * 32, kind=1, id=270, amount=5 * D, to=OTHER, asOf=3),
        log("Minted", 2200, _tx(6), 1, allocation=1, to=WALLET, amount=30 * D, by=F.ADDR["points_liquid"]),
        log("Minted", 2300, _tx(7), 1, allocation=1, to=F.ADDR["points_auto_max"], amount=40 * D, by=F.ADDR["points_auto_max"]),
        log("Transfer", 2300, _tx(7), 2, **{"from": F.ADDR["points_auto_max"], "to": F.ADDR["escrow"], "value": 40 * D}),
        log("Minted", 2400, _tx(8), 1, allocation=2, to=OTHER, amount=7 * D, by=F.ADDR["prologue_redeemer"]),
        log("Minted", 2500, _tx(9), 1, allocation=0, to=OTHER, amount=9 * D, by=OTHER),                  # неизвестный вызывающий
        log("WeeklyBudgetSet", 900, _tx(20), 0, week=F.FIRST_WEEK, amount=10_000_000 * D),
        log("WeeklyBudgetSet", 901, _tx(21), 0, week=F.FIRST_WEEK + 1, amount=9_800_000 * D),
        log("WeeklyBudgetSet", 1500, _tx(22), 0, week=F.FIRST_WEEK + 1, amount=9_900_000 * D),           # пересмотр бюджета
        log("WeeksClosed", 2050, _tx(23), 0, throughWeek=F.FIRST_WEEK, closedTotal=5_000_000 * D),
        log("RootPosted", 1100, _tx(24), 0, seq=0, root="0x" + "aa" * 32, cumulativeTotal=1_000 * D, asOf=W0 + 3600),
    ]
    by_src = {"minter.Minted": [], "stream.events": [], "token.Transfer.forwarders": []}
    for lg in L:
        src = "minter.Minted" if lg["address"] == F.ADDR["minter"] else "stream.events" if lg["address"] == F.ADDR["stream"] else "token.Transfer.forwarders"
        by_src[src].append(lg)
    for src, ls in by_src.items():
        FI.write_logs(src, 1, 3500, ls, overlap_to=0)
    if with_supply:
        for lg in L:
            if lg["address"] == F.ADDR["minter"]:
                a = F.decode("Minted", lg)
                db.ex("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,counterparty,amount_raw,amount) VALUES(?,?,?,?,?,?,?,?)",
                      (F.TOKEN_UID, "mint", lg["transactionHash"], 0, int(lg["blockNumber"], 16), a["to"], a["amount"], 0.0))
    FI.add_evidence("minter", "allocations", {"allocations": {str(i): {"ceiling": str(c * 10 ** 6 * D), "minted": "0"} for i, c in
                                                              enumerate([500, 120, 25, 40, 40, 80, 75, 50, 40, 30])},
                                              "totalMinted": str((40_000_000 + 15_000_000 + 100 + 200 + 5 + 30 + 40 + 7 + 9) * D), "tge": W0}, 3500)
    return L


def compute():
    _, data = FE.load(F.TOKEN_UID)
    return FE.compute(F.TOKEN_UID, data)


def cats(r):
    return {c["key"]: c for c in r["categories"]}


# ------------------------------------------------------------------ основа
def test_keccak_known_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert topic("Transfer(address,address,uint256)") == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    assert selector("minter()") == "0x07546172" and selector("balanceOf(address)") == "0x70a08231"


def test_s1_epoch_math_matches_contract_formula():
    """Сценарий 1: неделя 2963 = 2026-10-20 00:00 UTC (Stream.startAt); границы — вторник 00:00; 00:00:00 — уже новая неделя."""
    s = F.week_start(F.FIRST_WEEK)
    assert _dt.datetime.fromtimestamp(s, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M %a") == "2026-10-20 00:00 Tue"
    assert F.week_of(s) == F.FIRST_WEEK and F.week_of(s - 1) == F.FIRST_WEEK - 1
    assert F.week_start(F.FIRST_WEEK + 1) - s == 7 * 86400
    assert _dt.datetime.fromtimestamp(F.week_start(F.FIRST_WEEK + 2), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-11-03 00:00"


def test_s2_events_before_and_after_epoch_boundary():
    setup_fables()
    r = compute()
    e = {x["week"]: x for x in r["epochs"]}
    # mint на блоке 1999 — неделя 2963; на блоке 2000 (первый блок недели 2964) — уже 2964
    assert e[F.FIRST_WEEK]["minted_by_category"]["LP_EMISSION"]["raw"] == str(100 * D)
    assert e[F.FIRST_WEEK + 1]["minted_by_category"]["LP_EMISSION"]["raw"] == str(200 * D)
    assert e[F.FIRST_WEEK]["end_block"] == 2000 and e[F.FIRST_WEEK + 1]["start_block"] == 2000
    assert e[F.FIRST_WEEK]["first_week_note"]


def test_s3_s4_lp_to_wallet_and_rebase_via_internal_call():
    """3: LP mint прямо на кошелёк — LP_EMISSION. 4: rebase минтится на контракт потока внутренним вызовом;
    категория — по записи Minter и Claimed(kind=1), а не по transaction.to или получателю."""
    setup_fables()
    c = cats(compute())
    assert c["LP_EMISSION"]["amount"]["raw"] == str(300 * D) and c["LP_EMISSION"]["evidence"] == "confirmed"
    assert c["REBASE"]["amount"]["raw"] == str(5 * D) and c["REBASE"]["evidence"] == "confirmed"


def test_s5_s6_points_and_team_are_not_lp():
    setup_fables()
    c = cats(compute())
    assert c["POINTS_LIQUID"]["amount"]["raw"] == str(30 * D)
    assert c["POINTS_LOCKED"]["amount"]["raw"] == str(40 * D)
    assert c["GENESIS_TEAM"]["amount"]["raw"] == str(40_000_000 * D)
    assert c["GENESIS_SEEDER"]["amount"]["raw"] == str(15_000_000 * D)
    assert c["LP_EMISSION"]["amount"]["raw"] == str(300 * D)  # ни points, ни genesis в LP не попали


def test_s7_unknown_caller_and_unmatched_supply_stay_unknown():
    setup_fables()
    # mint в supply_events без записи Minter в той же транзакции
    db.ex("INSERT INTO supply_events(token_uid,kind,tx_hash,log_index,block,counterparty,amount_raw,amount) VALUES(?,?,?,?,?,?,?,?)",
          (F.TOKEN_UID, "mint", _tx(99), 0, 2600, OTHER, str(11 * D), 0.0))
    r = compute()
    c = cats(r)
    assert c["UNKNOWN"]["amount"]["raw"] == str(20 * D)  # 9 (неизвестный вызывающий) + 11 (без записи Minter)
    assert r["reconciliation"]["supply_without_minted_n"] == 1 and r["reconciliation"]["exact"] is False


def test_s8_weak_registry_evidence_is_not_upgraded():
    setup_fables()
    FI.add_evidence("bribes", "code", {"address": F.ADDR["bribes"], "kind": "contract", "code_bytes": 8086}, 3500)
    r = compute()
    row = next(x for x in r["investigations"]["registry"] if x["key"] == "bribes")
    assert row["evidence"] == "claimed" and "роль не подтверждает" in row["note"]


def test_s9_budget_statuses_never_final_with_unknown_split():
    setup_fables()
    r = compute()
    e = {x["week"]: x for x in r["epochs"]}
    w = e[F.FIRST_WEEK]
    assert w["utilization_total_stream"]["status"] in ("CONFIRMED", "INCOMPLETE_EPOCH")
    for ep in r["epochs"]:
        for k in ("utilization_total_stream", "utilization_lp", "utilization_rebase", "utilization_operating"):
            assert ep[k]["status"] in ("CONFIRMED", "PARTIAL", "UNKNOWN", "INCOMPLETE_EPOCH")
        assert ep["utilization_lp"]["status"] == "UNKNOWN"  # разбиение начисления по видам не доказано
    # бюджет недели 2964 пересмотрен: эффективный — последнее значение, на начало недели — значение до её первого блока
    w2 = e[F.FIRST_WEEK + 1]
    assert w2["budget_effective"]["raw"] == str(9_900_000 * D) and w2["budget_at_start"]["raw"] == str(9_900_000 * D)
    assert len(w2["budget_history"]) == 2


def test_s10_claims_do_not_double_supply():
    setup_fables()
    r = compute()
    assert r["totals"]["minted"]["raw"] == str((40_000_000 + 15_000_000 + 100 + 200 + 5 + 30 + 40 + 7 + 9) * D)
    assert r["reconciliation"]["exact"] is True  # Σ Minted = totalMinted, каждый mint сверен


def test_points_locked_forwarding_confirmed():
    setup_fables()
    p = compute()["investigations"]["points_locked"]["points_auto_max"]
    assert p["minted_to_itself"]["raw"] == str(40 * D) and p["transferred_to_escrow"]["raw"] == str(40 * D) and p["role_confirmed"]


# ------------------------------------------------------------------ индексатор
def test_s16_reindex_creates_no_duplicates():
    L = setup_fables()
    n = db.q1("SELECT COUNT(*) n FROM an_events")["n"]
    FI.write_logs("minter.Minted", 1, 3500, [l for l in L if l["address"] == F.ADDR["minter"]], overlap_to=3500)
    assert db.q1("SELECT COUNT(*) n FROM an_events")["n"] == n
    assert db.q1("SELECT COUNT(*) n FROM an_events WHERE orphaned=1")["n"] == 0


def test_reorg_marks_orphaned_and_rehashes_without_deleting():
    L = setup_fables()
    mints = [l for l in L if l["address"] == F.ADDR["minter"]]
    gone = mints[-1]
    moved = dict(mints[-2], blockHash="0x" + "ee" * 32)
    FI.write_logs("minter.Minted", 2000, 3500, [m for m in mints if int(m["blockNumber"], 16) >= 2000 and m is not gone and m is not mints[-2]] + [moved],
                  overlap_to=3500)
    o = db.q1("SELECT orphaned FROM an_events WHERE tx_hash=?", (gone["transactionHash"],))
    assert o["orphaned"] == 1  # не удалено
    assert db.q1("SELECT block_hash FROM an_events WHERE tx_hash=? AND log_index=1", (moved["transactionHash"],))["block_hash"] == "0x" + "ee" * 32
    r = compute()
    assert r["reconciliation"]["minted_events"] == len(mints) - 1  # осиротевшая запись Minter исключена
    # основной сканер ещё видит этот mint (supply_events) — он показан как mint без записи Minter, а не скрыт
    assert r["reconciliation"]["supply_without_minted_n"] == 1 and r["reconciliation"]["exact"] is False
    FI.write_logs("minter.Minted", 2000, 3500, [m for m in mints if int(m["blockNumber"], 16) >= 2000], overlap_to=3500)
    assert db.q1("SELECT orphaned FROM an_events WHERE tx_hash=?", (gone["transactionHash"],))["orphaned"] == 0


class FakeRpc:
    def __init__(self, logs, fail_after=None, limit=10_000):
        self.logs, self.fail_after, self.limit, self.calls = logs, fail_after, limit, 0

    def call(self, method, params):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise SourceError("rpc:robinhood", "unavailable", "HTTP 503")
        f = params[0]
        a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        out = [l for l in self.logs if a <= int(l["blockNumber"], 16) <= b and l["address"] == f["address"]]
        if len(out) > self.limit:
            raise SourceError("rpc:robinhood", "rpc_error", "-32000: logs matched by query exceeds limit of 10000")
        return out


def test_s17_rpc_failure_keeps_collected_data_and_checkpoint():
    setup_fables(with_supply=False)
    with db.tx() as c:
        c.execute("DELETE FROM an_events")
    mints = [log("Minted", F.DEPLOY_BLOCK + i * 100, _tx(1000 + i), 1, allocation=0, to=WALLET, amount=D, by=F.ADDR["stream"]) for i in range(30)]
    st = FI.STREAMS[0]
    rpc = FakeRpc(mints, fail_after=2)
    with pytest.raises(SourceError):
        FI.index_stream(rpc, st, F.DEPLOY_BLOCK + 3000, overlap=0, max_range=1000)
    n = db.q1("SELECT COUNT(*) n FROM an_events")["n"]
    cp = FI.checkpoint("logs:" + st["key"])["block"]
    assert n == 20 and cp == F.DEPLOY_BLOCK + 1999  # две успешные порции сохранены, точка — на конце последней
    FI.index_stream(FakeRpc(mints), st, F.DEPLOY_BLOCK + 3000, overlap=0, max_range=1000)
    assert db.q1("SELECT COUNT(*) n FROM an_events")["n"] == 30


def test_log_limit_halves_range():
    setup_fables(with_supply=False)
    mints = [log("Minted", F.DEPLOY_BLOCK + i, _tx(5000 + i), 1, allocation=0, to=WALLET, amount=D, by=F.ADDR["stream"]) for i in range(50)]
    FI.index_stream(FakeRpc(mints, limit=10), FI.STREAMS[0], F.DEPLOY_BLOCK + 60, overlap=0, max_range=100_000)
    assert db.q1("SELECT COUNT(*) n FROM an_events WHERE tx_hash LIKE ?", ("0x" + "0" * 50 + "%",))["n"] >= 50


def test_limited_rpc_budget(monkeypatch):
    from app.chains import CHAINS
    import time as _t
    r = FI.LimitedRpc(CHAINS["robinhood"], max_calls=2, deadline=_t.monotonic() + 60, min_interval=0)
    monkeypatch.setattr("app.sources.evm_rpc.Rpc.call", lambda self, m, p: "0x1")
    r.call("eth_blockNumber", [])
    r.call("eth_blockNumber", [])
    with pytest.raises(FI.BudgetExhausted):
        r.call("eth_blockNumber", [])


def test_s18_evidence_history_kept_and_not_erased():
    setup_fables(with_supply=False)
    assert FI.add_evidence("escrow", "state", {"totalLocked": "1"}, 10)
    assert not FI.add_evidence("escrow", "state", {"totalLocked": "1"}, 11)  # то же значение — без дубля
    assert FI.add_evidence("escrow", "state", {"totalLocked": "2"}, 12)
    assert db.q1("SELECT COUNT(*) n FROM an_evidence WHERE subject='escrow' AND kind='state'")["n"] == 2
    assert FI.latest_evidence("escrow", "state")["payload"]["totalLocked"] == "2"


def test_team_lock_chain_from_receipt():
    setup_fables()
    T = F.EVENTS["Transfer"]["topic0"]
    LOCKED = topic("Locked(uint256,address,uint128,uint64)")
    FI.add_evidence("team_lock", "creation_receipt", {"tx": _tx(1), "block": 500, "from": "0x" + "35" * 20, "to": F.ADDR["minter"], "selector": "0x42772d71",
                                                      "logs": [{"i": 0, "address": F.TOKEN, "topics": [T, _t("0x0"), _t(F.ADDR["minter"])], "data": "0x" + _w(40_000_000 * D)},
                                                               {"i": 3, "address": F.TOKEN, "topics": [T, _t(F.ADDR["minter"]), _t(F.ADDR["escrow"])], "data": "0x" + _w(40_000_000 * D)},
                                                               {"i": 4, "address": F.ADDR["escrow"], "topics": [T, _t("0x0"), _t(F.ADDR["team_safe"]), "0x" + _w(1)], "data": "0x"},
                                                               {"i": 5, "address": F.ADDR["escrow"], "topics": [LOCKED, "0x" + _w(1), _t(F.ADDR["team_safe"])],
                                                                "data": "0x" + _w(40_000_000 * D) + _w(2 ** 64 - 1)}]}, 500)
    FI.add_evidence("escrow", "state", {"PERMANENT": str(2 ** 64 - 1), "MAX_DURATION_s": 728 * 86400, "lock1_words": [str(40_100_000 * D), str(2 ** 64 - 1)],
                                        "lock1_owner": F.ADDR["team_safe"]}, 3500)
    t = compute()["investigations"]["team_lock"]
    assert t["chain_confirmed"] is True and t["current"]["permanent"] is True and t["current"]["owner"] == F.ADDR["team_safe"]
    assert t["withdrawable_now"] is False and "unlockPermanent" in t["withdraw_note"]  # «permanent» ≠ «навсегда вне обращения»


# ------------------------------------------------------------------ изоляция
def test_s20_disabled_fables_schedules_nothing():
    r = jobs.Runner()
    r.plan()
    assert r.lanes["analytics"].size() == 0
    assert not FI.schema_present()


def test_s19_indexer_yields_to_chain_lane(monkeypatch):
    analytics.set_enabled("fables", True)
    analytics._last_submit.clear()
    r = jobs.Runner()
    r.lanes["chain"].current = {"job": "onchain"}
    r.plan()
    assert all(j["job"] != "fables_index" for j in r.lanes["analytics"].keys.values())
    assert "ончейн" in analytics._deferred["reason"]
    r.lanes["chain"].current = None
    analytics._last_submit.clear()
    r.plan()
    assert any(j["job"] == "fables_index" for j in r.lanes["analytics"].keys.values())


def test_compute_is_network_free(monkeypatch):
    setup_fables()
    import httpx
    from app import http
    calls = []
    http.set_client(httpx.Client(transport=httpx.MockTransport(lambda q: calls.append(1) or httpx.Response(200, json={}))))
    try:
        st = analytics.run_module("fables", deadline_s=30)
        assert st["recomputed"] == 1 and st["errors"] == 0 and calls == []
    finally:
        http.set_client(None)


def test_s22_fables_existing_data_untouched():
    setup_fables()
    before = {t: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("supply_events", "observations", "tokenomics_versions", "address_labels")}
    facts = db.q1("SELECT facts, level, status FROM tokens WHERE uid=?", (F.TOKEN_UID,))
    analytics.run_module("fables", deadline_s=30, force=True)
    assert {t: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in before} == before
    assert tuple(db.q1("SELECT facts, level, status FROM tokens WHERE uid=?", (F.TOKEN_UID,))) == tuple(facts)
