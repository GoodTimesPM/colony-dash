// The frame loop: which panel redraws when its slice of state changes, plus
// the board, the filed and dropped lists, and the file tree.

import { $, act, ago, clock, el, empty, hhmm, parseTs, STATUS_LABEL, store, toks, until,
  usd } from "./core.js";
import { drawAvatar } from "./avatars.js";
import { renderCompleted, renderFlight, renderInbox } from "./panels.js";
import { renderForge, renderMacros, renderPulses } from "./composer.js";
import { renderSpend } from "./spend.js";
import { renderDivisions } from "./roster.js";
import { blk, getJSON, openDrawer } from "./drawer.js";
import { openConsole } from "./console.js";
import { openStory } from "./drawers.js";
import { confirmThen, dropStory, openAgent, openProject, openPulse, paintDiff } from "./details.js";
import { shows } from "./view.js";

// ── render ──────────────────────────────────────────────────────────────────

export let STATE = null;
export let filter = null;      // board column filter; null = everything
export let openDivisions = new Set();   // survives re-renders; the SSE feed is frequent

// Each panel with the state keys it reads. A panel is rebuilt only when one of
// those slices changed, so a frame that moves one number does not reset the
// scroll and selection in the other twelve.
export const PANELS = [
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
export const drawn = new Map();   // panel name -> the JSON it was last drawn from

// `force` redraws every panel. Callers pass it when something on the page,
// not in the state, changed what a panel shows: a filter, a view, a theme.
export function render(s, force) {
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
export let rosterRev = null;
export async function syncRoster(rev) {
  if (!rev || rev === rosterRev) return;
  rosterRev = rev;
  try {
    renderDivisions(await getJSON("/api/roster/summary"));
  } catch (_) {
    rosterRev = null;
  }
}

export function tag(text, cls) { const b = el("span"); b.append(el("b", cls || null, text)); return b; }

// A ledger timestamp as a person reads it: "Fri 5:00 AM".
export function when(ts) {
  const d = parseTs(ts);
  if (!d || isNaN(d)) return String(ts || "").slice(0, 16);
  return d.toLocaleString(undefined,
    { weekday: "short", hour: "numeric", minute: "2-digit" });
}

// The same with its date, for things that can be weeks old.
export function stamp(ts) {
  const d = parseTs(ts);
  if (!d || isNaN(d)) return String(ts || "").slice(0, 16);
  return d.toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function renderSprint(sp) {
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

export function renderOrdis(o) {
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

export function renderColony(c) {
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
  if (!box.children.length) box.append(empty("nobody hired yet", "open a persona in Standby to hire it"));
  tickElapsed();   // paint now; the ticker only refreshes from the next second
}

export function agentRow(o) {
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
export function relNode(cls, ts, kind) {
  const n = el("span", cls);
  n.dataset.rel = ts || "";
  n.dataset.relKind = kind === "until" ? "until" : "ago";
  n.textContent = (kind === "until" ? until : ago)(ts);
  return n;
}
export function tickRelative() {
  for (const n of document.querySelectorAll("[data-rel]")) {
    n.textContent = (n.dataset.relKind === "until" ? until : ago)(n.dataset.rel);
  }
}
setInterval(tickRelative, 15000);

export function tickElapsed() {
  for (const n of document.querySelectorAll(".elapsed")) {
    const secs = (Date.now() - Number(n.dataset.started)) / 1000;
    n.textContent = `${clock(secs)} · ${toks(Number(n.dataset.tokens))} tok`;
  }
}
setInterval(tickElapsed, 1000);

export function renderBoard(b) {
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
    box.append(empty(filter ? "nothing in " + STATUS_LABEL[filter] : "board empty"));
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
export const FILED_LABEL = { "done": "done", "shelved": "shelved", "not-started": "not started" };

export function renderFiled(rows) {
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
export function renderDropped(rows) {
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
export const SORT_KEY = "colony-proj-sort";
export let PROJ_SORT = store.get(SORT_KEY) || "changes";
export let PROJ_ROWS = [];

export const PROJ_SORTS = {
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

export function renderProjects(rows) {
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
  if (!rows.length) { box.append(empty("every project matches its last commit")); return; }

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

export const TREE_OPEN = new Set();
export let TREE_FILTER = "";
export function setTreeFilter(q) { TREE_FILTER = q; }

export async function renderTree() {
  const box = $("tree");
  box.replaceChildren(empty("reading the tree…"));
  try {
    const root = await getJSON("/api/tree?path=");
    box.replaceChildren();
    box.append(await treeLevel(root, ""));
  } catch {
    box.replaceChildren(empty("could not read the projects folder"));
  }
}

export async function treeLevel(data, path) {
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
  if (!frag.childNodes.length) frag.append(empty("nothing here"));
  return frag;
}

export async function treeDir(d) {
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
    kids.replaceChildren(empty("…"));
    try {
      const data = await getJSON("/api/tree?path=" + encodeURIComponent(d.path));
      kids.replaceChildren();
      kids.append(await treeLevel(data, d.path));
    } catch {
      kids.replaceChildren(empty("could not open that folder"));
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

export function treeFile(f) {
  const row = el("button", "node file");
  row.append(el("span", "tw", ""), el("span", "ic", "▤"), el("span", "nm", f.name));
  if (f.state) row.append(el("span", "dot " + f.state));
  row.title = f.path + "  ·  " + (f.size > 1024 ? Math.round(f.size / 1024) + " KB" : f.size + " B") +
              (f.state ? "  ·  " + f.state : "");
  row.onclick = () => openFile(f);
  return row;
}

export async function openFile(f) {
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
      ? empty(data.why || "nothing to show")
      : blk("contents", el("pre", "detail tall", data.text)));
  } catch {
    blocks.push(empty("this dashboard will not open that file"));
  }
  body.replaceChildren(...blocks);
}
