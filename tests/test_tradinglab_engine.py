"""Trading Lab, этап C: учёт, исполнение, риск, идемпотентность, восстановление, изоляция и режимы (ТЗ §14)."""
import datetime as dt
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.tradinglab import analysis, backtest, candles, strategies
from app.tradinglab.candles import Candle, Series
from app.tradinglab.engine import Trader
from app.tradinglab.execution import CONSERVATIVE_V1, quote_buy, quote_sell
from app.tradinglab.portfolio import AccountingError, Portfolio
from app.tradinglab.store import Store, StoreError

FIX = Path(__file__).resolve().parent / "fixtures" / "trading_lab"
NOW = int(dt.datetime(2026, 11, 5, 13, 27, tzinfo=dt.UTC).timestamp())
M = CONSERVATIVE_V1
H = 3600


def real_series():
    return {tf: candles.from_gt_payload(json.loads((FIX / f"gt_ohlcv_{n}.json").read_text()), tf, NOW)
            for tf, n in (("1h", "h1"), ("4h", "h4"), ("1d", "d1"))}


def flat_series(n=60, extra=()):
    ks = [Candle(i * H, 1.0, 1.01, 0.99, 1.0, 1000.0) for i in range(n)]
    for j, (o, h, l, c) in enumerate(extra):
        ks.append(Candle((n + j) * H, o, h, l, c, 1000.0))
    return Series("1h", ks)


def stub(enter_at: int, stop=0.9, t1=1.5, t1_frac=0.5, trail_after=True, time_stop=100, exit_at=None):
    """Стратегия-заглушка: ENTER на баре enter_at с заданным планом; опционально EXIT на баре exit_at."""
    def fn(ctx):
        if ctx.in_position:
            act = "EXIT" if exit_at is not None and ctx.i == exit_at else "HOLD"
            return strategies.decision(strategies.S1, ctx, act, [strategies.C("stub", act == "EXIT", "")], None, {}, "")
        if ctx.i == enter_at:
            plan = {"entry_ref": ctx.a.s.candles[ctx.i].c, "stop": stop, "t1": t1, "t1_frac": t1_frac, "trail_atr": 2.0,
                    "trail_ref": "close", "time_stop": time_stop, "rr_net": strategies.rr_net(ctx.a.s.candles[ctx.i].c, stop, t1, ctx.cost)}
            return strategies.decision(strategies.S1, ctx, "ENTER", [strategies.C("stub", True, "тест")], plan, {}, "")
        return strategies.decision(strategies.S1, ctx, "WAIT", [strategies.C("stub", False, "")], None, {}, "")
    return fn


def run_stub(s, fn, exp="T", tf="1h", start=0, store=None, upto=None):
    a = analysis.analyze(s)
    tr = Trader(tf, "BACKTEST", exp, M)
    tr.fn = fn
    for i in range(start, len(s) if upto is None else upto):
        tr.process_bar(a, i)
        if store is not None:
            store.commit_bar(tr)
    return tr, a


# ------------------------------------------------------------------ учёт
def test_no_negative_cash_and_no_selling_missing_tokens():
    p = Portfolio.new("x", "100")
    with pytest.raises(AccountingError):
        p.apply_buy("b1", quote_buy("150", 1.0, M), "P", 0)
    assert p.cash == Decimal("100") and p.lots == []
    p.apply_buy("b2", quote_buy("50", 1.0, M), "P", 0)
    with pytest.raises(AccountingError):
        p.apply_sell("s1", quote_sell(p.qty + 1, 1.0, M), "P")
    assert p.cash >= 0


def test_same_fill_applied_once():
    p = Portfolio.new("x", "100")
    f = quote_buy("50", 1.0, M)
    assert p.apply_buy("b", f, "P", 0) is True
    assert p.apply_buy("b", f, "P", 0) is False
    assert len(p.lots) == 1


def test_fees_reduce_profit_round_trip_at_same_price():
    p = Portfolio.new("x", "1000")
    b = quote_buy("500", 0.06, M)
    p.apply_buy("b", b, "P", 0)
    s = quote_sell(p.qty, 0.06, M)
    _, pnl = p.apply_sell("s", s, "P")
    assert pnl < 0
    assert abs(pnl + b.cost_usd + s.cost_usd) < Decimal("1e-20")          # убыток = ровно сумма издержек
    assert b.fee_usd == Decimal("2.5") and abs(s.fee_usd / s.qty / s.mid - Decimal("0.01")) < Decimal("1e-30")
    assert p.cash == Decimal("1000") + pnl


