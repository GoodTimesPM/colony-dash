// Theme, text size, hand-mixed colours, tile layout and the appearance
// drawer.

import { $, act, el, empty, store, toast } from "./core.js";
import { STATE, render } from "./render.js";
import { blk, closeDrawer, openDrawer } from "./drawer.js";

// ── theme ───────────────────────────────────────────────────────────────────

// `?theme=ember` overrides the stored choice for this load only, for inspection
// and headless screenshots.
export const forced = new URLSearchParams(location.search).get("theme");
export let saved = forced || store.get("colony-theme") || "";
// A retired theme name in localStorage falls back to system.
if (saved && !$("theme").querySelector(`option[value="${CSS.escape(saved)}"]`)) {
  store.remove("colony-theme");
  saved = "";
}
// A phone paints its status bar with `theme-color`, so it is read off the
// computed style for every theme and hand-mixed ground.
export function paintThemeColor() {
  const meta = $("theme-color");
  if (!meta) return;
  const bg = getComputedStyle(document.documentElement)
    .getPropertyValue("--ground").trim();
  if (bg) meta.setAttribute("content", bg);
}

if (saved) document.documentElement.dataset.theme = saved;
else delete document.documentElement.dataset.theme;   // boot.js may have set a retired one
$("theme").value = saved;
$("theme").onchange = (e) => {
  const v = e.target.value;
  if (v) { document.documentElement.dataset.theme = v; store.set("colony-theme", v); }
  else { delete document.documentElement.dataset.theme; store.remove("colony-theme"); }
  paintThemeColor();
  // Picking a theme drops hand-mixed colours, which belonged to the old
  // palette. Named presets are untouched.
  if (Object.keys(VARS).length) { VARS = {}; applyVars(); saveAppearance();
                                  toast("hand-mixed colors cleared", null); }
  if (STATE) render(STATE, true);   // avatars are painted on canvas, so they re-paint
};
$("halt-banner-resume").onclick = () => act("halt", { on: false });

// ── appearance ──────────────────────────────────────────────────────────────
// Text size, palette and tile layout, all in localStorage; nothing leaves the
// machine. Every contrast relationship shows its ratio beside the swatch and
// turns coral when it fails. Randomize follows the same audit.

export const SCALE_KEY = "colony-scale", VARS_KEY = "colony-vars",
      PRESET_KEY = "colony-presets", LAYOUT_KEY = "colony-layout";

// The shipped default is 1.15 and not 1: at the design size the body step is
// 13px, which is right for a wallboard and small for a page you actually read.
export const DEFAULT_SCALE = 1.15;

export function readJSON(key, fallback) {
  try { return JSON.parse(store.get(key) || "null") || fallback; }
  catch (_) { return fallback; }
}

export let SCALE = Number(store.get(SCALE_KEY) || DEFAULT_SCALE) || DEFAULT_SCALE;
export let VARS = readJSON(VARS_KEY, {});
export let LAYOUT = readJSON(LAYOUT_KEY, {});

