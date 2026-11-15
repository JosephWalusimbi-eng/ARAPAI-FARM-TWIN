// ARAPAI FARM TWIN app: offline-first.
//
// The farm pack (/api/pack) is downloaded when there is internet and kept on the device.
// Everything after that (climate view, rotation ranking, languages, farm settings,
// saved plans) runs in the browser with no network. Saved plans queue in an outbox and
// are sent to the server whenever a connection appears.
import * as E from "./engine.js";

const LS = { lang: "farmtwin.lang", pack: "farmtwin.pack", settings: "farmtwin.settings",
             weights: "farmtwin.weights", plans: "farmtwin.plans", outbox: "farmtwin.outbox",
             compare: "farmtwin.compare" };
const COMPARE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"];  // categorical slots 1-3 (validated)
const WEIGHTS = [["climate_suitability", "C"], ["water_resilience", "W"], ["soil_health", "H"],
                 ["rotation_compatibility", "K"], ["farmer_utility", "F"]];
const COLORS = { power: "#2a78d6", imerg: "#eb6834", grid: "#e1e0d9", axis: "#c3c2b7",
                 muted: "#898781", ink: "#0b0b0b", ink2: "#52514e" };
const CONTROL = "Maize → Maize → Maize";

let MORE = false;
let PACK = null, LANG = "en", ONLINE = false, MAP = null, SHOW_TABLE = false, RETRY = null;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = v => (v === null || v === undefined) ? "—" : v;
const f2 = v => (v == null ? "—" : v.toFixed(2));
const t = (key, params) => (PACK ? E.t(PACK, LANG, key, params) : key);

function load(key, dflt) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : dflt; } catch { return dflt; }
}
function store(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; }
  catch (e) { console.warn("ARAPAI FARM TWIN: could not save on this device", e); return false; }
}
async function fetchWithTimeout(url, ms, opts = {}) {
  const ctl = new AbortController();
  const id = setTimeout(() => ctl.abort(), ms);
  try { return await fetch(url, { ...opts, signal: ctl.signal }); } finally { clearTimeout(id); }
}
function shortDate(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString(LANG === "en" ? "en-GB" : LANG, { day: "numeric", month: "short", year: "numeric" });
}

// --- data: the farm pack -----------------------------------------------------

async function refreshPack(manual = false) {
  try {
    const res = await fetchWithTimeout("/api/pack", PACK ? 20000 : 60000, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const pack = await res.json();
    pack.downloaded_at = new Date().toISOString();
    PACK = pack;
    store(LS.pack, pack);
    setOnline(true);
    renderAll();
    if (manual) toast(t("data.updated"));
  } catch (e) {
    setOnline(false);
    if (!PACK) $("status").innerHTML = `<span class="err">${esc(t("data.no_pack"))}</span>`;
  }
}

function setOnline(on) {
  ONLINE = on && navigator.onLine !== false;
  const pill = $("net");
  pill.className = `pill ${ONLINE ? "on" : "off"}`;
  pill.textContent = PACK ? (ONLINE ? t("net.online") : t("net.offline")) : (ONLINE ? "Online" : "Offline");
}

function toast(text) {
  const el = $("toast");
  el.textContent = text; el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 2500);
}

// --- farm settings, weights (kept on the device) -------------------------------

function settings() { return load(LS.settings, null); }

function effectiveFarm() {
  const farm = JSON.parse(JSON.stringify(PACK.farm));
  const s = settings();
  if (s) {
    const hist = farm.farming_history.previous_crops.slice();
    if (s.previous_crop) { if (hist.length) hist[hist.length - 1] = s.previous_crop; else hist.push(s.previous_crop); }
    farm.farming_history.previous_crops = hist;
    if (s.farm_type) farm.farm.farm_type = s.farm_type;
  }
  return farm;
}

function currentWeights() {
  const saved = load(LS.weights, null);
  const out = {};
  for (const [k] of WEIGHTS) out[k] = Number((saved && saved[k]) ?? PACK.farm.farmer_preferences[k]);
  return out;
}

// --- rendering -----------------------------------------------------------------

function renderAll() {
  if (!PACK) return;
  if (!PACK.i18n[LANG]) LANG = "en";
  document.documentElement.lang = LANG;
  document.querySelectorAll("[data-t]").forEach(el => { el.textContent = t(el.dataset.t); });
  renderLanguages(); renderBanner(); setOnline(ONLINE);
  renderFarm(); renderSettings(); renderWeights(); renderRotations(); renderPlans();
  renderSms();
  renderClimate(); renderEvidence(); renderMap();
}

function renderLanguages() {
  // Just the language names. Whether a language is a draft (or not translated yet) is explained in the notice under the
  // header once that language is chosen, not in the list.
  const sel = $("lang");
  sel.innerHTML = PACK.languages.map(l => `<option value="${l.code}">${esc(l.native_name)}</option>`).join("");
  sel.value = LANG;
}

function renderBanner() {
  const meta = PACK.i18n[LANG].meta, lines = [];
  if (meta.status === "draft") lines.push(t("lang.draft"));
  // A reviewed language shows no notice at all: only drafts and untranslated languages explain themselves.
  if (meta.status === "needs_translation") lines.push(`${meta.native_name}: ${meta.note}`);
  $("draft").style.display = lines.length ? "block" : "none";
  $("draft").textContent = lines.join(" ");
  const ageDays = (Date.now() - new Date(PACK.generated_at)) / 864e5;
  $("fresh").innerHTML = esc(t("data.freshness", { through: PACK.data_through, downloaded: shortDate(PACK.downloaded_at || PACK.generated_at) })) +
    (ageDays > PACK.stale_after_days ? ` · <span class="err">${esc(t("data.stale", { days: PACK.stale_after_days }))}</span>` : "");
}

