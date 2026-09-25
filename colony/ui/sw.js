/* Colony Dash. The service worker, which deliberately caches almost nothing.
 *
 * A service worker exists here for one reason: a browser will not offer to
 * install a page to the home screen without one. It is not here to make the
 * dashboard work offline, and it must not try.
 *
 * Everything on this page is a live read of a SQLite file on a machine that is
 * either awake and reachable or not. A cached `/api/state` is a board showing
 * yesterday's tickets with today's confidence, and a cached `app.js` is a
 * change that does not show until a hard refresh. So every request goes to
 * the network, every time.
 *
 * The one thing it does add is a better failure. When the desktop is asleep and
 * you open the icon on your phone, the browser's own offline page tells you the
 * site is down; this tells you the colony is unreachable, which is the true and
 * useful version of the same sentence.
 */

const VERSION = "colony-1";
const SHELL = "colony-shell-" + VERSION;

// Only the pieces needed to paint the "cannot reach the colony" page. The app
// itself is never served from here.
const OFFLINE_HTML = `<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Colony Dash, offline</title>
<style>
  html { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0B0F14; color: #E6EDF3; text-align: center; padding: 24px;
         font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  h1 { font-size: 17px; letter-spacing: .04em; text-transform: uppercase; margin: 0 0 10px; }
  p { margin: 0 0 18px; color: #8B99A8; max-width: 34ch; }
  button { font: inherit; color: inherit; background: transparent; cursor: pointer;
           border: 1px solid #2A3441; border-radius: 8px; padding: 10px 18px; }
</style>
<h1>no answer from the colony</h1>
<p>The dashboard is served from your desktop. If that machine is asleep, off, or
off the tailnet, there is nothing here to reach. This page is the only part
that lives on the phone.</p>
<button onclick="location.reload()">try again</button>`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then((c) => c.put("/__offline",
        new Response(OFFLINE_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } })))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  // Navigations get the offline page as a fallback. Everything else, the API,
  // the script, the stylesheet, the event stream, is allowed to fail the way
  // it would with no service worker at all, because the page already knows how
  // to say "the ledger did not answer".
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/__offline")),
    );
  }
});
