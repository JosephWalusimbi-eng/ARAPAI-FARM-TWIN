// ARAPAI FARM TWIN service worker: lets the installed app open and run with no internet.
//
// - App files (page, scripts, icon): network first with a short timeout, so a slow or
//   absent connection falls back to the saved copy within a few seconds.
// - Map library and map tiles from the internet: kept after first use (tiles capped).
// - Farm data (/api/pack) is NOT cached here: the app stores it itself and shows its date,
//   so it can tell the farmer whether the data is fresh or saved.
const VERSION = "farmtwin-v15";
const SHELL = `${VERSION}-shell`;
const EXTERNAL = `${VERSION}-external`;
const CROPS = ["maize", "sorghum", "finger_millet", "beans", "groundnut", "cowpea", "sunflower", "cassava"];
const SHELL_FILES = ["/", "/static/app.js", "/static/engine.js", "/static/icon.svg", "/manifest.webmanifest",
                     ...CROPS.map(c => `/static/crops/${c}.jpg`)];
const MAX_TILES = 400;
const NETWORK_TIMEOUT_MS = 4000;

self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => !k.startsWith(VERSION)).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function networkFirst(request) {
  const cache = await caches.open(SHELL);
  const network = fetch(request).then(res => {
    if (res.ok) cache.put(request, res.clone());
    return res;
  });
  network.catch(() => {});  // a late failure after the timeout is not an error
  const timeout = new Promise(resolve => setTimeout(resolve, NETWORK_TIMEOUT_MS, null));
  try {
    const first = await Promise.race([network, timeout]);
    if (first) return first;
  } catch (e) { /* offline: fall through to the saved copy */ }
  const saved = await cache.match(request, { ignoreSearch: true });
  if (saved) return saved;
  return network;  // nothing saved yet: wait for the network after all
}

async function trim(cache, max) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

async function cacheFirst(request) {
  const cache = await caches.open(EXTERNAL);
  const saved = await cache.match(request);
  if (saved) return saved;
  const res = await fetch(request);
  if (res.ok || res.type === "opaque") {
    await cache.put(request, res.clone());
    trim(cache, MAX_TILES);
  }
  return res;
}

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin === self.location.origin) {
    if (url.pathname.startsWith("/api/")) return;  // live data: never served stale from here
    event.respondWith(networkFirst(req));
  } else if (url.hostname === "unpkg.com" || url.hostname.endsWith("tile.openstreetmap.org")) {
    event.respondWith(cacheFirst(req));
  }
});
