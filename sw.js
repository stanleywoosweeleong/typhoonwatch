/* 风眼 TYPHOONWATCH service worker
   Bump CACHE_VERSION on every release; it must match VERSION in index.html. */
var CACHE_VERSION = "twatch-v3.1.15";
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

function networkFirst(req, timeoutMs) {
  return caches.open(CACHE_VERSION).then(function (cache) {
    return new Promise(function (resolve) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        cache.match(req).then(function (hit) { if (hit && !settled) { settled = true; resolve(hit); } });
      }, timeoutMs);
      fetch(req).then(function (res) {
        clearTimeout(timer);
        if (res && res.ok) cache.put(req, res.clone());
        if (!settled) { settled = true; resolve(res); }
      }).catch(function () {
        clearTimeout(timer);
        cache.match(req).then(function (hit) {
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
    e.respondWith(networkFirst(req, 2000));
    return;
  }
  // Warning data: always try the network first — a cached bulletin that
  // silently stands in for a fresh one is exactly the failure to avoid.
  if (url.pathname.indexOf("/data/") !== -1) {
    e.respondWith(networkFirst(req, 4000));
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
