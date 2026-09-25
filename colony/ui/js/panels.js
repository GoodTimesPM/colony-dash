// In flight and completed work.

import { $, act, ago, el, longText, store, toast, toks, until, usd } from "./core.js";
import { STATE, relNode, stamp } from "./render.js";
import { NEW_PROJECT, openCompose, projectSelect } from "./composer.js";
import { openCompleted, openStory } from "./drawers.js";
import { confirmThen, dropStory, openPatch } from "./details.js";
import { shows } from "./view.js";

// ── in flight ───────────────────────────────────────────────────────────────
// Tickets the colony is working or staffed to work, and Notion changes queued
// on this machine: things the PO pressed that have not finished.

export function renderFlight(items) {
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

export function flightRow(f) {
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
export const DONE_FILTER_KEY = "colony-done-filter";
export let DONE_FILTER = store.get(DONE_FILTER_KEY) || "all";

export function renderCompleted(items) {
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

export function completedTile(it) {
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
export function lightFlight(key, on) {
  for (const n of document.querySelectorAll('[data-flight~="' + key + '"]')) {
    n.classList.toggle("lit", on);
  }
}

// Which rail rows an escalation is about: the ticket it was raised from, plus
// anything else in flight for the same story.
export function flightKeys(e) {
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

export function renderInbox(items) {
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
export function padSlots(box, count) {
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
export function gridCols(box) {
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

export function inboxTile(e) {
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
