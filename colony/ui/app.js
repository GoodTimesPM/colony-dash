// Colony Dash — the whole dashboard.
//
// One script, no build step and no framework: this page is served from
// 127.0.0.1 to one person, and a toolchain would be more moving parts than
// the thing it builds. It lived inside index.html until 2026-08-27.
//
// It reads `/api/state` and paints. Every write goes back through an
// `/api/...` POST with the `X-Colony` header; nothing here decides anything.

"use strict";

const $ = (id) => document.getElementById(id);
const STATUS_LABEL = {
  "backlog": "backlog", "needs-info": "needs info", "needs-criteria": "needs criteria",
  "ready": "ready", "in-progress": "running", "po-review": "po review",
  // Not "done". `accepted` means the PO approved one patch and the files landed
  // — it says nothing about whether the project is finished, and printing DONE
  // over a story whose Notion row still reads In Progress is the board deciding
  // that for him. The real finished state is `settled_as`, which only his Notion
  // status or his own button can set, and it has its own chip.
  "accepted": "delivered",
};

// ── formatting ──────────────────────────────────────────────────────────────
// Tokens are the unit; dollars are always the grayed secondary (§6).
function toks(n) {
  if (!n) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function usd(v) { return v ? "$" + Number(v).toFixed(2) : ""; }
function parseTs(s) { return s ? new Date(s.replace(" ", "T")) : null; }
function clock(sec) {
  if (sec == null || isNaN(sec)) return "—";
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m >= 60 ? Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") : m + "m" + String(s).padStart(2, "0");
}
function hhmm(s) { return s ? s.slice(11, 16) : ""; }
function ago(s) {
  const d = parseTs(s); if (!d) return "";
  const mins = (Date.now() - d) / 6e4;
  if (mins < 60) return Math.max(0, Math.round(mins)) + "m ago";
  if (mins < 1440) return Math.round(mins / 60) + "h ago";
  return Math.round(mins / 1440) + "d ago";
}
function until(s) {
  const d = parseTs(s); if (!d) return "—";
  const mins = (d - Date.now()) / 6e4;
  if (mins < 0) return "overdue by " + Math.round(-mins) + "m";
  if (mins < 90) return "in " + Math.round(mins) + "m";
  return "in " + Math.round(mins / 60) + "h";
}
// A block of text folded to a few lines with a way to see the rest. A button
// rather than a bare <details> so it can say how much more there is: "show all"
// with no size is a door with nothing written on it.
function longText(text, limit) {
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

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;   // textContent, always: ledger text is data, not markup
  return n;
}

// ── writing to the colony ───────────────────────────────────────────────────
// One function for every control on the page. The custom header is what stops
// a stray page in a browser from POSTing here across origins; the server
// requires it (see server.py). Refusals come back as 409 with the reason
// written for a person — so show that text, not "request failed".

async function act(path, body) {
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
    toast("the ledger did not answer — is the server still up?", "bad");
    return null;
  }
}

function toast(text, kind) {
  const t = el("div", "toast" + (kind ? " " + kind : ""), text);
  $("toasts").append(t);
  setTimeout(() => t.remove(), 6000);
}

function refresh() {
  fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});
}

// ── avatars ─────────────────────────────────────────────────────────────────
// A hired persona's identity carries from the roster file through to the running
// agent (§9.3): same seed, same face, every run. Mirrored down the vertical axis
// so eight bits of noise read as a character.
function hash32(str) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return h >>> 0;
}
// Roster colours are whatever the persona file's author typed: "#0A66C2",
// "blue", "slate", "neon-green". Canvas does not throw on a colour it cannot
// parse — it *ignores the assignment*, and whatever was in `fillStyle` before
// stays. What was in it before, here, was the panel background. So every hired
// persona whose file named a colour CSS has never heard of was painted onto the
// panel in the panel's own colour, and came out blank. The two structural
// agents had faces only because they have no roster row at all: `color` was
// null, and the seed-derived fallback ran.
//
// The words below are the ones the roster actually uses that CSS does not know.
// Anything else unrecognised falls through to the seed, so a persona still gets
// a face — never nothing.
const TINT_WORDS = {
  slate: "#94a3b8", amber: "#f59e0b", rose: "#fb7185",
  "neon-green": "#39ff14", "neon-cyan": "#00e5ff", "metallic-blue": "#4a749b",
};

// Two sentinels, because the only way to ask canvas whether it understood a
// colour is to watch whether it changed its mind. A value it accepts overwrites
// both starting points identically; one it rejects leaves both untouched.
function asHex(value) {
  if (!value) return null;
  const word = String(value).trim().toLowerCase();
  if (TINT_WORDS[word]) return TINT_WORDS[word];
  const t = document.createElement("canvas").getContext("2d");
  t.fillStyle = "#000000"; t.fillStyle = value; const a = t.fillStyle;
  t.fillStyle = "#ffffff"; t.fillStyle = value; const b = t.fillStyle;
  return (a === b && String(a).charAt(0) === "#") ? a : null;
}

