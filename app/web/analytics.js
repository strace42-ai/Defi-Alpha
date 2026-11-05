"use strict";
/* DeFi Alpha Scanner 2.1 — вкладки «Фундаментальный импульс» и «Эмиссионное давление».
   Отдельный файл: если он не загрузится или в нём ошибка, остальной интерфейс 2.0.1 работает без изменений. */
(function () {
  const EV = {
    confirmed: ["подтверждено", "ev-c"], final: ["окончательно", "ev-c"], complete: ["полностью", "ev-c"],
    reported: ["данные источника", "ev-r"], calculated: ["рассчитано", "ev-r"],
    estimated: ["оценка", "ev-e"], probable: ["вероятно", "ev-e"], claimed: ["заявлено, не проверено", "ev-u"], partial: ["частично", "ev-e"], in_progress: ["эпоха идёт", "ev-e"],
    unknown: ["неизвестно", "ev-u"], unconfirmed: ["границы не подтверждены", "ev-u"], see_momentum: ["см. импульс", "ev-u"],
  };
  const ev = s => { const [t, c] = EV[s] || [s || "—", "ev-u"]; return `<span class="fb ${c}">${esc(t)}</span>`; };
  const VERD = { improving: ["улучшается", "ev-c"], stable: ["стабильно", "ev-r"], deteriorating: ["ухудшается", "ev-bad"], insufficient: ["недостаточно данных", "ev-u"] };
  const verd = v => { const [t, c] = VERD[v] || [v || "—", "ev-u"]; return `<span class="fb ${c}">${esc(t)}</span>`; };
  const CONF = { high: "высокая", medium: "средняя", low: "низкая" };
  const tok = a => a == null ? '<span class="muted">н/д</span>' : num(a.tokens, 0);
  const unk = r => `<span class="muted">${esc(r || "неизвестно")}</span>`;
  const tokLink = (uid, sym) => `<a href="#/token/${encodeURIComponent(uid)}"><b>${esc(sym || "?")}</b></a>`;

  async function moduleBar(module, st) {
    const m = st.modules[module];
    const lr = m.last_run;
    return `<div class="card"><div class="toolbar">
      <b>${esc(m.name)}</b> ${m.enabled ? '<span class="fb ev-c">включён</span>' : '<span class="fb ev-u">выключен</span>'}
      <button id="anToggle" data-m="${module}" data-on="${m.enabled ? 1 : 0}">${m.enabled ? "Выключить модуль" : "Включить модуль"}</button>
      <button id="anRecalc" ${m.enabled ? "" : "disabled"}>Пересчитать</button>
      <button id="anReload">Обновить</button>
      <span class="small muted">${lr ? `последний проход ${dt(lr.finished_at)}: обработано ${lr.processed}, пересчитано ${lr.recomputed}, без изменений ${lr.unchanged}, ошибок ${lr.errors}${lr.stopped_reason ? " · " + esc(lr.stopped_reason) : ""}` : "ещё не выполнялся"}
      ${st.deferred && st.deferred.count ? ` · откладывался ${st.deferred.count} раз (${esc(st.deferred.reason || "")})` : ""}</span></div>
      <div class="small muted">Модуль работает в отдельной очереди с низким приоритетом, использует только уже собранные данные (сетевых запросов нет)
      и пишет результаты в собственные таблицы. Выключение мгновенно прекращает планирование; данные сканера не затрагиваются.</div></div>`;
  }
  function bindBar(module, rerender) {
    $("#anToggle").onclick = async () => {
      const on = $("#anToggle").dataset.on !== "1";
      if (on && !confirm("Включить модуль? Будут созданы отдельные таблицы аналитики; данные сканера не изменяются.")) return;
      await api("/api/analytics/settings", { method: "POST", body: { module, enabled: on } });
      toast(on ? "Модуль включён, первый расчёт поставлен в очередь «аналитика»" : "Модуль выключен; новые задания не планируются");
      setTimeout(rerender, on ? 1500 : 0);
    };
    $("#anRecalc").onclick = async () => { await api("/api/analytics/recompute", { method: "POST", body: { module } }); toast("Пересчёт поставлен в очередь"); setTimeout(rerender, 2000); };
    $("#anReload").onclick = rerender;
  }

  /* ---------------- Фундаментальный импульс ---------------- */
  const MSORT = { mode: "data" };
  async function vMomentum(v, arg) {
    const st = await api("/api/analytics/status");
    const d = await api("/api/analytics/momentum");
    const rerender = () => vMomentum(v, arg);
    let rows = d.rows.filter(r => r.summary);
    if (MSORT.mode === "data") rows = rows.filter(r => r.verdict && r.verdict !== "insufficient");
    if (MSORT.mode === "div") rows = rows.filter(r => r.summary.divergence);
    const nIns = d.rows.filter(r => r.verdict === "insufficient").length;
    v.innerHTML = `<h1>Фундаментальный импульс</h1>${await moduleBar("momentum", st)}
      ${!st.modules.momentum.enabled && !d.rows.length ? `<div class="card"><p>Модуль выключен. После включения он оценит динамику TVL, комиссий, выручки и объёмов
        подтверждённых протоколов по дневным рядам DefiLlama, сравнит её с собственной сетью и с изменением рыночной оценки.</p>
        <p class="small muted">Вывод — повод для исследования, а не рекомендация. Если нет двух сопоставимых периодов — «недостаточно данных».</p></div>` : ""}
      ${d.rows.length ? `<div class="toolbar"><label class="small">Показать <select id="mMode">
        <option value="default" ${MSORT.mode === "default" ? "selected" : ""}>все (${d.rows.length})</option>
        <option value="data" ${MSORT.mode === "data" ? "selected" : ""}>только с выводом (${d.rows.length - nIns})</option>
        <option value="div" ${MSORT.mode === "div" ? "selected" : ""}>экономика опережает оценку</option></select></label></div>
      <div class="card" style="overflow-x:auto"><table><tr><th>Токен</th><th>Протокол</th><th>Вывод</th><th>Достоверность</th><th class="num">TVL</th>
        <th class="num">TVL 30д</th><th class="num">к сети 30д</th><th class="num">Комиссии 30д</th><th class="num">Выручка 30д</th><th class="num">Объём 30д</th>
        <th class="num">Цена 7д</th><th class="num">Цена 30д</th><th>MCAP</th><th>Пометки</th></tr>
        ${rows.map(r => { const s = r.summary; return `<tr class="mrow" data-uid="${esc(r.token_uid)}" style="cursor:pointer">
          <td>${tokLink(r.token_uid, r.symbol)}${r.hidden ? ' <span class="small muted">скрыт</span>' : ""}</td><td class="small">${esc(s.name || s.slug || "")}</td>
          <td>${r.last_status === "error" ? '<span class="fb ev-bad">ошибка</span>' : verd(r.verdict)}${s.divergence ? ' <span class="fb ev-c" title="экономика улучшается быстрее оценки">★ опережение</span>' : ""}</td>
          <td class="small">${esc(CONF[r.confidence] || "")}</td><td class="num">${usd(s.tvl_usd)}</td><td class="num">${pct(s.tvl_30d)}</td><td class="num">${pct(s.tvl_vs_chain_30d)}</td>
          <td class="num">${pct(s.fees_30d_change)}</td><td class="num">${pct(s.revenue_30d_change)}</td><td class="num">${pct(s.volume_30d_change)}</td>
          <td class="num">${pct(s.price_7d)}</td><td class="num">${pct(s.price_30d)}</td>
          <td class="small">${s.mcap != null ? usd(s.mcap) + " " + mcapBadge(s.mcap_type) : '<span class="muted">н/д</span>'}</td>
          <td class="small">${(s.flags || []).map(f => `<span class="fb ev-e">${esc(f)}</span>`).join(" ")}</td></tr>`; }).join("")}</table></div>
      <div id="mDetail"></div>` : ""}`;
    bindBar("momentum", rerender);
    if ($("#mMode")) $("#mMode").onchange = () => { MSORT.mode = $("#mMode").value; rerender(); };
    document.querySelectorAll(".mrow").forEach(tr => tr.onclick = e => { if (e.target.closest("a")) return; location.hash = "#/momentum/" + encodeURIComponent(tr.dataset.uid); });
    if (arg) momentumDetail(arg);
  }

  async function momentumDetail(uid) {
    const d = await api("/api/analytics/momentum/" + encodeURIComponent(uid));
    const el = $("#mDetail"); if (!el) return;
    if (!d.latest) { el.innerHTML = '<div class="card muted">Для этого токена расчёта ещё нет.</div>'; return; }
    const r = d.latest.result, m = r.metrics || {}, mk = r.market || {};
    const wrow = (name, k) => `<tr><td>${name}</td>${[7, 30, 90].map(w => { const x = m[`${k}_${w}d`] || {}; return `<td class="num">${x.value != null ? pct(x.value) : unk(x.reason ? "нет истории" : "")}</td>`; }).join("")}</tr>`;
    const sums = (name, k) => { const x = m[k] || {}; if (!x.available) return `<tr><td>${name}</td><td colspan="4">${unk(x.note)}</td></tr>`;
      return `<tr><td>${name}${x.stale ? ' <span class="fb ev-e">устарело</span>' : ""}</td>${[7, 30].map(w => { const s = x["w" + w] || {}; return `<td class="num">${usd(s.cur)}</td><td class="num">${s.change != null ? pct(s.change) : unk("нет 2 периодов")}</td>`; }).join("")}</tr>`; };
    const fl = m.flows || {};
    el.innerHTML = `<h2>${esc(r.symbol || "")} — ${esc(r.name || r.slug || "")} ${verd(r.verdict)} <span class="small muted">окно ${r.window || "—"} дн., достоверность: ${esc(CONF[r.confidence] || "")}, расчёт ${dt(d.latest.computed_at)}, метод ${esc(d.latest.method_version)}</span></h2>
      <div class="card"><b>Объяснение</b><ul>${(r.explanation || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
        ${r.divergence ? `<div class="warnbox">Событие для исследования: ${esc(r.divergence.text)}. Это не рекомендация к покупке.</div>` : ""}
        ${(r.flags || []).length ? `<b>Оговорки</b><ul class="small">${r.flags.map(f => `<li class="${f.severity === "warn" ? "sev-medium" : "muted"}">${esc(f.text)}</li>`).join("")}</ul>` : ""}</div>
      <div class="grid g2"><div class="card"><h3>TVL</h3><table><tr><th></th><th class="num">7 дн.</th><th class="num">30 дн.</th><th class="num">90 дн.</th></tr>
        ${wrow("Протокол", "tvl")}
        <tr><td>Сеть</td>${[7, 30, 90].map(w => `<td class="num">${pct((m["tvl_" + w + "d"] || {}).chain_change)}</td>`).join("")}</tr>
        <tr><td>Относительно сети</td>${[7, 30, 90].map(w => `<td class="num">${pct((m["tvl_" + w + "d"] || {}).relative_to_chain)}</td>`).join("")}</tr>
        <tr><td>Доля в сети, п.п.</td>${[7, 30, 90].map(w => { const s = (m["tvl_" + w + "d"] || {}).share_of_chain; return `<td class="num">${s ? `${s.from_pct.toFixed(2)}% → ${s.to_pct.toFixed(2)}%` : unk("—")}</td>`; }).join("")}</tr></table>
        <div class="small muted">Ускорение 30 дн.: ${m.tvl_acceleration_30d && m.tvl_acceleration_30d.value != null ? pct(m.tvl_acceleration_30d.value) + " п.п." : unk(m.tvl_acceleration_30d && m.tvl_acceleration_30d.reason)}.
        Динамика — только между точными дневными точками ряда DefiLlama, без интерполяции. Рост долларового TVL ≠ чистый приток капитала.</div>
        <div style="height:200px"><canvas id="mTvl"></canvas></div></div>
      <div class="card"><h3>Комиссии, выручка, объём</h3><table><tr><th></th><th class="num">7 дн.</th><th class="num">к пред. 7</th><th class="num">30 дн.</th><th class="num">к пред. 30</th></tr>
        ${sums("Комиссии (gross)", "fees")}${sums("Выручка протокола", "revenue")}${sums("Выручка держателей", "holders_revenue")}${sums("Объём DEX", "dex_volume")}</table>
        <div class="small">Комиссии/TVL (год.): ${m.fees_to_tvl && m.fees_to_tvl.annual_pct != null ? m.fees_to_tvl.annual_pct.toFixed(1) + "%" : unk()}${m.fees_to_tvl && m.fees_to_tvl.change_pp != null ? ` (изменение ${pct(m.fees_to_tvl.change_pp)} п.п. к пред. 30 дн.)` : ""}
          · Объём/TVL в сутки: ${m.volume_to_tvl && m.volume_to_tvl.per_day != null ? m.volume_to_tvl.per_day.toFixed(2) + "×" : unk()}
          ${m.dex_share ? ` · Доля в объёме DEX сети: ${m.dex_share.from_pct.toFixed(2)}% → ${m.dex_share.to_pct.toFixed(2)}%` : ""}</div>
        ${fl.w30 ? `<div class="small" style="margin-top:6px"><b>Куда идут комиссии за 30 дн.</b> (по определениям DefiLlama): LP/поставщики ${usd(fl.w30.supply_side_lp)}, протокол ${usd(fl.w30.protocol_revenue)},
          держатели ${usd(fl.w30.holders_revenue)}, казначейство/прочее ${fl.w30.treasury_other != null ? usd(fl.w30.treasury_other) : unk("не выводится")}${fl.w30.consistent === false ? ' <span class="fb ev-e">данные источника несогласованы</span>' : ""}</div>` : ""}
        <div style="height:200px"><canvas id="mFees"></canvas></div></div></div>
      <div class="grid g2"><div class="card"><h3>Рыночная оценка</h3><div class="kv">
        <div>MCAP сейчас</div><div>${mk.mcap_now && mk.mcap_now.value != null ? usd(mk.mcap_now.value) + " " + mcapBadge(mk.mcap_now.type) : unk()}</div>
        <div>MCAP 7 / 30 дн.</div><div>${[7, 30].map(w => { const x = mk["mcap_" + w + "d"] || {}; return x.value != null ? pct(x.value) : unk("нет 2 подтв. оценок"); }).join(" / ")}</div>
        <div>Цена 7 / 30 дн.</div><div>${[7, 30].map(w => { const x = mk["price_" + w + "d"] || {}; return x.value != null ? pct(x.value) : unk("нет свечей"); }).join(" / ")} <span class="small muted">пул ${esc(short(mk.pool || ""))}</span></div>
        <div>Выручка / MCAP (год.)</div><div>${mk.revenue_annual_to_mcap_pct != null ? mk.revenue_annual_to_mcap_pct.toFixed(1) + "%" : unk()}</div>
        <div>Выручка держателей / MCAP</div><div>${mk.holders_revenue_annual_to_mcap_pct != null ? mk.holders_revenue_annual_to_mcap_pct.toFixed(1) + "%" : unk()}</div>
        <div>Комиссии / MCAP (справочно)</div><div>${mk.fees_annual_to_mcap_pct != null ? mk.fees_annual_to_mcap_pct.toFixed(0) + "%" : unk()}</div></div>
        <div class="small muted">${esc(mk.ratio_note || "")}</div></div>
      <div class="card"><h3>Сеть и конкуренты</h3><div class="kv"><div>Сеть</div><div>${esc((m.chain || {}).name || "")} · TVL ${usd((m.chain || {}).tvl_usd)}</div>
        <div>Комиссии сети 30д</div><div>${pct((m.chain || {}).fees_change_30d)}</div><div>Объём DEX сети 30д</div><div>${pct((m.chain || {}).dex_volume_change_30d)}</div></div>
        ${m.competitors ? `<div class="small">Доля в категории: ${m.competitors.share_pct.toFixed(1)}% (место ${m.competitors.rank} из ${m.competitors.of}). Лидеры: ${(m.competitors.top || []).map(t => `${esc(t.slug)} ${usd(t.tvl)}`).join(", ")}</div>
          <div class="small muted">${esc(m.competitors.note)}</div>` : unk("нет данных о категории")}</div></div>
      <div class="card"><h3>История оценок</h3><table><tr><th>Расчёт</th><th>Вывод</th><th>Достоверность</th><th>Метод</th></tr>
        ${d.history.map(h => `<tr><td>${dt(h.computed_at)}</td><td>${verd(h.verdict)}</td><td>${esc(CONF[h.confidence] || "")}</td><td class="small">${esc(h.method_version)}</td></tr>`).join("")}</table>
        <div class="small muted">Новая запись появляется только при изменении входных данных. Источники: ${(r.sources || []).map(s => s.url ? link(s.url, s.source) : esc(s.source + (s.pool ? " " + short(s.pool) : ""))).join(", ")}</div></div>`;
    const S = d.series || {};
    const ms = a => (a || []).filter(x => x[0] % 86400 === 0).map(x => ({ x: x[0] * 1000, y: x[1] }));
    // индекс = 100 на общей начальной дате (не ранее 60 дней назад), чтобы ранний малый TVL не искажал сравнение с сетью
    const pt = ms(S.tvl), cs = ms(S.chain_tvl);
    if ($("#mTvl") && pt.length) {
      const last = pt[pt.length - 1].x, start = Math.max(pt[0].x, cs.length ? cs[0].x : 0, last - 60 * 86400000);
      const idx = p => { const q = p.filter(z => z.x >= start); const b = q.length ? q[0].y : 0; return b ? q.map(z => ({ x: z.x, y: z.y / b * 100 })) : []; };
      lineChart($("#mTvl"), [{ label: "TVL протокола (индекс, 100 = " + new Date(start).toLocaleDateString("ru-RU") + ")", data: idx(pt) }, { label: "TVL сети (индекс)", data: idx(cs) }], { fmt: v => v.toFixed(0) });
    }
    if ($("#mFees") && S.fees && S.fees.length) lineChart($("#mFees"), [{ label: "Комиссии в сутки", data: ms(S.fees), type: "bar" }, { label: "Выручка в сутки", data: ms(S.revenue) }], { fmt: fmtUsdPlain, fmtAxis: fmtUsdPlain });
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* ---------------- Эмиссионное давление ---------------- */
  async function vEmission(v, arg) {
    const st = await api("/api/analytics/status");
    const d = await api("/api/analytics/emission");
    const rerender = () => vEmission(v, arg);
    let uid = arg || (d.rows.find(r => r.symbol === "FABLES") || d.rows[0] || {}).token_uid;
    v.innerHTML = `<h1>Эмиссионное давление</h1>${await moduleBar("emission", st)}
      ${!st.modules.emission.enabled && !d.rows.length ? `<div class="card"><p>Модуль выключен. После включения он разберёт уже собранные события mint/burn:
        стартовые аллокации (Genesis), LP-эмиссию, неизвестные выпуски, чистый выпуск по эпохам и использование недельного бюджета.</p>
        <p class="small muted">Неподтверждённые категории остаются UNKNOWN. Получение наград, ve-блокировки и продажи — этапы 2–3, сейчас показываются как неизвестные.</p></div>` : ""}
      ${d.rows.length ? `<div class="toolbar"><label class="small">Токен <select id="eTok">${d.rows.map(r => `<option value="${esc(r.token_uid)}" ${r.token_uid === uid ? "selected" : ""}>${esc(r.symbol)} (${esc(r.chain)})</option>`).join("")}</select></label></div>` : ""}
      <div id="eDetail"></div>`;
    bindBar("emission", rerender);
    if ($("#eTok")) $("#eTok").onchange = () => { location.hash = "#/emission/" + encodeURIComponent($("#eTok").value); };
    if (uid) emissionDetail(uid);
  }

  async function emissionDetail(uid) {
    const d = await api("/api/analytics/emission/" + encodeURIComponent(uid));
    const el = $("#eDetail"); if (!el) return;
    if (!d.latest) { el.innerHTML = '<div class="card muted">Расчёта ещё нет.</div>'; return; }
    const r = d.latest.result, t = r.totals, g = r.genesis;
    const exA = a => d.explorer_addr ? link(d.explorer_addr.replace("{address}", a), short(a)) : esc(short(a));
    const exT = h => d.explorer_tx ? link(d.explorer_tx.replace("{tx}", h), short(h)) : esc(short(h));
    el.innerHTML = `
      <div class="card"><b>${esc(r.symbol)}</b> · расчёт ${dt(d.latest.computed_at)} · метод ${esc(d.latest.method_version)} · покрытие ${ev(r.coverage.status)}
        блоки ${num(r.coverage.scan_from_block)}–${num(r.coverage.scan_to_block)}, событий ${num(r.coverage.events)}
        · <a href="/api/analytics/emission/${encodeURIComponent(uid)}/events.csv">выгрузить классификацию событий (CSV)</a>
        <div class="small muted">${esc(r.coverage.note)}</div>
        ${(r.conflicts || []).length ? `<div class="warnbox">Конфликт ролей адресов: ${r.conflicts.map(c => `${esc(short(c.address))}: ${esc(c.conflict)}`).join("; ")}</div>` : ""}
        ${(r.limitations || []).map(x => `<div class="small sev-medium">${esc(x)}</div>`).join("")}</div>
      <h2>Проверяемые ответы</h2><div class="card"><table><tr><th>Вопрос</th><th>Статус</th><th class="num">Значение</th><th>Основание</th></tr>
        ${r.answers.map(a => `<tr><td>${esc(a.q)}</td><td>${ev(a.status)}</td><td class="num">${a.value == null ? '<span class="muted">—</span>' : Array.isArray(a.value) ? a.value.map(x => `нед. ${x.week}: ${x.pct != null ? x.pct.toFixed(1) + "%" : x.min_pct != null ? "подтверждено ≥ " + x.min_pct.toFixed(1) + "%, полная доля неизвестна" : esc(EV[x.status] ? EV[x.status][0] : x.status)}`).join("<br>") : typeof a.value === "object" ? tok(a.value) : num(a.value, 0)}
          ${a.extra ? Object.entries(a.extra).filter(([, x]) => x && x.tokens).map(([k, x]) => `<div class="small muted">${esc(k)}: ${tok(x)}</div>`).join("") : ""}</td><td class="small">${esc(a.basis || "")}</td></tr>`).join("")}</table></div>
      <div class="grid g2"><div class="card"><h3>Выпуск и сжигание ${ev(t.status)}</h3><div class="kv">
        <div>Выпущено (Σ mint)</div><div>${tok(t.minted)} <span class="small muted">${num(t.mint_events)} событий</span></div>
        <div>Сожжено (Σ burn)</div><div>${tok(t.burned)} <span class="small muted">${num(t.burn_events)} событий</span></div>
        <div>Чистый выпуск</div><div>${tok(t.net)}</div>
        <div>totalSupply() на блоке ${num(t.reconciled_at_block)}</div><div>${tok(t.total_supply_rpc)} ${t.reconciled_exactly ? '<span class="fb ev-c">совпадает с Σmint − Σburn до этого блока</span>' : '<span class="fb ev-e">сверка не подтверждена</span>'}</div>
        <div>После блока сверки</div><div>${tok(t.after_reconciled_block)} <span class="small muted">будет сверено при следующем ончейн-обновлении</span></div></div>
        <div class="small muted">${esc(t.note)}</div></div>
      <div class="card"><h3>По категориям</h3><table><tr><th>Категория</th><th class="num">Токенов</th><th class="num">Доля</th><th class="num">Событий</th><th class="num">Получателей</th><th>Доказательность</th></tr>
        ${r.categories.map(c => `<tr><td>${esc(c.name)}</td><td class="num">${tok(c.amount)}</td><td class="num">${c.share_of_minted_pct != null ? c.share_of_minted_pct.toFixed(1) + "%" : ""}</td><td class="num">${num(c.events)}</td><td class="num">${num(c.recipients)}</td><td>${ev(c.evidence)}</td></tr>`).join("")}</table>
        <div style="height:190px"><canvas id="eEpochs"></canvas></div></div></div>
      <h2>Genesis и выпуски до запуска</h2><div class="card"><div class="small">Граница запуска: ${r.tge.block ? `блок ${num(r.tge.block)} (${esc(r.tge.at)}) — ${esc(r.tge.source)}` : unk(r.tge.note)}</div>
        <div class="kv"><div>Genesis подтверждено</div><div>${tok(g.confirmed)}</div><div>До запуска, категория не подтверждена</div><div>${tok(g.pre_tge_unclassified)}</div></div>
        <table><tr><th>Блок</th><th>Транзакция</th><th>Получатель</th><th>Роль</th><th class="num">Токенов</th><th>Категория</th><th>Пояснение</th></tr>
        ${g.items.slice(0, 20).map(x => `<tr><td>${num(x.block)}</td><td class="mono small">${exT(x.tx)}</td><td class="mono small">${exA(x.address)}</td><td class="small">${esc(x.role_label || x.role || "—")}</td>
          <td class="num">${tok(x.amount)}</td><td>${x.category === "genesis" ? ev(x.evidence) + " Genesis" : ev("unknown")}</td><td class="small">${esc(x.note || "")}${x.doc_hint ? `<div class="muted">${esc(x.doc_hint.join("; "))}</div>` : ""}</td></tr>`).join("")}</table>
        <div class="small muted">${esc(g.note)}</div></div>
      <h2>Эпохи и использование бюджета</h2><div class="card" style="overflow-x:auto"><table><tr><th>Неделя</th><th>Период (UTC)</th><th>Состояние</th><th class="num">Бюджет</th><th class="num">Все mint</th>
        <th class="num">LP-эмиссия</th><th class="num">Неизвестно</th><th>Использование бюджета</th><th class="num">Цена эпохи</th><th class="num">Mint после запуска, $</th><th class="num">Gross fees, $</th><th class="num">Стимулы / gross fees</th></tr>
        ${r.epochs.map(e => `<tr><td>${esc(e.week)}</td><td class="small">${esc((e.start || "").slice(0, 16))} — ${esc((e.end || "").slice(0, 16))}<div class="muted">блоки ${num(e.start_block)}–${e.end_block ? num(e.end_block) : "…"}</div></td>
          <td>${ev(e.state)}</td><td class="num">${tok(e.budget)}</td><td class="num">${tok(e.minted_all)}${e.all_mints_to_budget_pct != null ? `<div class="small muted">${e.all_mints_to_budget_pct.toFixed(0)}% бюджета — не использование</div>` : ""}</td>
          <td class="num">${e.lp_emission ? tok(e.lp_emission) : unk()}</td><td class="num">${tok(e.unknown)}</td>
          <td>${ev(e.utilization.status)} ${e.utilization.value_pct != null ? e.utilization.value_pct.toFixed(1) + "%" : e.utilization.confirmed_pct != null ? `подтверждено ≥ ${e.utilization.confirmed_pct.toFixed(1)}%` + (e.utilization.upper_bound_pct != null ? ` <span class="small muted">(не более ${e.utilization.upper_bound_pct.toFixed(1)}%, если вся не классифицированная часть — LP)</span>` : "") : ""}<div class="small muted">${esc(e.utilization.reason || e.utilization.basis || "")}</div></td>
          <td class="num">${e.price && e.price.value != null ? usd(e.price.value) + ` <span class="small muted">${e.price.days} дн.</span>` : unk(e.price ? "нет свечей" : "")}</td>
          <td class="num">${e.emission_usd ? usd(e.emission_usd.all_post_tge_mints) + ' <span class="fb ev-e">оценка</span>' : unk()}</td>
          <td class="num">${e.fees && e.fees.fees != null ? usd(e.fees.fees) : unk(e.fees && e.fees.fees_partial_days ? `${e.fees.fees_partial_days} из 7 дн.` : "")}</td>
          <td class="num">${e.incentives_to_gross_fees ? e.incentives_to_gross_fees.all_post_tge_mints.toFixed(2) + "×" : unk()}</td></tr>`).join("")}</table>
        <div class="small muted">Использование бюджета = LP-эмиссия эпохи / бюджет; только для завершённой эпохи с точными границами и идентифицированными адресами gauge/распределителя.
          «Mint после запуска, $» — по цене своей эпохи (VWAP дневных свечей); включает mint с неизвестным назначением, поэтому это верхняя граница стимулов.
          Gross fees DefiLlama ≠ eligible fees контрактного ограничения эмиссии (не установлены).</div></div>
      <div class="grid g2"><div class="card"><h3>Обращение, блокировки, награды, продажи</h3><div class="kv">
        <div>Изменение circulating supply</div><div>${r.circulating.status === "unknown" ? unk(r.circulating.reason) : num(r.circulating.change, 0) + " " + ev("calculated")}
          ${r.circulating.latest ? `<div class="small muted">последнее: ${num(r.circulating.latest.value, 0)} (${esc(r.circulating.latest.source)}, ${dt(r.circulating.latest.at)})</div>` : ""}</div>
        <div>Mint сразу в ve-escrow</div><div>${tok(r.net_circulating_growth.components.minted_into_escrow)} ${ev("confirmed")}</div>
        <div>Новые блокировки / разблокировки</div><div>${unk(r.ve_locks.reason)}</div>
        <div>Получено LP-наград (claims)</div><div>${unk(r.claims.reason)}</div>
        <div>Подтверждённые продажи наград</div><div>${unk(r.reward_sales.reason)}</div></div>
        <div class="small muted">${esc(r.net_circulating_growth.note)}. ${esc(r.reward_sales.note)}</div></div>
      <div class="card"><h3>Неизвестные получатели mint</h3><div class="small">Всего ${tok(r.unknown_recipients.total)} на ${num(r.unknown_recipients.addresses)} адресов. Роль назначается только по доказательству (документация, код контракта), не по объёму операций.</div>
        <table><tr><th>Адрес</th><th class="num">Токенов</th><th class="num">Доля</th><th class="num">Событий</th><th></th></tr>
        ${r.unknown_recipients.top.slice(0, 15).map(x => `<tr><td class="mono small">${exA(x.address)}${x.role === "minter" ? ' <span class="small muted">minter</span>' : ""}</td><td class="num">${tok(x.amount)}</td><td class="num">${x.share_of_unknown_pct != null ? x.share_of_unknown_pct.toFixed(1) + "%" : ""}</td><td class="num">${num(x.events)}</td>
          <td><button class="small" data-setrole="${esc(x.address)}">указать роль</button></td></tr>`).join("")}</table></div></div>
      <h2>Реестр ролей адресов</h2><div class="card" id="eRoles"></div>`;
    rolesBlock(uid, r);
    const eps = r.epochs.filter(e => e.minted_all);
    if ($("#eEpochs") && eps.length) {
      const cats = [...new Set(eps.flatMap(e => Object.keys(e.by_category || {})))];
      const NAMES = { genesis: "Genesis", pre_tge_unclassified: "до запуска (не подтв.)", lp_emission: "LP-эмиссия", emission_other: "прочая эмиссия", allocation_after_tge: "аллокации", mint_to_escrow: "в escrow", system_minter: "minter", unknown: "неизвестно" };
      const COL = { genesis: "#199e70", lp_emission: "#3987e5", unknown: "#6b6b66", pre_tge_unclassified: "#d95926", system_minter: "#a0a09a", allocation_after_tge: "#c9a227", mint_to_escrow: "#7b5bd6", emission_other: "#2bb0c9" };
      const c = new Chart($("#eEpochs"), { type: "bar", data: { labels: eps.map(e => "нед. " + e.week + (e.state === "in_progress" ? " (идёт)" : "")),
        datasets: cats.map(k => ({ label: NAMES[k] || k, backgroundColor: COL[k] || "#888", data: eps.map(e => ((e.by_category || {})[k] || {}).tokens || 0) })) },
        options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, ticks: { callback: fmtNum } } },
          plugins: { tooltip: { callbacks: { label: it => `${it.dataset.label}: ${fmtNum(it.parsed.y)}` } } } } });
      charts.push(c);
    }
    document.querySelectorAll("[data-setrole]").forEach(b => b.onclick = () => { const f = $("#roleForm"); if (f) { f.address.value = b.dataset.setrole; f.scrollIntoView({ behavior: "smooth" }); f.role.focus(); } });
  }

  async function rolesBlock(uid, r) {
    const tk = await api("/api/token/" + encodeURIComponent(uid));
    const chain = tk.token.chain;
    const d = await api(`/api/analytics/roles?chain=${encodeURIComponent(chain)}&token_uid=${encodeURIComponent(uid)}`);
    const el = $("#eRoles"); if (!el) return;
    el.innerHTML = `<table><tr><th>Адрес</th><th>Роль</th><th>Описание</th><th>Достоверность</th><th>Источник</th><th>Происхождение</th><th></th></tr>
      ${d.effective.map(x => `<tr><td class="mono small">${esc(short(x.address))}</td><td>${esc(d.roles[x.role] || x.role)}${x.conflict ? ` <span class="fb ev-bad">${esc(x.conflict)}</span>` : ""}</td>
        <td class="small">${esc(x.label || "")}${x.refines_legacy ? `<div class="muted">уточняет метку 2.0: ${esc(x.refines_legacy.label || x.refines_legacy.role)}</div>` : ""}</td><td>${ev(x.confidence === "claimed" ? "unknown" : x.confidence)}</td>
        <td class="small">${x.source_url ? link(x.source_url, "источник") : esc(x.source_type || "")}</td><td class="small muted">${esc(x.origin)}${x.token_specific ? " · только этот токен" : " · вся сеть"}</td>
        <td>${(x.entries || []).filter(e => e.active).map(e => `<button class="small" data-deact="${e.id}">отключить</button>`).join("")}</td></tr>`).join("")}</table>
      <form id="roleForm" class="inline" style="margin-top:10px"><label>Адрес<input name="address" required></label>
        <label>Роль<select name="role">${Object.entries(d.roles).map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join("")}</select></label>
        <label>Достоверность<select name="confidence"><option value="confirmed">подтверждено (нужна ссылка/доказательство)</option><option value="probable">вероятно</option><option value="claimed" selected>заявлено, не проверено</option></select></label>
        <label>Ссылка на доказательство<input name="source_url" placeholder="https://…"></label><label>Доказательство<input name="evidence" placeholder="документация, функция контракта…"></label>
        <label>Описание<input name="note"></label><label>&nbsp;<button class="primary" type="submit">Добавить роль</button></label></form>
      <div class="small muted">Роль применяется только к этому токену в сети ${esc(chain)} и не переносится на другие протоколы и сети. Роль не назначается по объёму операций.
        После добавления выполняется пересчёт; LP-эмиссия и использование бюджета появятся только при подтверждённых адресах gauge/распределителя.</div>`;
    $("#roleForm").onsubmit = async e => {
      e.preventDefault(); const fd = Object.fromEntries(new FormData(e.target));
      try { await api("/api/analytics/roles", { method: "POST", body: { ...fd, chain, token_uid: uid, source_type: "user" } }); toast("Роль добавлена, пересчёт поставлен в очередь"); setTimeout(() => emissionDetail(uid), 2000); } catch { }
    };
    el.querySelectorAll("[data-deact]").forEach(b => b.onclick = async () => { await api(`/api/analytics/roles/${b.dataset.deact}/deactivate`, { method: "POST", body: {} }); toast("Роль отключена"); rolesBlock(uid, r); });
  }

  /* ---------------- сводка в карточке токена ---------------- */
  window.analyticsCardCache = {};
  window.tokenAnalyticsSummary = async function (uid) {
    try {
      const d = await api("/api/analytics/token/" + encodeURIComponent(uid));
      let h = "";
      const m = d.momentum, e = d.emission;
      if (m && m.enabled && m.summary) {
        const s = m.summary;
        h += `<div class="small">Фундаментальный импульс: ${verd(m.verdict)}${s.window ? ` за ${s.window} дн.` : ""} · достоверность ${esc(CONF[m.confidence] || "")}${s.divergence ? ' · <span class="fb ev-c">★ экономика опережает оценку</span>' : ""}
          · TVL 30д ${pct(s.tvl_30d)}, комиссии 30д ${pct(s.fees_30d_change)} · <a href="#/momentum/${encodeURIComponent(uid)}">подробно →</a></div>`;
      }
      if (e && e.enabled && e.summary) {
        const s = e.summary;
        const u = (s.epochs || []).filter(x => x.state === "complete");
        h += `<div class="small">Эмиссионное давление: выпущено ${tok(s.minted)} ${s.reconciled ? '<span class="fb ev-c">сверено с totalSupply</span>' : '<span class="fb ev-e">не сверено</span>'}
          · Genesis подтв. ${tok(s.genesis_confirmed)} · до запуска не подтв. ${tok(s.pre_tge_unclassified)} · LP-эмиссия ${s.lp_emission ? tok(s.lp_emission) : unk("не выделена")}
          · неизвестно ${s.unknown_share_pct != null ? s.unknown_share_pct.toFixed(0) + "%" : "н/д"} · бюджет: ${u.length ? u.map(x => x.util_pct != null ? x.util_pct.toFixed(0) + "%" : x.util_confirmed_pct != null ? "≥" + x.util_confirmed_pct.toFixed(0) + "% (не полно)" : "не проверено").join(" / ") : "—"}
          · <a href="#/emission/${encodeURIComponent(uid)}">подробно →</a></div>`;
      }
      h = h ? `<div class="card" style="max-width:980px;padding:8px 12px"><b class="small">Аналитика 2.1</b>${h}</div>` : "";
      window.analyticsCardCache[uid] = h;
      const el = document.getElementById("anSummary");
      if (el && el.dataset.uid === uid) el.innerHTML = h;
    } catch { /* аналитика не должна мешать карточке */ }
  };

  window.EXTRA_VIEWS = { momentum: vMomentum, emission: vEmission };
})();
