"""The build tier: the first agent allowed to write.

    groomed → PO accepts criteria → project confirmed → agent hired with write
    scope → PO dispatches → build runs in a worktree → PO approves the patch

Six gates, four human, so the worst an unattended run can do is produce a
diff nobody has applied (docs/design.md §8). A build never touches the live
tree, commits or pushes.
"""

from __future__ import annotations

import json
import os
import sqlite3

from . import agent, attachments as attach, control, voice, worktree
from .prompt import render as render_prompt

BUILD_TIMEOUT_S = int(os.environ.get("COLONY_BUILD_TIMEOUT", "900"))

# One build per wake. A build is the most expensive thing the colony does and
# the one whose output most needs reading before the next one starts.
BUILD_LIMIT = int(os.environ.get("COLONY_BUILD_LIMIT", "1"))


def pending(conn: sqlite3.Connection, limit: int = BUILD_LIMIT) -> list[sqlite3.Row]:
    """Tickets the PO dispatched that have not run yet."""
    return conn.execute(
        """SELECT t.*, s.title AS story_title, s.description, s.acceptance_criteria,
                  s.project, s.project_source, s.id AS sid
             FROM tickets t JOIN stories s ON s.id = t.story_id
            WHERE t.intent = 'implement' AND t.status = 'staffed'
              AND s.project_source = 'confirmed'
            ORDER BY t.id LIMIT ?""",
        (limit,),
    ).fetchall()


# Earlier events a new run may see: work, not bookkeeping.
HISTORY_KINDS = ("finding", "learning", "note", "decided", "blocked")


