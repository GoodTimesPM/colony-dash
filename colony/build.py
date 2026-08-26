"""The build tier — the first agent allowed to write anything.

Grooming (M1) reads and reports. This module is the other half: a story the PO
accepted at the Inbox gate, staffed to an agent hired with write scope on a
confirmed project, run inside a git worktree, and handed back as a patch nobody
has applied yet.

The order of the gates matters more than the code does:

    groomed → PO accepts criteria → project confirmed → agent hired with write
    scope → PO dispatches → build runs in a worktree → PO approves the patch

Six gates, four of them human. That is not friction for its own sake — it is the
answer to "what is the worst thing this can do at 3am", and the answer has to
stay "nothing you haven't already read" (ARCHITECTURE.md §8).

A build never touches the live tree, never commits, never pushes. It produces a
diff. The diff is the deliverable.
"""

from __future__ import annotations

import json
import os
import sqlite3

from . import agent, attachments as attach, control, db, voice, worktree

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


# What an earlier run on the same story is allowed to tell this one. Findings
# and learnings are work; a `staffed` or `dispatched` row is bookkeeping and
# would fill the section with the colony talking about itself.
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
    """The section of the work order that says what is already known.

    Details are included, not just the one-line summaries: the useful thing in
    a run-request is the command's output, and that lives in the detail.
    """
    if not events:
        return ""
    out = ["--- what the colony already found out on this story ---",
           "Earlier runs and decisions, oldest first. Take these as established.",
           "If one of them is wrong, say so in your report rather than quietly "
           "working around it.", ""]
    for e in events:
        out.append(f"[{e['at']}] {e['kind']}: {e['summary']}")
        detail = (e.get("detail") or "").strip()
        if detail:
            body = (detail if len(detail) <= 1800
                    else detail[:1800] + "\n  — the rest is on the story")
            out.append("\n".join("  " + line for line in body.splitlines()))
        out.append("")
    out.append("--- end of what is already known ---")
    return "\n".join(out)


def seeded_note(seeded: dict | None) -> str:
    """What the checkout holds beyond the last commit, in the work order's words.

    An agent that does not know its checkout was seeded reads a file it half
    expects to be stale and hedges everything it says about it. An agent that
    does not know a credential file is absent reports the setting as unset,
    which is the mistake that made this paragraph necessary.
    """
    if not seeded:
        return ("This checkout is git's copy of the last commit. Files git does not "
                "track are not here: no `.env`, no build output, nothing Jordan has "
                "edited but not yet committed. If a criterion depends on one of "
                "those, say so plainly — an absent `.env` means the setting is not "
                "visible to you, not that it is unset.")

    moved = (seeded.get("tracked") or 0) + (seeded.get("untracked") or 0)
    lines = [
        f"This checkout is the last commit plus Jordan's uncommitted work: "
        f"{moved} file(s) were copied in from his live tree before you started, "
        f"so a file he has edited or staged but not committed IS here and IS "
        f"current. Your patch is taken against that seeded state, so his changes "
        f"will not show up as yours."
    ]
    secrets = seeded.get("secrets") or []
    if secrets:
        lines.append(
            "Credential files were copied in as well: "
            + ", ".join(secrets)
            + ". Read them when a criterion turns on what is configured. Never "
            "copy a value out of one into your report, a comment, a test, or any "
            "file you write — say which key is set or unset and stop there. "
            "Edits you make to these files are discarded and never reach a patch.")
    else:
        lines.append(
            "Credential files were NOT copied in: this contract does not have "
            "them. If a criterion turns on whether a key is configured, say that "
            "you could not see the file. An absent `.env` means the setting is "
            "not visible to you, not that it is unset.")
    if seeded.get("skipped"):
        lines.append(f"{len(seeded['skipped'])} untracked file(s) were too large to "
                     f"copy and are absent.")
    return "\n\n".join(lines)


