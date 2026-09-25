// The spend chart.

import { $, el, store, toks, usd } from "./core.js";

// ── the spend chart ─────────────────────────────────────────────────────────
// The spend chart: grains from an hour to a year, bar or line, with the value
// under the pointer. The series comes from /api/spend, not the snapshot. Grain
// and shape are kept in localStorage.

export const SVGNS = "http://www.w3.org/2000/svg";
export const GRAIN_UNIT = { hour: "h", day: "d", week: "w", month: "mo", year: "y" };
export let SPEND_GRAIN = store.get("colony-spend-grain") || "day";
export let SPEND_KIND = store.get("colony-spend-kind") || "line";
export let SPEND_SERIES = null;
export let SPEND_WIDTH = 0;
// Where the window stops; `null` is live. Not persisted, so the page always
// opens on the present.
export let SPEND_END = null;
export let SPEND_LAND = null;    // which bucket to sit on after a paging load

export function svgEl(name, attrs) {
  const n = document.createElementNS(SVGNS, name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

// A round number at or above the top of the data, so the gridlines land on
// figures a person can hold in their head. 40k, not 38.7k.
export function niceMax(v) {
  if (!(v > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}

// One bucket forward or back, `n` of them. setMonth and setFullYear normalise
// overflow the same way the server's `_back` does.
export function stepDate(d, grain, n) {
  const t = new Date(d.getTime());
  if (grain === "hour") t.setHours(t.getHours() + n);
  else if (grain === "day") t.setDate(t.getDate() + n);
  else if (grain === "week") t.setDate(t.getDate() + 7 * n);
  else if (grain === "month") t.setMonth(t.getMonth() + n);
  else t.setFullYear(t.getFullYear() + n);
  return t;
}

export const pad2 = (n) => String(n).padStart(2, "0");
export const wireDate = (d) => d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
export const wire = (d) => wireDate(d) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
// The keys come back as "YYYY-MM-DD HH:MM". Date.parse would read that as UTC
// in some engines and local in others; splitting it is the way to be sure.
export function fromKey(k) {
  const [d, t] = String(k).split(" ");
  const [Y, M, D] = d.split("-").map(Number);
  const [h, m] = (t || "0:0").split(":").map(Number);
  return new Date(Y, M - 1, D, h, m);
}

export function windowEnd() {
  const pts = SPEND_SERIES && SPEND_SERIES.points;
  return pts && pts.length ? fromKey(pts[pts.length - 1].key) : new Date();
}

// A whole window at a time, overlapping by one bucket so the bucket you were
// reading stays on screen.
export function pageSpend(dir) {
  const s = SPEND_SERIES;
  if (!s) return;
  const span = s.span || 1;
  const to = dir < 0 ? fromKey(s.points[0].key)
                     : stepDate(windowEnd(), s.grain, span - 1);
  if (dir > 0 && s.live) return;
  SPEND_END = wire(to);
  SPEND_LAND = dir < 0 ? "end" : "start";
  loadSpend();
}

export async function loadSpend() {
  let url = "/api/spend?grain=" + encodeURIComponent(SPEND_GRAIN);
  if (SPEND_END) url += "&end=" + encodeURIComponent(SPEND_END);
  try {
    const r = await fetch(url);
    if (!r.ok) return;
    SPEND_SERIES = await r.json();
  } catch (e) { return; }
  // The server clamps a window past the present back onto the live one; the
  // page has to agree, or `now` stays lit on a view that is already now.
  if (SPEND_SERIES.live) SPEND_END = null;
  SPEND_WIDTH = 0;
  drawSpend();
}

export function spendReadout(p) {
  const out = $("spend-readout");
  out.replaceChildren();
  const s = SPEND_SERIES;
  if (!s) return;
  if (p) {
    out.append(el("span", "rl", p.label),
               el("span", "rt", toks(p.tokens) + " tok"));
    if (p.usd) out.append(el("span", "ru", usd(p.usd)));
    out.append(el("span", "rr", p.runs ? p.runs + (p.runs === 1 ? " run" : " runs")
                                       : "nothing ran"));
  } else {
    // Off the live view the span alone is not enough, "this 30d" is a lie
    // about a window ending in July, so it names its two ends instead.
    const pts = s.points || [];
    const span = s.live ? "this " + s.span + GRAIN_UNIT[s.grain]
                        : pts.length ? pts[0].label + " \u2192 " + pts[pts.length - 1].label
                                     : s.span + GRAIN_UNIT[s.grain];
    const aside = [];
    if (s.outside) aside.push(s.outside + " before");
    if (s.ahead) aside.push(s.ahead + " after");
    out.append(el("span", "rl", span), el("span", "rt", toks(s.tokens) + " tok"));
    if (s.usd) out.append(el("span", "ru", usd(s.usd)));
    out.append(el("span", "rr", s.runs + (s.runs === 1 ? " run" : " runs")
                                + (aside.length ? "  \u00b7  " + aside.join(", ") : "")));
  }
}

export function drawSpend() {
  const s = SPEND_SERIES;
  const box = $("spend-chart");
  const svg = $("spend-svg");
  if (!s || !box) return;
  const W = box.clientWidth;
  if (!W) return;                       // the panel is folded, or not laid out yet
  SPEND_WIDTH = W;

  for (const b of $("spend-grain").children) b.classList.toggle("on", b.dataset.grain === SPEND_GRAIN);
  for (const b of $("spend-kind").children) b.classList.toggle("on", b.dataset.kind === SPEND_KIND);
  $("spend-count").textContent = s.span + GRAIN_UNIT[s.grain];

  // Disabled rather than hidden on the live view, so the layout holds.
  $("spend-next").disabled = !!s.live;
  $("spend-now").disabled = !!s.live;
  const dateIn = $("spend-date");
  dateIn.max = wireDate(new Date());
  dateIn.value = wireDate(windowEnd());

  const pts = s.points || [];
  const H = 152, PL = 46, PR = 8, PT = 10, PB = 20;
  const iw = Math.max(20, W - PL - PR), ih = H - PT - PB;
  const top = niceMax(Math.max(...pts.map((p) => p.tokens), 0));
  const y = (v) => PT + ih - (v / top) * ih;
  // Bars own a band; a line is plotted at the centre of the same band, so the
  // crosshair lands in the same place whichever shape is on screen.
  const band = iw / Math.max(1, pts.length);
  const x = (i) => PL + band * (i + 0.5);

  svg.replaceChildren();
  svg.setAttribute("width", W);
  svg.setAttribute("height", H);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  // `var()` is not allowed in SVG presentation attributes; `currentColor` is,
  // so the accent is set once on the element.
  svg.style.color = "var(--violet)";

  // Grid lines first, with their values. `toks` renders 0 as a dash, which is
  // wrong on an axis.
  const axisTok = (v) => (v >= 1 ? toks(Math.round(v)) : "0");
  for (let k = 0; k <= 4; k++) {
    const v = (top / 4) * k, yy = Math.round(y(v)) + 0.5;
    svg.append(svgEl("line", { class: "gline" + (k ? "" : " axis"),
                               x1: PL - 4, x2: W - PR, y1: yy, y2: yy }));
    const t = svgEl("text", { class: "glabel", x: PL - 7, y: yy + 3, "text-anchor": "end" });
    t.textContent = axisTok(v);
    svg.append(t);
  }

  // And verticals at whichever buckets get a label, so the eye can drop from a
  // point to the date under it without counting across.
  const every = Math.max(1, Math.ceil(pts.length / Math.max(2, Math.floor(iw / 74))));
  pts.forEach((p, i) => {
    if (i % every) return;
    const xx = Math.round(x(i)) + 0.5;
    svg.append(svgEl("line", { class: "gline", x1: xx, x2: xx, y1: PT, y2: PT + ih }));
    const t = svgEl("text", { class: "glabel", x: xx, y: H - 6, "text-anchor": "middle" });
    t.textContent = p.label;
    svg.append(t);
  });

  const bars = [];
  if (SPEND_KIND === "bar") {
    const w = Math.max(1, band - Math.min(6, band * 0.28));
    pts.forEach((p, i) => {
      const h = p.tokens ? Math.max(1, PT + ih - y(p.tokens)) : 0;
      const r = svgEl("rect", { class: "bar", x: x(i) - w / 2, width: w,
                                y: PT + ih - h, height: h });
      bars.push(r);
      if (h) svg.append(r);
    });
  } else {
    const line = pts.map((p, i) => `${x(i)},${y(p.tokens)}`).join(" ");
    if (pts.length > 1) {
      svg.append(svgEl("polygon", {
        points: `${x(0)},${PT + ih} ${line} ${x(pts.length - 1)},${PT + ih}`,
        fill: "currentColor", opacity: ".12",
      }));
    }
    svg.append(svgEl("polyline", { points: line, fill: "none", stroke: "currentColor",
                                   "stroke-width": "1.6", "stroke-linejoin": "round" }));
    // A single point has no line to be seen as, and every grain coarser than a
    // day currently has exactly one bucket with anything in it.
    if (pts.length === 1) {
      svg.append(svgEl("circle", { cx: x(0), cy: y(pts[0].tokens), r: 3, fill: "currentColor" }));
    }
  }

  // The crosshair is made once and moved, rather than redrawn on every pixel of
  // pointer travel.
  const cross = svgEl("line", { class: "cross", y1: PT, y2: PT + ih, x1: 0, x2: 0,
                                opacity: 0 });
  const dot = svgEl("circle", { r: 3.2, fill: "currentColor", opacity: 0, cx: 0, cy: 0 });
  svg.append(cross, dot);

  let hot = -1;
  const hover = (i) => {
    if (i === hot) return;
    hot = i;
    if (bars.length) bars.forEach((b, k) => b.classList.toggle("hot", k === i));
    if (i < 0) {
      cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0);
      spendReadout(null);
      return;
    }
    const p = pts[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
    cross.setAttribute("opacity", 1);
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.tokens));
    dot.setAttribute("opacity", SPEND_KIND === "bar" ? 0 : 1);
    spendReadout(p);
  };

  svg.onpointermove = (ev) => {
    const r = svg.getBoundingClientRect();
    // The svg is laid out at its own pixel width, so client x maps straight
    // through. But a zoomed page scales it, hence the ratio.
    const px = (ev.clientX - r.left) * (W / (r.width || W));
    hover(Math.max(0, Math.min(pts.length - 1, Math.floor((px - PL) / band))));
  };
  svg.onpointerleave = () => hover(-1);

  // Keyboard reading: arrows step a bucket and page the window at the ends.
  svg.tabIndex = 0;
  svg.onkeydown = (ev) => {
    const k = ev.key;
    if (k === "ArrowLeft" || k === "ArrowRight") {
      const d = k === "ArrowLeft" ? -1 : 1;
      let i = hot < 0 ? (d < 0 ? pts.length - 1 : 0) : hot + d;
      if (i < 0) pageSpend(-1);
      else if (i > pts.length - 1) pageSpend(1);
      else hover(i);
    } else if (k === "Home") hover(0);
    else if (k === "End") hover(pts.length - 1);
    else if (k === "PageUp") pageSpend(-1);
    else if (k === "PageDown") pageSpend(1);
    else if (k === "Escape") hover(-1);
    else return;
    ev.preventDefault();
  };

  spendReadout(null);
  // A window that was just paged into sits on the bucket that was on screen
  // before the arrow was pressed. The one the two windows share.
  if (SPEND_LAND && pts.length) {
    hover(SPEND_LAND === "end" ? pts.length - 1 : 0);
    SPEND_LAND = null;
  }

  const note = box.querySelector(".empty-note");
  if (note) note.remove();
  if (!s.tokens) {
    const where = s.live ? `the last ${s.span}${GRAIN_UNIT[s.grain]}` : "this window";
    const side = [];
    if (s.outside) side.push(`${s.outside} run${s.outside === 1 ? "" : "s"} before it`);
    if (s.ahead) side.push(`${s.ahead} run${s.ahead === 1 ? "" : "s"} after it`);
    box.append(el("div", "empty-note", side.length
      ? `nothing in ${where}: ${side.join(", ")}`
      : s.live ? "nothing spent yet" : `nothing in ${where}`));
  }
}

for (const seg of ["spend-grain", "spend-kind"]) {
  $(seg).addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.grain) {
      SPEND_GRAIN = b.dataset.grain;
      store.set("colony-spend-grain", SPEND_GRAIN);
      loadSpend();
    } else {
      SPEND_KIND = b.dataset.kind;
      store.set("colony-spend-kind", SPEND_KIND);
      drawSpend();
    }
  });
}

