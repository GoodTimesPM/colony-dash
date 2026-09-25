// The console: a shell that answers now.

import { $, act, el, hhmm, toast, toks } from "./core.js";
import { getJSON, openDrawer } from "./drawer.js";

// ── console ─────────────────────────────────────────────────────────────────
// A shell in a drawer: a terminal that answers while you watch, with full tool
// access and no worktree. `console.py` explains why. The transcript is polled,
// not on the SSE snapshot, so an open chat does not repaint the board.

export const CONSOLE_POLL_MS = 1500;

export async function openConsole() {
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