def build_prompt(ticket: sqlite3.Row, workdir: str,
                 attached: list[dict] | None = None,
                 scope: list[str] | None = None,
                 seeded: dict | None = None,
                 known: list[dict] | None = None) -> str:
    """The work order. Says what may be touched, in the words of the scope itself.

    `scope` is the folder list off the agent's contract, which the PO can widen
    from the contract drawer. It defaults to the story's own folder, which is
    what every contract holds until he changes one. `seeded` is what
    `worktree.seed` put in the checkout on top of the commit.
    """
    criteria = (ticket["acceptance_criteria"] or "").strip() or "(none recorded — ask, do not guess)"
    brief = (ticket["description"] or "").strip() or "(the Notion page body is empty)"
    project = ticket["project"]
    folders = list(scope or []) or [project]
    # Backslashes throughout: a sub-project reads as `job-search/assisted-apply`
    # everywhere else in the colony, but half a path in each separator is the
    # kind of detail an agent stops trusting.
    scope_lines = "\n".join(
        "  " + workdir.rstrip("\\/") + "\\" + f.replace("/", "\\") for f in folders)
    return f"""You are a build agent in Jordan's colony of Claude agents. Ordis is the Scrum
Master; Jordan is the Product Owner and has approved this work.

You are working inside an ISOLATED GIT WORKTREE at:
  {workdir}

This is a throwaway checkout. It is not Jordan's working tree. Your changes will
be turned into a patch that Jordan reads and approves before anything lands.

WRITE SCOPE — you may create and edit files ONLY under:
{scope_lines}

Everywhere else in this checkout is READ-ONLY to you. You have no shell: no
git commands, no package installs, no network.

Your command runs inside the project folder already. Do not begin it with
`cd` — the folder is the write scope, and a command that starts by leaving it
is refused.

You are not the only one working on this. When a criterion needs a command run
— a script, a test, a real API call — do not skip it and do not fake it. Put
the command in `needs_run` with the criterion it answers and what a correct
result looks like. Jordan sees the command, applies your patch, then runs it
against the live tree, and the whole transcript comes back on the story for
whoever picks it up next. Write `expect` carefully: it is recorded next to the
output and it is what the next agent compares against, so "exit 0" is never
enough — name the line you want to see. Do the rest of the work in the same
run; a `needs_run` entry is a handover, not a stop.

{seeded_note(seeded)}

STORY #{ticket['sid']}: {ticket['story_title']}

--- brief ---
{brief[:5000]}
--- end brief ---

--- acceptance criteria (approved by Jordan) ---
{criteria[:3000]}
--- end criteria ---
{attach.evidence(attached or [])}
{history_note(known)}

Read {project}/PROJECT.md first — it is that project's source of truth for
status and decisions. Match the surrounding code: its naming, its comment
density, its idioms. Do not restructure things you were not asked to change,
and do not add dependencies.

Work the criteria in order. If one of them turns out to be impossible or wrong,
do the others in full and say precisely which one you left and why — scaling the
work down is Jordan's call, not yours.

{voice.STYLE}

When you are done, reply with ONLY a JSON object, no prose around it:

{{
  "done": ["criteria you completed, verbatim from the list"],
  "skipped": [{{"criterion": "...", "why": "..."}}],
  "files": ["relative/paths/you/changed"],
  "summary": "one sentence for the dashboard, under 140 characters",
  "needs_run": [{{"command": "one shell command, as you would type it",
                  "why": "the criterion it answers",
                  "expect": "what a correct result looks like"}}],
  "risks": "anything Jordan should look at closely in the diff, or null",
  "learned": "one thing worth keeping about this codebase, or null"
}}"""


