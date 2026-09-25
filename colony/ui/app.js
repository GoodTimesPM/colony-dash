// Colony Dash. The whole dashboard.
//
// One script, no build step, no framework. It reads `/api/state` and paints.
// Every write goes back through an `/api/...` POST with the `X-Colony` header;
// nothing here decides anything.

"use strict";

const $ = (id) => document.getElementById(id);

// localStorage throws in a locked-down WebView, a private window, or when full.
// Everything kept there is a preference, so a failure means defaults.
const store = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(key); return v == null ? fallback : v; }
    catch (_) { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) {} },
  remove(key) { try { localStorage.removeItem(key); } catch (_) {} },
};
const STATUS_LABEL = {
  "backlog": "backlog", "needs-info": "needs info", "needs-criteria": "needs criteria",
  "ready": "ready", "in-progress": "running", "po-review": "po review",
  // Not "done". `accepted` means one patch landed; only `settled_as` (the PO's
  // Notion status or button) says a story is finished.
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
// A block of text folded to a few lines, with a button that says how much more
// there is.
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
// One function for every control on the page. The server requires the custom
// header, which a cross-origin page cannot send. Refusals come back as a 409
// with a reason written for a person, so show that text.

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
    toast("the ledger did not answer. Is the server still up?", "bad");
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
// A hired persona keeps one face from roster to running agent (§9.3). Mirrored
// on the vertical axis so the noise reads as a character.
function hash32(str) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return h >>> 0;
}
// Roster colours are whatever the persona's author typed ("blue", "slate",
// "neon-green"). Canvas ignores a colour it cannot parse and keeps the previous
// `fillStyle`, which painted those sprites in the panel's own colour. These are
// the words the roster uses that CSS does not know; anything else unrecognised
// falls back to the seed.
const TINT_WORDS = {
  slate: "#94a3b8", amber: "#f59e0b", rose: "#fb7185",
  "neon-green": "#39ff14", "neon-cyan": "#00e5ff", "metallic-blue": "#4a749b",
};

// Canvas only reveals whether it parsed a colour by changing `fillStyle`, so
// two sentinels are set: an accepted value overwrites both identically, a
// rejected one leaves both.
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

// The hue belongs to the persona; the lightness is adjusted for this panel.
// Near-black roster colours vanish on a dark panel, so the panel picks the
// direction with room, and the clamped start leaves the walk somewhere to go.
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
  // A seed with very few lit cells reads as a missing picture, so the top is
  // filled deterministically up to a minimum.
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

// Each panel with the state keys it reads. A panel is rebuilt only when one of
// those slices changed, so a frame that moves one number does not reset the
// scroll and selection in the other twelve.
const PANELS = [
  ["sprint", ["sprint"], (s) => renderSprint(s.sprint)],
  ["ordis", ["ordis"], (s) => renderOrdis(s.ordis)],
  ["colony", ["colony"], (s) => renderColony(s.colony)],
  ["board", ["board"], (s) => renderBoard(s.board)],
  ["completed", ["completed"], (s) => renderCompleted(s.completed || [])],
  ["projects", ["projects"], (s) => renderProjects(s.projects)],
  ["flight", ["flight"], (s) => renderFlight(s.flight || [])],
  ["inbox", ["inbox", "flight"], (s) => renderInbox(s.inbox)],
  ["macros", ["controls"], (s) => renderMacros(s.controls)],
  ["pulses", ["pulses"], (s) => renderPulses(s.pulses)],
  ["forge", ["forge", "sprint"], (s) => renderForge(s.forge)],
  ["spend", ["spend"], (s) => renderSpend(s.spend)],
];
const drawn = new Map();   // panel name -> the JSON it was last drawn from

// `force` redraws every panel. Callers pass it when something on the page,
// not in the state, changed what a panel shows: a filter, a view, a theme.
function render(s, force) {
  // A slow `/api/state` fetch can land after a newer SSE frame. `seq` only
  // grows, so an older state is dropped.
  if (!force && STATE && s.seq != null && STATE.seq != null && s.seq < STATE.seq) return;
  STATE = s;
  document.body.classList.toggle("halted", !!s.controls.halted);
  document.body.classList.toggle("beating", !s.controls.halted);
  $("halt-why").textContent = s.controls.halt_reason || "";
  for (const [name, keys, draw] of PANELS) {
    const slice = JSON.stringify(keys.map((k) => s[k]));
    if (!force && drawn.get(name) === slice) continue;
    drawn.set(name, slice);
    draw(s);
  }
  $("roster-count").textContent = s.roster.total + " personas";
  syncRoster(s.roster.rev);
}

// The full roster is fetched only when its revision moves. The browser keeps
// the ETag, so an unchanged roster is a 304.
let rosterRev = null;
async function syncRoster(rev) {
  if (!rev || rev === rosterRev) return;
  rosterRev = rev;
  try {
    renderDivisions(await getJSON("/api/roster/summary"));
  } catch (_) {
    rosterRev = null;
  }
}

function tag(text, cls) { const b = el("span"); b.append(el("b", cls || null, text)); return b; }

// A ledger timestamp as a person reads it: "Fri 5:00 AM".
function when(ts) {
  const d = parseTs(ts);
  if (!d || isNaN(d)) return String(ts || "").slice(0, 16);
  return d.toLocaleString(undefined,
    { weekday: "short", hour: "numeric", minute: "2-digit" });
}

// The same with its date, for things that can be weeks old.
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
    // The server counts the day from the allowance week's real edge (Friday
    // 05:00), not from midnight on the sprint row's date.
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
    // `seven_day_resets_at` is already local time.
    weekMeta.append(tag("week " + Number(usage.seven_day_pct).toFixed(1) + "%"),
                    tag("5h " + Number(usage.five_hour_pct).toFixed(0) + "%"),
                    tag("resets " + when(usage.seven_day_resets_at)));
    // A figure that stopped moving could be a quiet week or a dead tray app;
    // this says which.
    if (usage.stale) {
      const s = tag("cache stale", "boosted");
      s.title = "the tray app has not written since " + when(usage.sampled_at);
      weekMeta.append(s);
    }
  } else {
    weekMeta.append(tag("no usage sample. Is the tray app running?"));
  }

  // The bar measures spend against the colony's *allowance*, not the whole week:
  // 35% of the window is the ceiling, so 35% consumed has to read as full.
  const ceiling = band.effective || 35;
  // The cache can carry a null percentage; `Number(null)` gives 0 rather than a
  // blank strip.
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

  // The console sits in the Ordis panel: it is Ordis, and it answers without
  // waiting for a pulse.
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
  if (!box.children.length) box.append(el("div", "empty", "nobody hired yet. Open a persona in Standby to hire"));
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
    // row, which would leak a timer for every re-render.
    const stat = el("div", "line mono elapsed");
    stat.dataset.started = o.started.getTime();
    stat.dataset.tokens = o.tokens || 0;
    right.append(stat);
  }
  // The bar is tokens against this agent's ceiling. Lloyd measures context, we
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

// Relative times go stale between SSE pushes, which can be an hour apart, and a
// frozen "0m ago" looks like a stopped heartbeat. So a relative time is a node
// that keeps its timestamp and is repainted every fifteen seconds; the finest
// unit shown is a minute.
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
      p.title = s.project_source === "confirmed" ? "confirmed by you" : "inferred. Cannot authorise a write";
      sub.append(p);
    } else {
      sub.append(el("span", "dim", "no project"));
    }
    // Notion's ticked items, so the board can say "4 of 7 done".
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
// Done, Shipped, Shelved, New and Not started all mean the PO is not asking for
// anything, so they share one hidden shelf under the board rather than five
// columns. A toggle shows them.
const FILED_LABEL = { "done": "done", "shelved": "shelved", "not-started": "not started" };

function renderFiled(rows) {
  const box = $("stories");
  const btn = $("board-filed");
  btn.style.display = rows.length ? "" : "none";
  btn.textContent = shows("filed") ? "hide filed · " + rows.length : "filed · " + rows.length;
  if (!shows("filed") || !rows.length) return;

  const list = el("div", "dropped-list");
  list.append(el("div", "lb", "filed. Nothing is being asked about these"));
  for (const s of rows) {
    const line = el("div", "dropped-line is-filed");
    const t = el("button", "t filed", s.title);
    t.title = "open story #" + s.id + " · " + (s.project || "no project");
    t.onclick = () => openStory(s.id);
    line.append(t);
    line.append(el("span", "chip filed" + (s.settled_as === "done" ? " done" : ""),
                   FILED_LABEL[s.settled_as] || s.settled_as));
    // Notion's own status beside the ledger's, since the ledger folds five
    // statuses into three.
    if (s.notion_status) line.append(el("span", "why", s.notion_status));
    list.append(line);
  }
  box.append(list);
}

// ── the dropped ─────────────────────────────────────────────────────────────
// Dropped stories live under the board, hidden by default; the header count
// says there is something to open.
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

// Three orders. **changes** is the pulse's order (commits first, then volume),
// for "what is outstanding". **recent** is newest mtime, for "what was I just
// editing". **name** holds still. Stored in localStorage like the view menu.
const SORT_KEY = "colony-proj-sort";
let PROJ_SORT = store.get(SORT_KEY) || "changes";
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
    store.set(SORT_KEY, PROJ_SORT);
    renderProjects(PROJ_ROWS);
  };
}

function renderProjects(rows) {
  const box = $("projects");
  PROJ_ROWS = rows;
  box.replaceChildren();
  // Every project folder is in one git repo, so one baseline commit covers them
  // all; the header names it.
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
    // The time on the row, since *when* a folder was written usually explains
    // why it is dirty.
    b.append(el("span", "p", r.project), el("span", "s", r.summary));
    if (r.touched_at) b.append(el("span", "s", ago(r.touched_at)));
    const bar = el("div", "bar");
    const seg = (n, cls) => { if (!n) return; const i = el("i", cls); i.style.width = (n / max) * 100 + "%"; bar.append(i); };
    seg(r.added, "a"); seg(r.modified, "m"); seg(r.deleted, "d"); seg(r.untracked, "u");
    b.append(bar);
    b.title = `${r.project}
${r.summary}. Measured against ${r.head_sha || "?"} on ${r.branch || "?"}` +
              (r.touched_at ? `
last written ${r.touched_at} (${ago(r.touched_at)})` : "");
    b.onclick = () => openProject(r.project);
    box.append(b);
  }
}