function hex2hsl(hex) {
  const [r, g, b] = hex2rgb(hex).map((v) => v / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  const l = (mx + mn) / 2;
  if (!d) return [0, 0, l * 100];
  const s = d / (1 - Math.abs(2 * l - 1));
  const h = mx === r ? ((g - b) / d + (g < b ? 6 : 0))
          : mx === g ? (b - r) / d + 2
          : (r - g) / d + 4;
  return [h * 60, s * 100, l * 100];
}

// A hue is character and belongs to the persona; a lightness that survives this
// theme is arithmetic and does not. Nine roster colours are near-black —
// "#000000" among them — which on a sunk dark panel is the same failure as an
// unparseable word by a different route. `toward` is the function that already
// keeps a randomised palette legible; it keeps these legible too.
// `toward` takes its direction from the colour, which is right when the colour
// was chosen for a known background and wrong here, where 270 of them arrive
// from persona files written by people who never saw this panel. "#000000"
// starts below a dark panel and walks further down into a floor it is already
// on; yellow starts above a light one and walks up to white. Which way there is
// room is a fact about the panel, so the panel decides, and the clamped start
// keeps every hue inside the range where the walk has somewhere to go.
function legible(h, s, l, bg, target) {
  const dir = lum(bg) > 0.4 ? -1 : 1;
  for (let i = 0; i < 120 && contrast(hsl2hex(h, s, l), bg) < target; i++) l += dir;
  return hsl2hex(h, s, Math.min(100, Math.max(0, l)));
}

function tintFor(seed, color) {
  const bg = tokenValue("--panel-sunk");
  const hex = asHex(color);
  const hsl = hex ? hex2hsl(hex) : [hash32(seed || "anon") % 360, 52, 58];
  return legible(hsl[0], hsl[1], Math.min(80, Math.max(35, hsl[2])), bg, 3);
}

function drawAvatar(canvas, seed, color) {
  const S = 8, px = 4;
  canvas.width = S * px; canvas.height = S * px;
  const ctx = canvas.getContext("2d");
  let h = hash32(seed || "anon");
  const rnd = () => { h ^= h << 13; h ^= h >>> 17; h ^= h << 5; h >>>= 0; return h / 4294967296; };

  ctx.fillStyle = tokenValue("--panel-sunk");
  ctx.fillRect(0, 0, S * px, S * px);

  const cells = [];
  for (let y = 1; y < S - 1; y++) for (let x = 0; x < S / 2; x++) cells.push(rnd() > 0.5);
  // Twenty-four coin flips will occasionally come up nearly all tails, and a
  // sprite with three lit pixels reads as a missing picture rather than a quiet
  // one. Filled deterministically from the top so the same seed still gives the
  // same face.
  let ink = cells.filter(Boolean).length;
  for (let i = 0; ink < 6 && i < cells.length; i++) if (!cells[i]) { cells[i] = true; ink++; }

  ctx.fillStyle = tintFor(seed, color);
  let i = 0;
  for (let y = 1; y < S - 1; y++) {
    for (let x = 0; x < S / 2; x++, i++) {
      if (cells[i]) {
        ctx.fillRect(x * px, y * px, px, px);
        ctx.fillRect((S - 1 - x) * px, y * px, px, px);
      }
    }
  }
}

// ── render ──────────────────────────────────────────────────────────────────

let STATE = null;
let filter = null;      // board column filter; null = everything
let openDivisions = new Set();   // survives re-renders; the SSE feed is frequent

function render(s) {
  STATE = s;
  document.body.classList.toggle("halted", !!s.controls.halted);
  document.body.classList.toggle("beating", !s.controls.halted);
  $("halt-why").textContent = s.controls.halt_reason || "";
  renderSprint(s.sprint);
  renderOrdis(s.ordis);
  renderColony(s.colony);
  renderBoard(s.board);
  renderCompleted(s.completed || []);
  renderProjects(s.projects);
  renderFlight(s.flight || []);
  renderInbox(s.inbox);
  renderMacros(s.controls);
  renderPulses(s.pulses);
  renderForge(s.forge);
  renderSpend(s.spend);
  renderDivisions(s.roster);
  $("roster-count").textContent = s.roster.total + " personas";
}

function tag(text, cls) { const b = el("span"); b.append(el("b", cls || null, text)); return b; }

// A ledger timestamp, said out loud. "2026-08-28 05:00:00" is a fact; "Fri
// 5:00 AM" is the fact a person can act on, and the only reason the page ever
// printed the raw string is that it was slicing one it had not parsed.
function when(ts) {
  const d = parseTs(ts);
  if (!d || isNaN(d)) return String(ts || "").slice(0, 16);
  return d.toLocaleString(undefined,
    { weekday: "short", hour: "numeric", minute: "2-digit" });
}

// The same fact with its date on it. `when` is for the next few days, where the
// weekday alone is enough. A ticket or a run can be three weeks old, and "Fri
// 5:00 AM" does not say which Friday.
function stamp(ts) {
  const d = parseTs(ts);
  if (!d || isNaN(d)) return String(ts || "").slice(0, 16);
  return d.toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function renderSprint(sp) {
  const sprint = sp.sprint, spent = sp.spent || {}, usage = sp.usage, band = sp.allowance;
  $("goal").textContent = sprint ? (sprint.goal || sprint.name) : "no active sprint";
  $("spend-tok").textContent = toks(spent.tokens) + " tok";
  $("spend-usd").textContent = usd(spent.usd);

  const meta = $("sprint-meta");
  meta.replaceChildren();
  if (sprint) {
    // The day comes from the server, which counts it from the allowance week's
    // real edge — Friday 05:00 — rather than from midnight on whatever date the
    // sprint row happens to carry. Counting whole days off a date was five
    // hours out at both ends of every week and, on the seeded placeholder
    // window, five days out at the start.
    const week = sp.week || {};
    const day = week.day || 1, total = week.days || 7;
    meta.append(
      tag(sprint.name), tag("day " + day + "/" + total),
      tag(spent.runs + " run" + (spent.runs === 1 ? "" : "s")),
      // A total that has not moved in a day is either a quiet colony or a
      // broken counter, and the number alone cannot say which.
      ...(sp.last_run_at ? [(() => {
        // Built by hand rather than through tag(), because the relative time has
        // to be a node the fifteen-second ticker can find and rewrite.
        const wrap = el("span"), inner = el("b");
        inner.append("last run ", relNode(null, sp.last_run_at, "ago"));
        wrap.append(inner);
        wrap.title = "the sprint total only moves when a run ends";
        return wrap;
      })()] : []),
      // The designed baseline stays visible next to the exception, so a boosted
      // week never quietly becomes the new normal.
      tag(band.boost
        ? `allowance ${band.effective}% of week (${band.base}% +${band.boost} boost)`
        : `allowance ${band.base}% of week`, band.boost ? "boosted" : null),
    );
  }

  const weekMeta = $("week-meta");
  weekMeta.replaceChildren();
  if (usage) {
    // `seven_day_resets_at` is a local timestamp now. It used to be the first
    // sixteen characters of the UTC string the cache carries, so a window that
    // closes at five in the morning was on screen as "08:59" — the right
    // instant, told in a timezone nobody here lives in.
    weekMeta.append(tag("week " + Number(usage.seven_day_pct).toFixed(1) + "%"),
                    tag("5h " + Number(usage.five_hour_pct).toFixed(0) + "%"),
                    tag("resets " + when(usage.seven_day_resets_at)));
    // A figure that has stopped moving is what a quiet week looks like and also
    // what a dead tray app looks like, and the percentage alone cannot say
    // which. This is the only thing that can.
    if (usage.stale) {
      const s = tag("cache stale", "boosted");
      s.title = "the tray app has not written since " + when(usage.sampled_at);
      weekMeta.append(s);
    }
  } else {
    weekMeta.append(tag("no usage sample — is the tray app running?"));
  }

  // The bar measures spend against the colony's *allowance*, not the whole week:
  // 35% of the window is the ceiling, so 35% consumed has to read as full.
  const ceiling = band.effective || 35;
  // The cache can carry a null percentage — the tray app writes what the API
  // gave it, and the API sometimes gives it nothing. `Number(null)` is 0, which
  // is at least a number; `null.toFixed` is a blank strip.
  const wk = usage ? Number(usage.seven_day_pct) || 0 : 0;
  const pct = usage ? Math.min(100, (wk / ceiling) * 100) : 0;
  const g = $("gauge");
  g.firstElementChild.style.width = pct + "%";
  g.dataset.state = pct >= 100 ? "hot" : pct >= 80 ? "warm" : "ok";
  $("spend-of").textContent = usage
    ? `week ${wk.toFixed(0)}% of ${ceiling}% allowed` : "";
}

function renderOrdis(o) {
  const box = $("ordis");
  box.replaceChildren();
  const last = o.last;
  $("ordis-count").textContent = last ? hhmm(last.pulse_at) : "never beat";

  const head = el("div", "ordis-head");
  head.append(el("div", "sigil", "◈"));
  const right = el("div");
  right.append(el("div", "state", o.halted ? "halted" : (o.running ? "working" : "watching")));
  const sub = el("div", "sub");
  if (last) {
    sub.append("last beat ", relNode(null, last.pulse_at, "ago"),
               " · next ", relNode(null, last.next_pulse_at, "until"));
  } else {
    sub.textContent = "no heartbeat recorded yet";
  }
  right.append(sub);
  head.append(right);
  box.append(head);

  const grid = el("div", "ordis-grid");
  const cell = (n, lb) => { const d = el("div"); d.append(el("span", "n", n), el("span", "lb", lb)); return d; };
  grid.append(cell(String(o.beats), "beats"), cell(String(o.wakes), "wakes"), cell(toks(o.tokens), "spent"));
  grid.append(cell(String(o.groomable), "to groom"), cell(String(o.queued), "dispatched"),
              cell(String(o.anomalies), "anomalies"));
  box.append(grid);

  if (last) {
    // The Scrum Master's own last sentence, in its own words. This is the one
    // place on the page where the loop gets to speak rather than be counted.
    box.append(el("div", "ordis-said", "“" + (last.finding || "clean") + "”"));
    const b = el("button", "act", "read the last beat");
    b.onclick = () => openPulse(last.id);
    box.append(b);
  }

  // The console lives here rather than in its own panel because it is Ordis
  // himself, not another department. It is also the one control on the page
  // that does not wait for a pulse, which is the whole reason it exists.
  const term = el("button", "act go", "open the console");
  term.title = "talk to Ordis directly, with a real shell, right now";
  term.onclick = () => openConsole();
  box.append(term);
}

function renderColony(c) {
  const box = $("colony");
  box.replaceChildren();
  $("colony-count").textContent = c.running.length ? c.running.length + " running" : "idle";

  for (const r of c.running) {
    box.append(agentRow({
      seed: r.avatar_seed || r.agent_role, color: r.color, role: r.agent_role,
      scope: r.project || "structural",
      line: (r.ticket_title || "t" + r.ticket_id),
      tokens: r.tokens, ceiling: r.max_tokens_run, live: true, started: parseTs(r.started_at),
    }));
  }
  for (const a of c.standby) {
    if (c.running.some((r) => r.agent_role === a.role)) continue;
    box.append(agentRow({
      seed: a.avatar_seed, color: a.color, role: a.role, scope: a.project || "structural",
      line: `${a.model.replace("claude-", "")} · ${a.write_capable ? "write" : "read-only"} · ${a.run_count} run${a.run_count === 1 ? "" : "s"}`,
      tokens: null, ceiling: a.max_tokens_run, live: false, agent: a,
    }));
  }
  if (!box.children.length) box.append(el("div", "empty", "nobody hired yet — open a persona in Standby to hire"));
  tickElapsed();   // paint now; the ticker only refreshes from the next second
}

function agentRow(o) {
  const row = el("div", "agent" + (o.live ? "" : " idle"));
  const cv = document.createElement("canvas");
  drawAvatar(cv, o.seed, o.color);
  if (o.live) cv.classList.add("pulsing");
  const right = el("div");
  const who = el("div", "who");
  who.append(el("span", "role", o.role), el("span", "scope", o.scope));
  right.append(who, el("div", "line", o.line));

  if (o.live) {
    // Elapsed time is painted by the one global ticker below, not a timer per
    // row: this panel re-renders on every state push, and a per-row interval
    // would leave a timer behind for every frame the page has ever drawn.
    const stat = el("div", "line mono elapsed");
    stat.dataset.started = o.started.getTime();
    stat.dataset.tokens = o.tokens || 0;
    right.append(stat);
  }
  // The bar is tokens against this agent's ceiling — Lloyd measures context, we
  // measure the thing we actually budget.
  if (o.ceiling) {
    const bar = el("div", "ceiling");
    const pct = Math.min(100, ((o.tokens || 0) / o.ceiling) * 100);
    bar.dataset.state = pct >= 90 ? "hot" : "ok";
    const fill = el("i"); fill.style.width = pct + "%";
    bar.append(fill); right.append(bar);
  }
  if (o.agent) {
    const acts = el("div", "row-acts");
    const info = el("button", "act", "contract");
    info.onclick = () => openAgent(o.agent.id);
    acts.append(info);
    if (o.agent.project) {                       // structural roles cannot be retired
      const r = el("button", "act no", "retire");
      r.onclick = () => confirmThen(`Retire ${o.agent.role}? Its history stays in the ledger.`,
                                    () => act("retire", { agent_id: o.agent.id }));
      acts.append(r);
    }
    right.append(acts);
  }
  row.append(cv, right);
  return row;
}

// Relative times are the only thing on this page that goes stale while the
// ledger sits perfectly still. Everything else redraws when SSE pushes a new
// snapshot — but between two beats nothing is pushed for an hour, so "last beat
// 0m ago · next in 60m" was frozen at the moment of the beat and stayed there,
// which reads exactly like a heartbeat that has stopped. That is how the pulse
// outage was found, and a clock that lies in the direction of "everything is
// fine" is the one kind worth fixing on sight.
//
// So a relative time is a node that remembers its own timestamp rather than a
// string baked at render. Fifteen seconds is the interval because the coarsest
// unit shown is a minute and nothing here should burn a wakeup per second.
function relNode(cls, ts, kind) {
  const n = el("span", cls);
  n.dataset.rel = ts || "";
  n.dataset.relKind = kind === "until" ? "until" : "ago";
  n.textContent = (kind === "until" ? until : ago)(ts);
  return n;
}
function tickRelative() {
  for (const n of document.querySelectorAll("[data-rel]")) {
    n.textContent = (n.dataset.relKind === "until" ? until : ago)(n.dataset.rel);
  }
}
setInterval(tickRelative, 15000);

function tickElapsed() {
  for (const n of document.querySelectorAll(".elapsed")) {
    const secs = (Date.now() - Number(n.dataset.started)) / 1000;
    n.textContent = `${clock(secs)} · ${toks(Number(n.dataset.tokens))} tok`;
  }
}
setInterval(tickElapsed, 1000);

function renderBoard(b) {
  const flow = $("flow");
  flow.replaceChildren();
  let total = 0;
  for (const c of b.columns) {
    total += c.n;
    const btn = el("button");
    btn.setAttribute("aria-pressed", String(filter === c.status));
    btn.append(el("span", "n" + (c.n ? "" : " zero"), String(c.n)), el("span", "lb", STATUS_LABEL[c.status] || c.status));
    btn.onclick = () => { filter = filter === c.status ? null : c.status; renderBoard(STATE.board); };
    flow.append(btn);
  }
  $("board-count").textContent = total + " open";

  const box = $("stories");
  box.replaceChildren();
  const shown = b.stories.filter((s) => !filter || s.status === filter);
  if (!shown.length) {
    box.append(el("div", "empty", filter ? "nothing in " + STATUS_LABEL[filter] : "board empty"));
    renderFiled(b.settled || []);
    renderDropped(b.dropped || []);
    return;
  }

  for (const s of shown) {
    const card = el("button", "story");
    card.append(el("span", "t", s.title));
    card.append(el("span", "chip" + (s.status === "in-progress" ? " live" : ["needs-info", "po-review"].includes(s.status) ? " attn" : ""),
                    STATUS_LABEL[s.status] || s.status));
    const sub = el("span", "sub");   // a card is a <button>: phrasing content only
    if (s.priority === 1) sub.append(el("span", "chip p1", "high"));
    if (s.project) {
      // An inference is not a permission. Show the guess, mark it as a guess.
      const p = el("span", s.project_source === "confirmed" ? "" : "guess", s.project);
      p.title = s.project_source === "confirmed" ? "confirmed by you" : "inferred — cannot authorise a write";
      sub.append(p);
    } else {
      sub.append(el("span", "dim", "no project"));
    }
    // What Notion says is already ticked off. The colony reads both halves of
    // the checklist now, so the board can say "4 of 7 done" instead of leaving
    // you to open the page to find out you had already finished it.
    if (s.done_n || s.open_n) {
      const total = s.done_n + s.open_n;
      const prog = el("span", "chip prog" + (s.done_n ? "" : " none"), s.done_n + "/" + total);
      prog.title = s.done_n + " of " + total + " to-dos ticked in Notion";
      sub.append(prog);
    }
    if (s.tokens) sub.append(el("span", null, toks(s.tokens) + " tok"));
    if (s.events) sub.append(el("span", "dim", s.events + " event" + (s.events === 1 ? "" : "s")));
    sub.append(el("span", "dim", ago(s.updated_at)));
    card.append(sub);
    card.onclick = () => openStory(s.id);

    const line = el("div", "story-line");
    const drop = el("button", "link drop", "drop");
    drop.title = "take this story off the board";
    drop.onclick = () => dropStory(s.id, "“" + s.title + "”");
    line.append(card, drop);
    box.append(line);
  }

  renderFiled(b.settled || []);
  renderDropped(b.dropped || []);
}

// ── the filed ───────────────────────────────────────────────────────────────
//
// Done, Shipped, Shelved, New and Not started all mean the same thing to the
// loop — the PO is not asking for anything — so they share one hidden shelf
// under the board rather than five columns across it. A column is somewhere
// work passes through; these are where it stops.
//
// They are kept visible behind a toggle because "did I finish that, or do I
// only remember deciding to?" is a question you have while looking at the
// board, and an answer two clicks into Notion is not an answer.
const FILED_LABEL = { "done": "done", "shelved": "shelved", "not-started": "not started" };

function renderFiled(rows) {
  const box = $("stories");
  const btn = $("board-filed");
  btn.style.display = rows.length ? "" : "none";
  btn.textContent = shows("filed") ? "hide filed · " + rows.length : "filed · " + rows.length;
  if (!shows("filed") || !rows.length) return;

  const list = el("div", "dropped-list");
  list.append(el("div", "lb", "filed — nothing is being asked about these"));
  for (const s of rows) {
    const line = el("div", "dropped-line is-filed");
    const t = el("button", "t filed", s.title);
    t.title = "open story #" + s.id + " · " + (s.project || "no project");
    t.onclick = () => openStory(s.id);
    line.append(t);
    line.append(el("span", "chip filed" + (s.settled_as === "done" ? " done" : ""),
                   FILED_LABEL[s.settled_as] || s.settled_as));
    // What Notion actually says, next to what the ledger made of it: the ledger
    // folds five statuses into three, and hiding that would make "shipped" look
    // like the dashboard had lost the word.
    if (s.notion_status) line.append(el("span", "why", s.notion_status));
    list.append(line);
  }
  box.append(list);
}

// ── the dropped ─────────────────────────────────────────────────────────────
//
// Dropped stories live under the board rather than in a separate view, because
// the question they answer — "did we decide not to do that, or did I imagine
// deciding?" — comes up while you are looking at the board. Hidden by default;
// the count in the header is what tells you there is anything to open.
function renderDropped(rows) {
  const box = $("stories");
  const btn = $("board-dropped");
  btn.style.display = rows.length ? "" : "none";
  btn.textContent = shows("dropped") ? "hide dropped · " + rows.length : "dropped · " + rows.length;
  if (!shows("dropped") || !rows.length) return;

  const list = el("div", "dropped-list");
  list.append(el("div", "lb", "dropped"));
  for (const s of rows) {
    const line = el("div", "dropped-line");
    const t = el("span", "t", s.title);
    t.title = s.drop_reason || "no reason recorded";
    line.append(t);
    line.append(el("span", "why", s.drop_reason ? s.drop_reason.slice(0, 40) : "no reason"));
    const back = el("button", "link", "restore");
    back.title = "put it back in the backlog, un-groomed";
    back.onclick = () => act("restore", { story_id: s.id });
    line.append(back);
    list.append(line);
  }
  box.append(list);
}

// Three orders, because the list answers three different questions. **changes**
// is the pulse's own order — commits first, then sheer volume — and is what you
// want when asking "what is outstanding". **recent** sorts by the newest mtime
// under the folder, which is nearly the opposite: colony-dash can be eighth by
// volume and still be the thing you were editing a minute ago. **name** is for
// when you already know what you are looking for and just want it to hold still.
// The choice is localStorage like the view menu — how you read the panel is not
// something the server needs to know.
const SORT_KEY = "colony-proj-sort";
let PROJ_SORT = localStorage.getItem(SORT_KEY) || "changes";
let PROJ_ROWS = [];

const PROJ_SORTS = {
  changes: (a, b) => (b.commits_since - a.commits_since) ||
                     (b.dirty_files - a.dirty_files) ||
                     a.project.localeCompare(b.project),
  // A folder with no stat-able change sorts last rather than first: "" would
  // win a descending string compare, and a row with no time is not the newest.
  recent:  (a, b) => (b.touched_at || "").localeCompare(a.touched_at || "") ||
                     a.project.localeCompare(b.project),
  name:    (a, b) => a.project.localeCompare(b.project),
};

for (const b of document.querySelectorAll("[data-sort]")) {
  b.onclick = () => {
    PROJ_SORT = b.dataset.sort;
    localStorage.setItem(SORT_KEY, PROJ_SORT);
    renderProjects(PROJ_ROWS);
  };
}

function renderProjects(rows) {
  const box = $("projects");
  PROJ_ROWS = rows;
  box.replaceChildren();
  // "10 with changes" left the reader to guess what the change was measured
  // against, and the honest answer is one specific commit — every project folder
  // lives inside a single git repo, so one baseline covers all sixty. Naming it
  // in the header is the difference between a number and a fact.
  const base = rows.length ? rows[0].head_sha : "";
  $("proj-count").textContent = rows.length
    ? rows.length + (base ? " differ from " + base : " with changes")
    : "all match the last commit";
  for (const b of document.querySelectorAll("[data-sort]")) {
    b.setAttribute("aria-pressed", b.dataset.sort === PROJ_SORT ? "true" : "false");
  }
  if (!rows.length) { box.append(el("div", "empty", "every project matches its last commit")); return; }

  const max = Math.max(...rows.map((r) => r.dirty_files || 1), 1);
  for (const r of rows.slice().sort(PROJ_SORTS[PROJ_SORT] || PROJ_SORTS.changes)) {
    const b = el("button", "proj");
    // The time goes on the row rather than only in the tooltip. "Why is this
    // folder dirty when I never opened it" is usually answered by *when* it was
    // written — an hour ago, while the machine was doing something else — and a
    // fact that only exists on hover is a fact nobody has.
    b.append(el("span", "p", r.project), el("span", "s", r.summary));
    if (r.touched_at) b.append(el("span", "s", ago(r.touched_at)));
    const bar = el("div", "bar");
    const seg = (n, cls) => { if (!n) return; const i = el("i", cls); i.style.width = (n / max) * 100 + "%"; bar.append(i); };
    seg(r.added, "a"); seg(r.modified, "m"); seg(r.deleted, "d"); seg(r.untracked, "u");
    b.append(bar);
    b.title = `${r.project}
${r.summary} — measured against ${r.head_sha || "?"} on ${r.branch || "?"}` +
              (r.touched_at ? `
last written ${r.touched_at} (${ago(r.touched_at)})` : "");
    b.onclick = () => openProject(r.project);
    box.append(b);
  }
}

// ── the file tree ───────────────────────────────────────────────────────────
//
// The Projects list above answers "what moved"; a project with a clean tree
// vanishes from it, which is correct for the pulse log and useless as a file
// manager. This is the other half: everything that is there, lazily, one folder
// per request, with change state hung off it as a dot.
//
// Open folders are remembered in a Set of paths rather than in the DOM, so a
// refresh — a snapshot arriving, a rescan — re-renders the same shape instead
// of collapsing everything the user opened.

const TREE_OPEN = new Set();
let TREE_FILTER = "";

async function renderTree() {
  const box = $("tree");
  box.replaceChildren(el("div", "empty", "reading the tree…"));
  try {
    const root = await getJSON("/api/tree?path=");
    box.replaceChildren();
    box.append(await treeLevel(root, ""));
  } catch {
    box.replaceChildren(el("div", "empty", "could not read the projects folder"));
  }
}

async function treeLevel(data, path) {
  const frag = document.createDocumentFragment();
  const q = TREE_FILTER.toLowerCase();
  // The filter applies to the top level only — it is there to find a project in
  // a list of sixty, not to search the disk. Filtering every level would make
  // an open folder disappear out from under the cursor.
  for (const d of data.dirs) {
    if (!path && q && !d.name.toLowerCase().includes(q)) continue;
    frag.append(await treeDir(d));
  }
  for (const f of data.files) {
    if (!path && q && !f.name.toLowerCase().includes(q)) continue;
    frag.append(treeFile(f));
  }
  if (!frag.childNodes.length) frag.append(el("div", "empty", "nothing here"));
  return frag;
}

async function treeDir(d) {
  const wrap = el("div");
  const row = el("button", "node dir" + (d.project ? " pj" : ""));
  const tw = el("span", "tw", TREE_OPEN.has(d.path) ? "▾" : "▸");
  row.append(tw, el("span", "ic", "▣"), el("span", "nm", d.name));
  if (d.changed) {
    row.append(el("span", "n", d.changed));
    row.append(el("span", "dot modified"));
  }
  row.title = d.path + (d.project ? "  ·  a project (has a PROJECT.md)" : "");
  wrap.append(row);

  const kids = el("div", "kids");
  kids.style.display = TREE_OPEN.has(d.path) ? "" : "none";
  wrap.append(kids);

  const load = async () => {
    kids.replaceChildren(el("div", "empty", "…"));
    try {
      const data = await getJSON("/api/tree?path=" + encodeURIComponent(d.path));
      kids.replaceChildren();
      kids.append(await treeLevel(data, d.path));
    } catch {
      kids.replaceChildren(el("div", "empty", "could not open that folder"));
    }
  };
  if (TREE_OPEN.has(d.path)) load();

  row.onclick = () => {
    const open = TREE_OPEN.has(d.path);
    if (open) { TREE_OPEN.delete(d.path); kids.style.display = "none"; tw.textContent = "▸"; }
    else { TREE_OPEN.add(d.path); kids.style.display = ""; tw.textContent = "▾"; load(); }
  };
  return wrap;
}

function treeFile(f) {
  const row = el("button", "node file");
  row.append(el("span", "tw", ""), el("span", "ic", "▤"), el("span", "nm", f.name));
  if (f.state) row.append(el("span", "dot " + f.state));
  row.title = f.path + "  ·  " + (f.size > 1024 ? Math.round(f.size / 1024) + " KB" : f.size + " B") +
              (f.state ? "  ·  " + f.state : "");
  row.onclick = () => openFile(f);
  return row;
}

async function openFile(f) {
  const body = openDrawer(f.state ? f.state : "file", f.name);
  const blocks = [];
  blocks.push(el("div", "meta mono dim", f.path));
  // A changed file leads with its diff — that is why it is interesting — and
  // still offers the whole text underneath.
  if (f.state && f.state !== "untracked") {
    try {
      const project = f.path.split("/")[0];
      const d = await getJSON("/api/diff?project=" + encodeURIComponent(project) +
                              "&path=" + encodeURIComponent(f.path));
      const pre = el("pre", "detail diff tall");
      paintDiff(pre, d.diff || "(nothing uncommitted)");
      blocks.push(blk("working-tree diff", pre));
    } catch { /* fall through to the text */ }
  }
  try {
    const data = await getJSON("/api/file?path=" + encodeURIComponent(f.path));
    blocks.push(data.text == null
      ? el("div", "empty", data.why || "nothing to show")
      : blk("contents", el("pre", "detail tall", data.text)));
  } catch {
    blocks.push(el("div", "empty", "this dashboard will not open that file"));
  }
  body.replaceChildren(...blocks);
}

// ── in flight ───────────────────────────────────────────────────────────────
// Tickets the colony is working or staffed to work, and changes queued for the
// Notion board that have not left this machine. Two shapes, one question: the
// PO pressed something and it has not finished yet.

function renderFlight(items) {
  const rail = $("flight"), strip = $("flight-strip");
  rail.replaceChildren();
  $("flight-count").textContent = items.length ? String(items.length) : "";
  strip.classList.toggle("quiet", !items.length);
  if (!items.length) {
    rail.append(el("div", "empty", "nothing queued"));
    return;
  }
  for (const f of items) rail.append(flightRow(f));
}

function flightRow(f) {
  const row = el("div", "fl");
  row.dataset.flight = f.key;
  row.dataset.state = f.kind === "push"
    ? (f.stuck ? "stuck" : "push")
    : (f.run_id ? "running" : f.status);

  if (f.kind === "push") {
    // A push says what it would do to the board, in the words of the board —
    // "→ Done" is the thing the PO clicked, and the row is here precisely so
    // that click has somewhere to be visible until it lands.
    const what = f.verb === "status" ? "→ " + f.what
               : f.verb === "check"  ? (f.checked ? "☑ " : "☐ ") + f.what
               : "💬 " + (f.what || "").slice(0, 80);
    row.append(el("div", "t", what));
    const m = el("div", "m");
    m.append(el("span", "st", f.stuck ? "gave up" : "queued"),
             el("span", null, "notion"),
             el("span", "who", f.story_title || ("story #" + f.story_id)));
    row.append(m);
    if (f.last_error) row.append(el("div", "err", f.last_error.slice(0, 120)));
    row.title = f.stuck
      ? "tried " + f.attempts + " times and stopped — the pulse will not retry this on its own"
      : "queued " + ago(f.queued_at) + " — the next pulse sends it. Nothing has changed on the board yet.";
  } else {
    row.append(el("div", "t", f.title));
    const m = el("div", "m");
    // A reply is a ticket like any other, but "research · unstaffed" describes
    // the mechanism and not the thing: what the PO wants to read here is that
    // he said something and Ordis has not answered yet.
    const reply = !!f.po_message_id;
    m.append(el("span", "st", f.run_id ? "running" : f.status),
             el("span", null, reply ? "reply" : f.intent),
             el("span", "who", f.role || (reply ? "waiting for Ordis" : "unstaffed")));
    row.append(m);
    if (reply && f.po_message) row.append(el("div", "said", "“" + f.po_message + "”"));
    // A blocked ticket says what it found. Without this the rail carried two
    // identical rows reading BLOCKED IMPLEMENT weekly-funnel-report-builder for
    // a day, which is three facts and no information.
    if (f.status === "blocked" && f.note) row.append(el("div", "note", f.note));
    row.title = (f.status === "blocked" && f.note ? f.note + "\n\n" : "") +
                "ticket #" + f.id + " · " + (f.story_title || "") +
                (f.run_id ? " · run #" + f.run_id + " started " + ago(f.run_started_at)
                          : " · created " + ago(f.created_at));
  }

  row.onmouseenter = () => lightFlight(f.key, true);
  row.onmouseleave = () => lightFlight(f.key, false);
  if (f.story_id) row.onclick = () => openStory(f.story_id);
  return row;
}

// ── completed work ──────────────────────────────────────────────────────────
//
// "I want there to be a 'completed dispatches' or 'completed stories'. That way
// i can keep track of progress and check on work that has been done so i dont
// accidentally work on the same thing just cause i forgot we worked on
// something."
//
// Two things end and both are here: a dispatch, which is an implement ticket
// that delivered a patch, and a story, which is one Jordan filed. They share a
// list because the question is chronological — what has this colony produced,
// in what order — and they keep separate badges because the answer to "did we
// already build this" is different from "did I already close this".
//
// Nothing on this panel writes. It is the only panel that is purely a record.
const DONE_FILTER_KEY = "colony-done-filter";
let DONE_FILTER = localStorage.getItem(DONE_FILTER_KEY) || "all";

function renderCompleted(items) {
  const box = $("completed");
  box.replaceChildren();
  for (const b of document.querySelectorAll("[data-done]")) {
    b.setAttribute("aria-pressed", String(b.dataset.done === DONE_FILTER));
  }
  const shown = DONE_FILTER === "all" ? items : items.filter((i) => i.kind === DONE_FILTER);

  // The count says how much work is behind you, not how much of it is on
  // screen — a filter that changes the headline number makes the number
  // useless for the thing it is for.
  const dispatches = items.filter((i) => i.kind === "dispatch").length;
  const stories = items.length - dispatches;
  $("completed-count").textContent = items.length
    ? dispatches + " delivered  ·  " + stories + " filed" : "";

  if (!shown.length) {
    box.append(el("div", "empty", items.length
      ? "nothing under this filter"
      : "nothing has finished yet — a dispatch lands here when its ticket closes"));
    return;
  }
  for (const it of shown) box.append(completedTile(it));
}

for (const b of document.querySelectorAll("[data-done]")) {
  b.onclick = () => {
    DONE_FILTER = b.dataset.done;
    localStorage.setItem(DONE_FILTER_KEY, DONE_FILTER);
    if (STATE) renderCompleted(STATE.completed || []);
  };
}

function completedTile(it) {
  const card = el("div", "tile done");
  card.tabIndex = 0;
  card.dataset.kind = it.kind;
  if (it.outcome) card.dataset.outcome = it.outcome;

  const k = el("div", "k");
  k.append(el("span", null, it.kind === "dispatch"
    ? (it.outcome === "empty" ? "no changes" : "delivered") + "  ·  " + it.ref
    : "filed " + (it.settled_as || "") + "  ·  " + it.ref));
  k.append(relNode("age", it.at));
  card.append(k);

  card.append(el("div", "r", it.title || "(untitled)"));

  // The date it finished, said properly. This whole panel is sorted by it, so
  // it is the one fact that cannot be a relative age alone: "3 days ago" does
  // not tell you whether that was before or after the thing you are about to
  // start.
  card.append(el("div", "meta", "completed " + stamp(it.at) +
    (it.since ? "   ·   since the last delivery on " + stamp(it.since) : "")));

  if (it.summary) {
    const sum = longText(it.summary, 400);
    sum.classList.add("sum");
    card.append(sum);
  }

  const tally = el("div", "tally");
  const add = (text, cls) => { if (text) tally.append(el("span", cls || null, text)); };
  if (it.kind === "dispatch") {
    add(it.done_n + " criteria met", it.done_n ? "good" : null);
    add(it.skipped_n ? it.skipped_n + " skipped" : "", "hot");
    add(it.files_n ? it.files_n + " files" : "");
    add(it.patch ? "patch" : "");
  } else {
    add(it.dispatches ? it.dispatches + " dispatch" + (it.dispatches === 1 ? "" : "es") : "no dispatch");
    add(it.done_n ? it.done_n + " of " + (it.done_n + it.skipped_n) + " to-dos" : "");
  }
  add(it.blockers ? it.blockers + " blocker" + (it.blockers === 1 ? "" : "s") : "", "hot");
  add(it.questions ? it.questions + " question" + (it.questions === 1 ? "" : "s") : "");
  add(it.messages ? it.messages + " message" + (it.messages === 1 ? "" : "s") : "");
  add(it.learnings ? it.learnings + " learned" : "");
  add(it.runs ? it.runs + " run" + (it.runs === 1 ? "" : "s") : "");
  add(it.tokens ? toks(it.tokens) + " tok" : "");
  add(usd(it.usd));
  card.append(tally);

  const foot = [];
  if (it.project) foot.push(it.project);
  if (it.role) foot.push(it.role);
  if (foot.length) card.append(el("div", "meta", foot.join("   ·   ")));

  const open = () => openCompleted(it.kind + ":" + it.id);
  card.onclick = open;
  card.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  card.title = "open the whole record — the work order, what it produced, and every "
             + "message, question and decision since the last delivery";
  return card;
}

// The link between the Inbox and this rail, in one function used from both
// ends. Keys are whitespace-separated on the tile because one question can sit
// over several tickets, and `[data-flight~="t7"]` matches a list for free.
function lightFlight(key, on) {
  for (const n of document.querySelectorAll('[data-flight~="' + key + '"]')) {
    n.classList.toggle("lit", on);
  }
}

// Which rail rows a given escalation is about: the ticket it was raised from,
// plus anything else in flight for the same story. A write-approval question
// and the ticket waiting on that approval are the same piece of work, and the
// tile is where you decide it.
function flightKeys(e) {
  const flight = (STATE && STATE.flight) || [];
  const keys = [];
  for (const f of flight) {
    // A closed ticket is not in flight, so an escalation raised from one gets
    // no marker: a badge that lights nothing is worse than no badge, because
    // it teaches you the link does not work.
    const mine = (e.ticket_id && f.kind === "ticket" && f.id === e.ticket_id) ||
                 (e.story_id && f.story_id === e.story_id);
    if (mine && !keys.includes(f.key)) keys.push(f.key);
  }
  return keys;
}

function renderInbox(items) {
  const box = $("inbox"), strip = $("inbox-strip");
  box.replaceChildren();

  // A stale question is one the story has moved past: it was written against a
  // version of the brief that no longer exists, and answering it now answers
  // the wrong question. It is not deleted — the colony really was confused, and
  // that is worth being able to look at — but it is out of the way by default.
  const stale = items.filter((e) => e.stale);
  const live = shows("stale") ? items : items.filter((e) => !e.stale);

  const btn = $("inbox-show-stale");
  btn.style.display = stale.length ? "" : "none";
  btn.textContent = shows("stale") ? "hide stale · " + stale.length : "stale · " + stale.length;

  const waiting = items.filter((e) => !e.stale).length;
  $("inbox-count").textContent = waiting ? waiting + " waiting on you" : "";
  strip.classList.toggle("quiet", waiting === 0);
  if (!live.length) {
    // Nothing to run out to a full row, and the observer has to be told so —
    // `replaceChildren` clears the tiles but not the count written beside them.
    box.dataset.live = 0;
    // An empty Inbox means the system is working, and it still gets a row. The
    // message rides in a slot rather than replacing them all, because a panel
    // that changes shape between "clear" and "one question" makes the whole page
    // jump for the least important reason it has.
    box.dataset.note = stale.length
      ? "nothing current — " + stale.length + " stale question" +
        (stale.length === 1 ? "" : "s") + " behind the toggle"
      : "empty — nothing needs you";
    padSlots(box, 0);
    return;
  }
  delete box.dataset.note;
  for (const e of live) box.append(inboxTile(e));
  padSlots(box, live.length);
}

// How many tiles fit across, asked of the grid rather than worked out from the
// track width — `auto-fill` already did that arithmetic, and doing it twice is
// how the two answers drift apart the first time the text size moves.
//
// The catch is *when* you ask. The answer is only true of the width the grid had
// at that instant, and the render that pads the row is not always standing on a
// laid-out grid: a fold still opening, a window not yet sized, the first paint
// of a restored layout. Measured then, the row is padded to a width that no
// longer exists and the slots stop short of the Ticket Queue — which is exactly
// the "sometimes they come back, sometimes they don't" of it. So the live count
// is remembered on the element and the padding is redone whenever the width
// changes, which turns a one-shot guess into something that keeps being right.
function padSlots(box, count) {
  if (count != null) box.dataset.live = count;
  const live = Number(box.dataset.live || 0);
  for (const old of box.querySelectorAll(".tile.slot")) old.remove();
  const cols = gridCols(box);
  if (!live) {
    // An empty Inbox is a full row of slots rather than no row at all. The grid
    // cannot always say how wide it is — a fold still opening, a first paint —
    // and the message has to appear either way, so an unknown column count falls
    // back to the single slot that carries it and the observer widens the row
    // the moment there is a width to widen it to.
    for (let i = 0; i < (cols || 1); i++) {
      box.append(i === 0 && box.dataset.note
        ? el("div", "tile slot note", box.dataset.note)
        : el("div", "tile slot"));
    }
    return;
  }
  if (!cols || live % cols === 0) return;
  for (let i = live % cols; i < cols; i++) box.append(el("div", "tile slot"));
}

// The computed value is a list of resolved track sizes once the grid has been
// laid out, and the unresolved `repeat(auto-fill, minmax(...))` while it has
// not — a folded panel, a display:none ancestor. Counting words in that second
// case returns a confident 2, which is a wrong answer wearing a right one's
// clothes; it is reported as "don't know" instead, and the observer asks again
// once the box has a width to answer with.
function gridCols(box) {
  const t = getComputedStyle(box).gridTemplateColumns;
  if (!t || t === "none" || t.indexOf("repeat(") >= 0 || t.indexOf("minmax(") >= 0) return 0;
  return t.split(" ").filter(Boolean).length;
}

// The Inbox is the full width of the page, so it changes size for reasons the
// render never hears about: the window, the sidebar, a neighbouring tile being
// folded away. Width only — padding changes the height, and reacting to that
// would be a loop.
if (window.ResizeObserver) {
  let inboxWidth = 0;
  new ResizeObserver(() => {
    const box = $("inbox"), w = box.clientWidth;
    if (!w || w === inboxWidth) return;
    inboxWidth = w;
    padSlots(box, null);
  }).observe($("inbox"));
}

function inboxTile(e) {
  const card = el("div", "tile");
  card.dataset.kind = e.kind;
  if (e.snoozed) card.classList.add("snoozed");
  if (e.stale) card.classList.add("stale");
  const k = el("div", "k");
  k.append(el("span", null, e.kind.replace("-", " ")), relNode("age", e.raised_at));
  if (e.stale) {
    const flag = el("span", "flag", "stale");
    flag.title = "the story changed after this was asked — the question is about a version that no longer exists";
    k.append(flag);
  }
  const keys = flightKeys(e);
  if (keys.length) {
    // The marker only appears when there is something to point at. A badge on
    // every tile would be furniture; a badge on three of them is information.
    card.dataset.flight = keys.join(" ");
    const pin = el("button", "pin", "queued · " + keys.length);
    pin.title = "this question has work in the ticket queue — hover to find it, click to open the story";
    pin.onmouseenter = () => keys.forEach((key) => lightFlight(key, true));
    pin.onmouseleave = () => keys.forEach((key) => lightFlight(key, false));
    pin.onclick = () => {
      const first = document.querySelector('.fl[data-flight="' + keys[0] + '"]');
      if (first) first.scrollIntoView({ block: "nearest" });
      if (e.story_id) openStory(e.story_id);
    };
    k.append(pin);
  }
  // The x. Every other control on this tile is an answer to the question, and
  // the only one that could clear a tile without answering it was "drop story"
  // — which takes the whole story off the board, cancels its tickets and
  // closes its other questions. So the cheapest way to tidy the Inbox was also
  // the most destructive thing in it, and a stale question about a problem
  // already solved elsewhere had no exit that did not cost something.
  //
  // Not on a write approval: there is a patch on disk and a worktree behind it,
  // and closing that question without answering it strands both. The server
  // refuses it too — this only hides a button that would fail.
  if (e.id && e.kind !== "write-approval") {
    const x = el("button", "dismiss", "×");
    x.title = "this question stopped mattering — close the card and change nothing else. "
            + "The story keeps its status; edit the brief and Ordis may ask again.";
    x.setAttribute("aria-label", "dismiss this question");
    x.onclick = () => act("decide", { escalation_id: e.id, decision: "dismiss" });
    k.append(x);
  }
  card.append(k, el("div", "r", e.reason));
  if (e.recommendation) {
    const rec = longText(e.recommendation, 520);
    rec.classList.add("rec");
    card.append(rec);
  }
  if (e.snoozed) {
    card.append(el("div", "snooze-note", "snoozed · back " + until(e.snoozed_until)));
  }
  // Ordis's last answer used to be quoted here. It was the wrong place for it:
  // the answer is usually to something said days ago, so it reads as the card's
  // own text and buries the question the card is actually asking. The reply
  // button carries the count, and the whole exchange is one press away.
  //
  // What is still queued does belong here, because it changes what the buttons
  // mean — deciding now decides ahead of an answer you asked for.
  if (e.awaiting_ordis) {
    card.append(el("div", "waiting",
      e.awaiting_ordis + " repl" + (e.awaiting_ordis === 1 ? "y" : "ies") +
      " queued — Ordis answers on the next pulse"));
  }

  for (const b of (e.blockers || [])) card.append(el("div", "blocker", b));

  const meta = [];
  if (e.story_title) meta.push(e.story_title);
  if (e.est_tokens) meta.push(toks(e.est_tokens) + " tok");
  if (meta.length) card.append(el("div", "meta", meta.join("  ·  ")));

  const acts = el("div", "acts");

  // A story whose folder is still a guess is the single most common thing in
  // this Inbox, and it has exactly one answer: name the folder. So the answer
  // is on the tile rather than two clicks into a drawer.
  //
  // The kind decides what else belongs here. A `needs-info` escalation *is* the
  // question "which folder?" — approving it would approve nothing — so it gets
  // the picker alone. A `decision` is a real yes/no that may also happen to sit
  // on an unconfirmed story, so it gets both.
  const unconfirmed = e.story_id && e.project_source !== "confirmed";
  const asksForProject = e.kind === "needs-info";
  if (unconfirmed) {
    // The picker sits on its own full-width row. Two answers are possible and
    // the second one — "none of these, it's new" — used to have nowhere to go:
    // choosing it swaps the <select> for a text field and confirms against a
    // folder that gets created on the spot.
    const row = el("div", "picker");
    const sel = projectSelect(e.project, { allowNew: true });
    const field = el("input", "field");
    field.placeholder = "new-folder-name";
    field.style.display = "none";
    const go = el("button", "act go", "confirm project");

    sel.onchange = () => {
      const isNew = sel.value === NEW_PROJECT;
      field.style.display = isNew ? "" : "none";
      sel.style.display = isNew ? "none" : "";
      go.textContent = isNew ? "create + confirm" : "confirm project";
      if (isNew) field.focus();
    };
    field.onkeydown = (ev) => {
      if (ev.key === "Escape") { sel.value = ""; sel.onchange(); }
      if (ev.key === "Enter") go.click();
    };
    go.onclick = () => {
      const isNew = sel.value === NEW_PROJECT;
      const name = isNew ? field.value.trim() : sel.value;
      if (!name) {
        toast(isNew ? "name the new folder" : "pick the folder this story belongs to first", "bad");
        return;
      }
      act("confirm-project", {
        story_id: e.story_id, project: name, create: isNew,
        why: isNew ? e.story_title || e.reason : "",
      });
    };
    row.append(sel, field, go);
    acts.append(row);
  }

  if (e.kind === "ready") {
    // Not an escalation — there is no `e.id` here and nothing to approve or
    // snooze. It is a state the story is in, rendered for exactly as long as
    // it is true (see `_ready` in server.py), so the only two useful controls
    // are "start it" and "here is what is still in the way".
    const go = el("button", "act go", "dispatch to build");
    const stuck = (e.blockers || []).length;
    go.disabled = !!stuck;
    go.title = stuck ? e.blockers[0]
      : "cuts the implement ticket — the next wake opens a git worktree and writes in it";
    go.onclick = () => confirmThen(
      "Dispatch \u201c" + (e.story_title || "this story") + "\u201d to build?\n\n" +
      "This cuts a ticket. The next wake opens a git worktree, works in there, and " +
      "brings back a patch — nothing touches your working tree until you approve it.",
      () => act("dispatch", { story_id: e.story_id }));
    acts.append(go);
  } else if (e.kind === "write-approval") {
    const view = el("button", "act", "read the patch");
    view.onclick = () => openPatch(e);
    const yes = el("button", "act go", "apply");
    yes.onclick = () => confirmThen(
      "Apply this patch to the live tree? It lands uncommitted — you still review and commit it yourself.",
      () => act("decide", { escalation_id: e.id, decision: "approve" }));
    const no = el("button", "act no", "reject");
    no.onclick = () => act("decide", { escalation_id: e.id, decision: "reject" });
    acts.append(view, yes, no);
  } else if (e.kind === "run-request" && e.id) {
    // The one card that runs something on the live tree rather than proposing a
    // change to it. The command is in the card text above, verbatim, because
    // approving a command you have not read is the whole risk here.
    const yes = el("button", "act go", "run it");
    yes.title = "runs it in the project folder and puts the output on the story";
    yes.onclick = () => confirmThen(
      "Run this command against your live tree?\n\n" +
      (e.reason || "").replace(/^.*?: `/, "") .replace(/`$/, "") + "\n\n" +
      "It runs in the project folder with a 90 second limit. Nothing is committed " +
      "and no patch is applied \u2014 the output goes on the story so the next " +
      "build can read it.",
      () => act("decide", { escalation_id: e.id, decision: "approve" }));
    const no = el("button", "act no", "don\u2019t run it");
    no.title = "records that you declined; the story keeps its other findings";
    no.onclick = () => act("decide", { escalation_id: e.id, decision: "reject" });
    acts.append(yes, no);
  } else if (e.kind === "brief-changed" && e.id) {
    const yes = el("button", "act go", "reopen for grooming");
    yes.title = "clears the criteria and puts it back in the groom queue — the next wake re-reads the brief";
    yes.onclick = () => confirmThen(
      "Reopen “" + (e.story_title || "this story") + "”?\n\n" +
      "Its acceptance criteria are cleared and the next wake re-reads the whole " +
      "brief from Notion. Anything already built stays built.",
      () => act("decide", { escalation_id: e.id, decision: "approve" }));
    const no = el("button", "act no", "leave it");
    no.title = "records that the edit did not change the work; the card returns only if you edit the page again";
    no.onclick = () => act("decide", { escalation_id: e.id, decision: "reject" });
    acts.append(yes, no);
  } else if (!asksForProject && e.id) {
    const yes = el("button", "act go", "approve");
    yes.onclick = () => act("decide", { escalation_id: e.id, decision: "approve" });
    const no = el("button", "act no", "reject");
    no.onclick = () => act("decide", { escalation_id: e.id, decision: "reject" });
    acts.append(yes, no);
  }

  // Reply. The button that makes this an Inbox rather than a set of switches:
  // most of what a PO actually needs to say is a sentence, and every other
  // control here can only say one of two things.
  const say = el("button", "act warn",
                 e.messages ? "reply · " + e.messages : "reply to Ordis");
  say.title = "write to Ordis about this item — queued for the next pulse";
  say.onclick = () => openCompose(e);
  acts.append(say);

  // Later is a snooze with an end on it. It stays in the Inbox — an Inbox you
  // can empty without deciding anything stops meaning what it says — but it
  // grays out and sorts to the back until the snooze expires.
  if (e.snoozed) {
    const wake = el("button", "act", "un-snooze");
    wake.title = "bring it back to the front of the Inbox now";
    wake.onclick = () => act("decide", { escalation_id: e.id, decision: "defer", snooze_hours: 0 });
    acts.append(wake);
  } else if (e.id) {
    const later = el("button", "act", "later");
    later.title = "snooze 8h — it stays in the Inbox, dimmed, because nothing was decided";
    later.onclick = () => act("decide", { escalation_id: e.id, decision: "defer", snooze_hours: 8 });
    acts.append(later);
  }

  // Re-ask. The answer to a stale question is not yes and not no — it is "you
  // asked me about last week's version, go and read it again". This is the
  // button that says that: it resolves the escalation as amended, puts the
  // story back in the grooming queue, and voids the attempts the old grooming
  // used up so the colony is actually allowed to try again.
  if (e.stale) {
    const again = el("button", "act warn", "re-ask");
    again.title = "send it back to Ordis to re-read the current brief and ask again if it still needs to";
    again.onclick = () => act("reask", { escalation_id: e.id });
    acts.append(again);
  }

  if (e.story_id) {
    // Named for what it opens. "story" read like a category label on the tile;
    // it is a link to the story's own timeline, criteria and events.
    const open = el("button", "act", "open story");
    open.title = "the full story: brief, acceptance criteria, every event on it";
    open.onclick = () => openStory(e.story_id);
    acts.append(open);

    // Dropping from here rather than only from the board, because the Inbox is
    // where you find out a story is not worth doing: the question that arrives
    // about a story is often the moment you decide it was never the work.
    const drop = el("button", "act no", "drop story");
    drop.title = "take the whole story off the board — closes its questions, cancels its tickets";
    drop.onclick = () => dropStory(e.story_id, e.story_title || "this story");
    acts.append(drop);
  }
  card.append(acts);
  return card;
}

// ── attachments ─────────────────────────────────────────────────────────────
//
// The upload happens on paste, not on send. A screenshot that appears in the
// composer as a picture you can look at and remove is a fact; one that is
// merely promised until you press send is a hope, and the failure mode — a
// silent 8MB refusal discovered an hour later — is the exact one this dashboard
// keeps trying to design out.
const ATTACH_MAX = 6;

async function upload(file) {
  const data = await new Promise((ok, no) => {
    const r = new FileReader();
    r.onload = () => ok(r.result);
    r.onerror = () => no(r.error);
    r.readAsDataURL(file);
  }).catch(() => null);
  if (!data) { toast("could not read that file", "bad"); return null; }
  try {
    const res = await fetch("/api/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Colony": "1" },
      body: JSON.stringify({ name: file.name || "pasted-image.png", data }),
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) { toast(out.detail || "that file was refused", "bad"); return null; }
    return out.file;
  } catch (err) {
    toast("the upload did not land — is the server still up?", "bad");
    return null;
  }
}

// One attachment, drawn the same way whether it is still in the composer or
// already sent. `onRemove` is what tells the two apart.
function attachChip(f, onRemove) {
  const chip = el("div", "att");
  if (f.kind === "image") {
    const img = el("img");
    img.src = "/api/attachment/" + encodeURIComponent(f.name);
    img.alt = f.label;
    img.title = "click to enlarge";
    img.onclick = () => { img.classList.toggle("open"); chip.classList.toggle("open"); };
    chip.append(img);
  }
  const n = el("span", "n", f.label);
  n.title = f.label + " · " + Math.max(1, Math.round(f.bytes / 1024)) + "KB";
  chip.append(n);
  if (onRemove) {
    const x = el("button", "link", "remove");
    x.onclick = onRemove;
    chip.append(x);
  }
  return chip;
}

// ── the composer ────────────────────────────────────────────────────────────
//
// Typing to Ordis, with the thing you are answering pinned above the box. The
// drawer goes wide for this one view: drafting a reply while re-reading a
// proposal in a 640px column means scrolling between the two halves of one
// thought.

async function openCompose(e, opts) {
  // A story-only thread has no Inbox item behind it. It exists because the only
  // way to say something about a story used to be to wait to be asked about it,
  // which made the conversation the colony's to start and never the PO's.
  const esc = (opts && opts.storyOnly) ? null : e.id;
  openDrawer(esc ? e.kind.replace("-", " ") + " · #" + esc : "story #" + e.story_id,
             "Reply to Ordis", { wide: true });
  // Opened straight off an Inbox tile there is no trail to walk back up, but
  // there is still an obvious "where did this come from": the story the question
  // is about. Offer that, since it is the one place the reply's context lives.
  if (!TRAIL.length && e.story_id) {
    TRAIL.push({ kind: "story", arg: e.story_id,
                 label: e.story_title || ("story #" + e.story_id) });
    renderTrail();
  }

  const box = $("d-body");
  box.replaceChildren();
  const wrap = el("div", "compose");

  // Filled by `load()` below, from the server's own reading of the story.
  //
  // It used to be pinned here, above the proposal and above every message. That
  // put it furthest from the box you type in, so on a thread of any length the
  // one line saying what is actually blocking the story was the one line you
  // had to scroll back up to find. `load()` now appends it to the end of the
  // thread instead: the conversation runs oldest to newest, and where the work
  // stands is the newest thing in it.
  const state = el("div", "state");
  state.style.display = "none";

  const proposal = el("div", "proposal");
  proposal.append(el("div", "r", e.reason));
  if (e.recommendation) {
    const rec = longText(e.recommendation, 520);
    rec.classList.add("rec");
    proposal.append(rec);
  }
  const meta = [];
  if (e.story_title) meta.push(e.story_title);
  if (e.project) meta.push(e.project + (e.project_source === "confirmed" ? "" : " (guess)"));
  if (e.est_tokens) meta.push(toks(e.est_tokens) + " tok");
  if (e.raised_at) meta.push("raised " + ago(e.raised_at));
  proposal.append(el("div", "meta", meta.join("  ·  ")));
  wrap.append(proposal);

  const thread = el("div", "thread");
  wrap.append(thread);

  const ta = el("textarea", "compose-box");
  ta.placeholder =
    "Write to Ordis the way you would in a terminal.\n\n" +
    "He reads this on the next pulse, answers in the same thread, and revises " +
    "his recommendation if you have changed it. He cannot approve, reject or " +
    "confirm a project from your reply — those stay yours.";
  wrap.append(ta);

  // Paste, drop or pick. All three end in the same place, and the shelf below
  // the box is the receipt.
  const files = [];
  const shelf = el("div", "shelf");
  const paint = () => {
    shelf.replaceChildren();
    shelf.style.display = files.length ? "" : "none";
    files.forEach((f, i) => shelf.append(attachChip(f, () => { files.splice(i, 1); paint(); })));
  };
  paint();
  wrap.append(shelf);

  const take = async (list) => {
    for (const file of list) {
      if (files.length >= ATTACH_MAX) { toast(ATTACH_MAX + " files is the limit", "bad"); break; }
      const got = await upload(file);
      if (got) { files.push(got); paint(); }
    }
  };
  ta.addEventListener("paste", (ev) => {
    const got = Array.from((ev.clipboardData && ev.clipboardData.files) || []);
    if (got.length) { ev.preventDefault(); take(got); }
  });
  // The board's own drag handler lives on <main>; this drawer is outside it, so
  // the two never see each other's drops.
  wrap.addEventListener("dragover", (ev) => { ev.preventDefault(); wrap.classList.add("over"); });
  wrap.addEventListener("dragleave", () => wrap.classList.remove("over"));
  wrap.addEventListener("drop", (ev) => {
    ev.preventDefault();
    wrap.classList.remove("over");
    take(Array.from((ev.dataTransfer && ev.dataTransfer.files) || []));
  });

  const foot = el("div", "foot");
  const send = el("button", "act go", "send to ordis");
  const picker = el("input");
  picker.type = "file";
  picker.multiple = true;
  picker.style.display = "none";
  picker.onchange = () => { take(Array.from(picker.files)); picker.value = ""; };
  const clip = el("button", "act", "attach a file");
  clip.title = "or just paste a screenshot into the box — Ctrl+V";
  clip.onclick = () => picker.click();
  const hint = el("span", "hint",
    "queued, not sent — nothing here spends a token until the next pulse. Ctrl+Enter sends.");
  foot.append(send, clip, picker, hint);
  wrap.append(foot);
  box.append(wrap);
  ta.focus();

  // By story wherever there is one. Asking for the escalation's thread was how
  // the conversation kept vanishing: Ordis closes a question he thinks he
  // answered, the next groom raises a new one about the same story, and the
  // drawer opened on an empty thread with six messages sitting one row away in
  // the ledger.
  const HEADS = { blocked: "blocked", waiting: "waiting on you",
                  moving: "moving", settled: "closed" };

  const load = async () => {
    thread.replaceChildren();
    const data = await getJSON("/api/thread?" +
      (e.story_id ? "story_id=" + e.story_id : "escalation_id=" + esc));

    const st = data.state;
    state.replaceChildren();
    state.style.display = st ? "" : "none";
    if (st) {
      state.dataset.level = st.level;
      state.append(el("div", "head", HEADS[st.level] || st.level));
      state.append(el("div", "line", st.headline));
      // Every open ask on the story, not just the one this drawer was opened
      // on. A story parked on a question raised three grooms ago is still
      // parked, and the tile you clicked to get here may not be the one holding
      // it.
      for (const a of st.asks || []) state.append(el("div", "ask", a.text));
    }

    const entries = data.entries || [];
    if (!entries.length) {
      thread.append(el("div", "empty", "nothing has been said about this yet"));
    }
    for (const m of entries) {
      if (m.kind === "question") {
        const q = el("div", "msg ask" + (m.closed_at ? " closed" : ""));
        if (m.esc_kind) q.dataset.kind = m.esc_kind;
        q.append(el("div", "who", "ordis asked · " + (m.esc_kind || "") + " · " + ago(m.at) +
          (m.closed_at ? " · closed " + (m.decision || "") : " · still open")));
        q.append(el("div", "bubble", m.body));
        thread.append(q);
        continue;
      }
      if (m.kind === "learning") {
        const l = el("div", "msg learned");
        l.append(el("div", "who", "learned · " + ago(m.at)));
        l.append(el("div", "bubble", m.body));
        // The learning as stored is a gist; the whole thought is the detail.
        // It used to be a `title` attribute — a tooltip you had to already know
        // was there, on the one part of the exchange still worth reading later.
        if (m.detail) l.append(longText(m.detail));
        thread.append(l);
        continue;
      }
      const row = el("div", "msg " + m.kind + (m.status === "unread" ? " pending" : ""));
      row.append(el("div", "who",
        (m.kind === "po" ? "you" : "ordis") + " · " + ago(m.at) +
        (m.status === "unread" ? " · not read yet" : "")));
      if (m.body) row.append(el("div", "bubble", m.body));
      const sent = jsonList(m.attachments);
      if (sent.length) {
        const strip = el("div", "shelf");
        for (const f of sent) strip.append(attachChip(f, null));
        row.append(strip);
      }
      thread.append(row);
    }
    // Last, so it reads as where the conversation has got to, and so it sits
    // directly above the box.
    thread.append(state);
  };
  load();

  const submit = async () => {
    const body = ta.value.trim();
    if (!body && !files.length) { toast("nothing to send", "bad"); return; }
    send.disabled = true;
    const ok = await act("reply", { escalation_id: esc, story_id: e.story_id, body,
                                    attachments: files });
    send.disabled = false;
    if (ok) {
      ta.value = "";
      files.length = 0;
      paint();
      load();
      toast("queued — Ordis reads it on the next pulse", "good");
    }
  };
  send.onclick = submit;
  ta.onkeydown = (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submit(); }
  };
}

