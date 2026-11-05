/* DeFi Alpha Scanner — интерфейс. Все данные из API считаются недоверенными и экранируются. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
$("#tz").textContent = TZ;
let META = null;
const charts = [];

function esc(v) { return v == null ? "" : String(v).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function safeUrl(u) { try { const x = new URL(u); return (x.protocol === "https:" || x.protocol === "http:") ? x.href : null; } catch { return null; } }
function link(u, text) { const s = safeUrl(u); return s ? `<a href="${esc(s)}" target="_blank" rel="noopener noreferrer">${esc(text || u)}</a>` : esc(text || u || ""); }
function usd(v) {
  if (v == null || isNaN(v)) return '<span class="muted">н/д</span>';
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + " млрд";
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + " млн";
  if (a >= 1e3) return "$" + (v / 1e3).toFixed(1) + " тыс.";
  if (a >= 1) return "$" + v.toFixed(2);
  if (a === 0) return "$0";
  return "$" + Number(v).toPrecision(4);
}
function num(v, d = 0) { return v == null || isNaN(v) ? '<span class="muted">н/д</span>' : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: d }); }
function pct(v, d = 1) { return v == null || isNaN(v) ? '<span class="muted">н/д</span>' : (v > 0 ? "+" : "") + Number(v).toFixed(d) + "%"; }
function dt(s, withTime = true) {
  if (!s) return '<span class="muted">н/д</span>';
  const d = new Date(s); if (isNaN(d)) return esc(s);
  return `<span title="${esc(s)} UTC">${d.toLocaleString("ru-RU", withTime ? { dateStyle: "short", timeStyle: "short" } : { dateStyle: "short" })}</span>`;
}
function st(s, conf) {
  if (!s) return '<span class="muted">—</span>';
  const cls = s.split(" ")[0];
  const label = { PASS: "PASS", FAIL: "FAIL", UNKNOWN: "UNKNOWN", "MANUAL REVIEW": "РУЧН. ПРОВЕРКА", "STALE DATA": "УСТАРЕЛО", "DATA ERROR": "ОШИБКА ДАННЫХ" }[s] || s;
  return `<span class="st ${cls} ${conf === "preliminary" ? "pre" : ""}" title="${conf === "preliminary" ? "предварительно" : esc(conf || "")}">${label}${conf === "preliminary" ? "*" : ""}</span>`;
}
function short(a) { return a && a.length > 14 ? a.slice(0, 8) + "…" + a.slice(-6) : a || ""; }
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) { let m = r.status; try { m = (await r.json()).detail || m; } catch { } throw new Error(m); }
  return r.json();
}
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 4500); }
function clearCharts() { while (charts.length) charts.pop().destroy(); }

/* ---------- графики: тёмная тема, одна ось, подсказка при наведении ---------- */
const SERIES = ["#3987e5", "#d95926", "#199e70"];
Chart.defaults.color = "#c3c2b7"; Chart.defaults.borderColor = "#2f2f2c"; Chart.defaults.font.family = "Segoe UI, system-ui, sans-serif";
function lineChart(canvas, datasets, opts = {}) {
  const c = new Chart(canvas, {
    type: opts.type || "line",
    data: { datasets: datasets.map((d, i) => ({ borderColor: SERIES[i], backgroundColor: SERIES[i], borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
      tension: 0, borderRadius: 4, ...d })) },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: datasets.length > 1, labels: { boxWidth: 10 } },
        tooltip: { callbacks: { title: it => it.length ? new Date(it[0].parsed.x).toLocaleDateString("ru-RU") : "",
          label: it => `${it.dataset.label}: ${opts.fmt ? opts.fmt(it.parsed.y) : it.parsed.y.toLocaleString("ru-RU")}` } } },
      scales: {
        x: { type: "linear", grid: { display: false }, ticks: { maxTicksLimit: 7, callback: v => new Date(v).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) } },
        y: { type: opts.log ? "logarithmic" : "linear", grid: { color: "#2a2a27" }, ticks: { maxTicksLimit: 6, callback: v => opts.fmtAxis ? opts.fmtAxis(v) : v } },
      },
    },
  });
  charts.push(c); return c;
}
const fmtUsdPlain = v => { const a = Math.abs(v); return a >= 1e6 ? "$" + (v / 1e6).toFixed(1) + "M" : a >= 1e3 ? "$" + (v / 1e3).toFixed(0) + "K" : a >= 1 ? "$" + v.toFixed(2) : "$" + Number(v).toPrecision(3); };
const fmtNum = v => { const a = Math.abs(v); return a >= 1e6 ? (v / 1e6).toFixed(1) + "M" : a >= 1e3 ? (v / 1e3).toFixed(0) + "K" : Number(v).toPrecision(3); };

/* ---------- задания ---------- */
async function pollJobs() {
  try {
    const j = await api("/api/jobs");
    const cur = j.status.current, q = j.status.queued.length;
    const el = $("#jobStatus");
    if (cur) { el.classList.add("busy"); el.textContent = `${META.jobs[cur.job] || cur.job}: ${cur.progress || ""}${q ? ` (+${q} в очереди)` : ""}`; }
    else { el.classList.remove("busy"); const last = j.recent.find(x => x.status !== "queued"); el.textContent = last ? `последнее: ${META.jobs[last.job] || last.job} — ${last.status === "done" ? "готово" : last.status}` : ""; }
    $("#scanBtn").disabled = !!(cur && cur.job === "scan");
    if (window._lastBusy && !cur && ["dashboard", "scanner"].includes(route().name)) render();
    window._lastBusy = !!cur;
  } catch { }
  try { const a = await api("/api/alerts?only_open=true&limit=100"); const b = $("#alertBadge"); b.textContent = a.rows.length; b.classList.toggle("hidden", !a.rows.length); } catch { }
}
$("#scanBtn").onclick = async () => { await api("/api/scan", { method: "POST", body: {} }); toast("Сканирование поставлено в очередь. Первые результаты появятся через 1–3 минуты."); pollJobs(); };

