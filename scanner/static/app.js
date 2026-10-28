"use strict";

// Futures inefficiency scanner: renders /api/overview, /api/markets/<code> and /api/alerts.
// All text from the server (headlines especially) goes in through textContent, never innerHTML.

const MR = "mean_reversion", CO = "continuation";
const LABEL = { [MR]: "Mean reversion", [CO]: "Continuation" };
const TAG = { [MR]: "MR", [CO]: "Breakout" };
const CLS = { [MR]: "mr", [CO]: "co" };
const REGIME = { "mean-reverting": "Mean-reverting", "trending": "Trending", "random walk": "Random walk" };
const REGIME_SHORT = { "mean-reverting": "Reverting", "trending": "Trending", "random walk": "Random" };
const RANGES = [["3M", 65], ["6M", 130], ["1Y", 260]];
const SVG_NS = "http://www.w3.org/2000/svg";
const MINUS = "−";

const state = {
  overview: null,
  selected: null,
  detail: null,
  range: 130,
  sector: "",
  signalsOnly: false,
  alerts: [],
  lastAlertId: 0,
  kind: "",
  primed: false,
};

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// ---- formatting ---------------------------------------------------------------

function decimals(x) {
  const a = Math.abs(x);
  return a < 10 ? 3 : a < 2000 ? 2 : 0;
}
function fmtPrice(x, d) {
  if (x == null) return "–";
  d = d ?? decimals(x);
  return x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }).replace("-", MINUS);
}
function fmtSigned(x, digits = 2) {
  if (x == null) return "–";
  const text = Math.abs(x).toFixed(digits);
  return (Number(text) === 0 ? "" : x > 0 ? "+" : MINUS) + text;
}
function fmtPct(x, digits = 1) { return x == null ? "–" : fmtSigned(x * 100, digits) + "%"; }
function fmtRate(x) { return x == null ? "–" : Math.round(x * 100) + "%"; }
function fmtDay(iso, withYear = false) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", {
    day: "numeric", month: "short", year: withYear ? "numeric" : undefined, timeZone: "UTC" });
}
function fmtTime(iso) {
  if (!iso) return "–";
  const t = new Date(iso), now = new Date();
  const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return t.toDateString() === now.toDateString() ? time
    : `${t.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${time}`;
}
function side(direction) { return direction > 0 ? "long" : "short"; }
function arrow(direction) { return direction > 0 ? "▲" : "▼"; }
function safeUrl(u) {
  try {
    const url = new URL(u);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch { return null; }
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

// ---- header and status ------------------------------------------------------------

function setStatus(stateName, text, title = "") {
  const node = $("#status");
  node.dataset.state = stateName;
  node.textContent = text;
  node.title = title;
}

function renderHeader() {
  const ov = state.overview;
  $("#demo-banner").hidden = !ov.demo;
  $("#source").textContent = ov.demo
    ? "Synthetic demo data · generated locally"
    : "Yahoo Finance daily bars (delayed, front month) · Google News and RSS headlines";
  const failed = ov.markets.filter(m => m.error);
  if (!ov.last_scan) setStatus("scanning", "First scan running…");
  else if (failed.length) setStatus("error", `${failed.length} market${failed.length > 1 ? "s" : ""} failed to load`,
    failed.map(m => `${m.code}: ${m.error}`).join("\n"));
  else setStatus(ov.status === "scanning" ? "scanning" : "ok",
    `Updated ${fmtTime(ov.last_scan)} · prices every ${ov.refresh_minutes} min, ` +
    (ov.news_refresh_minutes == null ? "news off" : `news every ${ov.news_refresh_minutes} min`));
  const s = ov.settings;
  $("#markets-hint").textContent = `Ranked by |z*| of the variance ratio VR(${s.vr_lag}): distance from a random walk.`;
  $("#table-note").textContent =
    `Regime: VR(${s.vr_lag}) on up to ${s.vr_window} daily returns; |z*| ≥ ${s.z_threshold} is significant at 5%. ` +
    `Stretch: z of the close vs its ${s.lookback}-day mean, ticks at ±${s.entry_z}. ` +
    `Channel: where the close sits in the prior ${s.channel}-day range. ` +
    `Record: share of past triggers that moved the right way ${s.horizon} days later. * bar still forming.`;
  const newsErrors = Object.entries(ov.news_errors || {});
  const box = $("#news-errors");
  box.hidden = !newsErrors.length;
  box.textContent = newsErrors.length ? `${newsErrors.length} news source${newsErrors.length > 1 ? "s" : ""} failed on the last poll.` : "";
  box.title = newsErrors.map(([k, v]) => `${k}: ${v}`).join("\n");
}

// ---- markets table ----------------------------------------------------------------

function rank(row) { return row.efficiency ? Math.abs(row.efficiency.z) : -1; }

function renderSectors() {
  const sectors = [...new Set(state.overview.markets.map(m => m.sector).filter(Boolean))];
  const box = $("#sectors");
  box.replaceChildren(...[["All", ""], ...sectors.map(s => [s, s])].map(([label, value]) =>
    el("button", { type: "button", class: "chip", "aria-pressed": String(state.sector === value),
      onclick: () => { state.sector = value; renderSectors(); renderTable(); } }, label)));
}

function gauge(kind, value, entry) {
  // kind "z": -3..+3 with ticks at ±entry. kind "channel": position in the channel, 0..1, with room for breakouts.
  const W = 84, H = 14, pad = 5;
  const [lo, hi] = kind === "z" ? [-3, 3] : [-0.2, 1.2];
  const x = v => pad + (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo) * (W - 2 * pad);
  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
  root.append(svg("line", { class: "g-track", x1: pad, x2: W - pad, y1: H / 2, y2: H / 2 }));
  const ticks = kind === "z" ? [-entry, entry] : [0, 1];
  for (const t of ticks) root.append(svg("line", { class: "g-tick-mark", x1: x(t), x2: x(t), y1: 2, y2: H - 2 }));
  if (kind === "z") root.append(svg("line", { class: "g-zero", x1: x(0), x2: x(0), y1: 4, y2: H - 4 }));
  if (value != null) {
    const hot = kind === "z" ? Math.abs(value) >= entry : value < 0 || value > 1;
    root.append(svg("circle", { class: `g-dot ${hot ? (kind === "z" ? "mr" : "co") : ""}`, cx: x(value), cy: H / 2, r: 4 }));
  }
  return root;
}

function triggerPills(row) {
  const pills = [];
  for (const strategy of [MR, CO]) {
    const direction = row[strategy].trigger;
    if (!direction) continue;
    pills.push(el("span", { class: `pill ${CLS[strategy]}`, title: `${LABEL[strategy]} ${side(direction)}` +
        (row.live ? " on the bar that is still forming" : "") },
      el("span", { class: "mark", "aria-hidden": "true" }, arrow(direction)),
      `${TAG[strategy]} ${side(direction)}${row.live ? "*" : ""}`));
  }
  return pills.length ? el("div", { class: "pills" }, ...pills) : el("span", { class: "muted" }, "–");
}

function recordLine(strategy, record) {
  return el("div", {}, `${TAG[strategy]} `, el("b", {}, fmtRate(record.hit_rate)), ` of ${record.n}`);
}

function renderTable() {
  const ov = state.overview;
  const tbody = $("#markets tbody");
  let rows = ov.markets.filter(r => !state.sector || r.sector === state.sector);
  if (state.signalsOnly) rows = rows.filter(r => r.last != null && (r[MR].trigger || r[CO].trigger));
  rows = [...rows].sort((a, b) => rank(b) - rank(a));
  const entry = ov.settings.entry_z;
  const out = rows.map(r => {
    const select = () => selectMarket(r.code);
    const tr = el("tr", { tabindex: "0", "aria-selected": String(state.selected === r.code), onclick: select,
      onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); } } });
    const market = el("td", {}, el("span", { class: "code" }, r.code), el("span", { class: "name" }, r.name));
    tr.append(market);
    if (r.last == null) {
      tr.append(el("td", { colspan: "7", class: r.error ? "error-cell" : "muted" },
        r.error ? `Failed to load: ${r.error}` : "Waiting for the first scan…"));
      return tr;
    }
    const dir = r.change > 0 ? "up" : r.change < 0 ? "down" : "";
    tr.append(el("td", { class: "num" }, fmtPrice(r.last),
      el("span", { class: `delta ${dir}` }, fmtPct(r.change_pct))));
    const eff = r.efficiency;
    tr.append(el("td", {}, eff
      ? el("span", { class: "regime" },
          el("span", { class: `dot ${eff.regime === "mean-reverting" ? "mr" : eff.regime === "trending" ? "co" : ""}`,
            "aria-hidden": "true" }),
          el("span", { class: "long" }, REGIME[eff.regime]), el("span", { class: "short" }, REGIME_SHORT[eff.regime]),
          el("span", { class: "z tnum" }, fmtSigned(eff.z)))
      : el("span", { class: "muted" }, "Too little history")));
    const mr = r[MR], co = r[CO];
    tr.append(el("td", { class: "col-stretch" }, el("span", { class: "gauge" }, gauge("z", mr.z, entry),
      el("span", { class: "v tnum" }, fmtSigned(mr.z)))));
    tr.append(el("td", { class: "col-channel" }, el("span", { class: "gauge" }, gauge("channel", co.position),
      el("span", { class: "v tnum" }, co.position == null ? "–" : Math.round(co.position * 100) + "%"))));
    // On phones the Today column is hidden and the pills sit under the market name instead.
    if (r[MR].trigger || r[CO].trigger) market.append(el("div", { class: "market-pills" }, triggerPills(r)));
    tr.append(el("td", { class: "col-today" }, triggerPills(r)));
    tr.append(el("td", { class: "col-record record tnum" }, recordLine(MR, mr.record), recordLine(CO, co.record)));
    tr.append(el("td", { class: "num col-news" }, String(r.news_24h ?? 0)));
    return tr;
  });
  if (!out.length) {
    out.push(el("tr", {}, el("td", { colspan: "8", class: "muted" },
      state.signalsOnly ? "No market has a signal on its latest bar." : "No markets.")));
  }
  tbody.replaceChildren(...out);
}

