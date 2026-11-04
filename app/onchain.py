"""TOKENOMICS WATCHDOG (ончейн-часть) и модуль безопасности.

EVM:
- totalSupply() сейчас; события Transfer от/к нулевому адресу (mint/burn) с возобновлением по блокам;
- восстановление истории предложения из событий и сверка с totalSupply() (если не сходится —
  стандартная модель событий НЕ подтверждена, это показывается явно);
- сравнение фактических mint с заявленным бюджетом эмиссии (бюджет ≠ выпущенное количество);
- индикаторы безопасности по байткоду (селекторы функций), слоты EIP-1967, owner()/minter().
Solana: getTokenSupply, mintAuthority/freezeAuthority.
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections import defaultdict
from typing import Any, Callable

from . import db
from .chains import CHAINS, Chain
from .enrich import current_tokenomics, get_facts, save_facts, compute_caps
from .http import SourceError
from .sources.evm_rpc import EIP1967_ADMIN, EIP1967_IMPL, SELECTORS, Rpc, bytecode_flags, is_minimal_proxy
from .sources.solana_rpc import SolanaRpc
from .util import dumps, from_ts, iso, loads, now_iso, now_utc, parse_iso

log = logging.getLogger("das.onchain")
Progress = Callable[[str], None]


def find_block_by_time(rpc: Rpc, ts: int, head: int, head_ts: int, max_iter: int = 8) -> int:
    """Блок не позже момента ts. Метод секущих по заголовкам блоков (архив не нужен), ~4–8 запросов."""
    lo_b, lo_t = max(1, head - 1_000_000), None
    lo_t = rpc.block_timestamp(lo_b)
    hi_b, hi_t = head, head_ts
    if lo_t is None:
        return lo_b
    # расширяем нижнюю опору, пока она не окажется раньше ts
    while lo_t > ts and lo_b > 1 and max_iter > 0:
        rate = (hi_t - lo_t) / max(1, hi_b - lo_b) or 1.0
        nb = max(1, int(lo_b - (lo_t - ts) / rate * 1.1) - 1000)
        hi_b, hi_t = lo_b, lo_t
        lo_b, lo_t = nb, rpc.block_timestamp(nb) or 0
        max_iter -= 1
    for _ in range(max_iter):
        if hi_b - lo_b <= 1000:
            break
        rate = (hi_t - lo_t) / max(1, hi_b - lo_b) or 1.0
        mid = int(lo_b + (ts - lo_t) / rate)
        mid = min(max(mid, lo_b + 1), hi_b - 1)
        t = rpc.block_timestamp(mid)
        if t is None:
            break
        if t <= ts:
            lo_b, lo_t = mid, t
        else:
            hi_b, hi_t = mid, t
    return lo_b


def _labels(chain: str) -> dict[str, dict]:
    return {r["address"]: dict(r) for r in db.q("SELECT * FROM address_labels WHERE chain=?", (chain,))}


def scan_supply_events(uid: str, max_calls: int = 40, progress: Progress | None = None, max_seconds: float = 240) -> dict:
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    chain = CHAINS[tok["chain"]]
    if chain.kind != "evm":
        return {"error": "история mint для этой сети не реализована"}
    import time as _time
    deadline = _time.monotonic() + max_seconds
    rpc = Rpc(chain)
    addr = tok["address"]
    head = rpc.block_number()
    head_ts = rpc.block_timestamp(head) or int(now_utc().timestamp())
    decimals = tok["decimals"]
    if decimals is None:
        decimals = rpc.decimals(addr)
        if decimals is not None:
            db.ex("UPDATE tokens SET decimals=? WHERE uid=?", (decimals, uid))
    dec = 10 ** (decimals if decimals is not None else 18)
    pkey_to, pkey_from = f"mint_scan_to:{uid}", f"mint_scan_from:{uid}"
    scanned_to = int(db.get_progress(pkey_to) or 0)
    scanned_from = int(db.get_progress(pkey_from) or 0)
    calls = 0
    if not scanned_to:
        f = get_facts(uid)
        ref = parse_iso((f.get("earliest_pool") or {}).get("at")) or parse_iso(tok["first_seen_at"]) or now_utc()
        start_ts = int((ref - _dt.timedelta(days=14)).timestamp())
        start = find_block_by_time(rpc, start_ts, head, head_ts)
        scanned_from = scanned_to = start
        db.set_progress(pkey_from, str(start))
        db.set_progress(pkey_to, str(start))
    step = chain.max_log_range
    errors = []

    def fetch(a: int, b: int) -> int:
        nonlocal calls, step
        n = 0
        while a <= b and calls < max_calls and _time.monotonic() < deadline:
            e = min(b, a + step - 1)
            try:
                mints = rpc.transfer_logs(addr, a, e, mint=True)
                burns = rpc.transfer_logs(addr, a, e, mint=False)
                calls += 2
            except SourceError as ex_:
                calls += 1
                if "too many requests" in ex_.message.lower() or "429" in ex_.message:
                    import time as _t
                    _t.sleep(6)
                    continue
                if step > 1000 and ("range" in ex_.message.lower() or "limit" in ex_.message.lower() or "too many" in ex_.message.lower() or ex_.kind == "unavailable"):
                    step = max(1000, step // 4)
                    continue
                errors.append(str(ex_))
                break
            for kind, logs in (("mint", mints), ("burn", burns)):
                for lg in logs:
                    topics = lg.get("topics") or []
                    if len(topics) < 3:
                        continue  # нестандартное событие — не интерпретируем
                    cp = "0x" + (topics[2] if kind == "mint" else topics[1])[-40:]
                    amount = int(lg.get("data") or "0x0", 16)
                    db.get_conn().execute(
                        "INSERT OR IGNORE INTO supply_events(token_uid,kind,tx_hash,log_index,block,counterparty,amount_raw,amount) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (uid, kind, lg["transactionHash"], int(lg["logIndex"], 16), int(lg["blockNumber"], 16), cp,
                         str(amount), amount / dec))
                    n += 1
            db.get_conn().commit()
            a = e + 1
        return a

    # вперёд до головы
    nxt = fetch(scanned_to, head)
    db.set_progress(pkey_to, str(nxt))
    if progress:
        progress(f"ончейн {tok['symbol']}: блоки до {nxt} из {head}")
    # временные метки: кусочно-линейная интерполяция по опорным блокам (≤ 12 запросов на все события)
    rng = db.q1("SELECT MIN(block) lo, MAX(block) hi, COUNT(*) n FROM supply_events WHERE token_uid=? AND ts IS NULL", (uid,))
    if rng and rng["n"]:
        lo, hi = rng["lo"], rng["hi"]
        k = 10 if hi - lo > 100_000 else 2
        anchors = sorted({lo, hi} | {lo + (hi - lo) * i // k for i in range(1, k)})
        refs = []
        for bnum in anchors:
            try:
                t = rpc.block_timestamp(bnum)
                if t:
                    refs.append((bnum, t))
            except SourceError:
                pass
        if refs:
            if len(refs) == 1:
                refs.append((head, head_ts))
            blocks = [r["block"] for r in db.q("SELECT DISTINCT block FROM supply_events WHERE token_uid=? AND ts IS NULL", (uid,))]
            c = db.get_conn()
            for bnum in blocks:
                j = 0
                while j < len(refs) - 2 and refs[j + 1][0] < bnum:
                    j += 1
                (b0, t0), (b1, t1) = refs[j], refs[j + 1]
                t = t0 + (bnum - b0) * ((t1 - t0) / (b1 - b0) if b1 != b0 else 0)
                c.execute("UPDATE supply_events SET ts=? WHERE token_uid=? AND block=?", (iso(from_ts(t)), uid, bnum))
            c.commit()
    total_raw = rpc.total_supply_raw(addr)
    total = total_raw / dec if total_raw is not None else None
    if nxt > head and total is not None:
        # догоняем блоки, появившиеся за время сканирования (блоки ~0.1 с), сразу после чтения totalSupply
        try:
            head2 = rpc.block_number()
            if head2 >= nxt:
                calls_left = max_calls - calls
                if calls_left < 2:
                    calls -= 2  # гарантируем один короткий догоняющий запрос
                nxt = fetch(nxt, head2)
                db.set_progress(pkey_to, str(nxt))
                head = head2
        except SourceError:
            pass
    if total is not None:
        db.observe("token", uid, "total_supply_rpc", total, source=f"rpc:{chain.key}", unit="tokens",
                   confidence="confirmed", method="eth_call totalSupply()", verify_url=chain.explorer_token(addr))
    agg = db.q1("SELECT SUM(CASE WHEN kind='mint' THEN amount ELSE 0 END) m, SUM(CASE WHEN kind='burn' THEN amount ELSE 0 END) b, "
                "COUNT(*) n, MIN(block) fb FROM supply_events WHERE token_uid=?", (uid,))
    minted, burned = agg["m"] or 0.0, agg["b"] or 0.0
    reconstructed = minted - burned
    recon = None
    if total is not None and nxt <= head:
        recon = {"total_supply": total, "reconstructed": reconstructed, "diff": total - reconstructed, "matches": None,
                 "note": f"сканирование не завершено (обработано до блока {nxt:,} из {head:,}) — сверка будет выполнена после "
                         "завершения; продолжится при следующем обновлении"}
    elif total is not None:
        diff = total - reconstructed
        tol = max(1e-5 * total, 1e-9)  # допуск 0.001%: гонка между чтением totalSupply и последним блоком логов
        recon = {"total_supply": total, "reconstructed": reconstructed, "diff": diff, "tolerance": tol,
                 "matches": abs(diff) <= tol,
                 "note": ("история предложения сходится с totalSupply() — стандартная модель Transfer подтверждена"
                          if abs(diff) <= tol else
                          f"НЕ сходится на {diff:,.2f}: часть mint раньше начала сканирования (блок {scanned_from}) "
                          "или контракт использует нестандартную модель событий")}
    first = db.q1("SELECT * FROM supply_events WHERE token_uid=? AND kind='mint' ORDER BY block, log_index LIMIT 1", (uid,))
    out = {"scanned_from_block": scanned_from, "scanned_to_block": nxt, "head": head, "complete_to_head": nxt > head,
           "events": agg["n"], "minted": minted, "burned": burned, "total_supply": total,
           "reconciliation": recon, "errors": errors, "at": now_iso(),
           "first_mint": {"block": first["block"], "ts": first["ts"], "tx": first["tx_hash"]} if first and recon and recon.get("matches") else None,
           "first_mint_note": None if (recon and recon.get("matches")) else "первый mint не установлен: сканирование не дошло до начала"}
    # рекурсивно дотягиваем начало, если не сходится и есть бюджет
    if recon and recon["matches"] is False and recon["diff"] > 0 and calls < max_calls and scanned_from > 1:
        new_from = max(1, scanned_from - step * max(1, (max_calls - calls) // 2))
        got_to = fetch(new_from, scanned_from - 1)
        if got_to >= scanned_from:
            db.set_progress(pkey_from, str(new_from))
            out["scanned_from_block"] = new_from
            out["note"] = "диапазон сканирования расширен назад; сверка будет пересчитана при следующем запуске"
    return out


def supply_analytics(uid: str) -> dict:
    """Дневная эмиссия, получатели с метками, сравнение с бюджетом текущей версии токеномики."""
    tok = db.q1("SELECT chain FROM tokens WHERE uid=?", (uid,))
    labels = _labels(tok["chain"])
    rows = db.rows_to_dicts(db.q("SELECT kind, ts, counterparty, amount, tx_hash, block FROM supply_events WHERE token_uid=? ORDER BY block", (uid,)))
    daily: dict[str, dict] = defaultdict(lambda: {"mint": 0.0, "burn": 0.0})
    recips: dict[str, float] = defaultdict(float)
    for r in rows:
        d = (r["ts"] or "")[:10] or "?"
        daily[d][r["kind"]] += r["amount"] or 0
        if r["kind"] == "mint":
            recips[r["counterparty"]] += r["amount"] or 0
    cum = 0.0
    series = []
    for d in sorted(k for k in daily if k != "?"):
        cum += daily[d]["mint"] - daily[d]["burn"]
        series.append({"date": d, "mint": daily[d]["mint"], "burn": daily[d]["burn"], "cumulative": cum})
    top = sorted(recips.items(), key=lambda x: -x[1])[:20]
    top_l = [{"address": a, "amount": v, "label": (labels.get(a) or {}).get("label"),
              "kind": (labels.get(a) or {}).get("kind"), "label_source": (labels.get(a) or {}).get("source_url"),
              "note": None if labels.get(a) else "неизвестный адрес — не считать командой без доказательств"} for a, v in top]
    # бюджет: params.weekly_budgets = [{"week": 1, "start": "...", "amount": ...}, ...]
    tn = current_tokenomics(uid)
    budget_cmp = []
    if tn and tn["params"].get("weekly_budgets"):
        for w in tn["params"]["weekly_budgets"]:
            st = parse_iso(w.get("start"))
            if not st:
                continue
            en = st + _dt.timedelta(days=7)
            emis_kinds = set(tn["params"].get("emission_recipient_kinds") or ["gauge", "emissions", "minter"])
            minted_all = sum(r["amount"] or 0 for r in rows if r["kind"] == "mint" and r["ts"] and st <= parse_iso(r["ts"]) < en)
            minted_emis = sum(r["amount"] or 0 for r in rows if r["kind"] == "mint" and r["ts"] and st <= parse_iso(r["ts"]) < en
                              and (labels.get(r["counterparty"]) or {}).get("kind") in emis_kinds)
            in_progress = en > now_utc()
            has_labels = any((lb.get("kind") in emis_kinds) for lb in labels.values())
            budget_cmp.append({"week": w.get("week"), "start": w.get("start"), "budget": w.get("amount"),
                               "minted_all": minted_all, "minted_to_emission_contracts": minted_emis,
                               "in_progress": in_progress,
                               "over_budget": (None if not has_labels else
                                               (not in_progress) and w.get("amount") is not None and minted_emis > 1.1 * float(w["amount"])),
                               "labels_missing": not has_labels,
                               "note": "бюджет — потолок эмиссии, а не выпущенное количество; "
                                       "minted_all включает все mint (команда, ликвидность, клеймы)"})
    return {"daily": series[-180:], "top_recipients": top_l, "budget_comparison": budget_cmp,
            "tokenomics_version": {k: tn.get(k) for k in ("id", "version_label", "published_at", "source_url", "source_type")} if tn else None}


def security_check(uid: str) -> dict:
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    chain = CHAINS[tok["chain"]]
    addr = tok["address"]
    out: dict[str, Any] = {"at": now_iso(), "indicators": [], "unknown": [], "chain": chain.key}

    def ind(key, status, text, src="rpc"):
        out["indicators"].append({"key": key, "status": status, "text": text, "source": src})

    if chain.kind == "solana":
        try:
            info = SolanaRpc(chain).mint_info(addr)
        except SourceError as e:
            out["error"] = str(e)
            return out
        if not info:
            out["error"] = "mint-аккаунт не найден"
            return out
        ind("mint_authority", "risk" if info["mint_authority"] else "ok",
            f"mintAuthority: {info['mint_authority'] or 'отсутствует (выпуск невозможен)'}")
        ind("freeze_authority", "risk" if info["freeze_authority"] else "ok",
            f"freezeAuthority: {info['freeze_authority'] or 'отсутствует'}")
        ind("program", "info", f"программа токена: {info['program']}")
        out["unknown"] += ["верификация программ/аудит — не проверяется автоматически"]
        return out
    try:
        rpc = Rpc(chain)
        code = rpc.code(addr)
    except SourceError as e:
        out["error"] = str(e)
        return out
    if code in ("0x", "", None):
        ind("no_code", "risk", "по адресу нет кода контракта")
        return out
    out["bytecode_size"] = (len(code) - 2) // 2
    impl = None
    try:
        slot = rpc.storage(addr, EIP1967_IMPL)
        if slot and int(slot, 16):
            impl = "0x" + slot[-40:]
            ind("proxy", "risk", f"EIP-1967 прокси: реализация {impl} — контракт обновляемый")
        adm = rpc.storage(addr, EIP1967_ADMIN)
        if adm and int(adm, 16):
            ind("proxy_admin", "info", f"админ прокси: 0x{adm[-40:]}")
    except SourceError as e:
        out["unknown"].append(f"слоты прокси: {e}")
    mp = is_minimal_proxy(code)
    if mp:
        impl = mp
        ind("minimal_proxy", "info", f"EIP-1167 клон, реализация {mp}")
    analyzed = code
    if impl:
        try:
            analyzed = rpc.code(impl)
            out["implementation"] = impl
        except SourceError:
            out["unknown"].append("байткод реализации прокси недоступен")
    flags = bytecode_flags(analyzed)
    out["selectors"] = flags
    if flags["mint(address,uint256)"] or flags["mint(uint256)"]:
        ind("mint", "risk", "есть функция mint — кто может выпускать, нужно проверить (минтер/владелец)")
    if flags["pause()"]:
        ind("pause", "risk", "есть pause() — переводы могут быть остановлены")
    if flags["blacklist(address)"] or flags["addBlackList(address)"] or flags["setBlacklist(address,bool)"]:
        ind("blacklist", "risk", "есть функция чёрного списка")
    if flags["upgradeTo(address)"] or flags["upgradeToAndCall(address,bytes)"]:
        ind("upgrade", "risk", "есть функции обновления (UUPS)")
    if flags["grantRole(bytes32,address)"]:
        ind("roles", "info", "используется AccessControl (роли) — состав ролей не проверен")
    if flags["setFee/setTax(uint256)"]:
        ind("tax", "risk", "возможна настраиваемая комиссия перевода")
    for name, sel in (("owner", "8da5cb5b"), ("minter", "07546172")):
        if flags.get(f"{name}()"):
            try:
                a = rpc.address_call(addr, sel)
            except SourceError:
                a = None
            if a:
                out[name] = a
                if int(a, 16) == 0:
                    ind(name, "ok", f"{name}() = нулевой адрес (права сняты)")
                else:
                    try:
                        is_contract = rpc.code(a) not in ("0x", "")
                    except SourceError:
                        is_contract = None
                    ind(name, "info" if is_contract else "risk",
                        f"{name}() = {a} ({'контракт: мультисиг/таймлок/минтер — проверить' if is_contract else 'EOA — единоличный контроль' if is_contract is False else 'тип не установлен'})")
    out["unknown"] += ["верификация исходного кода (нужен API обозревателя; Blockscout закрыт Cloudflare, Etherscan — ключ)",
                       "наличие аудита (см. DefiLlama audits в фундаментале)",
                       "селекторы в байткоде — эвристика: функция может быть ограничена правами или отсутствовать при нестандартной компиляции"]
    out["note"] = "Индикаторы для ручной проверки, не заключение об уязвимости. Отсутствие флагов НЕ означает безопасность."
    return out


def onchain_update(uid: str, progress: Progress | None = None, max_calls: int = 40, max_seconds: float = 240) -> dict:
    tok = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
    chain = CHAINS[tok["chain"]]
    f = get_facts(uid)
    oc = f.get("onchain") or {}
    if chain.kind == "evm" and not chain.logs_ok:
        oc["error"] = f"история mint недоступна: {chain.logs_note}"
    elif chain.kind == "evm":
        try:
            sc = scan_supply_events(uid, max_calls=max_calls, progress=progress, max_seconds=max_seconds)
            oc.update(sc)
            oc["analytics"] = supply_analytics(uid)
        except SourceError as e:
            oc["error"] = str(e)
    else:
        try:
            sup = SolanaRpc(chain).token_supply(tok["address"])
            if sup:
                oc["total_supply"] = sup["ui"]
                db.observe("token", uid, "total_supply_rpc", sup["ui"], source=f"rpc:{chain.key}", unit="tokens",
                           confidence="confirmed", method="getTokenSupply")
        except SourceError as e:
            oc["error"] = str(e)
    oc["security"] = security_check(uid)
    f["onchain"] = oc
    compute_caps(uid, f)
    save_facts(uid, f, "last_onchain_at")
    return oc