def clip(text: str, limit: int) -> str:
    """Shorten a headline without letting it look like the text just stopped.

    Only for the one-line fields -- `story_events.summary`, a ticket title.
    Report bodies are never clipped: an agent that took the trouble to say
    which criterion it skipped and why should have all of that reach the card.
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
        # The checkout starts as git's copy of the last commit. Seeding brings
        # it up to what is actually on disk, because half the criteria on the
        # run that prompted this pointed at files Jordan had staged and not
        # committed, and the agent honestly reported they did not exist.
        seeded = worktree.seed(tid, folders, secrets=bool(terms.get("sees_secrets")))
    except Exception as exc:  # git refused; a blocked ticket, never a crashed pulse
        conn.execute("UPDATE tickets SET status = 'blocked', findings = ? WHERE id = ?",
                     (f"could not open a worktree: {exc}", tid))
        _event(conn, sid, "note", "build could not start — no isolated checkout", str(exc), tid)
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
             "The work was kept — the diff is waiting for you either way. Raise the ceiling "
             "or split the story before the next build.",
             result.chargeable_tokens),
        )

    # The diff is taken whatever the run's status. A timeout that wrote three
    # good files still wrote three good files, and throwing them away means
    # paying twice for the same work — the same lesson the over-budget path
    # learned in M1.
    try:
        patch = worktree.diff(tid)
        stat = worktree.stat(tid)
    except Exception as exc:
        patch, stat = "", f"(could not diff the worktree: {exc})"

    answer = result.json_payload() or {}
    summary = (answer.get("summary") or clip(result.text, 200) or "build finished").strip()

    if not patch.strip():
        conn.execute(
            "UPDATE tickets SET status = 'blocked', findings = ?, "
            "closed_at = datetime('now','localtime') WHERE id = ?",
            (result.error or result.text or "no changes", tid),
        )
        conn.execute("UPDATE stories SET status = 'ready' WHERE id = ?", (sid,))
        _event(conn, sid, "note", f"build changed nothing: {summary}",
               result.text or None, tid, result.chargeable_tokens)
        worktree.remove(tid)
        outcome["verdict"] = "no changes"
        return outcome

    path = worktree.save_patch(tid, patch)
    files = len(answer.get("files") or []) or patch.count("\ndiff --git ") + 1
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
         f'"{ticket["story_title"]}" has a patch waiting — {files} file'
         f'{"s" if files != 1 else ""} changed in {ticket["project"]}/.',
         "\n\n".join(b for b in detail_bits if b),
         json.dumps({"ticket_id": tid, "patch": str(path), "project": ticket["project"]}),
         result.chargeable_tokens),
    )
    _raise_run_requests(conn, ticket, answer)
    _event(conn, sid, "finding", summary, stat, tid, result.chargeable_tokens)
    if answer.get("learned"):
        _event(conn, sid, "learning", clip(str(answer["learned"]), 400),
               str(answer["learned"]), tid, 0)

    outcome["verdict"] = f"patch ready ({files} file{'s' if files != 1 else ''})"
    return outcome


def _raise_run_requests(conn: sqlite3.Connection, ticket: sqlite3.Row,
                        answer: dict) -> int:
    """Turn the agent's `needs_run` list into cards the PO can act on.

    A build agent has no shell, so a criterion phrased "run X and confirm Y"
    used to come back as a skip with a paragraph explaining why. The paragraph
    was correct and got nobody any closer. Now the agent writes the command it
    would have run and the colony asks Jordan whether to run it.

    Refused commands are not raised. Nothing is gained by putting a card in
    front of him that says `git push` on it; the reason is recorded on the
    story instead so the agent's request is not silently dropped.
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
            # Cleaned here rather than at run time so the card shows the line
            # that will actually be run, and so a `cd` out of the project folder
            # is refused before it is ever put in front of the PO.
            command = runner._drop_leading_cd(
                command, runner.check_folder(ticket["project"]))
        except runner.RunRefused as exc:
            _event(conn, ticket["sid"], "note",
                   f"{ticket['role']} asked to run a command the colony will not run",
                   f"$ {command}\n\n{exc}", ticket["id"], 0)
            continue
        body = [f"$ {command}", f"  in {ticket['project']}/"]
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
             json.dumps({"command": command, "project": ticket["project"],
                         "ticket_id": ticket["id"], "why": why,
                         "expect": expect})),
        )
        raised += 1
    return raised


def run(conn: sqlite3.Connection, limit: int = BUILD_LIMIT) -> list[dict]:
    """Every dispatched ticket this wake is willing to run."""
    return [run_one(conn, t) for t in pending(conn, limit)]