// The sentinel for "none of the folders on this list". A constant rather than a
// magic string in three places, and one that cannot collide with a real folder
// name because a real folder name can never contain a newline.
const NEW_PROJECT = "\n<new>";

function projectSelect(current, opts) {
  const sel = el("select", "pick");
  sel.append(el("option", null, "— name the folder —"));
  sel.firstChild.value = "";
  // `STATE` may not have arrived yet — a deep-linked drawer opens before the
  // first snapshot does — and a picker with no options is a far better failure
  // than a drawer that renders nothing because one list was undefined.
  //
  // Sorted here rather than trusted from either source. /api/projects arrives
  // alphabetical, but the fallback is the working-tree scan, which is ordered
  // by *change recency* — a sensible order for "what did I touch today" and a
  // useless one for "find job-search in this list". Sorting at the point of
  // render means the picker reads the same way no matter which list filled it.
  // localeCompare, not <, so the folder starting with ＋ and any accented name
  // land where a person would look for them.
  const all = (ALL_PROJECTS.length
    ? ALL_PROJECTS
    : ((STATE && STATE.projects) || []).map((r) => r.project)
  ).slice().sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  for (const p of all) {
    const o = el("option", null, p);
    o.value = p;
    if (p === current) o.selected = true;
    sel.append(o);
  }
  if (opts && opts.allowNew) {
    const o = el("option", null, "＋ new project folder…");
    o.value = NEW_PROJECT;
    sel.append(o);
  }
  return sel;
}

// ── filing a story from here ────────────────────────────────────────────────
//
// The board could show work and move work but never *start* work: a story only
// existed because a Notion page did. This is the other door, and it is
// deliberately the same shape as the Inbox's project picker — including the
// "＋ new project folder…" branch — because "which folder is this?" is the same
// question whether you are answering it after the fact or up front.

function openNewStory() {
  // No `nav`: a back button that reopened this would be an empty form claiming
  // to be the one you were typing in. Same rule the reply drawer follows.
  const body = openDrawer("board", "new story");
  const wrap = el("div", "form");

  const title = el("input", "field");
  title.placeholder = "what needs doing";
  title.maxLength = 200;

  const brief = el("textarea", "field");
  brief.placeholder = "the brief — why it matters, what done looks like, anything the colony cannot see from the code";
  brief.rows = 7;

  const sel = projectSelect(null, { allowNew: true });
  const field = el("input", "field");
  field.placeholder = "new-folder-name";
  field.style.display = "none";
  sel.onchange = () => {
    const isNew = sel.value === NEW_PROJECT;
    field.style.display = isNew ? "" : "none";
    sel.style.display = isNew ? "none" : "";
    if (isNew) field.focus();
  };
  field.onkeydown = (ev) => { if (ev.key === "Escape") { sel.value = ""; sel.onchange(); } };
  const picker = el("div", "picker");
  picker.append(sel, field);

  const prio = el("select", "pick");
  for (const [v, label] of [[3, "low"], [2, "medium"], [1, "high"]]) {
    const o = el("option", null, label); o.value = String(v);
    if (v === 3) o.selected = true;
    prio.append(o);
  }

  wrap.append(el("label", null, "title"), title);
  wrap.append(el("label", null, "brief"), brief);
  wrap.append(el("label", null, "project"), picker);
  wrap.append(el("label", null, "priority"), prio);

  const go = el("button", "act go wide", "file it");
  go.onclick = async () => {
    if (!title.value.trim()) { toast("give it a title", "bad"); return; }
    const isNew = sel.value === NEW_PROJECT;
    let project = isNew ? field.value.trim() : sel.value;
    if (isNew && !project) { toast("name the new folder", "bad"); return; }

    // Two calls rather than one, on purpose: `create_story` refuses a folder
    // that does not exist, so the folder has to be real before the story names
    // it. Making the story create folders as a side effect would mean a typo in
    // this box silently becomes a new directory on disk.
    go.disabled = true;
    try {
      if (isNew) {
        const made = await act("confirm-project", { project, create: true });
        if (!made) return;
        // The picker's list was fetched once at load. A folder created a second
        // ago is not in it, and the very next thing that happens is a story
        // naming that folder.
        await fetch("/api/projects").then((r) => r.json())
          .then((p) => { ALL_PROJECTS = p.all || ALL_PROJECTS; }).catch(() => {});
      }
      const out = await act("story", {
        title: title.value, description: brief.value,
        project, priority: Number(prio.value) || 3,
      });
      if (out) {
        toast(out.message || "filed", "good");
        closeDrawer();
      }
    } finally {
      go.disabled = false;
    }
  };
  wrap.append(go);
  wrap.append(el("div", "dim",
    "Filed straight into the ledger — no Notion page behind it, and the sync "
    + "will not touch it. Leave the folder blank and the Inbox will ask."));

  body.replaceChildren(wrap);
  title.focus();
}