// ---- market detail ------------------------------------------------------------------

async function selectMarket(code) {
  state.selected = code;
  if (!state.overview) return;   // the first overview poll loads the detail
  renderTable();
  await loadDetail(code);
}

async function loadDetail(code) {
  try {
    const detail = await api(`/api/markets/${encodeURIComponent(code)}`);
    if (state.selected !== code) return;
    state.detail = detail;
    renderDetail();
  } catch (e) {
    $("#detail").replaceChildren(el("p", { class: "muted" }, `Could not load ${code}: ${e.message}`));
  }
}

function factList(pairs) {
  const dl = el("dl");
  for (const [k, v] of pairs) dl.append(el("dt", {}, k), el("dd", {}, v));
  return dl;
}

function agreement(value) {
  const text = { agrees: "Regime agrees", against: "Regime against", neutral: "Regime neutral" }[value];
  return el("p", { class: `agree ${value}` }, text);
}

function lastTrigger(t) {
  return t ? `${side(t.direction)} · ${fmtDay(t.day)} at ${fmtPrice(t.price)}` : "none in history";
}

function recordFacts(record) {
  const range = record.n ? `${fmtRate(record.lo)}–${fmtRate(record.hi)}` : "–";
  return [
    [`Record, ${record.horizon} days`, record.n ? `${fmtRate(record.hit_rate)} of ${record.n}` : "no triggers"],
    ["95% range", range],
    ["Mean move", record.mean_atr == null ? "–" : `${fmtSigned(record.mean_atr)} ATR`],
    ["p vs coin flips", record.p_coin == null ? "–" : record.p_coin.toFixed(2)],
  ];
}

