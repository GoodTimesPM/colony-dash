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

from . import agent, attachments as attach, control, db, worktree

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


def build_prompt(ticket: sqlite3.Row, workdir: str,
                 attached: list[dict] | None = None,
                 scope: list[str] | None = None) -> str:
    """The work order. Says what may be touched, in the words of the scope itself.

    `scope` is the folder list off the agent's contract, which the PO can widen
    from the contract drawer. It defaults to the story's own folder, which is
    what every contract holds until he changes one.
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
git commands, no package installs, no network. If a change needs any of those,
stop and say so in your report instead of working around it.

This checkout is git's copy of the last commit, so files git does not track are
not here: no `.env`, no build output, nothing Jordan has edited but not yet
committed. If a criterion depends on one of those, say so plainly — an absent
`.env` means the setting is not visible to you, not that it is unset.

STORY #{ticket['sid']}: {ticket['story_title']}

--- brief ---
{brief[:5000]}
--- end brief ---

--- acceptance criteria (approved by Jordan) ---
{criteria[:3000]}
--- end criteria ---
{attach.evidence(attached or [])}

Read {project}/PROJECT.md first — it is that project's source of truth for
status and decisions. Match the surrounding code: its naming, its comment
density, its idioms. Do not restructure things you were not asked to change,
and do not add dependencies.

Work the criteria in order. If one of them turns out to be impossible or wrong,
do the others in full and say precisely which one you left and why — scaling the
work down is Jordan's call, not yours.

When you are done, reply with ONLY a JSON object, no prose around it:

{{
  "done": ["criteria you completed, verbatim from the list"],
  "skipped": [{{"criterion": "...", "why": "..."}}],
  "files": ["relative/paths/you/changed"],
  "summary": "one sentence for the dashboard, under 140 characters",
  "risks": "anything Jordan should look at closely in the diff, or null",
  "learned": "one thing worth keeping about this codebase, or null"
}}"""


def _event(conn, story_id, kind, summary, detail=None, ticket_id=None, tokens=0):
    conn.execute(
        """INSERT INTO story_events (story_id, ticket_id, kind, summary, detail, tokens)
           VALUES (?,?,?,?,?,?)""",
        (story_id, ticket_id, kind, summary[:400], detail, tokens),
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

    try:
        work = worktree.create(tid)
    except Exception as exc:  # git refused; a blocked ticket, never a crashed pulse
        conn.execute("UPDATE tickets SET status = 'blocked', findings = ? WHERE id = ?",
                     (f"could not open a worktree: {exc}", tid))
        _event(conn, sid, "note", "build could not start — no isolated checkout", str(exc), tid)
        outcome["verdict"] = "no worktree"
        return outcome

    prompt = build_prompt(ticket, str(work), attach.for_story(conn, ticket["sid"]),
                          scope=control.scope_projects(terms.get("write_scope")))
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
    summary = (answer.get("summary") or result.text[:200] or "build finished").strip()

    if not patch.strip():
        conn.execute(
            "UPDATE tickets SET status = 'blocked', findings = ?, "
            "closed_at = datetime('now','localtime') WHERE id = ?",
            ((result.error or result.text or "no changes")[:4000], tid),
        )
        conn.execute("UPDATE stories SET status = 'ready' WHERE id = ?", (sid,))
        _event(conn, sid, "note", f"build changed nothing: {summary}",
               result.text[:4000] or None, tid, result.chargeable_tokens)
        worktree.remove(tid)
        outcome["verdict"] = "no changes"
        return outcome

    path = worktree.save_patch(tid, patch)
    files = len(answer.get("files") or []) or patch.count("\ndiff --git ") + 1
    outcome["files"] = files

    conn.execute(
        "UPDATE tickets SET status = 'done', findings = ?, artifact_path = ?, "
        "closed_at = datetime('now','localtime') WHERE id = ?",
        (json.dumps(answer, indent=2)[:8000] if answer else result.text[:8000], str(path), tid),
    )
    conn.execute("UPDATE stories SET status = 'po-review', "
                 "updated_at = datetime('now','localtime') WHERE id = ?", (sid,))

    # Gate 5. The patch exists; nothing has been applied. This is the escalation
    # the whole milestone is built around.
    detail_bits = [stat.strip()]
    if answer.get("skipped"):
        detail_bits.append("SKIPPED:\n" + "\n".join(
            f"  · {s.get('criterion')} — {s.get('why')}" for s in answer["skipped"]))
    if answer.get("risks"):
        detail_bits.append("LOOK CLOSELY AT:\n  " + str(answer["risks"]))
    conn.execute(
        """INSERT INTO escalations (story_id, ticket_id, kind, reason, recommendation,
                                    proposal, est_tokens)
           VALUES (?,?,'write-approval',?,?,?,?)""",
        (sid, tid,
         f'"{ticket["story_title"]}" has a patch waiting — {files} file'
         f'{"s" if files != 1 else ""} changed in {ticket["project"]}/.',
         "\n\n".join(b for b in detail_bits if b)[:2000],
         json.dumps({"ticket_id": tid, "patch": str(path), "project": ticket["project"]}),
         result.chargeable_tokens),
    )
    _event(conn, sid, "finding", summary, stat, tid, result.chargeable_tokens)
    if answer.get("learned"):
        _event(conn, sid, "learning", str(answer["learned"])[:400], None, tid, 0)

    outcome["verdict"] = f"patch ready ({files} file{'s' if files != 1 else ''})"
    return outcome


def run(conn: sqlite3.Connection, limit: int = BUILD_LIMIT) -> list[dict]:
    """Every dispatched ticket this wake is willing to run."""
    return [run_one(conn, t) for t in pending(conn, limit)]