function renderFarm() {
  const f = effectiveFarm(), { latitude: lat, longitude: lon } = f.location;
  $("farm-name").textContent = f.name;
  const src = f.soil_profile.data_source;
  const soil = src === "pending" ? t("farm.soil.pending") : src === "SoilGrids" ? t("farm.soil.soilgrids") : src;
  $("farm-table").innerHTML = [
    [t("farm.location"), `${f.location.district}, ${f.location.region}, ${f.location.country}`],
    [t("farm.coordinates"), `${lat}, ${lon}`],
    [t("farm.area"), `${f.farm.area_hectares} ha`],
    [t("farm.type"), t(`farm.type.${f.farm.farm_type}`)],
    [t("farm.irrigation"), f.farm.irrigation ? t("farm.irrigation.yes") : t("farm.irrigation.no")],
    [t("farm.previous_crops"), f.farming_history.previous_crops.map(c => t(`crop.${c}`)).join(", ")],
    [t("farm.soil_data"), soil],
  ].map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(fmt(v))}</td></tr>`).join("");
  renderSoil(f);
}

// SoilGrids estimates with their likely range: modelled, not measured.
function renderSoil(f) {
  const d = (f.soil_profile || {}).details;
  if (!d) { $("soil").innerHTML = `<p class="note">${esc(t("soil.none"))}</p>`; return; }
  const p = d.properties, row = (key, field, digits = 1) => {
    const v = p[field];
    if (!v || v.value == null) return "";
    const unit = v.unit === "pH" ? "" : ` ${v.unit}`;
    return `<tr><th>${esc(t(key))}</th><td>${v.value.toFixed(digits)}${esc(unit)}
      <span class="note">(${esc(t("soil.range", { low: v.low.toFixed(digits), high: v.high.toFixed(digits) }))})</span></td></tr>`;
  };
  $("soil").innerHTML = `<h3>${esc(t("soil.heading"))}</h3><table>
    <tr><th>${esc(t("soil.texture"))}</th><td>${esc(d.texture_class || "—")}</td></tr>
    ${row("soil.ph", "ph")}${row("soil.organic_carbon", "organic_carbon")}${row("soil.nitrogen", "nitrogen", 2)}
    ${row("soil.clay", "clay", 0)}${row("soil.cec", "cec")}</table>
    <p class="note">${esc(t("soil.note"))}</p>`;
}

function renderSettings() {
  const s = settings() || {}, f = effectiveFarm();
  const prev = f.farming_history.previous_crops.at(-1);
  const crops = Object.keys(PACK.engine.crops);
  const opt = (v, label, sel) => `<option value="${v}"${v === sel ? " selected" : ""}>${esc(label)}</option>`;
  $("settings").innerHTML = `
    <label>${esc(t("settings.previous_crop"))}<select id="s-prev">${crops.map(c => opt(c, t(`crop.${c}`), prev)).join("")}</select></label>
    <label>${esc(t("farm.type"))}<select id="s-type">${["smallholder", "commercial"].map(v => opt(v, t(`farm.type.${v}`), f.farm.farm_type)).join("")}</select></label>
    <label>${esc(t("settings.residues"))}<select id="s-res">
      ${opt("kept", t("settings.residues.kept"), s.residues_removed ? "removed" : "kept")}
      ${opt("removed", t("settings.residues.removed"), s.residues_removed ? "removed" : "kept")}</select></label>
    <p class="note">${esc(t("settings.note"))}${s.changed_at ? ` ${esc(t("settings.changed"))}: ${esc(shortDate(s.changed_at))}.
      <button id="s-reset" class="link">${esc(t("settings.reset"))}</button>` : ""}</p>`;
  const save = () => {
    store(LS.settings, { previous_crop: $("s-prev").value, farm_type: $("s-type").value,
                         residues_removed: $("s-res").value === "removed", changed_at: new Date().toISOString() });
    renderAll();
  };
  ["s-prev", "s-type", "s-res"].forEach(id => $(id).addEventListener("change", save));
  if ($("s-reset")) $("s-reset").addEventListener("click", () => { localStorage.removeItem(LS.settings); renderAll(); });
}

function renderWeights() {
  const w = currentWeights();
  $("weights").innerHTML = WEIGHTS.map(([k, term]) => `<label>${esc(t(`prio.${term}`))}<br>
    <input type="range" min="0" max="1" step="0.05" value="${w[k]}" id="w-${k}">
    <span id="wv-${k}">${w[k]}</span></label>`).join("");
  WEIGHTS.forEach(([k]) => $(`w-${k}`).addEventListener("input", e => {
    $(`wv-${k}`).textContent = e.target.value;
    const cur = currentWeights(); cur[k] = Number(e.target.value);
    store(LS.weights, cur);
    renderRotations();  // local engine: instant, no network
  }));
}

// --- crop pictures --------------------------------------------------------------------------
const CROP_LATIN = { maize: "Zea mays", sorghum: "Sorghum bicolor", finger_millet: "Eleusine coracana", beans: "Phaseolus vulgaris",
                     groundnut: "Arachis hypogaea", cowpea: "Vigna unguiculata", sunflower: "Helianthus annuus", cassava: "Manihot esculenta" };
function cropImg(key, size = "") {
  return `<img class="cropimg ${size}" src="/static/crops/${esc(key)}.jpg" alt="" loading="lazy" decoding="async" onerror="this.remove()">`;
}
// one entry per crop block: a crop that fills two seasons (cassava) is shown once, with "(2 seasons)"
function cropBlocks(rotation) {
  const crops = PACK.engine.crops, out = [];
  let i = 0;
  while (i < rotation.length) {
    const key = rotation[i], n = crops[key] ? crops[key].slots : 1, name = t(`crop.${key}`);
    out.push({ key, label: n > 1 ? t("rot.multi_slot", { crop: name, n }) : name });
    i += n;
  }
  return out;
}
function cropTiles(rotation, size = "sm") {
  return `<span class="rseq">${cropBlocks(rotation).map(b => cropImg(b.key, size)).join("")}</span>`;
}
function cropStrip(rotation) {
  return `<div class="cstrip">${cropBlocks(rotation).map(b => `<div class="citem">${cropImg(b.key)}<span>${esc(b.label)}</span></div>`)
    .join(`<span class="carr" aria-hidden="true">&rarr;</span>`)}</div>`;
}

let LAST = null;
function renderRotations() {
  let res;
  try {
    res = E.rankRotations(PACK, { lang: LANG, farm: effectiveFarm(), weights: currentWeights(),
                                  residuesRemoved: !!(settings() || {}).residues_removed });
  } catch (e) {
    $("rot-summary").innerHTML = `<span class="err">${esc(t("rot.failed", { detail: e.message }))}</span>`;
    $("rot-table").innerHTML = ""; return;
  }
  LAST = res;
  const m = res.meta, c = m.confidence, sc = m.scenarios;
  $("conf").innerHTML = `<span class="badge ${c.level}" title="q = ${c.q.toFixed(2)}">${esc(t("rot.confidence", { level: c.level_text, q: c.q.toFixed(2) }))}</span>`;
  const slot = s => t("season.slot", { season: t(`season.${s.season}.short`), year: s.year });
  $("rot-summary").textContent = t("rot.summary", {
    slots: m.slots.map(slot).join(", "), previous: m.previous_crop_name || "—", feasible: m.rotations_feasible,
    a_base: sc.baseline[0].P_mm, a_dry: sc.dry[0].P_mm, b_base: sc.baseline[1].P_mm, b_dry: sc.dry[1].P_mm });
  const shown = res.rows.slice(0, MORE ? 10 : 5);
  const controls = res.rows.filter(r => r.label === CONTROL && !shown.includes(r));
  const saved = new Set(load(LS.plans, []).map(p => p.rotation.join("|") + "|" + p.lang + "|" + JSON.stringify(p.weights)));
  const row = (r, cls = "") => {
    const s = r.scores.baseline;
    return `<tr class="${cls}"><td>${r.rank}</td><td>${cropTiles(r.rotation)}${esc(r.display)}</td><td class="num"><b>${f2(s.S)}</b></td>
      ${E.TERMS.map(k => `<td class="num">${f2(s[k])}</td>`).join("")}
      <td class="num">${f2(r.robustness.min_S)} <span class="note">${esc(t(`scenario.${r.robustness.scenario}`))}</span></td>
      <td class="num ${r.dry_flag ? "flag" : ""}" title="${r.dry_flag ? esc(t("rot.flag_tip")) : ""}">${r.ranks.dry}</td>
      <td class="note">${esc(r.largest_penalty || "")}</td>
      <td><button class="save" data-rot="${r.rotation.join("|")}">${esc(t("plans.save"))}</button></td></tr>`;
  };
  // the column headings of this table stay in English in every language (team decision)
  const en = key => E.t(PACK, "en", key);
  $("rot-table").innerHTML = `<tr><th>${esc(en("rot.col.rank"))}</th><th>${esc(en("rot.col.rotation"))}</th>
     <th>${esc(en("rot.col.score"))}</th>${E.TERMS.map(k => `<th>${esc(en(`term.${k}`))}</th>`).join("")}
     <th>${esc(en("rot.col.worst"))}</th><th>${esc(en("rot.col.dry_rank"))}</th><th>${esc(en("rot.col.concern"))}</th><th></th></tr>` +
    shown.map(r => row(r)).join("") + controls.map(r => row(r, "control")).join("");
  const card = (r, cls = "") => {
    const s = r.scores.baseline;
    return `<div class="rcard ${cls}"><div class="rtop"><span class="rrank">${r.rank}</span>${cropStrip(r.rotation)}</div>
      <div class="rscorebar"><span>${esc(t("rot.col.score"))}</span><b>${f2(s.S)}</b></div>
      <div class="bar"><i style="width:${Math.round(Math.max(0, Math.min(1, s.S)) * 100)}%"></i></div>
      <details class="inner"><summary>${esc(t("rot.detail"))}</summary>
        <div class="chips">${E.TERMS.map(k => `<span><small>${esc(t(`term.${k}`))}</small> ${f2(s[k])}</span>`).join("")}</div></details>
      <div class="rdry ${r.dry_flag ? "flag" : ""}">${esc(t("rot.col.dry_rank"))}: <b>${r.ranks.dry}</b></div>
      ${r.largest_penalty ? `<div class="rbox"><small>${esc(t("rot.col.concern"))}</small>${esc(r.largest_penalty)}</div>` : ""}
      <button class="save" data-rot="${r.rotation.join("|")}">${esc(t("plans.save"))}</button></div>`;
  };
  $("rot-cards").innerHTML = shown.map(r => card(r)).join("") + controls.map(r => card(r, "control")).join("");
  $("rot-more").textContent = MORE ? t("more.hide") : t("more.show");
  $("rot-more").style.display = res.rows.length > 5 ? "" : "none";
  renderBest(res);
  $("rot-note").textContent = t("rot.note") + " " + t("rot.raise", { steps: c.raise_it.map(x => x.step).join("; ") });
  const v = m.versions;
  $("versions").textContent = `Engine ${v.engine} · rules ${v.rules} · crops ${v.crop_catalogue} · data ${v.data_pack}`;
  $("mismatch").style.display = m.version_mismatch ? "block" : "none";
  $("mismatch").textContent = m.version_mismatch ? t("version.mismatch", { app: v.engine, pack: PACK.versions.engine }) : "";
  document.querySelectorAll("button.save").forEach(b => b.addEventListener("click", () => savePlan(b.dataset.rot.split("|"), b)));
  renderCompare();
  scheduleSms();
}

function seasonLabel(code, year) {
  const m = PACK.engine.seasons[code].months;
  return `${t(`month.${m[0]}`)}-${t(`month.${m[m.length - 1]}`)} ${year}`;
}

// The answer first: the top plan in three plain steps, why it won, one thing to watch, and what to do next.
function renderBest(res) {
  const top = res.rows[0], m = res.meta, c = m.confidence;
  const steps = top.rotation.map((crop, i) => `<div class="step">${cropImg(crop, "lg")}<div class="stxt"><b>${esc(t(`crop.${crop}`))}</b>
      <span class="latin">${esc(CROP_LATIN[crop] || "")}</span>
      <span class="spill s-${esc(m.slots[i].season)}">${esc(seasonLabel(m.slots[i].season, m.slots[i].year))}</span></div></div>`).join("");
  const why = top.ranks.dry === 1 ? t("best.why1", { n: m.rotations_feasible })
                                  : t("best.why", { n: m.rotations_feasible, dry: top.ranks.dry });
  const care = top.largest_penalty ? `<p class="bcare">${esc(t("sms.concern", { concern: top.largest_penalty }))}</p>` : "";
  $("best").innerHTML = `<div class="bhead"><h2>${esc(t("best.heading"))}</h2>
      <span class="badge ${c.level}" title="q = ${c.q.toFixed(2)}">${esc(t("rot.confidence", { level: c.level_text, q: c.q.toFixed(2) }))}</span></div>
    <div class="steps">${steps}</div><p class="bwhy">${esc(why)}</p>${care}
    <div class="bactions"><button class="save" data-rot="${top.rotation.join("|")}">${esc(t("plans.save"))}</button>
      <a class="btn" href="#all-plans">${esc(t("best.see_all"))}</a></div>`;
  $("best").querySelector("a.btn").addEventListener("click", jumpTo);
}

// Sections open on a laptop and stay folded on a phone, so a small screen shows the answer, not a wall of tables.
function initFolds() {
  const wide = window.matchMedia("(min-width: 721px)").matches;
  document.querySelectorAll("details[data-desk]").forEach(d => { d.open = wide && d.dataset.desk === "open"; });
  $("fold-farm").addEventListener("toggle", () => { if ($("fold-farm").open && MAP) setTimeout(() => MAP.invalidateSize(), 60); });
  document.querySelectorAll("#jump a").forEach(a => a.addEventListener("click", jumpTo));
  $("rot-more").addEventListener("click", () => { MORE = !MORE; renderRotations(); });
}

function jumpTo(e) {
  e.preventDefault();
  const el = document.getElementById(e.currentTarget.getAttribute("href").slice(1));
  if (!el) return;
  if (el.tagName === "DETAILS") el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  if (el.id === "fold-farm" && MAP) setTimeout(() => MAP.invalidateSize(), 400);
}

// --- compare: the farmer's own plan vs the top recommendation vs maize every season ------

function yourPlan() { return load(LS.compare, ["maize", "beans", "groundnut"]); }

function renderCompare() {
  if (!LAST) return;
  const crops = Object.keys(PACK.engine.crops), plan = yourPlan();
  const slots = LAST.meta.slots;
  const slotName = s => t("season.slot", { season: t(`season.${s.season}.short`), year: s.year });
  $("compare-pick").innerHTML = slots.map((s, i) => `<label>${esc(t("compare.slot", { n: i + 1 }))} · ${esc(slotName(s))}
    <select data-i="${i}">${crops.map(c => `<option value="${c}"${c === plan[i] ? " selected" : ""}>${esc(t(`crop.${c}`))}</option>`).join("")}</select></label>`).join("");
  $("compare-pick").querySelectorAll("select").forEach(sel => sel.addEventListener("change", e => {
    const i = Number(e.target.dataset.i), p = yourPlan(), v = e.target.value;
    p[i] = v;
    // cassava fills two seasons: choosing it starts a block in the next season too
    if (v === "cassava" && i < 2 && !(i > 0 && p[i - 1] === "cassava")) p[i + 1] = "cassava";
    store(LS.compare, p);
    renderCompare(); scheduleSms();
  }));

  const valid = new Set(E.enumerateRotations(PACK.engine.crops).map(r => r.join("|")));
  const find = rot => LAST.rows.find(r => r.rotation.join("|") === rot.join("|"));
  const key = plan.join("|");
  let yoursErr = null;
  if (!valid.has(key)) yoursErr = t("compare.invalid");
  else if (LAST.excludedByRotation[key]) yoursErr = t("compare.excluded", { reason: LAST.excludedByRotation[key][0] });
  const cols = [
    { title: t("compare.yours"), row: yoursErr ? null : find(plan), err: yoursErr },
    { title: t("compare.top"), row: LAST.rows[0] },
    { title: t("compare.control"), row: find(["maize", "maize", "maize"]) },
  ];
  const cell = (c, f) => (c.row ? f(c.row) : `<span class="note">${esc(c.err || "—")}</span>`);
  const signed = v => `${v >= 0 ? "+" : ""}${v.toFixed(0)}`;
  const rows = [
    [t("compare.row.crops"), c => cell(c, r => `${cropTiles(r.rotation)}<b>${esc(r.display)}</b>`)],
    [t("compare.row.rank", { n: LAST.meta.rotations_feasible }), c => cell(c, r => String(r.rank))],
    ...E.SCENARIOS.map(sc => [t("compare.row.score", { scenario: t(`scenario.${sc}`) }), c => cell(c, r => f2(r.scores[sc].S))]),
    [t("compare.row.nitrogen"), c => cell(c, r => signed(r.state.N3_kg_ha))],
    [t("compare.row.concern"), c => cell(c, r => `<span class="note">${esc(r.largest_penalty || "")}</span>`)],
  ];
  $("compare-table").innerHTML = `<tr><th></th>${cols.map((c, i) => `<th><span class="key"><i style="background:${COMPARE_COLORS[i]}"></i>${esc(c.title)}</span></th>`).join("")}</tr>` +
    rows.map(([label, f]) => `<tr><th>${esc(label)}</th>${cols.map(c => `<td>${f(c)}</td>`).join("")}</tr>`).join("");
  const series = cols.map((c, i) => ({ name: c.title, color: COMPARE_COLORS[i],
    values: E.SCENARIOS.map(sc => (c.row ? Number(c.row.scores[sc].S.toFixed(2)) : null)) })).filter(s => s.values[0] != null);
  $("compare-legend").innerHTML = series.map(s => `<span class="key"><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
  barChart($("compare-chart"), E.SCENARIOS.map(sc => t(`scenario.${sc}`)), series, t("rot.col.score"), { max: 1 });
}