function renderDetail() {
  const d = state.detail, box = $("#detail");
  if (!d.summary) {
    box.replaceChildren(el("h2", {}, `${d.code} · ${d.name}`),
      el("p", { class: d.error ? "error-cell" : "muted" }, d.error ? `Failed to load: ${d.error}` : "Waiting for the first scan…"));
    return;
  }
  const s = d.summary, settings = state.overview.settings;
  const dir = s.change > 0 ? "up" : s.change < 0 ? "down" : "";
  const head = el("div", { class: "detail-head" },
    el("div", {}, el("h2", {}, `${s.code} · ${s.name}`),
      el("p", { class: "muted small" }, [s.sector, s.symbol, s.currency].filter(Boolean).join(" · "))),
    el("div", {}, el("span", { class: "price tnum" }, fmtPrice(s.last)),
      el("span", { class: `delta ${dir}` }, `${fmtSigned(s.change, decimals(s.last))} (${fmtPct(s.change_pct)})`),
      el("p", { class: "live-tag" }, s.live ? `${fmtDay(s.day)} bar still forming` : `Settled ${fmtDay(s.day)}`)));

  const seg = el("div", { class: "seg", role: "group", "aria-label": "Chart range" },
    ...RANGES.map(([label, bars]) => el("button", { type: "button", "aria-pressed": String(state.range === bars),
      onclick: () => { state.range = bars; renderDetail(); } }, label)));
  const legend = el("div", { class: "legend", "aria-label": "Legend" },
    el("span", {}, el("i", { class: "key-line close" }), "Close"),
    el("span", {}, el("i", { class: "key-band mr" }), `${settings.lookback}-day mean ±${settings.entry_z}σ`),
    el("span", {}, el("i", { class: "key-line co" }), `${settings.channel}-day channel`),
    el("span", {}, el("b", { class: "key-mark mr" }, "▲▼"), "mean-reversion trigger"),
    el("span", {}, el("b", { class: "key-mark co" }, "▲▼"), "breakout trigger"));
  const wrap = el("div", { class: "chart-wrap" });

  const eff = s.efficiency, mr = s[MR], co = s[CO];
  const regimeNote = !eff ? "Not enough history for the test."
    : eff.regime === "mean-reverting" ? `VR below 1: moves tend to be partly undone. Favours mean reversion.`
    : eff.regime === "trending" ? `VR above 1: moves tend to continue. Favours continuation.`
    : `Not distinguishable from a random walk at 5%. Neither strategy has the regime on its side.`;
  const facts = el("div", { class: "facts" },
    el("div", { class: "fact" },
      el("h3", {}, el("span", { class: `dot ${eff && eff.regime === "mean-reverting" ? "mr" : eff && eff.regime === "trending" ? "co" : ""}` }), "Regime"),
      el("p", { class: "big" }, eff ? REGIME[eff.regime] : "–"),
      eff ? factList([[`VR(${eff.q})`, eff.vr.toFixed(2)], ["z*", fmtSigned(eff.z)],
        ["ρ(1)", fmtSigned(eff.rho1)], ["Daily returns", String(eff.n)]]) : null,
      el("p", { class: "note" }, regimeNote)),
    el("div", { class: "fact" },
      el("h3", {}, el("span", { class: "dot mr" }), LABEL[MR]),
      el("p", { class: "big tnum" }, `z ${fmtSigned(mr.z)}`),
      factList([
        ["Now", mr.trigger ? `${side(mr.trigger)} trigger` : mr.zone ? `${side(mr.zone)} zone` : `inside ±${mr.entry_z}`],
        ["Last trigger", lastTrigger(mr.last)], ...recordFacts(mr.record)]),
      agreement(mr.agreement)),
    el("div", { class: "fact" },
      el("h3", {}, el("span", { class: "dot co" }), LABEL[CO]),
      el("p", { class: "big tnum" }, co.position == null ? "–" : `${Math.round(co.position * 100)}% of range`),
      factList([
        ["Channel", `${fmtPrice(co.lower)} – ${fmtPrice(co.upper)}`],
        ["Now", co.trigger ? `${side(co.trigger)} breakout` : "inside the channel"],
        ["Last trigger", lastTrigger(co.last)], ...recordFacts(co.record)]),
      agreement(co.agreement)));

  const news = el("ol", { class: "news-list" });
  if (!d.news.length) news.append(el("li", { class: "muted" }, "No matching headlines stored yet."));
  for (const n of d.news) {
    const url = safeUrl(n.link);
    const title = url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, n.title) : el("span", {}, n.title);
    const meta = [n.source, n.published ? fmtTime(n.published) : null, n.keywords.join(", "),
      n.impact.length ? `impact ${n.impact.join(", ")}` : null, n.tone ? `tone ${fmtSigned(n.tone, 0)}` : null]
      .filter(Boolean).join(" · ");
    news.append(el("li", {}, title, el("p", { class: "news-meta" },
      el("span", { class: "score", title: "Headline score" }, `${n.score}`), ` · ${meta}`)));
  }

  box.replaceChildren(head, el("div", { class: "chart-controls" }, seg, legend), wrap, facts,
    dataTable(d.chart), el("h3", { class: "sub" }, "Related news"), news);
  drawChart(wrap, d.chart, state.range);
}

