"""`python -m colony <command>`. Inspect and operate the ledger from a
terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import db, roster as roster_mod, seed as seed_mod, usage as usage_mod

# Tokens are the unit; dollars are the grey secondary (ARCHITECTURE.md §6). No
# escapes when piped. `sys.stdout` is None under pythonw, and this runs at
# import.
_COLOR = bool(sys.stdout and sys.stdout.isatty()) and not os.environ.get("NO_COLOR")
DIM = "\033[2m" if _COLOR else ""
RESET = "\033[0m" if _COLOR else ""


def toks(n: int | None) -> str:
    if not n:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def usd(amount: float | None) -> str:
    return f"{DIM}${amount:.2f}{RESET}" if amount else ""


def rule(title: str) -> None:
    print(f"\n{title}\n{'─' * max(len(title), 40)}")


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_init(conn: sqlite3.Connection, args) -> int:
    print(f"ledger  {db.LEDGER_PATH}")
    ran = db.migrate(conn)
    if not ran:
        print("  schema already current")
    result = seed_mod.seed(conn)
    if result["agents"]:
        print(f"  seeded {result['agents']} structural agents (investigator, reviewer)")
    if result["sprint_id"]:
        print(f"  opened sprint {result['sprint_id']}")

    if not args.skip_roster:
        try:
            stats = roster_mod.sync(conn, Path(args.roster_dir) if args.roster_dir else roster_mod.DEFAULT_ROSTER_DIR)
            print(f"  scanned {stats['total']} personas across {len(stats['divisions'])} divisions")
        except FileNotFoundError as exc:
            print(f"  roster skipped: {exc}")
    print("\nready.  `python -m colony status`")
    return 0


def cmd_status(conn: sqlite3.Connection, args) -> int:
    sprint = conn.execute(
        "SELECT * FROM sprints WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if sprint:
        # By run date within the sprint window, not story.sprint_id: Notion
        # stories arrive without a sprint.
        spent = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(chargeable_tokens, total_tokens)), 0) AS tok,
                   COALESCE(SUM(cost_usd), 0) AS usd
              FROM runs
             WHERE started_at >= ? AND started_at < ?
            """,
            # Half-open on the instant; the week turns at 05:00 Friday, not
            # midnight.
            (sprint["starts_at"] or sprint["starts_on"] + " 00:00:00",
             sprint["ends_at"] or sprint["ends_on"] + " 00:00:00"),
        ).fetchone()
        rule(f"{sprint['name']}: {sprint['goal'] or 'no goal set'}")
        print(f"  {sprint['starts_at'] or sprint['starts_on']} → "
              f"{sprint['ends_at'] or sprint['ends_on']}"
              f"   allowance {sprint['budget_pct']:.0f}% of week")
        print(f"  spent  {toks(spent['tok'])} tok  {usd(spent['usd'])}")
    else:
        rule("no active sprint")

    # The cache, not the last hourly copy of it. `colony status` typed at 09:55
    # should not be reporting the 09:07 pulse's idea of the week.
    live = usage_mod.read()
    usage = conn.execute("SELECT * FROM usage_samples ORDER BY sampled_at DESC LIMIT 1").fetchone()
    if live:
        print(f"  week   {float(live['seven_day'] or 0):.1f}% used   "
              f"resets {live['seven_day_resets'] or usage_mod.next_reset()}"
              + (f"   {DIM}(cache stale){RESET}" if live["stale"] else ""))
    elif usage:
        print(f"  week   {usage['seven_day_pct']:.1f}% used   resets {usage['seven_day_resets_at']}")
    else:
        print(f"  week   {DIM}no usage sample yet. See ARCHITECTURE.md §6.2{RESET}")

    rule("board")
    counts = conn.execute("SELECT status, COUNT(*) n FROM stories GROUP BY status").fetchall()
    print("  " + ("   ".join(f"{r['status']} {r['n']}" for r in counts) if counts else "empty"))

    rule("colony")
    live = conn.execute(
        """
        SELECT r.agent_role, r.model, r.status, r.ticket_id, r.total_tokens
        FROM runs r WHERE r.status = 'running' ORDER BY r.started_at
        """
    ).fetchall()
    if live:
        for r in live:
            print(f"  ● {r['agent_role']:<16}{r['model']:<18}t{r['ticket_id']:<6}{toks(r['total_tokens'])}")
    else:
        active = conn.execute("SELECT COUNT(*) n FROM agents WHERE status = 'active'").fetchone()["n"]
        print(f"  {active} agents on the books, none running")

    rule("po inbox")
    open_esc = conn.execute(
        "SELECT * FROM escalations WHERE resolved_at IS NULL ORDER BY raised_at DESC"
    ).fetchall()
    if not open_esc:
        print(f"  {DIM}empty. Nothing needs you{RESET}")
    for e in open_esc:
        print(f"  [{e['kind']}] {e['reason']}")
        if e["recommendation"]:
            print(f"      → {e['recommendation']}  {toks(e['est_tokens'])} tok")

    rule("pulse log")
    pulses = conn.execute("SELECT * FROM pulses ORDER BY pulse_at DESC LIMIT 8").fetchall()
    if not pulses:
        print(f"  {DIM}no pulses yet{RESET}")
    for p in pulses:
        print(f"  {p['pulse_at']}  {p['tier']:<5} {p['finding'] or '':<34} {toks(p['tokens'])}")
    print()
    return 0


