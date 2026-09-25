// Phone access: pairing, the QR code and the access switch.

import { $, act, el, toast } from "./core.js";
import { blk, getJSON, openDrawer } from "./drawer.js";
import { confirmThen } from "./details.js";
import { openAppearance } from "./appearance.js";
import { openManual } from "./manual.js";

// ── phone access ────────────────────────────────────────────────────────────
// Phone setup from the page: one `POST /api/act/phone` mints the token, writes
// `.env` and installs the task; `GET /api/phone` answers the panel. Neither is
// in `/api/state`, since answering costs a PowerShell call and a socket probe.

// A LAN address stops working away from home; a tailnet one does not. So
// Tailscale's state is drawn under the address it decides.
//
// Both buttons are desk-only, like the server (`act_tailscale`): one runs a UAC
// installer, the other is useful only to whoever finishes the sign-in.
export function tailscaleBlock(where, info, reload) {
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

export function openPhone() {
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
