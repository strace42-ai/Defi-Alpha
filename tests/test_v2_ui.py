"""14.13: автообновление экрана не сбрасывает прокрутку, вкладку, поиск, сортировку и несохранённый ввод.
Запускает настоящий браузер (Playwright/Node). Если Playwright недоступен — тест ПРОПУСКАЕТСЯ (не засчитывается как пройденный).
Данные — синтетические токены во временной базе."""
import json
import shutil
import socket
import subprocess
import threading
import time as _t

import pytest

from app import db, discovery
from app.chains import CHAINS
from app.util import dumps, now_iso

SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const base = process.argv[2], uid = process.argv[3];
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1300, height: 700 } });
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  const out = {};
  await p.goto(base + '/#/scanner'); await p.waitForSelector('#tw table');
  await p.fill('#q', 'несохранённый поиск');                 // ввод без применения
  await p.click('th[data-k="symbol"]');                       // сортировка по тикеру
  const sortBefore = await p.textContent('th[data-k="symbol"]');
  await p.evaluate(() => { document.querySelector('#tw').scrollTop = 400; window.scrollTo(0, 150); });
  await p.evaluate(() => { document.activeElement.blur(); });
  await p.evaluate(async () => { await render(true); });     // то же, что делает живой опрос
  out.scanner = { q: await p.inputValue('#q'), tw: await p.evaluate(() => document.querySelector('#tw').scrollTop),
                  y: await p.evaluate(() => window.scrollY), sort: await p.textContent('th[data-k="symbol"]'), sortBefore };
  await p.goto(base + '/#/token/' + encodeURIComponent(uid)); await p.waitForSelector('#tabs');
  await p.click('#tabs button[data-t="8"]');                  // вкладка «Ручные данные»
  await p.fill('#factForm input[name="note"]', 'черновик заметки');
  await p.evaluate(async () => { await render(true); });
  out.token = { tab: await p.evaluate(() => document.querySelector('#tabs button.active').dataset.t),
                note: await p.inputValue('#factForm input[name="note"]') };
  out.errors = errs;
  console.log(JSON.stringify(out));
  await b.close();
})();
"""


def _node_playwright() -> bool:
    if not shutil.which("node"):
        return False
    r = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _node_playwright(), reason="Playwright/Node недоступен — UI-тест не выполнялся")
def test_14_13_live_refresh_preserves_ui_state(tmp_path):
    import uvicorn
    from app.api import app
    for i in range(60):
        uid, _ = discovery.upsert_token(CHAINS["robinhood"], {"address": "0x" + f"{i:040x}", "symbol": f"T{i:02d}", "name": f"Token {i}"},
                                        source="test", reason="synthetic")
        db.ex("UPDATE tokens SET last_market_at=?, facts=? WHERE uid=?", (now_iso(), dumps({"price": {"value": 1.0 + i}}), uid))
    target = "robinhood:0x" + f"{5:040x}"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        threading.Event().wait(0.05)
    js = tmp_path / "ui.js"
    js.write_text(SCRIPT, encoding="utf-8")
    r = subprocess.run(["node", str(js), f"http://127.0.0.1:{port}", target], capture_output=True, text=True, timeout=120)
    srv.should_exit = True
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    sc = out["scanner"]
    assert sc["q"] == "несохранённый поиск"          # поисковый ввод сохранён
    assert sc["tw"] >= 350 and sc["y"] >= 100         # прокрутка таблицы и страницы сохранена
    assert sc["sort"] == sc["sortBefore"]             # сортировка сохранена
    assert out["token"]["tab"] == "8"                 # выбранная вкладка сохранена
    assert out["token"]["note"] == "черновик заметки" # несохранённый ввод в форме не стёрт