function dataTable(c) {
  const n = c.close.length, from = Math.max(0, n - 20);
  const signals = new Map();
  for (const t of c.triggers) signals.set(t.index, [...(signals.get(t.index) || []), `${TAG[t.strategy]} ${side(t.direction)}`]);
  const rows = [];
  for (let i = n - 1; i >= from; i--) {
    rows.push(el("tr", {}, ...[fmtDay(c.days[i]), fmtPrice(c.open[i]), fmtPrice(c.high[i]), fmtPrice(c.low[i]),
      fmtPrice(c.close[i]), fmtSigned(c.z[i]), fmtPrice(c.mean[i]), fmtPrice(c.channel_upper[i]),
      fmtPrice(c.channel_lower[i]), (signals.get(i) || []).join(", ")].map((v, k) =>
      el("td", { class: k > 0 && k < 9 ? "num" : "" }, v))));
  }
  const head = el("tr", {}, ...["Date", "Open", "High", "Low", "Close", "z", "Mean", "Chan. high", "Chan. low", "Signal"]
    .map((h, k) => el("th", { scope: "col", class: k > 0 && k < 9 ? "num" : "" }, h)));
  return el("details", {}, el("summary", {}, "Data table, last 20 bars"),
    el("div", { class: "table-wrap" }, el("table", { class: "data-table" }, el("thead", {}, head), el("tbody", {}, ...rows))));
}