function renderMacros(c) {
  const box = $("macros");
  box.replaceChildren();

  // HALT. Deliberately the biggest control on the page.
  const halt = el("div", "macro");
  const btn = el("button", "big" + (c.halted ? " resume" : ""), c.halted ? "resume" : "halt");
  btn.onclick = () => {
    if (c.halted) { act("halt", { on: false }); return; }
    confirmThen("Halt all production? The pulse keeps beating and logging — it just stops spending.",
                () => act("halt", { on: true, reason: "halted from the dashboard" }));
  };
  halt.append(btn);
  halt.append(el("div", "why", c.halted
    ? "No new work will be dispatched. The heartbeat is still running, still logging, still syncing Notion."
    : "Stops every dispatch colony-wide. It cannot claw back a run already in flight — the honest promise is “no new work”."));
  box.append(halt);
  box.append(el("hr", "macro-rule"));

  // Beating out of turn. The scheduled task and this button are independent:
  // the task fires at :07 whatever happens here, so a beat forced at 1:37 sits
  // between the 1:07 and 2:07 beats rather than replacing either of them.
  const hb = el("div", "macro");
  hb.append(el("div", "lb", "heartbeat"));
  const beating = !!c.pulse_running;
  const r0 = el("div", "row");
  const nowb = el("button", "act go", beating ? "beating\u2026" : "pulse now");
  nowb.disabled = beating;
  nowb.title = "one full beat: sync, look, and wake if there is a reason";
  nowb.onclick = async () => {
    const out = await act("pulse", { wake: true });
    if (out) { toast("beat started — its row lands in the pulse log when it ends", "good");
               setTimeout(refresh, 1500); }
  };
  const tickb = el("button", "act", "tick only");
  tickb.disabled = beating;
  tickb.title = "the free half — sync, reap and look, but never wake";
  tickb.onclick = async () => {
    const out = await act("pulse", { wake: false });
    if (out) { toast("tick started — free, no wake", "good"); setTimeout(refresh, 1500); }
  };
  r0.append(nowb, tickb);
  hb.append(r0);
  hb.append(el("div", "why", beating
    ? "A beat is running. Nothing else may beat until it finishes."
    : "The scheduled task beats once an hour at :07. This runs one extra now and leaves that alone — the next automatic beat still comes at its own time."));
  box.append(hb);
  box.append(el("hr", "macro-rule"));

  // Allowance. Stored beside the sprint's designed budget, never on top of it.
  const band = c.allowance;
  const al = el("div", "macro");
  al.append(el("div", "lb", "token allowance"));
  // The bar measures the whole week now, not the distance travelled inside a
  // boost cap that no longer exists — so the empty part of it is the share of
  // the quota left for Jordan's own sessions, which is the number the dial is
  // actually trading against.
  const lo = c.allowance_min === undefined ? 0 : c.allowance_min;
  const hi = c.allowance_max === undefined ? 100 : c.allowance_max;
  const at = band.effective;
  const clamp = (v) => Math.max(lo, Math.min(hi, v));
  const bar = el("div", "boostbar");
  const fill = el("i"); fill.style.width = ((at - lo) / (hi - lo)) * 100 + "%";
  bar.append(fill); al.append(bar);
  al.append(el("div", "why", band.boost
    ? `${at}% of the week — ${band.base}% baseline, moved ${band.boost > 0 ? "+" : ""}${band.boost}.`
    : `${band.base}% of the week, as designed.`));

  // A dial, not a ratchet. This was three sizes of "up" (+5, +10, +25) against a
  // hard +25 ceiling, so the allowance could only climb, and an overshoot could
  // only be cleared to zero and rebuilt. It walks both ways now over the whole
  // range: 0% is a real setting — the colony stops spending without the
  // finality of HALT — and 100% is the PO deciding the week is the colony's.
  // -5 and +5 are the same size on purpose, so a mispress costs one press.
  const row = el("div", "row");
  const down = el("button", "act", "\u22125");
  down.title = `down to ${clamp(at - 5)}% of the week`;
  down.disabled = at <= lo;
  down.onclick = () => act("allowance", { allowance: clamp(at - 5) });
  const up = el("button", "act warn", "+5");
  up.title = `up to ${clamp(at + 5)}% of the week`;
  up.disabled = at >= hi;
  up.onclick = () => act("allowance", { allowance: clamp(at + 5) });
  row.append(down, up);
  const clr = el("button", "act", "clear");
  clr.disabled = !band.boost;
  clr.title = `back to the designed ${band.base}% baseline`;
  clr.onclick = () => act("allowance", { boost: 0 });
  row.append(clr);
  al.append(row);

  // And a box, because "I need 80% this week" is a thing you know directly and
  // counting nine presses to get there is arithmetic the page should do.
  const typed = el("div", "row");
  const num = el("input", "numbox");
  num.type = "number"; num.min = lo; num.max = hi; num.step = 1; num.value = at;
  num.title = `the colony's share of the weekly window, ${lo}\u2013${hi}%`;
  const send = () => {
    const v = Number(num.value);
    if (!isFinite(v)) { num.value = at; return; }
    act("allowance", { allowance: clamp(v) });
  };
  num.onkeydown = (ev) => { if (ev.key === "Enter") { ev.preventDefault(); send(); } };
  const setb = el("button", "act", "set");
  setb.title = "set the allowance to exactly this";
  setb.onclick = send;
  typed.append(num, el("span", "unit", "% of the week"), setb);
  al.append(typed);
  box.append(al);
  box.append(el("hr", "macro-rule"));

  // ── the upward direction ────────────────────────────────────────────────
  //
  // Its own switch, separate from HALT, and the separation is the point. HALT
  // means "spend nothing"; a comment on a Notion page is not a token. A halted
  // colony that also went silent upward would look broken to anyone reading
  // the board on their phone, when what it actually is is paused.
  //
  // Off holds the queue rather than dropping it: everything you queued while
  // it was off goes up in order when you turn it back on.
  const nw = el("div", "macro");
  nw.append(el("div", "lb", "writing to notion"));
  const ob = c.outbox || { waiting: 0, stuck: 0 };
  const on = c.notion_write !== false;   // a bool from /api/state, not the raw control string
  const r3 = el("div", "row");
  const sw = el("button", "act " + (on ? "no" : "go"), on ? "hold writes" : "resume writes");
  sw.title = on
    ? "stop sending upward — queued changes wait rather than disappear"
    : "send the queue upward again, oldest first";
  sw.onclick = () => act("notion-write", { on: !on });
  r3.append(sw);
  nw.append(r3);
  nw.append(el("div", "why", on
    ? (ob.waiting
        ? `${ob.waiting} change${ob.waiting === 1 ? "" : "s"} queued — the next pulse sends them.`
        : "Status changes, ticks and comments go up on the pulse that follows them.")
    : `Held. ${ob.waiting} change${ob.waiting === 1 ? "" : "s"} waiting, and nothing goes up until you resume.`));
  if (ob.stuck) {
    nw.append(el("div", "why", `${ob.stuck} gave up after repeated failures — check the pulse log for what Notion said.`));
  }
  box.append(nw);
  box.append(el("hr", "macro-rule"));

  // Housekeeping.
  const misc = el("div", "macro");
  misc.append(el("div", "lb", "housekeeping"));
  const r2 = el("div", "row");
  const rescan = el("button", "act", "rescan personas");
  rescan.title = "re-read the agency-agents install from disk";
  rescan.onclick = async () => {
    const out = await act("rescan", {});
    if (out) toast(`${out.total} personas · ${out.added.length} new · ${out.changed.length} changed`, "good");
  };
  const reload = el("button", "act", "reload page");
  reload.onclick = () => location.reload();
  r2.append(rescan, reload);
  misc.append(r2);
  box.append(misc);

  if (c.recent && c.recent.length) {
    box.append(el("hr", "macro-rule"));
    const log = el("div", "macro");
    log.append(el("div", "lb", "your last decisions"));
    for (const a of c.recent.slice(0, 5)) {
      const d = el("div", "why");
      d.textContent = `${hhmm(a.at)}  ${a.action}${a.target_id ? " #" + a.target_id : ""} — ${a.detail || ""}`;
      log.append(d);
    }
    box.append(log);
  }
}

// The tier column records what the tick DECIDED; `acted` records what the wake
// actually did. An hour stamped "wake" that cost nothing is a tick that
// escalated and then found its job list empty — and calling that a wake, in
// mint, next to a token count of zero, is the log arguing with itself.
function tierOf(p) {
  if (p.tier !== "wake") return p.tier;
  return p.acted ? "wake" : "tick";
}

// Distinct from a plain tick in the log, because an escalated hour is worth
// reading back and a clean one is not.
function tierKey(p) {
  if (p.tier !== "wake") return p.tier;
  return p.acted ? "wake" : "escalated";
}

function renderPulses(rows) {
  const box = $("pulses");
  box.replaceChildren();
  $("pulse-count").textContent = rows.length ? hhmm(rows[0].pulse_at) : "";
  if (!rows.length) { box.append(el("div", "empty", "no pulses yet")); return; }

  // Consecutive clean ticks roll up; a *missing* hour is drawn in coral. A
  // heartbeat monitor that only shows the beats it received cannot show a
  // stopped heart, which is the one thing it exists to show.
  const out = [];
  for (let i = 0; i < rows.length; i++) {
    const p = rows[i], prev = rows[i + 1];
    // A forced beat never rolls up into "clean x6". You asked for it by hand,
    // so you get to see it happened.
    const plain = p.tier === "tick" && (p.finding || "") === "clean" && !p.forced;
    if (out.length && out[out.length - 1].roll && plain) {
      out[out.length - 1].roll++; continue;
    }
    out.push({ p, roll: plain ? 1 : 0 });
    if (prev) {
      const gapH = (parseTs(p.pulse_at) - parseTs(prev.pulse_at)) / 36e5;
      if (gapH > 1.6) out.push({ gap: Math.round(gapH) });
    }
  }
  for (const item of out) {
    if (item.gap) {
      const row = el("button", "pulse gap");
      row.disabled = true;
      row.append(el("span", null, "—"), el("span", "tier", "miss"),
                 el("span", "find", `no pulse for ~${item.gap}h`), el("span"));
      box.append(row); continue;
    }
    const p = item.p;
    const row = el("button", "pulse" + (item.roll > 1 ? " rolled" : ""));
    row.dataset.tier = tierKey(p);
    row.append(
      el("span", null, hhmm(p.pulse_at)),
      el("span", "tier", tierOf(p)),
      el("span", "find", item.roll > 1
          ? `clean ×${item.roll}`
          : ((p.finding || "") + (p.forced ? " · forced" : ""))),
      el("span", "num", p.tokens ? toks(p.tokens) : ""),
    );
    row.title = p.has_detail ? "open the full account of this beat" : p.pulse_at;
    row.onclick = () => openPulse(p.id);
    box.append(row);
  }
}

// The forge reads bottom-up on purpose: drafts first (something is waiting on
// you), then candidates (something could be), then what the active ones earned.
// The server already sorts it that way; this only has to not undo it.
const DETECTOR_LABEL = {
  "repeat": "seen 3+ times",
  "recovery": "failed, then worked",
  "shortcut": "came in cheap",
  "po-correction": "you kept fixing it",
};

// Why a wake would refuse to spend right now, in the words the pulse uses, or
// null if it would run. The page already holds both numbers; the forge just had
// no reason to look at them until a draft could sit queued behind them.
function standdown() {
  const sp = STATE && STATE.sprint;
  if (!sp || !sp.usage || !sp.allowance) return null;
  const ceiling = sp.allowance.effective;
  const used = Number(sp.usage.seven_day_pct);
  if (!(used >= ceiling)) return null;
  return `week at ${used.toFixed(0)}% of the ${ceiling}% allowance`;
}

function renderForge(f) {
  const box = $("forge");
  const items = (f && f.skills) || [];
  const decaying = new Set((f && f.decaying) || []);
  box.replaceChildren();
  $("forge-count").textContent = f && f.tokens_saved ? toks(f.tokens_saved) + " saved" : "";

  if (!items.length) {
    box.append(el("div", "empty",
      "nothing yet — the forge proposes a skill once a procedure repeats"));
    return;
  }

  for (const s of items) {
    const card = el("div", "skill " + s.status);
    const head = el("div", "skill-head");
    head.append(el("span", "mono", s.name));
    head.append(el("span", "pill " + s.status, s.status));
    if (decaying.has(s.slug)) head.append(el("span", "pill bad", "decaying"));
    card.append(head);

    if (s.detector) {
      card.append(el("div", "dim tiny", DETECTOR_LABEL[s.detector] || s.detector));
    }
    if (s.summary) card.append(el("div", "dim", s.summary));
    if (s.trigger_when) card.append(el("div", "said", s.trigger_when));

    if (s.status === "active") {
      const n = s.wins + s.losses;
      card.append(el("div", "mono dim tiny",
        `${s.times_used} run(s) · ${n ? s.wins + "/" + n + " won · " : ""}` +
        `${toks(s.tokens_saved)} tok saved`));
    }

    const bar = el("div", "row-acts");
    if (s.status === "candidate") {
      if (s.draft_requested_at) {
        // "on the next wake" is true and useless when every wake is standing
        // down. A request that has been waiting since Tuesday because the week
        // is over its allowance should say so here, next to the button that
        // made it, rather than leaving the PO to infer it from the Spend panel.
        const held = standdown();
        const w = el("span", held ? "waiting bad" : "waiting",
                     held ? "requested — held: " + held
                          : "Ordis drafts it on the next wake");
        w.title = held ? "raise the allowance in Macros, or wait for the week to reset"
                       : "queued; it costs nothing until the wake runs";
        bar.append(w);
      } else {
        const b = el("button", "act go", "ask for a draft");
        b.title = "costs nothing now — the next wake writes it";
        b.onclick = () => act("draft-skill", { skill_id: s.id });
        bar.append(b);
      }
    } else if (s.status === "drafted") {
      const read = el("button", "act go", "read + promote");
      read.onclick = () => openSkill(s.id);
      bar.append(read);
    } else {
      const read = el("button", "act", "read");
      read.onclick = () => openSkill(s.id);
      bar.append(read);
    }
    if (s.status !== "candidate" || s.draft_requested_at) {
      const drop = el("button", "act no", s.status === "active" ? "retire" : "discard");
      drop.onclick = () => act("retire-skill", { skill_id: s.id });
      bar.append(drop);
    }
    card.append(bar);
    box.append(card);
  }
}

// The promotion gate. It is the only PO action in the dashboard that writes a
// file outside the ledger, so the draft is shown in full first — approving a
// procedure you have not read is the failure mode the gate exists to prevent.
async function openSkill(id) {
  const body = openDrawer("skill #" + id, "", { wide: true });
  let s;
  try { s = await getJSON("/api/skill?id=" + id); }
  catch { body.replaceChildren(el("div", "empty", "could not load skill " + id)); return; }

  $("d-eyebrow").textContent = `skill · ${s.status}` + (s.detector ? ` · ${s.detector}` : "");
  $("d-title").textContent = s.name;
  body.replaceChildren();

  const facts = el("dl", "kv");
  const add = (k, v) => { facts.append(el("dt", null, k), el("dd", null, v)); };
  add("slug", s.slug);
  add("proposed by", DETECTOR_LABEL[s.detector] || s.detector || "—");
  if (s.trigger_when) add("loads when", s.trigger_when);
  add("evidence", JSON.parse(s.evidence_runs || "[]").map((r) => "#" + r).join(", ") || "—");
  if (s.baseline_tokens) add("baseline", toks(s.baseline_tokens) + " tok / run");
  if (s.path) add("on disk", s.path);
  if (s.roles) add("attached to", JSON.parse(s.roles).join(", "));
  body.append(facts);

  if (s.summary) body.append(sectionBlock("why it was proposed", s.summary));
  body.append(sectionBlock("the draft", s.draft_md || "(nothing drafted yet)"));

  if (s.status === "drafted") {
    const wrap = el("div", "blk");
    wrap.append(el("div", "lb", "promote — this writes the file"));
    const who = el("input", "search");
    who.value = "ordis, investigator";
    who.placeholder = "roles to attach it to, comma separated";
    wrap.append(who);
    const bar = el("div", "row-acts");
    const go = el("button", "act go", "promote");
    go.onclick = async () => {
      go.disabled = true;
      const out = await act("promote-skill", { skill_id: id, roles: who.value });
      if (out) { toast(out.outcome || "promoted", "good"); closeDrawer(); }
      else go.disabled = false;
    };
    const no = el("button", "act no", "discard this draft");
    no.onclick = async () => {
      const out = await act("retire-skill", { skill_id: id, reason: "PO discarded the draft" });
      if (out) closeDrawer();
    };
    bar.append(go, no);
    wrap.append(bar);
    body.append(wrap);
  }
}

// ── the spend chart ───────────────────────────────────────────────
// This was a 34px sparkline over the last fourteen days: the right size for
// "is it going up", the wrong one for any question with a number in it. The
// grain now goes from an hour to a year, both shapes are available, and the
// value under the pointer is readable, which is the difference between a
// decoration and an instrument.
//
// The series comes from /api/spend rather than the snapshot, because the
// snapshot is one payload for eleven panels and there is no reason for the
// other ten to carry 48 hourly buckets. Which grain and which shape is
// localStorage, like the Files sort order — how you read a panel is not a
// decision about the colony, so it does not belong in the ledger.

const SVGNS = "http://www.w3.org/2000/svg";
const GRAIN_UNIT = { hour: "h", day: "d", week: "w", month: "mo", year: "y" };
let SPEND_GRAIN = localStorage.getItem("colony-spend-grain") || "day";
let SPEND_KIND = localStorage.getItem("colony-spend-kind") || "line";
let SPEND_SERIES = null;
let SPEND_WIDTH = 0;
// Where the window stops. `null` is the live view. It is deliberately not
// persisted: which grain you read the panel at is a habit, but a date you paged
// back to is a look you took once, and a dashboard that opens in July because
// that is where you left it is a dashboard that lies about the present.
let SPEND_END = null;
let SPEND_LAND = null;    // which bucket to sit on after a paging load

