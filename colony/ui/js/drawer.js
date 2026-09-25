// The side drawer and its back trail.

import { $, el } from "./core.js";
import { openConsole } from "./console.js";
import { openCompleted, openPersona, openStory } from "./drawers.js";
import { openAgent, openProject, openPulse } from "./details.js";
import { SNAP, setSnap } from "./appearance.js";

// ── drawer ──────────────────────────────────────────────────────────────────

export let ALL_PROJECTS = [];   // every folder with a PROJECT.md, for the confirm pickers
export function setAllProjects(list) { ALL_PROJECTS = list; }

// A drawer can open another, so the drawer keeps a trail back. `openDrawer`
// runs at the top of every open* function, before the fetch, so whether a
// drawer was already open tells a step down from a fresh start.
export let TRAIL = [];        // where we came from, innermost last
export let HERE = null;       // the view on screen, if it is one we know how to re-open
export let GOING_BACK = false;

export function drawerViews() {
  return { story: openStory, persona: openPersona, pulse: openPulse,
           project: openProject, agent: openAgent, completed: openCompleted,
           console: openConsole };
}

export function renderTrail() {
  const b = $("d-back");
  const prev = TRAIL[TRAIL.length - 1];
  b.style.display = prev ? "" : "none";
  if (prev) {
    b.textContent = "← " + prev.label;
    b.title = "back to " + prev.label;
  }
}

export function goBack() {
  const prev = TRAIL.pop();
  if (!prev) return;
  GOING_BACK = true;
  try { drawerViews()[prev.kind](prev.arg); } finally { GOING_BACK = false; }
}

export function openDrawer(eyebrow, title, opts) {
  const d = $("drawer");
  const nav = opts && opts.nav;
  if (!d.classList.contains("on")) TRAIL = [];          // opened from the page: a fresh trail
  else if (HERE && !GOING_BACK) TRAIL.push(HERE);       // opened from a drawer: a step down
  // A view without `nav` cannot be returned to (reopening a half-written reply
  // would lose the draft), but it can still offer a way back to the view before
  // it.
  HERE = nav || null;
  renderTrail();
  d.classList.add("on"); $("scrim").classList.add("on"); d.setAttribute("aria-hidden", "false");
  d.classList.toggle("wide", !!(opts && opts.wide));
  $("d-eyebrow").textContent = eyebrow;
  $("d-title").textContent = title;
  const body = $("d-body");
  body.replaceChildren(el("div", "empty", "loading…"));
  return body;
}

export function closeDrawer() {
  TRAIL = []; HERE = null; renderTrail();
  $("drawer").classList.remove("on");
  $("scrim").classList.remove("on");
  $("drawer").setAttribute("aria-hidden", "true");
}
$("d-close").onclick = closeDrawer;
$("d-back").onclick = goBack;
$("scrim").onclick = closeDrawer;
addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // Snap is a mode, and a mode has to be leavable by the key that leaves every
  // other mode. Otherwise the only way out is a button you have to aim at.
  if (SNAP) { setSnap(false); return; }
  closeDrawer();
});

export async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(res.status);
  return res.json();
}

export function blk(label, ...nodes) {
  const w = el("div", "blk");
  w.append(el("div", "lb", label));
  w.append(...nodes);
  return w;
}
export function sectionBlock(label, text) { return blk(label, el("pre", "detail", text)); }

// Notion checklists are stored as JSON arrays. Missing (pre-008) and empty both
// read as "no checklist".
export function jsonList(raw) {
  if (!raw) return [];
  try { const v = JSON.parse(raw); return Array.isArray(v) ? v : []; } catch (_) { return []; }
}