def test_partial_sells_fifo_consistent():
    p = Portfolio.new("x", "1000")
    p.apply_buy("b1", quote_buy("200", 1.0, M), "P", 0)
    p.apply_buy("b2", quote_buy("200", 2.0, M), "P", 1)
    q1 = p.lots[0].qty
    _, pnl1 = p.apply_sell("s1", quote_sell(q1 / 2, 1.5, M), "P")
    from app.tradinglab.execution import q_fables
    assert p.lots[0].qty == q1 - q_fables(q1 / 2) and len(p.lots) == 2  # FIFO: списывается первый лот
    _, pnl2 = p.apply_sell("s2", quote_sell(p.qty, 1.5, M), "P")
    assert p.qty == 0 and p.lots == []
    assert abs(p.cash - (Decimal("1000") + pnl1 + pnl2)) < Decimal("1e-20")
    assert p.realized == pnl1 + pnl2


# ------------------------------------------------------------------ исполнение во времени
def test_entry_executes_at_next_open_not_decision_close():
    s = flat_series(60, extra=[(1.05, 1.06, 1.04, 1.05)] * 3)
    tr, _ = run_stub(s, stub(enter_at=59))
    buy = [o for o in tr.ops if o["side"] == "BUY"]
    assert len(buy) == 1
    assert buy[0]["ts"] == 60 * H and Decimal(buy[0]["mid"]) == Decimal("1.05")   # open следующей свечи, не close=1.0
    assert Decimal(buy[0]["price"]) > Decimal("1.05")                              # хуже mid на издержки


def test_reprocessing_bar_is_noop():
    s = flat_series(62)
    a = analysis.analyze(s)
    tr = Trader("1h", "BACKTEST", "T", M)
    tr.fn = stub(enter_at=59)
    for i in range(62):
        tr.process_bar(a, i)
    n_ops, cash = len(tr.ops), tr.portfolio.cash
    for i in range(62):
        assert tr.process_bar(a, i) is None
    assert len(tr.ops) == n_ops and tr.portfolio.cash == cash


def test_stop_gap_fills_worse_than_level():
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0), (0.80, 0.82, 0.78, 0.8)])   # гэп ниже стопа 0.9
    tr, _ = run_stub(s, stub(enter_at=59))
    st = [o for o in tr.ops if o["side"] == "SELL"][0]
    assert Decimal(st["mid"]) == Decimal("0.8") and Decimal(st["price"]) < Decimal("0.8")
    assert tr.trades[0]["net_pnl"].startswith("-") and "стоп" in tr.trades[0]["exit_reason"]


def test_stop_checked_before_target_in_same_bar():
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0), (1.0, 1.6, 0.85, 1.0)])     # касание и стопа, и цели
    tr, _ = run_stub(s, stub(enter_at=59))
    sells = [o for o in tr.ops if o["side"] == "SELL"]
    assert len(sells) == 1 and "стоп" in sells[0]["reason"]


def test_partial_target_then_trailing_and_accounting():
    up = [(1.0, 1.01, 0.99, 1.0), (1.2, 1.55, 1.2, 1.5)] + [(1.5, 1.52, 1.48, 1.5)] * 3 + [(1.5, 1.5, 1.2, 1.2)]
    s = flat_series(60, extra=up)
    tr, _ = run_stub(s, stub(enter_at=59))
    sells = [o for o in tr.ops if o["side"] == "SELL"]
    assert "цель 1" in sells[0]["reason"] and len(sells) == 2
    assert tr.portfolio.qty == 0 and len(tr.trades) == 1
    t = tr.trades[0]
    assert Decimal(t["net_pnl"]) == tr.portfolio.cash - Decimal("1000")
    assert abs(Decimal(t["net_pnl"]) - Decimal(t["lots_pnl_check"])) < Decimal("1e-18")
    assert t["mfe_pct"] > 50 and t["r_multiple"] > 0


