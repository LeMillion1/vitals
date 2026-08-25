// v9 — installs the PHI-free care notification and drops older shell state.
const CACHE_NAME = 'vitals-os-v9';
const PREFERENCES_CACHE_NAME = 'vitals-sw-prefs-v1';

const OFFLINE_PAGE = '/static/offline.html';
const CARE_INBOX = '/messages';
const CARE_NOTIFICATION_TAG = 'vitals-care-message';
const LOCALE_CACHE_KEY = '/__vitals/service-worker/locale';

function hasExactKeys(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = keys.slice().sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function isCareWakeup(value) {
  return hasExactKeys(value, ['kind', 'v'])
    && value.kind === 'care_message'
    && value.v === 1;
}

function isLocaleMessage(value) {
  return hasExactKeys(value, ['kind', 'locale', 'v'])
    && value.kind === 'set_locale'
    && value.v === 1
    && (value.locale === 'en' || value.locale === 'ru');
}

async function notificationLocale() {
  const cache = await caches.open(PREFERENCES_CACHE_NAME);
  const stored = await cache.match(LOCALE_CACHE_KEY);
  if (stored) {
    const locale = await stored.text();
    if (locale === 'en' || locale === 'ru') return locale;
  }
  const browserLocale = String(self.navigator.language || '').toLowerCase();
  return browserLocale.startsWith('ru') ? 'ru' : 'en';
}

function isSameOriginWindowClient(client) {
  if (!client || client.type !== 'window' || typeof client.url !== 'string') {
    return false;
  }
  try {
    return new URL(client.url).origin === self.location.origin;
  } catch (_error) {
    return false;
  }
}

async function showCareNotification() {
  const locale = await notificationLocale();
  const translations = self.__VITALS_PUSH_COPY__ || {};
  const copy = translations[locale] || translations.en;
  if (!copy || typeof copy.title !== 'string' || typeof copy.body !== 'string') {
    return;
  }
  await self.registration.showNotification(copy.title, {
    body: copy.body,
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    tag: CARE_NOTIFICATION_TAG,
    renotify: true,
    data: { kind: 'care_message', v: 1 }
  });
}

async function openCareInbox() {
  const inboxUrl = new URL(CARE_INBOX, self.location.origin).href;
  const windows = await self.clients.matchAll({
    type: 'window',
    includeUncontrolled: true
  });
  const exact = windows.find((client) => client.url === inboxUrl);
  if (exact) {
    try {
      await exact.focus();
      return;
    } catch (_error) {
      // The window may close between discovery and focus; use the fallback.
    }
  }

  const sameOrigin = windows.find((client) => {
    try {
      return new URL(client.url).origin === self.location.origin;
    } catch (_error) {
      return false;
    }
  });
  if (sameOrigin && typeof sameOrigin.navigate === 'function') {
    try {
      const navigated = await sameOrigin.navigate(CARE_INBOX);
      await (navigated || sameOrigin).focus();
      return;
    } catch (_error) {
      // A closed or non-navigable client falls through to a new app window.
    }
  }
  const opened = await self.clients.openWindow(CARE_INBOX);
  if (opened) {
    try {
      await opened.focus();
    } catch (_error) {
      // The requested window was opened; a focus race needs no second window.
    }
  }
}

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
          if (key !== CACHE_NAME && key !== PREFERENCES_CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (!isLocaleMessage(event.data) || !isSameOriginWindowClient(event.source)) return;
  event.waitUntil(
    caches.open(PREFERENCES_CACHE_NAME).then((cache) => {
      return cache.put(LOCALE_CACHE_KEY, new Response(event.data.locale));
    })
  );
});

self.addEventListener('push', (event) => {
  if (!event.data) return;
  let payload;
  try {
    payload = event.data.json();
  } catch (_error) {
    return;
  }
  if (!isCareWakeup(payload)) return;
  event.waitUntil(showCareNotification());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action || !isCareWakeup(event.notification.data)) return;
  event.waitUntil(openCareInbox());
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