function svgEl(name, attrs) {
  const n = document.createElementNS(SVGNS, name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

// A round number at or above the top of the data, so the gridlines land on
// figures a person can hold in their head — 40k, not 38.7k.
function niceMax(v) {
  if (!(v > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}

// One bucket forward or back, `n` of them. Date does the calendar arithmetic —
// setMonth and setFullYear normalise overflow the same way the server's
// `_back` does, so 31 March minus a month is the same day on both sides.
function stepDate(d, grain, n) {
  const t = new Date(d.getTime());
  if (grain === "hour") t.setHours(t.getHours() + n);
  else if (grain === "day") t.setDate(t.getDate() + n);
  else if (grain === "week") t.setDate(t.getDate() + 7 * n);
  else if (grain === "month") t.setMonth(t.getMonth() + n);
  else t.setFullYear(t.getFullYear() + n);
  return t;
}

const pad2 = (n) => String(n).padStart(2, "0");
const wireDate = (d) => d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
const wire = (d) => wireDate(d) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
// The keys come back as "YYYY-MM-DD HH:MM". Date.parse would read that as UTC
// in some engines and local in others; splitting it is the way to be sure.
function fromKey(k) {
  const [d, t] = String(k).split(" ");
  const [Y, M, D] = d.split("-").map(Number);
  const [h, m] = (t || "0:0").split(":").map(Number);
  return new Date(Y, M - 1, D, h, m);
}

function windowEnd() {
  const pts = SPEND_SERIES && SPEND_SERIES.points;
  return pts && pts.length ? fromKey(pts[pts.length - 1].key) : new Date();
}

// A whole window at a time. The new window ends where the old one began, so the
// two overlap by exactly one bucket — the bucket you were looking at when you
// pressed the arrow stays on screen, which is what stops paging feeling like a
// jump cut.
function pageSpend(dir) {
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

async function loadSpend() {
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

function spendReadout(p) {
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
    // Off the live view the span alone is not enough — "this 30d" is a lie
    // about a window ending in July — so it names its two ends instead.
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

function drawSpend() {
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

  // The forward controls are dead on the live view rather than hidden: a button
  // that vanishes takes the layout with it, and you lose the affordance the
  // moment you most want to know it exists.
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
  // `var()` is not legal inside an SVG presentation attribute. `currentColor`
  // is, so the accent is set once on the element and inherited by everything
  // drawn into it.
  svg.style.color = "var(--violet)";

  // Horizontal rules first, under everything, with the value they stand for.
  // `toks` renders 0 as an em dash, which is right in a table cell and wrong on
  // an axis — an axis tick has a value even when the value is nothing.
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
    // through — but a zoomed page scales it, hence the ratio.
    const px = (ev.clientX - r.left) * (W / (r.width || W));
    hover(Math.max(0, Math.min(pts.length - 1, Math.floor((px - PL) / band))));
  };
  svg.onpointerleave = () => hover(-1);

  // The chart takes focus so it can be read without a mouse. The arrows walk a
  // bucket at a time and page the window when they run off the end, which makes
  // the whole ledger reachable from the keyboard rather than only the part the
  // date box happens to be pointing at.
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
  // before the arrow was pressed — the one the two windows share.
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
      ? `nothing in ${where} — ${side.join(", ")}`
      : s.live ? "nothing spent yet" : `nothing in ${where}`));
  }
}

for (const seg of ["spend-grain", "spend-kind"]) {
  $(seg).addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.grain) {
      SPEND_GRAIN = b.dataset.grain;
      localStorage.setItem("colony-spend-grain", SPEND_GRAIN);
      loadSpend();
    } else {
      SPEND_KIND = b.dataset.kind;
      localStorage.setItem("colony-spend-kind", SPEND_KIND);
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
  // A bare date floors to midnight, which at the hour grain would end the window
  // at the *start* of the day picked and show the day before it. 23:00 puts the
  // whole of that day inside the window; every coarser grain floors to the same
  // bucket either way, so one spelling serves all five.
  SPEND_END = v + " 23:00";
  SPEND_LAND = "end";
  loadSpend();
});

// A column resize changes the pixel width the chart was measured at. Only the
// width matters — redrawing on a height change would be a loop, since drawing
// is what sets the height.
if (window.ResizeObserver) {
  new ResizeObserver(() => {
    const w = $("spend-chart").clientWidth;
    if (w && w !== SPEND_WIDTH) drawSpend();
  }).observe($("spend-chart"));
}

function renderSpend(sp) {
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

// ── standby: browse by division ─────────────────────────────────────────────
// 270 personas is too many to scroll and too few to need paging. Divisions are
// how the agency-agents repo already organises them, so they are how you browse
// them: a closed accordion of departments, biggest first, opening in place.

function renderDivisions(roster) {
  const box = $("roster-divisions");
  box.replaceChildren();
  for (const d of roster.divisions) {
    const det = el("details", "div");
    det.open = openDivisions.has(d.division);
    det.addEventListener("toggle", () => {
      det.open ? openDivisions.add(d.division) : openDivisions.delete(d.division);
    });
    const sum = el("summary");
    const hired = d.people.filter((p) => p.hired).length;
    sum.append(el("span", "dn", d.division.replace(/-/g, " ")),
               el("span", "cn", hired ? `${hired} hired / ${d.n}` : String(d.n)));
    det.append(sum);
    const people = el("div", "people");
    for (const p of d.people) people.append(personaButton(p));
    det.append(people);
    box.append(det);
  }
}

function personaButton(p) {
  const b = el("button", "persona" + (p.hired ? " hired" : ""));
  b.append(el("span", null, p.emoji || "·"), el("span", "nm", p.name), el("span", "dv", p.hired ? "hired" : ""));
  b.title = p.description || "";
  b.onclick = () => openPersona(p.slug);
  return b;
}

// ── drawer ──────────────────────────────────────────────────────────────────

let ALL_PROJECTS = [];   // every folder with a PROJECT.md, for the confirm pickers

// A drawer can open another drawer — a beat lists the projects that moved, a
// story lists its tickets — and until now that was a one-way trip: the second
// view replaced the first, and the only way back was to close everything and
// find the beat again. So the drawer keeps a trail.
//
// Nothing at the call sites had to change. `openDrawer` is called synchronously
// at the top of every open* function, before the fetch, so "was a drawer already
// on screen when this one opened?" is exactly the question of whether the user
// stepped *down* into something or started fresh — and it can be asked here,
// once, instead of at each of the dozen places that open a drawer.
let TRAIL = [];        // where we came from, innermost last
let HERE = null;       // the view on screen, if it is one we know how to re-open
let GOING_BACK = false;

function drawerViews() {
  return { story: openStory, persona: openPersona, pulse: openPulse,
           project: openProject, agent: openAgent, completed: openCompleted,
           console: openConsole };
}

function renderTrail() {
  const b = $("d-back");
  const prev = TRAIL[TRAIL.length - 1];
  b.style.display = prev ? "" : "none";
  if (prev) {
    b.textContent = "← " + prev.label;
    b.title = "back to " + prev.label;
  }
}

function goBack() {
  const prev = TRAIL.pop();
  if (!prev) return;
  GOING_BACK = true;
  try { drawerViews()[prev.kind](prev.arg); } finally { GOING_BACK = false; }
}

function openDrawer(eyebrow, title, opts) {
  const d = $("drawer");
  const nav = opts && opts.nav;
  if (!d.classList.contains("on")) TRAIL = [];          // opened from the page: a fresh trail
  else if (HERE && !GOING_BACK) TRAIL.push(HERE);       // opened from a drawer: a step down
  // Note the two halves of that: a view without a `nav` cannot be *returned to*
  // — re-opening a half-written reply from a back button would be a lie about
  // what was preserved — but it can still have somewhere to go back *to*. That
  // used to be conflated, and the cost was the reply drawer: you opened a story,
  // clicked "reply to Ordis" to answer the thing you were reading, and the way
  // back to the story was to close everything and find it again.
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

function closeDrawer() {
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
  // other mode — otherwise the only way out is a button you have to aim at.
  if (SNAP) { setSnap(false); return; }
  closeDrawer();
});

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(res.status);
  return res.json();
}

function blk(label, ...nodes) {
  const w = el("div", "blk");
  w.append(el("div", "lb", label));
  w.append(...nodes);
  return w;
}
function sectionBlock(label, text) { return blk(label, el("pre", "detail", text)); }

// The ledger stores the two halves of a Notion checklist as JSON arrays. A
// story synced before migration 008 has neither, and a story whose page has no
// to-dos has empty ones — both read as "no checklist" here, and neither is an
// error worth showing.
function jsonList(raw) {
  if (!raw) return [];
  try { const v = JSON.parse(raw); return Array.isArray(v) ? v : []; } catch (_) { return []; }
}

// ── console ─---------------------------------------------------------------
//
// A shell, in a drawer, in the Ordis panel. Everything else on this page is a
// message left for a loop that reads it on the hour; this is a terminal that
// answers while you watch, with full tool access and no worktree between it and
// the tree. `console.py` carries the argument for why that is deliberate.
//
// The transcript is polled rather than pushed. It is not on the SSE snapshot on
// purpose: the snapshot fans out to every panel on the page and a chat that is
// only open sometimes should not be repainting the board while it waits for a
// shell command.

const CONSOLE_POLL_MS = 1500;

async function openConsole() {
  const body = openDrawer("ordis \u00b7 console",
                          "talking directly, with a shell",
                          { wide: true, nav: { kind: "console", arg: undefined,
                                               label: "console" } });

  let data;
  try { data = await getJSON("/api/console"); }
  catch { body.replaceChildren(el("div", "empty", "the console did not answer")); return; }

  body.replaceChildren();
  const wrap = el("div", "console");
  body.append(wrap);

  const bar = el("div", "bar");
  const cwd = el("div", "cwd");
  const meta = el("div", "meta");

  // Both dropdowns are built from what the server says the CLI accepts, not
  // from a list typed in here. A hard-coded menu is a menu that lies the first
  // time a model alias moves.
  const model = el("select", "pick");
  model.title = "which model answers the next message";
  for (const m of data.models) {
    const o = el("option", null, m.id);
    o.value = m.id; o.title = m.why;
    model.append(o);
  }
  const effort = el("select", "pick");
  effort.title = "how hard it thinks before it acts";
  for (const e of data.efforts) {
    const o = el("option", null, e.id);
    o.value = e.id; o.title = e.why;
    effort.append(o);
  }

  // Compact is a real turn, not a local button: it sends `/compact` into the
  // same session, so the summarising happens where the context actually lives.
  // That is also why it costs tokens and shows up in the tape like anything
  // else -- it is work, and work on this page is always billed out loud.
  const compact = el("button", "act", "compact");
  compact.title = "summarise this conversation so far and keep going in less context";
  const clear = el("button", "act", "clear");
  clear.title = "start a new conversation \u2014 he forgets everything above this line";
  bar.append(cwd, model, effort, el("div", "sep"), meta, compact, clear);
  wrap.append(bar);

  model.onchange = () => setOpts({ model: model.value });
  effort.onchange = () => setOpts({ effort: effort.value });

  async function setOpts(patch) {
    try {
      const res = await fetch("/api/console/options", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Colony": "1" },
        body: JSON.stringify(patch),
      });
      const out = await res.json().catch(() => ({}));
      if (!res.ok) { toast(out.detail || "could not change that", "bad"); return; }
      paint(out);
    } catch (_) { toast("the console did not answer", "bad"); }
  }

  wrap.append(el("div", "warn",
    "This is not a colony ticket. It runs with a real shell at the path above, "
    + "it can edit and run anything on this machine, and nothing here waits for "
    + "a pulse or a patch approval. It will not push, amend or force-push "
    + "without asking."));

  const tape = el("div", "tape");
  wrap.append(tape);

  const prompt = el("div", "prompt");
  const box = el("textarea", "console-box");
  box.placeholder = "what do you want changed? enter sends, shift+enter for a new line";
  const row = el("div", "row");

  // The command list is read off disk by the server -- the verified built-ins
  // plus whatever skills are actually installed -- so it cannot drift from
  // what the CLI would accept. Picking one pastes it at the cursor and hands
  // focus back; it never sends. Typing "/comp" and guessing the rest is the
  // thing this replaces.
  const cmds = el("select", "pick");
  cmds.title = "paste a slash command at the cursor";
  const head = el("option", null, "/ command…");
  head.value = "";
  cmds.append(head);
  for (const c of data.commands) {
    const o = el("option", null, c.name);
    o.value = c.name; o.title = c.why || c.name;
    cmds.append(o);
  }
  cmds.onchange = () => {
    const pick = cmds.value;
    cmds.value = "";
    if (!pick) return;
    const a = box.selectionStart, b = box.selectionEnd;
    const text = pick + " ";
    box.value = box.value.slice(0, a) + text + box.value.slice(b);
    box.selectionStart = box.selectionEnd = a + text.length;
    box.focus();
  };

  const hint = el("div", "hint");
  const send = el("button", "act go", "send");
  row.append(cmds, hint, send);
  prompt.append(box, row);
  wrap.append(prompt);

  // Redrawing the whole transcript on every poll would eat a half-typed
  // message, so the textarea is built once above and only the tape is
  // replaced. Same reason the scroll position is only forced when you were
  // already at the bottom: a poll should not yank you off the line you were
  // reading.
  function paint(st) {
    const stuck = tape.scrollHeight - tape.scrollTop - tape.clientHeight < 60;
    tape.replaceChildren();
    if (!st.turns.length) {
      tape.append(el("div", "empty",
        "nothing yet. this is a fresh session \u2014 he has no memory of the last one."));
    }
    for (const t of st.turns) {
      const turn = el("div", "turn");
      turn.dataset.role = t.role;
      turn.dataset.status = t.status;
      let who = t.role === "po" ? "you" : "ordis";
      who += " \u00b7 " + hhmm(t.at);
      if (t.status === "done" && t.role === "ordis") {
        if (t.elapsed_s) who += " \u00b7 " + Math.round(t.elapsed_s) + "s";
        if (t.tokens) who += " \u00b7 " + toks(t.tokens);
      }
      if (t.status === "failed") who += " \u00b7 failed";
      turn.append(el("div", "who", who));
      const text = t.status === "pending"
        ? "working\u2026"
        : (t.status === "failed" ? (t.error || "no reason recorded") : t.body);
      turn.append(el("div", "txt", text));
      tape.append(turn);
    }
    cwd.textContent = st.cwd;
    cwd.title = "the shell runs here";
    meta.textContent = toks(st.tokens) + " this chat \u00b7 $" + st.cost_usd.toFixed(2);
    const busy = st.busy;
    if (st.model) model.value = st.model;
    if (st.effort) effort.value = st.effort;
    send.disabled = busy;
    // Nothing to compact until there is a session to compact, and the CLI says
    // so in as many words ("Not enough messages to compact") rather than
    // failing -- but a button that spends tokens to be told that is a bad
    // button, so it stays off until the conversation exists.
    compact.disabled = busy || !st.resuming;
    clear.disabled = busy || !st.turns.length;
    box.disabled = false;
    send.textContent = busy ? "working\u2026" : "send";
    hint.textContent = busy
      ? "he is running \u2014 shell commands can take minutes"
      : (st.resuming ? "same session, he remembers everything above" : "new session");
    if (stuck) tape.scrollTop = tape.scrollHeight;
  }

  paint(data);
  box.focus();

  // The poll stops itself when the drawer closes or the body is replaced by
  // another view. There is no unmount hook on this page, so the node's own
  // presence in the document is the liveness check.
  const timer = setInterval(async () => {
    if (!wrap.isConnected || !$("drawer").classList.contains("on")) {
      clearInterval(timer);
      return;
    }
    try { paint(await getJSON("/api/console")); } catch (_) {}
  }, CONSOLE_POLL_MS);

  // `override` is how the compact button gets in: it is an ordinary message
  // with a fixed body, so it queues behind a running turn and gets refused the
  // same way, instead of being a second path into the same lock.
  async function fire(override) {
    const text = (typeof override === "string" ? override : box.value).trim();
    if (!text) return;
    send.disabled = true;
    compact.disabled = true;
    try {
      const res = await fetch("/api/console/send", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Colony": "1" },
        body: JSON.stringify({ text: text }),
      });
      const out = await res.json().catch(() => ({}));
      if (!res.ok) { toast(out.detail || ("refused (" + res.status + ")"), "bad"); send.disabled = false; return; }
      if (typeof override !== "string") box.value = "";
      paint(await getJSON("/api/console"));
    } catch (err) {
      toast("the console did not answer \u2014 is the server still up?", "bad");
      send.disabled = false;
    }
  }

  send.onclick = () => fire();
  compact.onclick = () => fire("/compact");
  box.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); fire(); }
  };

  clear.onclick = async () => {
    const st = await getJSON("/api/console").catch(() => null);
    if (st && st.turns.length && !confirm(
        "clear the console? he keeps no memory of these " + st.turns.length
        + " messages. the record of what they cost stays in the ledger.")) return;
    const res = await fetch("/api/console/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Colony": "1" },
      body: "{}",
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) { toast(out.detail || "could not clear", "bad"); return; }
    box.value = "";
    paint(await getJSON("/api/console"));
    box.focus();
  };
}

// ── story drawer ─-------------------------------------------------------------

async function openStory(id) {
  const body = openDrawer("story #" + id, "", { nav: { kind: "story", arg: id, label: "story #" + id } });
  const mine = HERE;
  let data;
  try { data = await getJSON("/api/story/" + id); }
  catch { body.replaceChildren(el("div", "empty", "could not load story " + id)); return; }

  const s = data.story;
  ALL_PROJECTS = data.projects || ALL_PROJECTS;
  $("d-eyebrow").textContent = `story #${s.id} · ${STATUS_LABEL[s.status] || s.status}`;
  $("d-title").textContent = s.title;
  mine.label = s.title;
  renderTrail();
  body.replaceChildren();

  // Before anything else, if this story is not moving, why. It used to be the
  // last row of the list below, which is where you look after you have already
  // worked out that something is wrong.
  if (s.blocked_reason) {
    const box = el("div", "blocker");
    box.append(el("div", "h", "not moving · " + (STATUS_LABEL[s.status] || s.status)),
               el("div", "b", s.blocked_reason));
    body.append(box);
  }

  const facts = el("dl", "kv");
  const add = (k, v) => { facts.append(el("dt", null, k), el("dd", null, v)); };
  add("project", s.project ? `${s.project} (${s.project_source})` : "none");
  add("priority", ["", "high", "medium", "low"][s.priority] || s.priority);
  add("notion", s.notion_status || "—");
  add("synced", s.notion_synced_at || "—");
  body.append(facts);

  // Say something about this story without waiting to be asked about it.
  //
  // Replying used to require an Inbox item to reply *to*, which quietly made
  // every conversation the colony's to open. Most of what a PO wants to say
  // about a story — this is the wrong folder, that criterion is stale, look at
  // this screenshot — arrives while he is reading the story, not while he is
  // reading a question about it. It becomes a ticket the moment it is sent,
  // same as any other reply.
  const talk = el("button", "act go", "reply to ordis about this story");
  talk.style.width = "100%";
  talk.title = "queued for the next wake — it shows up in the Ticket Queue straight away";
  talk.onclick = () => openCompose({
    story_id: s.id, story_title: s.title, project: s.project,
    project_source: s.project_source, reason: s.description || s.title,
  }, { storyOnly: true });
  body.append(talk);

  // The two gates that live on a story: name its folder, then dispatch it.
  const gate = el("div", "form");
  gate.append(el("label", null, "project"));
  const sel = projectSelect(s.project);
  const confirm = el("button", "act go", s.project_source === "confirmed" ? "re-confirm" : "confirm");
  confirm.onclick = async () => {
    if (!sel.value) { toast("pick a folder first", "bad"); return; }
    if (await act("confirm-project", { story_id: s.id, project: sel.value })) openStory(s.id);
  };
  const cell = el("div", "row"); cell.style.display = "flex"; cell.style.gap = "6px";
  cell.append(sel, confirm);
  gate.append(cell);

  gate.append(el("label", null, "dispatch"));
  const disp = el("button", "act go", "dispatch to build");
  disp.title = "queues one ticket. Nothing is spent until the next wake.";
  disp.onclick = async () => {
    const out = await act("dispatch", { story_id: s.id });
    if (out) { toast("queued — the next wake will build it", "good"); openStory(s.id); }
  };
  gate.append(disp);

  // Drop, or undo the drop. On the gate rather than off in a corner, because
  // "not doing this" is one of the three things you can decide about a story
  // and the other two are already here.
  gate.append(el("label", null, s.dropped_at ? "dropped" : "drop"));
  if (s.dropped_at) {
    const note = el("div", "row");
    note.style.display = "flex"; note.style.gap = "6px"; note.style.alignItems = "center";
    note.append(el("span", "dim mono", s.drop_reason || "no reason recorded"));
    const back = el("button", "act go", "restore");
    back.onclick = async () => { if (await act("restore", { story_id: s.id })) openStory(s.id); };
    note.append(back);
    gate.append(note);
  } else {
    const kill = el("button", "act no", "drop this story");
    kill.title = "off the board, questions closed, tickets cancelled — reversible";
    kill.onclick = () => dropStory(s.id, "“" + s.title + "”");
    gate.append(kill);
  }
  body.append(gate);

  // ── the checklist, both halves ──────────────────────────────────────────
  //
  // This is the block that stops the loop from asking about finished work. The
  // ledger keeps the ticked and unticked to-dos apart now, so the drawer can
  // show you what the colony believes is done — which is also the thing to
  // check first when a question looks like it is about last month.
  const done = jsonList(s.done_items), open = jsonList(s.open_items);
  if (done.length || open.length) {
    const list = el("div", "blk");
    for (const it of done) list.append(el("div", "dim", "✓  " + it));
    for (const it of open) list.append(el("div", null, "☐  " + it));
    body.append(blk(`to-dos · ${done.length} of ${done.length + open.length} done`, list));
  }

  // ── talking back to Notion ──────────────────────────────────────────────
  //
  // Everything here queues; nothing here sends. The button writes a row to the
  // outbox and the next pulse performs the HTTP, which is the same rule that
  // keeps every other control on this page free of network I/O — and it means
  // a Notion outage costs you a delay rather than a lost decision.
  if (s.notion_page_id) {
    const push = el("div", "form");
    const statuses = (STATE && STATE.controls && STATE.controls.notion_statuses) || [];

    push.append(el("label", null, "status"));
    const row = el("div"); row.style.display = "flex"; row.style.gap = "6px";
    const pick = el("select", "pick");
    pick.append(el("option", null, "— set status —"));
    pick.firstChild.value = "";
    for (const st of statuses) {
      const o = el("option", null, st);
      o.value = st;
      if (st === s.notion_status) o.selected = true;
      pick.append(o);
    }
    const setIt = el("button", "act go", "queue");
    setIt.title = "queued now, sent on the next pulse";
    setIt.onclick = async () => {
      if (!pick.value) { toast("pick a status first", "bad"); return; }
      if (await act("notion", { story_id: s.id, kind: "status", status: pick.value })) openStory(s.id);
    };
    row.append(pick, setIt);
    push.append(row);

    if (open.length) {
      push.append(el("label", null, "tick"));
      const row2 = el("div"); row2.style.display = "flex"; row2.style.gap = "6px";
      const item = el("select", "pick");
      for (const it of open) { const o = el("option", null, it); o.value = it; item.append(o); }
      const tick = el("button", "act go", "queue");
      tick.title = "ticks that checkbox on the Notion page";
      tick.onclick = async () => {
        if (await act("notion", { story_id: s.id, kind: "check", item: item.value, checked: true })) openStory(s.id);
      };
      row2.append(item, tick);
      push.append(row2);
    }

    push.append(el("label", null, "comment"));
    const say = el("textarea", "field wide");
    say.rows = 2;
    say.placeholder = "leave a note on the Notion page — posted as Ordis";
    const send = el("button", "act go", "queue comment");
    send.onclick = async () => {
      const text = say.value.trim();
      if (!text) { toast("nothing to say", "bad"); return; }
      if (await act("notion", { story_id: s.id, kind: "comment", text })) { say.value = ""; openStory(s.id); }
    };
    const wrap = el("div"); wrap.className = "wide";
    wrap.style.display = "flex"; wrap.style.flexDirection = "column"; wrap.style.gap = "6px";
    wrap.append(say, send);
    push.append(wrap);
    body.append(blk("notion", push));
  }

  if (s.description) body.append(sectionBlock("brief", s.description));
  if (s.acceptance_criteria) body.append(sectionBlock("acceptance criteria", s.acceptance_criteria));

  if (data.tickets.length) {
    // `.rows` rather than `.blk`: the list scrolls inside itself, capped at the
    // same height as the brief. A story that has been open for two weeks has
    // thirty tickets on it, and unbounded they push the runs table and the
    // timeline off the bottom of the drawer.
    const rows = el("div", "rows");
    for (const t of data.tickets) {
      const r = el("div", "kv");
      // “Tickets can be more inclusive, acceptance/approval can be a ticket as
      // well. Basically any call/choice can be made a ticket to ensure that it
      // has been understood.” Every answered escalation writes one, so the
      // record of what was decided lives beside the record of what was built.
      // It is stored as `chore` because `tickets.intent` has a CHECK that
      // SQLite cannot widen in place; `decided_esc_id` is what makes it a
      // decision, exactly as `po_message_id` is what makes a ticket a reply.
      const decision = !!t.decided_esc_id;
      const dd = el("dd", null, decision ? (t.findings || "decided")
                                         : `${t.status} · ${t.role}`);
      // When it was opened, and when it stopped. Without dates this list cannot
      // answer the first question anyone asks of it — is #19 from this morning
      // or from last week — and the ids only say what order things happened in.
      const life = stamp(t.created_at) +
                   (t.closed_at && t.closed_at !== t.created_at
                      ? "  ·  closed " + stamp(t.closed_at) : "");
      dd.append(el("div", "stamp", life));
      r.append(el("dt", null, `#${t.id} ${decision ? "decision" : t.intent}`), dd);
      rows.append(r);
      if (["staffed", "open"].includes(t.status)) {
        const c = el("button", "act no", "cancel ticket #" + t.id);
        c.onclick = async () => { if (await act("cancel", { ticket_id: t.id })) openStory(s.id); };
        rows.append(c);
      }
    }
    body.append(blk("tickets  ·  " + data.tickets.length, rows));
  }

  if (data.runs.length) {
    const box = el("div", "wrap-x");
    const t = el("table", "runs");
    const head = el("tr");
    ["run", "started", "took", "role", "status", "chargeable", "total", "$"]
      .forEach((h) => head.append(el("th", null, h)));
    t.append(head);
    for (const r of data.runs) {
      const tr = el("tr");
      // Two clocks, because they answer different questions. "started" places
      // the run in the week; "took" is how long the agent was actually working,
      // which is the number you want when a run cost more than you expected.
      const secs = r.ended_at && r.started_at
        ? (parseTs(r.ended_at) - parseTs(r.started_at)) / 1000 : null;
      tr.append(el("td", null, "#" + r.id),
                el("td", "dim", stamp(r.started_at)),
                el("td", "dim", secs == null ? "—" : clock(secs)),
                el("td", null, r.agent_role), el("td", null, r.status),
                el("td", "num", toks(r.chargeable_tokens || r.total_tokens)),
                el("td", "num dim", toks(r.total_tokens)), el("td", "num dim", usd(r.cost_usd)));
      t.append(tr);
    }
    box.append(t);
    body.append(blk("runs", box));
  }

  // The timeline is the point of the drawer: not "where is this" but "what did
  // we find out, and when". `story_events` exists to make this readable.
  const tl = el("div", "blk");
  tl.append(el("div", "lb", "timeline"));
  if (!data.events.length) tl.append(el("div", "empty", "nothing has happened to this story yet"));
  for (const e of data.events) {
    const row = el("div", "ev");
    row.dataset.kind = e.kind;
    const role = evRole(e);
    if (role) row.dataset.role = role;
    const spine = el("div", "spine");
    spine.append(el("div", "node"), el("div", "stem"));
    const right = el("div");
    // Named the way the reply drawer names them. "note" twice in a row is not
    // a timeline of a conversation; "you" and then "ordis" is.
    right.append(el("div", "when", `${e.at}  ·  ${EV_VOICE[role] || e.kind}` +
                                   `${e.tokens ? "  ·  " + toks(e.tokens) + " tok" : ""}`));
    right.append(el("div", "sum", e.summary));
    if (e.detail) right.append(longText(e.detail));
    row.append(spine, right);
    tl.append(row);
  }
  body.append(tl);
}

// ── completed drawer ────────────────────────────────────────────────────────
//
// Everything that took place between one deliverable and the next, in the order
// it took place. The story drawer answers "where does this stand"; this answers
// "what happened, and what came out of it", which is the question you have when
// you are trying to remember whether a thing is already built.
//
// `arg` is "kind:id" because the drawer trail stores one argument per view.
async function openCompleted(arg) {
  const [kind, rawId] = String(arg).split(":");
  const id = Number(rawId);
  const body = openDrawer(kind === "dispatch" ? "ticket #" + id : "story #" + id, "",
                          { nav: { kind: "completed", arg: arg, label: "completed " + id } });
  const mine = HERE;
  let d;
  try { d = await getJSON("/api/completed/detail?kind=" + encodeURIComponent(kind) + "&id=" + id); }
  catch { body.replaceChildren(el("div", "empty", "could not load this record")); return; }

  $("d-eyebrow").textContent = (kind === "dispatch" ? "delivered" : "filed " + (d.settled_as || ""))
                             + "  ·  " + d.ref;
  $("d-title").textContent = d.title || "";
  mine.label = d.title || d.ref;
  renderTrail();
  body.replaceChildren();

  const facts = el("dl", "kv");
  const add = (k, v) => { if (v) facts.append(el("dt", null, k), el("dd", null, v)); };
  add("completed", stamp(d.at));
  add("covers", (d.since ? stamp(d.since) : "the start of the story") + "  →  " + stamp(d.at));
  add("project", d.project || "none");
  add("agent", d.role);
  add("ticket", d.ticket_title);
  add("write scope", d.write_scope);
  add("patch", d.artifact_path);
  add("story now", d.story_status
    ? (STATUS_LABEL[d.story_status] || d.story_status) + (d.settled_as ? "  ·  filed " + d.settled_as : "")
    : "");
  body.append(facts);

  if (d.story_id) {
    const go = el("button", "act");
    go.style.width = "100%";
    go.textContent = "open story #" + d.story_id;
    go.onclick = () => openStory(d.story_id);
    body.append(go);
  }

  // What it produced. `done` and `skipped` are the agent's own account of the
  // acceptance criteria, and `skipped` is the half that matters: an unread
  // skip list is how a story gets called finished twice.
  const f = d.findings || {};
  if (f.summary) body.append(sectionBlock("what it delivered", f.summary));
  // `list` takes an array of plain strings and nothing else. `skipped` arrives
  // as {criterion, why} objects and `risks` and `learned` arrive as single
  // strings, so both are handled on their own terms below. Handing a string to
  // a for..of loop iterates its characters, which is how a 294-character risk
  // note rendered as 294 rows of one letter each.
  const list = (label, arr, mark) => {
    if (!Array.isArray(arr) || !arr.length) return;
    const box = el("div", "blk");
    for (const item of arr) box.append(el("div", null, mark + "  " + item));
    body.append(blk(label + "  ·  " + arr.length, box));
  };
  const skipped = (Array.isArray(f.skipped) ? f.skipped : []).map((sk) =>
    sk && typeof sk === "object"
      ? [sk.criterion, sk.why].filter(Boolean).join("  —  ")
      : String(sk));
  list("criteria met", f.done, "✓");
  list("skipped", skipped, "✗");
  // The commands the agent handed over rather than ran. They are half the story
  // of a skipped criterion, and the card that carried them is long gone by the
  // time anyone opens this panel.
  list("handed over to be run", (Array.isArray(f.needs_run) ? f.needs_run : [])
    .map((n) => n && typeof n === "object"
      ? [n.command, n.why].filter(Boolean).join("  —  ") : String(n)), "$");
  list("files touched", f.files, "·");
  if (f.risks) body.append(sectionBlock("look closely at", String(f.risks)));
  if (f.learned) body.append(sectionBlock("learned", String(f.learned)));
  if (d.brief) body.append(sectionBlock("brief", d.brief));
  if (d.acceptance_criteria) body.append(sectionBlock("acceptance criteria", d.acceptance_criteria));
  if (d.done_items && d.done_items.length) list("to-dos ticked", d.done_items, "✓");
  if (d.open_items && d.open_items.length) list("to-dos still open", d.open_items, "☐");
  if (d.work_order) body.append(blk("the work order it was given", longText(d.work_order, 700)));

  if (d.runs.length) {
    const box = el("div", "wrap-x");
    const t = el("table", "runs");
    const head = el("tr");
    ["run", "started", "took", "role", "status", "chargeable", "total", "$"]
      .forEach((h) => head.append(el("th", null, h)));
    t.append(head);
    for (const r of d.runs) {
      const secs = r.ended_at && r.started_at
        ? (parseTs(r.ended_at) - parseTs(r.started_at)) / 1000 : null;
      const tr = el("tr");
      tr.append(el("td", null, "#" + r.id), el("td", "dim", stamp(r.started_at)),
                el("td", "dim", secs == null ? "—" : clock(secs)),
                el("td", null, r.agent_role), el("td", null, r.status),
                el("td", "num", toks(r.chargeable_tokens || r.total_tokens)),
                el("td", "num dim", toks(r.total_tokens)), el("td", "num dim", usd(r.cost_usd)));
      t.append(tr);
    }
    box.append(t);
    body.append(blk("runs", box));
  }

  if (d.tickets.length) {
    const rows = el("div", "rows");   // scrolls in place, same as the story drawer
    for (const t of d.tickets) {
      const r = el("div", "kv");
      const decision = !!t.decided_esc_id;
      const dd = el("dd", null, decision ? (t.findings || "decided") : t.status + "  ·  " + t.role);
      dd.append(el("div", "stamp", stamp(t.created_at) +
        (t.closed_at && t.closed_at !== t.created_at ? "  ·  closed " + stamp(t.closed_at) : "")));
      r.append(el("dt", null, "#" + t.id + " " + (decision ? "decision" : t.intent)), dd);
      rows.append(r);
    }
    body.append(blk("tickets cut in this window  ·  " + d.tickets.length, rows));
  }

  // The whole conversation, in order: what you said, what Ordis said, what the
  // colony asked, what it decided, what it learned. Same rendering as the story
  // timeline, because it is the same kind of thing and a second visual language
  // for it would just be a second thing to learn.
  const tl = el("div", "blk");
  tl.append(el("div", "lb", "everything that happened  ·  " + d.timeline.length + " entries"));
  if (!d.timeline.length) {
    tl.append(el("div", "empty", "nothing was said between the last delivery and this one"));
  }
  for (const e of d.timeline) {
    const row = el("div", "ev");
    row.dataset.kind = e.ev_kind || e.kind;
    const role = doneRole(e);
    if (role) row.dataset.role = role;
    const spine = el("div", "spine");
    spine.append(el("div", "node"), el("div", "stem"));
    const right = el("div");
    right.append(el("div", "when", e.at + "  ·  " + doneVoice(e) +
      (e.tokens ? "  ·  " + toks(e.tokens) + " tok" : "")));
    right.append(el("div", "sum", e.body || ""));
    if (e.detail) right.append(longText(e.detail));
    if (e.kind === "question" && e.decision) {
      right.append(el("div", "meta", "you " + e.decision + "d it" +
        (e.closed_at ? " on " + stamp(e.closed_at) : "")));
    }
    row.append(spine, right);
    tl.append(row);
  }
  body.append(tl);
}