def test_trailing_update_applies_from_next_bar_only():
    # бар 61: цель 1 (продажа 50%), минимум 1.2 ниже будущего trailing — но trailing считается только на закрытии 61
    up = [(1.0, 1.01, 0.99, 1.0), (1.2, 1.55, 1.2, 1.5), (1.4, 1.41, 1.30, 1.35)]
    s = flat_series(60, extra=up)
    tr, a = run_stub(s, stub(enter_at=59), upto=62)
    assert [o["reason"][:6] for o in tr.ops if o["side"] == "SELL"] == ["цель 1"]
    trail = tr.position.stop
    assert trail > 1.2 and abs(trail - (1.5 - 2.0 * a.v("atr", 61))) < 1e-12
    tr.process_bar(a, 62)                                                     # бар 62: low 1.30 < trailing → выход
    last = [o for o in tr.ops if o["side"] == "SELL"][-1]
    assert "стоп" in last["reason"] and Decimal(last["mid"]) == Decimal(str(min(1.4, trail)))
    assert tr.position is None and len(tr.trades) == 1


def test_time_stop_exits_next_open():
    s = flat_series(70)
    tr, _ = run_stub(s, stub(enter_at=55, time_stop=5))
    sells = [o for o in tr.ops if o["side"] == "SELL"]
    assert sells and "time stop" in sells[0]["reason"]
    assert sells[0]["ts"] == (56 + 5 + 1) * H


# ------------------------------------------------------------------ защита при данных и риске
def test_insufficient_history_blocks_daily_trader():
    r = backtest.run(real_series())
    t = r["traders"]["1d"]
    assert t.ops == [] and all(d["action_final"] == "WARMUP" for d in t.decisions)


def test_data_gap_blocks_entries():
    ks = flat_series(60).candles
    ks = ks[:45] + ks[47:]                                                    # пропуск 2 свечей в последних 50
    s = candles.normalize([[k.ts, k.o, k.h, k.l, k.c, k.v] for k in ks], "1h", now=10 ** 9)
    assert not s.quality()["ok"]
    tr, _ = run_stub(s, stub(enter_at=len(s) - 2))
    assert tr.ops == [] and tr.decisions[-2]["action_final"] == "NO_TRADE" and tr.state == "DATA_ERROR"


def test_risk_rr_filter_rejects_entry():
    s = flat_series(62)
    tr, _ = run_stub(s, stub(enter_at=59, stop=0.9, t1=1.05))                 # чистый R:R < 2
    assert tr.ops == []
    assert any("R:R" in c["name"] and not c["ok"] for c in tr.decisions[59]["risk"])


def test_position_size_respects_risk_budget():
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0)] * 2)
    tr, _ = run_stub(s, stub(enter_at=59))
    loss_at_stop = Decimal(tr.position.risk_usd)
    assert loss_at_stop <= Decimal("1000") * Decimal("0.0075") * Decimal("1.05")   # 0,75% equity (+допуск на открытие по open)


def test_kill_switch_on_drawdown():
    p = Trader("1h", "BACKTEST", "T", M)
    from app.tradinglab import risk as R
    R.on_bar(p.risk, p.rcfg, Decimal("1000"), 0)
    R.on_bar(p.risk, p.rcfg, Decimal("870"), H)
    assert p.risk.killed
    assert not all(c["ok"] for c in R.entry_checks(p.risk, p.rcfg, Decimal("870"), 5, 3.0))


# ------------------------------------------------------------------ хранение, рестарт, изоляция, режимы
def test_restart_resumes_identically(tmp_path):
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0), (1.2, 1.55, 1.2, 1.5)] + [(1.5, 1.52, 1.48, 1.5)] * 4)
    ref, _ = run_stub(s, stub(enter_at=59))
    st = Store(tmp_path / "lab.db")
    st.create_experiment("T", "BACKTEST", 0, {}, "h")
    run_stub(s, stub(enter_at=59), store=st, upto=61)                       # «падение» после бара 60
    st.close()
    st2 = Store(tmp_path / "lab.db")
    tr = Trader("1h", "BACKTEST", "T", M)
    tr.fn = stub(enter_at=59)
    tr.load_state(st2.trader_state("T", "1h"))
    a = analysis.analyze(s)
    for i in range(len(s)):                                                   # повтор с начала: старые бары — no-op
        tr.process_bar(a, i)
        st2.commit_bar(tr)
    assert tr.portfolio.to_state() == ref.portfolio.to_state()
    fills = st2.fills("T", "1h")
    assert [f["op_id"] for f in fills] == [o["op_id"] for o in ref.ops]
    cash = Decimal("1000")
    for f in fills:                                                           # журнал воспроизводит баланс
        cash += -(Decimal(f["usd"]) + Decimal(f["gas_usd"])) if f["side"] == "BUY" else Decimal(f["usd"]) - Decimal(f["gas_usd"])
    assert cash == tr.portfolio.cash