// ---- chart --------------------------------------------------------------------------

function niceTicks(lo, hi, count) {
  const raw = (hi - lo) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
  const ticks = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) ticks.push(Number(t.toPrecision(12)));
  return { ticks, step };
}

function pathOf(indices, values, x, y) {
  let d = "", pen = false;
  for (const i of indices) {
    const v = values[i];
    if (v == null) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  }
  return d;
}

function stepPathOf(indices, values, x, y) {
  let d = "", pen = false;
  for (const i of indices) {
    const v = values[i];
    if (v == null) { pen = false; continue; }
    d += pen ? `H${x(i).toFixed(1)}V${y(v).toFixed(1)}` : `M${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  }
  return d;
}

function bandPathOf(indices, upper, lower, x, y) {
  let d = "", run = [];
  const flush = () => {
    if (run.length > 1) {
      d += run.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(upper[i]).toFixed(1)}`).join("");
      d += run.slice().reverse().map(i => `L${x(i).toFixed(1)},${y(lower[i]).toFixed(1)}`).join("") + "Z";
    }
    run = [];
  };
  for (const i of indices) {
    if (upper[i] == null || lower[i] == null) flush(); else run.push(i);
  }
  flush();
  return d;
}

function drawChart(wrap, c, range) {
  const n = c.close.length, start = Math.max(0, n - range);
  const indices = Array.from({ length: n - start }, (_, k) => start + k);
  drawnWidth = wrap.clientWidth;
  const W = Math.max(280, Math.round(wrap.clientWidth || 640));
  const H = W < 560 ? 240 : 300;
  const m = { l: 4, r: 56, t: 12, b: 24 };
  const triggers = c.triggers.filter(t => t.index >= start);

  const values = [];
  for (const i of indices) for (const key of ["close", "band_upper", "band_lower", "channel_upper", "channel_lower"]) {
    if (c[key][i] != null) values.push(c[key][i]);
  }
  let lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.01 || 1;
  lo -= pad; hi += pad;
  const plotW = W - m.l - m.r, plotH = H - m.t - m.b;
  const x = i => m.l + (n - 1 === start ? plotW / 2 : (i - start) / (n - 1 - start) * plotW);
  const y = v => m.t + (hi - v) / (hi - lo) * plotH;

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, tabindex: "0", role: "img",
    "aria-label": `${state.detail.code} daily closes for the last ${indices.length} bars with the mean band, ` +
      `the channel and ${triggers.length} past triggers. Use the arrow keys to read values.` });

  const { ticks, step } = niceTicks(lo, hi, 5);
  let tickDigits = 0;   // decimals that show every tick exactly: 2.5 needs one, 5 needs none
  while (tickDigits < 4 && Math.abs(step * 10 ** tickDigits - Math.round(step * 10 ** tickDigits)) > 1e-6) tickDigits++;
  for (const t of ticks) {
    root.append(svg("line", { class: "g-grid", x1: m.l, x2: m.l + plotW, y1: y(t), y2: y(t) }));
    const label = svg("text", { class: "g-label", x: m.l + plotW + 8, y: y(t) + 4 });
    label.textContent = fmtPrice(t, tickDigits);
    root.append(label);
  }
  let lastMonth = c.days[start].slice(0, 7), lastX = -Infinity;
  for (const i of indices) {
    const month = c.days[i].slice(0, 7);
    if (month === lastMonth) continue;
    lastMonth = month;
    if (x(i) - lastX < 44 || x(i) > m.l + plotW - 20) continue;
    lastX = x(i);
    const [yr, mo] = month.split("-").map(Number);
    const label = svg("text", { class: "g-label", x: x(i), y: H - 6, "text-anchor": "middle" });
    label.textContent = new Date(Date.UTC(yr, mo - 1, 1)).toLocaleDateString("en-GB",
      { month: "short", year: mo === 1 ? "2-digit" : undefined, timeZone: "UTC" });
    root.append(svg("line", { class: "g-xtick", x1: x(i), x2: x(i), y1: m.t + plotH, y2: m.t + plotH + 4 }), label);
  }

  root.append(svg("path", { class: "g-band", d: bandPathOf(indices, c.band_upper, c.band_lower, x, y) }));
  root.append(svg("path", { class: "g-line mr", d: pathOf(indices, c.mean, x, y) }));
  root.append(svg("path", { class: "g-line co", d: stepPathOf(indices, c.channel_upper, x, y) }));
  root.append(svg("path", { class: "g-line co", d: stepPathOf(indices, c.channel_lower, x, y) }));
  root.append(svg("path", { class: "g-line close", d: pathOf(indices, c.close, x, y) }));

  for (const t of triggers) {
    const cx = x(t.index), cy = y(t.price) + (t.direction > 0 ? 11 : -11);
    const points = t.direction > 0
      ? `${cx},${cy - 5} ${cx + 5},${cy + 4} ${cx - 5},${cy + 4}`
      : `${cx},${cy + 5} ${cx + 5},${cy - 4} ${cx - 5},${cy - 4}`;
    root.append(svg("polygon", { class: `g-trigger ${CLS[t.strategy]}`, points }));
  }

  const cross = svg("line", { class: "g-cross", y1: m.t, y2: m.t + plotH, visibility: "hidden" });
  const crossDot = svg("circle", { class: "g-cross-dot", r: 4, visibility: "hidden" });
  const overlay = svg("rect", { x: m.l, y: 0, width: plotW, height: H, fill: "transparent" });
  root.append(cross, crossDot, overlay);
  const tip = el("div", { class: "tooltip", hidden: true });
  wrap.replaceChildren(root, tip);

  const byIndex = new Map();
  for (const t of triggers) byIndex.set(t.index, [...(byIndex.get(t.index) || []), t]);
  let current = null;

  function row(label, value, keyClass) {
    return el("div", { class: "row" },
      el("span", { class: "lab" }, keyClass ? el("i", { class: keyClass }) : null, label),
      el("span", { class: "val" }, value));
  }

  function show(i) {
    current = Math.max(start, Math.min(n - 1, i));
    const cx = x(current);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
    crossDot.setAttribute("cx", cx); crossDot.setAttribute("cy", y(c.close[current]));
    cross.setAttribute("visibility", "visible"); crossDot.setAttribute("visibility", "visible");
    const rows = [
      el("div", { class: "t-date" }, fmtDay(c.days[current], true)),
      row("Close", fmtPrice(c.close[current]), "key-line close"),
      row("Mean", fmtPrice(c.mean[current]), "key-line mr"),
      row("Band", c.band_upper[current] == null ? "–"
        : `${fmtPrice(c.band_lower[current])} – ${fmtPrice(c.band_upper[current])}`),
      row("z", fmtSigned(c.z[current])),
      row("Channel", c.channel_upper[current] == null ? "–"
        : `${fmtPrice(c.channel_lower[current])} – ${fmtPrice(c.channel_upper[current])}`, "key-line co"),
    ];
    for (const t of byIndex.get(current) || []) {
      rows.push(el("div", { class: "row trig" }, el("span", { class: "lab" },
        el("b", { class: `key-mark ${CLS[t.strategy]}` }, arrow(t.direction)), t.label),
        el("span", { class: "val" }, side(t.direction))));
    }
    tip.replaceChildren(...rows);
    tip.hidden = false;
    const scale = root.getBoundingClientRect().width / W || 1;
    const px = cx * scale, tipW = tip.offsetWidth;
    tip.style.left = `${px + 14 + tipW > wrap.clientWidth ? Math.max(0, px - 14 - tipW) : px + 14}px`;
    tip.style.top = `${m.t * scale}px`;
  }
  function hide() {
    current = null;
    tip.hidden = true;
    cross.setAttribute("visibility", "hidden");
    crossDot.setAttribute("visibility", "hidden");
  }
  overlay.addEventListener("pointermove", e => {
    const rect = root.getBoundingClientRect();
    const px = (e.clientX - rect.left) * (W / rect.width);
    show(Math.round(start + (px - m.l) / plotW * (n - 1 - start)));
  });
  overlay.addEventListener("pointerleave", hide);
  root.addEventListener("focus", () => show(n - 1));
  root.addEventListener("blur", hide);
  root.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      show((current ?? n - 1) + (e.key === "ArrowLeft" ? -1 : 1));
    } else if (e.key === "Escape") hide();
  });
}