// Name, label, what it paints, and what it must stay legible against. A row
// with no audit is a surface colour.
//
// Targets: ink 4.5 on panel, everything else 3, measured from the shipped
// palettes. Every row says what it paints, since `violet` alone drives Ordis,
// two panel titles, the focus ring and a bar. The four accents are the sources;
// downstream tokens can be pinned separately.
export const TOKEN_GROUPS = [
  { label: "surfaces",
    note: "The page and the cards on it. No contrast target of their own. They are what everything else is measured against.",
    rows: [
      ["--ground",     "ground",     "behind the whole page",            null],
      ["--panel",      "panel",      "the face of every card and panel", null],
      ["--panel-sunk", "well",       "hovered rows, inset boxes, gauges", null],
      ["--rule",       "rule",       "borders and dividers",             null],
      ["--rule-soft",  "rule soft",  "the faintest dividers",            null],
    ] },
  { label: "text",
    note: "Every word on the page sits on panel, so that is what all three are audited against.",
    rows: [
      ["--ink",      "ink",      "body text and headlines",        ["--panel", 4.5]],
      ["--ink-soft", "ink soft", "secondary text, counts, labels", ["--panel", 4.5]],
      ["--ink-dim",  "ink dim",  "timestamps, hints, empty states", ["--panel", 3]],
    ] },
  { label: "the four meanings",
    note: "The accents, and the source of everything below. Each one says one thing everywhere it appears, which is why there are four and not fourteen. Change one here and every title and bar that borrows it follows. Pin those separately below if you would rather they did not.",
    rows: [
      ["--amber",        "amber",     "yours: Inbox tiles, anything waiting on you", ["--panel", 3]],
      ["--amber-ground", "amber bed", "the bed an Inbox tile sits on",               ["--amber", 3]],
      ["--mint",         "mint",      "live work: running, healthy, go",             ["--panel", 3]],
      ["--mint-ground",  "mint bed",  "the bed a mint chip sits on",                 ["--mint", 3]],
      ["--coral",        "coral",     "anomaly: blocked, failed, halted, stop",      ["--panel", 3]],
      ["--coral-ground", "coral bed", "the bed a coral chip sits on",                ["--coral", 3]],
      ["--violet",       "violet",    "Ordis themselves, and every focus ring",         ["--panel", 3]],
    ] },
  { label: "panel titles",
    note: "One per panel, mixed from the accents above. Set one here and only that title moves.",
    rows: [
      ["--h-inbox",   "PO Inbox",    "title and left edge", ["--panel", 3]],
      ["--h-flight",  "Ticket Queue", "title, queued-push stripe, the link to a tile", ["--panel", 3]],
      ["--h-ordis",   "Ordis",       "title and left edge", ["--panel", 3]],
      ["--h-colony",  "Colony",      "title and left edge", ["--panel", 3]],
      ["--h-standby", "Standby",     "title and left edge", ["--panel", 3]],
      ["--h-board",   "Board",       "title and left edge", ["--panel", 3]],
      ["--h-completed", "Completed", "title and left edge", ["--panel", 3]],
      ["--h-files",   "Files",       "title and left edge", ["--panel", 3]],
      ["--h-spend",   "Spend",       "title and left edge", ["--panel", 3]],
      ["--h-macros",  "Macros",      "title and left edge", ["--panel", 3]],
      ["--h-pulse",   "Pulse log",   "title and left edge", ["--panel", 3]],
      ["--h-forge",   "Forge",       "title and left edge", ["--panel", 3]],
      ["--h-sprint",  "Sprint strip", "the figures across the top", ["--panel", 3]],
    ] },
  { label: "file changes",
    note: "The stacked bar on a project row, and the dot beside a file in the tree. The same four states in both places.",
    rows: [
      ["--git-added",     "added",     "new files",              ["--panel", 3]],
      ["--git-modified",  "modified",  "changed files",          ["--panel", 3]],
      ["--git-deleted",   "deleted",   "removed files",          ["--panel", 3]],
      ["--git-untracked", "untracked", "files git has not seen", ["--panel", 3]],
    ] },
];

// The base is what randomize rolls and what a preset stores whole; everything
// after it is derived from the base unless it has been pinned by hand.
export const BASE_TOKENS = TOKEN_GROUPS.slice(0, 3).reduce((a, g) => a.concat(g.rows.map((r) => r[0])), []);
export const TOKENS = TOKEN_GROUPS.reduce((a, g) => a.concat(g.rows), []);

// ── colour arithmetic ───────────────────────────────────────────────────────