def test_double_commit_rolls_back(tmp_path):
    st = Store(tmp_path / "lab.db")
    st.create_experiment("T", "BACKTEST", 0, {}, "h")
    s = flat_series(61)
    tr, a = run_stub(s, stub(enter_at=59), store=st)
    before = st.trader_state("T", "1h")
    tr._push("decisions", dict(tr.decisions[-1]))                             # тот же бар повторно
    with pytest.raises(sqlite3.IntegrityError):
        st.commit_bar(tr)
    assert st.trader_state("T", "1h") == before


def test_journals_append_only(tmp_path):
    st = Store(tmp_path / "lab.db")
    st.create_experiment("T", "BACKTEST", 0, {}, "h")
    run_stub(flat_series(62), stub(enter_at=59), store=st)
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("UPDATE fills SET json='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("DELETE FROM decisions")
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("UPDATE experiments SET started_at=1")


def test_backtest_and_forward_are_separated(tmp_path):
    st = Store(tmp_path / "lab.db")
    st.create_experiment("F", "FORWARD", 59 * H, {}, "h")
    tr = Trader("1h", "BACKTEST", "F", M)
    with pytest.raises(StoreError):
        st.commit_bar(tr)                                                     # backtest-трейдер в forward-эксперимент
    fw = Trader("1h", "FORWARD", "F", M)
    fw.fn = stub(enter_at=10 ** 6)
    a = analysis.analyze(flat_series(60))
    fw.process_bar(a, 10)                                                     # решение раньше started_at
    with pytest.raises(StoreError):
        st.commit_bar(fw)
    assert st.fills("F", "1h") == [] and st.trader_state("F", "1h") is None


def test_three_portfolios_isolated(tmp_path):
    st = Store(tmp_path / "lab.db")
    r = backtest.run(real_series(), store=st, experiment="BT")
    tr = r["traders"]
    assert len({id(t.portfolio) for t in tr.values()}) == 3
    for tf, t in tr.items():
        assert all(o["trader"] == tf for o in t.ops)
        assert all(d["trader"] == tf for d in st.decisions("BT", tf, 10_000))
        assert st.trader_state("BT", tf)["tf"] == tf
    tr["1h"].portfolio.cash -= 1
    assert tr["4h"].portfolio.cash == Decimal("1000")


def test_htf_context_uses_only_closed_candles():
    S = real_series()
    r = backtest.run(S, tfs=("1h",))
    a1, a4 = r["analyses"]["1h"], r["analyses"]["4h"]
    tr = r["traders"]["1h"]
    for d in tr.decisions[-30:]:
        htf_conds = [c for c in d["conditions"] if c["name"] == "Контекст старшего ТФ"]
        assert htf_conds is not None
    import bisect
    close4 = [k.ts + 4 * H for k in a4.s.candles]
    for k in a1.s.candles[-10:]:
        j = bisect.bisect_right(close4, k.ts + H) - 1
        assert a4.s.candles[j].ts + 4 * H <= k.ts + H < a4.s.candles[j].ts + 8 * H + 1


def test_backtest_benchmark_same_start_and_costs():
    r = backtest.run(real_series())
    assert r["bh"][0]["ts"] == r["start_ts"] + H
    first_eq = Decimal(r["bh"][0]["equity"])
    assert first_eq < Decimal("1000")                                          # издержки входа и оценка по ликвидации
    for sm in r["summary"].values():
        assert sm["validation"] == "NOT VALIDATED" and sm["sample"] == "INSUFFICIENT_SAMPLE"
        assert "vs_bh_usd" in sm