def cmd_roster(conn: sqlite3.Connection, args) -> int:
    if args.sync:
        root = Path(args.roster_dir) if args.roster_dir else roster_mod.DEFAULT_ROSTER_DIR
        stats = roster_mod.sync(conn, root)
        print(f"scanned {stats['total']} personas from {root}")
        for label in ("added", "changed", "removed"):
            if stats[label]:
                print(f"  {label}: {len(stats[label])}")
                for slug in stats[label][:10]:
                    print(f"    {slug}")
        return 0

    if args.query:
        rows = roster_mod.search(conn, args.query, limit=args.limit)
        rule(f"roster search: {args.query}")
    elif args.division:
        rows = conn.execute(
            "SELECT * FROM roster WHERE division = ? ORDER BY name LIMIT ?",
            (args.division, args.limit),
        ).fetchall()
        rule(f"division: {args.division}")
    else:
        rule("divisions")
        for r in conn.execute(
            "SELECT division, COUNT(*) n FROM roster GROUP BY division ORDER BY n DESC"
        ):
            print(f"  {r['division']:<22}{r['n']:>4}")
        total = conn.execute("SELECT COUNT(*) n FROM roster").fetchone()["n"]
        print(f"  {'total':<22}{total:>4}")
        return 0

    for r in rows:
        print(f"  {r['emoji'] or ' '} {r['name']:<34}{DIM}{r['slug']}{RESET}")
        if r["vibe"]:
            print(f"      {DIM}{r['vibe']}{RESET}")
    return 0


def cmd_agents(conn: sqlite3.Connection, args) -> int:
    rule("hired")
    for a in conn.execute("SELECT * FROM agents ORDER BY project IS NOT NULL, role"):
        scope = a["project"] or "structural"
        write = "write" if a["write_capable"] else "read-only"
        print(f"  {a['role']:<18}{scope:<22}{a['model']:<18}{write:<11}ceiling {toks(a['max_tokens_run'])}")
        print(f"      {DIM}tools {', '.join(json.loads(a['tools_allowed']))}{RESET}")
    return 0


def cmd_sql(conn: sqlite3.Connection, args) -> int:
    query = " ".join(args.query)
    if not query.lower().lstrip().startswith("select"):
        print("read-only: SELECT statements only", file=sys.stderr)
        return 2
    rows = conn.execute(query).fetchall()
    if not rows:
        print("(no rows)")
        return 0
    headers = rows[0].keys()
    widths = [max(len(h), *(len(str(r[h])) for r in rows)) for h in headers]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("─" * w for w in widths))
    for r in rows:
        print("  ".join(str(r[h]).ljust(w) for h, w in zip(headers, widths)))
    return 0


