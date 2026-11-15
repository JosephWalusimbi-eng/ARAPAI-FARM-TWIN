// FARM TWIN rotation engine: JavaScript port of backend/farm_twin/engine.py (spec v0.1.1).
//
// Runs in the browser with no network. Every input comes from the farm pack
// (/api/pack), which the phone stores while it is online. The arithmetic mirrors the
// Python engine operation by operation, and tests/test_offline.py checks that both rank
// every rotation identically. Change one, change the other.

// Must equal ENGINE_VERSION in backend/farm_twin/engine.py (checked by tests/test_offline.py).
export const ENGINE_VERSION = "0.2.0";

export const SCENARIOS = ["baseline", "dry", "wet", "hot"];
export const TERMS = ["C", "W", "H", "K", "F"];
const N_SLOTS = 3;

export class EngineError extends Error {}

const clip = (x, lo, hi) => Math.min(Math.max(x, lo), hi);
const mean = xs => xs.reduce((a, x) => a + x, 0) / xs.length;
const sum = xs => xs.reduce((a, x) => a + x, 0);

// Python round(x): exact ties go to the even neighbour.
export function pyRound(x) {
  const f = Math.floor(x), d = x - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

// Python f"{x:.Nf}" for N = 0 or 1. toFixed differs only on exact binary ties
// (12.5 -> "12" in Python, "13" in JS), which Python rounds half to even.
export function pyFixed(x, n) {
  const m = 10 ** n, y = x * m;
  if (Number.isInteger(x * 4) && Math.abs(y % 1) === 0.5) {
    return (pyRound(y) / m).toFixed(n);
  }
  return x.toFixed(n);
}

// --- translations -----------------------------------------------------------

export function t(pack, lang, key, params = {}) {
  const cat = pack.i18n[lang] || pack.i18n.en;
  const s = cat.strings[key] ?? pack.i18n.en.strings[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m));
}