// ── the file tree ───────────────────────────────────────────────────────────
// The Projects list shows what moved; this shows everything that is there,
// loaded one folder per request, with change state as a dot. Open folders are
// kept in a Set of paths, so a re-render keeps the same shape.

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
  // The filter applies to the top level only, so an open folder does not vanish
  // under the cursor.
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
  // A changed file leads with its diff, that is why it is interesting, and
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
// Tickets the colony is working or staffed to work, and Notion changes queued
// on this machine: things the PO pressed that have not finished.

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
    // A push says what it will do to the board, in the board's words.
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
      ? "tried " + f.attempts + " times and stopped, the pulse will not retry this on its own"
      : "queued " + ago(f.queued_at) + ", the next pulse sends it. Nothing has changed on the board yet.";
  } else {
    row.append(el("div", "t", f.title));
    const m = el("div", "m");
    // A reply is a ticket, but it shows as "waiting on Ordis", not its
    // mechanism.
    const reply = !!f.po_message_id;
    m.append(el("span", "st", f.run_id ? "running" : f.status),
             el("span", null, reply ? "reply" : f.intent),
             el("span", "who", f.role || (reply ? "waiting for Ordis" : "unstaffed")));
    row.append(m);
    if (reply && f.po_message) row.append(el("div", "said", "“" + f.po_message + "”"));
    // A blocked ticket shows what it found.
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
// Two things end, and both are listed here: a dispatch (an implement ticket
// that delivered a patch) and a story the PO filed. One chronological list,
// separate badges, since "did we build this" differs from "did I close this".
// Read-only.
const DONE_FILTER_KEY = "colony-done-filter";
let DONE_FILTER = store.get(DONE_FILTER_KEY) || "all";

function renderCompleted(items) {
  const box = $("completed");
  box.replaceChildren();
  for (const b of document.querySelectorAll("[data-done]")) {
    b.setAttribute("aria-pressed", String(b.dataset.done === DONE_FILTER));
  }
  const shown = DONE_FILTER === "all" ? items : items.filter((i) => i.kind === DONE_FILTER);

  // The count is all finished work; the filter does not change it.
  const dispatches = items.filter((i) => i.kind === "dispatch").length;
  const stories = items.length - dispatches;
  $("completed-count").textContent = items.length
    ? dispatches + " delivered  ·  " + stories + " filed" : "";

  if (!shown.length) {
    box.append(el("div", "empty", items.length
      ? "nothing under this filter"
      : "nothing has finished yet. A dispatch lands here when its ticket closes"));
    return;
  }
  for (const it of shown) box.append(completedTile(it));
}

for (const b of document.querySelectorAll("[data-done]")) {
  b.onclick = () => {
    DONE_FILTER = b.dataset.done;
    store.set(DONE_FILTER_KEY, DONE_FILTER);
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

  // The finish date, in full: the panel is sorted by it and a relative age
  // alone cannot place it.
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
  card.title = "open the whole record. The work order, what it produced, and every "
             + "message, question and decision since the last delivery";
  return card;
}

// Links an Inbox tile to its rail rows, from both ends. Keys on the tile are
// space-separated, so `[data-flight~="t7"]` matches one in a list.
function lightFlight(key, on) {
  for (const n of document.querySelectorAll('[data-flight~="' + key + '"]')) {
    n.classList.toggle("lit", on);
  }
}

// Which rail rows an escalation is about: the ticket it was raised from, plus
// anything else in flight for the same story.
function flightKeys(e) {
  const flight = (STATE && STATE.flight) || [];
  const keys = [];
  for (const f of flight) {
    // A closed ticket is not in flight, so a badge that lights nothing is not
    // shown.
    const mine = (e.ticket_id && f.kind === "ticket" && f.id === e.ticket_id) ||
                 (e.story_id && f.story_id === e.story_id);
    if (mine && !keys.includes(f.key)) keys.push(f.key);
  }
  return keys;
}

function renderInbox(items) {
  const box = $("inbox"), strip = $("inbox-strip");
  box.replaceChildren();

  // A stale question was written against an older brief. It is kept, but out of
  // the way by default.
  const stale = items.filter((e) => e.stale);
  const live = shows("stale") ? items : items.filter((e) => !e.stale);

  const btn = $("inbox-show-stale");
  btn.style.display = stale.length ? "" : "none";
  btn.textContent = shows("stale") ? "hide stale · " + stale.length : "stale · " + stale.length;

  const waiting = items.filter((e) => !e.stale).length;
  $("inbox-count").textContent = waiting ? waiting + " waiting on you" : "";
  strip.classList.toggle("quiet", waiting === 0);
  if (!live.length) {
    // Nothing to run out to a full row, and the observer has to be told so.
    // `replaceChildren` clears the tiles but not the count written beside them.
    box.dataset.live = 0;
    // An empty Inbox still gets a row, with the message in a slot, so the page
    // does not jump between "clear" and "one question".
    box.dataset.note = stale.length
      ? "nothing current. " + stale.length + " stale question" +
        (stale.length === 1 ? "" : "s") + " behind the toggle"
      : "empty. Nothing needs you";
    padSlots(box, 0);
    return;
  }
  delete box.dataset.note;
  for (const e of live) box.append(inboxTile(e));
  padSlots(box, live.length);
}

// How many tiles fit across, asked of the grid (`auto-fill` already did the
// arithmetic). The answer is only true for the width at that moment, and a
// render can run before layout, so the count is stored on the element and the
// padding redone whenever the width changes.
function padSlots(box, count) {
  if (count != null) box.dataset.live = count;
  const live = Number(box.dataset.live || 0);
  for (const old of box.querySelectorAll(".tile.slot")) old.remove();
  const cols = gridCols(box);
  if (!live) {
    // An unknown column count falls back to one slot, which carries the
    // message; the observer widens the row once there is a width.
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

// Before layout, the computed value is the unresolved `repeat(auto-fill, ...)`,
// and counting its words gives a wrong 2. That case returns "unknown" and the
// observer asks again.
function gridCols(box) {
  const t = getComputedStyle(box).gridTemplateColumns;
  if (!t || t === "none" || t.indexOf("repeat(") >= 0 || t.indexOf("minmax(") >= 0) return 0;
  return t.split(" ").filter(Boolean).length;
}

// The Inbox changes width for reasons the render never hears about. Width only:
// padding changes the height, and reacting to that would loop.
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
    flag.title = "the story changed after this was asked. The question is about a version that no longer exists";
    k.append(flag);
  }
  const keys = flightKeys(e);
  if (keys.length) {
    // The marker only appears when there is something to point at. A badge on
    // every tile would be furniture; a badge on three of them is information.
    card.dataset.flight = keys.join(" ");
    const pin = el("button", "pin", "queued · " + keys.length);
    pin.title = "this question has work in the ticket queue. Hover to find it, click to open the story";
    pin.onmouseenter = () => keys.forEach((key) => lightFlight(key, true));
    pin.onmouseleave = () => keys.forEach((key) => lightFlight(key, false));
    pin.onclick = () => {
      const first = document.querySelector('.fl[data-flight="' + keys[0] + '"]');
      if (first) first.scrollIntoView({ block: "nearest" });
      if (e.story_id) openStory(e.story_id);
    };
    k.append(pin);
  }
  // The x: close the question without answering it, and without the side
  // effects of dropping the story. Not on a write approval, which the server
  // also refuses: a patch and worktree sit behind it.
  if (e.id && e.kind !== "write-approval") {
    const x = el("button", "dismiss", "×");
    x.title = "this question stopped mattering. Close the card and change nothing else. "
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
  // The second opinion sits under the reasoning, not with the buttons; it is
  // evidence, not a third answer.
  if (e.second_opinion) {
    const box = el("div", "second");
    box.append(el("div", "who", "second opinion \u2014 agents-orchestrator, read-only"));
    box.append(longText(e.second_opinion, 520));
    card.append(box);
  }
  if (e.snoozed) {
    card.append(el("div", "snooze-note", "snoozed · back " + until(e.snoozed_until)));
  }
  // Ordis's answers live in the thread, behind the reply button's count. A
  // queued message does belong here: deciding now decides ahead of an answer
  // you asked for.
  if (e.awaiting_ordis) {
    card.append(el("div", "waiting",
      e.awaiting_ordis + " repl" + (e.awaiting_ordis === 1 ? "y" : "ies") +
      " queued. Ordis answers on the next pulse"));
  }

  for (const b of (e.blockers || [])) card.append(el("div", "blocker", b));

  const meta = [];
  if (e.story_title) meta.push(e.story_title);
  if (e.est_tokens) meta.push(toks(e.est_tokens) + " tok");
  if (meta.length) card.append(el("div", "meta", meta.join("  ·  ")));

  const acts = el("div", "acts");

  // A story whose folder is still a guess has one answer, so the picker is on
  // the tile. A `needs-info` card is the folder question and gets the picker
  // alone; a `decision` on an unconfirmed story gets both.
  const unconfirmed = e.story_id && e.project_source !== "confirmed";
  const asksForProject = e.kind === "needs-info";
  if (unconfirmed) {
    // The picker's own row. "None of these, it's new" swaps the select for a
    // text field and creates the folder on confirm.
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
    // Not an escalation: a state the story is in (see `_ready` in server.py),
    // so the controls are "start it" and what still blocks it.
    const go = el("button", "act go", "dispatch to build");
    const stuck = (e.blockers || []).length;
    go.disabled = !!stuck;
    go.title = stuck ? e.blockers[0]
      : "cuts the implement ticket. The next wake opens a git worktree and writes in it";
    go.onclick = () => confirmThen(
      "Dispatch \u201c" + (e.story_title || "this story") + "\u201d to build?\n\n" +
      "This cuts a ticket. The next wake opens a git worktree, works in there, and " +
      "brings back a patch. Nothing touches your working tree until you approve it.",
      () => act("dispatch", { story_id: e.story_id }));
    acts.append(go);
  } else if (e.kind === "write-approval") {
    const view = el("button", "act", "read the patch");
    view.onclick = () => openPatch(e);
    const yes = el("button", "act go", "apply");
    yes.onclick = () => confirmThen(
      "Apply this patch to the live tree? It lands uncommitted. You still review and commit it yourself.",
      () => act("decide", { escalation_id: e.id, decision: "approve" }));
    const no = el("button", "act no", "reject");
    no.onclick = () => act("decide", { escalation_id: e.id, decision: "reject" });
    acts.append(view, yes, no);
  } else if (e.kind === "run-request" && e.id) {
    // The one card that runs something on the live tree. The command is shown
    // verbatim above, since approving an unread command is the risk.
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
    yes.title = "clears the criteria and puts it back in the groom queue. The next wake re-reads the brief";
    yes.onclick = () => confirmThen(
      "Reopen “" + (e.story_title || "this story") + "”?\n\n" +
      "Its acceptance criteria are cleared and the next wake re-reads the whole " +
      "brief from Notion. Anything already built stays built.",
      () => act("decide", { escalation_id: e.id, decision: "approve" }));
    const no = el("button", "act no", "leave it");
    no.title = "records that the edit did not change the work; the card returns only if you edit the page again";
    no.onclick = () => act("decide", { escalation_id: e.id, decision: "reject" });
    acts.append(yes, no);
  } else if (e.kind === "hire" && e.id) {
    const yes = el("button", "act go", "approve");
    yes.title = "cuts the contract \u2014 write scope is that one project folder and nothing else";
    yes.onclick = () => act("decide", { escalation_id: e.id, decision: "approve" });
    const no = el("button", "act no", "reject");
    no.title = "records that this was the wrong person; the next pulse proposes someone else";
    no.onclick = () => act("decide", { escalation_id: e.id, decision: "reject" });
    acts.append(yes, no);

    // Ask `agents-orchestrator`, the persona the colony never hires, to audit
    // this pick. It cannot hire, reject or close the card; it writes a
    // paragraph. It reads the full roster, about a third of a grooming run, so
    // it runs only on this button.
    if (!e.second_opinion) {
      const ask = el("button", "act", "second opinion");
      ask.title = "asks agents-orchestrator to audit this pick, read-only. It cannot hire "
                + "or refuse anything \u2014 it writes its view onto this card and you still decide. "
                + "Costs one run against the full roster and takes a few minutes.";
      ask.onclick = () => confirmThen(
        "Ask agents-orchestrator to audit this hire?\n\n" +
        "It reads the story, the picked persona's file and the whole roster, then writes " +
        "its view onto this card. It decides nothing \u2014 approve and reject stay yours.\n\n" +
        "This costs one read-only run of about 20k tokens and takes a few minutes. The card " +
        "does not change until it comes back.",
        async () => {
          ask.disabled = true;
          ask.textContent = "reading the roster\u2026";
          const out = await act("second-opinion", { escalation_id: e.id });
          if (out && out.ok) toast("second opinion: " + out.verdict, "good");
          else if (out) toast(out.verdict || "no usable answer came back", "bad");
          else { ask.disabled = false; ask.textContent = "second opinion"; }
        });
      acts.append(ask);
    }
  } else if (!asksForProject && e.id) {
    const yes = el("button", "act go", "approve");
    yes.onclick = () => act("decide", { escalation_id: e.id, decision: "approve" });
    const no = el("button", "act no", "reject");
    no.onclick = () => act("decide", { escalation_id: e.id, decision: "reject" });
    acts.append(yes, no);
  }

  // Reply: most of what a PO needs to say is a sentence.
  const say = el("button", "act warn",
                 e.messages ? "reply · " + e.messages : "reply to Ordis");
  say.title = "write to Ordis about this item. Queued for the next pulse";
  say.onclick = () => openCompose(e);
  acts.append(say);

  // Later is a snooze with an end. The card stays in the Inbox but grays out
  // and sorts last until it expires.
  if (e.snoozed) {
    const wake = el("button", "act", "un-snooze");
    wake.title = "bring it back to the front of the Inbox now";
    wake.onclick = () => act("decide", { escalation_id: e.id, decision: "defer", snooze_hours: 0 });
    acts.append(wake);
  } else if (e.id) {
    const later = el("button", "act", "later");
    later.title = "snooze 8h. It stays in the Inbox, dimmed, because nothing was decided";
    later.onclick = () => act("decide", { escalation_id: e.id, decision: "defer", snooze_hours: 8 });
    acts.append(later);
  }

  // Re-ask: the answer to a stale question. Resolves it as amended, requeues
  // the groom, and restores the attempts the old groom used.
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

    // Drop from here too, since a question about a story is often when you
    // decide it is not the work.
    const drop = el("button", "act no", "drop story");
    drop.title = "take the whole story off the board. Closes its questions, cancels its tickets";
    drop.onclick = () => dropStory(e.story_id, e.story_title || "this story");
    acts.append(drop);
  }
  card.append(acts);
  return card;
}

// ── attachments ─────────────────────────────────────────────────────────────
// The upload happens on paste, not on send, so a too-large file is refused
// while you are still looking at it.
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
    toast("the upload did not land. Is the server still up?", "bad");
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
// Typing to Ordis, with the thing you are answering pinned above the box. The
// drawer goes wide so the proposal and the reply fit side by side.

async function openCompose(e, opts) {
  // A story-only thread has no Inbox item behind it, so the PO can start a
  // conversation.
  const esc = (opts && opts.storyOnly) ? null : e.id;
  openDrawer(esc ? e.kind.replace("-", " ") + " · #" + esc : "story #" + e.story_id,
             "Reply to Ordis", { wide: true });
  // Opened straight from a tile there is no trail, so offer the story as the
  // way back.
  if (!TRAIL.length && e.story_id) {
    TRAIL.push({ kind: "story", arg: e.story_id,
                 label: e.story_title || ("story #" + e.story_id) });
    renderTrail();
  }

  const box = $("d-body");
  box.replaceChildren();
  const wrap = el("div", "compose");

  // Filled by `load()`, which appends it after the newest message, next to the
  // box you type in.
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
    "They read this on the next pulse, answers in the same thread, and revises " +
    "their recommendation if you have changed it. They cannot approve, reject or " +
    "confirm a project from your reply. Those stay yours.";
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
  clip.title = "or just paste a screenshot into the box. Ctrl+V";
  clip.onclick = () => picker.click();
  const hint = el("span", "hint",
    "queued, not sent. Nothing here spends a token until the next pulse. Ctrl+Enter sends.");
  foot.append(send, clip, picker, hint);
  wrap.append(foot);
  box.append(wrap);
  ta.focus();

  // Keyed by story when there is one, so a re-raised card does not open on an
  // empty thread.
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
      // Every open ask on the story, not only the one this drawer was opened
      // on.
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
        // The learning's gist is stored; the full thought is `detail`.
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

    // Open on the newest message, next to the composer. Twice through rAF
    // because the first frame is before layout, and again per image, since late
    // decodes grow the thread.
    const toEnd = () => { box.scrollTop = box.scrollHeight; };
    requestAnimationFrame(() => requestAnimationFrame(toEnd));
    for (const img of thread.querySelectorAll("img")) {
      if (!img.complete) img.addEventListener("load", toEnd, { once: true });
    }
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
      toast("queued. Ordis reads it on the next pulse", "good");
    }
  };
  send.onclick = submit;
  ta.onkeydown = (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submit(); }
  };
}