def cmd_mirror(conn: sqlite3.Connection, args) -> int:
    from . import mirror as mirror_mod

    print("mirroring ledger → MySQL")
    counts = mirror_mod.mirror(conn)
    print(f"  {sum(counts.values())} rows across {len(counts)} tables")
    return 0


def cmd_dash(conn: sqlite3.Connection, args) -> int:
    # The dashboard opens its own connection; close this one so it blocks
    # nothing.
    conn.close()
    from . import access, desktop, net

    try:
        host, kind = net.resolve(args.host)
        # Checked here too so the refusal prints in this terminal, not the
        # server log.
        access.check(host)
    except (access.Unconfigured, net.NoAddress) as exc:
        print(str(exc))
        return 2

    if kind not in ("loopback", "given"):
        print(f"binding {host}  ({kind})")
    warning = net.advice(kind)
    if warning:
        print(f"  ⚠ {warning}")

    return desktop.launch(port=args.port, host=host, window=not args.serve,
                          replace=args.restart)


def cmd_halt(conn: sqlite3.Connection, args) -> int:
    """The stop switch, for when the window is wedged or the server is down.
    `control.halt` writes both `.colony/HALT` and a `controls` row so it
    cannot fail open.
    """
    from . import control

    on = args.command == "halt"
    out = control.halt(conn, on, args.reason if on else "")
    conn.commit()
    print("production halted. The pulse keeps beating, it just stops spending"
          if on else "production resumed")
    if out.get("flag"):
        print(f"{DIM}flag: {out['flag']}{RESET}")
    return 0


def cmd_allowance(conn: sqlite3.Connection, args) -> int:
    from . import control

    if args.points is not None:
        control.set_allowance(conn, args.points)
        conn.commit()
    band = control.effective_allowance(conn)
    print(f"allowance  {band['effective']:g}% of the week"
          + (f"   ({band['base']:g}% baseline {band['boost']:+g})" if band["boost"]
             else "   (the designed baseline)"))
    return 0


def cmd_projects(conn: sqlite3.Connection, args) -> int:
    """What has moved on disk. The same scan the pulse logs every hour."""
    from . import projects as projects_mod

    head = projects_mod.head()
    rows = projects_mod.scan()
    print(f"{head.get('branch')} @ {head.get('sha')}   "
          f"{DIM}{len(projects_mod.project_dirs())} projects tracked{RESET}")
    if not rows:
        print("  every project matches its last commit")
        return 0
    for r in rows:
        print(f"  {r['project']:<42} {r['summary']}")
    if args.diff:
        print()
        print(projects_mod.diff(args.diff))
    return 0


def cmd_shortcut(conn: sqlite3.Connection, args) -> int:
    from . import icon as icon_mod, shortcut as shortcut_mod

    conn.close()
    if args.icon_only:
        print(icon_mod.build())
        return 0
    print(f"icon      {icon_mod.build()}")
    try:
        print(f"shortcut  {shortcut_mod.create(port=args.port)}")
    except Exception as exc:
        print(f"could not write the shortcut: {exc}")
        return 1
    return 0


def cmd_pulse(conn: sqlite3.Connection, args) -> int:
    from . import control, pulse as pulse_mod

    try:
        return pulse_mod.run(conn, dry_run=args.dry_run, allow_wake=not args.no_wake)
    except control.Busy as exc:
        # A collision with a dashboard-forced beat is normal; exit 0.
        print(f"stood down {chr(8212)} {exc}")
        return 0