// Redraw the chart when its width changes (not its height: that would drop the hover readout).
let resizeTimer = null, drawnWidth = 0;
new ResizeObserver(() => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const wrap = $("#detail .chart-wrap");
    if (!wrap || !state.detail || !state.detail.chart || wrap.clientWidth === drawnWidth) return;
    drawChart(wrap, state.detail.chart, state.range);
  }, 120);
}).observe($("#detail"));

// ---- alerts ---------------------------------------------------------------------------

function alertItem(a, fresh) {
  const kindLabel = a.kind === "signal"
    ? el("span", { class: `pill ${CLS[a.strategy] || ""}` },
        el("span", { class: "mark", "aria-hidden": "true" }, arrow(a.direction)), `${LABEL[a.strategy] || "Signal"}`)
    : el("span", { class: "tag" }, "News");
  const codes = el("span", { class: "codes" }, ...a.markets.map(code =>
    el("button", { type: "button", title: `Show ${code}`, onclick: () => selectMarket(code) }, code)));
  const url = safeUrl(a.url);
  return el("li", { class: fresh ? "fresh" : "" },
    el("div", { class: "alert-meta" }, kindLabel, codes, el("time", { datetime: a.created_at }, fmtTime(a.created_at)),
      a.provisional ? el("span", { class: "tag", title: "Fired on a bar that was still forming" }, "provisional") : null),
    el("p", { class: "alert-title" }, a.title),
    el("p", { class: "alert-body" }, a.body),
    url ? el("a", { class: "small", href: url, target: "_blank", rel: "noopener noreferrer" }, "Open article ↗") : null);
}

