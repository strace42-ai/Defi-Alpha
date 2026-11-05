"""Ограниченный ончейн-индексатор FABLES (этап 2, релиз A). Только чтение публичного RPC.

Правила:
- включается отдельно (`analytics.fables_enabled`, по умолчанию выключен); только FABLES;
- собственные лимиты на задание: число RPC-вызовов, время, минимальный интервал между вызовами; плюс общий лимитер
  источника «rpc» (тот же, что у основного ончейн-модуля) — запросы не сверх общего лимита;
- сетевые запросы — вне транзакций; запись — короткими транзакциями db.tx() порциями;
- контрольные точки по каждому источнику событий; повторно читается окно перекрытия (реорганизации):
  исчезнувшее событие помечается orphaned (не удаляется), изменившийся хеш блока — обновляется;
- идентичность события — (chain_id, tx_hash, log_index): повторный проход не создаёт дублей;
- сбой RPC не трогает уже собранные данные (контрольная точка двигается только после записи порции).
"""
from __future__ import annotations

import time
from typing import Any

from . import config, db, fables as F
from .chains import CHAINS
from .http import SourceError
from .sources.evm_rpc import Rpc
from .util import dumps, loads, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS an_events (
    chain_id INTEGER NOT NULL, tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
    block INTEGER NOT NULL, block_hash TEXT, address TEXT NOT NULL, topic0 TEXT NOT NULL, event TEXT NOT NULL,
    args TEXT NOT NULL, raw_topics TEXT, raw_data TEXT, decode_version TEXT, source TEXT NOT NULL, fetched_at TEXT,
    orphaned INTEGER DEFAULT 0, PRIMARY KEY(chain_id, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS ix_an_events_ev ON an_events(event, block);
CREATE INDEX IF NOT EXISTS ix_an_events_tx ON an_events(tx_hash);
CREATE INDEX IF NOT EXISTS ix_an_events_src ON an_events(source, block);
CREATE TABLE IF NOT EXISTS an_checkpoints (key TEXT PRIMARY KEY, block INTEGER, block_hash TEXT, updated_at TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS an_epochs (
    chain_id INTEGER NOT NULL, week INTEGER NOT NULL, start_ts INTEGER NOT NULL, start_block INTEGER,
    start_evidence TEXT, method_version TEXT, updated_at TEXT, PRIMARY KEY(chain_id, week)
);
CREATE TABLE IF NOT EXISTS an_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL, block INTEGER, fetched_at TEXT, method_version TEXT, UNIQUE(subject, kind, payload_hash)
);
"""
_TABLES = ("an_events", "an_checkpoints", "an_epochs", "an_evidence")


def _pad(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


# Источники событий: адресный фильтр + подтверждённые сигнатуры
STREAMS: list[dict[str, Any]] = [
    {"key": "minter.Minted", "address": F.ADDR["minter"], "topics": [[F.EVENTS["Minted"]["topic0"]]]},
    {"key": "stream.events", "address": F.ADDR["stream"],
     "topics": [[F.EVENTS[n]["topic0"] for n in ("StreamClaimed", "RootPosted", "WeeksClosed", "WeeklyBudgetSet")]]},
    # исходящие FABLES контрактов, которые получают mint и сразу передают его дальше (points-блокировки, minter при TGE)
    {"key": "token.Transfer.forwarders", "address": F.TOKEN,
     "topics": [[F.EVENTS["Transfer"]["topic0"]], [_pad(F.ADDR["points_six_months"]), _pad(F.ADDR["points_auto_max"]), _pad(F.ADDR["minter"])]]},
]


class BudgetExhausted(Exception):
    pass


class LimitedRpc(Rpc):
    """RPC с собственным бюджетом задания (вызовы, время) и паузой между вызовами — поверх общего лимитера «rpc»."""

    def __init__(self, chain, max_calls: int, deadline: float, min_interval: float, stop=None):
        super().__init__(chain)
        self.max_calls, self.deadline, self.min_interval, self.stop = max_calls, deadline, min_interval, stop
        self.calls = 0
        self._last = 0.0

    def call(self, method: str, params: list[Any]) -> Any:
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"исчерпан лимит RPC-вызовов задания ({self.max_calls})")
        if time.monotonic() > self.deadline:
            raise BudgetExhausted("исчерпан лимит времени задания")
        if self.stop is not None and self.stop.is_set():
            raise BudgetExhausted("остановка программы")
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            (self.stop.wait if self.stop is not None else time.sleep)(wait)
        self.calls += 1
        self._last = time.monotonic()
        return super().call(method, params)


def settings() -> dict:
    s = (config.load_settings().get("analytics") or {})
    return {"enabled": bool(s.get("fables_enabled")), **(s.get("fables") or {})}


# ------------------------------------------------------------------ схема
def schema_present() -> bool:
    r = db.q1("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name IN (%s)" % ",".join("?" * len(_TABLES)), _TABLES)
    return bool(r and r["n"] == len(_TABLES))


def ensure_schema() -> None:
    if schema_present():
        return
    from . import analytics
    analytics.ensure_schema()
    with db.tx() as c:
        for chunk in SCHEMA.split(";"):
            stmt = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith("--")).strip()
            if stmt:
                c.execute(stmt)
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fables_schema','1')")


# ------------------------------------------------------------------ хранение
def checkpoint(key: str) -> dict | None:
    r = db.q1("SELECT * FROM an_checkpoints WHERE key=?", (key,))
    return dict(r) if r else None


def _set_checkpoint(c, key: str, block: int, block_hash: str | None = None, meta: dict | None = None) -> None:
    c.execute("INSERT INTO an_checkpoints(key,block,block_hash,updated_at,meta) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
              "block=excluded.block, block_hash=COALESCE(excluded.block_hash, an_checkpoints.block_hash), updated_at=excluded.updated_at, "
              "meta=COALESCE(excluded.meta, an_checkpoints.meta)", (key, block, block_hash, now_iso(), dumps(meta) if meta else None))


def decode_log(log: dict) -> tuple[str, dict] | None:
    addr = (log.get("address") or "").lower()
    t0 = (log.get("topics") or [None])[0]
    name = F.TOPIC_TO_EVENT.get((addr, t0))
    if not name:
        return None
    try:
        return name, F.decode(name, log)
    except (IndexError, ValueError, TypeError):
        return None


def write_logs(source: str, a: int, b: int, logs: list[dict], overlap_to: int) -> dict:
    """Записать события диапазона [a, b] одной короткой транзакцией. События того же источника в [a, min(b, overlap_to)],
    которых нет в повторном ответе, помечаются orphaned (реорганизация); вернувшиеся — снимают пометку."""
    rows = []
    for lg in logs:
        dec = decode_log(lg)
        if not dec:
            continue
        name, args = dec
        rows.append((F.CHAIN_ID, lg["transactionHash"].lower(), int(lg["logIndex"], 16), int(lg["blockNumber"], 16),
                     (lg.get("blockHash") or "").lower() or None, lg["address"].lower(), lg["topics"][0], name, dumps(args),
                     dumps(lg.get("topics")), lg.get("data"), F.DECODE_VERSION, source, now_iso()))
    st = {"rows": len(rows), "orphaned": 0, "rehashed": 0}
    with db.tx() as c:
        for i in range(0, len(rows), 500):
            c.executemany("INSERT OR IGNORE INTO an_events(chain_id,tx_hash,log_index,block,block_hash,address,topic0,event,args,raw_topics,"
                          "raw_data,decode_version,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows[i:i + 500])
        # тот же (tx, log) в другом блоке/с другим хешем блока — реорганизация: обновить место события
        for r in rows:
            cur = c.execute("UPDATE an_events SET block=?, block_hash=?, orphaned=0 WHERE chain_id=? AND tx_hash=? AND log_index=? "
                            "AND (block!=? OR COALESCE(block_hash,'')!=COALESCE(?,'') OR orphaned=1)",
                            (r[3], r[4], r[0], r[1], r[2], r[3], r[4]))
            st["rehashed"] += cur.rowcount
        hi = min(b, overlap_to)
        if a <= hi:
            got = {(r[1], r[2]) for r in rows}
            for x in c.execute("SELECT tx_hash, log_index FROM an_events WHERE source=? AND block BETWEEN ? AND ? AND orphaned=0",
                               (source, a, hi)).fetchall():
                if (x["tx_hash"], x["log_index"]) not in got:
                    c.execute("UPDATE an_events SET orphaned=1 WHERE chain_id=? AND tx_hash=? AND log_index=?",
                              (F.CHAIN_ID, x["tx_hash"], x["log_index"]))
                    st["orphaned"] += 1
        _set_checkpoint(c, f"logs:{source}", b)
    return st


def add_evidence(subject: str, kind: str, payload: dict, block: int | None) -> bool:
    """Доказательство добавляется только при изменении значения (история сохраняется)."""
    from .analytics import sha
    h = sha(payload)
    with db.tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO an_evidence(subject,kind,payload,payload_hash,block,fetched_at,method_version) VALUES(?,?,?,?,?,?,?)",
                        (subject, kind, dumps(payload), h, block, now_iso(), F.METHOD_VERSION))
        return cur.rowcount == 1


def latest_evidence(subject: str, kind: str) -> dict | None:
    r = db.q1("SELECT * FROM an_evidence WHERE subject=? AND kind=? ORDER BY id DESC LIMIT 1", (subject, kind))
    if not r:
        return None
    d = dict(r)
    d["payload"] = loads(d["payload"], {})
    return d


# ------------------------------------------------------------------ шаги задания (сеть — вне транзакций)
def index_stream(rpc: LimitedRpc, st: dict, head: int, overlap: int, max_range: int, prog=None) -> dict:
    cp = checkpoint(f"logs:{st['key']}")
    done_to = cp["block"] if cp else F.DEPLOY_BLOCK - 1
    a = max(F.DEPLOY_BLOCK, done_to - overlap + 1)
    step = int(((loads((cp or {}).get("meta"), {}) or {}).get("step")) or max_range)
    out = {"from": a, "to": None, "rows": 0, "orphaned": 0, "rehashed": 0, "calls": 0}
    while a <= head:
        b = min(head, a + step - 1)
        try:
            logs = rpc.call("eth_getLogs", [{"address": st["address"], "topics": st["topics"], "fromBlock": hex(a), "toBlock": hex(b)}]) or []
        except SourceError as e:
            msg = e.message.lower()
            if ("exceeds limit" in msg or "too many" in msg or "range" in msg) and step > 1:
                step = max(1, step // 2)  # плотная сеть: в 1000 блоках может быть > лимита событий
                continue
            raise
        out["calls"] += 1
        w = write_logs(st["key"], a, b, logs, overlap_to=done_to)
        for k in ("rows", "orphaned", "rehashed"):
            out[k] += w[k]
        out["to"] = b
        if prog:
            prog(f"FABLES {st['key']}: до блока {b:,}")
        a = b + 1
        if len(logs) < 2000 and step < max_range:
            step = min(max_range, step * 2)
    with db.tx() as c:
        _set_checkpoint(c, f"logs:{st['key']}", out["to"] if out["to"] is not None else done_to, meta={"step": step})
    return out


def index_epochs(rpc: LimitedRpc, head: int, head_ts: int) -> dict:
    """Точный первый блок каждой эпохи (timestamp блока ≥ начала, предыдущего — меньше). Эпохи — по контрактной формуле."""
    from .onchain import exact_block_at
    out = {"added": 0}
    w = F.FIRST_WEEK
    while F.week_start(w) <= head_ts:
        if not db.q1("SELECT 1 FROM an_epochs WHERE chain_id=? AND week=? AND start_block IS NOT NULL", (F.CHAIN_ID, w)):
            ts = F.week_start(w)
            b = exact_block_at(rpc, ts, head, head_ts)
            tb, tp = rpc.block_timestamp(b), rpc.block_timestamp(b - 1)
            if tb is None or tp is None or not (tp < ts <= tb):
                raise SourceError("rpc", "bad_response", f"граница эпохи {w} не подтверждена временем блоков")
            ev = {"block": b, "block_ts": tb, "prev_block_ts": tp, "rule": "первый блок с timestamp ≥ начала эпохи; предыдущий — раньше"}
            with db.tx() as c:
                c.execute("INSERT INTO an_epochs(chain_id,week,start_ts,start_block,start_evidence,method_version,updated_at) VALUES(?,?,?,?,?,?,?) "
                          "ON CONFLICT(chain_id,week) DO UPDATE SET start_block=excluded.start_block, start_evidence=excluded.start_evidence, "
                          "updated_at=excluded.updated_at", (F.CHAIN_ID, w, ts, b, dumps(ev), F.METHOD_VERSION, now_iso()))
            out["added"] += 1
        w += 1
    return out


def _u(h: str | None) -> int | None:
    return int(h, 16) if h and h != "0x" else None


def verify_contracts(rpc: LimitedRpc, head: int) -> dict:
    """Проверки реестра: код, тип адреса (контракт / EOA / EIP-7702), контрактные связи, состояние ключевых контрактов."""
    from .keccak import selector
    u256 = lambda i: format(int(i), "064x")
    call = lambda to, data: rpc.eth_call(to, data, hex(head))
    n = 0
    for c in F.CONTRACTS:
        code = rpc.call("eth_getCode", [c["address"], hex(head)]) or "0x"
        kind = "eoa" if code in ("0x", "") else ("eip7702_eoa" if code.lower().startswith("0xef0100") else "contract")
        n += add_evidence(c["key"], "code", {"address": c["address"], "chain_id": F.CHAIN_ID, "code_bytes": (len(code) - 2) // 2,
                                             "kind": kind, "expected_kind": c["kind"], "verified_source": "не проверено: обозреватель закрыт Cloudflare"}, head)
    tok_minter = rpc.address_call(F.TOKEN, selector("minter()")[2:])
    n += add_evidence("minter", "token.minter()", {"value": tok_minter, "matches": tok_minter == F.ADDR["minter"]}, head)
    M = F.ADDR["minter"]
    alloc = {}
    for i in range(10):
        alloc[i] = {"ceiling": str(_u(call(M, selector("ceilingOf(uint8)") + u256(i))) or 0),
                    "minted": str(_u(call(M, selector("mintedOf(uint8)") + u256(i))) or 0)}
    tot = _u(call(M, selector("totalMinted()")))
    tge = _u(call(M, selector("tge()")))
    n += add_evidence("minter", "allocations", {"allocations": alloc, "totalMinted": str(tot), "tge": tge}, head)
    pairs = [(0, "stream"), (1, "points_liquid"), (1, "points_six_months"), (1, "points_auto_max"), (2, "prologue_redeemer")]
    for r in db.q("SELECT DISTINCT json_extract(args,'$.allocation') a, json_extract(args,'$.by') b FROM an_events WHERE event='Minted' AND orphaned=0"):
        key = F.BY_ADDR.get(r["b"], {}).get("key") or r["b"]
        if (int(r["a"]), key) not in pairs:
            pairs.append((int(r["a"]), key))
    rel = {}
    for al, key in pairs:
        addr = F.ADDR.get(key, key)
        v = call(M, selector("isMinter(uint8,address)") + u256(al) + addr.lower().replace("0x", "").rjust(64, "0"))
        rel[f"{al}:{key}"] = (_u(v) == 1) if v else None
    n += add_evidence("minter", "isMinter", rel, head)
    S = F.ADDR["stream"]
    n += add_evidence("stream", "state", {"startAt": _u(call(S, selector("startAt()"))), "postedTotal": str(_u(call(S, selector("postedTotal()")))),
                                          "closedTotal": str(_u(call(S, selector("closedTotal()")))), "rootCount": _u(call(S, selector("rootCount()")))}, head)
    E = F.ADDR["escrow"]
    lock1 = call(E, selector("locks(uint256)") + u256(1)) or "0x"
    words = [int(lock1[2 + 64 * k: 2 + 64 * (k + 1)], 16) for k in range((len(lock1) - 2) // 64)]
    owner1 = rpc.address_call(E, (selector("ownerOf(uint256)") + u256(1))[2:])
    perm = _u(call(E, selector("PERMANENT()")))
    n += add_evidence("escrow", "state", {"PERMANENT": str(perm), "MAX_DURATION_s": _u(call(E, selector("MAX_DURATION()"))),
                                          "totalLocked": str(_u(call(E, selector("totalLocked()")))), "nextTokenId": _u(call(E, selector("nextTokenId()"))),
                                          "lock1_words": [str(x) for x in words], "lock1_owner": owner1}, head)
    ep = {}
    for w in (F.FIRST_WEEK, F.FIRST_WEEK + 1, F.FIRST_WEEK + 2):
        t = F.week_start(w)
        ep[str(w)] = {"epochStart(t)": _u(call(E, selector("epochStart(uint256)") + u256(t))),
                      "epochStart(t-1)": _u(call(E, selector("epochStart(uint256)") + u256(t - 1))), "t": t}
    n += add_evidence("escrow", "epochStart", ep, head)
    bal = {}
    for key in ("points_six_months", "points_auto_max", "minter", "stream"):
        bal[key] = str(_u(call(F.TOKEN, selector("balanceOf(address)") + F.ADDR[key].replace("0x", "").rjust(64, "0"))) or 0)
    n += add_evidence("token", "balances", bal, head)
    n += add_evidence("voter", "currentEpoch", {"value": _u(call(F.ADDR["voter"], selector("currentEpoch()")))}, head)
    with db.tx() as c:
        _set_checkpoint(c, "verify", head)
    return {"new_evidence": n}


def investigate_team_lock(rpc: LimitedRpc) -> dict:
    """Цепочка 40M команды: транзакция mint (allocation 3) → её полные события (receipt) → события Lock #1 после неё."""
    r = db.q1("SELECT tx_hash, block FROM an_events WHERE event='Minted' AND json_extract(args,'$.allocation')='3' AND orphaned=0 ORDER BY block LIMIT 1")
    if not r:
        return {"status": "нет события Minted(allocation=3) в индексе"}
    if latest_evidence("team_lock", "creation_receipt"):
        rec_done = True
    else:
        rec = rpc.call("eth_getTransactionReceipt", [r["tx_hash"]]) or {}
        tx = rpc.call("eth_getTransactionByHash", [r["tx_hash"]]) or {}
        add_evidence("team_lock", "creation_receipt", {"tx": r["tx_hash"], "block": r["block"], "from": (tx.get("from") or "").lower(),
                                                       "to": (tx.get("to") or "").lower(), "selector": (tx.get("input") or "")[:10],
                                                       "status": rec.get("status"),
                                                       "logs": [{"i": int(x["logIndex"], 16), "address": x["address"].lower(), "topics": x["topics"],
                                                                 "data": x["data"]} for x in rec.get("logs") or []]}, r["block"])
        rec_done = False
    # все события escrow по Lock #1 после создания: Locked/AmountIncreased/DepositedFor/UnlockExtended/Withdrawn/Merged/Split; ERC-721 Transfer
    from .keccak import topic
    sigs = ["Locked(uint256,address,uint128,uint64)", "AmountIncreased(uint256,uint128)", "DepositedFor(uint256,address,uint128)",
            "UnlockExtended(uint256,uint64)", "Withdrawn(uint256,address,uint128)", "Merged(uint256,uint256,uint128)", "Split(uint256,uint256,uint128)"]
    head = rpc.block_number()
    logs = rpc.call("eth_getLogs", [{"address": F.ADDR["escrow"], "topics": [[topic(s) for s in sigs], _pad("0x1")],
                                     "fromBlock": hex(r["block"]), "toBlock": hex(head)}]) or []
    nft = rpc.call("eth_getLogs", [{"address": F.ADDR["escrow"], "topics": [topic("Transfer(address,address,uint256)"), None, None, _pad("0x1")],
                                    "fromBlock": hex(r["block"]), "toBlock": hex(head)}]) or []
    names = {topic(s): s.split("(")[0] for s in sigs}
    hist = [{"event": names.get(x["topics"][0], "?"), "block": int(x["blockNumber"], 16), "tx": x["transactionHash"], "data": x["data"]} for x in logs]
    add_evidence("team_lock", "lock1_events", {"through_block": head, "events": hist,
                                               "nft_transfers": [{"block": int(x["blockNumber"], 16), "tx": x["transactionHash"],
                                                                  "from": "0x" + x["topics"][1][-40:], "to": "0x" + x["topics"][2][-40:]} for x in nft]}, head)
    return {"receipt_cached": rec_done, "lock1_events": len(hist), "nft_transfers": len(nft)}


# ------------------------------------------------------------------ задание
def run(prog=None, stop=None, budget_calls: int | None = None, budget_seconds: float | None = None) -> dict:
    s = settings()
    ensure_schema()
    ch = CHAINS[F.CHAIN]
    rpc = LimitedRpc(ch, int(budget_calls or s.get("rpc_calls_per_job", 40)),
                     time.monotonic() + float(budget_seconds or s.get("job_seconds", 60)),
                     60.0 / max(1.0, float(s.get("rpc_per_min", 30))), stop)
    out: dict[str, Any] = {"steps": {}, "stopped": None}
    try:
        head = rpc.block_number()
        head_ts = rpc.block_timestamp(head) or int(time.time())
        out["head"] = head
        overlap, max_range = int(s.get("reorg_overlap_blocks", 20000)), int(s.get("max_log_range", 1_000_000))
        for st in STREAMS:
            out["steps"][st["key"]] = index_stream(rpc, st, head, overlap, max_range, prog)
        out["steps"]["epochs"] = index_epochs(rpc, head, head_ts)
        vcp = checkpoint("verify")
        if not vcp or head - (vcp["block"] or 0) > int(s.get("verify_every_blocks", 400_000)):
            out["steps"]["verify"] = verify_contracts(rpc, head)
        out["steps"]["team_lock"] = investigate_team_lock(rpc)
        with db.tx() as c:
            _set_checkpoint(c, "head", head, meta={"head_ts": head_ts})
    except BudgetExhausted as e:
        out["stopped"] = str(e) + " — продолжится в следующем задании"
    out["rpc_calls"] = rpc.calls
    return out


def status() -> dict:
    if not schema_present():
        return {"schema_present": False}
    cps = {r["key"]: dict(r) for r in db.q("SELECT * FROM an_checkpoints")}
    head = (cps.get("head") or {}).get("block")
    return {"schema_present": True, "head": head, "checkpoints": cps,
            "events": {r["event"]: r["n"] for r in db.q("SELECT event, COUNT(*) n FROM an_events WHERE orphaned=0 GROUP BY event")},
            "orphaned": db.q1("SELECT COUNT(*) n FROM an_events WHERE orphaned=1")["n"],
            "epochs": db.q1("SELECT COUNT(*) n FROM an_epochs WHERE start_block IS NOT NULL")["n"]}
