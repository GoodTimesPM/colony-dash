// Agent, pulse, project and patch views in the drawer, plus confirmations,
// dropping a story and roster search.

import { $, act, ago, el, empty, toast, toks, usd } from "./core.js";
import { stamp, tag, when } from "./render.js";
import { tierOf } from "./composer.js";
import { personaButton } from "./roster.js";
import { HERE, blk, closeDrawer, getJSON, jsonList, openDrawer, renderTrail,
  sectionBlock } from "./drawer.js";

// ── agent, pulse, project, patch drawers ────────────────────────────────────

export async function openAgent(id) {
  const body = openDrawer("agent", "#" + id, { nav: { kind: "agent", arg: id, label: "agent #" + id } });
  const mine = HERE;
  let a;
  try { a = await getJSON("/api/agent/" + id); }
  catch { body.replaceChildren(empty("could not load that contract")); return; }
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
export function scopeEditor(a) {
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
export function secretsEditor(a) {
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

export async function openPulse(id) {
  const body = openDrawer("pulse #" + id, "", { nav: { kind: "pulse", arg: id, label: "the beat" } });
  let p;
  try { p = await getJSON("/api/pulse/" + id); }
  catch { body.replaceChildren(empty("could not load that beat")); return; }
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
  else body.append(empty("no long-form log", "this beat is older than the long-form pulse log, so only its one-line finding was kept"));

  if (p.changes && p.changes.length) body.append(blk("what moved on disk", movedList(p.changes)));
  body.append(sectionBlock("raw record", JSON.stringify(p.actions_json, null, 2)));
}

export async function openProject(name) {
  const body = openDrawer("project", name, { nav: { kind: "project", arg: name, label: name } });
  let p;
  try { p = await getJSON("/api/project?name=" + encodeURIComponent(name)); }
  catch { body.replaceChildren(empty("could not read that folder")); return; }
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
export const KIND_WORD = { A: "added", M: "edited", D: "deleted", "??": "new", R: "renamed" };
export const KIND_CLS = { A: "a", M: "m", D: "d", "??": "u", R: "m" };

export function fileKind(xy) {
  const k = String(xy || "").trim();
  if (k === "??" || k === "?") return ["new", "u"];
  for (const ch of k) if (KIND_WORD[ch]) return [KIND_WORD[ch], KIND_CLS[ch]];
  return [k || "changed", ""];
}

// The change since the previous beat. A null delta is a first sighting, not a
// zero.
export function movedDelta(c) {
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

export function movedList(changes) {
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
export function paintDiff(pre, text) {
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

export async function openPatch(e) {
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

export function confirmThen(question, fn) { if (window.confirm(question)) fn(); }

// ── dropping a story ────────────────────────────────────────────────────────
// Two prompts. The first asks why, the most useful fact later; cancelling it
// cancels the drop. The second asks whether to tell Notion, a separate act that
// changes a shared page; the server no-ops it for a story with no page.
export function dropStory(id, title) {
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

export let rosterTimer = null;
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
    if (!rows.length) { box.append(empty("no persona matches")); return; }
    for (const p of rows) box.append(personaButton(p));
  }, 140);
});