function renderAlerts(freshIds = new Set()) {
  const items = state.alerts.filter(a => !state.kind || a.kind === state.kind).slice(0, 200);
  const list = $("#alerts");
  if (!items.length) {
    const min = state.overview ? state.overview.settings.min_news_score : 4;
    list.replaceChildren(el("li", { class: "empty" },
      `No alerts yet. They appear when a signal fires on a market's latest bar or a headline scores ${min} or more.`));
    return;
  }
  list.replaceChildren(...items.map(a => alertItem(a, freshIds.has(a.id))));
}

function toast(text) {
  const node = $("#toast");
  node.textContent = text;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 6000);
}

function notify(fresh) {
  toast(fresh.length === 1 ? fresh[0].title : `${fresh.length} new alerts · latest: ${fresh[0].title}`);
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  for (const a of fresh.slice(0, 3)) {
    const note = new Notification(a.title, { body: a.body, tag: `scanner-${a.id}` });
    note.onclick = () => { window.focus(); selectMarket(a.markets[0]); };
  }
}

async function pollAlerts() {
  try {
    const { alerts } = await api(`/api/alerts?after=${state.lastAlertId}&limit=200`);
    if (alerts.length) {
      state.lastAlertId = Math.max(state.lastAlertId, ...alerts.map(a => a.id));
      state.alerts = alerts.concat(state.alerts).slice(0, 500);
      renderAlerts(state.primed ? new Set(alerts.map(a => a.id)) : undefined);
      if (state.primed) notify(alerts);
    } else if (!state.primed) {
      renderAlerts();
    }
    state.primed = true;
  } catch { /* the overview poll reports connection problems */ }
}