$("spend-prev").addEventListener("click", () => pageSpend(-1));
$("spend-next").addEventListener("click", () => pageSpend(1));
$("spend-now").addEventListener("click", () => {
  SPEND_END = null;
  SPEND_LAND = null;
  loadSpend();
});
$("spend-date").addEventListener("change", (ev) => {
  const v = ev.target.value;
  if (!v) return;
  // A bare date floors to midnight, which at hour grain ends on the day before.
  // 23:00 covers the whole day at every grain.
  SPEND_END = v + " 23:00";
  SPEND_LAND = "end";
  loadSpend();
});

// Redraw on width changes only; drawing sets the height, so height would loop.
if (window.ResizeObserver) {
  new ResizeObserver(() => {
    const w = $("spend-chart").clientWidth;
    if (w && w !== SPEND_WIDTH) drawSpend();
  }).observe($("spend-chart"));
}

export function renderSpend(sp) {
  loadSpend();

  const box = $("by-role");
  box.replaceChildren();
  const max = Math.max(...(sp.by_role || []).map((r) => r.tokens), 1);
  for (const r of sp.by_role || []) {
    const d = el("div", "rolebar");
    d.append(el("span", null, r.agent_role), el("span", null, `${toks(r.tokens)} tok ${usd(r.usd)}`));
    const track = el("div", "track"); const fill = el("i");
    fill.style.width = (r.tokens / max) * 100 + "%";
    track.append(fill); d.append(track);
    // Cache reads are excluded from every budget number on this page; showing
    // the raw total next to it is what stops that from looking like a bug.
    d.append(el("span", "dim", `${r.runs} run${r.runs === 1 ? "" : "s"}`),
             el("span", "dim", `${toks(r.total_tokens)} incl. cache reads`));
    box.append(d);
  }
  if (!box.children.length) box.append(el("div", "empty", "nothing spent yet"));
}
