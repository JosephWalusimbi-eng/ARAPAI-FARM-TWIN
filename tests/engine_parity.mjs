// Runs the browser engine (frontend/engine.js) under Node for tests/test_offline.py.
// Usage: node engine_parity.mjs <engine module url> <pack.json> <cases.json>
import { readFileSync } from "node:fs";

const [engineUrl, packPath, casesPath] = process.argv.slice(2);
const E = await import(engineUrl);
const pack = JSON.parse(readFileSync(packPath, "utf-8"));
const cases = JSON.parse(readFileSync(casesPath, "utf-8"));

const out = cases.map(c => {
  const res = E.rankRotations(pack, {
    today: c.today, lang: c.lang, weights: c.weights, alpha: c.alpha,
    residuesRemoved: c.residues_removed, farm: c.farm || pack.farm,
  });
  return {
    rows: res.rows.map(r => ({
      label: r.label, display: r.display, rank: r.rank, ranks: r.ranks,
      S: Object.fromEntries(Object.entries(r.scores).map(([k, v]) => [k, v.S])),
      terms: r.scores.baseline, dry_flag: r.dry_flag,
      largest_penalty: r.largest_penalty, issues: r.issues.map(i => i.text),
      min_S: r.robustness.min_S, worst: r.robustness.scenario,
    })),
    excluded: res.meta.excluded,
    q: res.meta.confidence.q,
    level: res.meta.confidence.level,
    steps: res.meta.confidence.raise_it.map(s => s.step),
    feasible: res.meta.rotations_feasible,
    versions: res.meta.versions,
    version_mismatch: res.meta.version_mismatch,
  };
});
process.stdout.write(JSON.stringify(out));