// One label per entry, in the voice it was said in. The event kinds keep their
// own word — "groomed", "staffed", "decided" — because those are the colony's
// own vocabulary and renaming them here would make the timeline and the ledger
// disagree about what happened.
function doneVoice(e) {
  if (e.kind === "po") return "you";
  if (e.kind === "ordis") return "ordis";
  if (e.kind === "learning") return "learned";
  if (e.kind === "question") return "ordis asked" + (e.esc_kind ? "  ·  " + e.esc_kind : "");
  return e.ev_kind || e.kind;
}

function doneRole(e) {
  if (e.kind === "po" || e.kind === "ordis" || e.kind === "learning") return e.kind;
  if (e.kind === "question") return "ask";
  if (e.ev_kind === "blocked" || e.ev_kind === "escalated") return "ask";
  return null;
}

// Which voice an event is in, if it is in one. Everything else — a Notion sync,
// a status filing, a groom that failed — is bookkeeping and stays grey, which
// is what makes the four coloured ones findable in a list of thirty.
const EV_VOICE = { po: "you", ordis: "ordis", learning: "learned", ask: "ordis asked" };

function evRole(e) {
  if (e.kind === "learning") return "learning";
  if (e.kind === "blocked" || e.kind === "escalated") return "ask";
  if (e.kind !== "note") return null;
  if (e.summary === "PO wrote to Ordis about this") return "po";
  if (e.summary === "Ordis answered the PO") return "ordis";
  return null;
}

// ── persona drawer ──────────────────────────────────────────────────────────
// The whole persona file, not a summary of it. What a persona refuses, what it
// insists on, what "done" means to it — those live in the markdown body, and
// they are exactly what you need to read *before* hiring someone rather than
// discover afterwards in a diff.

async function openPersona(slug) {
  const body = openDrawer("persona", slug, { nav: { kind: "persona", arg: slug, label: slug } });
  const mine = HERE;
  let p;
  try { p = await getJSON("/api/persona?slug=" + encodeURIComponent(slug)); }
  catch { body.replaceChildren(el("div", "empty", "could not read that persona")); return; }

  $("d-eyebrow").textContent = `${p.division} · standby`;
  $("d-title").textContent = `${p.emoji || ""} ${p.name}`.trim();
  mine.label = p.name;
  renderTrail();
  body.replaceChildren();

  const facts = el("dl", "kv");
  const add = (k, v) => { facts.append(el("dt", null, k), el("dd", null, v)); };
  add("slug", p.slug);
  add("division", p.division);
  if (p.vibe) add("vibe", p.vibe);
  add("file", p.path);
  if (p.hired) add("status", `hired as “${p.hired.role}”${p.hired.project ? " on " + p.hired.project : ""}`);
  body.append(facts);

  if (p.description) body.append(el("div", null, p.description));
  if (p.error) body.append(el("div", "empty", p.error));

  body.append(hireForm(p));

  // Every heading in the file becomes a block. Anything that fails to split
  // comes back as one section, which renders as the whole file — degrading to
  // "show me the text" is the right failure for a document viewer.
  for (const sec of p.sections) {
    if (!sec.body) { body.append(el("div", "lb mono dim", sec.title)); continue; }
    body.append(blk(sec.title || "criteria", el("pre", "detail tall", sec.body)));
  }
  if (!p.sections.length && p.body) body.append(sectionBlock("persona file", p.body));
}

function hireForm(p) {
  const wrap = el("div", "form");
  const role = el("input", "field");
  role.value = p.slug.split("/").pop();
  const proj = projectSelect(null);
  const model = el("select", "pick");
  for (const m of ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"]) {
    const o = el("option", null, m.replace("claude-", "")); o.value = m; model.append(o);
  }
  const write = el("input");
  write.type = "checkbox";
  const ceiling = el("input", "field");
  ceiling.type = "number"; ceiling.value = "400000"; ceiling.step = "20000"; ceiling.min = "10000";

  wrap.append(el("label", null, "role"), role);
  wrap.append(el("label", null, "project"), proj);
  wrap.append(el("label", null, "model"), model);
  const wline = el("div"); wline.style.display = "flex"; wline.style.gap = "7px"; wline.style.alignItems = "center";
  wline.append(write, el("span", "dim", "may edit files in that project (in a worktree, after you approve)"));
  wrap.append(el("label", null, "write"), wline);
  wrap.append(el("label", null, "ceiling"), ceiling);

  const go = el("button", "act go wide", p.hired ? "hire again" : "hire onto the colony");
  go.onclick = async () => {
    const out = await act("hire", {
      roster_slug: p.slug, role: role.value, project: proj.value || null,
      model: model.value, write_capable: write.checked,
      max_tokens_run: Number(ceiling.value) || 400000,
    });
    if (out) { toast(`hired ${role.value}`, "good"); closeDrawer(); }
  };
  wrap.append(go);
  return wrap;
}

// ── agent, pulse, project, patch drawers ────────────────────────────────────

async function openAgent(id) {
  const body = openDrawer("agent", "#" + id, { nav: { kind: "agent", arg: id, label: "agent #" + id } });
  const mine = HERE;
  let a;
  try { a = await getJSON("/api/agent/" + id); }
  catch { body.replaceChildren(el("div", "empty", "could not load that contract")); return; }
  $("d-eyebrow").textContent = `agent · ${a.status}`;
  $("d-title").textContent = a.role;
  mine.label = a.role;
  renderTrail();
  body.replaceChildren();

  const facts = el("dl", "kv");
  const add = (k, v) => { facts.append(el("dt", null, k), el("dd", null, v)); };
  add("persona", a.roster_slug || "—");
  add("project", a.project || "structural (no project)");
  add("model", a.model);
  add("writes", a.write_capable ? "yes — inside a worktree, after your approval" : "no");
  add("ceiling", toks(a.max_tokens_run) + " tok/run");
  add("hired", a.hired_at || "—");
  if (a.notes) add("notes", a.notes);
  body.append(facts);

  if (a.write_capable) body.append(scopeEditor(a));
  body.append(secretsEditor(a));
  for (const k of ["tools_allowed", "tools_denied", "read_scope"]) {
    if (a[k]) body.append(sectionBlock(k.replace("_", " "), a[k]));
  }
  if (a.write_scope && !a.write_capable) body.append(sectionBlock("write scope", a.write_scope));
  if (a.runs && a.runs.length) {
    const box = el("div", "wrap-x");
    const t = el("table", "runs");
    const head = el("tr");
    ["run", "started", "ticket", "status", "chargeable"].forEach((h) => head.append(el("th", null, h)));
    t.append(head);
    for (const r of a.runs) {
      const tr = el("tr");
      tr.append(el("td", null, "#" + r.id), el("td", "dim", stamp(r.started_at)),
                el("td", null, r.ticket_title || "—"),
                el("td", null, r.status), el("td", "num", toks(r.chargeable_tokens || r.total_tokens)));
      t.append(tr);
    }
    box.append(t);
    body.append(blk("runs", box));
  }
}

// The write scope started as one folder, derived from the project the agent was
// hired on, and nothing could change it afterwards. That is too narrow the
// moment a story's work crosses a folder boundary: a build agent scoped to
// `job-search/assisted-apply` skipped half its criteria because the files it
// needed sat in `job-search/job-radar`, and correctly said so rather than
// writing outside its contract. The scope is the PO's decision, so it belongs
// on the contract where he can see it and change it.
//
// The picker offers the folders the colony already knows about — every project
// with a PROJECT.md, plus the top-level folders that contain them — so picking
// the parent of a sub-project is one click. The server checks the folder exists
// under the projects root before it stores anything, because a typed path is
// still possible and a scope pointing nowhere is worse than a narrow one.
function scopeEditor(a) {
  let folders = (a.scope_folders || []).slice();
  const list = el("div");
  list.style.display = "flex";
  list.style.flexWrap = "wrap";
  list.style.gap = "6px";
  list.style.marginBottom = "8px";

  const save = el("button", "act go", "save scope");
  const draw = () => {
    list.replaceChildren();
    if (!folders.length) list.append(el("span", "dim", "no folder — this agent cannot build"));
    for (const f of folders) {
      const chip = el("span", "chip live");
      chip.style.textTransform = "none";
      chip.append(f + "/ ");
      const x = el("button", null, "\u00d7");
      x.title = "remove " + f;
      x.style.cssText = "background:none;border:0;color:inherit;cursor:pointer;padding:0 0 0 2px;font:inherit";
      x.onclick = () => { folders = folders.filter((v) => v !== f); draw(); };
      chip.append(x);
      list.append(chip);
    }
    save.disabled = !folders.length
      || folders.join("\u0000") === (a.scope_folders || []).join("\u0000");
  };

  const pick = el("select", "pick");
  const fill = () => {
    pick.replaceChildren();
    const head = el("option", null, "— add a folder —");
    head.value = "";
    pick.append(head);
    for (const p of (a.all_projects || []).slice().sort(
      (x, y) => x.localeCompare(y, undefined, { sensitivity: "base" }))) {
      const o = el("option", null, p);
      o.value = p;
      pick.append(o);
    }
  };
  fill();
  pick.onchange = () => {
    const v = pick.value;
    pick.value = "";
    if (v && !folders.includes(v)) { folders.push(v); draw(); }
  };

  const typed = el("input", "field");
  typed.placeholder = "or type a folder, relative to the projects root";
  typed.onkeydown = (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    const v = typed.value.trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    typed.value = "";
    if (v && !folders.includes(v)) { folders.push(v); draw(); }
  };

  save.onclick = async () => {
    const out = await act("scope", { agent_id: a.id, projects: folders });
    if (!out) return;
    toast(`${a.role} writes in ${folders.join(", ")}`, "good");
    a.scope_folders = folders.slice();
    draw();
  };

  const row = el("div");
  row.style.display = "flex";
  row.style.gap = "7px";
  row.style.alignItems = "center";
  row.append(pick, save);

  draw();
  return blk("write scope — the folders this agent may edit, inside its worktree",
             list, row, typed);
}

// Whether the checkout this agent works in contains the `.env` files. Off by
// default. It is here rather than folded into the scope editor because it is a
// different question: the scope is about writing, this is about reading a file
// git never puts in a worktree, which is why an agent once reported a key that
// is set as unset.
function secretsEditor(a) {
  let on = !!a.sees_secrets;
  const state = el("div", "dim");
  const btn = el("button", "act");
  const draw = () => {
    state.textContent = on
      ? "Its checkout gets a copy of every .env found in the projects it works "
        + "in. Values never reach a patch — the files are removed before the "
        + "diff is taken — but the agent can read them, so its report could "
        + "repeat one."
      : "Its checkout has no .env in it. The agent can tell you that a key is "
        + "not visible; it cannot tell you whether the key is set.";
    btn.textContent = on ? "hide the credentials" : "let it read the credentials";
    btn.classList.toggle("go", !on);
  };
  btn.onclick = async () => {
    const out = await act("secrets", { agent_id: a.id, on: !on });
    if (!out) return;
    on = !on;
    a.sees_secrets = on ? 1 : 0;
    toast(a.role + (on ? " can read .env" : " cannot read .env"), "good");
    draw();
  };
  draw();
  return blk("credentials — whether .env is copied into its checkout", state, btn);
}

async function openPulse(id) {
  const body = openDrawer("pulse #" + id, "", { nav: { kind: "pulse", arg: id, label: "the beat" } });
  let p;
  try { p = await getJSON("/api/pulse/" + id); }
  catch { body.replaceChildren(el("div", "empty", "could not load that beat")); return; }
  // "escalated" rather than "wake": the tick found a reason, a wake was
  // considered, and nothing was spent. The header used to say WAKE directly
  // above a body that said "spent nothing — the heartbeat is free".
  const escalated = p.tier === "wake" && !p.acted;
  $("d-eyebrow").textContent =
    `${tierOf(p)}${escalated ? " · escalated" : ""} · ${p.pulse_at}`;
  $("d-title").textContent = p.finding || "clean";
  body.replaceChildren();

  const facts = el("dl", "kv");
  const add = (k, v) => { facts.append(el("dt", null, k), el("dd", null, v)); };
  add("window", `${p.window_start} → ${p.window_end}`);
  add("took", (p.duration_ms / 1000).toFixed(1) + "s");
  add("spent", p.tokens ? toks(p.tokens) + " tok" : "nothing — the heartbeat is free");
  add("next", p.next_pulse_at || "—");
  if (p.anomalies) add("anomalies", String(p.anomalies));
  body.append(facts);

  if (p.detail) body.append(sectionBlock("what this beat saw", p.detail));
  else body.append(el("div", "empty", "this beat predates the long-form log (M3)"));

  if (p.changes && p.changes.length) body.append(blk("what moved on disk", movedList(p.changes)));
  body.append(sectionBlock("raw record", JSON.stringify(p.actions_json, null, 2)));
}

async function openProject(name) {
  const body = openDrawer("project", name, { nav: { kind: "project", arg: name, label: name } });
  let p;
  try { p = await getJSON("/api/project?name=" + encodeURIComponent(name)); }
  catch { body.replaceChildren(el("div", "empty", "could not read that folder")); return; }
  const st = p.state, repo = p.head || {};
  // The eyebrow read "undefined · undefined" on every project, because it was
  // pulling branch and sha off a row that has never carried either. They are a
  // property of the repo, not of the folder, so they come from the repo.
  $("d-eyebrow").textContent = repo.branch ? `${repo.branch} · ${repo.sha}` : "project";
  $("d-title").textContent = p.project;
  body.replaceChildren();

  // Every count here is a count of files that differ from one commit, and the
  // panel used to print the counts without ever naming it. The labels say what
  // each bucket is in words rather than in git's vocabulary — "untracked" is a
  // statement about git's index; "never committed" is a statement about the
  // file, and it is the one that explains folders full of changes nobody made.
  const kinds = p.kinds || {};
  const facts = el("dl", "kv");
  const add = (k, v, why) => {
    const dd = el("dd", null, v);
    if (why) dd.title = why;
    facts.append(el("dt", null, k), dd);
  };
  add("compared with", repo.sha
    ? `${repo.sha} on ${repo.branch}` + (repo.subject ? ` — "${repo.subject}"` : "")
    : "the last commit");
  if (repo.at) add("that commit landed", `${repo.at} (${ago(repo.at)})`);
  if (st) {
    add("in total", st.summary || "clean");
    add("edited", `${st.modified} file(s)`, kinds.modified);
    add("new, never committed", `${st.untracked} file(s)`, kinds.untracked);
    if (st.added) add("staged as new", `${st.added} file(s)`, kinds.added);
    if (st.deleted) add("deleted", `${st.deleted} file(s)`, kinds.deleted);
    if (st.touched_at) add("last written", `${st.touched_at} (${ago(st.touched_at)})`);
    if (st.commits_since) add("commits in window", String(st.commits_since));
  } else {
    add("in total", "clean — every file here matches that commit");
  }
  body.append(facts);

  if (p.commits && p.commits.length) {
    const list = el("div", "commits");
    for (const c of p.commits) {
      const sub_ = el("span", "sub", c.subject);
      sub_.title = `${c.sha} · ${c.author} · ${c.at} (${ago(c.at)})`;
      list.append(el("span", "sha", c.sha), sub_, el("span", "at", c.at));
    }
    body.append(blk("recent commits", list));
  }

  const holder = el("pre", "detail diff tall", "loading the diff…");
  body.append(blk("uncommitted changes", holder));
  try {
    const d = await getJSON("/api/diff?project=" + encodeURIComponent(name));
    paintDiff(holder, d.diff || "(nothing uncommitted)");
  } catch { holder.textContent = "could not read the diff"; }

  if (p.history && p.history.length > 1) {
    const list = el("div", "blk");
    for (const h of p.history) list.append(el("div", "why mono dim", `${h.seen_at}  ${h.summary || ""}`));
    body.append(blk("what the pulse has seen here", list));
  }
}

// "Projects that moved" was a list of names. It answered which folder and
// nothing else — not what changed in it, not where, not when, and above all not
// who, which is the question a PO asks first about a folder he never opened.
// Everything below was already being measured; none of it was being shown.
const KIND_WORD = { A: "added", M: "edited", D: "deleted", "??": "new", R: "renamed" };
const KIND_CLS = { A: "a", M: "m", D: "d", "??": "u", R: "m" };

function fileKind(xy) {
  const k = String(xy || "").trim();
  if (k === "??" || k === "?") return ["new", "u"];
  for (const ch of k) if (KIND_WORD[ch]) return [KIND_WORD[ch], KIND_CLS[ch]];
  return [k || "changed", ""];
}

// The delta against the previous beat, which is the whole reason the row is in
// the log. A null delta is a first sighting, not a zero: the folder had never
// been sampled, so "+14" would claim fourteen files appeared in that hour when
// what actually happened is that the colony looked for the first time.
function movedDelta(c) {
  const bits = [];
  for (const [k, word] of [["modified", "edited"], ["untracked", "new"],
                           ["added", "added"], ["deleted", "deleted"]]) {
    const d = c["d_" + k];
    if (d) bits.push((d > 0 ? "+" : "") + d + " " + word);
  }
  if (bits.length) return bits.join(", ") + " since the previous beat";
  // A beat from before migration 012 has no deltas because none were ever
  // recorded, which is a different fact from a folder that had never been seen,
  // and the two are told apart by whether the file list was stored at all.
  if (c.files == null) return "this beat predates the movement log";
  if (c.d_modified == null && c.d_untracked == null) return "first time this folder was sampled";
  if (c.commits_since) return "the same files — what moved was the commit";
  return "counts unchanged since the previous beat";
}

function movedList(changes) {
  const box = el("div", "moved");
  for (const c of changes) {
    const item = el("div", "item");

    const name = el("button", "name", c.project);
    name.onclick = () => openProject(c.project);
    item.append(name);
    // The colony writes to a folder through exactly one door — a patch the PO
    // approved — so it can say with certainty when a change was not its doing,
    // and that is the sentence worth putting on the row.
    const by = el("span", "who", c.moved_by === "colony" ? "the colony" : "not the colony");
    by.title = c.moved_by === "colony"
      ? "a patch you approved was applied into this folder during this beat"
      : "the colony did not write here — something else on the machine did";
    item.append(by);

    item.append(el("div", "delta", movedDelta(c)));

    const ctx = [];
    if (c.summary) ctx.push("now " + c.summary);
    if (c.head_sha) ctx.push("measured against " + c.head_sha
                            + (c.branch ? " on " + c.branch : ""));
    if (c.commits_since) ctx.push(c.commits_since + " commit(s) landed in the window");
    if (c.touched_at) ctx.push("last written " + c.touched_at + " (" + ago(c.touched_at) + ")");
    if (ctx.length) item.append(el("div", "ctx", ctx.join("  ·  ")));

    // Where it happened. The paths are the answer to "I can't find the
    // modification" — the counts never pointed at anything.
    const files = jsonList(c.files);
    if (files.length) {
      const list = el("div", "files");
      for (const f of files.slice(0, 8)) {
        const [word, cls] = fileKind(f.xy);
        const row = el("span", cls);
        // The project prefix is already the heading of this block; repeating it
        // on every path pushes the part that differs off the right-hand edge.
        const rel = String(f.path || "").startsWith(c.project + "/")
          ? String(f.path).slice(c.project.length + 1) : f.path;
        row.append(el("i", null, rel), el("b", null, word));
        list.append(row);
      }
      if (files.length > 8) list.append(el("span", null, `… and ${files.length - 8} more`));
      item.append(list);
    }
    box.append(item);
  }
  return box;
}

// A diff is the one place on this page where colour is not rationed: it is the
// whole point of the view. Line-prefix colouring only — no parser, no library.
function paintDiff(pre, text) {
  pre.replaceChildren();
  const lines = text.split("\n").slice(0, 3000);
  for (const line of lines) {
    const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") ? "file"
              : line.startsWith("@@") ? "hunk"
              : line.startsWith("+") ? "plus"
              : line.startsWith("-") ? "minus" : "";
    pre.append(el("span", cls, line + "\n"));
  }
  if (text.split("\n").length > 3000) pre.append(el("span", "hunk", "… truncated"));
}

