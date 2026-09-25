// In flight and completed work.

import { $, act, ago, btn, card, chip, el, empty, h, longText, row, setText, store,
  toast, toks, until, usd } from "./core.js";
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
  setText($("flight-count"), items.length ? String(items.length) : "");
  strip.classList.toggle("quiet", !items.length);
  if (!items.length) {
    rail.append(empty("nothing queued"));
    return;
  }
  for (const f of items) rail.append(flightRow(f));
}

export function flightRow(f) {
  // A row with a story opens it, so it is a button. One without a story is
  // not a control, and stays a div.
  const fl = h(f.story_id ? "button.fl" : "div.fl", {
    type: f.story_id ? "button" : null,
    "data-flight": f.key,
    "data-state": f.kind === "push" ? (f.stuck ? "stuck" : "push") : (f.run_id ? "running" : f.status),
  });

  if (f.kind === "push") {
    // A push says what it will do to the board, in the board's words.
    const what = f.verb === "status" ? "→ " + f.what
               : f.verb === "check"  ? (f.checked ? "☑ " : "☐ ") + f.what
               : "💬 " + (f.what || "").slice(0, 80);
    fl.append(chip(what, "t"),
              h("span.m", null, chip(f.stuck ? "gave up" : "queued", "st"), chip("notion"),
                chip(f.story_title || ("story #" + f.story_id), "who")));
    if (f.last_error) fl.append(chip(f.last_error.slice(0, 120), "err"));
    fl.title = f.stuck
      ? "tried " + f.attempts + " times and stopped, the pulse will not retry this on its own"
      : "queued " + ago(f.queued_at) + ", the next pulse sends it. Nothing has changed on the board yet.";
  } else {
    // A reply is a ticket, but it shows as "waiting on Ordis", not its
    // mechanism.
    const reply = !!f.po_message_id;
    fl.append(chip(f.title, "t"),
              h("span.m", null, chip(f.run_id ? "running" : f.status, "st"),
                chip(reply ? "reply" : f.intent),
                chip(f.role || (reply ? "waiting for Ordis" : "unstaffed"), "who")));
    if (reply && f.po_message) fl.append(chip("“" + f.po_message + "”", "said"));
    // A blocked ticket shows what it found.
    if (f.status === "blocked" && f.note) fl.append(chip(f.note, "note"));
    fl.title = (f.status === "blocked" && f.note ? f.note + "\n\n" : "") +
               "ticket #" + f.id + " · " + (f.story_title || "") +
               (f.run_id ? " · run #" + f.run_id + " started " + ago(f.run_started_at)
                         : " · created " + ago(f.created_at));
  }

  fl.onmouseenter = () => lightFlight(f.key, true);
  fl.onmouseleave = () => lightFlight(f.key, false);
  if (f.story_id) fl.onclick = () => openStory(f.story_id);
  return fl;
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
    box.append(items.length ? empty("nothing under this filter")
      : empty("nothing finished yet",
              "a dispatch lands here when its ticket closes, and a story when you file it"));
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
  const open = () => openCompleted(it.kind + ":" + it.id);
  const plural = (n, word, many) => n ? n + " " + word + (n === 1 ? "" : many || "s") : "";
  const counts = it.kind === "dispatch"
    ? [chip(it.done_n + " criteria met", it.done_n ? "good" : null),
       it.skipped_n && chip(it.skipped_n + " skipped", "hot"),
       it.files_n && chip(it.files_n + " files"),
       it.patch && chip("patch")]
    : [chip(it.dispatches ? plural(it.dispatches, "dispatch", "es") : "no dispatch"),
       it.done_n && chip(it.done_n + " of " + (it.done_n + it.skipped_n) + " to-dos")];
  const foot = [it.project, it.role].filter(Boolean);

  const tile = card("done", { "data-kind": it.kind, "data-outcome": it.outcome || null },
    h("div.k", null,
      chip(it.kind === "dispatch"
        ? (it.outcome === "empty" ? "no changes" : "delivered") + "  ·  " + it.ref
        : "filed " + (it.settled_as || "") + "  ·  " + it.ref),
      relNode("age", it.at)),
    // The title is the control. The card is clickable too, for the mouse, but
    // it holds the "show the rest" button and a button cannot hold another.
    btn(it.title || "(untitled)", "r", "open the whole record. The work order, what it "
        + "produced, and every message, question and decision since the last delivery", open),
    // The finish date, in full: the panel is sorted by it and a relative age
    // alone cannot place it.
    h("div.meta", null, "completed " + stamp(it.at) +
      (it.since ? "   ·   since the last delivery on " + stamp(it.since) : "")),
    it.summary && longText(it.summary, 400),
    row("div.tally", counts,
      it.blockers && chip(plural(it.blockers, "blocker"), "hot"),
      it.questions && chip(plural(it.questions, "question")),
      it.messages && chip(plural(it.messages, "message")),
      it.learnings && chip(it.learnings + " learned"),
      it.runs && chip(plural(it.runs, "run")),
      it.tokens && chip(toks(it.tokens) + " tok"),
      usd(it.usd) && chip(usd(it.usd))),
    foot.length && h("div.meta", null, foot.join("   ·   ")));
  const sum = tile.querySelector(".long");
  if (sum) sum.classList.add("sum");
  tile.onclick = (ev) => { if (!ev.target.closest("button")) open(); };
  return tile;
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
  setText(btn, shows("stale") ? "hide stale · " + stale.length : "stale · " + stale.length);

  const waiting = items.filter((e) => !e.stale).length;
  setText($("inbox-count"), waiting ? waiting + " waiting on you" : "");
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
  const decide = (decision, extra) => () => act("decide", { escalation_id: e.id, decision, ...extra });
  const keys = flightKeys(e);

  const tile = card(null, { "data-kind": e.kind, "data-flight": keys.join(" ") || null });
  if (e.snoozed) tile.classList.add("snoozed");
  if (e.stale) tile.classList.add("stale");

  const k = h("div.k", null, chip(e.kind.replace("-", " ")), relNode("age", e.raised_at),
    e.stale && chip("stale", "flag", "the story changed after this was asked. The question is "
                                   + "about a version that no longer exists"));
  if (keys.length) {
    // The marker only appears when there is something to point at. A badge on
    // every tile would be furniture; a badge on three of them is information.
    const pin = btn("queued · " + keys.length, "pin",
      "this question has work in the ticket queue. Hover to find it, click to open the story", () => {
        const first = document.querySelector('.fl[data-flight="' + keys[0] + '"]');
        if (first) first.scrollIntoView({ block: "nearest" });
        if (e.story_id) openStory(e.story_id);
      });
    pin.onmouseenter = () => keys.forEach((key) => lightFlight(key, true));
    pin.onmouseleave = () => keys.forEach((key) => lightFlight(key, false));
    k.append(pin);
  }
  // The x: close the question without answering it, and without the side
  // effects of dropping the story. Not on a write approval, which the server
  // also refuses: a patch and worktree sit behind it.
  if (e.id && e.kind !== "write-approval") {
    const x = btn("×", "dismiss", "this question stopped mattering. Close the card and change "
      + "nothing else. The story keeps its status; edit the brief and Ordis may ask again.",
      decide("dismiss"));
    x.setAttribute("aria-label", "dismiss this question");
    k.append(x);
  }

  const rec = e.recommendation && longText(e.recommendation, 520);
  if (rec) rec.classList.add("rec");
  const meta = [e.story_title, e.est_tokens && toks(e.est_tokens) + " tok"].filter(Boolean);
  tile.append(k, h("div.r", null, e.reason));
  tile.append(...[rec,
    // The second opinion sits under the reasoning, not with the buttons; it is
    // evidence, not a third answer.
    e.second_opinion && h("div.second", null,
      h("div.who", null, "second opinion — agents-orchestrator, read-only"),
      longText(e.second_opinion, 520)),
    e.snoozed && h("div.snooze-note", null, "snoozed · back " + until(e.snoozed_until)),
    // Ordis's answers live in the thread, behind the reply button's count. A
    // queued message does belong here: deciding now decides ahead of an answer
    // you asked for.
    e.awaiting_ordis && h("div.waiting", null,
      e.awaiting_ordis + " repl" + (e.awaiting_ordis === 1 ? "y" : "ies") +
      " queued. Ordis answers on the next pulse"),
    ...(e.blockers || []).map((b) => h("div.blocker", null, b)),
    meta.length && h("div.meta", null, meta.join("  ·  ")),
  ].filter(Boolean));

  const acts = h("div.acts");

  // A story whose folder is still a guess has one answer, so the picker is on
  // the tile. A `needs-info` card is the folder question and gets the picker
  // alone; a `decision` on an unconfirmed story gets both.
  const unconfirmed = e.story_id && e.project_source !== "confirmed";
  const asksForProject = e.kind === "needs-info";
  if (unconfirmed) {
    // The picker's own row. "None of these, it's new" swaps the select for a
    // text field and creates the folder on confirm.
    const sel = projectSelect(e.project, { allowNew: true });
    const field = h("input.field", { placeholder: "new-folder-name" });
    field.style.display = "none";
    const go = btn("confirm project", "act go", null, () => {
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
    });

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
    acts.append(h("div.picker", null, sel, field, go));
  }

  if (e.kind === "ready") {
    // Not an escalation: a state the story is in (see `_ready` in
    // web/state.py), so the controls are "start it" and what still blocks it.
    const stuck = (e.blockers || []).length;
    const go = btn("dispatch to build", "act go", stuck ? e.blockers[0]
      : "cuts the implement ticket. The next wake opens a git worktree and writes in it",
      () => confirmThen(
        "Dispatch “" + (e.story_title || "this story") + "” to build?\n\n" +
        "This cuts a ticket. The next wake opens a git worktree, works in there, and " +
        "brings back a patch. Nothing touches your working tree until you approve it.",
        () => act("dispatch", { story_id: e.story_id })));
    go.disabled = !!stuck;
    acts.append(go);
  } else if (e.kind === "write-approval") {
    acts.append(
      btn("read the patch", "act", null, () => openPatch(e)),
      btn("apply", "act go", null, () => confirmThen(
        "Apply this patch to the live tree? It lands uncommitted. You still review and commit it yourself.",
        decide("approve"))),
      btn("reject", "act no", null, decide("reject")));
  } else if (e.kind === "run-request" && e.id) {
    // The one card that runs something on the live tree. The command is shown
    // verbatim above, since approving an unread command is the risk.
    acts.append(
      btn("run it", "act go", "runs it in the project folder and puts the output on the story",
        () => confirmThen(
          "Run this command against your live tree?\n\n" +
          (e.reason || "").replace(/^.*?: `/, "") .replace(/`$/, "") + "\n\n" +
          "It runs in the project folder with a 90 second limit. Nothing is committed " +
          "and no patch is applied — the output goes on the story so the next " +
          "build can read it.",
          decide("approve"))),
      btn("don’t run it", "act no", "records that you declined; the story keeps its other findings",
        decide("reject")));
  } else if (e.kind === "brief-changed" && e.id) {
    acts.append(
      btn("reopen for grooming", "act go",
        "clears the criteria and puts it back in the groom queue. The next wake re-reads the brief",
        () => confirmThen(
          "Reopen “" + (e.story_title || "this story") + "”?\n\n" +
          "Its acceptance criteria are cleared and the next wake re-reads the whole " +
          "brief from Notion. Anything already built stays built.",
          decide("approve"))),
      btn("leave it", "act no", "records that the edit did not change the work; the card "
        + "returns only if you edit the page again", decide("reject")));
  } else if (e.kind === "hire" && e.id) {
    acts.append(
      btn("approve", "act go", "cuts the contract — write scope is that one project folder "
        + "and nothing else", decide("approve")),
      btn("reject", "act no", "records that this was the wrong person; the next pulse proposes "
        + "someone else", decide("reject")));

    // Ask `agents-orchestrator`, the persona the colony never hires, to audit
    // this pick. It cannot hire, reject or close the card; it writes a
    // paragraph. It reads the full roster, about a third of a grooming run, so
    // it runs only on this button.
    if (!e.second_opinion) {
      const ask = btn("second opinion", "act",
        "asks agents-orchestrator to audit this pick, read-only. It cannot hire "
        + "or refuse anything — it writes its view onto this card and you still decide. "
        + "Costs one run against the full roster and takes a few minutes.",
        () => confirmThen(
          "Ask agents-orchestrator to audit this hire?\n\n" +
          "It reads the story, the picked persona's file and the whole roster, then writes " +
          "its view onto this card. It decides nothing — approve and reject stay yours.\n\n" +
          "This costs one read-only run of about 20k tokens and takes a few minutes. The card " +
          "does not change until it comes back.",
          async () => {
            ask.disabled = true;
            ask.textContent = "reading the roster…";
            const out = await act("second-opinion", { escalation_id: e.id });
            if (out && out.ok) toast("second opinion: " + out.verdict, "good");
            else if (out) toast(out.verdict || "no usable answer came back", "bad");
            else { ask.disabled = false; ask.textContent = "second opinion"; }
          }));
      acts.append(ask);
    }
  } else if (!asksForProject && e.id) {
    acts.append(btn("approve", "act go", null, decide("approve")),
                btn("reject", "act no", null, decide("reject")));
  }

  // Reply: most of what a PO needs to say is a sentence.
  acts.append(btn(e.messages ? "reply · " + e.messages : "reply to Ordis", "act warn",
    "write to Ordis about this item. Queued for the next pulse", () => openCompose(e)));

  // Later is a snooze with an end. The card stays in the Inbox but grays out
  // and sorts last until it expires.
  if (e.snoozed) {
    acts.append(btn("un-snooze", "act", "bring it back to the front of the Inbox now",
      decide("defer", { snooze_hours: 0 })));
  } else if (e.id) {
    acts.append(btn("later", "act", "snooze 8h. It stays in the Inbox, dimmed, because nothing was decided",
      decide("defer", { snooze_hours: 8 })));
  }

  // Re-ask: the answer to a stale question. Resolves it as amended, requeues
  // the groom, and restores the attempts the old groom used.
  if (e.stale) {
    acts.append(btn("re-ask", "act warn", "send it back to Ordis to re-read the current brief "
      + "and ask again if it still needs to", () => act("reask", { escalation_id: e.id })));
  }

  if (e.story_id) {
    // Named for what it opens. "story" read like a category label on the tile;
    // it is a link to the story's own timeline, criteria and events. Drop is
    // here too, since a question about a story is often when you decide it is
    // not the work.
    acts.append(
      btn("open story", "act", "the full story: brief, acceptance criteria, every event on it",
        () => openStory(e.story_id)),
      btn("drop story", "act no", "take the whole story off the board. Closes its questions, "
        + "cancels its tickets", () => dropStory(e.story_id, e.story_title || "this story")));
  }
  tile.append(acts);
  return tile;
}
