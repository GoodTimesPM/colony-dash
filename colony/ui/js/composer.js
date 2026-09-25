// Attachments, the reply composer, and filing a story from the dashboard.

import { $, act, ago, el, hhmm, longText, parseTs, refresh, toast, toks } from "./core.js";
import { STATE } from "./render.js";
import { ALL_PROJECTS, TRAIL, closeDrawer, getJSON, jsonList, openDrawer,
  renderTrail, sectionBlock, setAllProjects } from "./drawer.js";
import { confirmThen, openPulse } from "./details.js";

// ── attachments ─────────────────────────────────────────────────────────────
// The upload happens on paste, not on send, so a too-large file is refused
// while you are still looking at it.
export const ATTACH_MAX = 6;

export async function upload(file) {
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
export function attachChip(f, onRemove) {
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

export async function openCompose(e, opts) {
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
export const NEW_PROJECT = "\n<new>";

export function projectSelect(current, opts) {
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

export function openNewStory() {
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
          .then((p) => { setAllProjects(p.all || ALL_PROJECTS); }).catch(() => {});
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

export function renderMacros(c) {
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
export function tierOf(p) {
  if (p.tier !== "wake") return p.tier;
  return p.acted ? "wake" : "tick";
}

// Distinct from a plain tick in the log, because an escalated hour is worth
// reading back and a clean one is not.
export function tierKey(p) {
  if (p.tier !== "wake") return p.tier;
  return p.acted ? "wake" : "escalated";
}

export function renderPulses(rows) {
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
export const DETECTOR_LABEL = {
  "repeat": "seen 3+ times",
  "recovery": "failed, then worked",
  "shortcut": "came in cheap",
  "po-correction": "you kept fixing it",
};

// Why a wake would refuse to spend now, in the pulse's words, or null if it
// would run.
export function standdown() {
  const sp = STATE && STATE.sprint;
  if (!sp || !sp.usage || !sp.allowance) return null;
  const ceiling = sp.allowance.effective;
  const used = Number(sp.usage.seven_day_pct);
  if (!(used >= ceiling)) return null;
  return `week at ${used.toFixed(0)}% of the ${ceiling}% allowance`;
}

export function renderForge(f) {
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
export async function openSkill(id) {
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