async function openPatch(e) {
  const body = openDrawer("patch waiting", e.story_title || "write approval");
  body.replaceChildren(el("div", "dim", "reading the patch..."));

  let p;
  try {
    p = await getJSON("/api/patch?escalation_id=" + e.id);
  } catch (err) {
    body.replaceChildren(el("div", "bad", "could not read the patch: " + err.message));
    return;
  }
  body.replaceChildren();

  // The headline. Every number a decision to apply nine files actually turns
  // on, on one line, before any prose: how much of the tree moves, how much it
  // cost, and whether the run that produced it finished cleanly.
  const t = (p.stat && p.stat.total) || { files: 0, added: 0, removed: 0 };
  const chips = el("div", "meta");
  chips.append(tag(t.files + " file" + (t.files === 1 ? "" : "s")),
               tag("+" + t.added, "good"),
               tag("-" + t.removed, "bad"));
  if (t.binary) chips.append(tag(t.binary + " binary"));
  if (p.project) chips.append(tag(p.project + "/"));
  if (p.run) {
    if (p.run.chargeable_tokens) chips.append(tag(toks(p.run.chargeable_tokens) + " tok"));
    if (p.run.cost_usd) chips.append(tag(usd(p.run.cost_usd)));
    if (p.run.model) chips.append(tag(String(p.run.model).replace("claude-", "")));
    if (p.run.status && p.run.status !== "ok") chips.append(tag(p.run.status, "boosted"));
  }
  if (p.ticket && p.ticket.role) chips.append(tag(p.ticket.role));
  body.append(chips);

  // What the agent says it did. A claim, not a finding: the diff below is the
  // evidence. It goes first anyway, because it is the only part that can say
  // which acceptance criteria it believes it met, and reading a nine-file diff
  // without knowing what it was aiming at is reading noise.
  const r = p.report || {};
  if (r.summary) body.append(el("div", null, r.summary));

  if ((r.done || []).length) {
    const ul = el("ul", "crit");
    for (const c of r.done) ul.append(el("li", "met", c));
    body.append(blk("criteria it says it met", ul));
  }
  if ((r.skipped || []).length) {
    const ul = el("ul", "crit");
    for (const sk of r.skipped) {
      ul.append(el("li", "missed", (sk.criterion || "") + " — " + (sk.why || "")));
    }
    body.append(blk("skipped, and why", ul));
  }
  if (r.risks) body.append(sectionBlock("look closely at", String(r.risks)));

  // The decision, above the file list rather than under a thousand lines of
  // diff. Scrolling to the bottom to approve is a design that assumes you read
  // all of it; putting the buttons here assumes you read as much as you needed.
  const acts = el("div", "row");
  acts.style.display = "flex"; acts.style.gap = "7px"; acts.style.margin = "10px 0";
  const yes = el("button", "act go", "apply to the live tree");
  yes.onclick = () => confirmThen(
    "Apply this patch? It lands uncommitted in your working tree — the colony never commits.",
    async () => { if (await act("decide", { escalation_id: e.id, decision: "approve" })) closeDrawer(); });
  const no = el("button", "act no", "reject");
  no.title = "throws the worktree away and puts the story back to ready. The patch file survives.";
  no.onclick = async () => { if (await act("decide", { escalation_id: e.id, decision: "reject" })) closeDrawer(); };
  acts.append(yes, no);
  body.append(acts);

  // Per file, sorted by how much of it moves. A nine-file patch where eight
  // files gained a line and one was rewritten is a different review from nine
  // files each half rewritten, and the totals alone cannot tell them apart.
  const files = ((p.stat && p.stat.files) || []).slice()
    .sort((a, b) => (b.added + b.removed) - (a.added + a.removed));
  if (files.length) {
    const tbl = el("table", "filestat");
    for (const f of files) {
      const tr = el("tr");
      tr.append(el("td", "verb " + f.verb, f.verb),
                el("td", "path", f.path),
                el("td", "plus", f.binary ? "bin" : "+" + f.added),
                el("td", "minus", f.binary ? "" : "-" + f.removed));
      tbl.append(tr);
    }
    body.append(blk("what moves", tbl));
  }

  const facts = el("dl", "kv");
  facts.append(el("dt", null, "ticket"), el("dd", null, "#" + ((p.ticket || {}).id || "?")),
               el("dt", null, "patch"), el("dd", null, p.path || "not on disk"));
  if (p.run && p.run.started_at) {
    facts.append(el("dt", null, "built"), el("dd", null, when(p.run.started_at)));
  }
  body.append(facts);

  // And the diff. `paintDiff` caps at 3000 lines of its own; `truncated` is the
  // server saying the file was bigger than it was willing to send, which is a
  // different fact and has to be said separately or the page quietly implies
  // you have seen the whole change.
  if (p.error) {
    body.append(blk("the change", el("pre", "detail", p.error)));
  } else {
    const pre = el("pre", "detail diff tall");
    paintDiff(pre, p.diff || "");
    if (p.truncated) pre.append(el("span", "hunk", "... the rest was too large to send"));
    body.append(blk("the change", pre));
  }
}

// ── confirmations ───────────────────────────────────────────────────────────
// Only for the things that are hard to walk back: halting production, applying
// a patch, retiring an agent. Everything else is one click, because a gate you
// have to click twice for is a gate people learn to click through.

function confirmThen(question, fn) { if (window.confirm(question)) fn(); }

// ── dropping a story ────────────────────────────────────────────────────────
//
// Two prompts, and both of them earn their interruption. The first asks for a
// reason, because "why did we not do this" is the single most useful thing to
// have written down six months later and the only moment anyone knows the
// answer is now. Cancelling the reason cancels the drop — there is no way to
// drop a story silently, which is deliberate.
//
// The second asks whether to say so in Notion, and it is separate because it
// is a different kind of act: the first changes the colony's mind, the second
// changes a page other people may be reading. The server no-ops the Notion
// half for a story that has no page, so answering yes is always safe.
function dropStory(id, title) {
  const reason = window.prompt(
    "Drop " + title + "?\n\n" +
    "It comes off the board, its open questions close, and any ticket waiting on " +
    "it is cancelled. Nothing is deleted and you can restore it.\n\n" +
    "Why are you dropping it?", "");
  if (reason === null) return;
  // Shelved, not Archived. "Archived" is not an option on the Status select and
  // Notion answers an unknown option by *creating* it, so this dialog was one
  // OK away from inventing an eighth status on the board — and once the colony
  // stopped being allowed to send it, from refusing the drop outright with a
  // sentence about starting work, which is not what dropping a story is.
  const shelve = window.confirm(
    "Also set it to Shelved in Notion?\n\n" +
    "OK queues the change — it goes up on the next pulse. Cancel drops it here only.");
  act("drop", { story_id: id, reason: reason.trim(),
                notion_status: shelve ? "Shelved" : null });
}

// ── roster search ───────────────────────────────────────────────────────────

let rosterTimer = null;
$("roster-q").addEventListener("input", (e) => {
  clearTimeout(rosterTimer);
  const q = e.target.value;
  rosterTimer = setTimeout(async () => {
    const box = $("roster-results");
    const divs = $("roster-divisions");
    if (!q.trim()) { box.replaceChildren(); divs.style.display = ""; return; }
    divs.style.display = "none";
    const rows = await getJSON("/api/roster?q=" + encodeURIComponent(q) + "&limit=14").catch(() => []);
    box.replaceChildren();
    if (!rows.length) { box.append(el("div", "empty", "no persona matches")); return; }
    for (const p of rows) box.append(personaButton(p));
  }, 140);
});

// ── theme ───────────────────────────────────────────────────────────────────

// `?theme=ember` overrides the stored choice for this load only — it never
// writes localStorage. It exists so a theme can be inspected without clicking
// through the picker, which is also the only way to screenshot one headlessly.
const forced = new URLSearchParams(location.search).get("theme");
let saved = forced || localStorage.getItem("colony-theme") || "";
// A retired theme leaves a name in localStorage that no palette answers to any
// more: the attribute lands, nothing styles it, and the picker shows blank. Fall
// back to system rather than to a page dressed in half a theme.
if (saved && !$("theme").querySelector(`option[value="${CSS.escape(saved)}"]`)) {
  localStorage.removeItem("colony-theme");
  saved = "";
}
// A phone paints the status bar and the task-switcher card with `theme-color`,
// and a fixed one means fifteen palettes all launching behind the same slab of
// basalt. Read back off the computed style, so a hand-mixed ground is honoured
// the same as a named theme.
function paintThemeColor() {
  const meta = $("theme-color");
  if (!meta) return;
  const bg = getComputedStyle(document.documentElement)
    .getPropertyValue("--ground").trim();
  if (bg) meta.setAttribute("content", bg);
}

if (saved) document.documentElement.dataset.theme = saved;
$("theme").value = saved;
$("theme").onchange = (e) => {
  const v = e.target.value;
  if (v) { document.documentElement.dataset.theme = v; localStorage.setItem("colony-theme", v); }
  else { delete document.documentElement.dataset.theme; localStorage.removeItem("colony-theme"); }
  paintThemeColor();
  // Picking a theme drops any hand-mixed colours. They were sampled from the
  // palette you just left — an ember ground held over phosphor is not a third
  // theme, it is two halves of two — and a picker that appeared to do nothing
  // is worse than one that asks you to mix again. Named presets are untouched:
  // that is what saving one is for.
  if (Object.keys(VARS).length) { VARS = {}; applyVars(); saveAppearance();
                                  toast("hand-mixed colors cleared", null); }
  if (STATE) render(STATE);   // avatars are painted on canvas, so they re-paint
};
$("halt-banner-resume").onclick = () => act("halt", { on: false });

// ── appearance ──────────────────────────────────────────────────────────────
//
// Three dials, one drawer, and not a byte of it leaves the machine. Text size,
// the fifteen palette tokens, and where each tile sits — all localStorage, for
// the same reason the view menu is: how you read the page is not something the
// colony needs to know, and a page that phoned home about its font size would
// be a page you could not trust to be only a page.
//
// The palette editor is the part that needed a rule to be safe. Handing over
// fifteen colour pickers is handing over the ability to make the page
// unreadable in four clicks, so every relationship that has to hold shows its
// contrast ratio next to the swatch and goes coral when it breaks. Randomize
// obeys the same audit rather than rolling dice — see below.

const SCALE_KEY = "colony-scale", VARS_KEY = "colony-vars",
      PRESET_KEY = "colony-presets", LAYOUT_KEY = "colony-layout";

// The shipped default is 1.15 and not 1: at the design size the body step is
// 13px, which is right for a wallboard and small for a page you actually read.
const DEFAULT_SCALE = 1.15;

function readJSON(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key) || "null") || fallback; }
  catch (_) { return fallback; }
}

let SCALE = Number(localStorage.getItem(SCALE_KEY) || DEFAULT_SCALE) || DEFAULT_SCALE;
let VARS = readJSON(VARS_KEY, {});
let LAYOUT = readJSON(LAYOUT_KEY, {});