def cmd_forge(conn: sqlite3.Connection, args) -> int:
    """The forge, in text. Detection is free, so `--detect` costs nothing."""
    from . import control, forge

    if args.draft:
        try:
            out = control.request_draft(conn, args.draft)
        except control.Refused as exc:
            print(f"refused: {exc}")
            return 1
        conn.commit()
        print(f"queued   {out['slug']}: {out['outcome']}")
        return 0

    if args.detect:
        found = forge.detect(conn)
        conn.commit()
        print(f"detected {len(found)} new candidate(s)")

    board = forge.board(conn)
    rule("forge")
    if not board["skills"]:
        print("  nothing yet. A skill is proposed once a procedure repeats")
        return 0
    for s in board["skills"]:
        flag = " !" if s["slug"] in board["decaying"] else "  "
        queued = " (draft queued)" if s["draft_requested_at"] else ""
        print(f"{flag}#{s['id']:<3} {s['status']:<10} {s['name'][:44]:<46}"
              f"{(s['detector'] or ''):<14}{queued}")
        if s["summary"]:
            print(f"      {s['summary'][:100]}")
    print()
    print(f"  {board['tokens_saved']:,} tokens saved across every run that loaded one")
    return 0


def cmd_schedule(conn: sqlite3.Connection, args) -> int:
    """Install, inspect or remove the hourly heartbeat."""
    from . import schedule as sched

    conn.close()
    if args.remove:
        sched._run_ps(
            f"Unregister-ScheduledTask -TaskName {sched._ps(sched.TASK_NAME)} "
            f"-Confirm:$false -ErrorAction SilentlyContinue"
        )
        print(f"removed  {sched.TASK_NAME}")
        return 0

    if not args.show:
        print(f"installed {sched.install()}")
    task = sched.describe()
    if not task:
        print("no task registered")
        return 1
    rule(sched.TASK_NAME)
    for key in ("execute", "arguments", "hidden", "last_run", "next_run", "last_result"):
        print(f"  {key:<12}{task.get(key, '')}")
    print()
    print("  windowless" if sched.windowless() else
          "  ⚠ still runs a console app. re-run without --show to fix it")
    return 0


