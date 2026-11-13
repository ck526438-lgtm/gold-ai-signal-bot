/* Gold AI Signal Bot (XAUUSD M5) — Espace d'analyse */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const C = {
    up: css("--up"), down: css("--down"), accent: css("--accent"), muted: css("--muted"),
    line: css("--line"), text: css("--text"), warn: css("--warn"), panel: css("--panel"),
    emaF: "#ff9f5a", emaM: "#4c8dff", emaS: "#b07cff",
  };
  const f5 = (v) => (v == null ? "—" : Number(v).toFixed(2));
  const sgn = (v, d = 1) => (v > 0 ? "+" : "") + Number(v).toFixed(d);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDate = (iso, withDay = true) => {
    const d = new Date(iso);
    const p = (n) => String(n).padStart(2, "0");
    const t = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
    return withDay ? `${p(d.getUTCDate())}/${p(d.getUTCMonth() + 1)} ${t}` : t;
  };

  const store = {
    get(k, d) { try { const v = localStorage.getItem("goldai." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("goldai." + k, JSON.stringify(v)); } catch { /* indisponible */ } },
  };

  const S = {
    snap: null, settings: null, meta: null, journal: [], view: "analyse",
    selected: null, fitted: false,
    prefs: { browser: store.get("browser", false), sound: store.get("sound", true) },
  };

  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch { /* corps vide */ }
      throw new Error(msg);
    }
    return r.json();
  }

  /* ------------------------------------------------------------------ navigation */
  function show(view) {
    S.view = view;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
    if (view === "signaux") loadJournal();
    if (view === "reglages") { fillSettings(); loadDiag(); loadAbout(); }
    if (view === "robot") loadBot(true);
    if (view === "analyse") requestAnimationFrame(resizeCharts);
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => show(t.dataset.view)));
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => show(b.dataset.goto)));

  /* ------------------------------------------------------------------ graphiques */
  const LWC = window.LightweightCharts;
  const baseOpts = {
    layout: { background: { color: "transparent" }, textColor: C.muted, fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
    grid: { vertLines: { color: "rgba(34,41,54,.55)" }, horzLines: { color: "rgba(34,41,54,.55)" } },
    rightPriceScale: { borderColor: C.line },
    timeScale: { borderColor: C.line, timeVisible: true, secondsVisible: false, rightOffset: 8 },
    crosshair: { mode: LWC.CrosshairMode.Normal },
    localization: { locale: "fr-FR", priceFormatter: (p) => p.toFixed(2) },
  };
  const chart = LWC.createChart($("#chart"), { ...baseOpts, autoSize: true });
  const candles = chart.addCandlestickSeries({
    upColor: C.up, downColor: C.down, borderUpColor: C.up, borderDownColor: C.down,
    wickUpColor: C.up, wickDownColor: C.down, priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  const ema = {
    fast: chart.addLineSeries({ color: C.emaF, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }),
    mid: chart.addLineSeries({ color: C.emaM, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }),
    slow: chart.addLineSeries({ color: C.emaS, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }),
  };
  const rsiChart = LWC.createChart($("#rsi-chart"), {
    ...baseOpts, autoSize: true,
    timeScale: { ...baseOpts.timeScale, visible: false },
    localization: { locale: "fr-FR", priceFormatter: (p) => p.toFixed(0) },
    rightPriceScale: { borderColor: C.line, scaleMargins: { top: 0.1, bottom: 0.1 } },
  });
  const rsi = rsiChart.addLineSeries({ color: C.accent, lineWidth: 1.5, priceLineVisible: false });
  [70, 30].forEach((v) => rsi.createPriceLine({ price: v, color: "rgba(138,147,163,.45)", lineStyle: 2, lineWidth: 1, axisLabelVisible: false }));
  rsi.createPriceLine({ price: 50, color: "rgba(138,147,163,.18)", lineStyle: 1, lineWidth: 1, axisLabelVisible: false });

  let syncing = false;
  const sync = (src, dst) => src.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (syncing || !r) return;
    syncing = true; dst.timeScale().setVisibleLogicalRange(r); syncing = false;
  });
  sync(chart, rsiChart); sync(rsiChart, chart);

  function resizeCharts() { /* autoSize gère le redimensionnement */ }

  let zoneLines = [], levelLines = [];
  function drawZones() {
    zoneLines.forEach((l) => candles.removePriceLine(l));
    zoneLines = [];
    if (!$("#t-zones").checked || !S.snap) return;
    for (const z of S.snap.zones) {
      zoneLines.push(candles.createPriceLine({
        price: z.price, lineStyle: 2, lineWidth: z.touches >= 2 ? 2 : 1, axisLabelVisible: false,
        color: z.kind === "support" ? "rgba(34,195,154,.35)" : "rgba(240,86,106,.35)",
      }));
    }
  }
  function drawLevels(sig) {
    levelLines.forEach((l) => candles.removePriceLine(l));
    levelLines = [];
    if (!sig) return;
    const L = [["Entrée", sig.entry, C.text, 0], ["SL", sig.sl, C.down, 2], ["TP1", sig.tp1, C.up, 2]];
    if (Math.abs(sig.tp2 - sig.tp1) > 1e-6) L.push(["TP2", sig.tp2, C.up, 2]);
    for (const [title, price, color, style] of L)
      levelLines.push(candles.createPriceLine({ price, color, lineStyle: style, lineWidth: 1, title, axisLabelVisible: true }));
  }
  function drawMarkers() {
    const m = !$("#t-sig").checked || !S.snap ? [] : S.snap.chart.markers.map((k) => ({
      time: k.time, position: k.direction === 1 ? "belowBar" : "aboveBar",
      color: k.direction === 1 ? C.up : C.down, shape: k.direction === 1 ? "arrowUp" : "arrowDown",
      text: k.quality, size: 1.1,
    }));
    candles.setMarkers(m);
  }
  $("#t-zones").addEventListener("change", drawZones);
  $("#t-sig").addEventListener("change", drawMarkers);
  $("#t-ema").addEventListener("change", (e) => Object.values(ema).forEach((s) => s.applyOptions({ visible: e.target.checked })));
  $("#fit-btn").addEventListener("click", () => { focusLatest(); });

  function focusLatest() {
    const n = S.snap?.chart.candles.length || 0;
    chart.timeScale().setVisibleLogicalRange({ from: n - 140, to: n + 8 });
  }
  function focusTime(isoClose) {
    const t = Math.floor(new Date(isoClose).getTime() / 1000) - 300;
    const arr = S.snap?.chart.candles || [];
    const i = arr.findIndex((c) => c.time >= t);
    if (i < 0) { toast("Signal hors de la fenêtre affichée", "Il est plus ancien que les 600 dernières bougies.", "err"); return false; }
    chart.timeScale().setVisibleLogicalRange({ from: i - 70, to: i + 70 });
    return true;
  }

  chart.subscribeCrosshairMove((p) => {
    const bar = p && p.time ? p.seriesData.get(candles) : null;
    renderLegend(bar);
  });
  chart.subscribeClick((p) => {
    if (!p || !p.time || !S.snap) return;
    const mk = S.snap.chart.markers.find((m) => m.time === p.time);
    if (!mk) return;
    const sig = S.journal.find((j) => j.key === mk.key) || null;
    if (sig) selectSignal(sig, false);
  });

  function renderLegend(bar) {
    const s = S.snap;
    if (!s) return;
    const last = s.chart.candles[s.chart.candles.length - 1];
    const b = bar || last;
    const e = (arr) => { const v = arr.find((x) => x.time === b.time); return v ? v.value.toFixed(2) : "—"; };
    $("#legend").innerHTML = `
      <span>O <span class="mono">${f5(b.open)}</span></span><span>H <span class="mono">${f5(b.high)}</span></span>
      <span>L <span class="mono">${f5(b.low)}</span></span><span>C <span class="mono ${b.close >= b.open ? "up" : "down"}">${f5(b.close)}</span></span>
      <span><i style="background:${C.emaF}"></i>EMA 20 <span class="mono">${e(s.chart.ema_fast)}</span></span>
      <span><i style="background:${C.emaM}"></i>EMA 50 <span class="mono">${e(s.chart.ema_mid)}</span></span>
      <span><i style="background:${C.emaS}"></i>EMA 200 <span class="mono">${e(s.chart.ema_slow)}</span></span>`;
  }

  /* ------------------------------------------------------------------ état */
  async function loadState() {
    try {
      const s = await api("/api/state");
      if (!s.ready) {
        setFeed("err", s.error ? "Données indisponibles" : "Chargement…");
        if (s.error) banner(s.error);
        return;
      }
      S.snap = s;
      banner(null);
      render();
    } catch (e) {
      setFeed("err", "Serveur injoignable");
    }
  }

  function render() {
    const s = S.snap;
    setFeed(s.source === "demo" ? "demo" : "live", s.source_label);
    $("#price").textContent = f5(s.price);
    const ch = s.change_24h_pips;
    $("#chg").innerHTML = ch == null ? "" : `<span class="${ch >= 0 ? "up" : "down"}">${sgn(ch)} pips · 24 h</span>`;
    $("#day-high").textContent = f5(s.context.day_high);
    $("#day-low").textContent = f5(s.context.day_low);
    $("#atr").textContent = s.context.atr_pips.toFixed(1) + " pips";
    $("#last-close").textContent = fmtDate(s.last_close) + " UTC";
    document.title = `${f5(s.price)} · XAUUSD M5`;

    candles.setData(s.chart.candles);
    ema.fast.setData(s.chart.ema_fast);
    ema.mid.setData(s.chart.ema_mid);
    ema.slow.setData(s.chart.ema_slow);
    rsi.setData(s.chart.rsi);
    const lastRsi = s.chart.rsi[s.chart.rsi.length - 1];
    $("#rsi-val").textContent = lastRsi ? lastRsi.value.toFixed(1) : "";
    drawMarkers(); drawZones();
    if (!S.fitted) { focusLatest(); S.fitted = true; }
    renderLegend();

    const showSig = S.selected || (s.radar && !s.radar.blocked ? s.radar : null);
    drawLevels(showSig);
    renderRadar(); renderContext(); renderZones(); renderRecent();
    const open = s.journal_stats.open;
    $("#open-count").hidden = !open;
    $("#open-count").textContent = open;
  }

  function gauge(score, min, dir) {
    const r = 31, c = 2 * Math.PI * r, pct = Math.max(0, Math.min(1, score / 10));
    const col = score >= min ? (dir === 1 ? C.up : C.down) : C.muted;
    const minA = (min / 10) * c;
    return `<div class="gauge"><svg width="74" height="74" viewBox="0 0 74 74">
      <circle cx="37" cy="37" r="${r}" fill="none" stroke="${C.line}" stroke-width="6"/>
      <circle cx="37" cy="37" r="${r}" fill="none" stroke="${col}" stroke-width="6" stroke-linecap="round" stroke-dasharray="${pct * c} ${c}"/>
      <circle cx="37" cy="37" r="${r}" fill="none" stroke="${C.warn}" stroke-width="8" stroke-dasharray="1.5 ${c}" stroke-dashoffset="${-minA}"/>
      </svg><div class="val"><div>${score.toFixed(1)}<small>/ 10</small></div></div></div>`;
  }

  function sigBlock(sig, title) {
    const buy = sig.direction === 1;
    const valid = !sig.blocked;
    const facts = sig.factors.map((f) => `<li class="${f.tone}"><span class="ic">${f.points > 0 ? "✓" : f.points < 0 ? "✕" : "·"}</span>
        <span>${esc(f.label)}</span><span class="pts">${f.points ? sgn(f.points, f.points % 1 ? 1 : 0) : "0"}</span></li>`).join("");
    return `
      <div class="radar-top">
        ${gauge(sig.score, S.snap.min_score, sig.direction)}
        <div>
          <span class="side-badge ${buy ? "buy" : "sell"}">${buy ? "▲ ACHAT" : "▼ VENTE"}</span>
          <span class="q-badge q-${sig.quality}">${sig.quality}</span>
          <div class="radar-title">${esc(sig.pattern)}</div>
          <div class="muted sm">${title}</div>
        </div>
      </div>
      <div class="verdict ${valid ? "ok" : "no"} ${buy ? "" : "sell"}">${valid ? "Signal validé — toutes les conditions sont réunies" : "Pas de signal : " + esc(sig.blocked)}</div>
      <div class="levels">
        <div><span class="label">Entrée</span><b>${f5(sig.entry)}</b></div>
        <div><span class="label">Stop</span><b class="down">${f5(sig.sl)}</b><span class="sub">${sig.risk_pips} p</span></div>
        <div><span class="label">TP1</span><b class="up">${f5(sig.tp1)}</b><span class="sub">1:${sig.rr1}</span></div>
        <div><span class="label">TP2</span><b class="up">${f5(sig.tp2)}</b><span class="sub">1:${sig.rr2}</span></div>
      </div>
      ${mlLine(sig.ml)}
      ${placeable(sig) ? `<button class="btn place-btn ${buy ? "buy" : "sell"}" data-place="${esc(sig.key)}">${buy ? "▲" : "▼"} Placer ce trade</button>` : ""}
      <ul class="factors">${facts}</ul>`;
  }

  // ---- bouton « Placer » : place un signal à la main (même robot désactivé)
  const placeable = (sig) => sig && sig.key && sig.close_time &&
    (Date.now() - new Date(sig.close_time).getTime()) < 30 * 60 * 1000 && sig.blocked !== "Figures achat et vente contradictoires";

  async function placeFlow(key) {
    let p;
    try { p = await api("/api/trading/place/" + encodeURIComponent(key)); }
    catch (e) { toast("Impossible de placer ce trade", e.message, "err", 8000); return; }
    const buy = p.side === "ACHAT";
    const moved = p.moved_pips ? `<span class="${p.moved_pips > 0 ? "down" : "up"}">${p.moved_pips > 0 ? "+" : ""}${p.moved_pips} pips depuis le signal</span>` : "au prix du signal";
    const warn = p.quality_warnings.length
      ? `<div class="box-warn"><b>Le robot ne l'aurait pas pris :</b><ul>${p.quality_warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
    const body = `
      <div class="place-sum">
        <span class="side-badge ${buy ? "buy" : "sell"}">${buy ? "▲ ACHAT" : "▼ VENTE"}</span>
        <b>${esc(p.pattern)}</b> <span class="muted">${p.score}/10 · compte ${p.account}</span></div>
      <div class="levels" style="margin:12px 0">
        <div><span class="label">Prix actuel</span><b>${f5(p.price)}</b><span class="sub">${moved}</span></div>
        <div><span class="label">Stop</span><b class="down">${f5(p.sl)}</b><span class="sub">${p.sl_pips} p</span></div>
        <div><span class="label">Objectif${p.tp1 === p.tp2 ? "" : " 1"}</span><b class="up">${f5(p.tp1)}</b></div>
        ${p.tp1 === p.tp2 ? "" : `<div><span class="label">Objectif 2</span><b class="up">${f5(p.tp2)}</b></div>`}
      </div>
      <p>Lot <b>${p.volume}</b> · montant risqué <b>${money(p.risk_money, p.currency)}</b> · spread ${p.spread_pips} pips</p>
      ${p.ok ? "" : `<p class="down">Le risque réglé est trop faible pour le lot minimum (${p.volume_min}).</p>`}
      ${warn}
      <p class="muted sm">Le robot gère ensuite la position comme les autres : break-even, stop suiveur, retournement, sortie au temps.</p>`;
    confirmModal(`Placer ce trade ${buy ? "à l'achat" : "à la vente"} ?`, body, async () => {
      try {
        const r = await api("/api/trading/place/" + encodeURIComponent(key), { method: "POST" });
        toast("Trade placé", r.orders.map((o) => `#${o.ticket} ${o.volume} lot à ${f5(o.price)}`).join(" · "), buy ? "buy" : "sell", 8000);
      } catch (e) { toast("Ordre refusé", e.message, "err", 9000); }
      if (typeof loadBot === "function") loadBot();
    }, "Placer");
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-place]");
    if (b) { e.preventDefault(); e.stopPropagation(); placeFlow(b.dataset.place); }
  });

  function mlLine(ml) {
    if (!ml) return "";
    const p = Math.round(ml.proba * 100), thr = Math.round(ml.threshold * 100);
    return `<div class="ml-proba" title="Estimée par le modèle IA entraîné sur ton historique${ml.useful ? "" : " — modèle non utilisé comme filtre (pas meilleur que la stratégie seule sur la période test)"}">
      <span>IA : <b class="${ml.pass ? "up" : "down"}">${p} %</b> de chances de gain</span>
      <span class="bar"><i class="${ml.pass ? "" : "low"}" style="width:${p}%"></i><b style="left:${thr}%"></b></span>
      <span class="muted sm">seuil ${thr} %${ml.useful ? "" : " · indicatif"}</span></div>`;
  }

  function renderRadar() {
    const s = S.snap, body = $("#radar-body");
    if (S.selected) {
      const j = S.selected;
      $("#radar-time").innerHTML = `<button class="link" id="unselect">← Revenir au direct</button>`;
      body.innerHTML = sigBlock({ ...j, close_time: j.time, blocked: null, rr1: rr(j, j.tp1), rr2: rr(j, j.tp2) }, `Signal du ${fmtDate(j.time)} UTC · ${statusLabel(j.status)}`);
      $("#unselect").onclick = () => { S.selected = null; render(); focusLatest(); };
      return;
    }
    $("#radar-time").textContent = "bougie de " + fmtDate(s.last_close, false) + " UTC";
    if (s.radar) {
      body.innerHTML = sigBlock(s.radar, "Analyse de la dernière bougie clôturée");
    } else {
      const prev = s.radar_recent;
      body.innerHTML = `<div class="radar-empty"><b>Aucune figure sur la dernière bougie</b>Le système attend une figure de chandelier exploitable.</div>
        ${prev ? `<div class="radar-prev">Dernier setup repéré à ${fmtDate(prev.close_time, false)} : <b>${prev.side} ${esc(prev.pattern)}</b> — ${prev.score}/10${prev.blocked ? " (" + esc(prev.blocked) + ")" : " ✓ validé"}
          ${placeable(prev) ? `<button class="btn sm place-btn ${prev.direction === 1 ? "buy" : "sell"}" data-place="${esc(prev.key)}">${prev.direction === 1 ? "▲" : "▼"} Placer ce trade</button>` : ""}</div>` : ""}`;
    }
  }
  const rr = (j, t) => (Math.abs(t - j.entry) / Math.max(Math.abs(j.entry - j.sl), 1e-9)).toFixed(2);

  function renderContext() {
    const c = S.snap.context;
    const trend = (v) => v === 1 ? `<b class="up">▲ Haussière</b>` : v === -1 ? `<b class="down">▼ Baissière</b>` : `<b class="muted">↔ Neutre</b>`;
    const rsiTxt = c.rsi >= 70 ? "Suracheté" : c.rsi <= 30 ? "Survendu" : c.rsi >= 55 ? "Acheteurs" : c.rsi <= 45 ? "Vendeurs" : "Équilibre";
    const adxTxt = c.adx >= 25 ? "Tendance forte" : c.adx >= 18 ? "Directionnel" : "Sans direction";
    const sess = c.sessions.map(([a, b]) => `${a}h–${b}h`).join(" · ");
    $("#ctx").innerHTML = `
      <div class="ctx"><span class="label">Tendance H1</span>${trend(c.h1_bias)}</div>
      <div class="ctx"><span class="label">Tendance M5</span>${trend(c.m5_trend)}</div>
      <div class="ctx"><span class="label">RSI 14</span><b>${c.rsi.toFixed(0)} · <span class="muted">${rsiTxt}</span></b><div class="bar"><i style="width:${c.rsi}%;background:${c.rsi >= 70 ? C.down : c.rsi <= 30 ? C.up : C.accent}"></i></div></div>
      <div class="ctx"><span class="label">ADX 14</span><b>${c.adx.toFixed(0)} · <span class="muted">${adxTxt}</span></b><div class="bar"><i style="width:${Math.min(100, c.adx * 2)}%;background:${c.adx >= 18 ? C.accent : C.muted}"></i></div></div>
      <div class="ctx"><span class="label">Volatilité (ATR)</span><b>${c.atr_pips.toFixed(1)} pips</b></div>
      <div class="ctx"><span class="label">Session</span><b class="${c.in_session ? "up" : "muted"}">${c.in_session ? "● Active" : "○ Fermée"}</b><span class="muted sm">${sess} UTC</span></div>`;
  }

  function renderZones() {
    const s = S.snap;
    const zs = [...s.zones].sort((a, b) => b.price - a.price);
    let html = "", placed = false;
    for (const z of zs) {
      if (!placed && z.price < s.price) { html += `<div class="zone now">prix ${f5(s.price)}</div>`; placed = true; }
      const dots = [1, 2, 3, 4].map((k) => `<i class="${z.touches >= k ? "on" : ""}"></i>`).join("");
      html += `<div class="zone"><span class="k ${z.kind}">${z.kind === "support" ? "Support" : "Résistance"}</span>
        <span class="dots" title="${z.touches} touche(s)">${dots}</span><span class="mono">${f5(z.price)}</span>
        <span class="d mono">${sgn(z.distance_pips)}</span></div>`;
    }
    if (!placed) html += `<div class="zone now">prix ${f5(s.price)}</div>`;
    $("#zones").innerHTML = zs.length ? html : `<div class="muted sm">Pas encore assez de pivots.</div>`;
  }

  const statusLabel = (st) => ({ "en cours": "En cours", TP2: "TP2 atteint", BE: "TP1 + BE", SL: "Stop", temps: "Clôturé (temps)" }[st] || st);
  const statusPill = (st) => `<span class="status st-${st.replace(" ", "")}">${statusLabel(st)}</span>`;
  const rCell = (j) => j.status === "en cours" ? `<span class="muted">${sgn(j.result_r, 2)} R</span>` : `<span class="${j.result_r > 0 ? "up" : j.result_r < 0 ? "down" : "muted"}">${sgn(j.result_r, 2)} R</span>`;

  function renderRecent() {
    const rec = S.snap.recent;
    if (!rec.length) { $("#recent").innerHTML = `<div class="muted sm">Aucun signal pour l'instant.</div>`; return; }
    $("#recent").innerHTML = rec.map((j, k) => `<div class="rec" data-k="${k}">
      <span class="t">${fmtDate(j.time)}</span>
      <span><b class="${j.direction === 1 ? "up" : "down"}">${j.direction === 1 ? "▲" : "▼"}</b> ${esc(j.pattern)} <span class="q-badge q-${j.quality}">${j.quality}</span></span>
      ${statusPill(j.status)}</div>`).join("");
    $$(".rec").forEach((el) => el.addEventListener("click", () => selectSignal(rec[+el.dataset.k])));
  }

  function selectSignal(j, move = true) {
    S.selected = j;
    show("analyse");
    render();
    if (move) focusTime(j.time);
  }

  /* ------------------------------------------------------------------ journal */
  const F = { side: "", status: "", quality: "" };
  async function loadJournal() {
    try {
      const d = await api("/api/journal");
      S.journal = d.signals;
      renderJournalKpis(d.stats);
      renderJournal();
    } catch (e) { toast("Journal indisponible", e.message, "err"); }
  }
  function renderJournalKpis(st) {
    const k = (label, val, hint, cls = "") => `<div class="kpi"><span class="label">${label}</span><b class="${cls}">${val}</b>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
    $("#journal-kpis").innerHTML = st.closed
      ? k("Signaux clôturés", st.closed, `${st.open} en cours`) +
        k("Réussite", st.win_rate + " %", "trades > 0 R") +
        k("Résultat total", sgn(st.total_r, 2) + " R", "", st.total_r >= 0 ? "up" : "down") +
        k("Espérance", sgn(st.expectancy_r, 2) + " R", "par signal") +
        k("Profit factor", st.profit_factor ?? "—", "gains / pertes")
      : k("Signaux clôturés", 0, `${st.open} en cours`) + k("Statistiques", "—", "disponibles après les premiers résultats");
  }
  function renderJournal() {
    const rows = S.journal.filter((j) => (!F.side || j.side === F.side) && (!F.status || j.status === F.status) && (!F.quality || j.quality === F.quality));
    $("#journal-empty").hidden = rows.length > 0;
    $("#journal-table tbody").innerHTML = rows.map((j, k) => `
      <tr class="row" data-k="${k}">
        <td class="mono">${fmtDate(j.time)}</td>
        <td><span class="side-badge ${j.direction === 1 ? "buy" : "sell"}">${j.direction === 1 ? "▲ ACHAT" : "▼ VENTE"}</span></td>
        <td>${esc(j.pattern)} ${j.origin === "historique" ? `<span class="muted sm" title="Détecté au démarrage sur l'historique">· hist.</span>` : ""}</td>
        <td><span class="q-badge q-${j.quality}">${j.quality}</span> <span class="mono muted">${j.score}</span></td>
        <td class="r">${f5(j.entry)}</td><td class="r down">${f5(j.sl)}</td><td class="r up">${f5(j.tp1)}</td><td class="r up">${f5(j.tp2)}</td>
        <td>${statusPill(j.status)}</td><td class="r">${rCell(j)}</td>
      </tr>`).join("");
    $$("#journal-table tr.row").forEach((tr) => tr.addEventListener("click", () => toggleDetail(tr, rows[+tr.dataset.k])));
  }
  function toggleDetail(tr, j) {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("detail")) { next.remove(); return; }
    const d = document.createElement("tr");
    d.className = "detail";
    d.innerHTML = `<td colspan="10"><div class="detail-grid">
      <div><h3 style="margin-bottom:8px">Pourquoi ce signal</h3><ul class="factors">${j.factors.map((f) => `<li class="${f.tone}"><span class="ic">${f.points > 0 ? "✓" : f.points < 0 ? "✕" : "·"}</span><span>${esc(f.label)}</span><span class="pts">${sgn(f.points, f.points % 1 ? 1 : 0)}</span></li>`).join("")}</ul></div>
      <div><h3 style="margin-bottom:8px">Note personnelle</h3><textarea placeholder="Pris ? Pas pris ? Ce que tu as observé…">${esc(j.note)}</textarea>
      <div style="display:flex;gap:8px;margin-top:8px"><button class="btn sm primary" data-a="save">Enregistrer la note</button><button class="btn sm ghost" data-a="chart">Voir sur le graphique</button>${placeable({ ...j, close_time: j.time }) && j.status === "en cours" ? `<button class="btn sm place-btn ${j.direction === 1 ? "buy" : "sell"}" data-place="${esc(j.key)}">Placer ce trade</button>` : ""}</div>
      <div class="muted sm" style="margin-top:8px">${j.exit_time ? "Sortie : " + fmtDate(j.exit_time) + " UTC · " : ""}Risque ${j.risk_pips} pips</div></div></div></td>`;
    tr.after(d);
    $("[data-a=save]", d).onclick = async () => {
      await api(`/api/journal/${j.key}/note`, { method: "PUT", body: JSON.stringify({ note: $("textarea", d).value }) });
      j.note = $("textarea", d).value;
      toast("Note enregistrée", "", "");
    };
    $("[data-a=chart]", d).onclick = () => selectSignal(j);
  }
  $$("#f-side button").forEach((b) => b.addEventListener("click", () => {
    $$("#f-side button").forEach((x) => x.classList.toggle("on", x === b));
    F.side = b.dataset.v; renderJournal();
  }));
  $("#f-status").addEventListener("change", (e) => { F.status = e.target.value; renderJournal(); });
  $("#f-quality").addEventListener("change", (e) => { F.quality = e.target.value; renderJournal(); });

  /* ------------------------------------------------------------------ backtest */
  let eqChart = null, eqSeries = null;
  $("#bt-run").addEventListener("click", async () => {
    const btn = $("#bt-run");
    btn.disabled = true; btn.textContent = "Calcul en cours…";
    try {
      const body = { bars: +$("#bt-bars").value, strategy: {} };
      if ($("#bt-score").value !== "") body.strategy.min_score = +$("#bt-score").value;
      if ($("#bt-spread").value !== "") body.strategy.spread_pips = +$("#bt-spread").value;
      renderBacktest(await api("/api/backtest", { method: "POST", body: JSON.stringify(body) }));
    } catch (e) { toast("Backtest impossible", e.message, "err"); }
    finally { btn.disabled = false; btn.textContent = "Lancer le backtest"; }
  });

  function groupRows(rows, max) {
    return rows.map((g) => {
      const w = Math.min(50, (Math.abs(g.total_r) / max) * 50);
      const bar = g.total_r >= 0 ? `<i style="left:50%;width:${w}%;background:${C.up}"></i>` : `<i style="right:50%;width:${w}%;background:${C.down}"></i>`;
      return `<tr><td>${esc(g.key)}</td><td class="r">${g.trades}</td><td class="r">${g.win_rate.toFixed(0)} %</td>
        <td class="r ${g.expectancy_r >= 0 ? "up" : "down"}">${sgn(g.expectancy_r, 2)}</td>
        <td class="bar-cell"><div style="display:flex;align-items:center;gap:8px"><div class="hbar" style="flex:1"><span class="zero"></span>${bar}</div><span class="mono sm ${g.total_r >= 0 ? "up" : "down"}" style="width:62px;text-align:right">${sgn(g.total_r, 1)} R</span></div></td></tr>`;
    }).join("");
  }

  function renderBacktest(r) {
    const st = r.stats;
    $("#bt-empty").hidden = true;
    $("#bt-result").hidden = false;
    if (!st.trades) {
      $("#bt-kpis").innerHTML = `<div class="kpi"><span class="label">Trades</span><b>0</b><div class="hint">aucun signal sur la période</div></div>`;
      return;
    }
    const k = (label, val, hint, cls = "") => `<div class="kpi"><span class="label">${label}</span><b class="${cls}">${val}</b><div class="hint">${hint}</div></div>`;
    $("#bt-kpis").innerHTML =
      k("Trades", st.trades, Object.entries(st.exits).map(([a, b]) => `${a} ${b}`).join(" · ")) +
      k("Réussite", st.win_rate + " %", `gain moyen ${sgn(st.avg_win_r, 2)} R`) +
      k("Résultat", sgn(st.total_r, 1) + " R", `≈ ${sgn(st.total_r, 1)} % à 1 % de risque`, st.total_r >= 0 ? "up" : "down") +
      k("Espérance", sgn(st.expectancy_r, 3) + " R", "par trade", st.expectancy_r >= 0 ? "up" : "down") +
      k("Profit factor", st.profit_factor ?? "∞", st.profit_factor >= 1.3 ? "solide" : st.profit_factor >= 1 ? "fragile" : "perdant") +
      k("Drawdown max", "−" + st.max_drawdown_r + " R", `${st.max_losing_streak} pertes d'affilée max`, "down") +
      (r.management && r.management.rules.active && r.management.classic.trades
        ? k("Gestion classique", sgn(r.management.classic.total_r, 1) + " R",
            `sans stop suiveur ni retournement · ${sgn(r.management.classic.expectancy_r, 3)} R / trade`,
            r.management.classic.total_r > st.total_r ? "down" : "muted") : "");
    $("#bt-period").textContent = `${fmtDate(r.period.start)} → ${fmtDate(r.period.end)} · ${r.period.bars.toLocaleString("fr-FR")} bougies · ${r.source}`;

    if (!eqChart) {
      eqChart = LWC.createChart($("#equity-chart"), { ...baseOpts, autoSize: true, localization: { locale: "fr-FR", priceFormatter: (p) => p.toFixed(1) + " R" } });
      eqSeries = eqChart.addBaselineSeries({ baseValue: { type: "price", price: 0 }, topLineColor: C.up, bottomLineColor: C.down,
        topFillColor1: "rgba(34,195,154,.25)", topFillColor2: "rgba(34,195,154,.02)", bottomFillColor1: "rgba(240,86,106,.02)", bottomFillColor2: "rgba(240,86,106,.25)", lineWidth: 2 });
    }
    const seen = new Set();
    const eq = r.equity.map((p) => ({ time: Math.floor(new Date(p.time).getTime() / 1000), value: p.value }))
      .filter((p) => !seen.has(p.time) && seen.add(p.time));
    eqSeries.setData(eq);
    eqChart.timeScale().fitContent();

    const maxP = Math.max(1, ...r.by_pattern.map((g) => Math.abs(g.total_r)));
    $("#bt-patterns tbody").innerHTML = groupRows(r.by_pattern, maxP);
    const maxQ = Math.max(1, ...r.by_quality.map((g) => Math.abs(g.total_r)));
    $("#bt-quality tbody").innerHTML = groupRows([...r.by_quality].sort((a, b) => ["A+", "A", "B"].indexOf(a.key) - ["A+", "A", "B"].indexOf(b.key)), maxQ);

    const hours = Object.fromEntries(r.by_hour.map((g) => [+g.key, g]));
    const maxH = Math.max(1, ...r.by_hour.map((g) => Math.abs(g.total_r)));
    let html = "";
    for (let h = 0; h < 24; h++) {
      const g = hours[h];
      const v = g ? g.total_r : 0;
      html += `<div class="h" title="${h}h : ${g ? g.trades + " trades, " + sgn(v, 1) + " R" : "aucun trade"}">
        <span class="v">${g ? sgn(v, 0) : ""}</span>
        <div class="col" style="height:${g ? Math.max(3, (Math.abs(v) / maxH) * 85) : 0}%;background:${v >= 0 ? C.up : C.down};opacity:${g ? 0.85 : 0}"></div>
        <span class="lab">${h}</span></div>`;
    }
    $("#bt-hours").innerHTML = html;

    $("#bt-trades tbody").innerHTML = r.trades.slice(0, 500).map((t) => `<tr>
      <td class="mono">${fmtDate(t.time)}</td><td><span class="side-badge ${t.side === "ACHAT" ? "buy" : "sell"}">${t.side === "ACHAT" ? "▲" : "▼"} ${t.side}</span></td>
      <td>${esc(t.pattern)}</td><td><span class="q-badge q-${t.quality}">${t.quality}</span> <span class="mono muted">${t.score}</span></td>
      <td class="r">${f5(t.entry)}</td><td>${statusPill(t.exit)}</td>
      <td class="r ${t.result_r > 0 ? "up" : t.result_r < 0 ? "down" : "muted"}">${sgn(t.result_r, 2)} R</td></tr>`).join("");
  }

  /* ------------------------------------------------------------------ réglages */
  const form = $("#settings-form");
  async function fillSettings() {
    try {
      [S.settings, S.meta] = await Promise.all([api("/api/settings"), S.meta ? S.meta : api("/api/meta")]);
    } catch (e) { toast("Réglages indisponibles", e.message, "err"); return; }
    const cfg = S.settings;
    for (const el of form.elements) {
      if (!el.name) continue;
      const v = el.name.startsWith("strategy.") ? cfg.strategy[el.name.slice(9)] : cfg[el.name];
      if (el.type === "checkbox") el.checked = !!v;
      else if (el.name === "strategy.sessions_utc") el.value = v.map(([a, b]) => `${a}-${b}`).join(", ");
      else el.value = v ?? "";
    }
    $("#ms-out").textContent = (+cfg.strategy.min_score).toFixed(1);
    $("#pattern-list").innerHTML = S.meta.patterns.map((p) => `<label><input type="checkbox" value="${esc(p)}" ${cfg.strategy.disabled_patterns.includes(p) ? "" : "checked"}>${esc(p)}</label>`).join("");
    $("#n-browser").checked = S.prefs.browser;
    $("#n-sound").checked = S.prefs.sound;
    $("#bt-score").placeholder = cfg.strategy.min_score;
    $("#bt-spread").placeholder = cfg.strategy.spread_pips;
    $("#save-msg").textContent = "";
  }
  form.elements["strategy.min_score"].addEventListener("input", (e) => { $("#ms-out").textContent = (+e.target.value).toFixed(1); });
  $("#reset-btn").addEventListener("click", fillSettings);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = { strategy: {} };
    try {
      for (const el of form.elements) {
        if (!el.name) continue;
        let v = el.type === "checkbox" ? el.checked : el.type === "number" || el.type === "range" ? +el.value : el.value.trim();
        if (el.name === "strategy.sessions_utc") {
          v = v ? v.split(",").map((p) => p.trim().split("-").map(Number)) : [];
          if (v.some((p) => p.length !== 2 || p.some(isNaN))) throw new Error("Sessions : écris par ex. 7-11, 12-17");
        }
        if (el.name.startsWith("strategy.")) out.strategy[el.name.slice(9)] = v; else out[el.name] = v;
      }
      out.strategy.disabled_patterns = $$("#pattern-list input").filter((i) => !i.checked).map((i) => i.value);
      $("#save-msg").textContent = "Enregistrement et nouvelle analyse…";
      S.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(out) });
      $("#save-msg").textContent = "✓ Réglages enregistrés dans config.yaml";
      toast("Réglages enregistrés", "L'analyse a été relancée avec les nouveaux paramètres.", "");
      loadState();
    } catch (err) {
      $("#save-msg").textContent = "";
      toast("Réglages refusés", err.message, "err");
    }
  });
  $("#n-browser").addEventListener("change", async (e) => { await setBrowserAlerts(e.target.checked); e.target.checked = S.prefs.browser; });
  $("#n-sound").addEventListener("change", (e) => { S.prefs.sound = e.target.checked; store.set("sound", S.prefs.sound); syncAlertBtn(); });

  /* ------------------------------------------------------------------ alertes */
  async function setBrowserAlerts(on) {
    if (on && "Notification" in window && Notification.permission !== "granted") {
      const p = await Notification.requestPermission();
      if (p !== "granted") { toast("Notifications refusées", "Autorise-les dans les réglages du navigateur.", "err"); on = false; }
    }
    S.prefs.browser = on; store.set("browser", on); syncAlertBtn();
  }
  function syncAlertBtn() {
    const on = S.prefs.browser || S.prefs.sound;
    $("#alerts-btn").classList.toggle("on", on);
    $("#alerts-btn").title = `Alertes : navigateur ${S.prefs.browser ? "activé" : "désactivé"}, son ${S.prefs.sound ? "activé" : "désactivé"}`;
  }
  $("#alerts-btn").addEventListener("click", async () => {
    const on = !(S.prefs.browser || S.prefs.sound);
    S.prefs.sound = on; store.set("sound", on);
    await setBrowserAlerts(on);
    toast(on ? "Alertes activées" : "Alertes coupées", on ? "Son + notification du navigateur à chaque signal." : "", "");
  });

  let audio;
  function beep(dir) {
    if (!S.prefs.sound) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const notes = dir === 1 ? [660, 880] : [880, 660];
      notes.forEach((f, k) => {
        const o = audio.createOscillator(), g = audio.createGain();
        o.type = "sine"; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, audio.currentTime + k * 0.18);
        g.gain.exponentialRampToValueAtTime(0.25, audio.currentTime + k * 0.18 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + k * 0.18 + 0.16);
        o.connect(g).connect(audio.destination);
        o.start(audio.currentTime + k * 0.18); o.stop(audio.currentTime + k * 0.18 + 0.18);
      });
    } catch { /* audio indisponible */ }
  }

  function onSignal(sig) {
    const buy = sig.direction === 1;
    const title = `${buy ? "▲ ACHAT" : "▼ VENTE"} XAUUSD — ${sig.quality} (${sig.score}/10)`;
    const body = `${sig.pattern} · Entrée ${f5(sig.entry)} · SL ${f5(sig.sl)} · TP ${f5(sig.tp1)}`;
    const el = toast(title, body, buy ? "buy" : "sell", 30000);
    if (el && sig.key) {
      const b = document.createElement("button");
      b.className = "btn sm place-btn " + (buy ? "buy" : "sell");
      b.dataset.place = sig.key;
      b.textContent = (buy ? "▲" : "▼") + " Placer";
      el.append(b);
    }
    beep(sig.direction);
    if (S.prefs.browser && "Notification" in window && Notification.permission === "granted") {
      const n = new Notification(title, { body, tag: sig.key });
      n.onclick = () => { window.focus(); n.close(); };
    }
  }

  function toast(title, body, kind = "", ms = 4500) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML = `<b>${esc(title)}</b>${body ? `<div class="muted">${esc(body)}</div>` : ""}`;
    $("#toasts").append(el);
    setTimeout(() => el.remove(), ms);
    el.addEventListener("click", () => el.remove());
    return el;
  }
  function banner(msg) {
    $("#error-banner").hidden = !msg;
    if (msg) $("#error-banner").textContent = "⚠ " + msg;
  }
  function setFeed(kind, label) {
    $("#feed").className = "feed " + kind;
    $("#feed-label").textContent = label;
  }

  /* ------------------------------------------------------------------ temps réel */
  function connect() {
    const es = new EventSource("/api/stream");
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.type === "update") { loadState(); if (S.view === "signaux") loadJournal(); }
      else if (ev.type === "signal") onSignal(ev.signal);
      else if (ev.type === "error") { banner(ev.message); setFeed("err", "Erreur données"); }
      else if (ev.type === "bot") onBotEvent(ev);
      else if (ev.type === "trade") onTrade(ev);
      else if (ev.type === "quality") onQuality(ev);
    };
    es.onerror = () => setFeed("err", "Reconnexion…");
    es.onopen = () => S.snap && setFeed(S.snap.source === "demo" ? "demo" : "live", S.snap.source_label);
  }

  $("#scan-btn").addEventListener("click", async () => {
    const b = $("#scan-btn");
    b.disabled = true; b.classList.add("loading");
    try {
      const r = await api("/api/scan", { method: "POST" });
      if (r.error) toast("Analyse impossible", r.error, "err");
    } catch (e) { toast("Analyse impossible", e.message, "err"); }
    finally { b.disabled = false; b.classList.remove("loading"); }
  });

  setInterval(() => {
    const now = new Date();
    const s = 300 - ((now.getUTCMinutes() % 5) * 60 + now.getUTCSeconds());
    $("#countdown").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }, 1000);


  /* ------------------------------------------------------------------ robot */
  const B = { st: null, formFilled: false };
  const money = (v, cur) => v == null ? "—" : `${Number(v).toLocaleString("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${cur || ""}`;
  const pl = (v, cur) => `<span class="${v > 0 ? "up" : v < 0 ? "down" : "muted"}">${v > 0 ? "+" : ""}${money(v, cur)}</span>`;

  async function loadBot(fillForm = false) {
    try {
      B.st = await api("/api/trading");
      renderBot(fillForm || !B.formFilled);
    } catch (e) { /* serveur indisponible : déjà signalé par le flux */ }
  }

  function renderReady(list) {
    const items = list || [];
    $("#bot-ready").hidden = !items.length;
    const ic = { err: "✕", warn: "!", info: "i" };
    $("#bot-ready-list").innerHTML = items.map((x) => `<li class="${x.level}"><span class="ic">${ic[x.level] || "•"}</span>
      <div>${esc(x.text)}${x.fix ? `<div class="fix">→ ${esc(x.fix)}</div>` : ""}</div></li>`).join("");
    const head = $("#bot-ready h3");
    head.textContent = items.some((x) => x.level === "err") ? "Pourquoi le robot ne trade pas" : "À savoir sur les signaux du robot";
  }

  function renderBot(fillForm) {
    const st = B.st, a = st.account, cur = a?.currency || "";
    renderReady(st.readiness);
    const auto = !!st.settings.autonomous, ab = $("#bot-auto");
    ab.textContent = auto ? "Mode autonome : activé" : "Mode autonome";
    ab.classList.toggle("on", auto);
    const state = st.enabled ? (st.paused ? ["EN PAUSE", "pause"] : ["ACTIF", "on"]) : ["ARRÊTÉ", ""];
    $("#bot-state").textContent = state[0];
    $("#bot-state").className = "state-pill " + state[1];
    $("#bot-dot").className = "bot-dot " + (st.enabled ? (st.paused ? "pause" : "on") : "");
    $("#bot-sub").textContent = st.paused ? st.paused : `${st.broker || "—"} · risque ${st.settings.risk_percent} % · score ≥ ${st.settings.min_score}`
      + (st.settings.low_score_enabled ? ` (≥ ${st.settings.low_score_min} à risque × ${st.settings.low_score_risk_factor})` : "")
      + (st.settings.trailing ? ` · stop suiveur ${st.settings.trail_start_r} / ${st.settings.trail_distance_r} R` : "")
      + (st.settings.reversal_exit ? " · sortie sur retournement" : "");
    $("#bot-toggle").textContent = st.enabled ? "Désactiver le robot" : "Activer le robot";
    $("#bot-toggle").className = "btn " + (st.enabled ? "ghost" : "primary");
    $("#bot-error").hidden = !st.error;
    if (st.error) $("#bot-error").textContent = "⚠ " + st.error;

    const typ = st.mode === "paper" ? `<span class="acct-type paper">SIMULATEUR</span>`
      : a ? `<span class="acct-type ${a.is_demo ? "demo" : "real"}">${a.type}</span>` : "—";
    $("#bot-account").innerHTML = a ? `
      <div><span class="label">Compte</span><b>${typ} ${a.login || ""}</b><span class="muted sm">${esc(a.server)}</span></div>
      <div><span class="label">Solde</span><b>${money(a.balance, cur)}</b></div>
      <div><span class="label">Équité</span><b>${money(a.equity, cur)}</b></div>
      <div><span class="label">Trading auto</span><b class="${a.trade_allowed ? "up" : "down"}">${a.trade_allowed ? "Autorisé" : "Bloqué"}</b><span class="muted sm">${esc(a.trade_allowed_reason || "levier 1:" + a.leverage)}</span></div>`
      : `<div style="grid-column:1/-1"><span class="label">Compte</span><b class="down">Non connecté</b><span class="muted sm">Ouvre MetaTrader 5 et connecte-toi, ou choisis le Simulateur.</span></div>`;

    const td = st.today || {};
    const k = (label, val, hint, cls = "") => `<div class="kpi"><span class="label">${label}</span><b class="${cls}">${val}</b><div class="hint">${hint}</div></div>`;
    const dayPl = (td.realized || 0) + (td.floating || 0);
    $("#bot-kpis").innerHTML =
      k("Résultat du jour", (dayPl > 0 ? "+" : "") + money(dayPl, cur), `réalisé ${money(td.realized, cur)} · en cours ${money(td.floating, cur)}`, dayPl >= 0 ? "up" : "down") +
      k("Trades aujourd'hui", `${td.trades ?? 0} / ${st.settings.max_trades_per_day}`, "limite journalière") +
      k("Perte max du jour", td.loss_limit != null ? "−" + money(td.loss_limit, cur) : "—", `${st.settings.daily_loss_limit_percent} % du solde, puis pause`) +
      k("Risque par trade", a ? money(a.balance * st.settings.risk_percent / 100, cur) : "—", `${st.settings.risk_percent} % du solde`);

    $("#bot-positions tbody").innerHTML = st.positions.map((p) => `<tr>
      <td class="mono">#${p.ticket}</td><td><span class="side-badge ${p.side === 1 ? "buy" : "sell"}">${p.side === 1 ? "▲ ACHAT" : "▼ VENTE"}</span></td>
      <td class="r">${p.volume}</td><td class="r">${f5(p.price_open)}</td><td class="r down">${f5(p.sl)}</td><td class="r up">${f5(p.tp)}</td>
      <td class="r">${pl(p.profit, cur)}</td><td class="mono">${fmtDate(p.time_open)}</td>
      <td><button class="btn sm ghost" data-close="${p.ticket}">Clôturer</button></td></tr>`).join("")
      + (st.pending || []).map((o) => `<tr class="pending-row">
      <td class="mono muted">en attente</td><td><span class="side-badge ${o.side === "ACHAT" ? "buy" : "sell"}">${o.side === "ACHAT" ? "▲" : "▼"} ${o.side}</span></td>
      <td class="r muted">—</td><td class="r">${f5(o.level)}</td><td colspan="3" class="muted sm">entrée sur repli (signal ${f5(o.entry)}, ${esc(o.pattern)})</td>
      <td class="mono">jusqu'à ${fmtDate(o.expires, false)}</td><td></td></tr>`).join("");
    const nb = st.positions.length + (st.pending || []).length;
    $("#bot-pos-empty").hidden = nb > 0;
    $("#bot-pos-sub").textContent = nb ? `${st.positions.length} position(s)${(st.pending || []).length ? ` · ${(st.pending || []).length} entrée(s) en attente` : ""}` : "";
    $$("[data-close]").forEach((b) => b.onclick = () => confirmModal("Clôturer la position #" + b.dataset.close + " ?",
      "<p>La position sera fermée au prix du marché.</p>", async () => {
        await api("/api/trading/close/" + b.dataset.close, { method: "POST" }); loadBot();
      }));

    $("#bot-log").innerHTML = st.log.length ? st.log.map((l) => `<div class="log-row ${l.level}"><span class="t">${fmtDate(l.time, false)}</span><span>${esc(l.message)}</span></div>`).join("")
      : `<div class="muted sm">Aucune activité pour l'instant.</div>`;
    const stTxt = { ouvert: "st-encours", "fermé": "st-temps", erreur: "st-SL" };
    $("#bot-trades tbody").innerHTML = st.trades.map((t) => `<tr class="row" data-tid="${t.id}" title="${esc(t.message || "Clique pour voir l'analyse de cet ordre")}">
      <td class="mono">${fmtDate(t.time)}</td><td><span class="side-badge ${t.side === "ACHAT" ? "buy" : "sell"}">${t.side === "ACHAT" ? "▲" : "▼"} ${t.side}${t.leg ? " · TP" + t.leg : ""}</span></td>
      <td class="r">${t.volume}</td><td class="r">${f5(t.price)}</td>
      <td><span class="status ${stTxt[t.status] || ""}">${t.status === "erreur" ? "refusé" : t.status}</span>${t.mode === "paper" ? ' <span class="muted sm">sim.</span>' : ""}</td>
      <td class="r">${t.profit == null ? (t.status === "erreur" ? `<span class="muted sm">${esc(t.message)}</span>` : "—") : pl(t.profit, cur)}</td></tr>`).join("");

    renderQuality(st.quality);

    if (fillForm) {
      const f = $("#bot-form");
      for (const el of f.elements) {
        if (!el.name) continue;
        const v = st.settings[el.name];
        if (el.type === "checkbox") el.checked = !!v; else el.value = v;
      }
      B.formFilled = true;
      syncQuickPreset();
    }
    riskHint();
  }

  const QICON = { ok: "✓", fail: "✕", skip: "·" };
  const qChecks = (checks) => checks.map((c) => `<div class="q-check ${c.status}"><span class="ic">${QICON[c.status]}</span>
      <span><b>${esc(c.label)}</b><span class="muted">${esc(c.detail)}</span></span></div>`).join("");

  function renderQuality(q) {
    if (!q) return;
    const last = q.last;
    if (last) {
      $("#q-last-time").textContent = `${fmtDate(last.time, false)} · ${esc(last.signal.side)} ${esc(last.signal.pattern)}`;
      $("#q-last").innerHTML = qChecks(last.checks) +
        `<div class="q-verdict ${last.passed ? "ok" : "fail"}">${last.passed ? "✓ Tous les contrôles sont passés : ordre autorisé" : "✕ Ordre refusé : au moins un contrôle a échoué"}</div>`;
    } else {
      $("#q-last-time").textContent = "";
      $("#q-last").innerHTML = `<div class="muted sm">Aucun signal analysé par le robot depuis le lancement. Le détail s'affichera ici au prochain signal.</div>`;
    }
    const e = q.edge || {};
    if (e.computing) $("#q-edge-sub").textContent = "Calcul en cours sur ton historique…";
    else if (!e.ready) $("#q-edge-sub").innerHTML = `<span class="down">${esc(e.error || "Pas encore calculé")}</span> — clique sur Recalculer.`;
    else {
      const p = e.period || {};
      $("#q-edge-sub").textContent = `Backtest du ${fmtDate(p.start)} au ${fmtDate(p.end)} (${(p.bars || 0).toLocaleString("fr-FR")} bougies, score ≥ ${e.score_min}) · ${e.qualified.length} rentable(s), ${(e.blocked || []).length} écartée(s), ${(e.unproven || []).length} avec peu de données`;
    }
    $("#q-edge tbody").innerHTML = (e.patterns || []).map((r) => `<tr title="${esc(r.reason)}">
      <td>${esc(r.pattern)}</td><td class="r">${r.trades}</td>
      <td class="r ${r.expectancy_r >= 0 ? "up" : "down"}">${sgn(r.expectancy_r, 2)}</td>
      <td class="r">${r.profit_factor ?? "∞"}</td>
      <td>${r.qualified === true ? `<span class="status st-TP2">tradée</span>`
        : r.qualified === null ? `<span class="status st-BE" title="pas assez d'historique : tradée quand même (non bloquant)">tradée · peu de données</span>`
        : `<span class="status st-SL">${r.trades < (B.st.settings.edge_min_trades || 20) ? "bloquée · peu de données" : "écartée"}</span>`}</td></tr>`).join("")
      || `<tr><td colspan="5" class="muted sm">—</td></tr>`;
    renderML(q.ml);
    renderMgmt(e.management);
    const n = q.news || {};
    const evs = n.upcoming || [];
    $("#q-news").innerHTML = (n.error && !n.available ? `<div class="muted sm down">${esc(n.error)}</div>` : "") +
      (n.blocking_now && n.blocking_now.length ? `<div class="q-verdict fail">Annonce majeure en cours : le robot attend</div>` : "") +
      (evs.length ? evs.map((ev) => `<div class="q-ev ${ev.impact.toLowerCase()}"><span class="mono">${fmtDate(ev.time)}</span>
        <span class="imp">${ev.country}</span><span>${esc(ev.title)} <span class="muted sm">${ev.impact === "High" ? "fort impact" : "impact moyen"}</span></span></div>`).join("")
        : `<div class="muted sm">${n.available ? "Aucune annonce importante à venir cette semaine." : "Calendrier pas encore chargé."}</div>`);
  }

  function renderMgmt(m) {
    const el = $("#q-mgmt");
    if (!m) { el.innerHTML = `<div class="muted sm note">Calculé avec les statistiques.</div>`; return; }
    const col = (title, p, cls) => `<div class="col ${cls}"><div class="t">${title}</div>
      <div class="big ${p.expectancy_r >= 0 ? "up" : "down"}">${p.trades ? sgn(p.expectancy_r, 3) + " R" : "—"}</div><div class="muted sm">par trade</div>
      <div class="row"><span class="muted">Trades</span><b>${p.trades || 0}</b></div>
      <div class="row"><span class="muted">Réussite</span><b>${p.trades ? p.win_rate.toFixed(0) + " %" : "—"}</b></div>
      <div class="row"><span class="muted">Total</span><b>${p.trades ? sgn(p.total_r, 1) + " R" : "—"}</b></div>
      <div class="row"><span class="muted">Drawdown</span><b>${p.trades ? p.max_drawdown_r + " R" : "—"}</b></div></div>`;
    const ex = m.robot && m.robot.exits ? Object.entries(m.robot.exits).map(([k, v]) => `${k} ${v}`).join(" · ") : "";
    let note;
    if (!m.active) note = `<span class="muted">Stop suiveur et retournement désactivés : gestion classique.</span>`;
    else if (m.use_robot === null) note = `<span class="muted">Pas assez de trades pour trancher : tes réglages s'appliquent.</span>`;
    else if (m.use_robot) note = `<span class="up">✓ Le suivi des gains fait mieux sur l'historique : il est utilisé par le robot.</span>`;
    else note = m.auto ? `<span class="down">La gestion classique fait mieux sur l'historique : le robot l'utilise à la place.</span>`
      : `<span class="down">La gestion classique fait mieux sur l'historique (tu as choisi de garder le suivi).</span>`;
    el.innerHTML = col("Classique", m.classic || {}, "") + col("Suivi des gains", m.robot || {}, "ai")
      + `<div class="note">${note}${ex ? `<div class="muted sm">Sorties : ${esc(ex)}</div>` : ""}</div>`;
  }

  function renderML(m) {
    if (!m) return;
    const r = m.report, st = $("#ml-state");
    const perfCol = (title, p, cls) => `<div class="col ${cls}"><div class="t">${title}</div>
      <div class="big">${p.trades ? p.win_rate.toFixed(0) + " %" : "—"}</div><div class="muted sm">trades gagnants</div>
      <div class="row"><span class="muted">Trades</span><b>${p.trades}</b></div>
      <div class="row"><span class="muted">Espérance</span><b class="${p.expectancy_r >= 0 ? "up" : "down"}">${sgn(p.expectancy_r, 2)} R</b></div>
      <div class="row"><span class="muted">Total</span><b class="${p.total_r >= 0 ? "up" : "down"}">${sgn(p.total_r, 1)} R</b></div>
      <div class="row"><span class="muted">Profit factor</span><b>${p.profit_factor ?? "∞"}</b></div></div>`;
    if (!m.available) {
      st.className = "ml-state off"; st.textContent = "indisponible";
      $("#ml-sub").innerHTML = `<span class="down">${esc(m.error || "scikit-learn manquant")}</span>`;
    } else if (m.computing) {
      st.className = "ml-state"; st.textContent = "entraînement…";
      $("#ml-sub").textContent = "Entraînement et validation en cours sur ton historique…";
    } else if (!r) {
      st.className = "ml-state off"; st.textContent = "pas entraîné";
      $("#ml-sub").innerHTML = m.error ? `<span class="down">${esc(m.error)}</span>` : "Le modèle sera entraîné automatiquement avec les statistiques (ou clique sur Réentraîner).";
    } else {
      const active = m.enabled && (r.useful || m.threshold_mode === "manuel");
      st.className = "ml-state " + (active ? "on" : "off");
      st.textContent = !m.enabled ? "désactivée" : active ? "filtre actif" : "indicatif seulement";
      const P = r.periods;
      $("#ml-sub").innerHTML = `${r.samples.toLocaleString("fr-FR")} opportunités · apprentissage ${fmtDate(P.train.start)} → ${fmtDate(P.train.end)} · test ${fmtDate(P.test.start)} → ${fmtDate(P.test.end)}`
        + ` · AUC ${r.auc_test ?? "—"} <span class="muted">(0,5 = hasard)</span> · seuil ${m.threshold != null ? Math.round(m.threshold * 100) + " % (" + m.threshold_mode + ")" : "—"}`
        + (m.error ? ` · <span class="down">dernier entraînement : ${esc(m.error)}</span>` : "");
    }
    if (r) {
      const better = r.useful;
      $("#ml-compare").innerHTML = perfCol("Stratégie seule", r.test_baseline, "") + perfCol("Avec filtre IA", r.test_filtered, "ai")
        + `<div class="note ${better ? "up" : "muted"}">${better
          ? "✓ Sur la période test, le filtre IA a amélioré les résultats : il est utilisé par le robot."
          : "Le filtre IA n'a pas fait mieux que la stratégie seule sur la période test : il reste indicatif et ne bloque aucun ordre."}</div>`;
      $("#ml-calib tbody").innerHTML = (r.calibration || []).map((b) => `<tr><td>${b.range}</td><td class="r">${b.count}</td>
        <td class="r">${b.predicted} %</td><td class="r ${Math.abs(b.actual - b.predicted) <= 10 ? "up" : ""}">${b.actual} %</td></tr>`).join("")
        || `<tr><td colspan="4" class="muted sm">—</td></tr>`;
      const imp = r.importance || [], mx = Math.max(1e-9, ...imp.map((x) => x.importance));
      $("#ml-imp").innerHTML = imp.length ? imp.map((x) => `<div class="it"><span>${esc(x.label)}</span>
        <span class="bar"><i style="width:${Math.max(2, x.importance / mx * 100)}%"></i></span></div>`).join("")
        : `<div class="muted sm">Aucune caractéristique ne se détache nettement.</div>`;
    } else {
      $("#ml-compare").innerHTML = `<div class="muted sm">—</div>`;
      $("#ml-calib tbody").innerHTML = `<tr><td colspan="4" class="muted sm">—</td></tr>`;
      $("#ml-imp").innerHTML = "";
    }
  }

  $("#ml-train").addEventListener("click", async () => {
    const b = $("#ml-train"); b.disabled = true; b.textContent = "Entraînement…";
    try {
      const q = await api("/api/trading/quality/recompute", { method: "POST" });
      if (q.ml && q.ml.error) toast("Entraînement impossible", q.ml.error, "err");
      else toast("Modèle IA réentraîné", "Résultats mesurés sur la période test jamais vue.", "");
    } catch (e) { toast("Entraînement impossible", e.message, "err"); }
    finally { b.disabled = false; b.textContent = "Réentraîner"; loadBot(); }
  });

  $("#q-recompute").addEventListener("click", async () => {
    const b = $("#q-recompute"); b.disabled = true; b.textContent = "Calcul…";
    try { await api("/api/trading/quality/recompute", { method: "POST" }); toast("Statistiques recalculées", "Les figures qualifiées sont à jour.", ""); }
    catch (e) { toast("Calcul impossible", e.message, "err"); }
    finally { b.disabled = false; b.textContent = "Recalculer"; loadBot(); }
  });
  $("#q-news-refresh").addEventListener("click", async () => {
    try { await api("/api/trading/quality/news", { method: "POST" }); } catch (e) { toast("Calendrier indisponible", e.message, "err"); }
    loadBot();
  });
  $("#bot-trades").addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr.row");
    if (!tr || !B.st) return;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("detail")) { next.remove(); return; }
    const t = B.st.trades.find((x) => String(x.id) === tr.dataset.tid);
    const d = document.createElement("tr");
    d.className = "detail";
    d.innerHTML = `<td colspan="6" class="trade-analysis"><div class="q-sub">Analyse avant cet ordre</div>${t && t.analysis && t.analysis.length ? qChecks(t.analysis) : `<div class="muted sm">${esc(t && t.message || "Pas de rapport d'analyse pour cet ordre (version antérieure).")}</div>`}</td>`;
    tr.after(d);
  });

  function riskHint() {
    const a = B.st?.account;
    const r = +$("#bot-form").elements.risk_percent.value;
    $("#risk-hint").textContent = a ? `≈ ${money(a.balance * r / 100, a.currency)} risqués par trade` : "";
  }
  $("#bot-form").elements.risk_percent.addEventListener("input", riskHint);

  function confirmModal(title, html, onOk, okLabel = "Confirmer", danger = false) {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = html;
    $("#modal-ok").textContent = okLabel;
    $("#modal-ok").className = "btn " + (danger ? "danger" : "primary");
    $("#modal").hidden = false;
    const close = () => { $("#modal").hidden = true; };
    $("#modal-cancel").onclick = close;
    $("#modal-ok").onclick = async () => {
      $("#modal-ok").disabled = true;
      try { await onOk(); close(); } catch (e) { toast("Action impossible", e.message, "err", 8000); }
      finally { $("#modal-ok").disabled = false; }
    };
    const inp = $("#modal-body input");
    if (inp) setTimeout(() => inp.focus(), 50);
  }

  $("#bot-toggle").addEventListener("click", () => {
    const st = B.st;
    if (!st) return;
    if (st.enabled) {
      api("/api/trading/disable", { method: "POST" }).then(() => loadBot());
      return;
    }
    const a = st.account;
    const real = a && !a.is_demo && st.mode !== "paper";
    const risk = a ? money(a.balance * st.settings.risk_percent / 100, a.currency) : "—";
    confirmModal(real ? "⚠ Activer le robot sur un compte RÉEL" : "Activer le robot",
      `<p>Le robot va passer <b>tout seul</b> des ordres sur ${st.mode === "paper" ? "le <b>Simulateur</b> (aucun argent réel)" : `le compte <b>${a ? a.type + " " + a.login : "MetaTrader 5"}</b>`} pour chaque signal de score ≥ <b>${st.settings.min_score}</b>.</p>
       <p>Risque par trade : <b>${risk}</b> (${st.settings.risk_percent} %) · pause après −${st.settings.daily_loss_limit_percent} % dans la journée.</p>
       ${real ? `<p class="down">Argent réel en jeu. Tape <b>REEL</b> pour confirmer.</p><input id="live-confirm" autocomplete="off">` : ""}`,
      async () => {
        if (real && $("#live-confirm").value.trim().toUpperCase() !== "REEL") throw new Error("Tape REEL pour confirmer.");
        await api("/api/trading/enable", { method: "POST", body: JSON.stringify({ confirm_live: real }) });
        loadBot();
      }, "Activer", real);
  });

  async function loadProfiles() {
    try {
      const list = await api("/api/trading/profiles");
      $("#bot-profiles").innerHTML = list.map((p) => `<button type="button" class="btn sm ghost" data-profile="${p.id}" title="${esc(p.desc)}">${esc(p.label)}</button>`).join("");
      $$("[data-profile]").forEach((b) => b.onclick = () => {
        const p = list.find((x) => x.id === b.dataset.profile);
        confirmModal(`Profil « ${p.label} »`, `<p>${esc(p.desc)}</p><p class="muted sm">Le mode (MetaTrader 5 / simulateur) et l'autorisation du compte réel ne changent pas. Les statistiques du robot sont recalculées.</p>`,
          async () => { B.st = await api("/api/trading/profile", { method: "POST", body: JSON.stringify({ name: p.id }) }); renderBot(true); toast("Profil appliqué", p.label, ""); },
          "Appliquer");
      });
    } catch (e) { /* ignoré */ }
  }
  loadProfiles();

  $("#bot-auto").addEventListener("click", () => {
    const on = !(B.st && B.st.settings.autonomous);
    confirmModal(on ? "Activer le mode autonome" : "Désactiver le mode autonome",
      on ? `<p>L'application va :</p><ul>
          <li>démarrer <b>toute seule</b> à l'ouverture de ta session, sans ouvrir de fenêtre ;</li>
          <li><b>activer le robot</b> à chaque lancement, et le relancer s'il n'a pas pu démarrer (MT5 pas encore ouvert…) ;</li>
          <li>empêcher la <b>mise en veille</b> de Windows tant qu'elle tourne ;</li>
          <li>t'alerter si les données n'arrivent plus depuis 10 min, et faire un <b>bilan du jour</b> à 21 h UTC.</li></ul>
          <p class="muted sm">Garde MetaTrader 5 ouvert et connecté (ou lancé au démarrage). Tous les garde-fous restent actifs ; si tu désactives le robot toi-même, il n'est pas relancé.</p>`
         : "<p>L'application ne démarrera plus seule et le robot ne sera plus relancé automatiquement.</p>",
      async () => {
        try {
          const r = await api("/api/trading/autonomous", { method: "POST", body: JSON.stringify({ enabled: on }) });
          toast(on ? "Mode autonome activé" : "Mode autonome désactivé", r.message, "");
        } catch (e) { toast("Mode autonome", e.message, "err"); }
        loadBot();
      }, on ? "Activer" : "Désactiver");
  });

  const QUICK = { off: { quick_target_r: 0 }, petit: { quick_target_r: 0.6, trailing: true, trail_start_r: 0.4, trail_distance_r: 0.25 },
                  moyen: { quick_target_r: 1.2, trailing: true, trail_start_r: 0.7, trail_distance_r: 0.4 } };
  function syncQuickPreset() {
    const f = $("#bot-form"), q = +f.elements.quick_target_r.value || 0;
    $("#quick-preset").value = q === 0 ? "off" : q === 0.6 ? "petit" : q === 1.2 ? "moyen" : "perso";
  }
  $("#quick-preset").addEventListener("change", (e) => {
    const f = $("#bot-form"), p = QUICK[e.target.value];
    if (!p) return;
    for (const [k, v] of Object.entries(p)) {
      const el = f.elements[k];
      if (el.type === "checkbox") el.checked = v; else el.value = v;
    }
  });
  $("#bot-form").elements.quick_target_r.addEventListener("input", syncQuickPreset);

  $("#bot-test").addEventListener("click", () => confirmModal("Tester un ordre",
    "<p>Le robot va <b>acheter le lot minimum</b> puis <b>fermer aussitôt</b> la position, pour vérifier que les ordres passent bien (connexion, Algo Trading, courtier).</p><p>Uniquement sur un compte <b>démo</b>. Coût : le spread, quelques centimes.</p>",
    async () => {
      try {
        const r = await api("/api/trading/test_order", { method: "POST" });
        toast(r.ok ? "Test d'ordre réussi" : "Test d'ordre refusé", r.message, r.ok ? "" : "err");
      } catch (e) { toast("Test d'ordre impossible", e.message, "err"); }
      loadBot();
    }, "Lancer le test"));

  $("#bot-kill").addEventListener("click", () => confirmModal("Arrêt d'urgence",
    "<p>Le robot est <b>désactivé</b> et <b>toutes ses positions sont clôturées</b> immédiatement au prix du marché.</p><p>Les positions ouvertes à la main ne sont pas touchées.</p>",
    async () => { const r = await api("/api/trading/close_all", { method: "POST" }); toast("Arrêt d'urgence effectué", `${r.closed.length} position(s) clôturée(s)`, "err"); loadBot(); },
    "Tout arrêter", true));

  $("#bot-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, trading = {};
    for (const el of f.elements) {
      if (!el.name) continue;
      trading[el.name] = el.type === "checkbox" ? el.checked : el.tagName === "SELECT" ? el.value : +el.value;
    }
    const save = async (confirm) => {
      B.st = await api("/api/trading/settings", { method: "PUT", body: JSON.stringify({ trading, confirm }) });
      renderBot(true);
      $("#bot-save-msg").textContent = "✓ Réglages enregistrés";
      setTimeout(() => { $("#bot-save-msg").textContent = ""; }, 4000);
    };
    if (trading.allow_live && !B.st.settings.allow_live) {
      confirmModal("⚠ Autoriser le trading sur compte RÉEL",
        `<p>Le robot pourra alors trader de l'argent réel. Une confirmation sera encore demandée à chaque activation.</p><p>Tape <b>REEL</b> pour autoriser.</p><input id="live-allow" autocomplete="off">`,
        async () => save($("#live-allow").value.trim().toUpperCase()), "Autoriser", true);
      return;
    }
    try { await save(""); } catch (err) { toast("Réglages refusés", err.message, "err"); }
  });

  function onQuality() { if (S.view === "robot") loadBot(); }

  function onBotEvent(ev) {
    if (ev.notify) {
      const kind = { ok: "buy", err: "err", warn: "" }[ev.entry.level] || "";
      toast("Robot", ev.entry.message, kind, ev.entry.level === "err" ? 12000 : 7000);
    }
    if (S.view === "robot") loadBot(); else api("/api/trading").then((st) => { B.st = st; $("#bot-dot").className = "bot-dot " + (st.enabled ? (st.paused ? "pause" : "on") : ""); }).catch(() => {});
  }
  function onTrade(ev) {
    const buy = ev.side === "ACHAT";
    beep(buy ? 1 : -1);
    if (S.prefs.browser && "Notification" in window && Notification.permission === "granted")
      new Notification(`Robot : ${ev.side} ${ev.volume} lot${ev.mode === "paper" ? " (simulateur)" : ""}`, { body: `Prix ${f5(ev.price)} · SL ${f5(ev.sl)} · TP ${ev.tps.map(f5).join(" / ")}` });
  }
  setInterval(() => { if (S.view === "robot") loadBot(); }, 5000);
  api("/api/trading").then((st) => { B.st = st; $("#bot-dot").className = "bot-dot " + (st.enabled ? (st.paused ? "pause" : "on") : ""); }).catch(() => {});

  /* ------------------------------------------------------------------ état du système */
  const ICON = { ok: "✓", warn: "!", err: "✕", skip: "·" };
  async function loadDiag() {
    $("#diag-run").disabled = true;
    try {
      const d = await api("/api/diagnostics");
      $("#diag").innerHTML = d.checks.map((c) => `<div class="diag-row ${c.status}">
        <span class="ic">${ICON[c.status]}</span><span class="lab">${esc(c.label)}</span>
        <span class="det">${esc(c.detail)}${c.fix ? `<span class="fix">→ ${esc(c.fix)}</span>` : ""}</span></div>`).join("");
    } catch (e) { $("#diag").innerHTML = `<div class="muted sm">Diagnostic indisponible : ${esc(e.message)}</div>`; }
    finally { $("#diag-run").disabled = false; }
  }
  async function loadAbout() {
    try {
      const st = await api("/api/setup");
      $("#about-version").textContent = "v" + st.version;
      $("#about-dir").innerHTML = `Données : <span class="mono">${esc(st.data_dir)}</span>`;
    } catch { /* hors ligne */ }
  }
  $("#diag-run").addEventListener("click", loadDiag);
  $("#open-folder").addEventListener("click", async () => {
    const r = await api("/api/open-data-folder", { method: "POST" }).catch(() => null);
    if (r && !r.ok) toast("Dossier des données", r.path, "", 9000);
  });
  $("#wizard-open").addEventListener("click", () => openWizard());

  /* ------------------------------------------------------------------ assistant */
  const STEPS = ["welcome", "mt5", "market", "alerts", "done"];
  const W = { i: 0, det: null, busy: false, pick: {} };

  async function openWizard() {
    const st = await api("/api/setup").catch(() => ({}));
    W.i = 0; W.det = null;
    W.pick = { source: st.source === "demo" ? "auto" : (st.source || "auto"), symbol: st.symbol || "XAUUSD",
               broker_utc_offset: st.broker_utc_offset ?? 0, mt5_path: st.mt5_path || "", fallback: "yfinance" };
    $("#wizard").hidden = false;
    renderWizard();
  }
  function closeWizard() { $("#wizard").hidden = true; }

  function wzCheck(kind, title, detail) {
    return `<div class="wz-check ${kind}"><span class="ic">${ICON[kind]}</span><span><b>${title}</b>${detail ? `<span class="muted">${detail}</span>` : ""}</span></div>`;
  }

  async function detectMt5() {
    W.busy = true; renderWizard();
    try { W.det = await api("/api/setup/detect"); }
    catch (e) { W.det = { error: e.message, terminals: [], connected: false }; }
    W.busy = false;
    const d = W.det;
    if (d.connected) {
      W.pick.source = "auto";
      if (d.symbol) W.pick.symbol = d.symbol;
      if (d.offset != null) W.pick.broker_utc_offset = d.offset;
      if (d.terminal_path) W.pick.mt5_path = W.pick.mt5_path || "";
    }
    renderWizard();
  }

  function renderWizard() {
    const step = STEPS[W.i], d = W.det, p = W.pick;
    $("#wz-steps").innerHTML = STEPS.map((_, k) => `<i class="${k <= W.i ? "done" : ""}"></i>`).join("");
    $("#wz-back").hidden = W.i === 0 || step === "done";
    $("#wz-skip").hidden = step === "done";
    $("#wz-next").disabled = W.busy;
    let html = "";
    if (step === "welcome") {
      $("#wz-next").textContent = "Commencer";
      html = `<h2 id="wz-title">Bienvenue dans Gold AI Signal Bot</h2>
        <p class="lead">Cet assistant configure l'application en une minute. Il détecte automatiquement ton MetaTrader 5, le nom de l'or (XAUUSD, GOLD…) chez ton courtier et le décalage horaire.</p>
        <ul><li>Ouvre <b>MetaTrader 5</b> et connecte-toi à ton compte (démo pour commencer).</li>
        <li>Garde la <b>fenêtre noire</b> de l'application ouverte : c'est le moteur.</li>
        <li>Le <b>robot de trading</b> reste désactivé tant que tu ne l'actives pas.</li></ul>
        <p class="muted sm">Tu pourras relancer cet assistant à tout moment depuis Réglages.</p>`;
    } else if (step === "mt5") {
      $("#wz-next").textContent = "Continuer";
      if (W.busy || !d) {
        html = `<h2 id="wz-title">Connexion à MetaTrader 5</h2><div class="wz-spinner">Recherche de MetaTrader 5 sur ce PC…</div>`;
      } else if (d.connected) {
        const a = d.account || {};
        html = `<h2 id="wz-title">MetaTrader 5 est connecté</h2><p class="lead">Tout est prêt de ce côté.</p>
          ${wzCheck("ok", "Terminal trouvé", esc(d.terminal_path || "terminal par défaut"))}
          ${wzCheck(a.type === "RÉEL" ? "warn" : "ok", `Compte ${esc(a.type || "")} ${a.login || ""}`, `${esc(a.server || "")} · ${money(a.balance, a.currency)}${a.type === "RÉEL" ? " — conseillé : commence sur un compte démo" : ""}`)}
          ${wzCheck(d.algo_trading ? "ok" : "warn", d.algo_trading ? "Algo Trading activé" : "Algo Trading désactivé", d.algo_trading ? "Le robot pourra passer des ordres." : "Seulement nécessaire pour le robot : bouton « Algo Trading » dans MT5 (Ctrl+E).")}`;
      } else {
        const found = (d.terminals || []).length;
        html = `<h2 id="wz-title">MetaTrader 5 n'est pas connecté</h2>
          ${wzCheck("err", "Connexion impossible", esc(d.error || ""))}
          ${found ? wzCheck("warn", `${found} terminal(aux) installé(s) trouvé(s)`, "Ouvre-le, connecte-toi à ton compte puis clique sur « Réessayer ».") :
                    wzCheck("warn", "Aucun MetaTrader 5 détecté", "Installe MetaTrader 5 depuis le site de ton courtier, ou continue sans (prix Yahoo Finance ou Démo).")}
          ${found > 1 ? `<div class="field" style="margin:8px 0 12px"><label>Terminal à utiliser</label><select id="wz-path"><option value="">Automatique</option>${d.terminals.map((t) => `<option ${t === p.mt5_path ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>` : ""}
          <button class="btn sm ghost" id="wz-retry">Réessayer</button>`;
      }
    } else if (step === "market") {
      $("#wz-next").textContent = "Continuer";
      if (d && d.connected) {
        const syms = d.symbols && d.symbols.length ? d.symbols : [p.symbol];
        html = `<h2 id="wz-title">Symbole et heure du courtier</h2>
          <p class="lead">Valeurs détectées automatiquement — vérifie-les puis continue.</p>
          <div class="wz-grid">
            <div class="field"><label>Symbole de l'or chez ton courtier</label><select id="wz-symbol">${syms.map((n) => `<option ${n === p.symbol ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
            <div class="field"><label>Décalage heure serveur → UTC (h)</label><input id="wz-offset" type="number" min="-12" max="14" value="${p.broker_utc_offset}"></div>
          </div>
          ${d.offset != null ? wzCheck("ok", `Décalage détecté : ${d.offset > 0 ? "+" : ""}${d.offset} h`, "Calculé à partir de l'heure de la dernière cotation.") :
                               wzCheck("warn", "Décalage non détecté", esc(d.offset_reason) + " Tu pourras le corriger plus tard dans Réglages.")}`;
      } else {
        html = `<h2 id="wz-title">Source des prix</h2><p class="lead">Sans MetaTrader 5, choisis d'où viennent les prix. Tu pourras passer sur MT5 plus tard.</p>
          <label class="wz-choice"><input type="radio" name="wz-src" value="yfinance" ${p.fallback === "yfinance" ? "checked" : ""}><span><b>Yahoo Finance</b><span class="muted sm">Prix de l'or (contrat COMEX GC=F, à quelques dollars du XAUUSD), gratuits, léger retard possible. Analyse et alertes, robot en Simulateur uniquement.</span></span></label>
          <label class="wz-choice"><input type="radio" name="wz-src" value="demo" ${p.fallback === "demo" ? "checked" : ""}><span><b>Démo</b><span class="muted sm">Marché simulé pour découvrir l'interface. Jamais pour trader.</span></span></label>
          <label class="wz-choice"><input type="radio" name="wz-src" value="auto" ${p.fallback === "auto" ? "checked" : ""}><span><b>MetaTrader 5 (plus tard)</b><span class="muted sm">L'application se connectera automatiquement dès que MT5 sera ouvert.</span></span></label>`;
      }
    } else if (step === "alerts") {
      $("#wz-next").textContent = "Continuer";
      html = `<h2 id="wz-title">Notifications</h2><p class="lead">Comment veux-tu être prévenu d'un nouveau signal ?</p>
        <label class="wz-choice"><input type="checkbox" id="wz-sound" ${S.prefs.sound ? "checked" : ""}><span><b>Son</b><span class="muted sm">Deux notes montantes pour un achat, descendantes pour une vente.</span></span></label>
        <label class="wz-choice"><input type="checkbox" id="wz-browser" ${S.prefs.browser ? "checked" : ""}><span><b>Notification du navigateur</b><span class="muted sm">S'affiche même si l'onglet est en arrière-plan (le navigateur demandera l'autorisation).</span></span></label>
        <p class="muted sm">Les signaux s'affichent aussi en couleur dans la fenêtre noire, avec un bip.</p>`;
    } else {
      $("#wz-next").textContent = "Terminer";
      const src = d && d.connected ? "MetaTrader 5" : { yfinance: "Yahoo Finance", demo: "Démo (simulé)", auto: "MetaTrader 5 (dès qu'il sera ouvert)" }[p.fallback];
      html = `<h2 id="wz-title">C'est prêt</h2><p class="lead">Résumé de ta configuration :</p>
        ${wzCheck("ok", "Prix : " + esc(src), d && d.connected ? `Symbole ${esc(p.symbol)} · décalage ${p.broker_utc_offset} h` : "")}
        ${wzCheck("ok", "Alertes", [S.prefs.sound && "son", S.prefs.browser && "notification du navigateur", "terminal"].filter(Boolean).join(" · "))}
        ${wzCheck("warn", "Robot de trading désactivé", "Pour l'essayer sans risque : onglet Robot → Exécution « Simulateur » → Activer.")}
        <p class="muted sm">Lis le <a href="/guide" target="_blank" rel="noopener">guide d'utilisation</a> (chapitres 5, 6 et 10) avant de trader.</p>`;
    }
    $("#wz-body").innerHTML = html;
    const retry = $("#wz-retry");
    if (retry) retry.onclick = () => { const sel = $("#wz-path"); if (sel) W.pick.mt5_path = sel.value; applyPathThenDetect(); };
  }

  async function applyPathThenDetect() {
    if (W.pick.mt5_path) await api("/api/settings", { method: "PUT", body: JSON.stringify({ mt5_path: W.pick.mt5_path }) }).catch(() => {});
    detectMt5();
  }

  function readStep() {
    const step = STEPS[W.i], p = W.pick;
    if (step === "market") {
      if (W.det && W.det.connected) {
        p.symbol = $("#wz-symbol").value;
        p.broker_utc_offset = +$("#wz-offset").value || 0;
      } else {
        const r = $("input[name=wz-src]:checked");
        p.fallback = r ? r.value : "yfinance";
      }
    }
    if (step === "mt5") { const sel = $("#wz-path"); if (sel) p.mt5_path = sel.value; }
  }

  $("#wz-next").addEventListener("click", async () => {
    readStep();
    const step = STEPS[W.i];
    if (step === "alerts") {
      S.prefs.sound = $("#wz-sound").checked; store.set("sound", S.prefs.sound);
      await setBrowserAlerts($("#wz-browser").checked);
    }
    if (step === "done") {
      const p = W.pick, connected = W.det && W.det.connected;
      const body = connected ? { source: "auto", symbol: p.symbol, broker_utc_offset: p.broker_utc_offset, mt5_path: p.mt5_path }
                             : { source: p.fallback, mt5_path: p.mt5_path };
      $("#wz-next").disabled = true; $("#wz-next").textContent = "Enregistrement…";
      try {
        const r = await api("/api/setup/apply", { method: "POST", body: JSON.stringify(body) });
        closeWizard();
        toast("Configuration enregistrée", r.ok ? "L'analyse a démarré avec tes réglages." : "Attention : " + r.error, r.ok ? "" : "err", 7000);
        S.fitted = false; loadState();
        if (S.view === "reglages") { fillSettings(); loadDiag(); }
      } catch (e) { toast("Enregistrement impossible", e.message, "err"); $("#wz-next").disabled = false; $("#wz-next").textContent = "Terminer"; }
      return;
    }
    W.i++;
    if (STEPS[W.i] === "mt5" && !W.det) detectMt5(); else renderWizard();
  });
  $("#wz-back").addEventListener("click", () => { readStep(); W.i = Math.max(0, W.i - 1); renderWizard(); });
  $("#wz-skip").addEventListener("click", async () => { await api("/api/setup/skip", { method: "POST" }).catch(() => {}); closeWizard(); });

  api("/api/setup").then((st) => { if (!st.setup_done) openWizard(); }).catch(() => {});

  syncAlertBtn();
  loadState().then(() => api("/api/journal").then((d) => { S.journal = d.signals; }).catch(() => {}));
  connect();
})();