// The sentinel for "none of the folders on this list". A newline cannot occur
// in a folder name, so it cannot collide.
const NEW_PROJECT = "\n<new>";

function projectSelect(current, opts) {
  const sel = el("select", "pick");
  sel.append(el("option", null, "name the folder…"));
  sel.firstChild.value = "";
  // `STATE` may not have arrived for a deep-linked drawer, so an empty picker
  // is the failure mode. Sorted here because the fallback list is ordered by
  // recency. localeCompare puts the ＋ entry and accented names where a person
  // looks.
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
// Create a story here without Notion. Same shape as the Inbox's project picker,
// including "＋ new project folder…", since it is the same question.

function openNewStory() {
  // No `nav`: a back button that reopened this would be an empty form claiming
  // to be the one you were typing in. Same rule the reply drawer follows.
  const body = openDrawer("board", "New Story");
  const wrap = el("div", "form");

  const title = el("input", "field");
  title.placeholder = "what needs doing";
  title.maxLength = 200;

  const brief = el("textarea", "field");
  brief.placeholder = "the brief. Why it matters, what done looks like, anything the colony cannot see from the code";
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

    // Two calls: `create_story` refuses a missing folder, so a typo cannot
    // become a directory as a side effect.
    go.disabled = true;
    try {
      if (isNew) {
        const made = await act("confirm-project", { project, create: true });
        if (!made) return;
        // Refetch so the new folder is in the list.
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
    "Filed straight into the ledger. No Notion page behind it, and the sync "
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
    confirmThen("Halt all production? The pulse keeps beating and logging. It just stops spending.",
                () => act("halt", { on: true, reason: "halted from the dashboard" }));
  };
  halt.append(btn);
  halt.append(el("div", "why", c.halted
    ? "No new work will be dispatched. The heartbeat is still running, still logging, still syncing Notion."
    : "Stops every dispatch colony-wide. It cannot claw back a run already in flight. The honest promise is “no new work”."));
  box.append(halt);
  box.append(el("hr", "macro-rule"));

  // Beat out of turn. The scheduled task still fires at :07; a forced beat sits
  // between scheduled ones.
  const hb = el("div", "macro");
  hb.append(el("div", "lb", "heartbeat"));
  const beating = !!c.pulse_running;
  const r0 = el("div", "row");
  const nowb = el("button", "act go", beating ? "beating\u2026" : "pulse now");
  nowb.disabled = beating;
  nowb.title = "one full beat: sync, look, and wake if there is a reason";
  nowb.onclick = async () => {
    const out = await act("pulse", { wake: true });
    if (out) { toast("beat started. Its row lands in the pulse log when it ends", "good");
               setTimeout(refresh, 1500); }
  };
  const tickb = el("button", "act", "tick only");
  tickb.disabled = beating;
  tickb.title = "the free half. Sync, reap and look, but never wake";
  tickb.onclick = async () => {
    const out = await act("pulse", { wake: false });
    if (out) { toast("tick started. Free, no wake", "good"); setTimeout(refresh, 1500); }
  };
  r0.append(nowb, tickb);
  hb.append(r0);
  hb.append(el("div", "why", beating
    ? "A beat is running. Nothing else may beat until it finishes."
    : "The scheduled task beats once an hour at :07. This runs one extra now and leaves that alone. The next automatic beat still comes at its own time."));
  box.append(hb);
  box.append(el("hr", "macro-rule"));

  // Allowance. Stored beside the sprint's designed budget, never on top of it.
  const band = c.allowance;
  const al = el("div", "macro");
  al.append(el("div", "lb", "token allowance"));
  // The bar is the whole week; its empty part is what remains for the PO's own
  // sessions.
  const lo = c.allowance_min === undefined ? 0 : c.allowance_min;
  const hi = c.allowance_max === undefined ? 100 : c.allowance_max;
  const at = band.effective;
  const clamp = (v) => Math.max(lo, Math.min(hi, v));
  const bar = el("div", "boostbar");
  const fill = el("i"); fill.style.width = ((at - lo) / (hi - lo)) * 100 + "%";
  bar.append(fill); al.append(bar);
  al.append(el("div", "why", band.boost
    ? `${at}% of the week. ${band.base}% baseline, moved ${band.boost > 0 ? "+" : ""}${band.boost}.`
    : `${band.base}% of the week, as designed.`));

  // A dial over 0 to 100%. 0% stops spending without HALT's finality; 100%
  // gives the week to the colony. -5 and +5 match, so a mispress costs one
  // press.
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

  // ── the upward direction ──────────────────────────────────────────────────
  // Its own switch, separate from HALT: a Notion comment is not a token, and a
  // halted colony should not look broken from the phone. Off holds the queue;
  // it goes up in order when turned back on.
  const nw = el("div", "macro");
  nw.append(el("div", "lb", "writing to notion"));
  const ob = c.outbox || { waiting: 0, stuck: 0 };
  const on = c.notion_write !== false;   // a bool from /api/state, not the raw control string
  const r3 = el("div", "row");
  const sw = el("button", "act " + (on ? "no" : "go"), on ? "hold writes" : "resume writes");
  sw.title = on
    ? "stop sending upward. Queued changes wait rather than disappear"
    : "send the queue upward again, oldest first";
  sw.onclick = () => act("notion-write", { on: !on });
  r3.append(sw);
  nw.append(r3);
  nw.append(el("div", "why", on
    ? (ob.waiting
        ? `${ob.waiting} change${ob.waiting === 1 ? "" : "s"} queued. The next pulse sends them.`
        : "Status changes, ticks and comments go up on the pulse that follows them.")
    : `Held. ${ob.waiting} change${ob.waiting === 1 ? "" : "s"} waiting, and nothing goes up until you resume.`));
  if (ob.stuck) {
    nw.append(el("div", "why", `${ob.stuck} gave up after repeated failures. Check the pulse log for what Notion said.`));
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
      d.textContent = `${hhmm(a.at)}  ${a.action}${a.target_id ? " #" + a.target_id : ""}: ${a.detail || ""}`;
      log.append(d);
    }
    box.append(log);
  }
}

// `tier` is what the tick decided; `acted` is what the wake did. A "wake" hour
// that spent nothing is shown as a tick.
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

  // Consecutive clean ticks roll up, and a missing hour is drawn in coral, so a
  // stopped heartbeat shows.
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

// Drafts first (waiting on you), then candidates, then what active skills
// earned. The server sorts it; this keeps the order.
const DETECTOR_LABEL = {
  "repeat": "seen 3+ times",
  "recovery": "failed, then worked",
  "shortcut": "came in cheap",
  "po-correction": "you kept fixing it",
};

// Why a wake would refuse to spend now, in the pulse's words, or null if it
// would run.
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
      "nothing yet. The forge proposes a skill once a procedure repeats"));
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
        // When every wake is standing down, say why beside the button that
        // queued the request.
        const held = standdown();
        const w = el("span", held ? "waiting bad" : "waiting",
                     held ? "requested. Held: " + held
                          : "Ordis drafts it on the next wake");
        w.title = held ? "raise the allowance in Macros, or wait for the week to reset"
                       : "queued; it costs nothing until the wake runs";
        bar.append(w);
      } else {
        const b = el("button", "act go", "ask for a draft");
        b.title = "costs nothing now. The next wake writes it";
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

// The promotion gate writes a file outside the ledger, so the draft is shown in
// full first.
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
    wrap.append(el("div", "lb", "promote. This writes the file"));
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

// ── the spend chart ─────────────────────────────────────────────────────────
// The spend chart: grains from an hour to a year, bar or line, with the value
// under the pointer. The series comes from /api/spend, not the snapshot. Grain
// and shape are kept in localStorage.

const SVGNS = "http://www.w3.org/2000/svg";
const GRAIN_UNIT = { hour: "h", day: "d", week: "w", month: "mo", year: "y" };
let SPEND_GRAIN = store.get("colony-spend-grain") || "day";
let SPEND_KIND = store.get("colony-spend-kind") || "line";
let SPEND_SERIES = null;
let SPEND_WIDTH = 0;
// Where the window stops; `null` is live. Not persisted, so the page always
// opens on the present.
let SPEND_END = null;
let SPEND_LAND = null;    // which bucket to sit on after a paging load

function svgEl(name, attrs) {
  const n = document.createElementNS(SVGNS, name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

// A round number at or above the top of the data, so the gridlines land on
// figures a person can hold in their head. 40k, not 38.7k.
function niceMax(v) {
  if (!(v > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}

// One bucket forward or back, `n` of them. setMonth and setFullYear normalise
// overflow the same way the server's `_back` does.
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

// A whole window at a time, overlapping by one bucket so the bucket you were
// reading stays on screen.
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
// The roster, browsed by division as an accordion, biggest first.

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
  b.append(el("span", null, p.emoji || "·"), el("span", "nm", p.name),
           el("span", "dv", p.hired ? "hired" : (p.source === "local" ? "yours" : "")));
  b.title = p.description || "";
  b.onclick = () => openPersona(p.slug);
  return b;
}

// ── adding a persona ────────────────────────────────────────────────────────
// Add a persona from the dashboard instead of hand-writing YAML frontmatter.
//
// Everything written here lands in `~/.colony-agents`, never in
// `~/.agency-agents`, which is someone else's git clone; the two are scanned
// together. Dropping a `.md` file and typing both fill the same form, so the
// division is always checked before writing.

function parseFrontmatter(text) {
  // The same line reader as `roster.parse_persona`, so the import accepts only
  // what the scanner will show.
  const out = { body: text, meta: {} };
  if (!text.startsWith("---")) return out;
  const rest = text.slice(text.indexOf("\n") + 1);
  const end = rest.indexOf("\n---");
  if (end < 0) return out;
  for (const line of rest.slice(0, end).split("\n")) {
    const i = line.indexOf(":");
    if (i < 0) continue;
    const key = line.slice(0, i).trim().toLowerCase();
    if (["name", "description", "color", "emoji", "vibe"].includes(key)) {
      out.meta[key] = line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
    }
  }
  out.body = rest.slice(end + 4).replace(/^\n+/, "");
  return out;
}

async function openPersonaNew() {
  const body = openDrawer("standby", "Add a Persona");
  let dirs = { divisions: [], local_dir: "", agency_dir: "" };
  try { dirs = await getJSON("/api/roster/divisions"); } catch (_) {}
  body.replaceChildren();

  body.append(el("div", "note",
    "Written to " + (dirs.local_dir || "your personas folder") + ", not into the "
    + "agency-agents clone. So a git pull there can never clobber it, and "
    + "nothing you add here becomes part of the Colony Dash repo."));

  const form = el("div", "form");

  // A file input as well as a drop target, because a drop target alone is
  // unusable on a phone and this dashboard is used from one.
  const drop = el("div", "dropzone", "drop a persona .md here, or tap to pick one");
  const file = el("input");
  file.type = "file";
  file.accept = ".md,.markdown,text/markdown,text/plain";
  file.style.display = "none";
  drop.onclick = () => file.click();
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (ev) => {
    ev.preventDefault();
    drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, () => {
    drop.classList.remove("over");
  }));

  const fields = {};
  const field = (key, label, hint, tag) => {
    const input = el(tag || "input", "field");
    input.placeholder = hint || "";
    fields[key] = input;
    return blk(label, input);
  };

  // The division is required. A datalist: existing divisions are suggestions,
  // not a closed set.
  const division = el("input", "field");
  division.placeholder = "engineering, finance, your-own-department…";
  division.setAttribute("list", "roster-divisions-list");
  const list = el("datalist");
  list.id = "roster-divisions-list";
  for (const d of dirs.divisions || []) {
    const o = el("option");
    o.value = d.division;
    o.label = d.n + (d.mine ? " (" + d.mine + " yours)" : "");
    list.append(o);
  }
  fields.division = division;

  form.append(drop, file);
  form.append(blk("division", division, list,
    el("div", "note", "Each division becomes a folder. Pick one of yours, "
      + "or type a new department and it gets created.")));
  form.append(field("name", "name", "Project Shepherd"));
  form.append(field("slug", "file name", "leave blank to use the name"));
  form.append(field("description", "description",
    "one line. This is what the hiring prompt reads.", "textarea"));
  form.append(field("vibe", "vibe", "optional. One line, shown on the card."));

  const short = el("div", "row");
  const emoji = el("input", "field");
  emoji.placeholder = "🐑";
  emoji.style.maxWidth = "6em";
  const color = el("input", "field");
  color.placeholder = "blue";
  color.setAttribute("list", "roster-colors-list");
  const colors = el("datalist");
  colors.id = "roster-colors-list";
  for (const c of ["red", "orange", "yellow", "green", "teal", "blue", "purple",
                   "pink", "brown", "grey"]) {
    const o = el("option");
    o.value = c;
    colors.append(o);
  }
  fields.emoji = emoji;
  fields.color = color;
  short.append(emoji, color, colors);
  form.append(blk("emoji and colour", short,
    el("div", "note", "both optional. They are the face on the Standby card.")));

  const bodyBox = el("textarea", "field tall");
  bodyBox.placeholder = "# Who they are\n\nYou are …\n\n## How they work\n\n…";
  fields.body = bodyBox;
  form.append(blk("the persona itself", bodyBox,
    el("div", "note", "Markdown. Headings become the sections shown on the "
      + "persona card. This is the part an agent is actually given; the "
      + "frontmatter above is only how it gets found.")));

  const replace = el("input");
  replace.type = "checkbox";
  const replaceRow = el("label", "row");
  replaceRow.append(replace, el("span", null,
    " replace a persona of mine with the same file name"));
  form.append(replaceRow);

  const fill = (text, filename) => {
    const parsed = parseFrontmatter(text);
    for (const key of ["name", "description", "emoji", "color", "vibe"]) {
      if (parsed.meta[key]) fields[key].value = parsed.meta[key];
    }
    bodyBox.value = parsed.body;
    if (filename) {
      const stem = filename.replace(/\.mdx?$/i, "");
      fields.slug.value = stem;
      if (!fields.name.value) fields.name.value = stem;
    }
    drop.textContent = (filename ? "loaded " + filename : "loaded")
      + ", check the division, then add";
    if (!division.value) division.focus();
  };
  const take = (f) => {
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => fill(String(reader.result || ""), f.name);
    reader.readAsText(f);
  };
  drop.addEventListener("drop", (ev) => {
    ev.preventDefault();
    take(ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0]);
  });
  file.onchange = () => take(file.files && file.files[0]);

  const save = el("button", "act", "add persona");
  save.onclick = async () => {
    if (!fields.name.value.trim()) { toast("a persona needs a name", "bad"); return; }
    if (!division.value.trim()) { toast("pick or type a division", "bad"); return; }
    save.disabled = true;
    const out = await act("persona", {
      division: division.value,
      slug: fields.slug.value || fields.name.value,
      name: fields.name.value,
      description: fields.description.value,
      emoji: emoji.value,
      color: color.value,
      vibe: fields.vibe.value,
      body: bodyBox.value,
      overwrite: replace.checked,
    });
    save.disabled = false;
    if (!out) return;                       // `act` has already said why
    toast("added. " + out.total + " personas on file");
    closeDrawer();
  };
  form.append(save);
  body.append(form);
}

$("roster-add").onclick = openPersonaNew;
$("roster-rescan").onclick = async () => {
  const out = await act("rescan", {});
  if (out) {
    toast(out.total + " personas (" + out.local + " yours), "
      + out.added.length + " added, " + out.changed.length + " changed, "
      + out.removed.length + " gone");
  }
};

// ── drawer ──────────────────────────────────────────────────────────────────

let ALL_PROJECTS = [];   // every folder with a PROJECT.md, for the confirm pickers

// A drawer can open another, so the drawer keeps a trail back. `openDrawer`
// runs at the top of every open* function, before the fetch, so whether a
// drawer was already open tells a step down from a fresh start.
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
  // other mode. Otherwise the only way out is a button you have to aim at.
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

// Notion checklists are stored as JSON arrays. Missing (pre-008) and empty both
// read as "no checklist".
function jsonList(raw) {
  if (!raw) return [];
  try { const v = JSON.parse(raw); return Array.isArray(v) ? v : []; } catch (_) { return []; }
}

// ── console ─────────────────────────────────────────────────────────────────
// A shell in a drawer: a terminal that answers while you watch, with full tool
// access and no worktree. `console.py` explains why. The transcript is polled,
// not on the SSE snapshot, so an open chat does not repaint the board.

const CONSOLE_POLL_MS = 1500;

async function openConsole() {
  const body = openDrawer("ordis \u00b7 console",
                          "Talking Directly, with a Shell",
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

  // Both dropdowns come from what the server says the CLI accepts.
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

  // Compact sends `/compact` into the same session, so it costs tokens and
  // shows in the tape.
  const compact = el("button", "act", "compact");
  compact.title = "summarise this conversation so far and keep going in less context";
  const clear = el("button", "act", "clear");
  clear.title = "start a new conversation \u2014 they forget everything above this line";
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

  // The command list is read off disk by the server (built-ins plus installed
  // skills). Picking one pastes it at the cursor; it never sends.
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

  // Read from anywhere the token reaches; write only from this machine, because
  // the console is a shell and the token crosses the LAN in plain HTTP. The
  // server refuses either way (`_desk_only`); this row explains before you
  // type. Held here because `paint` also gets `/api/console/options` replies,
  // which lack these keys.
  let writable = data.writable !== false;
  let remote = data.remote === true;
  const desk = data.desk === true;

  const access = el("div", "access");
  prompt.prepend(access);

  // Remote access can be turned off from anywhere but on only at the desk (see
  // `act_console_remote`), so the phone gets a button in one direction.
  function drawAccess() {
    access.replaceChildren();
    if (desk) {
      const btn = el("button", "act", remote ? "restrict to this machine"
                                            : "answer from anywhere");
      btn.onclick = async () => {
        if (!remote && !confirm(
            "let the console take commands from your phone?\n\n"
            + "it is a real shell with no restrictions, and the access token "
            + "that reaches it crosses your network as plain HTTP. anything on "
            + "that network which reads the token gets a command prompt on this "
            + "machine.\n\nyou can turn this back off from anywhere.")) return;
        btn.disabled = true;
        const out = await act("console-remote", { on: !remote });
        btn.disabled = false;
        if (!out) return;
        remote = out.remote;
        writable = true;
        toast(remote ? "console now answers from anywhere"
                     : "console restricted to this machine");
        drawAccess();
      };
      access.append(el("span", "note", remote
        ? "This console takes commands from any device with the access token. "
        : "This console only takes commands from this machine. "), btn);
      return;
    }
    if (remote) {
      const btn = el("button", "act", "restrict to the desktop");
      btn.onclick = async () => {
        btn.disabled = true;
        const out = await act("console-remote", { on: false });
        if (!out) { btn.disabled = false; return; }
        remote = false;
        writable = false;
        toast("console restricted to the desktop. Including this page");
        drawAccess();
        paint(await getJSON("/api/console").catch(() => ({ turns: [] })));
      };
      access.append(el("span", "note",
        "The console is open to the network, so this page can run a real shell "
        + "on the machine at home. Turning that off works from here; turning it "
        + "back on does not. "), btn);
      return;
    }
    access.append(el("span", "note",
      "The console is a real shell on the machine running the colony, so it "
      + "only takes commands from that machine. An access token that "
      + "leaked off your network should not be worth a command prompt. You can "
      + "still read everything it did. To open it up, use the console on the "
      + "desktop."));
  }
  drawAccess();

  // Only the tape is replaced, so a half-typed message survives a poll. Scroll
  // is forced only if you were already at the bottom.
  function paint(st) {
    const stuck = tape.scrollHeight - tape.scrollTop - tape.clientHeight < 60;
    tape.replaceChildren();
    if (!st.turns.length) {
      tape.append(el("div", "empty",
        "nothing yet. this is a fresh session \u2014 they have no memory of the last one."));
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
    send.disabled = busy || !writable;
    // Off until a session exists; compacting nothing still costs tokens.
    compact.disabled = busy || !writable || !st.resuming;
    clear.disabled = busy || !writable || !st.turns.length;
    box.disabled = !writable;
    box.placeholder = writable
      ? "what do you want changed? enter sends, shift+enter for a new line"
      : "read-only from here";
    cmds.disabled = !writable;
    send.textContent = busy ? "working\u2026" : "send";
    hint.textContent = busy
      ? "they are running \u2014 shell commands can take minutes"
      : (st.resuming ? "same session, they remember everything above" : "new session");
    if (stuck) tape.scrollTop = tape.scrollHeight;
  }

  paint(data);
  box.focus();

  // No unmount hook, so the poll stops when its node leaves the document.
  const timer = setInterval(async () => {
    if (!wrap.isConnected || !$("drawer").classList.contains("on")) {
      clearInterval(timer);
      return;
    }
    try { paint(await getJSON("/api/console")); } catch (_) {}
  }, CONSOLE_POLL_MS);

  // `override` is how compact gets in: an ordinary message with a fixed body,
  // through the same queue and lock.
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
        "clear the console? they keep no memory of these " + st.turns.length
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

// ── story drawer ────────────────────────────────────────────────────────────

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

  // First, if this story is not moving, why.
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

  // Say something about this story without waiting to be asked. It becomes a
  // ticket when sent, like any reply.
  const talk = el("button", "act go", "reply to ordis about this story");
  talk.style.width = "100%";
  talk.title = "queued for the next wake. It shows up in the Ticket Queue straight away";
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
    if (out) { toast("queued. The next wake will build it", "good"); openStory(s.id); }
  };
  gate.append(disp);

  // Drop, or undo the drop, beside the other decisions about a story.
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
    kill.title = "off the board, questions closed, tickets cancelled. reversible";
    kill.onclick = () => dropStory(s.id, "“" + s.title + "”");
    gate.append(kill);
  }
  body.append(gate);

  // ── the checklist, both halves ────────────────────────────────────────────
  // What the colony believes is done and still open, from the Notion checklist.
  // Check this first when a question seems out of date.
  const done = jsonList(s.done_items), open = jsonList(s.open_items);
  if (done.length || open.length) {
    const list = el("div", "blk");
    for (const it of done) list.append(el("div", "dim", "✓  " + it));
    for (const it of open) list.append(el("div", null, "☐  " + it));
    body.append(blk(`to-dos · ${done.length} of ${done.length + open.length} done`, list));
  }

  // ── talking back to Notion ────────────────────────────────────────────────
  // Everything here queues; the next pulse sends it. A Notion outage costs a
  // delay, not a decision.
  if (s.notion_page_id) {
    const push = el("div", "form");
    const statuses = (STATE && STATE.controls && STATE.controls.notion_statuses) || [];

    push.append(el("label", null, "status"));
    const row = el("div"); row.style.display = "flex"; row.style.gap = "6px";
    const pick = el("select", "pick");
    pick.append(el("option", null, "set status…"));
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
    say.placeholder = "leave a note on the Notion page. Posted as Ordis";
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

  // Who is on this story, lead first: the lead's seat gets the implement
  // ticket.
  if (data.crew && data.crew.length) {
    const rows = el("div", "rows");
    for (const a of data.crew) {
      const r = el("div", "kv");
      const who = a.seat === 0 ? "lead" : "seat " + a.seat;
      const dt = el("dt", null, a.role);
      dt.append(el("div", "stamp", who + (a.story_id ? "" : "  ·  hired to the folder")));
      const dd = el("dd", null, (a.name || a.roster_slug || "hand-written contract")
                                + "  ·  " + a.status
                                + "  ·  " + (a.model || "").replace("claude-", ""));
      // No `story_id`: hired by hand or before seats, working here because
      // nobody else is.
      if (!a.story_id) {
        dd.append(el("div", "stamp",
          "cut for the project, not for this story. It takes the ticket only "
          + "while this story has no crew of its own"));
      }
      r.append(dt, dd);
      rows.append(r);
    }
    const label = data.crew.length === 1 ? "crew  ·  1 person"
                                         : "crew  ·  " + data.crew.length + " people";
    body.append(blk(label, rows));
  }

  if (data.tickets.length) {
    // `.rows`, not `.blk`: the list scrolls inside itself, capped like the
    // brief.
    const rows = el("div", "rows");
    for (const t of data.tickets) {
      const r = el("div", "kv");
      // Every answered escalation writes a decision ticket, so what was decided
      // sits beside what was built. Stored as `chore` because
      // `tickets.intent`'s CHECK cannot be widened in place; `decided_esc_id`
      // marks it.
      const decision = !!t.decided_esc_id;
      const dd = el("dd", null, decision ? (t.findings || "decided")
                                         : `${t.status} · ${t.role}`);
      // Opened and closed dates; ids give only the order.
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
      // "started" places the run in the week; "took" is how long the agent
      // worked.
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
// Everything between one deliverable and the next, in order: what happened and
// what came out of it. `arg` is "kind:id" because the trail stores one argument
// per view.
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

  // The agent's account of the criteria. `skipped` matters most.
  const f = d.findings || {};
  if (f.summary) body.append(sectionBlock("what it delivered", f.summary));
  // `list` takes plain strings. `skipped` arrives as {criterion, why} and
  // `risks`/`learned` as single strings, so they are handled separately; a
  // string in for..of iterates characters.
  const list = (label, arr, mark) => {
    if (!Array.isArray(arr) || !arr.length) return;
    const box = el("div", "blk");
    for (const item of arr) box.append(el("div", null, mark + "  " + item));
    body.append(blk(label + "  ·  " + arr.length, box));
  };
  const skipped = (Array.isArray(f.skipped) ? f.skipped : []).map((sk) =>
    sk && typeof sk === "object"
      ? [sk.criterion, sk.why].filter(Boolean).join(": ")
      : String(sk));
  list("criteria met", f.done, "✓");
  list("skipped", skipped, "✗");
  // Commands the agent handed over instead of running.
  list("handed over to be run", (Array.isArray(f.needs_run) ? f.needs_run : [])
    .map((n) => n && typeof n === "object"
      ? [n.command, n.why].filter(Boolean).join(": ") : String(n)), "$");
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

  // The whole conversation in order, rendered like the story timeline.
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

// One label per entry, in the voice it was said in. Event kinds keep the
// ledger's own words ("groomed", "staffed", "decided").
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

// Which voice an event is in. Bookkeeping stays grey, so the four coloured
// voices stand out.
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
// The whole persona file: what it refuses and what "done" means to it, to read
// before hiring.

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
  add("source", p.source === "local"
    ? "yours, written on this machine, safe from a git pull"
    : "the agency-agents clone, read only, and a pull upstream can rewrite it");
  if (p.hired) add("status", `hired as “${p.hired.role}”${p.hired.project ? " on " + p.hired.project : ""}`);
  body.append(facts);

  if (p.description) body.append(el("div", null, p.description));
  if (p.error) body.append(el("div", "empty", p.error));

  body.append(hireForm(p));

  // Every heading becomes a block; an unsplittable file is one section.
  for (const sec of p.sections) {
    if (!sec.body) { body.append(el("div", "lb mono dim", sec.title)); continue; }
    body.append(blk(sec.title || "criteria", el("pre", "detail tall", sec.body)));
  }
  if (!p.sections.length && p.body) body.append(sectionBlock("persona file", p.body));

  // Only for this machine's personas. Deleting an agency one would dirty
  // someone else's clone and come back on the next pull. The server refuses it
  // too.
  if (p.source === "local") {
    const gone = el("button", "act warn", "delete this persona");
    gone.onclick = () => confirmThen(
      `Delete ${p.name}?

The file at ${p.path} is removed. Any agent already `
      + `hired from it keeps running. A contract is not the résumé it came from.`,
      async () => {
        const out = await act("persona-delete", { slug: p.slug });
        if (out) { toast(`${p.name} removed`); closeDrawer(); }
      });
    body.append(blk("remove", gone));
  }
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
  add("writes", a.write_capable ? "yes. Inside a worktree, after your approval" : "no");
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

// The write scope is the PO's call, so it is editable on the contract. Stories
// can span folders, and an agent correctly refuses to write outside its scope.
//
// The picker offers every project with a PROJECT.md plus the top-level folders
// that contain them. The server checks the folder exists before storing it.
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
    if (!folders.length) list.append(el("span", "dim", "no folder. This agent cannot build"));
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
    const head = el("option", null, "add a folder…");
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
  return blk("write scope. The folders this agent may edit, inside its worktree",
             list, row, typed);
}

// Whether this agent's checkout contains the `.env` files. Off by default.
// Separate from the scope editor: this is about reading a file git never puts
// in a worktree.
function secretsEditor(a) {
  let on = !!a.sees_secrets;
  const state = el("div", "dim");
  const btn = el("button", "act");
  const draw = () => {
    state.textContent = on
      ? "Its checkout gets a copy of every .env found in the projects it works "
        + "in. Values never reach a patch, the files are removed before the "
        + "diff is taken, but the agent can read them, so its report could "
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
  return blk("credentials. Whether .env is copied into its checkout", state, btn);
}

async function openPulse(id) {
  const body = openDrawer("pulse #" + id, "", { nav: { kind: "pulse", arg: id, label: "the beat" } });
  let p;
  try { p = await getJSON("/api/pulse/" + id); }
  catch { body.replaceChildren(el("div", "empty", "could not load that beat")); return; }
  // "escalated", not "wake": a wake was considered and nothing was spent.
  const escalated = p.tier === "wake" && !p.acted;
  $("d-eyebrow").textContent =
    `${tierOf(p)}${escalated ? " · escalated" : ""} · ${p.pulse_at}`;
  $("d-title").textContent = p.finding || "clean";
  body.replaceChildren();

  const facts = el("dl", "kv");
  const add = (k, v) => { facts.append(el("dt", null, k), el("dd", null, v)); };
  add("window", `${p.window_start} → ${p.window_end}`);
  add("took", (p.duration_ms / 1000).toFixed(1) + "s");
  add("spent", p.tokens ? toks(p.tokens) + " tok" : "nothing. The heartbeat is free");
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
  // Branch and sha belong to the repo, not the folder.
  $("d-eyebrow").textContent = repo.branch ? `${repo.branch} · ${repo.sha}` : "project";
  $("d-title").textContent = p.project;
  body.replaceChildren();

  // Every count is files differing from one commit. Labels use plain words:
  // "never committed" instead of git's "untracked".
  const kinds = p.kinds || {};
  const facts = el("dl", "kv");
  const add = (k, v, why) => {
    const dd = el("dd", null, v);
    if (why) dd.title = why;
    facts.append(el("dt", null, k), dd);
  };
  add("compared with", repo.sha
    ? `${repo.sha} on ${repo.branch}` + (repo.subject ? `: "${repo.subject}"` : "")
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
    add("in total", "clean. Every file here matches that commit");
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

// What changed in each moved folder, where, when and by whom.
const KIND_WORD = { A: "added", M: "edited", D: "deleted", "??": "new", R: "renamed" };
const KIND_CLS = { A: "a", M: "m", D: "d", "??": "u", R: "m" };

function fileKind(xy) {
  const k = String(xy || "").trim();
  if (k === "??" || k === "?") return ["new", "u"];
  for (const ch of k) if (KIND_WORD[ch]) return [KIND_WORD[ch], KIND_CLS[ch]];
  return [k || "changed", ""];
}

// The change since the previous beat. A null delta is a first sighting, not a
// zero.
function movedDelta(c) {
  const bits = [];
  for (const [k, word] of [["modified", "edited"], ["untracked", "new"],
                           ["added", "added"], ["deleted", "deleted"]]) {
    const d = c["d_" + k];
    if (d) bits.push((d > 0 ? "+" : "") + d + " " + word);
  }
  if (bits.length) return bits.join(", ") + " since the previous beat";
  // Pre-012 beats stored no file list, which differs from a first sighting.
  if (c.files == null) return "this beat predates the movement log";
  if (c.d_modified == null && c.d_untracked == null) return "first time this folder was sampled";
  if (c.commits_since) return "the same files. What moved was the commit";
  return "counts unchanged since the previous beat";
}

function movedList(changes) {
  const box = el("div", "moved");
  for (const c of changes) {
    const item = el("div", "item");

    const name = el("button", "name", c.project);
    name.onclick = () => openProject(c.project);
    item.append(name);
    // The colony writes only through approved patches, so it can say for
    // certain when a change was not its doing.
    const by = el("span", "who", c.moved_by === "colony" ? "the colony" : "not the colony");
    by.title = c.moved_by === "colony"
      ? "a patch you approved was applied into this folder during this beat"
      : "the colony did not write here. Something else on the machine did";
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
    // modification". The counts never pointed at anything.
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
// whole point of the view. Line-prefix colouring only. No parser, no library.
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
  const body = openDrawer("patch waiting", e.story_title || "Write Approval");
  body.replaceChildren(el("div", "dim", "reading the patch..."));

  let p;
  try {
    p = await getJSON("/api/patch?escalation_id=" + e.id);
  } catch (err) {
    body.replaceChildren(el("div", "bad", "could not read the patch: " + err.message));
    return;
  }
  body.replaceChildren();

  // The headline: how much moves, what it cost, and whether the run finished
  // cleanly.
  const t = (p.stat && p.stat.total) || { files: 0, added: 0, removed: 0 };
  const chips = el("div", "meta");
  chips.append(tag(t.files + " file" + (t.files === 1 ? "" : "s")),
               tag("+" + t.added, "good"),
               tag("-" + t.removed, "bad"));
  if (t.binary) chips.append(tag(t.binary + " binary"));
  if (t.outside) chips.append(tag(t.outside + " outside scope", "bad"));
  if (p.project) chips.append(tag(p.project + "/"));
  if (p.run) {
    if (p.run.chargeable_tokens) chips.append(tag(toks(p.run.chargeable_tokens) + " tok"));
    if (p.run.cost_usd) chips.append(tag(usd(p.run.cost_usd)));
    if (p.run.model) chips.append(tag(String(p.run.model).replace("claude-", "")));
    if (p.run.status && p.run.status !== "ok") chips.append(tag(p.run.status, "boosted"));
  }
  if (p.ticket && p.ticket.role) chips.append(tag(p.ticket.role));
  body.append(chips);

  // What the agent says it did: a claim, and the diff is the evidence. First,
  // because it says which criteria it aimed at.
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
      ul.append(el("li", "missed", (sk.criterion || "") + ": " + (sk.why || "")));
    }
    body.append(blk("skipped, and why", ul));
  }
  if (r.risks) body.append(sectionBlock("look closely at", String(r.risks)));

  const stray = ((p.stat && p.stat.files) || []).filter(f => f.outside);
  if (stray.length) {
    body.append(sectionBlock("outside the write scope",
      stray.map(f => f.path).join("\n") + "\n\nApply will refuse these. Widen the "
      + "contract's write scope (" + (p.scope || []).join(", ") + ") or reject."));
  }

  // The decision buttons above the file list, not under the diff.
  const acts = el("div", "row");
  acts.style.display = "flex"; acts.style.gap = "7px"; acts.style.margin = "10px 0";
  const yes = el("button", "act go", "apply to the live tree");
  yes.onclick = () => confirmThen(
    "Apply this patch? It lands uncommitted in your working tree. The colony never commits.",
    async () => { if (await act("decide", { escalation_id: e.id, decision: "approve" })) closeDrawer(); });
  const no = el("button", "act no", "reject");
  no.title = "throws the worktree away and puts the story back to ready. The patch file survives.";
  no.onclick = async () => { if (await act("decide", { escalation_id: e.id, decision: "reject" })) closeDrawer(); };
  acts.append(yes, no);
  body.append(acts);

  // Per file, largest change first, since totals hide how the change is spread.
  const files = ((p.stat && p.stat.files) || []).slice()
    .sort((a, b) => (b.added + b.removed) - (a.added + a.removed));
  if (files.length) {
    const tbl = el("table", "filestat");
    for (const f of files) {
      const tr = el("tr");
      tr.append(el("td", "verb " + f.verb, f.verb),
                el("td", "path" + (f.outside ? " bad" : ""), f.outside ? f.path + "  (outside scope)" : f.path),
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

  // `paintDiff` caps at 3000 lines; `truncated` means the server sent less than
  // the file, which must be said separately.
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
// Confirmation only for hard-to-undo acts: halting, applying a patch, retiring
// an agent.

function confirmThen(question, fn) { if (window.confirm(question)) fn(); }

// ── dropping a story ────────────────────────────────────────────────────────
// Two prompts. The first asks why, the most useful fact later; cancelling it
// cancels the drop. The second asks whether to tell Notion, a separate act that
// changes a shared page; the server no-ops it for a story with no page.
function dropStory(id, title) {
  const reason = window.prompt(
    "Drop " + title + "?\n\n" +
    "It comes off the board, its open questions close, and any ticket waiting on " +
    "it is cancelled. Nothing is deleted and you can restore it.\n\n" +
    "Why are you dropping it?", "");
  if (reason === null) return;
  // Shelved, not Archived: Notion creates an unknown select option rather than
  // refusing it.
  const shelve = window.confirm(
    "Also set it to Shelved in Notion?\n\n" +
    "OK queues the change. It goes up on the next pulse. Cancel drops it here only.");
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

// `?theme=ember` overrides the stored choice for this load only, for inspection
// and headless screenshots.
const forced = new URLSearchParams(location.search).get("theme");
let saved = forced || store.get("colony-theme") || "";
// A retired theme name in localStorage falls back to system.
if (saved && !$("theme").querySelector(`option[value="${CSS.escape(saved)}"]`)) {
  store.remove("colony-theme");
  saved = "";
}
// A phone paints its status bar with `theme-color`, so it is read off the
// computed style for every theme and hand-mixed ground.
function paintThemeColor() {
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

const SCALE_KEY = "colony-scale", VARS_KEY = "colony-vars",
      PRESET_KEY = "colony-presets", LAYOUT_KEY = "colony-layout";

// The shipped default is 1.15 and not 1: at the design size the body step is
// 13px, which is right for a wallboard and small for a page you actually read.
const DEFAULT_SCALE = 1.15;

function readJSON(key, fallback) {
  try { return JSON.parse(store.get(key) || "null") || fallback; }
  catch (_) { return fallback; }
}

let SCALE = Number(store.get(SCALE_KEY) || DEFAULT_SCALE) || DEFAULT_SCALE;
let VARS = readJSON(VARS_KEY, {});
let LAYOUT = readJSON(LAYOUT_KEY, {});

// Name, label, what it paints, and what it must stay legible against. A row
// with no audit is a surface colour.
//
// Targets: ink 4.5 on panel, everything else 3, measured from the shipped
// palettes. Every row says what it paints, since `violet` alone drives Ordis,
// two panel titles, the focus ring and a bar. The four accents are the sources;
// downstream tokens can be pinned separately.
const TOKEN_GROUPS = [
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
// Walk lightness away from the background until the ratio clears. The hue is
// chosen for character; the lightness is whatever legibility needs.
function toward(h, s, l, bg, target) {
  const dir = lum(hsl2hex(h, s, l)) > lum(bg) ? 1 : -1;
  for (let i = 0; i < 120 && contrast(hsl2hex(h, s, l), bg) < target; i++) l += dir;
  return hsl2hex(h, s, Math.min(100, Math.max(0, l)));
}

// ── applying ────────────────────────────────────────────────────────────────

// A custom property reads back as text, often a `color-mix(...)` recipe, so a
// zero-sized probe resolves it. Chromium returns `rgb(...)` (0 to 255) for a
// plain colour and `color(srgb ...)` (0 to 1) for a mix; both are parsed.
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
  store.set(SCALE_KEY, String(SCALE));
  store.set(VARS_KEY, JSON.stringify(VARS));
  store.set(LAYOUT_KEY, JSON.stringify(LAYOUT));
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

// Read from the markup before anything moves, so "reset layout" restores the
// page as built.
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
// Dragging is a mode you enter from the drawer and leave by the bar at the
// bottom; tiles wiggle while it is on. A drop inserts rather than swaps, so no
// second tile moves unasked.

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

// Measured against tile middles, so the insert line flips halfway past a tile.
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
// Not uniform dice, which is almost always unreadable and breaks the accents'
// meanings. Neutrals get a random hue, cast and dark or light ground; accents
// vary inside their hue bands; every lightness is solved for its contrast
// target.

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

function presets() { return readJSON(PRESET_KEY, {}); }
function writePresets(all) { store.set(PRESET_KEY, JSON.stringify(all)); }

// A preset is the whole look, colours and text size together.
function savePreset(name) {
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

function applyPreset(name) {
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
    for (const [name, label, does, audit] of g.rows) {
      const row = el("div", "sw");
      const input = document.createElement("input");
      input.type = "color"; input.value = tokenValue(name);
      input.title = name + ": " + does;
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
    box.append(el("div", "empty", "nothing saved yet. Mix a palette above and name it"));
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
// ── the manual ──────────────────────────────────────────────────────────────
// How the colony works, in plain language, for the PO.
//
// A **frozen document**, not a live view: it reads nothing from the ledger and
// will drift. The date is when it was true. When it is wrong, rewrite it whole.
// Data rather than markup, since the page never uses innerHTML.
const MANUAL_AS_OF = "2026-08-22";

const MANUAL = [

{ h: "What this program is",
  p: ["Colony Dash runs a small team of Claude agents on the projects in your projects folder. You file the work as stories, either with the + Story button on the Board or from a Notion database if you keep one. Once an hour this program reads those stories, looks at your disk and your token budget, and decides whether there is anything worth doing. If there is, it does one small piece of it and asks you to approve the result.",
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
       ["project folder", "One directory under your projects folder. A story has to be matched to one before any writing can happen, and you confirm the match yourself."],
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
  ul: ["Write outside your projects folder, ever.",
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
  const body = openDrawer("manual", "How the Colony Works", { wide: true });
  body.replaceChildren();
  body.append(el("p", "man-asof",
    "Written " + MANUAL_AS_OF + ". This is a snapshot, not a live view. Nothing here "
    + "reads the ledger, so it will drift as the code changes. When it is wrong, "
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
// Phone setup from the page: one `POST /api/act/phone` mints the token, writes
// `.env` and installs the task; `GET /api/phone` answers the panel. Neither is
// in `/api/state`, since answering costs a PowerShell call and a socket probe.

// A LAN address stops working away from home; a tailnet one does not. So
// Tailscale's state is drawn under the address it decides.
//
// Both buttons are desk-only, like the server (`act_tailscale`): one runs a UAC
// installer, the other is useful only to whoever finishes the sign-in.
function tailscaleBlock(where, info, reload) {
  const ts = info.tailscale;
  if (!ts) return;
  const here = ["127.0.0.1", "::1", "localhost"].includes(location.hostname);

  const note = (text) => where.append(el("div", "note", text));
  const showCode = (svg) => {
    if (!svg) return;
    const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
    const node = doc.documentElement;
    node.style.width = "min(220px, 55vw)";
    node.style.height = "auto";
    node.style.borderRadius = "6px";
    node.style.marginTop = "8px";
    where.append(node);
  };

  if (ts.connected && info.kind === "tailnet") {
    note("Tailscale is connected" + (ts.name ? " as " + ts.name : "")
         + ", which is why the address above is the one it is.");
    return;
  }

  if (ts.connected) {
    // Almost always a server that started before Tailscale; say the fix.
    note("Tailscale is connected on " + (ts.address || "this machine")
         + ", but the dashboard bound a local address instead, which means it "
         + "started before Tailscale did. Restart the dashboard and it will "
         + "pick the tailnet address up.");
    return;
  }

  if (!ts.installed) {
    note("Tailscale is not installed, so the phone can only reach this on the "
         + "same wifi. Tailscale puts this machine and your phone on one "
         + "private network, which makes cellular work and adds a second lock "
         + "in front of the token. It is free for personal use.");
    if (!here) {
      note("Installing is only offered on the machine itself. Open the "
           + "dashboard on the desktop, or run `py -m colony tailscale --install`.");
      return;
    }
    if (ts.installer) {
      note("There is already an installer in your Downloads folder.");
      const go = el("button", "act", "run the installer");
      go.title = ts.installer;
      go.onclick = async () => {
        go.disabled = true;
        go.textContent = "starting…";
        const out = await act("tailscale", { do: "install" });
        if (out) toast("the installer is opening. Press refresh when it is done");
        reload();
      };
      where.append(go);
    } else {
      const link = el("a", "link", "download Tailscale");
      link.href = ts.download;
      link.target = "_blank";
      link.rel = "noreferrer";
      where.append(link);
      note("Download it, run it, then press refresh above.");
    }
    return;
  }

  // The phone must also be signed in to the same account, which nothing here
  // can check.
  note("Tailscale is installed but not signed in, so the address above is a "
       + "local one and the phone will only reach it on the same wifi.");
  if (!here) {
    note("Signing in is only offered on the machine itself. Open the dashboard "
         + "on the desktop, or run `py -m colony tailscale --login`.");
    return;
  }
  const go = el("button", "act", "sign in to Tailscale");
  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "asking…";
    const out = await act("tailscale", { do: "login" });
    go.remove();
    if (out && out.url) {
      note("Open this to sign in, or point the phone's camera at the code. "
           + "Sign the phone's Tailscale app in to the same account, then "
           + "restart the dashboard.");
      const line = el("div", "mono", out.url);
      line.style.wordBreak = "break-all";
      where.append(line);
      showCode(out.svg);
    } else if (out) {
      note("Tailscale did not ask for a sign-in, which usually means it was "
           + "already signed in and has just reconnected. Press refresh.");
    }
  };
  where.append(go);
}

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
      // Redrawn from the server on both paths, so a half-failed setup does not
      // show as on.
      load();
    };
    // This panel describes the machine, not the ledger, and is not on the live
    // feed, so it needs its own refresh.
    const again = el("button", "link", "refresh");
    again.title = "ask the machine again. Address, firewall, and whether it is serving";
    again.onclick = () => { again.textContent = "checking…"; load(); };

    row.append(button, el("span", "val", on ? "on" : "off"), again);
    set.append(blk("phone access", row));

    if (info.problem) {
      // No private network at all, so there is nothing to bind until one
      // exists.
      const none = el("div", "blk");
      none.append(el("div", "lb", "no address"));
      none.append(el("pre", "detail", info.problem));
      tailscaleBlock(none, info, load);
      set.append(none);
      body.append(set);
      return;
    }

    const where = el("div", "blk");
    where.append(el("div", "lb", "address"));
    where.append(el("div", "mono", info.address + ":" + info.port
                                  + "  (" + (info.kind || "") + ")"));
    if (info.advice) where.append(el("div", "note", info.advice));
    tailscaleBlock(where, info, load);
    if (on && !info.unlimited) {
      where.append(el("div", "note",
        "Task Scheduler will kill this task after three days. Re-run "
        + "`py -m colony autostart` to clear the limit."));
    }
    if (on && !info.serving) {
      where.append(el("div", "note",
        "nothing is answering on that address yet. The logon task waits 45 "
        + "seconds for the network before it binds."));
    }
    set.append(where);

    // The firewall is the failure the address line cannot show: the `serving`
    // probe runs locally and never meets it.
    if (info.firewall === "blocked" || info.firewall === "unknown") {
      const warn = el("div", "blk");
      warn.append(el("div", "lb", "windows firewall"));
      warn.append(el("div", "note", info.firewall === "blocked"
        ? "there is no rule for this port, so the phone's request will be "
          + "dropped rather than refused: the browser loads forever and "
          + "nothing is logged at either end. Run the command below in a "
          + "terminal. It asks for administrator once."
        : "the firewall rules could not be read, which usually means this "
          + "process is not allowed to. If the phone loads forever, check "
          + "this first."));
      const cmd = el("div", "mono", "py -m colony phone --allow-firewall");
      cmd.style.wordBreak = "break-all";
      cmd.style.cursor = "pointer";
      cmd.title = "click to copy";
      cmd.onclick = () => navigator.clipboard.writeText(cmd.textContent)
        .then(() => toast("copied", ""))
        .catch(() => toast("this browser would not copy it. Select it instead", "bad"));
      warn.append(cmd);
      set.append(warn);
    }

    // The one fact measured from the phone's side: whether any packet from
    // another device arrived. Nothing arrived means the network; arrived and
    // refused means a stale token.
    if (on && info.serving) {
      const seen = info.arrivals || [];
      const reach = el("div", "blk");
      reach.append(el("div", "lb", "has anything reached this"));

      if (!seen.length) {
        const started = info.since
          ? new Date(info.since * 1000).toLocaleTimeString(
              undefined, { hour: "numeric", minute: "2-digit" })
          : "";
        reach.append(el("div", "note",
          "No device off this machine has reached the server"
          + (started ? " since it started at " + started : "") + ". The "
          + "address is bound and answering here, so the request is being "
          + "dropped before it arrives. Four things cause that:"));
        const list = el("ul", "note");
        list.style.margin = "6px 0 0";
        list.style.paddingLeft = "18px";
        for (const line of [
          "The phone is on a different network. It has to be on the same one "
            + "as this machine"
            + (info.neighbourhood
                ? ", and on most home routers that means its Wi-Fi address also "
                  + "starts " + info.neighbourhood : "")
            + ". Cellular data instead of Wi-Fi is the usual version of this.",
          "A VPN or private-relay setting on the phone. Those send every "
            + "address out to the internet, and this one only exists inside "
            + "your house. Turn it off and reload.",
          "The router keeps wireless clients away from wired ones. It is "
            + "called client isolation or AP isolation, it is often on by "
            + "default on a guest network, and a guest SSID is the common way "
            + "to hit it by accident.",
          "The phone is on the 5GHz band of a mesh network that routes "
            + "separately from the wired side. Joining the same band this "
            + "machine is on, or the main SSID rather than an extender, "
            + "rules it out.",
        ]) list.append(el("li", null, line));
        reach.append(list);
        reach.append(el("div", "note",
          "To tell them apart in ten seconds: open the phone's Wi-Fi details "
          + "and read its IP address. If it does not look like this machine's, "
          + "it is the first one."));
      } else {
        for (const r of seen) {
          const line = el("div", "row");
          const secs = Math.max(0, (Date.now() / 1000) - r.at);
          const rel = secs < 90 ? "just now"
            : secs < 5400 ? Math.round(secs / 60) + "m ago"
            : Math.round(secs / 3600) + "h ago";
          line.append(el("span", "mono", r.host));
          line.append(el("span", "val", r.ok ? "connected" : "turned away"));
          line.append(el("span", "note", rel + " · " + r.hits
            + (r.hits === 1 ? " request" : " requests")));
          reach.append(line);
        }
        // Refused arrivals are a token problem; say which button fixes it.
        if (seen.some((r) => !r.ok)) {
          reach.append(el("div", "note",
            "Turned away means the request arrived and the token did not "
            + "match, so the network is working. The phone is holding an old "
            + "token: scan the code below again, or paste the token into the "
            + "unlock page it is showing."));
        }
      }
      set.append(reach);
    }

    if (info.url) {
      const link = el("div", "mono", info.url);
      link.style.wordBreak = "break-all";
      link.title = "click to copy";
      link.style.cursor = "pointer";
      link.onclick = () => navigator.clipboard.writeText(info.url)
        .then(() => toast("copied", ""))
        .catch(() => toast("this browser would not copy it. Select it instead", "bad"));
      set.append(blk("open this on the phone", link));

      if (info.svg) {
        // Parsed rather than set via innerHTML, as a rule that does not depend
        // on this string being safe.
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

      // Rotate the token in one press, for a token someone has seen.
      //
      // Only offered on this machine: pressed on a phone, it logs that phone
      // out mid-press and the redraw returns 401. The desktop window is trusted
      // by peer address, so it survives its own click.
      const here = ["127.0.0.1", "::1", "localhost"].includes(location.hostname);
      const roll = el("div", "blk");
      roll.append(el("div", "lb", "token"));
      roll.append(el("div", "note", here
        ? "rotating writes a new token to .env and logs out every phone that "
          + "has the old one. This window stays signed in. It is on this "
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
          if (out) toast("new token written to .env. Scan the code again");
          // Redrawn from the server on both paths, so a failed write cannot
          // leave a QR code for a token not in the file.
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
      el("div", "empty", "could not read phone access. Is the server still up?")));
  load();
}

$("open-appearance").onclick = () => openAppearance();
$("open-manual").onclick = () => { $("filemenu").open = false; openManual(); };
$("open-phone").onclick = () => { $("filemenu").open = false; openPhone(); };

// ── what the page shows ─────────────────────────────────────────────────────
// The page remembers which sections you want. Three rules:
//
//   1. It is local. Hiding a panel does not stop the colony filling it.
//   2. Panels default to *on*, detail to *off*, so a new panel appears for
//      existing users.
//   3. Nothing hides silently: the detail toggles show a count when they hide
//      something.

const VIEW_KEY = "colony-view";
const PANEL_KEYS = ["sprint", "inbox", "flight", "ordis", "colony", "standby",
                    "board", "completed", "files", "spend", "macros", "pulse",
                    "forge"];
let VIEW = {};
try { VIEW = JSON.parse(store.get(VIEW_KEY) || "{}") || {}; } catch (_) { VIEW = {}; }

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
    setConn(false, "disconnected, retrying");
    es.close();
    setTimeout(connect, 3000);
  };
}

fetch("/api/projects").then((r) => r.json()).then((p) => { ALL_PROJECTS = p.all || []; }).catch(() => {});

// The tree is a filesystem read (`git status` plus a listing), not on the SSE
// feed. It loads once and refreshes on request.
renderTree();
$("tree-refresh").onclick = () => renderTree();
$("tree-q").addEventListener("input", (e) => {
  TREE_FILTER = e.target.value.trim();
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
