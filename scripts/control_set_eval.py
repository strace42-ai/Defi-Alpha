"""Проверка на размеченном контрольном наборе РЕАЛЬНЫХ контрактов (research/control_set.json).

Запуск:  python scripts/control_set_eval.py   (нужен интернет; ~10–20 минут из-за лимитов API)
Результат: docs/CONTROL_SET_REPORT.md. Ожидания — только проверяемые факты; это не эталон полноты поиска.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DAS_DATA_DIR", str(ROOT / "data" / "control_eval"))

from app import db, discovery, enrich, jobs, research  # noqa: E402
from app.chains import CHAINS  # noqa: E402
from app.util import now_utc  # noqa: E402


def main() -> None:
    db.init_db()
    research.load_seed()
    items = json.loads((ROOT / "research" / "control_set.json").read_text(encoding="utf-8"))["items"]
    rows = []
    errors = {"false_include": 0, "false_exclude": 0, "identity": 0, "checks": 0, "passed": 0}
    for it in items:
        uid, _ = discovery.add_token_manually(it["chain"], it["address"], "control set")
        print("…", it["label"], flush=True)
        try:
            jobs.deep_analyze(uid, lambda m: None, onchain_calls=10)
        except Exception as e:  # noqa: BLE001
            rows.append({"label": it["label"], "error": str(e)})
            continue
        t = db.q1("SELECT * FROM tokens WHERE uid=?", (uid,))
        fc = {r["filter_key"]: dict(r) for r in db.q("SELECT * FROM filter_current WHERE token_uid=?", (uid,))}
        f = enrich.get_facts(uid)
        exp = it["expect"]
        checks = []

        def chk(name, ok, detail):
            errors["checks"] += 1
            errors["passed"] += 1 if ok else 0
            checks.append(("✅" if ok else "❌") + f" {name}: {detail}")
            return ok

        if "defi_link" in exp:
            got = t["protocol_slug"]
            want = exp["defi_link"]
            ok = (got == want) if want is None else (got is not None and want[:4] in got)
            if not chk("связь с протоколом", ok, f"ожидалось {want}, получено {got}"):
                errors["identity"] += 1
        if "first_trade_known" in exp:
            start = dt.datetime.fromisoformat(exp["first_trade_known"]).replace(tzinfo=dt.timezone.utc)
            age = (now_utc() - start).days
            expected = "PASS" if age <= 90 else "FAIL"
            got = (fc.get("age") or {}).get("status")
            ok = got == expected
            chk("возраст", ok, f"по известной дате {exp['first_trade_known']} ({age} дн.) ожидалось {expected}, получено {got}")
            if not ok:
                errors["false_exclude" if expected == "PASS" else "false_include"] += 1
        if exp.get("age_over_90"):
            got = (fc.get("age") or {}).get("status")
            ok = got == "FAIL"
            chk("возраст > 90", ok, f"получено {got}: {(fc.get('age') or {}).get('reason')}")
            if not ok:
                errors["false_include"] += 1
        if exp.get("mcap_under_5m"):
            got = (fc.get("mcap") or {}).get("status")
            ok = got == "PASS"
            chk("MCAP ≤ $5 млн", ok, f"получено {got}: {(fc.get('mcap') or {}).get('reason')}")
            if not ok:
                errors["false_exclude"] += 1
        if exp.get("drawdown_under_86"):
            got = (fc.get("drawdown") or {}).get("status")
            ok = got == "PASS"
            chk("просадка ≤ 86%", ok, f"получено {got}: {(fc.get('drawdown') or {}).get('reason')}")
            if not ok:
                errors["false_exclude"] += 1
        if exp.get("mature_project_warning"):
            reason = (fc.get("age") or {}).get("reason") or ""
            chk("предупреждение «новый токен зрелого проекта»", "зрелого проекта" in reason or (fc.get("age") or {}).get("status") == "FAIL",
                reason[:200])
        if exp.get("utility_pass") is False:
            got = (fc.get("utility") or {}).get("status")
            ok = got != "PASS"
            chk("утилити не PASS", ok, f"получено {got}")
            if not ok:
                errors["false_include"] += 1
        rows.append({"label": it["label"], "uid": uid, "level": t["level"], "status": t["status"], "reason": t["status_reason"],
                     "checks": checks, "price": (f.get("price") or {}).get("value")})
    now = now_utc().strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Контрольный набор — отчёт\n\nСформирован: {now}. Скрипт: `scripts/control_set_eval.py`. Данные — живые API.\n",
           f"Проверок: **{errors['checks']}**, успешно: **{errors['passed']}**. Ложных включений: **{errors['false_include']}**, "
           f"ложных исключений: **{errors['false_exclude']}**, ошибок идентификации: **{errors['identity']}**.\n",
           "> Набор мал (9 контрактов) и не является эталоном полноты поиска. Он проверяет корректность идентификации и фильтров.\n"]
    for r in rows:
        out.append(f"\n## {r['label']}\n")
        if r.get("error"):
            out.append(f"Ошибка: {r['error']}\n")
            continue
        out.append(f"`{r['uid']}` — уровень **{r['level']}**, статус **{r['status']}**, цена {r['price']}\n\n> {r['reason']}\n")
        out += [f"- {c}\n" for c in r["checks"]]
    (ROOT / "docs" / "CONTROL_SET_REPORT.md").write_text("".join(out), encoding="utf-8")
    print("".join(out))


if __name__ == "__main__":
    main()