def history(conn: sqlite3.Connection, story_id: int, limit: int = 12) -> list[dict]:
    """What earlier runs and PO decisions on this story already established."""
    rows = conn.execute(
        f"""SELECT kind, summary, detail, at FROM story_events
             WHERE story_id = ? AND kind IN ({','.join('?' * len(HISTORY_KINDS))})
             ORDER BY id DESC LIMIT ?""",
        (story_id, *HISTORY_KINDS, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def history_note(events: list[dict] | None) -> str:
    """The "already known" section of the work order, with full details, since
    a run-request's value is its output.
    """
    if not events:
        return ""
    out = ["--- what the colony already found out on this story ---",
           "Earlier runs and decisions, oldest first. Take these as established.",
           "If one of them is wrong, say so in your report rather than quietly "
           "working around it.",
           "A `decided` entry is the PO speaking. Where one waives a check or "
           "settles a criterion, it settles it: read the criterion as met and "
           "move on. Do not reason about what they must have meant more "
           "narrowly, and do not ask for the same thing again in `needs_run` "
           "under a different justification.", ""]
    for e in events:
        out.append(f"[{e['at']}] {e['kind']}: {e['summary']}")
        detail = (e.get("detail") or "").strip()
        if detail:
            body = (detail if len(detail) <= 1800
                    else detail[:1800] + "\n  (truncated, the rest is on the story)")
            out.append("\n".join("  " + line for line in body.splitlines()))
        out.append("")
    out.append("--- end of what is already known ---")
    return "\n".join(out)


def seeded_note(seeded: dict | None) -> str:
    """What the checkout holds beyond the last commit, so the agent neither
    hedges about seeded files nor reports a missing credential as unset.
    """
    if not seeded:
        return ("This checkout is git's copy of the last commit. Files git does not "
                "track are not here: no `.env`, no build output, nothing the PO has "
                "edited but not yet committed. If a criterion depends on one of "
                "those, say so plainly. An absent `.env` means the setting is not "
                "visible to you, not that it is unset.")

    moved = (seeded.get("tracked") or 0) + (seeded.get("untracked") or 0)
    lines = [
        f"This checkout is the last commit plus the PO's uncommitted work: "
        f"{moved} file(s) were copied in from their live tree before you started, "
        f"so a file they have edited or staged but not committed IS here and IS "
        f"current. Your patch is taken against that seeded state, so their changes "
        f"will not show up as yours."
    ]
    secrets = seeded.get("secrets") or []
    if secrets:
        lines.append(
            "Credential files were copied in as well: "
            + ", ".join(secrets)
            + ". Read them when a criterion turns on what is configured. Never "
            "copy a value out of one into your report, a comment, a test, or any "
            "file you write. Say which key is set or unset and stop there. "
            "Edits you make to these files are discarded and never reach a patch.")
    else:
        lines.append(
            "Credential files were NOT copied in: this contract does not have "
            "them. If a criterion turns on whether a key is configured, say that "
            "you could not see the file. An absent `.env` means the setting is "
            "not visible to you, not that it is unset.")
    if seeded.get("ignored"):
        lines.append(
            f"{seeded['ignored']} git-ignored file(s) were copied in too. Runtime "
            f"state such as a sync cache or a log, which git does not track but the "
            f"code reads. They are current. Edits you make to them are discarded "
            f"and never reach a patch, the same as a credential file.")
    if seeded.get("ignored_skipped"):
        lines.append(
            "These ignored directories were left out for being generated output: "
            + ", ".join(seeded["ignored_skipped"])
            + ". If a criterion turns on something in one of them, say that you "
            "could not see it.")
    if seeded.get("skipped"):
        lines.append(f"{len(seeded['skipped'])} untracked file(s) were too large to "
                     f"copy and are absent.")
    return "\n\n".join(lines)


def build_prompt(ticket: sqlite3.Row, workdir: str,
                 attached: list[dict] | None = None,
                 scope: list[str] | None = None,
                 seeded: dict | None = None,
                 known: list[dict] | None = None) -> str:
    """The work order. `scope` is the contract's folder list (default: the
    story's folder); `seeded` is what `worktree.seed` added on top of the
    commit.
    """
    criteria = (ticket["acceptance_criteria"] or "").strip() or "(none recorded. Ask, do not guess)"
    brief = (ticket["description"] or "").strip() or "(the Notion page body is empty)"
    project = ticket["project"]
    folders = list(scope or []) or [project]
    # Backslashes throughout, so no path mixes separators.
    scope_lines = "\n".join(
        "  " + workdir.rstrip("\\/") + "\\" + f.replace("/", "\\") for f in folders)
    return render_prompt(
        "build", workdir=workdir, scope_lines=scope_lines, project=project,
        seeded=seeded_note(seeded), sid=ticket["sid"], title=ticket["story_title"],
        brief=brief[:5000], criteria=criteria[:3000],
        evidence=attach.evidence(attached or []), history=history_note(known),
        style=voice.STYLE)


def clip(text: str, limit: int) -> str:
    """Shorten a one-line field with a visible ellipsis. Report bodies are
    never clipped.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def _event(conn, story_id, kind, summary, detail=None, ticket_id=None, tokens=0):
    conn.execute(
        """INSERT INTO story_events (story_id, ticket_id, kind, summary, detail, tokens)
           VALUES (?,?,?,?,?,?)""",
        (story_id, ticket_id, kind, clip(summary, 400), detail, tokens),
    )


def contract(conn: sqlite3.Connection, role: str, project: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM agents WHERE role = ? AND project = ? AND status != 'retired'",
        (role, project),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    for key in ("tools_allowed", "tools_denied", "read_scope", "write_scope", "skills"):
        if out.get(key):
            out[key] = json.loads(out[key])
    return out


def run_one(conn: sqlite3.Connection, ticket: sqlite3.Row) -> dict:
    """One dispatched ticket: worktree in, patch out."""
    tid, sid = ticket["id"], ticket["sid"]
    outcome = {"ticket_id": tid, "story_id": sid, "title": ticket["story_title"],
               "tokens": 0, "raw_tokens": 0, "verdict": None, "files": 0}

    terms = contract(conn, ticket["role"], ticket["project"])
    if not terms or not terms["write_capable"]:
        conn.execute("UPDATE tickets SET status = 'blocked', findings = ? WHERE id = ?",
                     (f"{ticket['role']} has no write-capable contract on {ticket['project']}", tid))
        outcome["verdict"] = "no write contract"
        return outcome

    folders = control.scope_projects(terms.get("write_scope")) or [ticket["project"]]
    try:
        work = worktree.create(tid)
        # Seed the checkout to match disk, including the PO's uncommitted work.
        seeded = worktree.seed(tid, folders, secrets=bool(terms.get("sees_secrets")))
    except Exception as exc:  # git refused; a blocked ticket, never a crashed pulse
        conn.execute("UPDATE tickets SET status = 'blocked', findings = ? WHERE id = ?",
                     (f"could not open a worktree: {exc}", tid))
        _event(conn, sid, "note", "build could not start. No isolated checkout", str(exc), tid)
        outcome["verdict"] = "no worktree"
        return outcome

    prompt = build_prompt(ticket, str(work), attach.for_story(conn, ticket["sid"]),
                          scope=folders, seeded=seeded,
                          known=history(conn, ticket["sid"]))
    conn.execute("UPDATE tickets SET work_order = ? WHERE id = ?", (prompt, tid))

    result = agent.run_ticket(
        conn,
        ticket_id=tid,
        role=terms["role"],
        prompt=prompt,
        model=terms["model"],
        tools_allowed=terms["tools_allowed"],
        tools_denied=terms.get("tools_denied"),
        cwd=work,
        timeout_s=BUILD_TIMEOUT_S,
        max_tokens=terms.get("max_tokens_run"),
        allow_writes=True,
        worktree_path=str(work),
    )
    outcome["tokens"] = result.chargeable_tokens
    outcome["raw_tokens"] = result.total_tokens

    if result.over_budget:
        conn.execute(
            """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation, est_tokens)
               VALUES (?,?,'cost',?,?,?)""",
            (sid, tid,
             f"Building \"{ticket['story_title']}\" spent {result.chargeable_tokens:,} chargeable "
             f"tokens against a {terms.get('max_tokens_run'):,} ceiling.",
             "The work was kept. The diff is waiting for you either way. Raise the ceiling "
             "or split the story before the next build.",
             result.chargeable_tokens),
        )

    # Diff whatever the status; a timed-out run's files are still paid for.
    try:
        patch = worktree.diff(tid)
        stat = worktree.stat(tid)
    except Exception as exc:
        patch, stat = "", f"(could not diff the worktree: {exc})"

    answer = result.json_payload() or {}
    summary = (answer.get("summary") or clip(result.text, 200) or "build finished").strip()

    if not patch.strip():
        # No files written, but the agent may still have handed over commands.
        # Raise those, and store the answer JSON so skipped criteria stay
        # readable.
        conn.execute(
            "UPDATE tickets SET status = 'blocked', findings = ?, "
            "closed_at = datetime('now','localtime') WHERE id = ?",
            (json.dumps(answer, indent=2) if answer
             else (result.error or result.text or "no changes"), tid),
        )
        _event(conn, sid, "note", f"build changed nothing: {summary}",
               result.text or None, tid, result.chargeable_tokens)
        if answer.get("learned"):
            _event(conn, sid, "learning", clip(str(answer["learned"]), 400),
                   str(answer["learned"]), tid, 0)
        handed = _raise_run_requests(conn, ticket, answer,
                                     wrote_nothing=True)
        _park_no_change(conn, ticket, answer, handed)
        worktree.remove(tid)
        outcome["verdict"] = (f"handed off {handed} command(s)" if handed
                              else "no changes")
        return outcome

    path = worktree.save_patch(tid, patch)
    paths = worktree.patch_files(patch)
    stray = worktree.outside_scope(paths, folders)
    files = len(paths)
    outcome["files"] = files

    conn.execute(
        "UPDATE tickets SET status = 'done', findings = ?, artifact_path = ?, "
        "closed_at = datetime('now','localtime') WHERE id = ?",
        (json.dumps(answer, indent=2) if answer else result.text, str(path), tid),
    )
    conn.execute("UPDATE stories SET status = 'po-review', "
                 "updated_at = datetime('now','localtime') WHERE id = ?", (sid,))

    # Gate 5. The patch exists; nothing has been applied. This is the escalation
    # the whole milestone is built around.
    detail_bits = [stat.strip()]
    if stray:
        detail_bits.insert(0, "Outside the write scope (" + ", ".join(folders) + "):\n  "
                           + "\n  ".join(stray)
                           + "\nApply refuses these. Widen the contract's write scope "
                             "first, or reject.")
    if answer.get("skipped"):
        detail_bits.append("SKIPPED:\n" + "\n\n".join(
            f"  · {s.get('criterion')}\n    → {s.get('why')}"
            for s in answer["skipped"]))
    if answer.get("risks"):
        detail_bits.append("LOOK CLOSELY AT:\n  " + str(answer["risks"]))
    conn.execute(
        """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation,
                                    proposal, est_tokens)
           VALUES (?,?,'write-approval',?,?,?,?)""",
        (sid, tid,
         f'"{ticket["story_title"]}" has a patch waiting. {files} file'
         f'{"s" if files != 1 else ""} changed in {ticket["project"]}/.',
         "\n\n".join(b for b in detail_bits if b),
         json.dumps({"ticket_id": tid, "patch": str(path), "project": ticket["project"],
                     "files": paths, "scope": folders, "outside": stray}),
         result.chargeable_tokens),
    )
    _raise_run_requests(conn, ticket, answer)
    _event(conn, sid, "finding", summary, stat, tid, result.chargeable_tokens)
    if answer.get("learned"):
        _event(conn, sid, "learning", clip(str(answer["learned"]), 400),
               str(answer["learned"]), tid, 0)

    outcome["verdict"] = f"patch ready ({files} file{'s' if files != 1 else ''})"
    return outcome


def _park_no_change(conn: sqlite3.Connection, ticket: sqlite3.Row,
                    answer: dict, handed: int) -> None:
    """Where a story goes when the build wrote no files: `needs-info`, not
    `ready`, which would re-dispatch the same empty build.

    `needs-info` shows the reason from `blocked_reason` on the story and
    Inbox, and `pulse.ensure_blocked_visible` keeps a card open for it. When
    run-request cards were raised, they are that card.
    """
    sid = ticket["sid"]
    if handed:
        why = (f"The build found nothing left for it to write. {handed} command(s) "
               f"it has no shell to run are waiting on your yes or no in the "
               f"Inbox, and the story moves again as soon as one has been run.")
    else:
        why = ("The build ran and changed no files. "
               + (clip(str(answer.get("summary") or ""), 300)
                  or "The agent gave no reason.")
               + " Dispatching it again as it stands produces the same empty "
                 "build, so it is parked until the criteria or the tree change.")
    conn.execute(
        "UPDATE stories SET status = 'needs-info', blocked_reason = ?, "
        "updated_at = datetime('now','localtime') "
        "WHERE id = ? AND status <> 'archived'", (why, sid))
    if handed:
        return

    story = conn.execute("SELECT title, notion_hash FROM stories WHERE id = ?",
                         (sid,)).fetchone()
    if not story:
        return
    open_card = conn.execute(
        "SELECT id FROM escalations WHERE story_id = ? AND kind = 'needs-info' "
        "AND resolved_at IS NULL", (sid,)).fetchone()
    if open_card:
        return
    body = [why]
    for sk in (answer.get("skipped") or []):
        if isinstance(sk, dict):
            body.append(f"Skipped: {sk.get('criterion')}\n  why: {sk.get('why')}")
    body.append("Change what it is being asked for, or drop the criterion it "
                "could not meet, and dispatch again.")
    conn.execute(
        """INSERT INTO escalations (story_id, ticket_id, kind, reason,
                                    recommendation, raised_hash)
           VALUES (?,?,'needs-info',?,?,?)""",
        (sid, ticket["id"],
         f'"{story["title"]}" was built and nothing changed.',
         control.card_text("\n\n".join(body)), story["notion_hash"]))


def _same_command(a: str, b: str) -> bool:
    """Same command ignoring whitespace only."""
    return " ".join((a or "").split()) == " ".join((b or "").split())


def _already_asked(conn: sqlite3.Connection, story_id: int, command: str,
                   wrote_nothing: bool) -> str | None:
    """Why this run-request must not go to the PO again, or None. Checks
    earlier cards on the story so an answered question is not asked twice.
    """
    if not story_id:
        return None
    rows = conn.execute(
        "SELECT proposal, resolved_at, po_decision, dismissed_at FROM escalations "
        "WHERE story_id = ? AND kind = 'run-request' ORDER BY id DESC",
        (story_id,)).fetchall()
    ran_before = False
    for row in rows:
        try:
            earlier = json.loads(row["proposal"] or "{}").get("command") or ""
        except ValueError:
            continue
        if not _same_command(earlier, command):
            continue
        if not row["resolved_at"]:
            return "it is already in the PO's Inbox, unanswered"
        if row["dismissed_at"] or row["po_decision"] in ("reject", "defer"):
            # A no is an answer. Re-raising it reads to the PO as the colony not
            # listening, which is exactly what it is.
            return "the PO has already declined to run it on this story"
        if row["po_decision"] == "approve":
            ran_before = True
    if ran_before and wrote_nothing:
        # Already run, and this build changed nothing: rerunning tells nobody
        # anything.
        return ("it has already been run on this story and this build changed "
                "nothing, so running it again would print the same thing")
    return None


def _raise_run_requests(conn: sqlite3.Connection, ticket: sqlite3.Row,
                        answer: dict, wrote_nothing: bool = False) -> int:
    """Turn the agent's `needs_run` list into run-request cards. Refused
    commands get a note on the story instead of a card.
    """
    from . import runner

    wanted = answer.get("needs_run") or []
    if isinstance(wanted, (str, dict)):
        wanted = [wanted]
    raised = 0
    for item in wanted:
        if isinstance(item, str):
            item = {"command": item}
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        why = str(item.get("why") or "").strip()
        expect = str(item.get("expect") or "").strip()
        try:
            command = runner.check(command)
            # Resolve the `cd` now so the card shows the real command and
            # folder, and an escape is refused before the PO sees it.
            command, where = runner.resolve_cd(
                command, runner.check_folder(ticket["project"]))
            where = where.relative_to(runner.ROOT).as_posix()
        except runner.RunRefused as exc:
            _event(conn, ticket["sid"], "note",
                   f"{ticket['role']} asked to run a command the colony will not run",
                   f"$ {command}\n\n{exc}", ticket["id"], 0)
            continue
        settled = _already_asked(conn, ticket["sid"], command, wrote_nothing)
        if settled:
            # On the story, where the next agent will read it.
            _event(conn, ticket["sid"], "note",
                   f"{ticket['role']} asked again for `{command}`. Not raised",
                   f"$ {command}\n\nNot put in front of the PO: {settled}."
                   "\n\nThe earlier answer is on this story. Read it. If it "
                   "does not settle the criterion, say so in your report and say "
                   "what would settle it, rather than asking for the same "
                   "command again.",
                   ticket["id"], 0)
            continue
        body = [f"$ {command}", f"  in {where}/"]
        if why:
            body.append("Why it is needed:\n" + why)
        if expect:
            body.append("What it expects to see:\n" + expect)
        body.append("Nothing else runs. The output goes on the story and the story "
                    "goes back to the queue, so the next build reads what happened.")
        conn.execute(
            """INSERT INTO escalations (story_id, ticket_id, kind, reason,
                                        recommendation, proposal, est_tokens)
               VALUES (?,?,'run-request',?,?,?,0)""",
            (ticket["sid"], ticket["id"],
             f'{ticket["role"]} needs a command run: `{command}`',
             "\n\n".join(body),
             # `where`, since the `cd` has already been stripped from the
             # command.
             json.dumps({"command": command, "project": where,
                         "ticket_id": ticket["id"], "why": why,
                         "expect": expect})),
        )
        raised += 1
    return raised


def run(conn: sqlite3.Connection, limit: int = BUILD_LIMIT) -> list[dict]:
    """Every dispatched ticket this wake is willing to run."""
    return [run_one(conn, t) for t in pending(conn, limit)]
