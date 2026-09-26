/* 风眼 TYPHOONWATCH service worker
   Bump CACHE_VERSION on every release; it must match VERSION in index.html. */
var CACHE_VERSION = "twatch-v3.5.0";
var SHELL = ["./", "./index.html", "./manifest.json", "./icon.svg"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE_VERSION).then(function (c) { return c.addAll(SHELL); }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (k) {
      return k === CACHE_VERSION ? null : caches.delete(k);
    }));
  }).then(function () { return self.clients.claim(); }));
});

/* The page asks for data/typhoon.json?t=<now> so no HTTP cache can hand it an
   old copy. Caching under that exact URL meant every refresh stored a new
   entry that no later request could ever match — offline, both the ?t= fetch
   and the plain fallback got 503 despite a cached copy being there. So the
   cache key is the path WITHOUT the query: one entry, always the newest. */
function stableKey(req) {
  var u = new URL(req.url);
  return u.origin + u.pathname;
}

/* A copy served from the cache is marked, so the page can say "offline:
   cached copy" instead of presenting it as a fresh fetch. */
function marked(hit) {
  var h = new Headers(hit.headers);
  h.set("X-TW-Cache", "1");
  return hit.blob().then(function (b) {
    return new Response(b, { status: hit.status, statusText: hit.statusText, headers: h });
  });
}

function networkFirst(req, timeoutMs, mark) {
  var key = stableKey(req);
  return caches.open(CACHE_VERSION).then(function (cache) {
    function fromCache() {
      return cache.match(key).then(function (hit) {
        if (!hit) return null;
        return mark ? marked(hit) : hit;
      });
    }
    return new Promise(function (resolve) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        fromCache().then(function (hit) { if (hit && !settled) { settled = true; resolve(hit); } });
      }, timeoutMs);
      fetch(req).then(function (res) {
        clearTimeout(timer);
        if (res && res.ok) cache.put(key, res.clone());
        if (!settled) { settled = true; resolve(res); }
      }).catch(function () {
        clearTimeout(timer);
        fromCache().then(function (hit) {
          if (settled) return;
          settled = true;
          resolve(hit || new Response("offline", { status: 503 }));
        });
      });
    });
  });
}

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // App shell: network-first with a 2s race, so a new build is used on the
  // launch it arrives, not the one after.
  if (req.mode === "navigate" || url.pathname.endsWith("/index.html")) {
    e.respondWith(networkFirst(req, 2000, false));
    return;
  }
  // Warning data: always try the network first — a cached bulletin that
  // silently stands in for a fresh one is exactly the failure to avoid.
  if (url.pathname.indexOf("/data/") !== -1) {
    e.respondWith(networkFirst(req, 4000, true));
    return;
  }
  // Everything else: cache, then refresh in the background.
  e.respondWith(caches.open(CACHE_VERSION).then(function (cache) {
    return cache.match(req).then(function (hit) {
      var net = fetch(req).then(function (res) {
        if (res && res.ok) cache.put(req, res.clone());
        return res;
      }).catch(function () { return hit; });
      return hit || net;
    });
  }));
});