// --- SMS preview: built by the server, like the real SMS channel ------------------------

let SMS_FOR = "top", SMS_TIMER = null;
function scheduleSms() { clearTimeout(SMS_TIMER); SMS_TIMER = setTimeout(renderSms, 700); }

async function renderSms() {
  if (!PACK || !LAST) return;
  $("sms-top").classList.toggle("on", SMS_FOR === "top");
  $("sms-yours").classList.toggle("on", SMS_FOR === "yours");
  const s = settings() || {}, w = currentWeights(), f = effectiveFarm();
  const q = new URLSearchParams({ lang: LANG, previous_crop: f.farming_history.previous_crops.at(-1),
    farm_type: f.farm.farm_type, residues_removed: !!s.residues_removed });
  for (const [k] of WEIGHTS) q.set(k, w[k]);
  if (SMS_FOR === "yours") q.set("rotation", yourPlan().join(","));
  try {
    const res = await fetchWithTimeout(`/api/sms?${q}`, 15000);
    const body = await res.json();
    if (!res.ok) { $("sms-out").innerHTML = `<p class="note">${esc(body.detail)}</p>`; return; }
    $("sms-out").innerHTML = body.segments.map(seg => `<div class="bubble">${esc(seg)}</div>`).join("") +
      `<p class="note">${esc(t("sms.count", { n: body.segments.length, chars: body.chars, encoding: body.encoding }))}</p>`;
  } catch (e) {
    $("sms-out").innerHTML = `<p class="note">${esc(t("sms.offline"))}</p>`;
  }
}

