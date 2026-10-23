/* ngrok-chat service worker
 *
 * NOTE: static/app.js is written against this exact interface — keep the
 * cache name, event names and behaviour stable.
 *
 * BASE-PATH AWARE
 * ---------------
 * This app is deployed at BOTH `/` (direct) and `/chat/` (shared onto the
 * ROMulator2K ngrok domain via nginx, which strips the prefix). Those are the
 * SAME origin, so the service worker MUST stay scoped to the base path it was
 * served from — otherwise it would intercept ROMulator2K's own `/` traffic.
 * `self.registration.scope` is always `<base>/`, so we derive every URL and
 * path check from it rather than hardcoding root-relative paths.
 *
 * Caching strategy
 *   <base>/api/*   network only (dynamic JSON; /api/stream is the SSE channel
 *                                 and must never be intercepted)
 *   <base>/static/*, <base>/  stale-while-revalidate (instant paint, refresh
 *                                                      in background)
 */

'use strict';

/* Bump the version to force a full re-precache on the next install. */
const CACHE = 'ngrok-chat-v2';

/* Scope, e.g. "https://host/" or "https://host/chat/". The browser may hand
 * back the registration scope WITHOUT a trailing slash ("https://host/chat"),
 * so normalize once here — every prefix and URL below depends on it ending
 * in "/". */
const SCOPE = new URL(self.registration.scope);
SCOPE.pathname = scopePathWithSlash(SCOPE.pathname);   // "/chat" -> "/chat/"

/** Pathname normalized to end in "/": "/chat" -> "/chat/", "/" -> "/". */
function scopePathWithSlash(path) {
  return path.endsWith("/") ? path : path + "/";
}

/* Mount-point path, always "/"-suffixed: "/" or "/chat/". */
const scopePath = SCOPE.pathname;

/* Resolve a path relative to the scope: "static/x" -> "<base>/static/x". */
const url = (path) => new URL(path, SCOPE.href).href;

/* App shell: everything the UI needs to boot. Relative to the scope so the
 * precache is correct under /chat/ and would never touch ROMulator2K's root. */
const SHELL = [
  url(''),                            // the app HTML at the base path
  url('static/style.css'),
  url('static/app.js'),
  url('static/manifest.webmanifest'),
  url('static/icons/icon-192.png'),
  url('static/icons/icon-512.png'),
  url('static/icons/maskable-512.png'),
];

/* Scope-relative path prefixes used by the fetch handler. */
const API_PREFIX = scopePath + 'api/';
const STATIC_PREFIX = scopePath + 'static/';

/* ------------------------------------------------------------------ install */
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      // Take over immediately instead of waiting for every tab to close.
      .then(() => self.skipWaiting())
  );
});

/* ------------------------------------------------------------------ activate */
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))
        )
      )
      // Claim open clients so the SW controls them without a reload.
      .then(() => self.clients.claim())
  );
});

/* --------------------------------------------------------------------- fetch */
/** Stale-while-revalidate: answer from cache instantly, refresh it in the
 *  background. Falls back to the network when nothing is cached. */
async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);

  // Kick off the network fetch regardless; never let it reject unhandled.
  const network = fetch(request)
    .then((response) => {
      // Only cache complete, same-origin GET responses.
      if (response && response.ok && response.type !== 'opaque') {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => null);

  if (cached) return cached; // `network` keeps running in the background.

  const fresh = await network;
  return fresh || Response.error();
}

self.addEventListener('fetch', (event) => {
  const { request } = event;

  // Non-GET (POST etc.) always goes straight to the network.
  if (request.method !== 'GET') return;

  const reqUrl = new URL(request.url);

  // Never touch cross-origin requests.
  if (reqUrl.origin !== self.location.origin) return;

  // Defense in depth: the browser already limits us to the scope, but never
  // touch anything outside the base path (e.g. ROMulator2K's own routes).
  if (!reqUrl.pathname.startsWith(scopePath)) return;

  // <base>/api/* is dynamic — including /api/stream, the EventSource SSE
  // channel. Network-only passthrough; never cache, never intercept.
  if (reqUrl.pathname.startsWith(API_PREFIX)) return;

  // App shell + static assets: stale-while-revalidate.
  if (reqUrl.pathname.startsWith(STATIC_PREFIX) || reqUrl.pathname === scopePath) {
    event.respondWith(staleWhileRevalidate(request));
  }

  // Anything else: default browser handling (network).
});

/* ---------------------------------------------------------------------- push */
self.addEventListener('push', (event) => {
  event.waitUntil(showPushNotification(event));
});

/** Display a notification, skipping it when the user is already looking at
 *  an open, focused window of this app. This is the single source of truth
 *  for notifications — the page does NOT create Notifications itself — so a
 *  focused window suppresses the alert instead of double-notifying.
 *
 *  Expected payload shape: { title, body, tag, icon, data }
 *  Tolerates missing, non-JSON or plain-text payloads. */
async function showPushNotification(event) {
  const payload = {
    title: 'ngrok-chat',
    body: '',
    tag: 'chat',
    icon: null,
    data: null,
  };

  if (event.data) {
    try {
      const parsed = event.data.json();
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        Object.assign(payload, parsed);
      } else {
        payload.body = String(parsed);
      }
    } catch (err) {
      // Not JSON — fall back to the raw text as the body.
      try {
        payload.body = event.data.text();
      } catch (err2) {
        /* nothing usable; show the bare title */
      }
    }
  }

  // If any window of this app is focused the user is already reading it —
  // don't interrupt them with a notification.
  const windows = await self.clients.matchAll({
    type: 'window',
    includeUncontrolled: true,
  });
  if (windows.some((client) => client.focused)) return;

  await self.registration.showNotification(payload.title, {
    body: payload.body,
    tag: payload.tag,
    icon: payload.icon || url('static/icons/icon-192.png'),
    badge: url('static/icons/icon-192.png'),
    data: payload.data,
    renotify: true,
    requireInteraction: false,
  });
}

/* --------------------------------------------------------- notificationclick */
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  // The server attaches a scope-relative url (e.g. "#general"); resolve it
  // against the base so a click lands on <base>/#general, not the origin root.
  const raw =
    (event.notification.data && event.notification.data.url) || '';
  let targetUrl;
  try {
    targetUrl = new URL(raw || './', SCOPE).href;
  } catch (err) {
    targetUrl = SCOPE.href;
  }

  event.waitUntil(
    self.clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then((clientList) => {
        // Reuse an already-open window if we can find one.
        for (const client of clientList) {
          if (!client.focus) continue;
          client.focus();
          if (client.navigate) return client.navigate(targetUrl);
          return undefined; // focus-only is the best we can do here
        }
        // No window open — launch one.
        return self.clients.openWindow(targetUrl);
      })
  );
});

/* ------------------------------------------------------- pushsubscriptionchange */
self.addEventListener('pushsubscriptionchange', (event) => {
  // Best-effort stub: a real re-subscribe needs the app's VAPID key and a
  // server round-trip, which the client JS owns. Acknowledge the event so
  // the browser doesn't treat it as failed — and so we don't crash.
  event.waitUntil(Promise.resolve());
});
