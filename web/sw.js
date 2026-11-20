// Offline support: the app shell is cached so the installed app opens without a connection.
const CACHE = "arcolorize-v2";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icons/icon-192.png", "./icons/icon-512.png", "./icons/maskable-512.png", "./icons/apple-touch-icon.png", "./icons/logo-128.png", "./favicon.ico"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const req = e.request; if (req.method !== "GET") return;
  const same = new URL(req.url).origin === location.origin;
  // Our own files: network first (so updates arrive), cache as fallback. Fonts: cache after first load.
  e.respondWith(fetch(req).then(res => {
    if (res.ok && (same || req.url.includes("fonts.g"))) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
    return res;
  }).catch(() => caches.match(req).then(r => r || caches.match("./index.html"))));
});
