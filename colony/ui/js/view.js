// Which panels and details the page shows, kept per browser.

import { $, store } from "./core.js";
import { STATE, render } from "./render.js";
import { openNewStory } from "./composer.js";

// ── what the page shows ─────────────────────────────────────────────────────
// The page remembers which sections you want. Three rules:
//
//   1. It is local. Hiding a panel does not stop the colony filling it.
//   2. Panels default to *on*, detail to *off*, so a new panel appears for
//      existing users.
//   3. Nothing hides silently: the detail toggles show a count when they hide
//      something.

export const VIEW_KEY = "colony-view";
export const PANEL_KEYS = ["sprint", "inbox", "flight", "ordis", "colony", "standby",
                    "board", "completed", "files", "spend", "macros", "pulse",
                    "forge"];
export let VIEW = {};
try { VIEW = JSON.parse(store.get(VIEW_KEY) || "{}") || {}; } catch (_) { VIEW = {}; }

export function shows(key) {
  return VIEW[key] === undefined ? PANEL_KEYS.includes(key) : !!VIEW[key];
}

export function applyView() {
  for (const key of PANEL_KEYS) {
    const node = document.querySelector('[data-panel="' + key + '"]');
    if (node) node.classList.toggle("hidden-by-view", !shows(key));
  }
  document.body.classList.toggle("dense", shows("dense"));
  for (const box of document.querySelectorAll("[data-view]")) box.checked = shows(box.dataset.view);
  $("inbox-show-stale").setAttribute("aria-pressed", String(shows("stale")));
  $("board-dropped").setAttribute("aria-pressed", String(shows("dropped")));
  $("board-filed").setAttribute("aria-pressed", String(shows("filed")));
}

export function setView(key, on) {
  VIEW[key] = !!on;
  store.set(VIEW_KEY, JSON.stringify(VIEW));
  applyView();
  if (STATE) render(STATE, true);   // stale and dropped change what the lists contain
}

for (const box of document.querySelectorAll("[data-view]")) {
  box.onchange = () => setView(box.dataset.view, box.checked);
}
$("view-reset").onclick = () => {
  VIEW = {};
  store.remove(VIEW_KEY);
  applyView();
  if (STATE) render(STATE, true);
};
$("inbox-show-stale").onclick = () => setView("stale", !shows("stale"));
$("board-new").onclick = openNewStory;
$("board-dropped").onclick = () => setView("dropped", !shows("dropped"));
$("board-filed").onclick = () => setView("filed", !shows("filed"));

// Click-away close for every header dropdown; <details> has none.
document.addEventListener("click", (ev) => {
  document.querySelectorAll("details.viewmenu[open]").forEach((menu) => {
    if (!menu.contains(ev.target)) menu.open = false;
  });
});

applyView();