export function render(pack, lang, msg, capitalize = true) {
  const p = { ...msg.params };
  for (const name of ["crop", "prev"]) {
    if (name in p) p[name] = t(pack, lang, `crop.${p[name]}`).toLowerCase();
  }
  if ("scenario" in p) p.scenario = t(pack, lang, `scenario.${p.scenario}`);
  if ("season" in p) {
    const [code, year] = p.season;
    const months = pack.engine.seasons[code].months;
    p.season = `${t(pack, lang, `month.${months[0]}`)}-${t(pack, lang, `month.${months[months.length - 1]}`)} ${year}`;
  }
  for (const name of ["family", "disease"]) {
    if (name in p) p[name] = t(pack, lang, `${name}.${p[name]}`);
  }
  if ("reasons" in p) {
    p.reasons = p.reasons.map(r => render(pack, lang, r, false)).join(t(pack, lang, "reason.join"));
  }
  const text = t(pack, lang, msg.key, p);
  return capitalize ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

const msg = (key, params) => ({ key, params });

// --- inputs -----------------------------------------------------------------

export function nextSlots(seasons, today, n = N_SLOTS) {
  const [ty, tm, td] = today;
  const order = Object.keys(seasons).sort((a, b) => seasons[a].months[0] - seasons[b].months[0]);
  const after = (y, m) => y > ty || (y === ty && (m > tm || (m === tm && 1 > td)));
  const out = [];
  let year = ty;
  while (out.length < n) {
    for (const key of order) {
      if (after(year, seasons[key].months[0]) && out.length < n) out.push([key, year]);
    }
    year += 1;
  }
  return out;
}

// FAO-56 average crop coefficient over the cycle (mirrors kc_average in engine.py).
export function kcAverage(crop) {
  const total = crop.st_ini + crop.st_dev + crop.st_mid + crop.st_late;
  return (crop.st_ini * crop.kc_ini + crop.st_dev * ((crop.kc_ini + crop.kc_mid) / 2)
          + crop.st_mid * crop.kc_mid + crop.st_late * ((crop.kc_mid + crop.kc_end) / 2)) / total;
}

// Crop water need for one slot, mm, and its source (mirrors water_need in engine.py).
export function waterNeed(crop, sc, params) {
  if (sc.et0 === null || sc.et0 === undefined || !(crop.kc_mid > 0)) return [crop.water_need_mm, "assumed"];
  const days = crop.slots === 1 ? crop.days : params.season_window_days;
  return [sc.et0 * days * kcAverage(crop), "fao56"];
}

export function scenarioClimates(seasonStats, slots, params) {
  const picks = {
    baseline: ["mean_rain_mm", "mean_longest_dry_spell_days", 0.0],
    dry: ["p20_rain_mm", "p80_longest_dry_spell_days", 0.0],
    wet: ["p80_rain_mm", "p20_longest_dry_spell_days", 0.0],
    hot: ["mean_rain_mm", "mean_longest_dry_spell_days", params.hot_delta_c],
  };
  const out = {};
  for (const [sc, [pKey, dKey, dT]] of Object.entries(picks)) {
    out[sc] = slots.map(([season, year], i) => {
      const summ = (seasonStats[season] || {}).summary || {};
      const vals = [summ[pKey], summ[dKey], summ.mean_tmax_c];
      if (!summ.years_used || vals.some(v => v === null || v === undefined)) {
        throw new EngineError(`season ${season}: no complete years of climate data`);
      }
      const et0 = summ.mean_et0_mm_day;
      return { slot: i + 1, season, year, P: vals[0], D: vals[1], T: vals[2] + dT,
               et0: (et0 === null || et0 === undefined) ? null : et0 };
    });
  }
  return out;
}

export function farmWeights(pack, farm, overrides = {}) {
  const prefs = { ...(farm.farmer_preferences || {}), ...overrides };
  const w = {};
  for (const [key, term] of Object.entries(pack.engine.weight_keys)) w[term] = Number(prefs[key] ?? 0);
  const vals = Object.values(w);
  if (vals.some(v => v < 0) || sum(vals) <= 0) throw new EngineError("weights must be non-negative and not all zero");
  const total = sum(vals);
  for (const k of Object.keys(w)) w[k] = w[k] / total;
  return w;
}

export function historyState(crops, farm) {
  const hist = ((farm.farming_history || {}).previous_crops) || [];
  if (!hist.length) return [null, 0];
  const c0 = hist[hist.length - 1];
  const fam = crops[c0] ? crops[c0].family : null;
  let k = 0;
  for (let i = hist.length - 1; i >= 0; i--) {
    const c = hist[i];
    if (fam !== null && crops[c] && crops[c].family === fam) k += 1; else break;
  }
  return [c0, Math.max(k, 1)];
}

// --- rotations and constraints ----------------------------------------------

function product(items, repeat) {
  let out = [[]];
  for (let i = 0; i < repeat; i++) out = out.flatMap(p => items.map(x => [...p, x]));
  return out;
}

export function enumerateRotations(crops, n = N_SLOTS) {
  const annual = Object.keys(crops).filter(k => crops[k].slots === 1);
  const multi = Object.keys(crops).filter(k => crops[k].slots > 1);
  const out = product(annual, n);
  for (const m of multi) {
    const span = crops[m].slots;
    for (let start = 0; start < n - span + 1; start++) {
      for (const fill of product(annual, n - span)) {
        out.push([...fill.slice(0, start), ...Array(span).fill(m), ...fill.slice(start)]);
      }
    }
  }
  return out;
}

function blockStarts(crops, rotation) {
  const starts = rotation.map(() => false);
  let i = 0;
  while (i < rotation.length) { starts[i] = true; i += crops[rotation[i]].slots; }
  return starts;
}

export function slotFeasibility(crops, climates, params) {
  const window = params.season_window_days + params.residual_moisture_days;
  const out = {};
  for (const c of Object.values(crops)) {
    for (const sc of climates) {
      let reason = null;
      if (c.slots === 1 && c.days > window) {
        reason = msg("exclude.duration", { crop: c.key, days: c.days, window });
      } else if (params.effective_rain_fraction * sc.P / waterNeed(c, sc, params)[0] < params.min_baseline_r) {
        reason = msg("exclude.water", { crop: c.key, pct: pyRound(100 * params.min_baseline_r),
                                         season: [sc.season, sc.year] });
      } else if (sc.T >= c.tmax_crit_c) {
        reason = msg("exclude.heat", { crop: c.key, temp: pyFixed(sc.T, 1), crit: pyFixed(c.tmax_crit_c, 0) });
      }
      out[`${c.key}|${sc.slot}`] = reason;
    }
  }
  return out;
}

// --- simulation and scoring -------------------------------------------------

function fT(T, crop) {
  if (T <= crop.tmax_opt_c) return 1.0;
  if (T >= crop.tmax_crit_c) return 0.0;
  return (crop.tmax_crit_c - T) / (crop.tmax_crit_c - crop.tmax_opt_c);
}

function transitionPenalty(crops, diseases, prev, crop, streak, params) {
  let p = 0.0;
  const why = [];
  if (prev === null) return [p, why];
  const a = crops[prev], b = crops[crop];
  if (prev === crop) {
    p += params.repeat_penalty;
    why.push(msg("reason.same_crop", { crop }));
  } else if (a && a.family === b.family) {
    p += params.same_family_penalty;
    why.push(msg("reason.same_family", { crop, prev, family: b.family }));
  }
  if (streak >= params.streak_threshold) {
    p += params.streak_penalty;
    why.push(msg("reason.streak", { n: streak, family: b.family }));
  }
  const disease = diseases[[prev, crop].sort().join("|")];
  if (disease && prev !== crop && a) {
    p += params.disease_penalty;
    why.push(msg("reason.disease", { crop, prev, disease }));
  }
  return [p, why];
}

export function simulate(ctx, rotation, climates, weights) {
  const { crops, diseases, params, c0, k0, alpha, m } = ctx;
  let N = 0.0, R = 0.0, prev = c0, k = k0;
  const seasons = [], transitions = [];
  const starts = blockStarts(crops, rotation);
  rotation.forEach((key, i) => {
    const sc = climates[i], crop = crops[key];
    const [wr, wrSrc] = waterNeed(crop, sc, params);
    const r = Math.min(1.0, params.effective_rain_fraction * sc.P / wr);
    const d = crop.dry_spell_tol_days;
    const g = clip(1 - Math.max(0.0, sc.D - d) / d, 0, 1);
    const f = fT(sc.T, crop);
    const fwet = (crop.waterlog_sensitive && sc.P > params.wet_factor * wr)
      ? params.wet_penalty : 1.0;
    const C = f * fwet, W = r * g;
    const F = r * (alpha * crop.food + (1 - alpha) * crop.market);
    N += r * crop.net_n_kg_ha;
    R += r * crop.residue_index * m;
    if (starts[i]) {
      const prevCrop = prev ? crops[prev] : undefined;
      k = (prevCrop && prevCrop.family === crop.family) ? k + 1 : 1;
      const [p, why] = transitionPenalty(crops, diseases, prev, key, k, params);
      transitions.push({ slot: sc.slot, from: prev, to: key, penalty: p, reasons: why });
      prev = key;
    }
    seasons.push({ slot: sc.slot, season: `${sc.season} ${sc.year}`, season_code: sc.season, year: sc.year,
                   crop: key, P_mm: sc.P, D_days: sc.D, T_c: sc.T, WR_mm: wr, WR_source: wrSrc, r, g, f_T: f, f_wet: fwet, C, W, F });
  });
  const Cs = seasons.map(s => s.C), Ws = seasons.map(s => s.W);
  const terms = {
    C: 0.5 * mean(Cs) + 0.5 * Math.min(...Cs),
    W: 0.5 * mean(Ws) + 0.5 * Math.min(...Ws),
    H: params.h_n_weight * clip(0.5 + N / params.n_scale_kg_ha, 0, 1) + params.h_residue_weight * R / N_SLOTS,
    K: clip(1 - sum(transitions.map(t => t.penalty)), 0, 1),
    F: mean(seasons.map(s => s.F)),
  };
  const S = TERMS.reduce((a, term) => a + weights[term] * terms[term], 0);
  return { S, ...terms, N3: N, R3: R, seasons, transitions };
}

// --- explanations -----------------------------------------------------------

function issues(runs, weights, params) {
  const out = [];
  for (const sc of SCENARIOS) {
    for (const s of runs[sc].seasons) {
      const c = runs.crops[s.crop];
      const where = { scenario: sc, crop: c.key, slot: s.slot, season: [s.season_code, s.year] };
      if (s.r < 1) out.push({ scenario: sc, term: "W", cost: weights.W * (1 - s.r) / N_SLOTS,
                              msg: msg("issue.water", { pct: pyRound(100 * s.r), ...where }) });
      if (s.g < 1) out.push({ scenario: sc, term: "W", cost: weights.W * s.r * (1 - s.g) / N_SLOTS,
                              msg: msg("issue.dry_spell", { days: pyFixed(s.D_days, 0),
                                                            tol: pyFixed(c.dry_spell_tol_days, 0), ...where }) });
      if (s.f_T < 1) out.push({ scenario: sc, term: "C", cost: weights.C * (1 - s.f_T) / N_SLOTS,
                                msg: msg("issue.heat", { temp: pyFixed(s.T_c, 1), opt: pyFixed(c.tmax_opt_c, 0), ...where }) });
      if (s.f_wet < 1) out.push({ scenario: sc, term: "C", cost: weights.C * (1 - s.f_wet) * s.f_T / N_SLOTS,
                                  msg: msg("issue.waterlog", { rain: pyFixed(s.P_mm, 0), ...where }) });
    }
  }
  const base = runs.baseline;
  for (const tr of base.transitions) {
    if (tr.penalty > 0) out.push({ scenario: "all", term: "K", cost: weights.K * tr.penalty,
                                   msg: msg("issue.rotation", { reasons: tr.reasons, slot: tr.slot }) });
  }
  if (base.N3 < 0) {
    const lost = Math.min(0.5, -base.N3 / params.n_scale_kg_ha);
    out.push({ scenario: "baseline", term: "H", cost: weights.H * params.h_n_weight * lost,
               msg: msg("issue.nitrogen", { kg: pyFixed(-base.N3, 0) }) });
  }
  return out.sort((a, b) => b.cost - a.cost);  // stable, like Python's sort
}

export function rotationLabel(pack, rotation, lang = "en") {
  const crops = pack.engine.crops;
  const starts = blockStarts(crops, rotation);
  const parts = [];
  rotation.forEach((key, i) => {
    if (!starts[i]) return;
    const name = t(pack, lang, `crop.${key}`);
    const n = crops[key].slots;
    parts.push(n > 1 ? t(pack, lang, "rot.multi_slot", { crop: name, n }) : name);
  });
  return parts.join(t(pack, lang, "rot.arrow"));
}

// --- confidence -------------------------------------------------------------

export function confidence(pack, farm, lang) {
  const perYear = pack.climate.annual.per_year || [];
  const years = perYear.length;
  const qClim = years ? perYear.filter(y => y.rain_mm !== null && y.rain_mm !== undefined).length / years : 0.0;
  const src = (((farm.soil_profile || {}).data_source) || "pending").toLowerCase();
  const qSoil = ["lab", "lab_test"].includes(src) ? 1.0 : ["soilgrids", "isda"].includes(src) ? 0.5 : 0.0;
  const qCrop = pack.engine.crop_params_status === "reviewed" ? 1.0 : 0.0;
  const q = 0.4 * qClim + 0.3 * qSoil + 0.3 * qCrop;
  const level = q >= 0.75 ? "High" : q >= 0.5 ? "Medium" : "Low";
  const steps = [];
  if (qCrop < 1) steps.push({ key: "confidence.step.agronomist", gain: 0.3 });
  if (qSoil < 1) steps.push({ key: qSoil ? "confidence.step.lab" : "confidence.step.soil_data", gain: 0.3 * (1 - qSoil) });
  if (qClim < 1) steps.push({ key: "confidence.step.climate", gain: 0.4 * (1 - qClim) });
  steps.sort((a, b) => b.gain - a.gain);
  for (const st of steps) st.step = t(pack, lang, st.key);
  return { q, level, level_text: t(pack, lang, `confidence.${level}`), q_clim: qClim, q_soil: qSoil,
           q_crop: qCrop, raise_it: steps };
}

// --- top level --------------------------------------------------------------

/**
 * Score every feasible rotation under every scenario and rank by baseline S.
 * opts: { today: [y, m, d], weights: {farmer_preferences overrides}, alpha, residuesRemoved,
 *         lang, farm (an edited copy of pack.farm) }
 */
export function rankRotations(pack, opts = {}) {
  const lang = opts.lang || "en";
  const farm = opts.farm || pack.farm;
  const params = pack.engine.params;
  const crops = {};
  for (const [k, c] of Object.entries(pack.engine.crops)) crops[k] = c;
  const diseases = {};
  for (const [a, b, code] of pack.engine.disease_pairs) diseases[[a, b].sort().join("|")] = code;
  const m = opts.residuesRemoved === true ? params.residue_removed
    : opts.residuesRemoved === false ? params.residue_kept : params.residue_kept;
  const w = farmWeights(pack, farm, opts.weights);
  let alpha = opts.alpha;
  if (alpha === undefined || alpha === null) {
    alpha = pack.engine.alpha_by_farm_type[(farm.farm || {}).farm_type] ?? 0.6;
  }
  if (!(alpha >= 0 && alpha <= 1)) throw new EngineError("alpha must be between 0 and 1");
  const [c0, k0] = historyState(crops, farm);
  const now = new Date();
  const today = opts.today || [now.getFullYear(), now.getMonth() + 1, now.getDate()];
  const slots = nextSlots(pack.engine.seasons, today);
  const climates = scenarioClimates(pack.climate.seasons, slots, params);
  const feas = slotFeasibility(crops, climates.baseline, params);

  const all = enumerateRotations(crops);
  const feasible = [], excluded = {}, excludedByRotation = {};
  for (const rot of all) {
    const reasons = rot.map((c, i) => feas[`${c}|${i + 1}`]).filter(Boolean);
    if (reasons.length) {
      excludedByRotation[rot.join("|")] = reasons.map(r => render(pack, lang, r));
      for (const r of reasons) {
        const text = render(pack, lang, r);
        excluded[text] = excluded[text] || { reason: text, code: r.key, rotations: 0 };
        excluded[text].rotations += 1;
      }
    } else feasible.push(rot);
  }
  if (!feasible.length) throw new EngineError("no rotation passes the hard constraints");

  const ctx = { crops, diseases, params, c0, k0, alpha, m };
  const rows = feasible.map(rot => {
    const runs = { crops };
    for (const sc of SCENARIOS) runs[sc] = simulate(ctx, rot, climates[sc], w);
    return { rotation: rot, label: rotationLabel(pack, rot, "en"), display: rotationLabel(pack, rot, lang),
             runs, ranks: {} };
  });
  const byLabel = (a, b) => (a.label < b.label ? -1 : a.label > b.label ? 1 : 0);
  for (const sc of SCENARIOS) {
    [...rows].sort((a, b) => (b.runs[sc].S - a.runs[sc].S) || byLabel(a, b))
      .forEach((row, i) => { row.ranks[sc] = i + 1; });
  }
  rows.sort((a, b) => a.ranks.baseline - b.ranks.baseline);
  for (const row of rows) {
    const runs = row.runs;
    let worst = SCENARIOS[0];
    for (const sc of SCENARIOS) if (runs[sc].S < runs[worst].S) worst = sc;
    const iss = issues(runs, w, params);
    row.rank = row.ranks.baseline;
    row.scores = {};
    for (const sc of SCENARIOS) {
      row.scores[sc] = { S: runs[sc].S };
      for (const term of TERMS) row.scores[sc][term] = runs[sc][term];
    }
    row.robustness = { min_S: runs[worst].S, scenario: worst };
    row.dry_flag = row.ranks.dry - row.ranks.baseline > params.dry_rank_drop_flag;
    row.state = { N3_kg_ha: runs.baseline.N3, R3: runs.baseline.R3 };
    row.seasons = {};
    for (const sc of SCENARIOS) row.seasons[sc] = runs[sc].seasons;
    row.transitions = runs.baseline.transitions;
    row.largest_penalty = iss.length ? render(pack, lang, iss[0].msg) : null;
    row.issues = iss.slice(0, 3).map(i => ({ scenario: i.scenario, term: i.term, cost: i.cost,
                                             code: i.msg.key, text: render(pack, lang, i.msg) }));
    delete row.runs;
  }
  const meta = {
    slots: slots.map(([s, y], i) => ({ slot: i + 1, season: s, year: y, months: pack.engine.seasons[s].months })),
    previous_crop: c0, previous_crop_name: c0 ? t(pack, lang, `crop.${c0}`) : null, previous_streak: k0,
    weights: w, alpha, residue_factor_m: m,
    scenarios: Object.fromEntries(SCENARIOS.map(sc => [sc, climates[sc].map(c => (
      { slot: c.slot, season: `${c.season} ${c.year}`, P_mm: c.P, D_days: c.D, T_c: c.T }))])),
    rotations_considered: all.length, rotations_feasible: feasible.length,
    evaluations: feasible.length * SCENARIOS.length,
    excluded: Object.keys(excluded).sort().map(k => excluded[k]),
    confidence: confidence(pack, farm, lang),
    versions: { engine: ENGINE_VERSION, rules: (pack.versions || {}).rules,
                crop_catalogue: (pack.versions || {}).crop_catalogue, data_pack: (pack.versions || {}).data_pack },
    // A pack made for a different engine: results still run, but the app must say so.
    version_mismatch: !!pack.versions && pack.versions.engine !== ENGINE_VERSION,
  };
  // excludedByRotation: why each removed rotation broke a hard rule (for the comparison view)
  return { rows, meta, excludedByRotation };
}
