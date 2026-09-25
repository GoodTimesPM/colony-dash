// Colony Dash. No build step, no framework: the page reads `/api/state` and
// paints. Every write goes back through an `/api/...` POST with the
// `X-Colony` header; nothing here decides anything.
//
// Entry point: loads every module in order, then starts the live feed.

import "./core.js";
import "./avatars.js";
import "./render.js";
import "./panels.js";
import "./composer.js";
import "./spend.js";
import "./roster.js";
import "./drawer.js";
import "./console.js";
import "./drawers.js";
import "./details.js";
import "./appearance.js";
import "./manual.js";
import "./phone.js";
import "./view.js";

import { $, onState } from "./core.js";
import { render, renderTree, setTreeFilter } from "./render.js";
import { setAllProjects } from "./drawer.js";
import { openPersona, openStory } from "./drawers.js";
import { openAgent, openProject, openPulse } from "./details.js";
import { paintThemeColor } from "./appearance.js";

// ── live feed ───────────────────────────────────────────────────────────────

function setConn(live, text) {
  $("conn").dataset.live = String(live);
  $("conn-text").textContent = text;
}

let es = null;
onState(render);

function connect() {
  es = new EventSource("/events");
  es.addEventListener("state", (ev) => { setConn(true, "live"); render(JSON.parse(ev.data)); });
  es.onopen = () => setConn(true, "live");
  es.onerror = () => {
    // Numbers on screen are now of unknown age. Say so rather than let a frozen
    // page look like a quiet one.
    setConn(false, "disconnected, retrying");
    es.close();
    setTimeout(connect, 3000);
  };
}

fetch("/api/projects").then((r) => r.json()).then((p) => { setAllProjects(p.all || []); }).catch(() => {});

// The tree is a filesystem read (`git status` plus a listing), not on the SSE
// feed. It loads once and refreshes on request.
renderTree();
$("tree-refresh").onclick = () => renderTree();
$("tree-q").addEventListener("input", (e) => {
  setTreeFilter(e.target.value.trim());
  renderTree();
});
fetch("/api/state").then((r) => r.json()).then(render).catch(() => setConn(false, "ledger unreachable"));

paintThemeColor();

// The server redirects `?k=` and `?pair=` away before the page loads. A
// loopback visit skips that redirect, so strip them here too.
{
  const url = new URL(location.href);
  if (url.searchParams.has("k") || url.searchParams.has("pair")) {
    url.searchParams.delete("k");
    url.searchParams.delete("pair");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
}

// The service worker exists only so a phone offers to install the page; it
// caches an offline notice (see sw.js). Registered last and failing silently.
// Inert in the pywebview window.
if ("serviceWorker" in navigator && location.protocol !== "file:") {
  addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

// ?nostream skips the live feed and leaves one static frame, so a headless
// browser can settle for screenshots and debugging.
if (location.search.includes("nostream")) setConn(false, "static snapshot");
else connect();

// ?open=persona:engineering/python-pro. Every drawer is addressable by link.
const deep = new URLSearchParams(location.search).get("open");
if (deep) {
  const [kind, ...rest] = deep.split(":");
  const id = rest.join(":");
  const open = { story: openStory, persona: openPersona, pulse: openPulse,
                 project: openProject, agent: openAgent }[kind];
  if (open) open(kind === "story" || kind === "pulse" || kind === "agent" ? Number(id) : id);
}
