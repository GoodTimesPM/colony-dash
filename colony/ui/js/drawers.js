// Story, completed and persona views in the drawer.

import { $, act, clock, el, empty, longText, parseTs, STATUS_LABEL, toast,
  toks, usd } from "./core.js";
import { STATE, stamp } from "./render.js";
import { openCompose, projectSelect } from "./composer.js";
import { ALL_PROJECTS, HERE, blk, closeDrawer, getJSON, jsonList, openDrawer,
  renderTrail, sectionBlock, setAllProjects } from "./drawer.js";
import { confirmThen, dropStory } from "./details.js";

// ── story drawer ────────────────────────────────────────────────────────────

export async function openStory(id) {
  const body = openDrawer("story #" + id, "", { nav: { kind: "story", arg: id, label: "story #" + id } });
  const mine = HERE;
  let data;
  try { data = await getJSON("/api/story/" + id); }
  catch { body.replaceChildren(empty("could not load story " + id)); return; }

  const s = data.story;
  setAllProjects(data.projects || ALL_PROJECTS);
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
  if (!data.events.length) tl.append(empty("nothing has happened to this story yet"));
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
export async function openCompleted(arg) {
  const [kind, rawId] = String(arg).split(":");
  const id = Number(rawId);
  const body = openDrawer(kind === "dispatch" ? "ticket #" + id : "story #" + id, "",
                          { nav: { kind: "completed", arg: arg, label: "completed " + id } });
  const mine = HERE;
  let d;
  try { d = await getJSON("/api/completed/detail?kind=" + encodeURIComponent(kind) + "&id=" + id); }
  catch { body.replaceChildren(empty("could not load this record")); return; }

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
    tl.append(empty("nothing was said between the last delivery and this one"));
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
export function doneVoice(e) {
  if (e.kind === "po") return "you";
  if (e.kind === "ordis") return "ordis";
  if (e.kind === "learning") return "learned";
  if (e.kind === "question") return "ordis asked" + (e.esc_kind ? "  ·  " + e.esc_kind : "");
  return e.ev_kind || e.kind;
}

export function doneRole(e) {
  if (e.kind === "po" || e.kind === "ordis" || e.kind === "learning") return e.kind;
  if (e.kind === "question") return "ask";
  if (e.ev_kind === "blocked" || e.ev_kind === "escalated") return "ask";
  return null;
}

// Which voice an event is in. Bookkeeping stays grey, so the four coloured
// voices stand out.
export const EV_VOICE = { po: "you", ordis: "ordis", learning: "learned", ask: "ordis asked" };

export function evRole(e) {
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

export async function openPersona(slug) {
  const body = openDrawer("persona", slug, { nav: { kind: "persona", arg: slug, label: slug } });
  const mine = HERE;
  let p;
  try { p = await getJSON("/api/persona?slug=" + encodeURIComponent(slug)); }
  catch { body.replaceChildren(empty("could not read that persona")); return; }

  $("d-eyebrow").textContent = `${p.division} · standby`;
  $("d-title").textContent = p.name;
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
  if (p.error) body.append(empty(p.error));

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

export function hireForm(p) {
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
