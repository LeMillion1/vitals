// v8 — bumped so `activate` drops the v7 cache after this review batch's
// vitals.css/base.html edits, so clients get the new assets on next load
// instead of one stale-while-revalidate paint.
const CACHE_NAME = 'vitals-os-v8';

const OFFLINE_PAGE = '/static/offline.html';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.add(OFFLINE_PAGE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_PAGE))
    );
    return;
  }

  // Medical files (lab sheets, InBody printouts, progress photos) are served
  // from /files/* and are user data, not app shell — caching them would leave
  // medical images in Cache Storage forever, outside the session and untouched
  // by logout. They never match the /static/ test below, so nothing here has to
  // exclude them; /static/uploads/* is kept out too because the private tree
  // still lives on disk under that prefix even though nothing serves from it.
  if (sameOrigin && url.pathname.startsWith('/static/')
      && !url.pathname.startsWith('/static/uploads/')) {
    // Stale-while-revalidate: serve the cached copy instantly (offline-friendly),
    // but ALWAYS kick off a background fetch to refresh the cache. Cache-first
    // (the old strategy) pinned /static/* to whatever was cached until CACHE_NAME
    // was bumped by hand, so updated CSS/JS stayed stale after a deploy. Now a
    // deploy is picked up on the next load after one stale paint.
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) =>
        cache.match(req).then((cached) => {
          const network = fetch(req)
            .then((res) => {
              if (res && res.status === 200) {
                cache.put(req, res.clone());
              }
              return res;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
  }
});
