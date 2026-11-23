// Service worker for the VDAB Job Applier PWA.
// Network-first for the app shell + data so updates always reach the phone
// (previously the shell was cache-first, which pinned users to an old UI).
const CACHE = "vjobs-v92";
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

function networkFirst(request, keep = true, mode = "no-store") {
  // cache:"no-store" so the browser's own HTTP cache can't hand back a stale
  // shell: GitHub Pages serves index.html with max-age, which kept a phone on
  // an old app version (and so an old sync payload) even after a reload.
  // The stored copy is keyed WITHOUT the ?t= cache-buster the app adds: keyed
  // by full URL, every load added another copy (listing.json is ~38 MB) to the
  // cache until the phone ran out of room.
  const key = request.url.split("?")[0];
  return fetch(request, { cache: mode })
    .then((r) => {
      if (keep && r.ok) {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(key, copy));
      }
      return r;
    })
    .catch(() => caches.match(key).then((r) => r || caches.match("./index.html")));
}

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  const isHTML = e.request.mode === "navigate" ||
    url.pathname.endsWith("/") || url.pathname.endsWith("index.html");

  // App shell (HTML) and data: always try the network first so the UI updates.
  if (isHTML || url.pathname.endsWith("jobs.json") || url.pathname.endsWith("listing.json") ||
      url.pathname.endsWith("cv.en.md") || url.pathname.endsWith("cv.nl.md") ||
      url.pathname.endsWith("cv.pdf") || url.pathname.endsWith("cv-nl.pdf")) {
    // listing.json: not kept (38 MB) and revalidated with its ETag, so an
    // unchanged file costs a 304, not a download.
    const big = url.pathname.endsWith("listing.json");
    e.respondWith(big ? networkFirst(e.request, false, "no-cache") : networkFirst(e.request));
    return;
  }
  // Static assets (icons, manifest): cache-first is fine.
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});