/* ---------- маршрутизация ---------- */
function route() { const h = location.hash.replace(/^#\//, ""); const [name, ...rest] = h.split("/"); return { name: name || "dashboard", arg: decodeURIComponent(rest.join("/")) }; }
async function render() {
  clearCharts();
  const r = route();
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("active", a.dataset.r === r.name || (r.name === "token" && a.dataset.r === "scanner")));
  const v = $("#view");
  try {
    const fn = { dashboard: vDashboard, scanner: vScanner, token: vToken, review: vReview, watchlist: vWatch, tokenomics: vTokenomics, alerts: vAlerts, sources: vSources, settings: vSettings }[r.name] || vDashboard;
    await fn(v, r.arg);
  } catch (e) { v.innerHTML = `<div class="warnbox">Ошибка: ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", render);

/* ---------- Обзор ---------- */
async function vDashboard(v) {
  const d = await api("/api/dashboard");
  const L = d.levels || {}, S = d.statuses || {};
  const tile = (l, val, s = "") => `<div class="card tile"><div class="l">${l}</div><div class="v">${val}</div><div class="s">${s}</div></div>`;
  v.innerHTML = `<h1>Обзор</h1>
  ${!d.tokens ? `<div class="card"><b>База пока пуста.</b> Нажмите <b>«Сканировать»</b> справа вверху — программа получит реальные новые пулы из GeckoTerminal, проверит токены и покажет, почему каждый подходит или нет.</div>` : ""}
  <div class="grid g4">
    ${tile("Пулов в базе", num(d.pools), `за 24 ч: ${num(d.pools_24h)}`)}
    ${tile("Уникальных токенов", num(d.tokens), `служебных активов: ${num(d.service_assets)}`)}
    ${tile("DeFi-кандидатов (C–E)", num((L.C || 0) + (L.D || 0) + (L.E || 0)), `B: ${num(L.B || 0)} · D: ${num(L.D || 0)} · E: ${num(L.E || 0)}`)}
    ${tile("Прошли все 6 фильтров", num(S.PASS || 0), "PASS (вкл. предварительные)")}
    ${tile("Требуют ручной проверки", `<a href="#/review">${num(S["MANUAL REVIEW"] || 0)}</a>`, "UNKNOWN по части фильтров")}
    ${tile("Отклонено", num(S.FAIL || 0), "с причиной и доказательством")}
    ${tile("В наблюдении", `<a href="#/watchlist">${num(d.watchlist)}</a>`)}
    ${tile("Открытых сигналов", `<a href="#/alerts">${num(d.alerts_open)}</a>`)}
  </div>
  <h2>Лучшие DeFi-кандидаты (предварительная оценка)</h2>
  <div class="card">${tokenTable(d.top, true)}</div>
  <div class="grid g2">
    <div><h2>Покрытие по сетям (новые пулы)</h2><div class="card">${coverageTable(d.coverage)}</div></div>
    <div><h2>Последние сигналы</h2><div class="card">${d.recent_alerts.length ? d.recent_alerts.map(a => `<div class="small"><span class="sev-${esc(a.severity)}">●</span> ${dt(a.created_at)} — ${a.token_uid ? `<a href="#/token/${encodeURIComponent(a.token_uid)}">${esc(a.title)}</a>` : esc(a.title)}</div>`).join("") : '<span class="muted">сигналов пока нет</span>'}</div>
      <h2>Подключения</h2><div class="card">${srcTable(d.sources)}</div></div>
  </div>
  <p class="muted small">Фоновый сбор: ${d.scheduler_enabled ? "включён" : "выключен (включается в «Настройках»)"} · сети: ${esc(d.chains_enabled.join(", "))}</p>`;
}
function coverageTable(cov) {
  if (!cov || !cov.length) return '<span class="muted">нет данных</span>';
  return `<table><tr><th>Сеть</th><th>Запусков</th><th>Непрерывно с</th><th>Последний пул</th><th>Разрывов</th><th>Примечание</th></tr>${cov.map(c => `<tr><td>${esc(c.chain)}</td><td class="num">${num(c.runs)}</td><td>${dt(c.continuous_from)}</td><td>${dt(c.last)}</td><td class="num">${c.gaps ? `<span class="sev-medium">${c.gaps}</span>` : num(c.gaps || 0)}</td><td class="small muted">${esc(c.last_note || "")}</td></tr>`).join("")}</table>
  <div class="small muted">Сеть никогда не считается «полностью просканированной»: показан фактически полученный интервал создания пулов.</div>`;
}
function srcTable(s) {
  if (!s || !s.length) return '<span class="muted">запросов ещё не было</span>';
  return `<table><tr><th>Источник</th><th>Успех</th><th>Ошибка</th><th class="num">Вызовов</th><th class="num">429</th></tr>${s.map(x => `<tr><td>${esc(x.source)}</td><td>${dt(x.last_success_at)}</td><td class="small">${x.last_error_at ? dt(x.last_error_at) + " " + esc((x.last_error || "").slice(0, 80)) : "—"}</td><td class="num">${num(x.calls_total)}</td><td class="num">${num(x.rate_limited_total)}</td></tr>`).join("")}</table>`;
}

/* ---------- таблица токенов ---------- */
const COLS = [
  ["symbol", "Проект / тикер"], ["chain", "Сеть"], ["address", "Контракт"], ["first_trade", "Начало торгов"], ["age_days", "Возраст"],
  ["price", "Цена"], ["mcap", "MCAP"], ["fdv_total", "FDV"], ["liquidity", "Ликвидность"], ["volume", "Объём 24ч"], ["drawdown", "От ATH"],
  ["f_age", "Ф1 Возраст"], ["f_mcap", "Ф2 MCAP"], ["f_drawdown", "Ф3 ATH"], ["f_vc", "Ф4 No-VC"], ["f_utility", "Ф5 DeFi"], ["f_ecosystem", "Ф6 Сеть"],
  ["level", "Ур."], ["status", "Статус"], ["score_preliminary", "Оценка"], ["confidence", "Достов."],
];
let tableState = { sort: null, dir: -1 };
function tokenTable(rows, compact = false) {
  if (!rows || !rows.length) return '<span class="muted">нет записей</span>';
  if (tableState.sort) rows = [...rows].sort((a, b) => { const x = a[tableState.sort], y = b[tableState.sort]; if (x == null) return 1; if (y == null) return -1; return (x > y ? 1 : x < y ? -1 : 0) * tableState.dir; });
  const cols = compact ? COLS.filter(c => !["address", "fdv_total", "volume", "confidence"].includes(c[0])) : COLS;
  return `<table class="tok"><thead><tr>${cols.map(c => `<th data-k="${c[0]}" class="${["price", "mcap", "fdv_total", "liquidity", "volume", "drawdown", "age_days", "score_preliminary"].includes(c[0]) ? "num" : ""}">${c[1]}${tableState.sort === c[0] ? (tableState.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead><tbody>
  ${rows.map(r => `<tr>${cols.map(c => cell(r, c[0])).join("")}</tr>`).join("")}</tbody></table>`;
}
function cell(r, k) {
  const u = `#/token/${encodeURIComponent(r.uid)}`;
  switch (k) {
    case "symbol": return `<td><a href="${u}"><b>${esc(r.symbol || "?")}</b></a> <span class="muted small">${esc((r.name || "").slice(0, 28))}</span>${r.watch ? " ★" : ""}${r.protocol ? `<div class="small sec">↳ ${esc(r.protocol)}</div>` : ""}</td>`;
    case "chain": return `<td class="small">${esc(r.chain_name)}</td>`;
    case "address": return `<td class="addr">${r.explorer ? link(r.explorer, short(r.address)) : esc(short(r.address))}</td>`;
    case "first_trade": return `<td class="small" title="${esc(r.age_basis || "")}">${dt(r.first_trade, false)}</td>`;
    case "age_days": return `<td class="num">${r.age_days == null ? '<span class="muted">н/д</span>' : num(r.age_days, 0) + " дн."}</td>`;
    case "price": return `<td class="num">${usd(r.price)}</td>`;
    case "mcap": return `<td class="num" title="${esc(r.mcap_basis || "circulating не установлен — MCAP не приравнивается к FDV")}">${usd(r.mcap)}</td>`;
    case "fdv_total": return `<td class="num">${usd(r.fdv_total)}</td>`;
    case "liquidity": return `<td class="num">${usd(r.liquidity)}</td>`;
    case "volume": return `<td class="num">${usd(r.volume)}</td>`;
    case "drawdown": return `<td class="num">${r.drawdown == null ? '<span class="muted">н/д</span>' : "−" + r.drawdown.toFixed(0) + "%"}</td>`;
    case "level": return `<td><span class="lvl" title="${esc(META.levels[r.level] || "")}">${esc(r.level)}</span></td>`;
    case "status": return `<td title="${esc(r.status_reason || "")}">${r.last_market_at || r.status !== "UNKNOWN" ? st(r.status) : '<span class="muted small">ожидает анализа</span>'}</td>`;
    case "score_preliminary": return `<td class="num" title="подтверждено ${r.score_confirmed ?? "—"} из проверяемых ${r.score_max_verifiable ?? "—"}">${r.score_preliminary == null ? '<span class="muted">—</span>' : r.score_preliminary.toFixed(0) + `<span class="muted small"> /${(r.score_max_verifiable || 0).toFixed(0)}</span>`}</td>`;
    case "confidence": return `<td class="small">${esc({ confirmed: "подтв.", preliminary: "предв.", low: "низкая" }[r.confidence] || "—")}</td>`;
    default: if (k.startsWith("f_")) return `<td>${st(r[k], r["c_" + k.slice(2)])}</td>`; return `<td>${esc(r[k])}</td>`;
  }
}
function bindSort(container, rerender) {
  container.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => { const k = th.dataset.k; tableState.dir = tableState.sort === k ? -tableState.dir : -1; tableState.sort = k; rerender(); });
}

/* ---------- Сканер ---------- */
let scanQ = { search: "", chain: "", status: "", level: "", show_service: false };
async function vScanner(v) {
  const qs = new URLSearchParams({ ...scanQ, show_service: scanQ.show_service ? "true" : "false", limit: 2000 }).toString();
  const d = await api("/api/tokens?" + qs);
  const chains = Object.entries(META.chains).map(([k, c]) => `<option value="${k}" ${scanQ.chain === k ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const sts = ["PASS", "FAIL", "UNKNOWN", "MANUAL REVIEW", "STALE DATA", "DATA ERROR"].map(s => `<option ${scanQ.status === s ? "selected" : ""}>${s}</option>`).join("");
  v.innerHTML = `<h1>Сканер <span class="muted small">${num(d.total)} токенов${d.total > d.rows.length ? `, показано ${d.rows.length}` : ""}</span></h1>
  <div class="toolbar">
    <input id="q" placeholder="Поиск: тикер, название, адрес" value="${esc(scanQ.search)}" style="min-width:260px">
    <select id="fc"><option value="">Все сети</option>${chains}</select>
    <select id="fs"><option value="">Все статусы</option>${sts}</select>
    <select id="fl"><option value="">Все уровни</option>${["A", "B", "C", "D", "E", "B,C,D,E", "C,D,E"].map(l => `<option ${scanQ.level === l ? "selected" : ""} value="${l}">${l.length > 1 ? l + " (и выше)" : l + " — " + META.levels[l]}</option>`).join("")}</select>
    <label class="small"><input type="checkbox" id="fsv" ${scanQ.show_service ? "checked" : ""}> служебные активы</label>
    <button id="apply">Применить</button>
    <span style="flex:1"></span>
    <button id="addTok">+ Добавить токен</button>
    <a href="/api/export.csv?${qs}"><button>CSV</button></a> <a href="/api/export.xlsx?${qs}"><button>XLSX</button></a>
  </div>
  <div class="small muted" style="margin-bottom:8px">* — предварительный PASS (например, возраст по первой сделке без подтверждённого TGE). Наведите на статус, чтобы увидеть причину; полная аргументация — в карточке.</div>
  <div class="tablewrap" id="tw">${tokenTable(d.rows)}</div>`;
  const rr = () => { $("#tw").innerHTML = tokenTable(d.rows); bindSort($("#tw"), rr); };
  bindSort($("#tw"), rr);
  const apply = () => { scanQ = { search: $("#q").value.trim(), chain: $("#fc").value, status: $("#fs").value, level: $("#fl").value, show_service: $("#fsv").checked }; render(); };
  $("#apply").onclick = apply; $("#q").onkeydown = e => { if (e.key === "Enter") apply(); };
  $("#addTok").onclick = async () => {
    const chain = prompt("Сеть (" + Object.keys(META.chains).join(", ") + "):", "robinhood"); if (!chain) return;
    const address = prompt("Адрес контракта / mint:"); if (!address) return;
    try { const r = await api("/api/tokens/add", { method: "POST", body: { chain, address } }); toast("Токен добавлен, анализ в очереди"); location.hash = "#/token/" + encodeURIComponent(r.uid); } catch (e) { toast("Ошибка: " + e.message); }
  };
}

/* ---------- Карточка проекта ---------- */
async function vToken(v, uid) {
  const d = await api("/api/token/" + encodeURIComponent(uid));
  const f = d.facts || {}, t = d.token, r = d.row, p = f.protocol || {}, oc = f.onchain || {}, sc = f.score || {};
  const fOrder = ["age", "mcap", "drawdown", "vc", "utility", "ecosystem"];
  const ex = d.explorer_addr ? a => link(d.explorer_addr.replace("{address}", a), short(a)) : a => esc(short(a));
  const exTx = d.explorer_tx ? h => link(d.explorer_tx.replace("{tx}", h), short(h)) : h => esc(short(h));
  v.innerHTML = `
  <div class="toolbar"><a href="#/scanner">← Сканер</a><span style="flex:1"></span>
    <button id="wBtn">${t.watch ? "★ В наблюдении (убрать)" : "☆ В наблюдение"}</button>
    <button id="rBtn" class="primary">Обновить данные</button></div>
  <h1>${esc(t.symbol || "?")} <span class="sec">${esc(t.name || "")}</span> ${st(t.status)} <span class="lvl">${esc(t.level)}</span> <span class="small muted">${esc(META.levels[t.level] || "")}</span></h1>
  <div class="small"><span class="addr">${esc(t.address_display || t.address)}</span> · ${esc(META.chains[t.chain].name)} ·
    ${link(d.links.explorer, "обозреватель")} · ${link(d.links.geckoterminal, "GeckoTerminal")} · ${link(d.links.dexscreener, "DEX Screener")} ${d.links.defillama ? "· " + link(d.links.defillama, "DefiLlama") : ""}</div>
  <p><b>Итог:</b> ${esc(t.status_reason || "")}</p>
  <p class="small muted">Обнаружен ${dt(t.first_seen_at)} через <b>${esc(t.discovery_source)}</b>: ${esc(t.discovery_reason || "")}</p>
  ${d.same_symbol.length ? `<div class="warnbox">Тикер <b>${esc(t.symbol)}</b> есть ещё у ${d.same_symbol.length} других контрактов в базе — это РАЗНЫЕ токены, их цены/ATH не смешиваются: ${d.same_symbol.slice(0, 8).map(s => `<a href="#/token/${encodeURIComponent(s.uid)}">${esc(s.chain)}:${esc(short(s.address_display || s.address))}${s.protocol_slug ? " (" + esc(s.protocol_slug) + ")" : ""}</a>`).join(", ")}</div>` : ""}
  ${(d.conflicts || []).filter(c => !c.resolved).map(c => `<div class="warnbox">Конфликт источников (${esc(c.metric)}): ${esc(c.note)} · ${dt(c.detected_at)}</div>`).join("")}
  <div class="tabs" id="tabs">${["Фильтры", "Метрики", "Фундаментал", "Токеномика", "Безопасность", "Оценка", "Пулы", "История", "Ручные данные"].map((x, i) => `<button data-t="${i}" class="${i === 0 ? "active" : ""}">${x}</button>`).join("")}</div>
  <div id="tabc"></div>`;
  $("#wBtn").onclick = async () => { await api("/api/watchlist/" + encodeURIComponent(uid), { method: t.watch ? "DELETE" : "POST", body: {} }); render(); };
  $("#rBtn").onclick = async () => { await api("/api/token/" + encodeURIComponent(uid) + "/refresh", { method: "POST" }); toast("Обновление поставлено в очередь"); pollJobs(); };
  const tabs = [
    () => `<h2>Почему проект подходит или не подходит</h2>${fOrder.map(k => filterBox(d.filters[k], k)).join("")}
      <h2>Что проверить человеку перед решением</h2><ul>${checklist(d).map(x => `<li>${esc(x)}</li>`).join("")}</ul>`,
    () => metricsTab(d, f, r),
    () => fundTab(p, f),
    () => tokenomicsTab(d, f, oc, ex, exTx),
    () => securityTab(oc.security),
    () => scoreTab(sc),
    () => poolsTab(f, d),
    () => historyTab(d),
    () => manualTab(d, uid),
  ];
  const show = i => { clearCharts(); document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", +b.dataset.t === i)); $("#tabc").innerHTML = tabs[i](); afterTab(i, d, f, uid); };
  document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => show(+b.dataset.t));
  show(0);
}
function filterBox(fr, k) {
  if (!fr) return `<div class="filterbox UNKNOWN"><b>${esc(META.filters[k])}</b> — не оценивался</div>`;
  const ev = fr.evidence || [];
  return `<div class="filterbox ${esc(fr.status)}"><b>${esc(META.filters[k])}</b> ${st(fr.status, fr.confidence)} <span class="small muted">${dt(fr.evaluated_at)}</span>
    <div>${esc(fr.reason)}</div>
    ${ev.length ? `<ul class="ev">${ev.map(e => `<li>${esc(e.label)}: <b>${typeof e.value === "number" ? (Math.abs(e.value) > 1000 ? usd(e.value) : esc(e.value)) : esc(e.value)}</b>${e.at ? " · " + dt(e.at) : ""}${e.source ? ` <span class="muted">[${esc(e.source)}]</span>` : ""}${e.url ? " " + link(e.url, "проверить") : ""}${e.note ? ` <span class="muted">— ${esc(e.note)}</span>` : ""}</li>`).join("")}</ul>` : ""}</div>`;
}
function checklist(d) {
  const out = [];
  Object.values(d.filters).forEach(fr => { if (fr.status !== "PASS" || fr.confidence === "preliminary") out.push(`${META.filters[fr.filter_key]}: ${fr.reason}`); });
  const f = d.facts || {};
  out.push("Сверить адрес контракта с официальным сайтом/документацией и соцсетями проекта.");
  if (!(f.onchain || {}).security) out.push("Ончейн-проверка безопасности не выполнялась.");
  out.push("Проверить аудит, права администраторов, мультисиг/таймлок.");
  out.push("Проверить, получает ли свободный (незаблокированный) держатель экономическую ценность.");
  out.push("Проверить предстоящие unlock и фактическую эмиссию за последние недели.");
  return out;
}
function metricsTab(d, f, r) {
  const cands = f.mcap_candidates || [];
  return `<div class="grid g2"><div class="card"><h3>Цена и капитализация</h3><div class="kv">
    <div>Цена</div><div>${usd((f.price || {}).value)} <span class="small muted">${esc((f.price || {}).method || "")}</span></div>
    ${cands.map(c => `<div>${esc(c.label)}</div><div>${usd(c.value)} <span class="small muted">[${esc(c.confidence)}]</span> ${c.url ? link(c.url, "источник") : ""}</div>`).join("") || "<div>Circulating MCAP</div><div class='muted'>не установлен — MCAP не приравнивается к FDV</div>"}
    <div>FDV по totalSupply</div><div>${usd((f.fdv_total || {}).value)} <span class="small muted">${esc((f.fdv_total || {}).supply_source || "")}</span></div>
    <div>FDV по max supply</div><div>${usd((f.fdv_max || {}).value)} <span class="small muted">${esc((f.fdv_max || {}).supply_source || "max supply не задан")}</span></div>
    <div>Total supply</div><div>${num((f.total_supply || {}).value)} <span class="small muted">${esc((f.total_supply || {}).source || "")}</span></div>
    <div>Circulating (офиц.)</div><div>${num((f.circulating_official || {}).value)} ${(f.circulating_official || {}).url ? link(f.circulating_official.url, "эндпоинт") : ""}</div>
    <div>Max supply</div><div>${num((f.max_supply || {}).value)} <span class="small muted">${esc((f.max_supply || {}).version || "")}</span></div>
  </div></div>
  <div class="card"><h3>Рынок</h3><div class="kv">
    <div>Ликвидность (качественные пулы)</div><div>${usd((f.pools || {}).liquidity_quality_usd)} <span class="small muted">из ${usd((f.pools || {}).liquidity_total_usd)} во всех пулах</span></div>
    <div>Объём 24ч (GT)</div><div>${usd((f.gt || {}).volume_h24)}</div>
    <div>ATH (без аномальных хвостов)</div><div>${usd((f.ath || {}).ath_usd)} · ${dt((f.ath || {}).ath_at, false)}</div>
    <div>Макс. дневное закрытие</div><div>${usd((f.ath || {}).ath_close_usd)} · ${dt((f.ath || {}).ath_close_at, false)}</div>
    <div>Макс. свечи (сырой)</div><div>${usd((f.ath || {}).ath_raw_high_usd)}</div>
    <div>Просадка</div><div>${r.drawdown == null ? "н/д" : "−" + r.drawdown + "%"}</div>
    <div>Первая сделка</div><div>${dt((f.ath || {}).first_trade_at, false)} <span class="small muted">точность — сутки</span></div>
    <div>Самый ранний известный пул</div><div>${dt((f.earliest_pool || {}).at)} <span class="small muted">${esc((f.earliest_pool || {}).dex || "")}</span></div>
    <div>Держатели (GT)</div><div>${num((((f.gt_info || {}).holders) || {}).count)}</div>
  </div>${((f.ath || {}).wick_anomalies || []).length ? `<div class="small muted">Отброшено аномальных хвостов свечей: ${f.ath.wick_anomalies.length} (напр. ${dt(f.ath.wick_anomalies[0].at, false)}, high в ${f.ath.wick_anomalies[0].ratio}× выше тела)</div>` : ""}</div></div>
  <h2>Цена (дневные закрытия по качественным пулам)</h2><div class="card"><div class="chartbox"><canvas id="chPrice"></canvas></div><div class="small muted">Каждая линия — отдельный пул. Пропуски не заполняются искусственно.</div></div>
  ${linksBlock(f)}
  ${(f.gt_info || {}).description_untrusted ? `<h2>Описание проекта <span class="small muted">(недоверенный текст из профиля GeckoTerminal)</span></h2><div class="card small sec">${esc(f.gt_info.description_untrusted)}</div>` : ""}`;
}
function linksBlock(f) {
  const l = f.links || {}, g = f.gt_info || {};
  const w = [...new Set([...(l.websites || []), ...(g.websites || [])])];
  if (!w.length && !(l.socials || []).length) return "";
  return `<h2>Ссылки проекта <span class="small muted">(недоверенные данные профилей, требуют проверки)</span></h2><div class="card small">${w.map(x => link(x)).join(" · ")} ${(l.socials || []).map(s => link(s.url, s.type)).join(" · ")}</div>`;
}
function fundTab(p, f) {
  const link_ = (f.protocol_link || {}).linked;
  const poss = (f.protocol_link || {}).possible || [];
  if (!link_) return `<div class="card">Связь токена с DeFi-протоколом <b>не подтверждена</b>. Совпадение названия не используется как доказательство.
    ${poss.length ? `<h3>Возможные совпадения по тикеру (НЕ подтверждены)</h3><ul>${poss.map(x => `<li>${esc(x.name)} (${esc(x.category)}) — адрес в DefiLlama: <span class="addr">${esc(x.address || "не указан")}</span></li>`).join("")}</ul>` : ""}
    <p class="small muted">Связь можно установить вручную во вкладке «Ручные данные» (protocol_link) с доказательством.</p></div>`;
  const fe = p.fees || {}, rv = p.revenue || {}, hr = p.holders_revenue || {}, dv = p.dex_volume || {}, cs = p.category_share;
  const row = (l, x) => `<div>${l}</div><div>${x}</div>`;
  return `<div class="card"><b>${esc(p.name || link_.name)}</b> · ${esc(p.category || "")} · ${link(p.verify_url, "DefiLlama")}<div class="small muted">Связь: ${esc(link_.method)} — ${esc(link_.evidence)}</div>
    <div class="small sec" style="margin-top:6px">${esc(p.description || "")}</div></div>
  <div class="grid g2"><div class="card"><h3>Активность</h3><div class="kv">
    ${row("TVL", usd(p.tvl_usd))}${row("TVL 7д / 30д / 90д", `${pct(p.tvl_change_7d)} / ${pct(p.tvl_change_30d)} / ${pct(p.tvl_change_90d)}`)}
    ${row("Возраст протокола", `листинг DefiLlama ${dt(p.listed_at, false)}, первый TVL ${dt(p.first_tvl_at, false)}`)}
    ${p.borrowed_usd ? row("Займы (borrowed)", usd(p.borrowed_usd)) + row("Депозиты (TVL+borrowed)", usd(p.deposits_usd)) + row("Утилизация", pct(p.utilization_pct).replace("+", "")) : ""}
    ${row("Стейкинг", usd(p.staking_usd))}
    ${row("Доля в категории на сети", cs ? `${cs.share_pct.toFixed(1)}% (место ${cs.rank} из ${cs.of})` : "н/д")}
    ${row("Аудиты (DefiLlama)", esc(p.audits ?? "н/д"))}${row("Раунды (DefiLlama raises)", (p.raises || []).length ? esc(JSON.stringify(p.raises)) : "нет записей (не доказательство отсутствия VC)")}
  </div></div>
  <div class="card"><h3>Экономика (30 дней)</h3><div class="kv">
    ${row("Комиссии пользователей (fees)", fe.available ? usd(fe.total30d) + ` · 7д ${usd(fe.total7d)}` : '<span class="muted">нет данных у DefiLlama (не ноль)</span>')}
    ${row("Выручка протокола (revenue)", rv.available ? usd(rv.total30d) : '<span class="muted">нет данных</span>')}
    ${row("Выплаты держателям (holders revenue)", hr.available ? usd(hr.total30d) : '<span class="muted">нет данных</span>')}
    ${row("DEX-объём", dv.available ? usd(dv.total30d) : '<span class="muted">н/д</span>')}
    ${row("Комиссии/TVL годовых", p.fees_to_tvl_annual_pct == null ? "н/д" : p.fees_to_tvl_annual_pct.toFixed(1) + "%")}
    ${row("Объём/TVL (30д)", p.volume_to_tvl_30d == null ? "н/д" : p.volume_to_tvl_30d.toFixed(2))}
    ${row("Эффективная ставка комиссии", p.effective_fee_rate_pct == null ? "н/д" : p.effective_fee_rate_pct.toFixed(3) + "%")}
    ${row("Выручка годовых / MCAP", p.revenue_annual_to_mcap_pct == null ? "н/д" : p.revenue_annual_to_mcap_pct.toFixed(1) + "%")}
    ${row("Holders revenue годовых / MCAP", p.holders_revenue_annual_to_mcap_pct == null ? "н/д" : p.holders_revenue_annual_to_mcap_pct.toFixed(1) + "%")}
  </div><div class="small muted">Комиссии пользователей ≠ выручка протокола ≠ выплаты держателям. Методология DefiLlama ниже.</div></div></div>
  <h2>TVL протокола</h2><div class="card"><div class="chartbox"><canvas id="chTvl"></canvas></div></div>
  <h2>Комиссии и выручка по дням</h2><div class="card"><div class="chartbox"><canvas id="chFees"></canvas></div></div>
  ${fe.methodology ? `<details><summary>Методология DefiLlama</summary><div class="kv small">${Object.entries(fe.methodology).map(([k, v]) => `<div>${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div></details>` : ""}`;
}
function tokenomicsTab(d, f, oc, ex, exTx) {
  const an = oc.analytics || {}, rec = oc.reconciliation;
  return `<div class="grid g2"><div class="card"><h3>Ончейн-предложение</h3><div class="kv">
    <div>totalSupply() сейчас</div><div>${num(oc.total_supply, 2)}</div>
    <div>Сумма mint / burn (найдено)</div><div>${num(oc.minted, 2)} / ${num(oc.burned, 2)} (${num(oc.events)} событий)</div>
    <div>Сканированы блоки</div><div>${num(oc.scanned_from_block)} → ${num(oc.scanned_to_block)} (голова ${num(oc.head)})</div>
    <div>Сверка</div><div>${rec ? (rec.matches === true ? '<span class="st PASS">сходится</span> ' : rec.matches === false ? '<span class="st UNKNOWN">не сходится</span> ' : '<span class="st UNKNOWN">сканирование идёт</span> ') + esc(rec.note) : '<span class="muted">не выполнялась</span>'}</div>
    <div>Первый mint</div><div>${oc.first_mint ? dt(oc.first_mint.ts) + " (блок " + num(oc.first_mint.block) + ", время " + (oc.first_mint.ts_source === "block" ? "точное" : "≈") + ") · " + exTx(oc.first_mint.tx) : esc(oc.first_mint_note || "н/д")}</div>
  </div>${oc.error ? `<div class="warnbox">${esc(oc.error)}</div>` : ""}</div>
  <div class="card"><h3>Версии токеномики</h3>${d.tokenomics.length ? d.tokenomics.map(v => `<div class="filterbox ${v.is_current ? "PASS" : ""}"><b>${esc(v.version_label)}</b> ${v.is_current ? '<span class="st PASS">актуальная</span>' : `<button class="small" data-cur="${v.id}">сделать актуальной</button>`}
      <div class="small">опубликовано ${esc(v.published_at || "н/д")} · источник: ${link(v.source_url, v.source_type)}</div><div class="small muted">${esc(v.note || "")}</div>
      <details><summary>параметры</summary><pre class="mono">${esc(JSON.stringify(v.params, null, 1))}</pre></details></div>`).join("") : '<span class="muted">версий нет — добавьте во вкладке «Ручные данные»</span>'}
    <div class="small muted">Бюджет эмиссии — это потолок, а не выпущенное количество.</div></div></div>
  <h2>Выпуск по дням (mint) и накопленное предложение из событий</h2>
  <div class="grid g2"><div class="card"><div class="chartbox"><canvas id="chMint"></canvas></div></div><div class="card"><div class="chartbox"><canvas id="chCum"></canvas></div></div></div>
  ${(an.budget_comparison || []).length ? `<h2>Заявленный бюджет vs фактический mint</h2><div class="card"><table><tr><th>Неделя</th><th>Начало</th><th class="num">Бюджет</th><th class="num">Mint в контракты эмиссии*</th><th class="num">Все mint</th><th>Статус</th></tr>
    ${an.budget_comparison.map(w => `<tr><td>${esc(w.week)}</td><td>${dt(w.start)}<div class="small muted">${w.start_block ? "блоки " + num(w.start_block) + " → " + (w.end_block ? num(w.end_block) : "…") : ""}</div></td><td class="num">${num(w.budget)}</td><td class="num">${num(w.minted_to_emission_contracts)}</td><td class="num">${num(w.minted_all)}</td><td>${w.in_progress ? "идёт" : !w.boundary_source ? '<span class="muted">не проверено: границы недели не подтверждены временем блоков</span>' : w.labels_missing ? '<span class="muted">не проверено: нет меток адресов эмиссии</span>' : w.over_budget ? st("FAIL") + " выше бюджета" : "в пределах"}</td></tr>`).join("")}</table>
    <div class="small muted">* только адреса с метками вида «gauge/emissions». Без меток адресов сравнение неполное. ${esc((an.budget_comparison[0] || {}).note || "")}</div></div>` : ""}
  <h2>Крупнейшие получатели mint</h2><div class="card"><table><tr><th>Адрес</th><th class="num">Получено</th><th>Метка</th><th>Источник метки</th></tr>
    ${(an.top_recipients || []).map(x => `<tr><td class="addr">${ex(x.address)}</td><td class="num">${num(x.amount, 0)}</td><td>${esc(x.label || "")} ${x.kind ? `<span class="muted">(${esc(x.kind)})</span>` : ""}</td><td class="small muted">${esc(x.label_source || x.note || "")}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">нет данных</td></tr>'}</table>
    <div class="small muted">Один адрес ≠ один человек. Неизвестный адрес не считается адресом команды без доказательств.</div></div>
  <h2>Последние события выпуска/сжигания</h2><div class="card" style="max-height:340px;overflow:auto"><table><tr><th>Тип</th><th title="интерполяция по опорным блокам, точность — секунды/минуты">Время ≈</th><th>Блок</th><th>Контрагент</th><th class="num">Количество</th><th>Транзакция</th></tr>
    ${d.supply_events.slice(0, 120).map(e => `<tr><td>${e.kind === "mint" ? "mint" : "burn"}</td><td>${dt(e.ts)}${e.ts_source === "interpolated" ? ' <span class="muted small" title="интерполяция между опорными блоками">≈</span>' : ""}</td><td class="num">${num(e.block)}</td><td class="addr">${ex(e.counterparty)}</td><td class="num">${num(e.amount, 2)}</td><td class="addr">${exTx(e.tx_hash)}</td></tr>`).join("")}</table></div>`;
}
function securityTab(sec) {
  if (!sec) return `<div class="card muted">Проверка безопасности ещё не выполнялась (выполняется для уровня C и выше или по кнопке «Обновить данные» для токенов в наблюдении).</div>`;
  return `<div class="card"><p class="small warnbox">${esc(sec.note || "Индикаторы для ручной проверки.")}</p>
    ${sec.error ? `<div class="warnbox">${esc(sec.error)}</div>` : ""}
    <table><tr><th>Индикатор</th><th>Оценка</th><th>Описание</th></tr>${(sec.indicators || []).map(i => `<tr><td>${esc(i.key)}</td><td>${i.status === "risk" ? '<span class="st FAIL">риск</span>' : i.status === "ok" ? '<span class="st PASS">ok</span>' : '<span class="st UNKNOWN">инфо</span>'}</td><td>${esc(i.text)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">флагов не обнаружено — это НЕ означает безопасность</td></tr>'}</table>
    <h3>Не проверено (UNKNOWN)</h3><ul class="small">${(sec.unknown || []).map(u => `<li>${esc(u)}</li>`).join("")}</ul>
    <div class="small muted">Размер байткода: ${num(sec.bytecode_size)} байт ${sec.implementation ? "· реализация " + esc(sec.implementation) : ""} · ${dt(sec.at)}</div></div>`;
}
function scoreTab(sc) {
  if (!sc || !sc.criteria) return '<div class="card muted">Оценка ещё не рассчитана</div>';
  return `<div class="grid g4">
    <div class="card tile"><div class="l">Подтверждённые баллы</div><div class="v">${sc.confirmed}</div><div class="s">из проверяемых ${sc.max_verifiable} (из 100)</div></div>
    <div class="card tile"><div class="l">Предварительная оценка</div><div class="v">${sc.preliminary == null ? "—" : sc.preliminary.toFixed(0)}</div><div class="s">не сопоставимый рейтинг</div></div>
    <div class="card tile"><div class="l">Покрытие данными</div><div class="v">${sc.coverage_pct}%</div><div class="s">неопределённость: ${esc(sc.uncertainty)}</div></div></div>
  ${sc.blocking_risks.length ? `<h2>Блокирующие риски</h2><ul>${sc.blocking_risks.map(b => `<li class="sev-high">${esc(b)}</li>`).join("")}</ul>` : ""}
  <h2>Матрица (100 баллов)</h2>${sc.criteria.map(c => `<div class="card crit"><b>${esc(c.name)}</b> — ${c.awarded} / ${c.max} <span class="small muted">(проверено пунктов на ${c.checked_max} б.)</span>
    <div class="bar"><i style="width:${c.awarded / c.max * 100}%"></i></div>
    <ul class="small">${c.items.map(i => `<li class="${i.status === "awarded" ? "item-aw" : i.status === "unverified" ? "item-unv" : "item-na"}">${i.status === "awarded" ? "✓" : i.status === "unverified" ? "?" : "✕"} ${esc(i.label)} (${i.points}/${i.max}) — ${esc(i.reason)}</li>`).join("")}</ul></div>`).join("")}
  <p class="small muted">${esc(sc.disclaimer)}</p>`;
}
function poolsTab(f, d) {
  const items = (f.pools || {}).items || [];
  return `<div class="card"><div class="small muted">${esc((f.pools || {}).note || "")}</div><table><tr><th>Пул</th><th>DEX</th><th>Сторона</th><th>Контр-актив</th><th class="num">Цена токена</th><th class="num">Ликвидность</th><th>Создан</th><th>Источник</th><th>Качество</th></tr>
  ${items.map(p => `<tr><td class="addr">${esc(short(p.address))}</td><td>${esc(p.dex)}</td><td>${esc(p.side || "—")}</td><td>${esc(p.counter_symbol || "")}</td><td class="num">${usd(p.price_usd)}</td><td class="num">${usd(p.liquidity_usd)}</td><td class="small">${dt(p.created_at)}</td><td class="small">${esc(p.source)}${p.also_in ? "+" + esc(p.also_in.join(",")) : ""}</td>
    <td class="small">${p.quality ? '<span class="st PASS">исп.</span>' : '<span class="muted">' + esc((p.excluded_reasons || []).join("; ")) + "</span>"}</td></tr>`).join("") || '<tr><td colspan="9" class="muted">пулы ещё не анализировались</td></tr>'}</table></div>
  <h2>Все известные пулы токена в базе (${d.pools.length})</h2><div class="card" style="max-height:300px;overflow:auto"><table><tr><th>Пул</th><th>DEX</th><th>Создан</th><th>Обнаружен</th><th>Источники</th></tr>${d.pools.map(p => `<tr><td class="addr">${esc(short(p.address))}</td><td>${esc(p.dex || "")}</td><td>${dt(p.pool_created_at)}</td><td>${dt(p.first_seen_at)}</td><td class="small">${esc(p.sources)}</td></tr>`).join("")}</table>
  <div class="small muted">Создание нового пула НЕ означает, что токен новый: возраст считается по самому раннему пулу/сделке/TGE.</div></div>`;
}
function historyTab(d) {
  return `<div class="grid g2"><div class="card"><h3>Изменения статусов</h3><table><tr><th>Время</th><th>Поле</th><th>Было → стало</th><th>Причина</th></tr>${d.status_history.map(h => `<tr><td class="small">${dt(h.at)}</td><td>${esc(h.field)}</td><td>${esc(h.old_value)} → <b>${esc(h.new_value)}</b></td><td class="small">${esc((h.reason || "").slice(0, 160))}</td></tr>`).join("")}</table></div>
  <div class="card"><h3>История решений фильтров</h3><table><tr><th>Время</th><th>Фильтр</th><th>Статус</th><th>Причина</th></tr>${d.filter_history.map(h => `<tr><td class="small">${dt(h.evaluated_at)}</td><td>${esc(META.filters[h.filter_key])}</td><td>${st(h.status, h.confidence)}</td><td class="small">${esc(h.reason)}</td></tr>`).join("")}</table></div></div>
  <h2>Сигналы проекта</h2><div class="card">${alertsTable(d.alerts)}</div>
  <h2>Ликвидность качественных пулов (снимки)</h2><div class="card"><div class="chartbox"><canvas id="chLiq"></canvas></div><div class="small muted">Точки — фактические снимки; график не интерполируется.</div></div>`;
}
function manualTab(d, uid) {
  const cats = Object.entries(META.funding_categories).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  return `<div class="card"><h3>Ручные факты и доказательства</h3><table><tr><th>Тип</th><th>Значение</th><th>Категория</th><th>Источник</th><th>Примечание</th><th>Кто/когда</th><th></th></tr>
    ${d.manual_facts.map(m => `<tr class="${m.active ? "" : "muted"}"><td>${esc(m.fact_key)}</td><td>${esc(m.value)}</td><td>${esc(META.funding_categories[m.category] || m.category || "")}</td><td>${link(m.evidence_url, m.source_type)}</td><td class="small">${esc(m.note)}</td><td class="small">${esc(m.entered_by)} ${dt(m.entered_at)}</td><td>${m.active ? `<button class="small" data-deact="${m.id}">отключить</button>` : "откл."}</td></tr>`).join("")}</table></div>
  <div class="card"><h3>Добавить факт</h3><form class="inline" id="factForm">
    <label>Тип<select name="fact_key">
      <option value="funding">Финансирование / VC</option><option value="tge_date">Дата TGE (ISO, UTC)</option><option value="utility">Утилити (PASS/FAIL)</option>
      <option value="circulating_supply">Circulating supply (число)</option><option value="supply_endpoint_circulating">Эндпоинт circulating (URL)</option>
      <option value="protocol_link">Связь с протоколом (slug DefiLlama)</option><option value="value_capture">Ценность для свободного держателя (yes/no)</option>
      <option value="vesting_documented">Вестинг команды документирован (yes/no)</option><option value="audit">Аудит подтверждён (yes/no)</option>
      <option value="team">Команда проверена (yes/no)</option><option value="catalyst">Катализатор</option><option value="official_link">Официальная ссылка</option><option value="note">Заметка</option></select></label>
    <label>Значение<input name="value"></label>
    <label>Категория финансирования<select name="category"><option value=""></option>${cats}</select></label>
    <label>Ссылка-доказательство<input name="evidence_url" placeholder="https://"></label>
    <label>Тип источника<select name="source_type"><option value="official_docs">официальная документация</option><option value="official_social">официальные соцсети</option><option value="onchain">ончейн</option><option value="third_party">сторонний источник</option><option value="user">мой вывод</option></select></label>
    <label>Примечание<input name="note"></label><button class="primary">Сохранить</button></form>
    <div class="small muted">PASS по No-VC ставится только категорией «Отсутствие внешнего финансирования задокументировано» с обязательной ссылкой. Ангел/грант/публичная продажа не приравниваются к VC.</div></div>
  <div class="card"><h3>Новая версия токеномики</h3><form class="inline" id="tnForm">
    <label>Название версии<input name="version_label" required></label><label>Дата публикации<input name="published_at" placeholder="2026-10-20"></label>
    <label>Источник (URL)<input name="source_url"></label><label>Тип источника<select name="source_type"><option value="official_docs">официальная документация</option><option value="official_social">официальные соцсети</option><option value="third_party">сторонний</option></select></label>
    <label>Актуальная<select name="is_current"><option value="1">да</option><option value="0">нет</option></select></label>
    <label style="grid-column:1/-1">Параметры (JSON: max_supply, weekly_budgets[{week,start,amount}], unlocks[{date,amount,note}] …)<textarea name="params">{"max_supply": null}</textarea></label>
    <label style="grid-column:1/-1">Примечание<input name="note"></label><button class="primary">Добавить версию</button></form></div>
  <div class="card"><h3>Метка адреса</h3><form class="inline" id="lbForm"><label>Адрес<input name="address" required></label><label>Метка<input name="label" required></label>
    <label>Вид<select name="kind"><option>escrow</option><option>treasury</option><option>team</option><option>gauge</option><option>emissions</option><option>vesting</option><option>cex</option><option>dex_pool</option><option>minter</option><option>other</option></select></label>
    <label>Доказательство (URL)<input name="source_url"></label><button class="primary">Сохранить</button></form></div>`;
}
function afterTab(i, d, f, uid) {
  const tsDay = ts => ts * 1000;
  if (i === 1 && f.price_history) {
    const ds = Object.entries(f.price_history).slice(0, 3).map(([pool, rows], k) => ({ label: "пул " + short(pool), data: rows.map(r => ({ x: tsDay(r[0]), y: r[1] })) }));
    if (ds.length && $("#chPrice")) lineChart($("#chPrice"), ds, { fmt: v => "$" + Number(v).toPrecision(4), fmtAxis: fmtUsdPlain });
  }
  if (i === 2) {
    const p = f.protocol || {};
    if (p.tvl_series && $("#chTvl")) lineChart($("#chTvl"), [{ label: "TVL", data: p.tvl_series.map(r => ({ x: tsDay(r[0]), y: r[1] })) }], { fmt: fmtUsdPlain, fmtAxis: fmtUsdPlain });
    const fe = (p.fees || {}).chart, rv = (p.revenue || {}).chart;
    if (fe && $("#chFees")) {
      const ds = [{ label: "Комиссии", data: fe.map(r => ({ x: tsDay(r[0]), y: r[1] })) }];
      if (rv) ds.push({ label: "Выручка протокола", data: rv.map(r => ({ x: tsDay(r[0]), y: r[1] })) });
      lineChart($("#chFees"), ds, { fmt: fmtUsdPlain, fmtAxis: fmtUsdPlain });
    }
  }
  if (i === 3) {
    document.querySelectorAll("[data-cur]").forEach(b => b.onclick = async () => { await api(`/api/tokenomics/${b.dataset.cur}/current`, { method: "POST" }); render(); });
    const daily = ((f.onchain || {}).analytics || {}).daily || [];
    const tsd = s => new Date(s + "T00:00:00Z").getTime();
    if (daily.length && $("#chMint")) {
      lineChart($("#chMint"), [{ label: "Выпущено за день (mint)", data: daily.map(x => ({ x: tsd(x.date), y: x.mint })), barPercentage: 0.8 }], { type: "bar", fmt: fmtNum, fmtAxis: fmtNum });
      lineChart($("#chCum"), [{ label: "Накоплено с начала сканирования (mint − burn)", data: daily.map(x => ({ x: tsd(x.date), y: x.cumulative })) }], { fmt: fmtNum, fmtAxis: fmtNum });
    }
  }
  if (i === 7) {
    const s = (d.series || {}).liquidity_usd_quality || [];
    if (s.length && $("#chLiq")) lineChart($("#chLiq"), [{ label: "Ликвидность", data: s.map(x => ({ x: new Date(x.observed_at || x.fetched_at).getTime(), y: x.value })), pointRadius: 3 }], { fmt: fmtUsdPlain, fmtAxis: fmtUsdPlain });
  }
  if (i === 8) {
    document.querySelectorAll("[data-deact]").forEach(b => b.onclick = async () => { await api(`/api/facts/${b.dataset.deact}/deactivate`, { method: "POST" }); render(); });
    const fd = form => Object.fromEntries(new FormData(form).entries());
    $("#factForm").onsubmit = async e => { e.preventDefault(); try { await api(`/api/token/${encodeURIComponent(uid)}/facts`, { method: "POST", body: fd(e.target) }); toast("Сохранено, фильтры пересчитаны"); render(); } catch (er) { toast("Ошибка: " + er.message); } };
    $("#tnForm").onsubmit = async e => { e.preventDefault(); const b = fd(e.target); b.is_current = b.is_current === "1"; try { b.params = JSON.parse(b.params || "{}"); } catch { toast("Параметры должны быть корректным JSON"); return; } try { await api(`/api/token/${encodeURIComponent(uid)}/tokenomics`, { method: "POST", body: b }); toast("Версия добавлена"); render(); } catch (er) { toast("Ошибка: " + er.message); } };
    $("#lbForm").onsubmit = async e => { e.preventDefault(); const b = fd(e.target); b.chain = d.token.chain; b.token_uid = uid; await api("/api/labels", { method: "POST", body: b }); toast("Метка сохранена"); };
  }
}

/* ---------- Ручная проверка ---------- */
async function vReview(v) {
  const d = await api("/api/review");
  v.innerHTML = `<h1>Очередь ручной проверки <span class="muted small">${d.total}</span></h1>
  <p class="sec small">Здесь кандидаты без FAIL, у которых часть критериев UNKNOWN (чаще всего No-VC). UNKNOWN ≠ FAIL: проект не скрыт, но и не считается прошедшим.</p>
  <div class="tablewrap"><table><tr><th>Проект</th><th>Сеть</th><th>Ур.</th><th>Оценка</th><th>Что проверить</th></tr>
  ${d.rows.map(r => `<tr><td><a href="#/token/${encodeURIComponent(r.uid)}"><b>${esc(r.symbol)}</b></a> <span class="muted small">${esc(r.name || "")}</span></td><td>${esc(r.chain_name)}</td><td><span class="lvl">${esc(r.level)}</span></td><td class="num">${r.score_preliminary == null ? "—" : r.score_preliminary.toFixed(0)}</td><td class="small">${r.needs.map(n => `<div><b>${esc(n.filter)}</b>: ${esc(n.reason)}</div>`).join("")}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">очередь пуста</td></tr>'}</table></div>`;
}

/* ---------- Наблюдение ---------- */
async function vWatch(v) {
  const d = await api("/api/watchlist");
  v.innerHTML = `<h1>Список наблюдения <span class="muted small">${d.total}</span></h1>
  <p class="small sec">Проекты для углублённого ручного исследования. Добавляются кнопкой «☆ В наблюдение» в карточке. Для них регулярно обновляются данные и ончейн-проверки.</p>
  <div class="tablewrap" id="tw">${tokenTable(d.rows)}</div>`;
  const rr = () => { $("#tw").innerHTML = tokenTable(d.rows); bindSort($("#tw"), rr); }; bindSort($("#tw"), rr);
}

/* ---------- Токеномика ---------- */
async function vTokenomics(v) {
  const d = await api("/api/tokenomics");
  v.innerHTML = `<h1>Токеномика и эмиссия</h1><p class="small sec">Токены с версиями токеномики или ончейн-данными о выпуске. Детали — во вкладке «Токеномика» карточки проекта.</p>
  <div class="card"><table><tr><th>Токен</th><th>Сеть</th><th class="num">totalSupply</th><th class="num">Circulating (офиц.)</th><th class="num">Max supply</th><th class="num">Mint-событий</th><th class="num">Выпущено (найдено)</th><th class="num">Сожжено</th><th>Сверка с totalSupply</th><th>Версий</th><th>Обновлено</th></tr>
  ${d.rows.map(r => `<tr><td><a href="#/token/${encodeURIComponent(r.uid)}"><b>${esc(r.symbol)}</b></a></td><td>${esc(r.chain)}</td><td class="num">${num(r.total_supply)}</td><td class="num">${num(r.circulating)}</td><td class="num">${num(r.max_supply)}</td><td class="num">${num(r.mints)}</td><td class="num">${num(r.minted)}</td><td class="num">${num(r.burned)}</td><td>${r.reconciliation ? (r.reconciliation.matches === true ? st("PASS") : st("UNKNOWN") + ` <span class="small muted">${r.reconciliation.matches === false ? "не сходится" : "сканирование не завершено"}</span>`) : '<span class="muted">—</span>'}</td><td class="num">${num(r.versions)}</td><td class="small">${dt(r.last_onchain_at)}</td></tr>`).join("") || '<tr><td colspan="11" class="muted">нет данных</td></tr>'}</table></div>`;
}

/* ---------- Сигналы ---------- */
function alertsTable(rows) {
  if (!rows || !rows.length) return '<span class="muted">сигналов нет</span>';
  return `<table><tr><th>Время</th><th>Важность</th><th>Тип</th><th>Сигнал</th><th>Условие</th><th>Значения</th><th>Источники / ограничения</th><th></th></tr>${rows.map(a => `<tr class="${a.acknowledged ? "muted" : ""}"><td class="small">${dt(a.created_at)}</td><td class="sev-${esc(a.severity)}">${esc({ high: "высокая", medium: "средняя", low: "низкая", info: "инфо" }[a.severity] || a.severity)}</td><td class="small">${esc(a.alert_type)}</td>
    <td>${a.token_uid ? `<a href="#/token/${encodeURIComponent(a.token_uid)}">${esc(a.title)}</a>` : esc(a.title)}</td><td class="small">${esc(a.condition)}</td><td class="small mono">${esc((a.values_json || "").slice(0, 160))}</td><td class="small">${esc(a.sources_json)} ${a.limitations ? `<div class="muted">${esc(a.limitations)}</div>` : ""}</td><td>${a.acknowledged ? "" : `<button class="small" data-ack="${a.id}">прочитано</button>`}</td></tr>`).join("")}</table>`;
}
async function vAlerts(v) {
  const d = await api("/api/alerts");
  v.innerHTML = `<h1>Журнал сигналов</h1><div class="card">${alertsTable(d.rows)}</div>`;
  document.querySelectorAll("[data-ack]").forEach(b => b.onclick = async () => { await api(`/api/alerts/${b.dataset.ack}/ack`, { method: "POST" }); render(); pollJobs(); });
}

/* ---------- Источники ---------- */
async function vSources(v) {
  const d = await api("/api/sources");
  v.innerHTML = `<h1>Источники данных</h1>
  <h2>Сети</h2><div class="card"><table><tr><th>Сеть</th><th>Поддержка</th><th>Включена</th><th>Провайдеры</th><th class="num">Токенов</th><th>TVL сети</th><th>Последний успешный сбор</th><th>Ограничения</th></tr>
  ${d.chains.map(c => `<tr><td>${esc(c.name)}</td><td>${esc({ supported: "поддерживается", experimental: "экспериментально", planned: "план" }[c.support])}${c.kind === "solana" ? " (отдельный адаптер)" : ""}</td><td>${c.enabled ? "да" : "нет"}</td><td class="small">${esc(c.providers.join(", "))}</td><td class="num">${num(c.tokens)}</td><td>${c.health ? usd(c.health.tvl_usd) + ` <span class="small muted">30д ${pct(c.health.tvl_change_30d, 0)}</span>` : '<span class="muted">н/д</span>'}</td><td>${dt(c.last_success)}</td><td class="small muted">${esc(c.notes)}</td></tr>`).join("")}</table></div>
  <h2>Покрытие новых пулов</h2><div class="card">${coverageTable(d.coverage)}</div>
  <h2>Состояние API</h2><div class="card">${srcTable(d.sources)}<div class="small muted">Лимиты (запросов/мин): ${esc(JSON.stringify(d.rate_limits))}. Частота заданий автоматически ограничена этими лимитами.</div></div>
  <h2>Журнал запусков обнаружения</h2><div class="card" style="max-height:360px;overflow:auto"><table><tr><th>Время</th><th>Сеть</th><th>Источник</th><th class="num">Стр.</th><th class="num">Пулов</th><th>Окно создания пулов</th><th>Непрерывно</th><th>Примечание</th></tr>
  ${d.coverage_log.map(c => `<tr><td class="small">${dt(c.run_at)}</td><td>${esc(c.chain)}</td><td class="small">${esc(c.source)}</td><td class="num">${num(c.pages)}</td><td class="num">${num(c.pools)}</td><td class="small">${c.window_start ? dt(c.window_start) + " — " + dt(c.window_end) : "—"}</td><td>${c.continuous ? "да" : "нет"}</td><td class="small muted">${esc(c.note)}</td></tr>`).join("")}</table></div>
  <h2>Известные ограничения</h2><div class="card"><ul class="small">${d.limitations.map(l => `<li>${esc(l)}</li>`).join("")}</ul></div>
  <h2>Ошибки</h2><div class="card" style="max-height:300px;overflow:auto"><table>${d.errors.map(e => `<tr><td class="small">${dt(e.at)}</td><td class="small">${esc(e.where_)}</td><td class="small mono">${esc((e.message || "").slice(0, 400))}</td></tr>`).join("") || '<tr><td class="muted">ошибок нет</td></tr>'}</table></div>
  <h2>Диагностика</h2><div class="card"><button id="diagBtn">Проверить окружение и доступ к API</button><div id="diagOut"></div></div>`;
  $("#diagBtn").onclick = async () => { $("#diagOut").innerHTML = "проверка…"; const r = await api("/api/diagnostics?network=true"); $("#diagOut").innerHTML = `<ul>${r.checks.map(c => `<li>${c.ok ? st("PASS") : st("FAIL")} ${esc(c.name)} <span class="muted">${esc(c.detail)}</span></li>`).join("")}</ul>`; };
}

/* ---------- Настройки ---------- */
async function vSettings(v) {
  const d = await api("/api/settings");
  const s = d.settings;
  const fld = (path, label, hint = "") => { const val = path.split(".").reduce((o, k) => o[k], s); return `<label>${esc(label)}<input data-p="${path}" value="${esc(Array.isArray(val) ? val.join(",") : val)}">${hint ? `<span class="muted">${esc(hint)}</span>` : ""}</label>`; };
  v.innerHTML = `<h1>Настройки</h1>
  <div class="card"><h3>Шесть фильтров</h3><form class="inline">
    ${fld("filters.max_age_days", "Макс. возраст, дней")}${fld("filters.max_circ_mcap_usd", "Макс. circulating MCAP, $")}${fld("filters.max_drawdown_pct", "Макс. просадка от ATH, %")}
    ${fld("filters.ath_wick_factor", "Фильтр хвостов свечей, ×", "high > k × тела = аномалия")}${fld("filters.min_pool_liquidity_usd", "Мин. ликвидность пула для цены/ATH, $")}
    ${fld("filters.price_outlier_pct", "Отклонение цены пула от медианы, %")}${fld("filters.mcap_conflict_pct", "Порог конфликта MCAP источников, %")}
    ${fld("filters.vc_fail_categories", "Категории = VC (FAIL)", "через запятую: vc_fund, strategic…")}
    ${fld("filters.chain_min_tvl_usd", "Сеть: мин. TVL, $")}${fld("filters.chain_max_tvl_decline_90d_pct", "Сеть: макс. падение TVL 90д, %")}
    ${fld("filters.chain_min_dex_volume_30d_usd", "Сеть: мин. DEX-объём 30д, $")}${fld("filters.chain_max_stable_decline_90d_pct", "Сеть: макс. падение стейблкоинов 90д, %")}
  </form></div>
  <div class="card"><h3>Рыночный минимум (уровень B) и оценка</h3><form class="inline">${fld("market_minimum.min_liquidity_usd", "Мин. ликвидность, $")}${fld("market_minimum.min_volume_24h_usd", "Мин. объём 24ч, $")}${fld("scoring.watchlist_min_preliminary", "Мин. предв. оценка для уровня E")}</form></div>
  <div class="card"><h3>Сети</h3>${Object.entries(d.chains).map(([k, c]) => `<label class="small" style="margin-right:14px"><input type="checkbox" data-chain="${k}" ${d.enabled_chains.includes(k) ? "checked" : ""}> ${esc(c.name)} <span class="muted">(${esc(c.support)})</span></label>`).join("")}</div>
  <div class="card"><h3>Обнаружение и интервалы</h3><form class="inline">
    <label>Фоновый сбор<select data-p="scheduler_enabled"><option value="false" ${!s.scheduler_enabled ? "selected" : ""}>выключен</option><option value="true" ${s.scheduler_enabled ? "selected" : ""}>включён</option></select></label>
    ${fld("intervals_min.discovery", "Новые пулы, каждые N мин")}${fld("intervals_min.candidates_refresh", "Проверка кандидатов, мин")}${fld("intervals_min.fundamentals", "Фундаментал, мин")}${fld("intervals_min.weekly_snapshot", "Недельный снимок, мин")}${fld("intervals_min.backup", "Резервная копия, мин")}
    ${fld("discovery.max_pages_per_chain", "Макс. страниц new_pools на сеть")}${fld("discovery.historical_top_pools_pages", "Страниц топ-пулов (исторический добор)")}
    <label>DEX Screener профили (промо)<select data-p="discovery.dexscreener_profiles"><option value="false" ${!s.discovery.dexscreener_profiles ? "selected" : ""}>выключено</option><option value="true" ${s.discovery.dexscreener_profiles ? "selected" : ""}>включено (нерепрезентативно)</option></select></label>
  </form></div>
  <div class="card"><h3>Сигналы</h3><form class="inline">${fld("alerts.tvl_outperform_pp", "TVL быстрее сети, п.п.")}${fld("alerts.fees_growth_pct", "Рост комиссий, %")}${fld("alerts.liquidity_drop_pct", "Падение ликвидности, %")}${fld("alerts.emission_spike_x", "Всплеск эмиссии, ×")}${fld("alerts.streak_periods", "Недель подряд")}${fld("alerts.stale_hours", "Данные устарели через, ч")}${fld("alerts.unlock_lookahead_days", "Предупреждать о unlock за, дн.")}</form></div>
  <div class="card"><h3>Лимиты API (запросов/мин)</h3><form class="inline">${Object.keys(s.rate_limits_per_min).map(k => fld("rate_limits_per_min." + k, k)).join("")}</form></div>
  <p><button class="primary" id="saveS">Сохранить настройки</button> <button id="reevalS">Пересчитать фильтры для всех токенов</button></p>
  <div class="card"><h3>API-ключи (необязательно)</h3><p class="small sec">Программа работает без ключей. Ключи хранятся локально (${esc(d.data_dir)}), на Windows шифруются DPAPI, не попадают в экспорт и логи.</p>
    ${Object.entries(d.secrets).map(([k, set]) => `<div class="toolbar"><span style="min-width:200px">${esc(k)}</span><span class="small">${set ? "задан" : "не задан"}</span><input type="password" data-secret="${k}" placeholder="новое значение"><button class="small" data-savesec="${k}">сохранить</button></div>`).join("")}
    <div class="small muted">coingecko_demo_key — бесплатный ключ CoinGecko (доп. проверка circulating). etherscan_key — бесплатный ключ Etherscan V2 (верификация кода). В текущей версии ключи не обязательны и используются только в будущих модулях.</div></div>
  <div class="card"><h3>Резервные копии</h3><button id="bkBtn">Создать резервную копию сейчас</button><ul class="small">${d.backups.slice(0, 10).map(b => `<li>${esc(b.name)} — ${(b.size / 1e6).toFixed(1)} МБ</li>`).join("")}</ul><div class="small muted">Папка: ${esc(d.data_dir)}\\backups</div></div>`;
  $("#saveS").onclick = async () => {
    const ns = JSON.parse(JSON.stringify(s));
    document.querySelectorAll("[data-p]").forEach(el => {
      const ks = el.dataset.p.split("."); let o = ns; ks.slice(0, -1).forEach(k => o = o[k]);
      const old = o[ks.at(-1)]; let val = el.value;
      if (typeof old === "number") val = Number(val); else if (typeof old === "boolean") val = val === "true"; else if (Array.isArray(old)) val = val.split(",").map(x => x.trim()).filter(Boolean);
      o[ks.at(-1)] = val;
    });
    ns.chains_enabled = [...document.querySelectorAll("[data-chain]")].filter(x => x.checked).map(x => x.dataset.chain);
    await api("/api/settings", { method: "POST", body: ns }); toast("Настройки сохранены. Нажмите «Пересчитать фильтры», чтобы применить к базе.");
  };
  $("#reevalS").onclick = async () => { await api("/api/jobs/reevaluate", { method: "POST" }); toast("Пересчёт поставлен в очередь"); pollJobs(); };
  $("#bkBtn").onclick = async () => { await api("/api/jobs/backup", { method: "POST" }); toast("Резервная копия создаётся"); };
  document.querySelectorAll("[data-savesec]").forEach(b => b.onclick = async () => { const k = b.dataset.savesec; await api("/api/secrets", { method: "POST", body: { name: k, value: $(`[data-secret="${k}"]`).value } }); toast("Ключ сохранён"); render(); });
}

(async () => { META = await api("/api/meta"); render(); pollJobs(); setInterval(pollJobs, 4000); })();