// Name, label, what it paints, and what it has to stay legible against. A row
// with no audit is a surface colour — it has no single relationship worth one
// number.
//
// The targets are the ones this design actually holds, measured off the
// twenty-four shipped palettes rather than copied off a checklist: ink 4.5 on
// panel, everything else 3. An accent on its own chip bed is a 10px uppercase
// label, which argues for 4.5 — but eight of the shipped themes sit between
// 3.55 and 4.4 there and none of them is hard to read, and an audit that opens
// by calling a third of the existing design broken is noise, not signal.
//
// The rows are grouped, and every one of them says what it paints, because a
// palette named after its hues is unusable as a control panel: `violet` is
// simultaneously Ordis, the Standby and Board titles, the focus ring and the
// modified-files bar, and a picker that reads "violet" tells you the one thing
// you already knew — the colour — and none of the four things that will move.
// So the four accents keep their meanings and stay the *source*, and everything
// downstream of them is now a token of its own that can be pinned separately.
const TOKEN_GROUPS = [
  { label: "surfaces",
    note: "The page and the cards on it. No contrast target of their own — they are what everything else is measured against.",
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
    note: "The accents, and the source of everything below. Each one says one thing everywhere it appears, which is why there are four and not fourteen. Change one here and every title and bar that borrows it follows — pin those separately below if you would rather they did not.",
    rows: [
      ["--amber",        "amber",     "yours: Inbox tiles, anything waiting on you", ["--panel", 3]],
      ["--amber-ground", "amber bed", "the bed an Inbox tile sits on",               ["--amber", 3]],
      ["--mint",         "mint",      "live work: running, healthy, go",             ["--panel", 3]],
      ["--mint-ground",  "mint bed",  "the bed a mint chip sits on",                 ["--mint", 3]],
      ["--coral",        "coral",     "anomaly: blocked, failed, halted, stop",      ["--panel", 3]],
      ["--coral-ground", "coral bed", "the bed a coral chip sits on",                ["--coral", 3]],
      ["--violet",       "violet",    "Ordis himself, and every focus ring",         ["--panel", 3]],
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
const BASE_TOKENS = TOKEN_GROUPS.slice(0, 3).reduce((a, g) => a.concat(g.rows.map((r) => r[0])), []);
const TOKENS = TOKEN_GROUPS.reduce((a, g) => a.concat(g.rows), []);

// ── colour arithmetic ───────────────────────────────────────────────────────

function hex2rgb(h) {
  h = String(h).trim().replace("#", "");
  if (h.length === 3) h = [...h].map((c) => c + c).join("");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) || 0);
}
function rgb2hex(r, g, b) {
  return "#" + [r, g, b].map((v) =>
    Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, "0")).join("");
}
function hsl2hex(h, s, l) {
  h = ((h % 360) + 360) % 360;
  s = Math.min(100, Math.max(0, s)) / 100;
  l = Math.min(100, Math.max(0, l)) / 100;
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return rgb2hex(f(0) * 255, f(8) * 255, f(4) * 255);
}
function lum(hex) {
  const [r, g, b] = hex2rgb(hex).map((v) => {
    v /= 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a, b) {
  const x = lum(a), y = lum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
// Walk lightness *away* from the background until the ratio clears. This is the
// one function that makes a random palette usable: a hue gets chosen for
// character, and its lightness is then whatever legibility demands — rather
// than choosing a colour and hoping it lands somewhere readable.
function toward(h, s, l, bg, target) {
  const dir = lum(hsl2hex(h, s, l)) > lum(bg) ? 1 : -1;
  for (let i = 0; i < 120 && contrast(hsl2hex(h, s, l), bg) < target; i++) l += dir;
  return hsl2hex(h, s, Math.min(100, Math.max(0, l)));
}

// ── applying ────────────────────────────────────────────────────────────────

// Reading a custom property off the root gives its *text*, and half of these
// are written as `color-mix(in oklab, var(--mint) 55%, var(--violet))` — a
// recipe, not a colour. So the browser is asked to cook it: a zero-sized probe
// takes `color: var(--token)`, and its computed colour is the answer. Chromium
// hands back `rgb(…)` for a plain colour and `color(srgb …)` for a mix, one in
// 0–255 and the other in 0–1, so both are parsed rather than one assumed.
const PROBE = document.createElement("span");
PROBE.style.cssText = "position:absolute;width:0;height:0;opacity:0;pointer-events:none";
document.body.append(PROBE);

function parseColor(text) {
  const nums = String(text).match(/-?[\d.]+(?:e-?\d+)?/g);
  if (!nums || nums.length < 3) return null;
  const v = nums.slice(0, 3).map(Number);
  return String(text).indexOf("srgb") >= 0 ? v.map((n) => n * 255) : v;
}

function tokenValue(name) {
  if (VARS[name]) return VARS[name];
  PROBE.style.color = "var(" + name + ")";
  const rgb = parseColor(getComputedStyle(PROBE).color);
  return rgb ? rgb2hex(rgb[0], rgb[1], rgb[2]) : "#808080";
}

function applyScale() {
  document.documentElement.style.setProperty("--ui-scale", String(SCALE));
}

function applyVars() {
  const root = document.documentElement.style;
  for (const pair of TOKENS) root.removeProperty(pair[0]);
  for (const name of Object.keys(VARS)) root.setProperty(name, VARS[name]);
  paintThemeColor();
}

function saveAppearance() {
  localStorage.setItem(SCALE_KEY, String(SCALE));
  localStorage.setItem(VARS_KEY, JSON.stringify(VARS));
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(LAYOUT));
}

// ── tile layout ─────────────────────────────────────────────────────────────

const MAIN_PANELS = ["ordis", "colony", "standby", "board", "completed", "files",
                     "spend", "macros", "pulse", "forge"];
// The sprint strip is one row of figures with no heading of its own; capping it
// would put a scrollbar on something already one line tall.
const CAPPABLE = ["inbox", "flight"].concat(MAIN_PANELS);

// Panels whose *contents* scroll rather than the panel itself, and the variable
// their ceiling lives in. Their heading stays put without needing sticky.
const SELF_SCROLL = { "inbox": "--inbox-max", "pulse": "--pulse-max",
                      "completed": "--done-max" };
const PANEL_LABEL = {
  inbox: "PO Inbox", flight: "Ticket Queue", ordis: "Ordis", colony: "Colony",
  standby: "Standby", board: "Board", completed: "Completed", files: "Files",
  spend: "Spend",
  macros: "Macros", pulse: "Pulse log", forge: "Forge",
};
const COL_LABEL = ["left", "middle", "right"];

// Read out of the markup before anything is moved, so "back to the designed
// layout" means the layout the page was built with rather than a second copy
// of it kept in sync by hand.
const HOME = {};
for (const [ci, col] of [...document.querySelectorAll("main > .col")].entries()) {
  for (const [i, node] of [...col.children].entries()) {
    if (node.dataset.panel) HOME[node.dataset.panel] = { col: ci, i: i };
  }
}

function panelNode(key) { return document.querySelector('[data-panel="' + key + '"]'); }

function applyLayout() {
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
    const max = (LAYOUT[key] || {}).max || 0;
    // Two panels already scroll inside their own body — the Inbox grid and the
    // pulse log — so capping the *section* would nest one scroller in another
    // and give the same list two bars. Their cap moves the inner ceiling.
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

// Read the order back out of the DOM and store *that*, so the indices stay
// dense however they were arrived at — whether they were arrived at by the
// arrows, by a column change, or by dragging a tile across the page. A move is
// then always "swap with the neighbour", never "insert at 2.5 and hope".
function captureLayout() {
  for (const [ci, col] of [...document.querySelectorAll("main > .col")].entries()) {
    let i = 0;
    for (const node of col.children) {
      const key = node.dataset && node.dataset.panel;
      if (!key) continue;
      LAYOUT[key] = Object.assign({}, LAYOUT[key], { col: ci, i: i++ });
    }
  }
}

function foldTile(key, on) {
  LAYOUT[key] = Object.assign({}, LAYOUT[key], { min: on });
  applyLayout(); saveAppearance();
}

// Clicking the title bar folds it. The heading is already the one part of a
// tile that is never content, which makes it the obvious handle and means no
// new control had to be added to nine panels. Two things it must not swallow:
// the buttons that live inside some headings (Board's "filed", Files' sort
// order), and a click while the board is in snap mode, where dragging a tile by
// its title is the whole interaction.
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

function moveTile(key, delta) {
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
//
// Dragging is the fastest way to say where a tile goes and the easiest thing to
// do by accident, so it is a mode with a door at both ends: you enter it from
// the drawer, and a bar at the bottom of the screen is the only thing on the
// page while it is on. The tiles wiggle for the same reason — it is the only
// signal that an ordinary click will now rearrange the page.
//
// A drop *inserts* rather than swaps. Swapping moves a second tile you never
// named; inserting pushes the rest of the column down, which is what dragging
// something into a list looks like everywhere else.

let SNAP = false, DRAG = null, LINE = null;

function setSnap(on) {
  SNAP = on;
  document.body.classList.toggle("snapping", on);
  for (const key of MAIN_PANELS) {
    const node = panelNode(key);
    if (node) node.draggable = on;
  }
  if (!on) endDrag();
}

function endDrag() {
  if (LINE && LINE.parentNode) LINE.remove();
  if (DRAG) DRAG.classList.remove("dragging");
  DRAG = null;
}

// The first tile whose middle is below the pointer is the one being pushed
// down; measuring against middles rather than edges means the line flips when
// you have passed half of a tile, not when you have cleared all of it.
function insertBefore(col, y) {
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
//
// Not dice. A palette rolled uniformly is unreadable roughly always, and — more
// quietly wrong — it breaks the four accents loose from their meanings: a
// "coral" that came out green stops saying *anomaly*. So the neutrals get a
// random hue, a random cast and a coin-flip between a dark and a light ground,
// while the four accents keep their hue *bands* and vary inside them. Every
// colour then has its lightness solved for the contrast it owes, which is the
// same audit the twenty-four shipped themes were held to.

function randomPalette() {
  const rnd = (a, b) => a + Math.random() * (b - a);
  const hue = rnd(0, 360);
  const dark = Math.random() < 0.62;
  const cast = rnd(6, 26);                      // how far the neutrals lean on the hue

  const ground = hsl2hex(hue, cast, dark ? rnd(6, 13) : rnd(92, 96));
  const panel  = hsl2hex(hue, cast * 0.8, dark ? rnd(12, 18) : rnd(98, 100));
  const sunk   = hsl2hex(hue, cast * 0.8, dark ? rnd(17, 23) : rnd(93, 96));
  const rule   = hsl2hex(hue, cast * 0.7, dark ? rnd(24, 30) : rnd(85, 89));
  const soft   = hsl2hex(hue, cast * 0.7, dark ? rnd(19, 24) : rnd(90, 93));

  // The chip bed is a tint of its own accent, sitting a hair off the panel, and
  // it is chosen *first*. Solving it the other way round cannot work on a light
  // theme: if the accent is only just clear of a near-white panel, no bed light
  // enough to belong on that panel can also be clear of the accent — the bed
  // walks to #ffffff and the pair still fails. Four thousand sampled palettes
  // said so before this was written that way.
  const bed = (h, sat) => hsl2hex(h, sat, dark ? 15 : 95);
  // An accent has two jobs — a label on its own bed, a heading on the panel —
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

function presets() { return readJSON(PRESET_KEY, {}); }
function writePresets(all) { localStorage.setItem(PRESET_KEY, JSON.stringify(all)); }

// A preset is the whole look and not just the colours: the same palette read at
// 130% and at 100% is two different designs, and restoring one without the
// other restores neither.
function savePreset(name) {
  name = (name || "").trim().slice(0, 40);
  if (!name) { toast("a preset needs a name", "bad"); return; }
  const all = presets();
  // The base is stored resolved, so the look is exact. The derived tokens are
  // stored only if they were pinned by hand — baking all of them in would make
  // every preset a palette where moving `--violet` no longer moves Ordis, which
  // is the behaviour the panel-title rows exist to make optional, not default.
  const full = {};
  for (const name of BASE_TOKENS) full[name] = tokenValue(name);
  for (const name of Object.keys(VARS)) full[name] = VARS[name];
  all[name] = { theme: document.documentElement.dataset.theme || "", scale: SCALE, vars: full };
  writePresets(all);
  toast("saved " + name, "good");
  openAppearance();
}

function applyPreset(name) {
  const p = presets()[name];
  if (!p) return;
  if (p.theme) {
    document.documentElement.dataset.theme = p.theme;
    localStorage.setItem("colony-theme", p.theme);
  } else {
    delete document.documentElement.dataset.theme;
    localStorage.removeItem("colony-theme");
  }
  $("theme").value = p.theme || "";
  SCALE = Number(p.scale) || DEFAULT_SCALE;
  VARS = Object.assign({}, p.vars || {});
  applyScale(); applyVars(); saveAppearance();
  if (STATE) render(STATE);
  openAppearance();
}

// ── the drawer ──────────────────────────────────────────────────────────────

function openAppearance() {
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
  //
  // Every swatch is repainted after every edit rather than only its own row,
  // because one colour is never one relationship: nudging `--panel` moves all
  // fifteen ratios measured against it, and a readout that only refreshed the
  // row you touched would be telling the truth about one number and stale about
  // the rest.
  const ratios = [];
  const swatches = [];
  const paint = () => {
    for (const w of swatches) if (document.activeElement !== w.node) w.node.value = tokenValue(w.name);
    for (const r of ratios) {
      const got = contrast(tokenValue(r.name), tokenValue(r.against));
      r.node.textContent = got.toFixed(1) + ":1";
      r.node.classList.toggle("fail", got < r.target);
      r.node.title = got.toFixed(2) + ":1 against " + r.against.slice(2)
                   + (got < r.target ? " — wants " + r.target + ":1" : "");
    }
  };

  const groups = [];
  for (const g of TOKEN_GROUPS) {
    const grid = el("div", "swatches");
    for (const [name, label, does, audit] of g.rows) {
      const row = el("div", "sw");
      const input = document.createElement("input");
      input.type = "color"; input.value = tokenValue(name);
      input.title = name + " — " + does;
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
    if (STATE) render(STATE);
    openAppearance();
  };
  const revert = el("button", "link revert", "revert");
  revert.title = "throw away every colour mixed here and show "
               + (document.documentElement.dataset.theme || "system") + " as written";
  revert.onclick = () => {
    VARS = {}; applyVars(); saveAppearance();
    if (STATE) render(STATE);
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
    box.append(el("div", "empty", "nothing saved yet — mix a palette above and name it"));
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
      where.title = "the strip across the top is fixed — it is the first thing you read";
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
// ── the manual ──────────────────────────────────────────────────────────────
//
// A snapshot of how the colony works, written for the PO, in the plainest
// language the subject allows.
//
//     "just make the info tab so i can see how allllllll systems work so i can
//      make changes where there are gaps ... I dont need you to update this
//      with every change, thats a waste of tokens, I will remove and re-make it
//      later when the time comes"
//
// So this is deliberately a **frozen document**, not a live view. It reads
// nothing from the ledger and it will drift as the code changes. The date below
// is the promise it makes: everything here was true on that day, and nothing
// checks it afterwards. When it is wrong, delete it and write it again — do not
// patch it a line at a time.
//
// Data rather than markup because the page has no innerHTML anywhere in it and
// this is not the file to start.
const MANUAL_AS_OF = "2026-08-22";

const MANUAL = [

{ h: "What this program is",
  p: ["Colony Dash runs a small team of Claude agents on the projects in D:\\ALL STUFF\\PROJECTS. You write the projects down in Notion. Once an hour this program reads Notion, looks at your disk and your token budget, and decides whether there is anything worth doing. If there is, it does one small piece of it and asks you to approve the result.",
      "Everything it has ever done is written to one file: colony-dash/.colony/ledger.db. This page is a view of that file and nothing else. If something on screen looks wrong, the ledger is the thing to check, and the SQL console is the way to check it."],
  dl: [["You", "The Product Owner. You decide what gets built, when a project is finished, and whether any tokens are spent. Four things cannot happen without you clicking a button."],
       ["Ordis", "The Scrum Master. It reads your briefs, drafts acceptance criteria, proposes who to hire, and brings you questions. It never decides what to build and it never decides that something is finished."],
       ["The agents", "Hired one at a time for one job. Each gets one folder it may write to and is shut down when the job ends."]] },

{ h: "The hourly cycle",
  p: ["This is the part worth understanding first, because almost everything else hangs off it."],
  dl: [["pulse", "The hourly event. One row in the pulses table, every hour, whether or not anything happened. A missing row means the schedule is broken, and that is the only thing a missing row can mean."],
       ["tick", "The free half of a pulse. Plain Python, no model, zero tokens. It syncs Notion, reads your usage figures, scans the project folders, and decides whether this hour is worth spending on. Most hours it decides no, and that is the design working."],
       ["wake", "The paid half. Ordis actually runs. This happens only when the tick found a reason and there is a job in the queue. A wake that finds its job list empty stands down and costs nothing."],
       ["beat", "Just a word for one pulse. Nothing in the code knows it."]],
  note: "A pulse labelled tick cost you nothing. A pulse labelled wake spent tokens. If a row says wake and shows zero tokens, the tick escalated and the wake then found nothing to do." },

{ h: "The words for work",
  dl: [["story", "One project. It comes from one row in your Notion database, and the Notion page is the source of truth for what it says. The colony copies it; it does not own it."],
       ["ticket", "One job on one story. Grooming is a ticket, building is a ticket, answering your reply is a ticket. Tickets are what actually get handed to an agent."],
       ["run", "One execution of one agent against one ticket. This is where tokens are spent, and every run records what it cost."],
       ["groom", "Reading a brief and turning it into a numbered list of acceptance criteria, plus a list of what the brief does not say. This is the step that decides whether a story can be built at all."],
       ["build", "Writing the code. It happens inside a throwaway git worktree, never in your real folder, and it produces a patch you review."],
       ["harvest", "Collecting the result of a finished run into the ledger. An unharvested run is one that finished while nothing was watching."],
       ["sprint", "One week of budget. It runs Friday 05:00 to Friday 05:00 because that is when your Anthropic allowance resets, not because a week starts on a Friday."]] },

{ h: "The words for decisions",
  dl: [["card", "One question in your Inbox. Called an escalation in the code. Every card has buttons, and nothing behind it moves until you press one."],
       ["gate", "A point where the loop stops and waits for you. There are four of them, listed further down."],
       ["dismiss", "The x on a card. It closes the question without answering it, and the ledger records that you gave no answer rather than pretending you gave one."],
       ["stale", "A card whose story has been edited in Notion since the question was written. The question may no longer make sense, so it is set aside rather than answered."]] },

{ h: "The words for people",
  dl: [["roster", "270 personas on file. Job descriptions, not employees. Nobody on the roster costs anything."],
       ["agent", "A persona that has actually been hired, given a role name and a write scope. Only agents can be handed tickets."],
       ["standby", "A hired agent with nothing to do. It stays on the books and costs nothing while idle."],
       ["write scope", "The one folder an agent may modify. It is set when the agent is hired and cannot be widened by the agent."],
       ["skill", "A reusable instruction file the colony writes for itself. Candidates appear in the Forge panel. Drafting one costs tokens, so it waits for you."]] },

{ h: "The words for money",
  dl: [["token", "The unit of everything. Dollars are shown next to tokens but tokens are what the budget is kept in."],
       ["chargeable tokens", "Input plus output plus cache writes. This is the number that counts against your allowance, and it is larger than the output count you might expect."],
       ["allowance", "The share of your weekly Anthropic window the colony is allowed to use. The sprint sets a baseline and you can move it up or down for one week."],
       ["ceiling", "A per-job estimate. When a build looks like it will cost more than the ceiling, the loop stops and asks you before spending it."],
       ["halt", "The stop switch. No new work starts. Anything already running finishes, because the colony cannot kill a child process mid-sentence. The dashboard keeps updating so you can see what is going on."]] },

{ h: "The words for places",
  dl: [["Notion", "Where you write projects. The colony reads every row once an hour. It only ever writes to Notion when you press a button that says it will."],
       ["ledger", "colony-dash/.colony/ledger.db. Every story, ticket, run, card, and pulse, forever. Nothing is deleted."],
       ["project folder", "One directory under D:\\ALL STUFF\\PROJECTS. A story has to be matched to one before any writing can happen, and you confirm the match yourself."],
       ["worktree", "A temporary git checkout where a build happens. Your real folder is untouched until you approve the patch."],
       ["patch", "The diff a build produced. Approving it copies the files into your working tree, uncommitted. The colony never commits and never pushes."]] },

{ h: "The lanes on the board",
  p: ["A story sits in exactly one lane. The lane is the colony's own idea of where the work stands, and it is separate from the Status you set in Notion."],
  dl: [["backlog", "Seen, and the Notion row says In Progress, so it is work you want. Nothing has been read closely yet. Leaves when a wake grooms it."],
       ["needs criteria", "Waiting for a groom. Same as backlog in practice; it is where a story lands when its criteria have been cleared and it needs reading again."],
       ["needs info", "A groom ran and could not finish. The brief does not say something the writer needs. There will be a card in your Inbox with the specific question."],
       ["po review", "Ordis drafted acceptance criteria and is waiting for you to approve them. This is gate two. Nothing gets built from unapproved criteria."],
       ["ready", "You approved the criteria. The story can be staffed and built. It leaves when an agent is hired and dispatched."],
       ["running", "An agent is working on it right now."],
       ["delivered", "You approved a patch and the files are in your working tree, uncommitted. This does not mean the project is finished. If you then add more to the Notion page, the story goes back to needs criteria on the next pulse and the work continues."]],
  note: "Finished is a separate thing entirely, and only you can set it. Set the Notion Status to Done or Shipped, or press the button in the story drawer. Those stories leave the board and appear under done, shelved, or not started." },

{ h: "The life of a story, start to finish",
  ol: ["You write a page in the Notion database and set its Status to In Progress. Nothing else you set matters to the loop; In Progress is the only word that means work on this.",
       "Within the hour, a tick reads it and creates a story in the backlog. If the colony cannot tell which project folder it belongs to, it raises a card asking you. That is gate one, and it exists because the folder is what later authorizes writing to disk.",
       "A wake grooms it. Ordis reads the whole brief and either drafts acceptance criteria or writes down exactly what is missing. Either way it costs tokens and either way it produces a card.",
       "You approve the criteria. That is gate two. The story becomes ready. If the criteria are wrong, reject them and the story goes back for another read with the attempt counter reset.",
       "A wake proposes someone to hire from the 270 personas, with a role name and a write scope. That is gate three. Rejecting is a real answer and the next pulse proposes someone else.",
       "The agent builds, in a worktree, against the criteria you approved. If it looks like it will cost more than the ceiling, you get asked first.",
       "You review the patch and approve or discard it. That is gate four. Approving copies the files into your working tree, uncommitted, and the story becomes delivered. You commit it yourself.",
       "If the project is not finished, add the next part to the same Notion page. The next pulse notices the page changed, clears the old criteria, and puts the story back in needs criteria. The cycle repeats on the same story.",
       "When you are actually finished, set the Notion Status to Done or Shipped. Only then does anything call it done."] },

{ h: "The four gates",
  p: ["These are the four places the loop stops and waits for you. Everything else it does on its own."],
  ol: ["Which project folder does this story belong to? Answering this is what turns a guess into a permission.",
       "Are these acceptance criteria right? Nothing is built from criteria you have not approved.",
       "Should I hire this person for this job, with this write scope?",
       "Here is the patch. Apply it or throw it away?"],
  note: "There is a fifth stop that is not a gate: if a job is going to cost more than its ceiling, you are asked to approve the spend." },

{ h: "What the colony may never do",
  ul: ["Write outside D:\\ALL STUFF\\PROJECTS, ever.",
       "Write to any folder other than the one named by the ticket it is working on.",
       "Write to disk at all before you approve the patch.",
       "Touch .env files, credentials, or anything inside .git.",
       "Run git commit, git push, force-push, or delete a branch.",
       "Move a story into a lane that reads as finished, or write a Status back to Notion. Ending things is yours alone.",
       "Spend anything while production is halted."] },

{ h: "Things that look wrong, and what they actually mean",
  dl: [["The pulse log has a gap", "The hourly job did not run. It is a Windows Task Scheduler task called Colony Dash Pulse. Run python -m colony schedule --show to see its state, or python -m colony schedule to reinstall it. A gap is never the colony deciding to skip an hour; every hour writes a row."],
       ["A pulse says wake but spent nothing", "The tick found a reason to escalate, then the wake looked at its job list and found it empty. The drawer labels these escalated. They are free."],
       ["Acceptance criteria appeared and you do not know why", "Open the story and read its history. The groomed event holds Ordis's own summary of what it understood, and the full criteria are the detail underneath it. The ticket behind it holds the raw answer, including what Ordis thought was missing. If the reasoning is not good enough, reject the criteria; that sends it back for a fresh read rather than arguing with the old one."],
       ["A story says delivered but is not finished", "Delivered means one patch was approved, nothing more. Add the next part to the Notion page and the story comes back automatically."],
       ["Nothing is being built", "Check, in this order: is production halted, is the weekly allowance already spent, is there a card in the Inbox waiting on you, and is any story actually in ready. A story in po review is waiting for you, not for the colony."],
       ["A number looks stale", "The token figures come from the tray app's cache file, read fresh on every refresh. If the strip says cache stale, the tray app has stopped and the number on screen is old."]] },

{ h: "Commands worth knowing",
  dl: [["python -m colony pulse", "Run a pulse right now instead of waiting for the hour."],
       ["python -m colony pulse --dry-run", "Show what a pulse would do without writing a row."],
       ["python -m colony schedule --show", "Is the hourly task installed and when did it last run."],
       ["python -m colony sql \"SELECT ...\"", "Read the ledger directly. SELECT only."],
       ["python -m colony halt \"reason\"", "Stop all spending now. Resume lifts it."],
       ["python -m colony allowance 10", "Give this week ten more points of your weekly window. Zero clears it."],
       ["python -m colony agents", "Who is hired and what each may write to."]] },

];

function manualSection(sec, id) {
  const box = el("section", "man-sec");
  box.id = id;
  box.append(el("h4", null, sec.h));
  (sec.p || []).forEach((t) => box.append(el("p", null, t)));
  if (sec.dl) {
    const dl = el("dl", "man-dl");
    sec.dl.forEach((pair) => dl.append(el("dt", null, pair[0]), el("dd", null, pair[1])));
    box.append(dl);
  }
  ["ol", "ul"].forEach((tag) => {
    if (!sec[tag]) return;
    const list = el(tag, "man-list");
    sec[tag].forEach((t) => list.append(el("li", null, t)));
    box.append(list);
  });
  if (sec.note) box.append(el("p", "man-note", sec.note));
  return box;
}

function openManual() {
  const body = openDrawer("manual", "How the colony works", { wide: true });
  body.replaceChildren();
  body.append(el("p", "man-asof",
    "Written " + MANUAL_AS_OF + ". This is a snapshot, not a live view. Nothing here "
    + "reads the ledger, so it will drift as the code changes — when it is wrong, "
    + "delete it and write it again."));
  const toc = el("nav", "man-toc");
  MANUAL.forEach((sec, i) => {
    const jump = el("button", "link", sec.h);
    jump.onclick = () => {
      const target = $("man-" + i);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    toc.append(jump);
  });
  body.append(toc);
  MANUAL.forEach((sec, i) => body.append(manualSection(sec, "man-" + i)));
}

// ── phone access ────────────────────────────────────────────────────────────
//
// The whole setup used to be five steps at the desk: mint a token, open `.env`
// in an editor, paste it, save, run `py -m colony autostart`. All five happen
// on the machine you are about to walk away from, which is the argument for
// doing them here — you are already looking at the page.
//
// One `POST /api/act/phone` does all of it and `GET /api/phone` answers the
// panel. Neither is in `/api/state`: answering costs a PowerShell call and a
// socket probe, and the live feed polls every few seconds.

function openPhone() {
  const body = openDrawer("phone", "Phone Access");   // a control panel, no `nav`
  const draw = (info) => {
    body.replaceChildren();
    const set = el("div", "set");

    const row = el("div", "row");
    const on = !!info.on;
    const button = el("button", on ? "act warn" : "act", on ? "turn off" : "turn on");
    button.onclick = async () => {
      button.disabled = true;
      button.textContent = on ? "stopping…" : "setting up…";
      const out = await act("phone", { on: !on });
      if (out && out.minted) toast("minted an access token and wrote it to .env");
      // Redrawn from the server rather than from `out`, and on the refusal path
      // too: `act` has already shown the reason, and a setup that failed
      // halfway must not leave the panel claiming it is on.
      load();
    };
    // Everything in this panel is a snapshot of a machine, not of the ledger,
    // and the live feed deliberately does not carry it (see `/api/phone`). So
    // the one thing that goes stale here -- the address after a network change,
    // `serving` after the logon task finally binds, the firewall after the
    // rule is added in a terminal -- needs a way to be asked again that is not
    // "close the drawer and open it".
    const again = el("button", "link", "refresh");
    again.title = "ask the machine again — address, firewall, and whether it is serving";
    again.onclick = () => { again.textContent = "checking…"; load(); };

    row.append(button, el("span", "val", on ? "on" : "off"), again);
    set.append(blk("phone access", row));

    if (info.problem) {
      set.append(blk("no address", el("pre", "detail", info.problem)));
      body.append(set);
      return;
    }

    const where = el("div", "blk");
    where.append(el("div", "lb", "address"));
    where.append(el("div", "mono", info.address + ":" + info.port
                                  + "  (" + (info.kind || "") + ")"));
    if (info.advice) where.append(el("div", "note", info.advice));
    if (on && !info.unlimited) {
      where.append(el("div", "note",
        "Task Scheduler will kill this task after three days. Re-run "
        + "`py -m colony autostart` to clear the limit."));
    }
    if (on && !info.serving) {
      where.append(el("div", "note",
        "nothing is answering on that address yet — the logon task waits 45 "
        + "seconds for the network before it binds."));
    }
    set.append(where);

    // The firewall is the one failure the address line cannot show. The probe
    // behind `serving` runs on this machine, and a packet from this machine
    // never meets the firewall -- so the address can answer here and still be
    // dropped for the phone, with a spinner at one end and no log at the other.
    if (info.firewall === "blocked" || info.firewall === "unknown") {
      const warn = el("div", "blk");
      warn.append(el("div", "lb", "windows firewall"));
      warn.append(el("div", "note", info.firewall === "blocked"
        ? "there is no rule for this port, so the phone's request will be "
          + "dropped rather than refused: the browser loads forever and "
          + "nothing is logged at either end. Run the command below in a "
          + "terminal — it asks for administrator once."
        : "the firewall rules could not be read, which usually means this "
          + "process is not allowed to. If the phone loads forever, check "
          + "this first."));
      const cmd = el("div", "mono", "py -m colony phone --allow-firewall");
      cmd.style.wordBreak = "break-all";
      cmd.style.cursor = "pointer";
      cmd.title = "click to copy";
      cmd.onclick = () => navigator.clipboard.writeText(cmd.textContent)
        .then(() => toast("copied", ""))
        .catch(() => toast("this browser would not copy it — select it instead", "bad"));
      warn.append(cmd);
      set.append(warn);
    }

    if (info.url) {
      const link = el("div", "mono", info.url);
      link.style.wordBreak = "break-all";
      link.title = "click to copy";
      link.style.cursor = "pointer";
      link.onclick = () => navigator.clipboard.writeText(info.url)
        .then(() => toast("copied", ""))
        .catch(() => toast("this browser would not copy it — select it instead", "bad"));
      set.append(blk("open this on the phone", link));

      if (info.svg) {
        // Parsed rather than assigned to `innerHTML`. Nothing hostile can reach
        // this markup -- the encoder never puts the text into the document, only
        // into the modules -- but "this particular string is safe" is not a rule
        // that survives the next person editing it, and a parser is one line.
        const doc = new DOMParser().parseFromString(info.svg, "image/svg+xml");
        const node = doc.documentElement;
        node.style.width = "min(260px, 60vw)";
        node.style.height = "auto";
        node.style.borderRadius = "6px";
        const wrap = el("div", "blk");
        wrap.append(el("div", "lb", "or point a camera at this"));
        wrap.append(node);
        set.append(wrap);
      }

      // The whole reason this button exists: rotating used to mean minting a
      // token in a terminal, opening `.env` in an editor, replacing one line,
      // saving, and re-running a command to get a QR code that matched. Five
      // steps to undo one mistake, and the mistake -- a token that has been
      // seen by someone -- is one you want undone in the next ten seconds.
      //
      // Offered only on the machine itself, because "log every phone out" is a
      // button that, pressed on a phone, logs that phone out mid-press: the
      // rotate succeeds, the redraw after it comes back 401, and the person is
      // staring at a login page wondering whether it worked. The server would
      // let it happen -- the request carried a valid token right up until it
      // did not -- so the guard belongs here, where the caller knows where it
      // is standing. The desktop window is loopback and is trusted by peer
      // address rather than by token, which is why it survives its own click.
      const here = ["127.0.0.1", "::1", "localhost"].includes(location.hostname);
      const roll = el("div", "blk");
      roll.append(el("div", "lb", "token"));
      roll.append(el("div", "note", here
        ? "rotating writes a new token to .env and logs out every phone that "
          + "has the old one. This window stays signed in — it is on this "
          + "machine, and the dashboard trusts that by address rather than by "
          + "token. Scan the new code above afterwards to pair again."
        : "rotating is only offered on the machine itself: doing it from here "
          + "would log this device out in the middle of the click. Open the "
          + "dashboard on the desktop, or run `py -m colony phone --rotate`."));
      const spin = el("button", "act warn", "rotate token");
      spin.disabled = !here;
      spin.onclick = () => confirmThen(
        "Rotate the access token?\n\nEvery paired phone stops working until it "
        + "scans the new QR code. This window is unaffected.",
        async () => {
          spin.disabled = true;
          spin.textContent = "rotating…";
          const out = await act("phone-token", { port: info.port });
          if (out) toast("new token written to .env — scan the code again");
          // Redrawn from the server on both paths, so a write that failed
          // halfway cannot leave a QR code on screen for a token that is not
          // in the file.
          load();
        });
      roll.append(spin);
      set.append(roll);
    } else {
      set.append(blk("no token yet", el("div", "note",
        "turning this on mints one and appends it to .env. An access token "
        + "that is already there is used as it is and never overwritten.")));
    }

    body.append(set);
  };

  const load = () => getJSON("/api/phone")
    .then(draw)
    .catch(() => body.replaceChildren(
      el("div", "empty", "could not read phone access — is the server still up?")));
  load();
}

$("open-appearance").onclick = () => openAppearance();
$("open-manual").onclick = () => { $("filemenu").open = false; openManual(); };
$("open-phone").onclick = () => { $("filemenu").open = false; openPhone(); };

// ── what the page shows ─────────────────────────────────────────────────────
//
// Eleven sections is the right number for a colony you are running and the
// wrong number for a colony you are only checking on. So the page remembers
// which of them you want. Three rules keep this from becoming a second kind of
// state to reason about:
//
//   1. It is local. Nothing here is sent to the server, and hiding a panel
//      does not stop the colony from filling it — you are choosing what to
//      look at, not what runs. Come back with the panel shown and the work
//      that happened while it was hidden is all there.
//   2. Panels default to *on* and detail defaults to *off*. A key that has
//      never been written reads as its default, so a new panel added later
//      appears for people who have been using the menu for months.
//   3. Nothing is ever hidden silently. The two detail toggles have a count
//      in the header when they are hiding something, because a filter you
//      forgot you set is indistinguishable from a bug.

const VIEW_KEY = "colony-view";
const PANEL_KEYS = ["sprint", "inbox", "flight", "ordis", "colony", "standby",
                    "board", "completed", "files", "spend", "macros", "pulse",
                    "forge"];
let VIEW = {};
try { VIEW = JSON.parse(localStorage.getItem(VIEW_KEY) || "{}") || {}; } catch (_) { VIEW = {}; }

function shows(key) {
  return VIEW[key] === undefined ? PANEL_KEYS.includes(key) : !!VIEW[key];
}

function applyView() {
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

function setView(key, on) {
  VIEW[key] = !!on;
  localStorage.setItem(VIEW_KEY, JSON.stringify(VIEW));
  applyView();
  if (STATE) render(STATE);   // stale and dropped change what the lists contain
}

for (const box of document.querySelectorAll("[data-view]")) {
  box.onchange = () => setView(box.dataset.view, box.checked);
}
$("view-reset").onclick = () => {
  VIEW = {};
  localStorage.removeItem(VIEW_KEY);
  applyView();
  if (STATE) render(STATE);
};
$("inbox-show-stale").onclick = () => setView("stale", !shows("stale"));
$("board-new").onclick = openNewStory;
$("board-dropped").onclick = () => setView("dropped", !shows("dropped"));
$("board-filed").onclick = () => setView("filed", !shows("filed"));

// Click-away close. <details> has no such behaviour of its own, and a menu
// that stays open over the board while you try to click the board is worse
// than no menu.
// Every header dropdown, not just the view menu: `file` is a second one now
// and a third would otherwise be a third place to remember this.
document.addEventListener("click", (ev) => {
  document.querySelectorAll("details.viewmenu[open]").forEach((menu) => {
    if (!menu.contains(ev.target)) menu.open = false;
  });
});

applyView();

// ── live feed ───────────────────────────────────────────────────────────────

function setConn(live, text) {
  $("conn").dataset.live = String(live);
  $("conn-text").textContent = text;
}

let es = null;
function connect() {
  es = new EventSource("/events");
  es.addEventListener("state", (ev) => { setConn(true, "live"); render(JSON.parse(ev.data)); });
  es.onopen = () => setConn(true, "live");
  es.onerror = () => {
    // Numbers on screen are now of unknown age. Say so rather than let a frozen
    // page look like a quiet one.
    setConn(false, "disconnected — retrying");
    es.close();
    setTimeout(connect, 3000);
  };
}

fetch("/api/projects").then((r) => r.json()).then((p) => { ALL_PROJECTS = p.all || []; }).catch(() => {});

// The tree is not part of the snapshot and deliberately not on the SSE feed: it
// is a filesystem read, it costs a `git status` and a directory listing, and
// nothing about it changes because the ledger did. It loads once and refreshes
// when asked.
renderTree();
$("tree-refresh").onclick = () => renderTree();
$("tree-q").addEventListener("input", (e) => {
  TREE_FILTER = e.target.value.trim();
  renderTree();
});
fetch("/api/state").then((r) => r.json()).then(render).catch(() => setConn(false, "ledger unreachable"));

paintThemeColor();

// `?k=<token>` is how the first visit from a phone can be a link or a QR code
// rather than a password typed on a touch keyboard. The server swaps it for an
// HttpOnly cookie on that first request, so by the time this runs the parameter
// has already done its whole job — and a token left in the address bar is a
// token in the history, in a screenshot, and in whatever gets pasted next.
if (new URLSearchParams(location.search).has("k")) {
  const url = new URL(location.href);
  url.searchParams.delete("k");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

// The service worker only exists so a phone will offer to install this to the
// home screen; it caches nothing but an offline notice (see sw.js). Registered
// last and failing silently, because a dashboard that will not load because a
// worker did not register would be a far worse bug than no install prompt. It
// registers inside the pywebview window too, where it is simply inert — the
// only thing it ever serves is a page you reach by losing the network, and the
// desktop shell is running on the machine the server is on.
if ("serviceWorker" in navigator && location.protocol !== "file:") {
  addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

// ?nostream skips the live feed and leaves one static frame on screen. An
// endless SSE response keeps a headless browser from ever settling, so this is
// how the page gets screenshotted, printed, or debugged without the socket.
if (location.search.includes("nostream")) setConn(false, "static snapshot");
else connect();

// ?open=persona:engineering/python-pro — every drawer is addressable. A
// persona's criteria or a patch waiting on you is the kind of thing you want to
// leave a link to, and the drawers were already one function call each.
const deep = new URLSearchParams(location.search).get("open");
if (deep) {
  const [kind, ...rest] = deep.split(":");
  const id = rest.join(":");
  const open = { story: openStory, persona: openPersona, pulse: openPulse,
                 project: openProject, agent: openAgent }[kind];
  if (open) open(kind === "story" || kind === "pulse" || kind === "agent" ? Number(id) : id);
}