// --- saved plans and sync --------------------------------------------------------

function newId() {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return Date.now().toString(36) + "-" + Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}

function savePlan(rotation, button) {
  const row = LAST.rows.find(r => r.rotation.join("|") === rotation.join("|"));
  const s = settings() || {};
  const plan = {
    id: newId(), created_at: new Date().toISOString(), rotation, label: row.label, display: row.display,
    lang: LANG, rank: row.rank, score: Number(row.scores.baseline.S.toFixed(3)),
    weights: LAST.meta.weights, previous_crop: LAST.meta.previous_crop,
    residues_removed: !!s.residues_removed, farm_type: effectiveFarm().farm.farm_type,
    slots: LAST.meta.slots.map(x => `${x.season} ${x.year}`), data_through: PACK.data_through,
    pack_generated_at: PACK.generated_at, versions: LAST.meta.versions, synced: false,
  };
  const plans = load(LS.plans, []); plans.unshift(plan); store(LS.plans, plans);
  const outbox = load(LS.outbox, []); outbox.push(plan.id); store(LS.outbox, outbox);
  if (button) { button.textContent = t("plans.saved"); button.disabled = true; }
  renderPlans();
  syncPlans();
}

function renderPlans() {
  const plans = load(LS.plans, []), waiting = load(LS.outbox, []).length;
  $("plans-wait").textContent = waiting ? t("plans.waiting", { n: waiting }) : "";
  if (!plans.length) { $("plans").innerHTML = `<tr><td class="note">${esc(t("plans.none"))}</td></tr>`; return; }
  $("plans").innerHTML = `<tr><th>${esc(t("rot.col.rotation"))}</th><th>${esc(t("rot.col.score"))}</th>
    <th>${esc(t("plans.col.saved"))}</th><th>${esc(t("plans.col.status"))}</th></tr>` +
    plans.slice(0, 20).map(p => `<tr><td>${cropTiles(p.rotation)}${esc(E.rotationLabel(PACK, p.rotation, LANG))}${planVersionNote(p)}</td><td class="num">${p.score.toFixed(2)}</td>
      <td>${esc(shortDate(p.created_at))}</td>
      <td class="${p.synced ? "" : "note"}">${esc(p.synced ? t("plans.synced") : t("plans.pending"))}</td></tr>`).join("");
}