export function hex2rgb(h) {
  h = String(h).trim().replace("#", "");
  if (h.length === 3) h = [...h].map((c) => c + c).join("");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) || 0);
}
export function rgb2hex(r, g, b) {
  return "#" + [r, g, b].map((v) =>
    Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, "0")).join("");
}
export function hsl2hex(h, s, l) {
  h = ((h % 360) + 360) % 360;
  s = Math.min(100, Math.max(0, s)) / 100;
  l = Math.min(100, Math.max(0, l)) / 100;
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return rgb2hex(f(0) * 255, f(8) * 255, f(4) * 255);
}
export function lum(hex) {
  const [r, g, b] = hex2rgb(hex).map((v) => {
    v /= 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function contrast(a, b) {
  const x = lum(a), y = lum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
// Walk lightness away from the background until the ratio clears. The hue is
// chosen for character; the lightness is whatever legibility needs.
export function toward(h, s, l, bg, target) {
  const dir = lum(hsl2hex(h, s, l)) > lum(bg) ? 1 : -1;
  for (let i = 0; i < 120 && contrast(hsl2hex(h, s, l), bg) < target; i++) l += dir;
  return hsl2hex(h, s, Math.min(100, Math.max(0, l)));
}

// ── applying ────────────────────────────────────────────────────────────────

// A custom property reads back as text, often a `color-mix(...)` recipe, so a
// zero-sized probe resolves it. Chromium returns `rgb(...)` (0 to 255) for a
// plain colour and `color(srgb ...)` (0 to 1) for a mix; both are parsed.
export const PROBE = document.createElement("span");
PROBE.style.cssText = "position:absolute;width:0;height:0;opacity:0;pointer-events:none";
document.body.append(PROBE);

export function parseColor(text) {
  const nums = String(text).match(/-?[\d.]+(?:e-?\d+)?/g);
  if (!nums || nums.length < 3) return null;
  const v = nums.slice(0, 3).map(Number);
  return String(text).indexOf("srgb") >= 0 ? v.map((n) => n * 255) : v;
}

export function tokenValue(name) {
  if (VARS[name]) return VARS[name];
  PROBE.style.color = "var(" + name + ")";
  const rgb = parseColor(getComputedStyle(PROBE).color);
  return rgb ? rgb2hex(rgb[0], rgb[1], rgb[2]) : "#808080";
}

export function applyScale() {
  document.documentElement.style.setProperty("--ui-scale", String(SCALE));
}

export function applyVars() {
  const root = document.documentElement.style;
  for (const pair of TOKENS) root.removeProperty(pair[0]);
  for (const name of Object.keys(VARS)) root.setProperty(name, VARS[name]);
  paintThemeColor();
}

export function saveAppearance() {
  store.set(SCALE_KEY, String(SCALE));
  store.set(VARS_KEY, JSON.stringify(VARS));
  store.set(LAYOUT_KEY, JSON.stringify(LAYOUT));
}

// ── tile layout ─────────────────────────────────────────────────────────────

export const MAIN_PANELS = ["ordis", "colony", "standby", "board", "completed", "files",
                     "spend", "macros", "pulse", "forge"];
// The sprint strip is one row of figures with no heading of its own; capping it
// would put a scrollbar on something already one line tall.
export const CAPPABLE = ["inbox", "flight"].concat(MAIN_PANELS);

// Panels whose *contents* scroll rather than the panel itself, and the variable
// their ceiling lives in. Their heading stays put without needing sticky.
export const SELF_SCROLL = { "inbox": "--inbox-max", "pulse": "--pulse-max",
                      "completed": "--done-max" };
export const PANEL_LABEL = {
  inbox: "PO Inbox", flight: "Ticket Queue", ordis: "Ordis", colony: "Colony",
  standby: "Standby", board: "Board", completed: "Completed", files: "Files",
  spend: "Spend",
  macros: "Macros", pulse: "Pulse log", forge: "Forge",
};
export const COL_LABEL = ["left", "middle", "right"];

// Read from the markup before anything moves, so "reset layout" restores the
// page as built.
export const HOME = {};
for (const [ci, col] of [...document.querySelectorAll("main > .col")].entries()) {
  for (const [i, node] of [...col.children].entries()) {
    if (node.dataset.panel) HOME[node.dataset.panel] = { col: ci, i: i };
  }
}

export function panelNode(key) { return document.querySelector('[data-panel="' + key + '"]'); }

export function applyLayout() {
  const cols = [...document.querySelectorAll("main > .col")];
  if (!cols.length) return;
  const want = MAIN_PANELS.map((key) => {
    const home = HOME[key] || { col: 1, i: 99 };
    const set = LAYOUT[key] || {};
    return {
      key: key, node: panelNode(key),
      col: Math.min(cols.length - 1, Math.max(0, set.col === undefined ? home.col : set.col)),
      i: set.i === undefined ? home.i : set.i,
    };
  }).sort((a, b) => a.col - b.col || a.i - b.i);

  for (const p of want) if (p.node) cols[p.col].append(p.node);

  captureLayout();

  for (const key of CAPPABLE) {
    const node = panelNode(key);
    if (!node) continue;
    // Folded is a layout fact, not a session one: it lives in LAYOUT beside
    // the column and the cap, so a tile you put away is still away tomorrow.
    node.classList.toggle("min", !!(LAYOUT[key] || {}).min);
    const head = node.querySelector(":scope > h2");
    if (head) head.setAttribute("aria-expanded", String(!(LAYOUT[key] || {}).min));
    const max = (LAYOUT[key] || {}).max || 0;
    // The Inbox grid and pulse log scroll their own bodies, so their cap
    // applies inside, avoiding nested scrollbars.
    if (SELF_SCROLL[key]) {
      if (max) node.style.setProperty(SELF_SCROLL[key], max + "px");
      else node.style.removeProperty(SELF_SCROLL[key]);
      continue;
    }
    node.classList.toggle("capped", max > 0);
    if (max) node.style.setProperty("--tile-max", max + "px");
    else node.style.removeProperty("--tile-max");
  }
}

// Read the order back from the DOM and store that, so indices stay dense and a
// move is always a swap with a neighbour.
export function captureLayout() {
  for (const [ci, col] of [...document.querySelectorAll("main > .col")].entries()) {
    let i = 0;
    for (const node of col.children) {
      const key = node.dataset && node.dataset.panel;
      if (!key) continue;
      LAYOUT[key] = Object.assign({}, LAYOUT[key], { col: ci, i: i++ });
    }
  }
}

export function foldTile(key, on) {
  LAYOUT[key] = Object.assign({}, LAYOUT[key], { min: on });
  applyLayout(); saveAppearance();
}

// Clicking a title bar folds the tile. Not for buttons inside some headings,
// nor in snap mode, where the title is the drag handle.
for (const key of CAPPABLE) {
  const node = panelNode(key);
  const h = node && node.querySelector(":scope > h2");
  if (!h) continue;
  h.title = "click to fold this away";
  h.tabIndex = 0;
  h.setAttribute("role", "button");
  h.addEventListener("click", (ev) => {
    if (SNAP || ev.target.closest("button, input, a")) return;
    foldTile(key, !node.classList.contains("min"));
  });
  h.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    if (SNAP || ev.target.closest("button, input, a")) return;
    ev.preventDefault();
    foldTile(key, !node.classList.contains("min"));
  });
}

export function moveTile(key, delta) {
  const col = (LAYOUT[key] || {}).col;
  const siblings = MAIN_PANELS
    .filter((k) => (LAYOUT[k] || {}).col === col)
    .sort((a, b) => (LAYOUT[a] || {}).i - (LAYOUT[b] || {}).i);
  const at = siblings.indexOf(key), to = at + delta;
  if (at < 0 || to < 0 || to >= siblings.length) return;
  const other = siblings[to], mine = LAYOUT[key].i;
  LAYOUT[key] = Object.assign({}, LAYOUT[key], { i: LAYOUT[other].i });
  LAYOUT[other] = Object.assign({}, LAYOUT[other], { i: mine });
  applyLayout(); saveAppearance(); openAppearance();
}

// ── snap ────────────────────────────────────────────────────────────────────
// Dragging is a mode you enter from the drawer and leave by the bar at the
// bottom; tiles wiggle while it is on. A drop inserts rather than swaps, so no
// second tile moves unasked.

export let SNAP = false, DRAG = null, LINE = null;

export function setSnap(on) {
  SNAP = on;
  document.body.classList.toggle("snapping", on);
  for (const key of MAIN_PANELS) {
    const node = panelNode(key);
    if (node) node.draggable = on;
  }
  if (!on) endDrag();
}

export function endDrag() {
  if (LINE && LINE.parentNode) LINE.remove();
  if (DRAG) DRAG.classList.remove("dragging");
  DRAG = null;
}

// Measured against tile middles, so the insert line flips halfway past a tile.
export function insertBefore(col, y) {
  for (const node of col.children) {
    if (node === DRAG || node === LINE || !node.dataset.panel) continue;
    if (node.offsetParent === null) continue;         // hidden by the view menu
    const box = node.getBoundingClientRect();
    if (y < box.top + box.height / 2) return node;
  }
  return null;
}

(function wireSnap() {
  const main = document.querySelector("main");
  if (!main) return;

  main.addEventListener("dragstart", (e) => {
    const node = e.target.closest ? e.target.closest("[data-panel]") : null;
    if (!SNAP || !node || MAIN_PANELS.indexOf(node.dataset.panel) < 0) return;
    DRAG = node;
    node.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    // Firefox will not start a drag without payload; the panel key is the
    // smallest true thing to say.
    e.dataTransfer.setData("text/plain", node.dataset.panel);
  });

  main.addEventListener("dragover", (e) => {
    if (!SNAP || !DRAG) return;
    const col = e.target.closest ? e.target.closest("main > .col") : null;
    if (!col) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (!LINE) LINE = el("div", "dropline");
    const before = insertBefore(col, e.clientY);
    if (before) col.insertBefore(LINE, before); else col.append(LINE);
  });

  main.addEventListener("drop", (e) => {
    if (!SNAP || !DRAG) return;
    e.preventDefault();
    if (LINE && LINE.parentNode) LINE.parentNode.insertBefore(DRAG, LINE);
    endDrag();
    captureLayout(); saveAppearance();
  });

  main.addEventListener("dragend", () => endDrag());
})();

$("snap-done").onclick = () => { setSnap(false); openAppearance(); };

// ── randomize ───────────────────────────────────────────────────────────────
// Not uniform dice, which is almost always unreadable and breaks the accents'
// meanings. Neutrals get a random hue, cast and dark or light ground; accents
// vary inside their hue bands; every lightness is solved for its contrast
// target.

export function randomPalette() {
  const rnd = (a, b) => a + Math.random() * (b - a);
  const hue = rnd(0, 360);
  const dark = Math.random() < 0.62;
  const cast = rnd(6, 26);                      // how far the neutrals lean on the hue

  const ground = hsl2hex(hue, cast, dark ? rnd(6, 13) : rnd(92, 96));
  const panel  = hsl2hex(hue, cast * 0.8, dark ? rnd(12, 18) : rnd(98, 100));
  const sunk   = hsl2hex(hue, cast * 0.8, dark ? rnd(17, 23) : rnd(93, 96));
  const rule   = hsl2hex(hue, cast * 0.7, dark ? rnd(24, 30) : rnd(85, 89));
  const soft   = hsl2hex(hue, cast * 0.7, dark ? rnd(19, 24) : rnd(90, 93));

  // The chip bed is chosen first. The other order fails on light themes: no bed
  // light enough for a near-white panel can also clear an accent that barely
  // clears the panel.
  const bed = (h, sat) => hsl2hex(h, sat, dark ? 15 : 95);
  // An accent has two jobs, a label on its own bed, a heading on the panel,
  // and its lightness is solved for the harder of the two.
  const accent = (h, sat, on) => {
    const dir = dark ? 1 : -1;
    let l = dark ? 55 : 52;
    for (let i = 0; i < 120 && l >= 0 && l <= 100; i++, l += dir) {
      const c = hsl2hex(h, sat, l);
      if (contrast(c, on) >= 4.0 && contrast(c, panel) >= 3.05) return c;
    }
    return hsl2hex(h, sat, Math.min(100, Math.max(0, l)));
  };

  const hA = rnd(24, 52), hM = rnd(130, 178), hC = rnd(352, 378), hV = rnd(252, 292);
  const sA = rnd(30, 60), sM = rnd(25, 55), sC = rnd(30, 60);
  const bedA = bed(hA, sA), bedM = bed(hM, sM), bedC = bed(hC, sC);
  const amber  = accent(hA, rnd(55, 92), bedA);
  const mint   = accent(hM, rnd(45, 85), bedM);
  const coral  = accent(hC, rnd(55, 90), bedC);
  const violet = accent(hV, rnd(40, 80), panel);

  return {
    "--ground": ground, "--panel": panel, "--panel-sunk": sunk,
    "--rule": rule, "--rule-soft": soft,
    "--ink":      toward(hue, cast * 0.35, dark ? 92 : 12, panel, 9),
    "--ink-soft": toward(hue, cast * 0.50, dark ? 68 : 34, panel, 4.6),
    "--ink-dim":  toward(hue, cast * 0.50, dark ? 48 : 52, panel, 3.05),
    "--amber": amber,  "--amber-ground": bedA,
    "--mint": mint,    "--mint-ground":  bedM,
    "--coral": coral,  "--coral-ground": bedC,
    "--violet": violet,
  };
}

// ── presets ─────────────────────────────────────────────────────────────────

export function presets() { return readJSON(PRESET_KEY, {}); }
export function writePresets(all) { store.set(PRESET_KEY, JSON.stringify(all)); }

// A preset is the whole look, colours and text size together.
export function savePreset(name) {
  name = (name || "").trim().slice(0, 40);
  if (!name) { toast("a preset needs a name", "bad"); return; }
  const all = presets();
  // Base tokens are stored resolved; derived tokens only if pinned by hand, so
  // moving `--violet` still moves what derives from it.
  const full = {};
  for (const name of BASE_TOKENS) full[name] = tokenValue(name);
  for (const name of Object.keys(VARS)) full[name] = VARS[name];
  all[name] = { theme: document.documentElement.dataset.theme || "", scale: SCALE, vars: full };
  writePresets(all);
  toast("saved " + name, "good");
  openAppearance();
}

export function applyPreset(name) {
  const p = presets()[name];
  if (!p) return;
  if (p.theme) {
    document.documentElement.dataset.theme = p.theme;
    store.set("colony-theme", p.theme);
  } else {
    delete document.documentElement.dataset.theme;
    store.remove("colony-theme");
  }
  $("theme").value = p.theme || "";
  SCALE = Number(p.scale) || DEFAULT_SCALE;
  VARS = Object.assign({}, p.vars || {});
  applyScale(); applyVars(); saveAppearance();
  if (STATE) render(STATE, true);
  openAppearance();
}

// ── the drawer ──────────────────────────────────────────────────────────────

export function openAppearance() {
  // No `nav` descriptor: this is a control panel, not a place, and a back
  // button that returned you to your own settings would be noise.
  const body = openDrawer("appearance", "Template Palette");
  body.replaceChildren();
  const set = el("div", "set");

  // ── text size
  const size = el("div", "row");
  const slider = document.createElement("input");
  slider.type = "range"; slider.min = "85"; slider.max = "175"; slider.step = "5";
  slider.value = String(Math.round(SCALE * 100));
  const val = el("span", "val", Math.round(SCALE * 100) + "%");
  slider.oninput = () => {
    SCALE = Number(slider.value) / 100;
    val.textContent = slider.value + "%";
    applyScale(); saveAppearance();
  };
  const sizeReset = el("button", "link", "reset");
  sizeReset.onclick = () => {
    SCALE = DEFAULT_SCALE;
    slider.value = String(Math.round(SCALE * 100));
    val.textContent = Math.round(SCALE * 100) + "%";
    applyScale(); saveAppearance();
  };
  size.append(slider, val, sizeReset);
  set.append(blk("text size", size));

  // ── palette
  // Every swatch is repainted after every edit, since one colour affects every
  // ratio measured against it.
  const ratios = [];
  const swatches = [];
  const paint = () => {
    for (const w of swatches) if (document.activeElement !== w.node) w.node.value = tokenValue(w.name);
    for (const r of ratios) {
      const got = contrast(tokenValue(r.name), tokenValue(r.against));
      r.node.textContent = got.toFixed(1) + ":1";
      r.node.classList.toggle("fail", got < r.target);
      r.node.title = got.toFixed(2) + ":1 against " + r.against.slice(2)
                   + (got < r.target ? ", wants " + r.target + ":1" : "");
    }
  };

  const groups = [];
  for (const g of TOKEN_GROUPS) {
    const grid = el("div", "swatches");
    grid.setAttribute("role", "group");
    grid.setAttribute("aria-label", g.label);
    for (const [name, label, does, audit] of g.rows) {
      const row = el("div", "sw");
      const input = document.createElement("input");
      input.type = "color"; input.value = tokenValue(name);
      input.title = name + ": " + does;
      input.setAttribute("aria-label", label + ", " + does);
      input.oninput = () => {
        VARS[name] = input.value;
        document.documentElement.style.setProperty(name, input.value);
        saveAppearance(); paint();
      };
      swatches.push({ node: input, name: name });
      const lbl = el("div", "lbl");
      lbl.append(el("div", "nm", label), el("div", "does", does));
      lbl.title = does;
      row.append(input, lbl);
      if (audit) {
        const r = el("span", "ratio");
        ratios.push({ node: r, name: name, against: audit[0], target: audit[1] });
        row.append(r);
      }
      grid.append(row);
    }
    const wrap = el("div", "grp");
    wrap.append(el("div", "hd", g.label), el("div", "note", g.note), grid);
    groups.push(wrap);
  }
  paint();

  const acts = el("div", "row");
  const dice = el("button", "act", "randomize");
  dice.title = "a new world for the neutrals; the four accents keep their meanings";
  dice.onclick = () => {
    VARS = randomPalette();
    applyVars(); saveAppearance();
    if (STATE) render(STATE, true);
    openAppearance();
  };
  const revert = el("button", "link revert", "revert");
  revert.title = "throw away every colour mixed here and show "
               + (document.documentElement.dataset.theme || "system") + " as written";
  revert.onclick = () => {
    VARS = {}; applyVars(); saveAppearance();
    if (STATE) render(STATE, true);
    openAppearance();
  };
  acts.append(dice, revert);

  const saveRow = el("div", "row");
  const nameBox = document.createElement("input");
  nameBox.className = "field"; nameBox.placeholder = "name this look";
  nameBox.maxLength = 40; nameBox.style.flex = "1 1 140px";
  nameBox.onkeydown = (e) => { if (e.key === "Enter") savePreset(nameBox.value); };
  const keep = el("button", "act", "save preset");
  keep.onclick = () => savePreset(nameBox.value);
  saveRow.append(nameBox, keep);

  const pal = el("div", "set");
  pal.append(...groups, acts, saveRow);
  set.append(blk("colors", pal));

  // ── presets
  const all = presets();
  const names = Object.keys(all).sort((a, b) => a.localeCompare(b));
  const box = el("div", "presets");
  if (!names.length) {
    box.append(empty("no presets yet", "mix a palette above, name it, and it is saved here"));
  }
  for (const name of names) {
    const row = el("div", "preset");
    const nm = el("div", "nm");
    const use = el("button", "link", name);
    use.onclick = () => applyPreset(name);
    nm.append(use);
    const chips = el("div", "chips");
    for (const t of ["--ground", "--panel", "--amber", "--mint", "--coral", "--violet"]) {
      const i = el("i");
      i.style.background = (all[name].vars || {})[t] || "transparent";
      chips.append(i);
    }
    const del = el("button", "link", "forget");
    del.onclick = () => {
      const a = presets(); delete a[name]; writePresets(a); openAppearance();
    };
    row.append(nm, chips, del);
    box.append(row);
  }
  set.append(blk("presets", box));

  // ── tiles
  const lay = el("div", "lay");
  for (const key of CAPPABLE) {
    const movable = MAIN_PANELS.indexOf(key) >= 0;
    lay.append(el("div", "nm", PANEL_LABEL[key] || key));

    const where = document.createElement("select");
    where.className = "pick";
    if (movable) {
      for (const [i, label] of COL_LABEL.entries()) {
        const o = document.createElement("option");
        o.value = String(i); o.textContent = label;
        where.append(o);
      }
      where.value = String((LAYOUT[key] || {}).col || 0);
      where.onchange = () => {
        // Land at the bottom of the new column; applyLayout renumbers.
        LAYOUT[key] = Object.assign({}, LAYOUT[key], { col: Number(where.value), i: 99 });
        applyLayout(); saveAppearance(); openAppearance();
      };
    } else {
      const o = document.createElement("option");
      o.textContent = "top strip"; where.append(o);
      where.disabled = true;
      where.title = "the strip across the top is fixed. It is the first thing you read";
    }
    lay.append(where);

    const mv = el("div", "mv");
    if (movable) {
      const up = el("button", "link", "↑"), down = el("button", "link", "↓");
      up.title = "move up"; down.title = "move down";
      up.onclick = () => moveTile(key, -1);
      down.onclick = () => moveTile(key, 1);
      mv.append(up, down);
    }
    lay.append(mv);

    const cap = document.createElement("select");
    cap.className = "pick";
    cap.title = SELF_SCROLL[key]
      ? "how tall this list gets before it scrolls"
      : "cap the height and this tile scrolls inside itself";
    const heights = SELF_SCROLL[key]
      ? [[0, key === "pulse" ? "40vh" : "46vh"], [260, "260px"], [380, "380px"],
         [520, "520px"], [700, "700px"]]
      : [[0, "grows"], [260, "260px"], [380, "380px"], [520, "520px"], [700, "700px"]];
    for (const [v, label] of heights) {
      const o = document.createElement("option");
      o.value = String(v); o.textContent = label;
      cap.append(o);
    }
    cap.value = String((LAYOUT[key] || {}).max || 0);
    cap.onchange = () => {
      LAYOUT[key] = Object.assign({}, LAYOUT[key], { max: Number(cap.value) });
      applyLayout(); saveAppearance();
    };
    lay.append(cap);
  }
  const layActs = el("div", "row");
  const snap = el("button", "act", "snap");
  snap.title = "close this and drag the tiles where you want them";
  snap.onclick = () => { closeDrawer(); setSnap(true); };
  const layReset = el("button", "link", "back to the designed layout");
  layReset.onclick = () => { LAYOUT = {}; applyLayout(); saveAppearance(); openAppearance(); };
  layActs.append(snap, layReset);
  const layBox = el("div", "set");
  layBox.append(lay, layActs);
  set.append(blk("tiles", layBox));

  body.append(set);
}

applyScale();
applyVars();
applyLayout();