def _can_draw() -> bool:
    """Whether this terminal can print half blocks. `_force_utf8` replaces what
    it cannot encode, which for a QR code yields something that looks right
    and will not scan. Fall back to the URL instead.
    """
    try:
        "█▄▀".encode(getattr(sys.stdout, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def cmd_phone(conn: sqlite3.Connection, args) -> int:
    """Phone access in one command: on, off, or status, with a QR to scan.
    `autostart` still explains the scheduled task in detail.
    """
    from . import access, autostart, firewall, net, phone, qr

    conn.close()
    if args.off:
        phone.turn_off(args.port)
        print("phone access off")
        print("  the logon task is gone. The token stays in .env, so a phone "
              "that is already paired stays paired the next time you turn it on.")
        return 0

    if args.allow_firewall:
        # The one step that needs administrator rights, kept as its own flag so
        # the UAC prompt is always something you asked for by name.
        try:
            firewall.allow(args.port)
        except RuntimeError as exc:
            print(str(exc))
            return 2
        print(f"opened TCP {args.port} inbound for private networks")
        print()

    if args.rotate:
        # Before `--on`, so `--rotate --on` brings it up with the new token.
        result = phone.rotate(args.port)
        print(f"rotated {access.TOKEN_ENV} in .env")
        print("  every paired phone is logged out until it scans the code below")
        print()

    if args.on:
        try:
            result = phone.turn_on(args.port)
        except (access.Unconfigured, net.NoAddress) as exc:
            print(str(exc))
            return 2
        print("phone access on")
        if result["minted"]:
            print(f"  minted an access token and appended it to .env "
                  f"as {access.TOKEN_ENV}")
        else:
            print(f"  used the {access.TOKEN_ENV} already in .env")
        print(f"  registered {autostart.TASK_NAME} and started it")
    else:
        result = phone.state(args.port)

    rule("phone access")
    print(f"  {'switch':<10}{'on' if result['on'] else 'off'}")
    if result["problem"]:
        print()
        print(result["problem"])
        return 1
    print(f"  {'address':<10}{result['address']}  ({result['kind']})")
    print(f"  {'serving':<10}"
          + ("yes" if result["serving"] else
             "not yet. The task waits 45s at logon for the network"
             if result["on"] else "no"))
    if result["on"] and not result["unlimited"]:
        print("  ⚠ the task has a time limit and will be killed after three "
              "days; re-run `py -m colony autostart` to fix it")
    # A blocked port gives no error anywhere, only a spinner. Say so here.
    if result.get("firewall") == "blocked":
        print("  ⚠ Windows Firewall has no rule for this port, so the "
              "phone's request will be dropped")
        print("    rather than refused. The browser loads forever and "
              "neither end logs anything. Fix it with:")
        print()
        print("        py -m colony phone --allow-firewall")
        print()
        print("    or, in an administrator PowerShell:")
        print()
        print(f"        {result['firewall_fix']}")
        print()
    elif result.get("firewall") == "unknown":
        print("  ? could not read the firewall rules. That usually needs "
              "administrator rights.")
        print("    If the phone loads forever, this is the first thing to "
              "check.")
    warning = net.advice(result["kind"] or "")
    if warning:
        print(f"  ⚠ {warning}")

    if not result["url"]:
        print()
        print("  no access token set, so there is no address to open. "
              "Run `py -m colony phone --on`.")
        return 1

    print()
    print(f"  {result['url']}")
    print()
    if _can_draw():
        print(qr.text_art(result["url"], ec="M", quiet=2))
    else:
        print("  (this terminal cannot draw the code. Its encoding is "
              f"{getattr(sys.stdout, 'encoding', 'unknown')}. Type the address "
              "above, or open the panel in the dashboard under file → phone.)")
    return 0


def cmd_tailscale(conn: sqlite3.Connection, args) -> int:
    """Report Tailscale and offer install and login, like the phone panel.
    `--login` blocks here, which is fine in a terminal.
    """
    from . import net, qr, tailscale

    if args.install:
        try:
            started = tailscale.install()
        except tailscale.NotInstalled as exc:
            print(exc)
            return 1
        print(f"started {started['installer']}")
        print("  finish it, then run `py -m colony tailscale` again")
        return 0

    if args.login:
        try:
            out = tailscale.login()
        except tailscale.NotInstalled as exc:
            print(exc)
            return 1
        if out.get("url"):
            print("open this to sign in, on this machine or by camera:")
            print()
            print(f"  {out['url']}")
            print()
            if _can_draw():
                print(qr.text_art(out["url"], ec="M", quiet=2))
            return 0
        print("Tailscale did not ask for a sign-in, so it was already signed "
              "in and has reconnected.")

    state = tailscale.state()
    rule("tailscale")
    if not state["installed"]:
        print(f"  {'installed':<12}no")
        if state["installer"]:
            print(f"  {'installer':<12}{state['installer']}")
            print()
            print("  There is one in your Downloads already. Run it with:")
            print()
            print("      py -m colony tailscale --install")
        else:
            print()
            print(f"  Get it from {state['download']}, then run this again.")
        return 1

    print(f"  {'installed':<12}{state['exe']}")
    print(f"  {'backend':<12}{state['backend'] or 'unknown'}")
    if state["connected"]:
        print(f"  {'address':<12}{state['address']}")
        if state["name"]:
            print(f"  {'name':<12}{state['name']}")
    elif state["needs_login"]:
        print()
        print("  Signed out, so the dashboard will bind a local address and "
              "the phone")
        print("  will only reach it on the same wifi. Sign in with:")
        print()
        print("      py -m colony tailscale --login")
        return 1

    # Being on a tailnet and having the server bound to it are two different
    # facts, and only the second one is what the phone experiences.
    address, kind = None, ""
    try:
        address, kind = net.auto()
    except net.NoAddress:
        pass
    print()
    if kind == "tailnet":
        print(f"  The dashboard binds {address}, which works over cellular.")
    elif address:
        print(f"  The dashboard would bind {address} ({kind}), not the tailnet "
              f"address.")
        print("  Restart it and it will prefer the tailnet one.")
    return 0


def cmd_autostart(conn: sqlite3.Connection, args) -> int:
    """Install, inspect or remove the logon server task."""
    from . import access, autostart, net

    conn.close()
    if args.remove:
        autostart.remove()
        print(f"removed  {autostart.TASK_NAME}")
        print("  the dashboard still runs on demand. This only stopped it "
              "starting by itself")
        return 0

    if not args.show:
        try:
            address, kind = autostart.preflight(args.host, args.port)
        except (access.Unconfigured, net.NoAddress) as exc:
            print(str(exc))
            return 2
        print(f"installed {autostart.install(host=args.host, port=args.port)}")
        print(f"  right now --host {args.host} resolves to {address}  ({kind})")
        warning = net.advice(kind)
        if warning:
            print(f"  ⚠ {warning}")
        autostart.start_now()

    task = autostart.describe()
    if not task:
        print("no task registered")
        return 1
    rule(autostart.TASK_NAME)
    for key in ("execute", "arguments", "state", "last_run", "last_result"):
        print(f"  {key:<12}{task.get(key, '')}")
    print(f"  {'time limit':<12}"
          + ("none. It is meant to stay up" if autostart.unlimited(task)
             else f"{task.get('time_limit')} ⚠ it will be killed; re-run without --show"))
    print()

    serving = autostart.live(args.port)
    print(f"  answering now on http://{serving}:{args.port}" if serving else
          "  nothing answering yet. The task waits 45s at logon for the network")
    return 0


# ── wiring ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="colony", description="Colony Dash ledger")
    p.add_argument("--ledger", default=str(db.LEDGER_PATH), help="path to ledger.db")
    p.add_argument("--log", metavar="PATH",
                   help="append all output to PATH instead of the console. This is how "
                        "the scheduled pulse gets a log without needing a shell to "
                        "redirect one, and therefore without needing a console at all")
    # `--log` works before or after the subcommand. Every subparser inherits
    # it, and SUPPRESS keeps an absent copy from overwriting the top-level
    # value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log", metavar="PATH", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="command", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda name, **kw: _add(name, parents=[common], **kw)

    init = sub.add_parser("init", help="create/upgrade the ledger and seed it")
    init.add_argument("--roster-dir")
    init.add_argument("--skip-roster", action="store_true")
    init.set_defaults(func=cmd_init)

    status = sub.add_parser("status", help="the dashboard, in text")
    status.set_defaults(func=cmd_status)

    ros = sub.add_parser("roster", help="browse the hiring pool")
    ros.add_argument("query", nargs="?", help="search terms")
    ros.add_argument("--sync", action="store_true", help="re-scan the agency-agents install")
    ros.add_argument("--roster-dir")
    ros.add_argument("--division")
    ros.add_argument("--limit", type=int, default=20)
    ros.set_defaults(func=cmd_roster)

    ag = sub.add_parser("agents", help="who is on the books and what they may touch")
    ag.set_defaults(func=cmd_agents)

    sq = sub.add_parser("sql", help="run a SELECT against the ledger")
    sq.add_argument("query", nargs="+")
    sq.set_defaults(func=cmd_sql)

    mir = sub.add_parser("mirror", help="full refresh of the MySQL reporting mirror")
    mir.set_defaults(func=cmd_mirror)

    dash = sub.add_parser("dash", help="open the dashboard window")
    dash.add_argument("--port", type=int, default=8787)
    dash.add_argument("--serve", action="store_true",
                      help="serve only, no window. Use a browser at 127.0.0.1")
    dash.add_argument("--host", default=None,
                      help="address to bind (default 127.0.0.1). 'auto' picks "
                           "this machine's tailnet address, or its private LAN "
                           "address if there is no tailnet. Anything but "
                           "loopback requires COLONY_ACCESS_TOKEN in .env")
    dash.add_argument("--restart", action="store_true",
                      help="stop the dashboard already on this port and take it "
                           "over. Happens on its own when the running one is "
                           "older than the code on disk; this forces it when "
                           "they match")
    dash.set_defaults(func=cmd_dash)

    hlt = sub.add_parser("halt", help="stop all dispatch colony-wide")
    hlt.add_argument("--reason", default="halted from the CLI")
    hlt.set_defaults(func=cmd_halt)

    res = sub.add_parser("resume", help="let the colony dispatch work again")
    res.set_defaults(func=cmd_halt)

    alw = sub.add_parser("allowance", help="show or move the sprint's token allowance")
    alw.add_argument("points", nargs="?", type=float,
                     help="percentage points off the baseline, signed; 0 clears it")
    alw.set_defaults(func=cmd_allowance)

    prj = sub.add_parser("projects", help="what has moved in the projects on disk")
    prj.add_argument("--diff", metavar="PROJECT", help="also print that project's diff")
    prj.set_defaults(func=cmd_projects)

    sct = sub.add_parser("shortcut", help="build the icon and a Desktop shortcut")
    sct.add_argument("--port", type=int, default=8787)
    sct.add_argument("--icon-only", action="store_true")
    sct.set_defaults(func=cmd_shortcut)

    pul = sub.add_parser("pulse", help="run one pulse (tick, escalating to wake)")
    pul.add_argument("--dry-run", action="store_true", help="report, write nothing")
    pul.add_argument("--no-wake", action="store_true",
                     help="tick only. Never spawn an agent, guaranteeing zero tokens")
    pul.set_defaults(func=cmd_pulse)

    frg = sub.add_parser("forge", help="what the skill forge has noticed")
    frg.add_argument("--detect", action="store_true",
                     help="re-run detection now instead of waiting for the next tick")
    frg.add_argument("--draft", type=int, metavar="ID",
                     help="queue a candidate for drafting on the next wake")
    frg.set_defaults(func=cmd_forge)

    # `phone` folds the autostart steps into one flag and adds a QR;
    # `autostart` stays for inspecting the task.
    phn = sub.add_parser("phone",
                         help="open the dashboard on your phone. State, or --on to set it up")
    phn.add_argument("--on", action="store_true",
                     help="mint a token if there is none, register the logon "
                          "task, start it now")
    phn.add_argument("--off", action="store_true",
                     help="stop serving at logon. The token and any paired "
                          "phone are left alone")
    phn.add_argument("--allow-firewall", action="store_true",
                     help="add the Windows Firewall rule for the port (prompts "
                          "for administrator once)")
    phn.add_argument("--rotate", action="store_true",
                     help="write a new access token to .env. Every paired "
                          "phone is logged out until it scans the new code")
    phn.add_argument("--port", type=int, default=8787)
    phn.set_defaults(func=cmd_phone)

    tls = sub.add_parser("tailscale",
                         help="the private network that makes phone access work "
                              "off your wifi")
    tls.add_argument("--install", action="store_true",
                     help="run the installer from your Downloads folder")
    tls.add_argument("--login", action="store_true",
                     help="sign in, and print the URL and a QR code for it")
    tls.set_defaults(func=cmd_tailscale)

    aut = sub.add_parser("autostart",
                         help="keep the dashboard served from logon, for the phone")
    aut.add_argument("--show", action="store_true", help="report the task, change nothing")
    aut.add_argument("--remove", action="store_true", help="unregister it")
    aut.add_argument("--host", default="auto",
                     help="address to bind at logon (default: auto)")
    aut.add_argument("--port", type=int, default=8787)
    aut.set_defaults(func=cmd_autostart)

    sch = sub.add_parser("schedule", help="install the hourly pulse as a hidden task")
    sch.add_argument("--show", action="store_true", help="report the task, change nothing")
    sch.add_argument("--remove", action="store_true", help="unregister it")
    sch.set_defaults(func=cmd_schedule)

    return p


def _force_utf8() -> None:
    """Make stdout and stderr safe to print to.

    Redirected output on Windows is cp1252 and cannot encode this CLI's box
    and arrow characters; under pythonw both streams are None. Either way
    output must never decide whether a command succeeds.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        try:
            # A windowed launch can hand over a stream that fails only on first
            # write.
            stream.write("")
            stream.flush()
        except OSError:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _redirect(path: str) -> None:
    """Send both streams to a file, appending, with a banner per invocation."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stream = open(target, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = stream
    print()
    print(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} =====")


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    if getattr(args, "log", None):
        _redirect(args.log)
    conn = db.connect(args.ledger)
    try:
        if args.command != "init":
            db.migrate(conn, verbose=False)
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