// A plan keeps the versions it was made with; flag ones made under different rules or data.
function planVersionNote(p) {
  const pv = p.versions || {}, cur = (LAST && LAST.meta.versions) || {};
  const diff = ["engine", "rules", "crop_catalogue", "data_pack"].filter(k => pv[k] && cur[k] && pv[k] !== cur[k]);
  return diff.length ? `<br><span class="note">${esc(t("version.older_plan"))} (${diff.map(k => `${k} ${pv[k]}`).join(", ")})</span>` : "";
}

async function syncPlans() {
  const outbox = load(LS.outbox, []);
  if (!outbox.length || !PACK) return;
  const plans = load(LS.plans, []);
  const toSend = plans.filter(p => outbox.includes(p.id)).map(({ synced, display, ...p }) => p);
  try {
    const res = await fetchWithTimeout(`/api/farms/${encodeURIComponent(PACK.farm.farm_id)}/plans`, 20000, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plans: toSend }) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const out = await res.json();
    const done = new Set([...out.accepted, ...out.duplicates, ...out.rejected.map(r => r.id)]);
    store(LS.plans, load(LS.plans, []).map(p => (done.has(p.id) ? { ...p, synced: !out.rejected.some(r => r.id === p.id) } : p)));
    store(LS.outbox, load(LS.outbox, []).filter(id => !done.has(id)));
    setOnline(true);
  } catch (e) {
    clearTimeout(RETRY);
    RETRY = setTimeout(syncPlans, 30000);  // keep trying while the app is open
  }
  renderPlans();
}

// --- climate view and charts (plain SVG: no chart library to download) ----------