// ---- overview loop --------------------------------------------------------------------

async function pollOverview() {
  try {
    const ov = await api("/api/overview");
    const changed = !state.overview || ov.last_scan !== state.overview.last_scan || ov.last_news !== state.overview.last_news;
    state.overview = ov;
    renderHeader();
    renderSectors();
    renderTable();
    if (!state.selected) {
      const first = [...ov.markets].filter(r => r.last != null).sort((a, b) => rank(b) - rank(a))[0];
      if (first) selectMarket(first.code);
    } else if (changed) {
      loadDetail(state.selected);
    }
  } catch (e) {
    setStatus("error", "Cannot reach the scanner server", e.message);
  }
}

function setupControls() {
  $("#signals-only").addEventListener("change", e => { state.signalsOnly = e.target.checked; renderTable(); });
  for (const tab of document.querySelectorAll(".tabs [role=tab]")) {
    tab.addEventListener("click", () => {
      state.kind = tab.dataset.kind;
      for (const t of document.querySelectorAll(".tabs [role=tab]")) t.setAttribute("aria-selected", String(t === tab));
      renderAlerts();
    });
  }
  const refresh = $("#refresh");
  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    setStatus("scanning", "Scanning…");
    try {
      await api("/api/refresh", { method: "POST", headers: { "X-Scanner": "1" } });
    } catch (e) {
      setStatus("error", "Refresh failed", e.message);
    }
    for (const wait of [3000, 10000, 25000]) setTimeout(() => { pollOverview(); pollAlerts(); }, wait);
    setTimeout(() => { refresh.disabled = false; }, 5000);
  });
  const button = $("#notify");
  if ("Notification" in window) {
    const sync = () => {
      button.hidden = false;
      button.disabled = Notification.permission !== "default";
      button.textContent = Notification.permission === "granted" ? "Notifications on"
        : Notification.permission === "denied" ? "Notifications blocked" : "Enable notifications";
    };
    sync();
    button.addEventListener("click", async () => { await Notification.requestPermission(); sync(); });
  }
}

setupControls();
pollOverview();
pollAlerts();
setInterval(pollOverview, 30000);
setInterval(pollAlerts, 15000);
