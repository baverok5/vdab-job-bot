// Service worker for the VDAB Job Applier PWA.
// Network-first for the app shell + data so updates always reach the phone
// (previously the shell was cache-first, which pinned users to an old UI).
const CACHE = "vjobs-v30";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png",
];

self.addEventListener("message", (e) => {
  if (e.data === "skip-waiting") self.skipWaiting();
});

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(request) {
  return fetch(request)
    .then((r) => {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(request, copy));
      return r;
    })
    .catch(() => caches.match(request).then((r) => r || caches.match("./index.html")));
}

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  const isHTML = e.request.mode === "navigate" ||
    url.pathname.endsWith("/") || url.pathname.endsWith("index.html");

  // App shell (HTML) and data: always try the network first so the UI updates.
  if (isHTML || url.pathname.endsWith("jobs.json")) {
    e.respondWith(networkFirst(e.request));
    return;
  }
  // Static assets (icons, manifest): cache-first is fine.
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});
