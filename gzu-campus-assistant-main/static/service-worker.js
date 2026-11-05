
/* GZU Campus Assistant service worker.
   Static assets may be cached; chat POST requests are always sent to the server. */

const CACHE_NAME = "gzu-campus-assistant-v2";

const APP_SHELL = [
  "/",
  "/manifest.json",
  "/static/offline.html",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .catch(error => console.warn("GZU app-shell cache could not be fully populated:", error))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key.startsWith("gzu-campus-assistant-") && key !== CACHE_NAME)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  // Never intercept chat submissions or other non-GET requests.
  // The chatbot still needs the server to generate answers.
  if (
    request.method !== "GET" ||
    url.origin !== self.location.origin ||
    url.pathname === "/chat" ||
    url.pathname === "/health"
  ) {
    return;
  }

  // Try the network first for pages.
  // If offline, show the cached homepage or the offline page.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then(response => {
          if (response && response.ok) {
            const copy = response.clone();

            caches.open(CACHE_NAME)
              .then(cache => cache.put("/", copy))
              .catch(error => console.warn("Could not cache the homepage:", error));
          }

          return response;
        })
        .catch(() =>
          caches.match(request)
            .then(cached => cached || caches.match("/"))
            .then(cached => cached || caches.match("/static/offline.html"))
        )
    );

    return;
  }

  // Use cached static files when available.
  // Cache successfully retrieved static assets for future visits.
  event.respondWith(
    caches.match(request).then(cached => {
      if (cached) {
        return cached;
      }

      return fetch(request).then(response => {
        if (response && response.ok && url.pathname.startsWith("/static/")) {
          const copy = response.clone();

          caches.open(CACHE_NAME)
            .then(cache => cache.put(request, copy))
            .catch(error => console.warn("Could not cache a static asset:", error));
        }

        return response;
      });
    })
  );
});
