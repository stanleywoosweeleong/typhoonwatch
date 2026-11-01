/* 风眼 TYPHOONWATCH service worker

   No version to bump. Earlier builds named the cache after the release
   ("twatch-v3.5.1") and every release had to bump it by hand to match
   VERSION in index.html; forgetting kept phones on the old shell. Now:

   - One cache with a fixed name. Every shell file and every data file is
     fetched network-first, and each good response overwrites its entry, so
     the cache always holds the newest copy the phone has seen.
   - Whether index.html changed is decided by the CONTENT (a SHA-256 digest),
     not by a name. The page asks for that digest and is told when the copy
     on the server differs from the one it is running ("Update ready").
   - This file itself is byte-compared by the browser on every update check,
     so editing it still installs a new worker; skipWaiting() makes that take
     effect on the next reload instead of after every tab is closed. That is
     safe because old and new workers share the same cache and rules. */
var CACHE = "twatch-cache";
var SHELL = ["./", "./index.html", "./manifest.json", "./icon.svg"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) {
    // cache:"reload" so a stale HTTP-cached copy is never precached.
    return Promise.all(SHELL.map(function (u) {
      return fetch(new Request(u, { cache: "reload" })).then(function (res) {
        if (res.ok) return c.put(stableKey(new Request(u)), res);
      }).catch(function () {});
    }));
  }).then(function () { return self.skipWaiting(); }));
});

/* Cache storage is shared by every app on this GitHub Pages origin, so only
   our own caches are touched: the old versioned ones (twatch-v*) go. */
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (k) {
      return (k.indexOf("twatch-") === 0 && k !== CACHE) ? caches.delete(k) : null;
    }));
  }).then(function () { return self.clients.claim(); }));
});

/* The page asks for data/typhoon.json?t=<now> so no HTTP cache can hand it an
   old copy. Caching under that exact URL meant every refresh stored a new
   entry that no later request could ever match — offline, both the ?t= fetch
   and the plain fallback got 503 despite a cached copy being there. So the
   cache key is the path WITHOUT the query: one entry, always the newest.
   "./" and "./index.html" are the same page, so they share one key too. */
function stableKey(req) {
  var u = new URL(req.url, self.location.href);
  var p = u.pathname;
  if (p.charAt(p.length - 1) === "/") p += "index.html";
  return u.origin + p;
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

function digestOf(res) {
  return res.clone().arrayBuffer().then(function (buf) {
    return crypto.subtle.digest("SHA-256", buf);
  }).then(function (d) {
    return Array.prototype.map.call(new Uint8Array(d), function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
  });
}

/* Which index.html each open page is running, by its digest. Filled when a
   navigation is answered; the page asks for it right after it loads, while
   this worker is certainly still awake. */
var served = {};
function noteServed(clientId, res) {
  if (!clientId || !res) return;
  digestOf(res).then(function (d) { served[clientId] = d; }).catch(function () {});
}

function networkFirst(req, timeoutMs, mark, clientId) {
  var key = stableKey(req);
  return caches.open(CACHE).then(function (cache) {
    function fromCache() {
      return cache.match(key).then(function (hit) {
        if (!hit) return null;
        return mark ? marked(hit) : hit;
      });
    }
    return new Promise(function (resolve) {
      var settled = false;
      function done(res) {
        if (settled || !res) return;
        settled = true;
        noteServed(clientId, res);
        resolve(res);
      }
      var timer = setTimeout(function () {
        if (!settled) fromCache().then(done);
      }, timeoutMs);
      fetch(req).then(function (res) {
        clearTimeout(timer);
        if (res && res.ok) {
          cache.put(key, res.clone());
          done(res);
          return;
        }
        // A 4xx/5xx (e.g. a Pages 503) is no better than being offline:
        // prefer the cached copy, and pass the error through only if none.
        fromCache().then(function (hit) { done(hit || res); });
      }).catch(function () {
        clearTimeout(timer);
        fromCache().then(function (hit) {
          done(hit || new Response("offline", { status: 503 }));
        });
      });
    });
  });
}

/* "Is there a newer index.html than the one I am running?" Fetches the page
   from the network (bypassing the HTTP cache), refreshes the cache with it,
   and compares digests. Offline or on error the answer is simply "no". */
function checkShell(running) {
  var key = stableKey(new Request("./index.html"));
  return fetch(new Request("./index.html", { cache: "no-cache" })).then(function (res) {
    if (!res.ok) return false;
    return caches.open(CACHE).then(function (c) { return c.put(key, res.clone()); })
      .then(function () { return digestOf(res); })
      .then(function (d) { return !!running && d !== running; });
  }).catch(function () { return false; });
}

self.addEventListener("message", function (e) {
  var m = e.data || {}, port = e.ports && e.ports[0];
  if (!port) return;
  var id = e.source && e.source.id;
  if (m.type === "shell-digest") {
    // Fallback for a worker that restarted since serving the page: the
    // cached copy is what the last navigation got in the common case.
    if (served[id]) { port.postMessage({ digest: served[id] }); return; }
    caches.open(CACHE).then(function (c) {
      return c.match(stableKey(new Request("./index.html")));
    }).then(function (hit) { return hit ? digestOf(hit) : null; })
      .then(function (d) { port.postMessage({ digest: d }); })
      .catch(function () { port.postMessage({ digest: null }); });
  } else if (m.type === "shell-check") {
    e.waitUntil(checkShell(m.digest).then(function (newer) {
      port.postMessage({ newer: newer });
    }));
  }
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // App shell: network-first with a 2s race, so a new build is used on the
  // launch it arrives, not the one after.
  if (req.mode === "navigate" || url.pathname.endsWith("/index.html")) {
    e.respondWith(networkFirst(req, 2000, false,
      req.mode === "navigate" ? (e.resultingClientId || e.clientId) : null));
    return;
  }
  // Warning data: always try the network first — a cached bulletin that
  // silently stands in for a fresh one is exactly the failure to avoid.
  if (url.pathname.indexOf("/data/") !== -1) {
    e.respondWith(networkFirst(req, 4000, true));
    return;
  }
  // Everything else (manifest, icon): network-first too, so no file of the
  // shell can lag behind; the cache only answers when the network cannot.
  e.respondWith(networkFirst(req, 3000, false));
});