function renderClimate() {
  const cl = PACK.climate;
  $("status").textContent = "";          // loading and error messages only; the normal summary lives under "How we compared"
  $("status-sum").textContent = t("status.summary", { start: cl.period.start, end: cl.period.end,
    rain: fmt(cl.annual.mean_rain_mm), temp: fmt(cl.annual.mean_t2m_c) });
  const m = cl.monthly_climatology;
  const months = m.map(r => t(`month.${r.month}`));
  const hasImerg = m.some(r => r.IMERG_PRECTOT != null);
  const series = [{ name: t("chart.rain_power"), color: COLORS.power, values: m.map(r => r.PRECTOTCORR) }];
  if (hasImerg) series.push({ name: t("chart.rain_imerg"), color: COLORS.imerg, values: m.map(r => r.IMERG_PRECTOT) });
  $("rain-legend").innerHTML = series.map(s => `<span class="key"><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
  barChart($("rain-chart"), months, series, "mm");
  lineChart($("temp-chart"), months, m.map(r => r.T2M_MAX), "°C", COLORS.power);
  $("chart-toggle").textContent = SHOW_TABLE ? t("chart.hide_table") : t("chart.show_table");
  $("chart-table").style.display = SHOW_TABLE ? "table" : "none";
  $("chart-table").innerHTML = `<tr><th>${esc(t("chart.month"))}</th>${series.map(s => `<th>${esc(s.name)}</th>`).join("")}
    <th>${esc(t("chart.tmax"))}</th></tr>` + m.map((r, i) => `<tr><td>${esc(months[i])}</td>
    ${series.map(s => `<td class="num">${fmt(s.values[i])}</td>`).join("")}<td class="num">${fmt(r.T2M_MAX)}</td></tr>`).join("");

  const d = t("unit.days_short");
  $("season-table").innerHTML = `<tr><th>${esc(t("seasons.col.season"))}</th><th>${esc(t("seasons.col.months"))}</th>
    <th>${esc(t("seasons.col.mean_rain"))}</th><th>${esc(t("seasons.col.cv"))}</th><th>${esc(t("seasons.col.dry_year"))}</th>
    <th>${esc(t("seasons.col.dry_spell"))}</th><th>${esc(t("seasons.col.years"))}</th></tr>` +
    Object.entries(cl.seasons).map(([k, s]) => `<tr><td>${k}: ${esc(t(`season.${k}.name`))}</td>
      <td>${s.months.map(x => esc(t(`month.${x}`))).join(", ")}</td><td>${fmt(s.summary.mean_rain_mm)} mm</td>
      <td>${fmt(s.summary.cv)}</td><td>${fmt(s.summary.p20_rain_mm)} mm</td>
      <td>${fmt(s.summary.mean_longest_dry_spell_days)} ${esc(d)}</td><td>${s.summary.years_used}</td></tr>`).join("");
  const cc = cl.rain_crosscheck;
  $("crosscheck").innerHTML = cc ? `<b>${esc(t("crosscheck.title"))}</b> ` + Object.entries(cc).map(([k, c]) =>
    esc(t("crosscheck.item", { season: k, pct: fmt(c.imerg_minus_power_pct), r: fmt(c.year_to_year_correlation), n: c.years_compared }))).join("; ") : "";
  const sm = cl.soil_moisture;
  $("soilmoisture").innerHTML = sm ? `<b>${esc(t("smap.title"))}</b> ` + Object.entries(sm).map(([k, s]) =>
    esc(t("smap.item", { season: k, mean: fmt(s.summary.mean_m3m3), p20: fmt(s.summary.p20_m3m3), n: s.summary.years_used }))).join("; ") : "";
  const src = (PACK.sources.power || {}).provenance || {};
  $("source-note").textContent = t("source.note", { dataset: src.dataset, access: src.access, lat: src.latitude, lon: src.longitude });
  $("sources-table").innerHTML = `<tr><th>${esc(t("sources.col.dataset"))}</th><th>${esc(t("sources.col.status"))}</th>
    <th>${esc(t("sources.col.detail"))}</th></tr>` + Object.values(PACK.sources).map(s => {
      const p = s.provenance || {};
      const where = p.latitude != null ? `cell ${p.latitude}, ${p.longitude}` :
        p.pixel_latitude != null ? `pixel ${p.pixel_latitude.toFixed(3)}, ${p.pixel_longitude.toFixed(3)}` : "";
      return `<tr><td>${esc(s.label)}</td><td>${esc(t(`sources.status.${s.status}`))}</td>
        <td class="note">${esc(s.status === "ok" ? where : (s.detail || ""))}</td></tr>`;
    }).join("");
}

function renderEvidence() {
  const ev = PACK.evidence;
  $("evidence").style.display = ev ? "" : "none";
  if (!ev) return;
  $("ev-footprint").textContent = t("evidence.footprint");
  const v = ev.vegetation, tr = ev.terrain;
  const monthList = ms => ms.map(m => t(`month.${m}`)).join(", ");
  if (v) {
    const ms = Object.keys(v.monthly).map(Number).sort((a, b) => a - b);
    const lines = [t("evidence.veg.peaks", { months: monthList(v.greenness_peaks_months) }),
                   t("evidence.veg.low", { months: monthList(v.low_cover_months) })];
    for (const [k, s] of Object.entries(v.seasons)) {
      lines.push(t("evidence.veg.season", { season: t(`season.${k}.short`), months: monthList(s.months),
                                            mean: s.mean.toFixed(2), p20: s.p20.toFixed(2), p80: s.p80.toFixed(2) }));
    }
    lines.push(v.season_window_check.peaks_inside_windows ? t("evidence.veg.check_ok") : t("evidence.veg.check_off"));
    $("ev-veg").innerHTML = `<h3>${esc(t("evidence.veg.heading"))}</h3><p class="note">${esc(t("evidence.veg.what"))}</p>
      <div id="ndvi-chart"></div>
      <ul class="note">${lines.map(l => `<li>${esc(l)}</li>`).join("")}</ul>
      <p class="note">${esc(t("evidence.veg.limit"))}</p>
      <p class="note">${esc(t("evidence.veg.source", { start: v.period.start, end: v.period.end }))}</p>`;
    barChart($("ndvi-chart"), ms.map(m => t(`month.${m}`)),
             [{ name: t("evidence.veg.series"), color: "#3f8f4f", values: ms.map(m => v.monthly[m].mean) }], "NDVI", { max: 1 });
  } else $("ev-veg").innerHTML = "";
  const f = ev.field;
  if (f) {
    const ms = Object.keys(f.monthly).map(Number).sort((x, y) => x - y);
    const thin = ms.filter(m => f.monthly[m].thin);
    const lines = [t("evidence.field.least", { months: monthList(f.least_green_months) })];
    for (const [k, s] of Object.entries(f.seasons)) {
      lines.push(t("evidence.field.season", { season: t(`season.${k}.short`), mean: s.mean.toFixed(2), bare: Math.round(s.bare_share * 100) }));
    }
    if (thin.length) lines.push(t("evidence.field.thin", { months: monthList(thin) }));
    $("ev-field").innerHTML = `<h3>${esc(t("evidence.field.heading"))}</h3><p class="note">${esc(t("evidence.field.what"))}</p>
      <div id="field-chart"></div><h3>${esc(t("evidence.field.bare_heading"))}</h3>
      <p class="note">${esc(t("evidence.field.bare_what"))}</p><div id="bare-chart"></div>
      <ul class="note">${lines.map(l => `<li>${esc(l)}</li>`).join("")}</ul>
      <p class="note">${esc(t("evidence.field.limit"))}</p>
      <p class="note">${esc(t("evidence.field.source", { clear: f.scenes.clear, start: f.period.start, end: f.period.end }))}</p>`;
    const wide = v ? v.monthly : {};
    barChart($("field-chart"), ms.map(m => t(`month.${m}`)), [
      { name: t("evidence.field.wide"), color: "#b9b8ad", values: ms.map(m => wide[m] ? wide[m].mean : null) },
      { name: t("evidence.field.series"), color: "#3f8f4f", values: ms.map(m => f.monthly[m].mean) }], "NDVI", { max: 1 });
    barChart($("bare-chart"), ms.map(m => t(`month.${m}`)),
      [{ name: t("evidence.field.bare_series"), color: "#c0762b", values: ms.map(m => f.monthly[m].bare_share) }], "", { max: 1 });
  } else $("ev-field").innerHTML = "";
  const rs = ev.rain_sources;
  if (rs && Object.keys(rs.seasons).length) {
    const rows = Object.entries(rs.seasons).map(([k, s]) => `<tr><td>${esc(t(`season.${k}.short`))}</td>
      <td class="num">${fmt(s.power_mean_mm)}</td><td class="num">${fmt(s.era5_rain_mean_mm)}</td>
      <td class="num">${fmt(s.imerg_mean_mm)}</td><td class="num">${fmt(s.era5_et0_mean_mm)}</td></tr>`).join("");
    $("ev-rain").innerHTML = `<h3>${esc(t("evidence.rain.heading"))}</h3><p class="note">${esc(t("evidence.rain.note"))}</p>
      <table><tr><th>${esc(t("evidence.rain.col.season"))}</th><th>POWER (mm)</th><th>ERA5 (mm)</th><th>IMERG (mm)</th>
      <th>${esc(t("evidence.rain.col.et0"))} (mm)</th></tr>${rows}</table>
      <p class="note">${esc(t("evidence.rain.et0_note"))}</p><p class="note">${esc(t("evidence.rain.source"))}</p>`;
  } else $("ev-rain").innerHTML = "";
  if (tr) {
    const cls = { flat: "flat", "gently sloping": "gentle", sloping: "sloping", "strongly sloping": "strong", steep: "steep" }[tr.slope_class];
    const pct = tr.landscape_position.percent_of_window_lower_than_farm;
    const pos = pct > 70 ? "high" : pct < 30 ? "low" : "mid";
    const rows = [[t("evidence.terrain.elevation"), `${Math.round(tr.elevation_m.farm_mean)} m`],
      [t("evidence.terrain.slope"), `${tr.slope_percent.farm_mean.toFixed(1)} % (${t(`evidence.slope.${cls}`)})`],
      [t("evidence.terrain.drains"), `${tr.downslope_direction.compass} (${tr.downslope_direction.degrees}°)`],
      [t("evidence.terrain.position"), t(`evidence.pos.${pos}`)],
      [t("evidence.terrain.frost"), t("evidence.terrain.frost_none")]];
    $("ev-terrain").innerHTML = `<h3>${esc(t("evidence.terrain.heading"))}</h3><table>${rows.map(r =>
      `<tr><th>${esc(r[0])}</th><td>${esc(r[1])}</td></tr>`).join("")}</table>
      <p class="note">${esc(t("evidence.terrain.limit"))}</p><p class="note">${esc(t("evidence.terrain.source"))}</p>`;
  } else $("ev-terrain").innerHTML = "";
}

const SVGNS = "http://www.w3.org/2000/svg";
function niceMax(v) { const step = v > 100 ? 50 : v > 20 ? 10 : 5; return Math.ceil(v / step) * step; }

function axes(svg, W, H, M, ticks, y, unit) {
  let s = "";
  for (const v of ticks) {
    s += `<line x1="${M.l}" x2="${W - M.r}" y1="${y(v)}" y2="${y(v)}" stroke="${COLORS.grid}" stroke-width="1"/>
          <text x="${M.l - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="${COLORS.muted}">${v}</text>`;
  }
  s += `<text x="${M.l - 6}" y="${M.t - 12}" text-anchor="end" font-size="11" fill="${COLORS.muted}">${esc(unit)}</text>`;
  return s;
}

