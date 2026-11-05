"""Снимок поведения сканера на КОПИИ базы — для сравнения версий (2.0.1 ↔ 2.1) и режимов (аналитика выкл/вкл).

Сеть отключена: все запросы получают отказ (снимок воспроизводим и не зависит от внешних API).
Исходная база не изменяется: она копируется во временную папку через SQLite backup API.

    python scripts/baseline_compare.py --db data/scanner.db --out baseline.json [--code-dir <папка версии>] [--analytics]
    python scripts/baseline_compare.py --compare a.json b.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--db")
ap.add_argument("--out")
ap.add_argument("--code-dir", default=str(Path(__file__).resolve().parent.parent))
ap.add_argument("--analytics", action="store_true", help="включить оба аналитических модуля 2.1 и выполнить их расчёт")
ap.add_argument("--compare", nargs=2)
args = ap.parse_args()

PRIMARY = ["tokens", "pools", "observations", "supply_events", "tokenomics_versions", "manual_facts", "address_labels",
           "hidden_tokens", "user_actions", "watchlist", "mcap_assessments", "protocols", "raw_responses", "scan_progress"]
USER = ["hidden_tokens", "user_actions", "watchlist", "manual_facts", "tokenomics_versions", "address_labels"]
IMMUTABLE = ["supply_events", "observations", "tokenomics_versions", "raw_responses"]


def table_hash(c: sqlite3.Connection, t: str) -> dict:
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    h = hashlib.sha256()
    n = 0
    for row in c.execute(f"SELECT * FROM {t} ORDER BY {', '.join(cols)}"):
        h.update(repr(tuple(row)).encode())
        n += 1
    return {"rows": n, "sha256": h.hexdigest()}


def compare(a: dict, b: dict) -> int:
    print(f"A: {a['label']}\nB: {b['label']}\n")
    bad = 0
    for t in sorted(set(a["immutable"]) | set(b["immutable"])):
        same = a["immutable"].get(t) == b["immutable"].get(t)
        bad += not same
        print(f"  первичные данные {t:22s}: {'не изменены' if same else 'ИЗМЕНЕНЫ'} {a['immutable'].get(t)} → {b['immutable'].get(t)}")
    for t in sorted(set(a["user"]) | set(b["user"])):
        same = a["user"].get(t) == b["user"].get(t)
        bad += not same
        print(f"  записи пользователя {t:20s}: {'не изменены' if same else 'ИЗМЕНЕНЫ'}")
    diff = [u for u in a["tokens"] if a["tokens"][u] != b["tokens"].get(u)]
    print(f"  токенов: {len(a['tokens'])} / {len(b['tokens'])}; отличий уровня/статуса/оценки: {len(diff)}")
    for u in diff[:15]:
        print(f"    {u}: {a['tokens'][u]} → {b['tokens'].get(u)}")
    fd = [k for k in a["filters"] if a["filters"][k] != b["filters"].get(k)]
    print(f"  решений фильтров: {len(a['filters'])} / {len(b['filters'])}; отличий: {len(fd)}")
    for k in fd[:15]:
        print(f"    {k}: {a['filters'][k]} → {b['filters'].get(k)}")
    same_best = a["best"] == b["best"]
    print(f"  лучшие кандидаты: {'совпадают' if same_best else 'ОТЛИЧАЮТСЯ'} ({len(a['best'])} / {len(b['best'])})")
    extra = sorted(set(b["tables"]) - set(a["tables"]))
    print(f"  новые таблицы в B: {extra or 'нет'}")
    bad += len(diff) + len(fd) + (not same_best)
    print("\nИТОГ: " + ("поведение совпадает" if bad == 0 else f"есть отличия ({bad})"))
    return 0 if bad == 0 else 1


if args.compare:
    sys.exit(compare(*(json.loads(Path(p).read_text(encoding="utf-8")) for p in args.compare)))

tmp = Path(tempfile.mkdtemp(prefix="das-baseline-"))
src = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=ro", uri=True)
dst = sqlite3.connect(str(tmp / "scanner.db"))
with dst:
    src.backup(dst)
dst.close()
src.close()
os.environ["DAS_DATA_DIR"] = str(tmp)
sys.path.insert(0, str(Path(args.code_dir).resolve()))

import httpx  # noqa: E402

from app import __version__, config, db, http, pipeline  # noqa: E402

http.set_client(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)), timeout=5))
db.set_db_path(tmp / "scanner.db")
info = db.init_db()
c0 = db.connect(tmp / "scanner.db")
before_immutable = {t: table_hash(c0, t) for t in IMMUTABLE}
c0.close()
t0 = time.time()
uids = [r["uid"] for r in db.q("SELECT uid FROM tokens ORDER BY uid")]
for u in uids:
    pipeline.evaluate(u, old_facts=None)
t_eval = time.time() - t0
an = None
if args.analytics:
    s = config.load_settings()
    s.setdefault("analytics", {})
    s["analytics"]["momentum_enabled"] = True
    s["analytics"]["emission_enabled"] = True
    config.save_settings(s)
    from app import analytics  # noqa: E402 — только в 2.1
    t1 = time.time()
    an = {"momentum": analytics.run_module("momentum", deadline_s=600), "emission": analytics.run_module("emission", deadline_s=600),
          "seconds": None}
    an["seconds"] = round(time.time() - t1, 2)
    for u in uids:  # повторная оценка после аналитики: основной сканер не должен изменить решения
        pipeline.evaluate(u, old_facts=None)

from fastapi.testclient import TestClient  # noqa: E402

from app.api import app  # noqa: E402

best = [r["uid"] for r in TestClient(app).get("/api/best").json()["rows"]]
c = db.connect(tmp / "scanner.db")
tables = sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"))
out = {"label": f"{__version__}{' + аналитика' if args.analytics else ''} ({Path(args.code_dir).name})", "version": __version__,
       "init_db": info, "eval_seconds": round(t_eval, 2), "analytics": an,
       "tables": {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables},
       "immutable_before": before_immutable,
       "immutable": {t: table_hash(c, t) for t in IMMUTABLE},
       "user": {t: table_hash(c, t) for t in USER},
       "tokens": {r["uid"]: [r["level"], r["status"], r["score_preliminary"], r["score_confirmed"]]
                  for r in c.execute("SELECT uid, level, status, score_preliminary, score_confirmed FROM tokens")},
       "filters": {f"{r['token_uid']}|{r['filter_key']}": [r["status"], r["reason"]] for r in c.execute("SELECT * FROM filter_current")},
       "best": best,
       "integrity": c.execute("PRAGMA integrity_check").fetchone()[0]}
c.close()
Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
print(f"{out['label']}: токенов {len(out['tokens'])}, решений фильтров {len(out['filters'])}, лучших {len(best)}, "
      f"integrity {out['integrity']}, оценка {out['eval_seconds']} с" + (f", аналитика {an['seconds']} с" if an else ""))
print(f"первичные данные не изменены во время прогона: {before_immutable == out['immutable']}")
