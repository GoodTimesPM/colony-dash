// Shared helpers: the DOM shorthand, safe storage, formatting, and the one
// write path to the colony.

export const $ = (id) => document.getElementById(id);

// localStorage throws in a locked-down WebView, a private window, or when full.
// Everything kept there is a preference, so a failure means defaults.
export const store = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(key); return v == null ? fallback : v; }
    catch (_) { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) {} },
  remove(key) { try { localStorage.removeItem(key); } catch (_) {} },
};
export const STATUS_LABEL = {
  "backlog": "backlog", "needs-info": "needs info", "needs-criteria": "needs criteria",
  "ready": "ready", "in-progress": "running", "po-review": "po review",
  // Not "done". `accepted` means one patch landed; only `settled_as` (the PO's
  // Notion status or button) says a story is finished.
  "accepted": "delivered",
};

// ── formatting ──────────────────────────────────────────────────────────────
// Tokens are the unit; dollars are always the grayed secondary (§6).
export function toks(n) {
  if (!n) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
export function usd(v) { return v ? "$" + Number(v).toFixed(2) : ""; }
export function parseTs(s) { return s ? new Date(s.replace(" ", "T")) : null; }
export function clock(sec) {
  if (sec == null || isNaN(sec)) return "—";
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m >= 60 ? Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") : m + "m" + String(s).padStart(2, "0");
}
export function hhmm(s) { return s ? s.slice(11, 16) : ""; }
export function ago(s) {
  const d = parseTs(s); if (!d) return "";
  const mins = (Date.now() - d) / 6e4;
  if (mins < 60) return Math.max(0, Math.round(mins)) + "m ago";
  if (mins < 1440) return Math.round(mins / 60) + "h ago";
  return Math.round(mins / 1440) + "d ago";
}
export function until(s) {
  const d = parseTs(s); if (!d) return "—";
  const mins = (d - Date.now()) / 6e4;
  if (mins < 0) return "overdue by " + Math.round(-mins) + "m";
  if (mins < 90) return "in " + Math.round(mins) + "m";
  return "in " + Math.round(mins / 60) + "h";
}
// A block of text folded to a few lines, with a button that says how much more
// there is.
export function longText(text, limit) {
  const wrap = el("div", "long");
  const body = el("pre", "detail", text || "");
  wrap.append(body);
  if (!text || text.length <= (limit || 340)) return wrap;
  body.classList.add("clip");
  const t = el("button", "link more", "");
  const paint = () => {
    t.textContent = body.classList.contains("clip")
      ? "show the rest (" + text.length + " characters)" : "show less";
  };
  t.onclick = () => { body.classList.toggle("clip"); paint(); };
  paint();
  wrap.append(t);
  return wrap;
}

export function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;   // textContent, always: ledger text is data, not markup
  return n;
}

// Text that a live region reads out. Writing the same string again would
// still replace the text node, and some screen readers announce that, so a
// repaint with nothing new writes nothing.
export function setText(n, text) {
  if (n.textContent !== text) n.textContent = text;
}

// `h("button.act.go", { title, onclick }, "apply")`. Classes ride on the tag.
// `role`, `aria-*` and `data-*` keys become attributes, everything else a
// property. Children that are strings become text nodes, never markup. Falsy
// children are skipped, so a child can be `count && node`; pass a zero as "0".
export function h(tag, props, ...kids) {
  const [name, ...classes] = tag.split(".");
  const n = document.createElement(name || "div");
  if (classes.length) n.className = classes.join(" ");
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "role" || k.startsWith("aria-") || k.startsWith("data-")) n.setAttribute(k, String(v));
    else n[k] = v;
  }
  for (const c of kids.flat()) {
    if (c) n.append(c);
  }
  return n;
}

// The shapes every panel repeats.
export const chip = (text, cls, title) => h("span" + (cls ? "." + cls : ""), { title }, text);
export const btn = (label, cls, title, onclick) =>
  h("button" + (cls ? "." + cls.split(" ").join(".") : ""), { type: "button", title, onclick }, label);
// A line of chips, `row("div.tally", ...)`. One with nothing in it is left out.
export const row = (tag, ...kids) => {
  const r = h(tag, null, ...kids);
  return r.childNodes.length ? r : null;
};
export const card = (cls, props, ...kids) => h("div.tile" + (cls ? "." + cls : ""), props, ...kids);

// An empty state is one line. The longer why sits behind a "?" for whoever
// wants it: a tooltip on hover, and a toast on tap, since a phone never shows
// a title.
export function empty(text, more) {
  const n = h("div.empty", null, text);
  if (more) n.append(" ", h("button.why", { type: "button", title: more, "aria-label": more,
                                           onclick: () => toast(more, "") }, "?"));
  return n;
}

// ── writing to the colony ───────────────────────────────────────────────────
// One function for every control on the page. The server requires the custom
// header, which a cross-origin page cannot send. Refusals come back as a 409
// with a reason written for a person, so show that text.

export async function act(path, body) {
  try {
    const res = await fetch("/api/act/" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Colony": "1" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(data.detail || ("refused (" + res.status + ")"), "bad"); return null; }
    refresh();
    return data;
  } catch (err) {
    toast("the ledger did not answer. Is the server still up?", "bad");
    return null;
  }
}

export function toast(text, kind) {
  const t = el("div", "toast" + (kind ? " " + kind : ""), text);
  $("toasts").append(t);
  setTimeout(() => t.remove(), 6000);
}

// core.js imports nothing, so it runs first. The painter lives in render.js
// and main.js hands it over here.
export let paint = () => {};
export function onState(f) { paint = f; }

export function refresh() {
  fetch("/api/state").then((r) => r.json()).then((s) => paint(s)).catch(() => {});
}