function tooltip(evt, html) {
  const tip = $("tip");
  tip.innerHTML = html; tip.style.display = "block";
  const x = Math.min(evt.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
  tip.style.left = `${x}px`; tip.style.top = `${evt.clientY + 12}px`;
}
function hideTip() { $("tip").style.display = "none"; }

// Some languages have long month names (Luganda has no short forms), so when the labels would
// touch each other they are turned 45 degrees and the chart gets a taller bottom margin.
function labelLayout(labels, band) {
  const widest = Math.max(...labels.map(l => String(l).length)) * 6.3;     // about 6.3 px a letter at 11 px
  return widest <= band - 4 ? { rotate: false, bottom: 26 } : { rotate: true, bottom: Math.ceil(widest * Math.SQRT1_2) + 22 };
}
function axisLabel(cx, axisY, text, lay) {          // axisY: where the labels hang from (the baseline of the chart)
  const common = `font-size="11" fill="${COLORS.muted}"`;
  return lay.rotate
    ? `<text transform="translate(${cx + 4},${axisY + 14}) rotate(-45)" text-anchor="end" ${common}>${esc(text)}</text>`
    : `<text x="${cx}" y="${axisY + 18}" text-anchor="middle" ${common}>${esc(text)}</text>`;
}

function barChart(el, labels, series, unit, opts = {}) {
  const W = 860, M = { l: 44, r: 8, t: 28, b: 26 };
  const max = opts.max ?? niceMax(Math.max(...series.flatMap(s => s.values.filter(v => v != null)), 1));
  const iw = W - M.l - M.r, band = iw / labels.length, lay = labelLayout(labels, band);
  M.b = lay.bottom;
  const H = 260 - 26 + M.b, ih = H - M.t - M.b;
  const y = v => M.t + ih - (v / max) * ih;
  const gap = 2, bw = Math.max(4, (band - 10 - gap * (series.length - 1)) / series.length);
  let s = axes(el, W, H, M, [0, max / 4, max / 2, (3 * max) / 4, max], y, unit);
  labels.forEach((lab, i) => {
    const x0 = M.l + i * band + (band - (bw * series.length + gap * (series.length - 1))) / 2;
    s += `<rect class="hover" data-i="${i}" x="${M.l + i * band}" y="${M.t}" width="${band}" height="${ih}" fill="transparent"/>`;
    series.forEach((ser, j) => {
      const v = ser.values[i];
      if (v == null) return;
      const x = x0 + j * (bw + gap), top = y(v), h = M.t + ih - top, r = Math.min(4, h, bw / 2);
      s += `<path pointer-events="none" fill="${ser.color}" d="M${x},${M.t + ih} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${M.t + ih} Z"/>`;
    });
    s += axisLabel(M.l + i * band + band / 2, M.t + ih, lab, lay);
  });
  s += `<line x1="${M.l}" x2="${W - M.r}" y1="${M.t + ih}" y2="${M.t + ih}" stroke="${COLORS.axis}"/>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img">${s}</svg>`;
  el.querySelectorAll("rect.hover").forEach(rc => {
    const i = Number(rc.dataset.i);
    rc.addEventListener("pointermove", e => {
      el.querySelectorAll("rect.hover").forEach(o => o.setAttribute("fill", "transparent"));
      rc.setAttribute("fill", "rgba(11,11,11,0.04)");
      tooltip(e, `<b>${esc(labels[i])}</b><br>` + series.map(ser =>
        `<span class="key"><i style="background:${ser.color}"></i>${esc(ser.name)}: <b>${fmt(ser.values[i])}</b></span>`).join("<br>"));
    });
    rc.addEventListener("pointerleave", () => { rc.setAttribute("fill", "transparent"); hideTip(); });
  });
}

function lineChart(el, labels, values, unit, color) {
  const W = 860, M = { l: 44, r: 8, t: 28, b: 26 };
  const vals = values.filter(v => v != null);
  const lo = Math.floor(Math.min(...vals) - 1), hi = Math.ceil(Math.max(...vals) + 1);
  const iw = W - M.l - M.r, band = iw / labels.length, lay = labelLayout(labels, band);
  M.b = lay.bottom;
  const H = 190 - 26 + M.b, ih = H - M.t - M.b;
  const x = i => M.l + i * band + band / 2, y = v => M.t + ih - ((v - lo) / (hi - lo)) * ih;
  const ticks = [lo, Math.round((lo + hi) / 2), hi];
  let s = axes(el, W, H, M, ticks, y, unit);
  const pts = values.map((v, i) => (v == null ? null : [x(i), y(v)])).filter(Boolean);
  s += `<polyline fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" points="${pts.map(p => p.join(",")).join(" ")}"/>`;
  s += `<line id="cross" x1="0" x2="0" y1="${M.t}" y2="${M.t + ih}" stroke="${COLORS.axis}" visibility="hidden"/>`;
  values.forEach((v, i) => {
    if (v != null) s += `<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="${color}" stroke="#fcfcfb" stroke-width="2"/>`;
    s += axisLabel(x(i), M.t + ih, labels[i], lay);
    s += `<rect class="hover" data-i="${i}" x="${M.l + i * band}" y="${M.t}" width="${band}" height="${ih}" fill="transparent"/>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img">${s}</svg>`;
  const cross = el.querySelector("#cross");
  el.querySelectorAll("rect.hover").forEach(rc => {
    const i = Number(rc.dataset.i);
    rc.addEventListener("pointermove", e => {
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      tooltip(e, `<b>${esc(labels[i])}</b><br>${esc(t("chart.tmax"))}: <b>${fmt(values[i])}</b>`);
    });
    rc.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  });
}

// --- map: Leaflet when it can load; a plain card when offline ------------------

function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  return new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    document.head.appendChild(css);
    const js = document.createElement("script");
    js.src = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    const timer = setTimeout(() => reject(new Error("timeout")), 8000);
    js.onload = () => { clearTimeout(timer); resolve(window.L); };
    js.onerror = () => { clearTimeout(timer); reject(new Error("offline")); };
    document.head.appendChild(js);
  });
}

