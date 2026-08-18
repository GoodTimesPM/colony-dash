"""`python -m colony <command>` — inspect and operate the ledger.

The dashboard (M2) is a read view over exactly these queries. Until it exists,
this is the whole UI.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import db, roster as roster_mod, seed as seed_mod

# Dollars are always the grayed secondary; tokens are the unit. ARCHITECTURE.md §6.
# Piped output gets no escapes — a log file full of \033[2m is worse than plain text.
# `sys.stdout` is None under pythonw.exe — no console exists at all — and this
# line runs at import, before main() can repair anything. A windowed launch died
# here with exit code 1 and no traceback anywhere: the one stream that would
# have reported the problem was the problem.
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
        # By run date inside the sprint window, not by story.sprint_id: stories
        # come from Notion without a sprint attached, so the join version summed
        # nothing and the sprint always read as "— tok" no matter what was spent.
        # Every token the colony burns in the window belongs to the window.
        spent = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(chargeable_tokens, total_tokens)), 0) AS tok,
                   COALESCE(SUM(cost_usd), 0) AS usd
              FROM runs
             WHERE date(started_at) BETWEEN ? AND ?
            """,
            (sprint["starts_on"], sprint["ends_on"]),
        ).fetchone()
        rule(f"{sprint['name']} — {sprint['goal'] or 'no goal set'}")
        print(f"  {sprint['starts_on']} → {sprint['ends_on']}   allowance {sprint['budget_pct']:.0f}% of week")
        print(f"  spent  {toks(spent['tok'])} tok  {usd(spent['usd'])}")
    else:
        rule("no active sprint")

    usage = conn.execute("SELECT * FROM usage_samples ORDER BY sampled_at DESC LIMIT 1").fetchone()
    if usage:
        print(f"  week   {usage['seven_day_pct']:.1f}% used   resets {usage['seven_day_resets_at']}")
    else:
        print(f"  week   {DIM}no usage sample yet — see ARCHITECTURE.md §6.2{RESET}")

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
        print(f"  {DIM}empty — nothing needs you{RESET}")
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
    # The dashboard opens its own read-only connection to the ledger; this one
    # exists only because every command gets handed one. Close it first so the
    # window is never the reason a write is blocked.
    conn.close()
    from . import desktop

    return desktop.launch(port=args.port, window=not args.serve)


def cmd_pulse(conn: sqlite3.Connection, args) -> int:
    from . import pulse as pulse_mod

    return pulse_mod.run(conn, dry_run=args.dry_run, allow_wake=not args.no_wake)


# ── wiring ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="colony", description="Colony Dash ledger")
    p.add_argument("--ledger", default=str(db.LEDGER_PATH), help="path to ledger.db")
    sub = p.add_subparsers(dest="command", required=True)

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
                      help="serve only, no window — use a browser at 127.0.0.1")
    dash.set_defaults(func=cmd_dash)

    pul = sub.add_parser("pulse", help="run one pulse (tick, escalating to wake)")
    pul.add_argument("--dry-run", action="store_true", help="report, write nothing")
    pul.add_argument("--no-wake", action="store_true",
                     help="tick only — never spawn an agent, guaranteeing zero tokens")
    pul.set_defaults(func=cmd_pulse)

    return p


def _force_utf8() -> None:
    """Redirected stdout on Windows defaults to cp1252, which cannot encode the
    box-drawing and arrow characters this CLI prints. The scheduled pulse writes
    to a log file, so without this a clean tick dies on its own output *after*
    the ledger row is committed — a crash that means nothing and looks like
    everything. Never let formatting decide whether a run succeeded.

    Under `pythonw.exe` there is no console at all and both streams are None, so
    the *first* print raises and the process dies before it does anything. That
    is how the dashboard is meant to be launched — windowed, no console behind
    it — so the same rule applies twice over: output is never allowed to decide
    whether a command runs.
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
            # A handle can exist and still be unwritable — a windowed launch with
            # no redirect hands the child a stream that only fails on first use.
            # Find that out here, once, instead of somewhere with a ledger open.
            stream.write("")
            stream.flush()
        except OSError:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    conn = db.connect(args.ledger)
    try:
        if args.command != "init":
            db.migrate(conn, verbose=False)
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
