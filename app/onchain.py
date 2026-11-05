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
from decimal import Decimal, getcontext
from typing import Any, Callable

from . import db
from .chains import CHAINS, Chain
from .enrich import current_tokenomics, get_facts, save_facts, compute_caps
from .http import SourceError
from .sources.evm_rpc import EIP1967_ADMIN, EIP1967_IMPL, SELECTORS, Rpc, bytecode_flags, is_minimal_proxy
from .sources.solana_rpc import SolanaRpc
from .util import dumps, from_ts, iso, loads, now_iso, now_utc, parse_iso

log = logging.getLogger("das.onchain")
getcontext().prec = 80  # достаточно для uint256
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
    # инкрементально: только новые блоки + перекрытие для контроля реорганизаций/пропусков
    from . import config as _cfg
    overlap = int(_cfg.load_settings().get("reorg_overlap_blocks", 2000))
    prev_to = scanned_to
    rescan_from = max(scanned_from, scanned_to - overlap) if scanned_to > scanned_from else scanned_to
    seen_overlap: set[tuple[str, int]] = set()

    def fetch(a: int, b: int, force: bool = False) -> int:
        nonlocal calls, step
        n = 0
        while a <= b and (force or (calls < max_calls and _time.monotonic() < deadline)):
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
                    data = lg.get("data") or "0x"
                    if len(data) != 66:
                        continue  # нестандартный Transfer — не интерпретируем
                    amount = int(data, 16)  # целые минимальные единицы
                    if int(lg["blockNumber"], 16) < prev_to:
                        seen_overlap.add((lg["transactionHash"], int(lg["logIndex"], 16)))
                    db.get_conn().execute(
                        "INSERT OR IGNORE INTO supply_events(token_uid,kind,tx_hash,log_index,block,counterparty,amount_raw,amount) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (uid, kind, lg["transactionHash"], int(lg["logIndex"], 16), int(lg["blockNumber"], 16), cp,
                         str(amount), float(to_decimal(amount, decimals))))  # amount — только для отображения
                    n += 1
            db.get_conn().commit()
            a = e + 1
        return a

    # вперёд до головы (с перекрытием)
    nxt = fetch(rescan_from, head)
    if nxt >= prev_to and prev_to > rescan_from:
        # события в перекрытии, которых больше нет в каноничной цепи, помечаются orphaned (не удаляются)
        c = db.get_conn()
        rows_ov = c.execute("SELECT tx_hash, log_index FROM supply_events WHERE token_uid=? AND block>=? AND block<? AND orphaned=0",
                            (uid, rescan_from, prev_to)).fetchall()
        orphans = [(t, li) for t, li in rows_ov if (t, li) not in seen_overlap]
        for t, li in orphans:
            c.execute("UPDATE supply_events SET orphaned=1 WHERE token_uid=? AND tx_hash=? AND log_index=?", (uid, t, li))
        for t, li in c.execute("SELECT tx_hash, log_index FROM supply_events WHERE token_uid=? AND orphaned=1 AND block>=? AND block<?",
                               (uid, rescan_from, prev_to)).fetchall():
            if (t, li) in seen_overlap:  # событие снова в каноничной цепи
                c.execute("UPDATE supply_events SET orphaned=0 WHERE token_uid=? AND tx_hash=? AND log_index=?", (uid, t, li))
        c.commit()
        if orphans:
            errors.append(f"реорганизация: {len(orphans)} событий в перекрытии больше не подтверждаются — помечены orphaned")
    db.set_progress(pkey_to, str(max(nxt, prev_to)))
    if progress:
        progress(f"ончейн {tok['symbol']}: блоки до {nxt} из {head}")
    # totalSupply() читается НА ТОМ ЖЕ БЛОКЕ, до которого получены логи — без гонки с новыми блоками
    total_raw = None
    total_block = None
    supply_method = None
    if nxt > head:
        try:
            head2 = rpc.block_number()
            if head2 >= nxt:
                nxt = fetch(nxt, head2, force=True)
                db.set_progress(pkey_to, str(nxt))
            head = nxt - 1
            total_raw = rpc.total_supply_raw(addr, block=hex(head))
            total_block, supply_method = head, f"eth_call totalSupply() на блоке {head}"
        except SourceError:
            total_raw = None
    if total_raw is None:
        try:
            total_raw = rpc.total_supply_raw(addr)
            supply_method = "eth_call totalSupply() на latest (блок логов может не совпадать)"
        except SourceError as e:
            errors.append(str(e))
    head_ts = rpc.block_timestamp(head) or head_ts
    _timestamps(rpc, uid, head, head_ts)
    _week_boundaries(rpc, uid, head, head_ts)
    total = to_decimal(total_raw, decimals) if total_raw is not None else None
    if total is not None:
        db.observe("token", uid, "total_supply_rpc", float(total), source=f"rpc:{chain.key}", unit="tokens",
                   confidence="confirmed", method=supply_method, verify_url=chain.explorer_token(addr))
    tot = raw_totals(uid)
    reconstructed_raw = tot["minted_raw"] - tot["burned_raw"]
    recon = None
    if total_raw is not None and nxt <= head:
        recon = {"total_supply_raw": str(total_raw), "reconstructed_raw": str(reconstructed_raw), "matches": None,
                 "total_supply": float(total), "reconstructed": float(to_decimal(reconstructed_raw, decimals)),
                 "note": f"сканирование не завершено (обработано до блока {nxt:,} из {head:,}) — сверка будет выполнена после "
                         "завершения; продолжится при следующем обновлении"}
    elif total_raw is not None:
        rc = reconcile(total_raw, reconstructed_raw)
        recon = {"total_supply_raw": str(total_raw), "reconstructed_raw": str(reconstructed_raw), "diff_raw": str(rc["diff_raw"]),
                 "matches": rc["exact"], "at_block": total_block, "method": supply_method,
                 "total_supply": float(total), "reconstructed": float(to_decimal(reconstructed_raw, decimals)),
                 "diff": float(to_decimal(rc["diff_raw"], decimals)),
                 "note": (f"точное совпадение в минимальных единицах: Σmint − Σburn = totalSupply() = {total_raw}"
                          + (f" (на блоке {total_block})" if total_block else "")
                          if rc["exact"] else
                          f"НЕ сходится на {rc['diff_raw']} мин. единиц ({to_decimal(rc['diff_raw'], decimals)} токенов): "
                          f"часть mint раньше начала сканирования (блок {scanned_from}), гонка чтения на latest "
                          "или нестандартная модель событий")}
    first = db.q1("SELECT * FROM supply_events WHERE token_uid=? AND kind='mint' ORDER BY block, log_index LIMIT 1", (uid,))
    first_info = None
    if first and recon and recon.get("matches"):
        ts_exact = rpc.block_timestamp(first["block"])
        if ts_exact:
            db.ex("UPDATE supply_events SET ts=?, ts_source='block' WHERE token_uid=? AND block=?",
                  (iso(from_ts(ts_exact)), uid, first["block"]))
        first_info = {"block": first["block"], "ts": iso(from_ts(ts_exact)) if ts_exact else first["ts"],
                      "ts_source": "block" if ts_exact else first["ts_source"], "tx": first["tx_hash"]}
    out = {"scanned_from_block": scanned_from, "scanned_to_block": nxt, "head": head, "complete_to_head": nxt > head,
           "events": tot["events"], "minted_raw": str(tot["minted_raw"]), "burned_raw": str(tot["burned_raw"]),
           "minted": float(to_decimal(tot["minted_raw"], decimals)), "burned": float(to_decimal(tot["burned_raw"], decimals)),
           "decimals": decimals, "total_supply": float(total) if total is not None else None,
           "total_supply_raw": str(total_raw) if total_raw is not None else None,
           "reconciliation": recon, "errors": errors, "at": now_iso(),
           "first_mint": first_info,
           "first_mint_note": None if first_info else "первый mint не установлен: сканирование не дошло до начала"}
    # дотягиваем начало, если не сходится (минтов не хватает) и есть бюджет
    if recon and recon["matches"] is False and int(recon["diff_raw"]) > 0 and calls < max_calls and scanned_from > 1:
        new_from = max(1, scanned_from - step * max(1, (max_calls - calls) // 2))
        got_to = fetch(new_from, scanned_from - 1)
        if got_to >= scanned_from:
            db.set_progress(pkey_from, str(new_from))
            out["scanned_from_block"] = new_from
            out["note"] = "диапазон сканирования расширен назад; сверка будет пересчитана при следующем запуске"
    return out


# ------------------------------------------------------------------ целочисленная арифметика
def to_decimal(raw: int | None, decimals: int | None) -> Decimal | None:
    if raw is None:
        return None
    return Decimal(int(raw)) / (Decimal(10) ** int(decimals if decimals is not None else 18))


def raw_totals(uid: str) -> dict:
    """Суммы в целых минимальных единицах (без float)."""
    minted = burned = n = 0
    for r in db.q("SELECT kind, amount_raw FROM supply_events WHERE token_uid=? AND COALESCE(orphaned,0)=0", (uid,)):
        v = int(r["amount_raw"])
        n += 1
        if r["kind"] == "mint":
            minted += v
        else:
            burned += v
    return {"minted_raw": minted, "burned_raw": burned, "events": n}


def reconcile(total_raw: int, reconstructed_raw: int) -> dict:
    d = int(total_raw) - int(reconstructed_raw)
    return {"exact": d == 0, "diff_raw": d}


# ------------------------------------------------------------------ время блоков
def exact_block_at(rpc: Any, ts: int, head: int, head_ts: int) -> int:
    """Наименьший блок с timestamp ≥ ts (несколько блоков могут иметь одну секунду). Проверено по соседнему блоку."""
    lo = find_block_by_time(rpc, ts, head, head_ts)
    t = rpc.block_timestamp(lo)
    step = 1000
    while t is not None and t >= ts and lo > 1:  # инвариант: ts(lo) < ts
        lo = max(1, lo - step)
        step *= 2
        t = rpc.block_timestamp(lo)
    hi = min(head, lo + 1000)
    step = 1000
    th = rpc.block_timestamp(hi)
    while th is not None and th < ts and hi < head:  # инвариант: ts(hi) ≥ ts
        lo = hi
        hi = min(head, hi + step)
        step *= 2
        th = rpc.block_timestamp(hi)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if rpc.block_timestamp(mid) >= ts:
            hi = mid
        else:
            lo = mid
    return hi


def _timestamps(rpc: Any, uid: str, head: int, head_ts: int) -> None:
    """Опорные блоки получают ТОЧНОЕ время блока (ts_source=block), остальные — интерполяцию (ts_source=interpolated)."""
    rng = db.q1("SELECT MIN(block) lo, MAX(block) hi, COUNT(*) n FROM supply_events WHERE token_uid=? AND ts IS NULL", (uid,))
    if not rng or not rng["n"]:
        return
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
    if not refs:
        return
    if len(refs) == 1:
        refs.append((head, head_ts))
    exact = dict(refs)
    c = db.get_conn()
    for (bnum,) in c.execute("SELECT DISTINCT block FROM supply_events WHERE token_uid=? AND ts IS NULL", (uid,)).fetchall():
        if bnum in exact:
            c.execute("UPDATE supply_events SET ts=?, ts_source='block' WHERE token_uid=? AND block=?", (iso(from_ts(exact[bnum])), uid, bnum))
            continue
        j = 0
        while j < len(refs) - 2 and refs[j + 1][0] < bnum:
            j += 1
        (b0, t0), (b1, t1) = refs[j], refs[j + 1]
        t = t0 + (bnum - b0) * ((t1 - t0) / (b1 - b0) if b1 != b0 else 0)
        c.execute("UPDATE supply_events SET ts=?, ts_source='interpolated' WHERE token_uid=? AND block=?", (iso(from_ts(t)), uid, bnum))
    c.commit()


def _week_boundaries(rpc: Any, uid: str, head: int, head_ts: int) -> None:
    """Для каждой границы недельного бюджета — точный первый блок с timestamp ≥ границы."""
    tn = current_tokenomics(uid)
    if not tn or not tn["params"].get("weekly_budgets"):
        return
    key = f"week_blocks:{uid}"
    known = loads(db.get_progress(key), {}) or {}
    changed = False
    for w in tn["params"]["weekly_budgets"]:
        st = parse_iso(w.get("start"))
        if not st:
            continue
        for bound in (st, st + _dt.timedelta(days=7)):
            k = iso(bound)
            if k in known or bound.timestamp() > head_ts:
                continue
            try:
                b = exact_block_at(rpc, int(bound.timestamp()), head, head_ts)
                tb, tp = rpc.block_timestamp(b), rpc.block_timestamp(b - 1)
            except SourceError:
                continue
            if tb is not None and tp is not None and tp < bound.timestamp() <= tb:
                known[k] = {"block": b, "ts": iso(from_ts(tb)), "prev_block_ts": iso(from_ts(tp))}
                changed = True
    if changed:
        db.set_progress(key, dumps(known))


def supply_analytics(uid: str) -> dict:
    """Дневная эмиссия, получатели с метками, сравнение с бюджетом. Все суммы — в целых минимальных единицах."""
    tok = db.q1("SELECT chain, decimals FROM tokens WHERE uid=?", (uid,))
    decimals = tok["decimals"] if tok["decimals"] is not None else 18
    labels = _labels(tok["chain"])
    rows = db.rows_to_dicts(db.q("SELECT kind, ts, ts_source, counterparty, amount_raw, tx_hash, block FROM supply_events "
                                 "WHERE token_uid=? AND COALESCE(orphaned,0)=0 ORDER BY block, log_index", (uid,)))
    daily: dict[str, dict] = defaultdict(lambda: {"mint": 0, "burn": 0})
    recips: dict[str, int] = defaultdict(int)
    for r in rows:
        v = int(r["amount_raw"])
        d = (r["ts"] or "")[:10] or "?"
        daily[d][r["kind"]] += v
        if r["kind"] == "mint":
            recips[r["counterparty"]] += v
    D = lambda raw: float(to_decimal(raw, decimals))
    cum = 0
    series = []
    for d in sorted(k for k in daily if k != "?"):
        cum += daily[d]["mint"] - daily[d]["burn"]
        series.append({"date": d, "mint": D(daily[d]["mint"]), "burn": D(daily[d]["burn"]), "cumulative": D(cum),
                       "mint_raw": str(daily[d]["mint"]), "cumulative_raw": str(cum)})
    top = sorted(recips.items(), key=lambda x: -x[1])[:20]
    top_l = [{"address": a, "amount": D(v), "amount_raw": str(v), "label": (labels.get(a) or {}).get("label"),
              "kind": (labels.get(a) or {}).get("kind"), "label_source": (labels.get(a) or {}).get("source_url"),
              "note": None if labels.get(a) else "неизвестный адрес — не считать командой без доказательств"} for a, v in top]
    tn = current_tokenomics(uid)
    bounds = loads(db.get_progress(f"week_blocks:{uid}"), {}) or {}
    budget_cmp = []
    if tn and tn["params"].get("weekly_budgets"):
        emis_kinds = set(tn["params"].get("emission_recipient_kinds") or ["gauge", "emissions", "minter"])
        has_labels = any((lb.get("kind") in emis_kinds) for lb in labels.values())
        for w in tn["params"]["weekly_budgets"]:
            st = parse_iso(w.get("start"))
            if not st:
                continue
            en = st + _dt.timedelta(days=7)
            in_progress = en > now_utc()
            b0 = (bounds.get(iso(st)) or {}).get("block")
            b1 = (bounds.get(iso(en)) or {}).get("block")
            confirmed = b0 is not None and (b1 is not None or in_progress)
            if confirmed:
                sel = [r for r in rows if r["kind"] == "mint" and r["block"] >= b0 and (b1 is None or r["block"] < b1)]
            else:
                sel = []
            all_raw = sum(int(r["amount_raw"]) for r in sel)
            emis_raw = sum(int(r["amount_raw"]) for r in sel if (labels.get(r["counterparty"]) or {}).get("kind") in emis_kinds)
            budget_raw = int(Decimal(str(w["amount"])) * (Decimal(10) ** decimals)) if w.get("amount") is not None else None
            if not confirmed or not has_labels or budget_raw is None or in_progress:
                over = None
            else:
                over = emis_raw * 10 > budget_raw * 11  # > 110% бюджета, целочисленно
            budget_cmp.append({
                "week": w.get("week"), "start": w.get("start"), "budget": w.get("amount"),
                "budget_raw": budget_raw, "start_block": b0, "end_block": b1,
                "boundary_source": "block_timestamps" if confirmed else None,
                "minted_all_raw": all_raw if confirmed else None, "minted_to_emission_contracts_raw": emis_raw if confirmed else None,
                "minted_all": D(all_raw) if confirmed else None, "minted_to_emission_contracts": D(emis_raw) if confirmed else None,
                "in_progress": in_progress, "over_budget": None if in_progress else over, "labels_missing": not has_labels,
                "note": ("границы недели — точные блоки по timestamp блоков; " if confirmed else
                         "границы недели не подтверждены timestamp блоков — сравнение не выполняется; ")
                        + "бюджет — потолок эмиссии, а не выпущенное количество; minted_all включает все mint"})
    return {"daily": series[-180:], "daily_ts_note": "даты по дням — интерполяция времени между опорными блоками",
            "top_recipients": top_l, "budget_comparison": budget_cmp, "decimals": decimals,
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
    if chain.kind == "evm":
        try:
            from .refresh import refresh_circulation
            f2 = get_facts(uid)
            f2["onchain"] = oc
            save_facts(uid, f2, "updated_at")
            refresh_circulation(uid)
            oc = get_facts(uid).get("onchain") or oc
        except SourceError as e:
            oc["circulation_error"] = str(e)
    if not oc.get("security") or (now_utc() - (parse_iso((oc.get("security") or {}).get("at")) or now_utc())).total_seconds() > 86400 \
            or not (oc.get("security") or {}).get("at"):
        oc["security"] = security_check(uid)  # байткод меняется редко: не чаще раза в сутки
    f["onchain"] = oc
    compute_caps(uid, f)
    save_facts(uid, f, "last_onchain_at")
    return oc