async function renderMap() {
  const { latitude: lat, longitude: lon } = PACK.farm.location;
  if (MAP) return;
  try {
    const L = await loadLeaflet();
    $("map").innerHTML = "";
    MAP = L.map("map").setView([lat, lon], 11);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 18, attribution: "&copy; OpenStreetMap contributors" }).addTo(MAP);
    L.marker([lat, lon]).addTo(MAP).bindPopup(PACK.farm.name);
  } catch (e) {
    $("map").innerHTML = `<div class="mapcard">${esc(t("map.offline", { lat, lon }))}</div>`;
  }
}

// --- start ------------------------------------------------------------------------

async function main() {
  initFolds();
  LANG = new URLSearchParams(location.search).get("lang") || load(LS.lang, "en");
  $("lang").addEventListener("change", e => { LANG = e.target.value; store(LS.lang, LANG); renderAll(); });
  $("update").addEventListener("click", () => refreshPack(true));
  $("chart-toggle").addEventListener("click", () => { SHOW_TABLE = !SHOW_TABLE; renderClimate(); });
  $("sms-top").addEventListener("click", () => { SMS_FOR = "top"; renderSms(); });
  $("sms-yours").addEventListener("click", () => { SMS_FOR = "yours"; renderSms(); });
  window.addEventListener("online", () => { refreshPack(); syncPlans(); });
  window.addEventListener("offline", () => setOnline(false));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) syncPlans(); });
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(e => console.warn("ARAPAI FARM TWIN: offline mode unavailable", e));
  }
  PACK = load(LS.pack, null);
  if (PACK) renderAll();                // show saved data at once, even with no network
  else $("status").textContent = "Loading NASA data…";
  await refreshPack();                  // then try for fresh data
  syncPlans();
}
main().catch(e => { $("status").innerHTML = `<span class="err">${esc(e)}</span>`; });
