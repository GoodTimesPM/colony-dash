"""PO actions — the only writes the dashboard is allowed to make.

M2 was read-only by construction: every request opened the ledger with
`read_only=True` and the dashboard could not be the reason state changed. M3
opens exactly one door, and this module is the door. Nothing else in `server.py`
takes a write connection, so the set of things the dashboard can do to the
colony is the list of public functions in this file, and that list is short on
purpose.

Two rules hold for every function here:

  1. **The action is recorded before it takes effect.** `po_actions` gets a row
     first, inside the same transaction. If a change landed, the reason it
     landed is in the ledger next to it — an autonomous system whose state moves
     without an attributable decision is a system you stop trusting.

  2. **Nothing here spends tokens.** Approving a story does not dispatch an
     agent; it marks the story dispatchable and lets the next wake decide. The
     gate and the spender stay separate, so a mis-click costs nothing and the
     budget guard still gets its say (ARCHITECTURE.md §4.4, §6.2).

HALT is the deliberate asymmetry: it writes a file *and* a control row. The one
moment you most need dispatch to stop is the moment something is wrong, and a
file on disk cannot be blocked by a locked database or an unresponsive server.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import db

HALT_FILE = db.RUNTIME_DIR / "HALT"

# The ceiling on the ceiling. `allowance_boost` exists for a genuinely heavy
# sprint, not for turning the guard off: 35% + 25% still leaves the majority of
# the week for Jordan's own Claude Code sessions, which share the same quota.
MAX_BOOST_POINTS = 25.0


class Refused(Exception):
    """A PO action the ledger will not perform, with the reason the UI shows."""


# ── plumbing ──────────────────────────────────────────────────────────────────


def _record(conn: sqlite3.Connection, action: str, kind: str | None,
            target: int | None, detail: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO po_actions (action, target_kind, target_id, detail) VALUES (?,?,?,?)",
        (action, kind, target, detail),
    )
    return cur.lastrowid


def _event(conn: sqlite3.Connection, story_id: int, kind: str, summary: str,
           detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO story_events (story_id, kind, summary, detail) VALUES (?,?,?,?)",
        (story_id, kind, summary[:400], detail),
    )


def get_control(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM controls WHERE key = ?", (key,)).fetchone()
    return (row["value"] if row and row["value"] is not None else default)


def set_control(conn: sqlite3.Connection, key: str, value: str, note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO controls (key, value, note, updated_at)
           VALUES (?,?,?,datetime('now','localtime'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                 note = COALESCE(excluded.note, controls.note),
                 updated_at = excluded.updated_at""",
        (key, value, note),
    )


# ── the big red switch ────────────────────────────────────────────────────────


def is_halted() -> bool:
    return HALT_FILE.exists()


def halt(conn: sqlite3.Connection, on: bool, reason: str = "") -> dict[str, Any]:
    """Stop or resume all dispatch, colony-wide.

    Halting does not stop the pulse. The heartbeat keeps logging, keeps syncing
    Notion and keeps reaping orphans — it just refuses to spend. A halt that
    also blinded the dashboard would make the emergency harder to diagnose,
    which is the opposite of what an emergency switch is for.

    Nothing here can kill a run that is already in flight; `claude -p` is a
    child process we wait on. The honest promise is "no new work", and the
    Colony panel keeps showing whatever is still running until it ends.
    """
    _record(conn, "halt" if on else "resume", "colony", None,
            reason or ("production halted" if on else "production resumed"))
    set_control(conn, "halt", "1" if on else "0", reason or None)
    HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if on:
        HALT_FILE.write_text(
            f"halted from the dashboard\n{reason}\n", encoding="utf-8")
    elif HALT_FILE.exists():
        HALT_FILE.unlink()
    return {"halted": on, "reason": reason}


def set_allowance(conn: sqlite3.Connection, boost_points: float) -> dict[str, Any]:
    """Raise the colony's share of the weekly window for a heavy sprint.

    The sprint's `budget_pct` is left alone and a *boost* is stored separately,
    so the baseline the colony was designed around stays visible next to the
    exception. Clearing the boost is the same call with 0 — there is no separate
    reset path to forget about.
    """
    asked = float(boost_points)
    if asked > MAX_BOOST_POINTS:
        raise Refused(
            f"boost is capped at +{MAX_BOOST_POINTS:.0f} points. The weekly quota is shared "
            f"with your own Claude Code sessions; the colony does not get to take the week."
        )
    # A negative boost is not an error, it is someone reaching for the reset —
    # and the reset is the same call with 0, so let it mean that.
    boost = max(0.0, asked)
    _record(conn, "allowance", "sprint", None,
            f"boost +{boost:g} points" if boost else "boost cleared — back to the baseline")
    set_control(conn, "allowance_boost", f"{boost:g}",
                "extra allowance for a high-volume sprint")
    return {"boost": boost}


def effective_allowance(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the budget guard should actually compare against, base + boost."""
    row = conn.execute(
        "SELECT budget_pct FROM sprints WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    base = float(row["budget_pct"]) if row else 35.0
    try:
        boost = float(get_control(conn, "allowance_boost", "0") or 0)
    except ValueError:
        boost = 0.0
    boost = max(0.0, min(MAX_BOOST_POINTS, boost))
    return {"base": base, "boost": boost, "effective": base + boost}


# ── the Inbox gate ────────────────────────────────────────────────────────────


def _close_escalation(conn: sqlite3.Connection, esc_id: int, decision: str) -> None:
    conn.execute(
        "UPDATE escalations SET resolved_at = datetime('now','localtime'), po_decision = ? "
        "WHERE id = ? AND resolved_at IS NULL",
        (decision, esc_id),
    )


def decide(conn: sqlite3.Connection, esc_id: int, decision: str,
           note: str = "", snooze_hours: float = 8) -> dict[str, Any]:
    """Answer one Inbox item. The single entry point for every escalation kind.

    What an approval *means* depends on the escalation, and that mapping lives
    here rather than in the UI so a future surface (a phone, a CLI, Ordis
    itself) cannot invent a different one:

      decision   → a groomed story's criteria are accepted; the story becomes
                   `ready`, which is the only state a dispatch may staff from.
      needs-info → the item is dismissed; the story stays blocked until the
                   answer arrives as an edit to the Notion page. Approving a
                   question you have not answered would be lying to the loop.
      hire       → the proposed contract is written into `agents`.
      write-     → the patch is applied to the live tree and left uncommitted,
      approval     for Jordan to read in his own editor and commit himself. The
                   colony never commits. Rejecting throws the worktree away and
                   returns the story to `ready`; the patch file survives, because
                   a rejected change you can no longer read is a decision you
                   cannot revisit.
      cost       → acknowledged. The overspend already happened; this is a
                   receipt, not a control.
    """
    if decision not in ("approve", "reject", "defer", "amend"):
        raise Refused(f"unknown decision {decision!r}")

    esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    if not esc:
        raise Refused("no such Inbox item")
    if esc["resolved_at"]:
        raise Refused("already decided")

    _record(conn, decision, "escalation", esc_id, note or esc["reason"][:400])
    kind = esc["kind"]
    outcome = decision

    if decision == "defer":
        # Deferring is not resolving: the item stays open and stays visible.
        # An Inbox you can empty without deciding anything is an Inbox that
        # stops meaning what it says.
        #
        # But it does stop being *loud*. `snoozed_until` is what lets the tile
        # gray itself out and sort to the back until the snooze runs out — the
        # difference between "not now" and "not important", which an Inbox with
        # only one visual weight cannot express.
        conn.execute(
            "UPDATE escalations SET raised_at = datetime('now','localtime'), "
            "snoozed_until = datetime('now','localtime', ?) WHERE id = ?",
            (f"+{int(snooze_hours)} hours", esc_id),
        )
        return {"ok": True, "kind": kind,
                "outcome": f"snoozed {int(snooze_hours)}h" if snooze_hours else "back in the Inbox"}

    # Any decision other than "later" wakes the item back up, so an approved
    # item never carries a stale snooze into the audit trail.
    conn.execute("UPDATE escalations SET snoozed_until = NULL WHERE id = ?", (esc_id,))

    _close_escalation(conn, esc_id, decision)

    if kind == "decision" and esc["story_id"] and decision == "approve":
        conn.execute(
            "UPDATE stories SET status = 'ready', blocked_reason = NULL, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (esc["story_id"],),
        )
        _event(conn, esc["story_id"], "decided",
               "PO accepted the acceptance criteria — story is ready to staff", note or None)
        outcome = "story is ready"
    elif kind == "decision" and esc["story_id"] and decision == "reject":
        conn.execute(
            "UPDATE stories SET status = 'needs-criteria', acceptance_criteria = NULL, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (esc["story_id"],),
        )
        _event(conn, esc["story_id"], "decided",
               "PO rejected the draft criteria — back for re-grooming", note or None)
        outcome = "sent back for re-grooming"
    elif kind == "hire" and decision == "approve":
        proposal = json.loads(esc["proposal"] or "{}")
        hired = hire(conn, **proposal, _skip_record=True)
        outcome = f"hired as agent #{hired['agent_id']}"
    elif kind == "write-approval":
        outcome = _settle_patch(conn, esc, decision, note)
    elif esc["story_id"]:
        _event(conn, esc["story_id"], "decided", f"PO {decision}d: {esc['reason'][:200]}",
               note or None)

    return {"ok": True, "outcome": outcome, "kind": kind}


def _settle_patch(conn: sqlite3.Connection, esc: sqlite3.Row, decision: str,
                  note: str) -> str:
    """Apply or discard a build's patch. The last gate before code is real."""
    from . import worktree

    proposal = json.loads(esc["proposal"] or "{}")
    ticket_id = proposal.get("ticket_id") or esc["ticket_id"]
    story_id = esc["story_id"]

    if decision == "reject":
        if ticket_id:
            worktree.remove(int(ticket_id))
        if story_id:
            conn.execute(
                "UPDATE stories SET status = 'ready', updated_at = datetime('now','localtime') "
                "WHERE id = ?", (story_id,))
            _event(conn, story_id, "decided",
                   "PO rejected the patch — nothing was applied", note or None)
        return "patch discarded, nothing applied"

    try:
        applied = worktree.apply_patch(int(ticket_id))
    except Exception as exc:
        # An escalation that cannot be actioned must stay open. Closing it would
        # tell the loop a change landed that did not.
        conn.execute("UPDATE escalations SET resolved_at = NULL, po_decision = NULL "
                     "WHERE id = ?", (esc["id"],))
        raise Refused(f"the patch would not apply: {exc}")

    worktree.remove(int(ticket_id))
    if story_id:
        conn.execute(
            "UPDATE stories SET status = 'accepted', updated_at = datetime('now','localtime') "
            "WHERE id = ?", (story_id,))
        _event(conn, story_id, "accepted",
               f"PO approved the patch — {applied['files']} file(s) applied, uncommitted",
               "Review and commit it yourself; the colony does not commit.")
    return f"{applied['files']} file(s) applied to your working tree, uncommitted"


def confirm_project(conn: sqlite3.Connection, story_id: int, project: str) -> dict[str, Any]:
    """Name the folder a story belongs to — the answer to five of six Inbox items.

    This is the moment an inference becomes a permission. `project_source` goes
    to 'confirmed', and only a confirmed project can ever become a write scope
    (§8.2). Every open escalation asking about this story's project is closed by
    the same call, because they were all asking the one question.
    """
    project = (project or "").strip().replace("\\", "/").strip("/")
    if not project:
        raise Refused("name a folder, or say it is a new project")

    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")

    known = set(_project_dirs())
    if project not in known:
        # A folder that does not exist yet is a legitimate answer — new work is
        # new work — but it is recorded as such rather than silently accepted as
        # if it were an existing tree.
        note = f"{project} (does not exist yet — new project)"
    else:
        note = project

    _record(conn, "confirm-project", "story", story_id, note)
    conn.execute(
        "UPDATE stories SET project = ?, project_source = 'confirmed', "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        (project, story_id),
    )
    conn.execute(
        """UPDATE escalations SET resolved_at = datetime('now','localtime'),
                  po_decision = 'approve'
            WHERE story_id = ? AND resolved_at IS NULL AND kind IN ('needs-info','decision')
              AND (reason LIKE '%project folder%' OR reason LIKE '%belongs to%'
                   OR reason LIKE '%I am guessing%')""",
        (story_id,),
    )
    _event(conn, story_id, "decided", f"PO confirmed the project folder: {note}")
    return {"ok": True, "project": project, "note": note}


# ── dropping things ───────────────────────────────────────────────────────────
#
# A backlog you cannot take things off is not a backlog, it is a guilt trip. The
# PO asked for this in exactly those terms: some rows arrive from Notion, get
# looked at, and are simply not going to happen — and until now the only way to
# say so was to change the row in Notion and wait an hour for the sync.
#
# Dropping is local, reversible, and honest about being a decision: the story
# goes to 'archived' with a timestamp and a reason, every open question about it
# closes as 'reject', and the Notion side is *offered* rather than assumed. A PO
# who drops a story from the dashboard has not necessarily decided to change what
# his own board says.


def drop_story(conn: sqlite3.Connection, story_id: int, *, reason: str = "",
               notion_status: str | None = None) -> dict[str, Any]:
    """Take a story off the board. Reversible; `restore_story` is the undo."""
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")
    if story["dropped_at"]:
        raise Refused("already dropped")
    if story["status"] == "in-progress":
        # Not a rule about tidiness: a running ticket has a worktree and a budget
        # attached, and archiving the story out from under it would leave both
        # orphaned. Cancel the ticket first and the drop goes through.
        raise Refused("a ticket is running on this story — cancel it first")

    reason = (reason or "").strip()
    _record(conn, "drop", "story", story_id, reason or "(no reason given)")
    conn.execute(
        """UPDATE stories SET status = 'archived', dropped_at = datetime('now','localtime'),
                  drop_reason = ?, updated_at = datetime('now','localtime')
            WHERE id = ?""",
        (reason or None, story_id),
    )
    closed = conn.execute(
        """UPDATE escalations SET resolved_at = datetime('now','localtime'),
                  po_decision = 'reject'
            WHERE story_id = ? AND resolved_at IS NULL""",
        (story_id,),
    ).rowcount
    # Open tickets die with it. They exist to serve a story that no longer wants
    # doing, and leaving them 'open' would keep the colony offering to staff them.
    orphaned = conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE story_id = ? AND status IN ('open','staffed','blocked')""",
        (story_id,),
    ).rowcount
    _event(conn, story_id, "decided",
           "PO dropped this story" + (f": {reason}" if reason else ""))

    pushed = None
    if notion_status and story["notion_page_id"]:
        pushed = queue_notion(conn, story_id=story_id, kind="status",
                              payload={"status": notion_status}, record=False)
    return {"ok": True, "closed": closed, "tickets": orphaned, "queued": pushed,
            "message": f"dropped · {closed} question(s) closed · {orphaned} ticket(s) wontfix"}


def restore_story(conn: sqlite3.Connection, story_id: int) -> dict[str, Any]:
    """Undo a drop. Back to the backlog, ungroomed, as if it had just arrived.

    Criteria are cleared on the way back in. The drop may have been the right
    call for a week and wrong today, and criteria drafted against the old
    reading of the story are exactly the stale prose M5 exists to stop.
    """
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")
    if not story["dropped_at"]:
        raise Refused("that story was not dropped")

    _record(conn, "restore", "story", story_id, story["drop_reason"])
    conn.execute(
        """UPDATE stories SET status = 'backlog', dropped_at = NULL, drop_reason = NULL,
                  acceptance_criteria = NULL, updated_at = datetime('now','localtime')
            WHERE id = ?""",
        (story_id,),
    )
    _event(conn, story_id, "decided", "PO put this story back on the board")
    return {"ok": True, "message": "back on the board, ungroomed"}


# ── talking back to Notion ────────────────────────────────────────────────────


def queue_notion(conn: sqlite3.Connection, *, story_id: int, kind: str,
                 payload: dict, record: bool = True) -> int:
    """Queue one upward write. Nothing here touches the network — see outbox.py.

    The button is instant and transactional; the tick does the HTTP an hour
    later, or sooner if Jordan runs `python -m colony pulse` himself. Same shape
    as the skill-draft request in 007, for the same reason.
    """
    from . import notion as notion_mod
    from . import outbox

    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")
    if not story["notion_page_id"]:
        raise Refused("this story was written by the loop — it has no Notion page")

    if kind == "status":
        status = (payload or {}).get("status")
        if status not in notion_mod.WRITABLE_STATUS:
            raise Refused(
                f"the colony may only set {', '.join(notion_mod.WRITABLE_STATUS)} — "
                f"starting work is yours"
            )
        detail = f"Status -> {status}"
    elif kind == "comment":
        text = ((payload or {}).get("text") or "").strip()
        if not text:
            raise Refused("write something first")
        payload = {"text": text[:1800]}
        detail = text[:120]
    elif kind == "check":
        item = ((payload or {}).get("item") or "").strip()
        if not item:
            raise Refused("name the checklist item to tick")
        payload = {"item": item, "checked": bool((payload or {}).get("checked", True))}
        detail = f"tick: {item[:100]}"
    else:
        raise Refused(f"the colony cannot do {kind!r} to a Notion page")

    if record:
        _record(conn, "notion", "story", story_id, detail)
    row_id = outbox.queue(conn, story_id=story_id, page_id=story["notion_page_id"],
                          kind=kind, payload=payload)
    _event(conn, story_id, "note", f"queued for Notion: {detail}")
    return row_id


def set_notion_write(conn: sqlite3.Connection, on: bool) -> dict[str, Any]:
    """The kill switch for the whole upward direction.

    Off does not drop the queue — it holds it. Turning the colony's voice off
    for an afternoon should not lose the three things it was going to say.
    """
    _record(conn, "note", "colony", None, f"notion_write -> {'on' if on else 'off'}")
    set_control(conn, "notion_write", "1" if on else "0",
                "the colony may push status, comments and checkboxes back to Notion")
    return {"ok": True, "on": bool(on),
            "message": "Notion writes on" if on else "Notion writes held — nothing is lost"}


def reask(conn: sqlite3.Connection, esc_id: int) -> dict[str, Any]:
    """Throw a stale question back to Ordis instead of answering it.

    The card said something true about a version of the story that no longer
    exists. Answering it would be answering the wrong question; rejecting it
    would lose the fact that something here still needs a look. So it resolves
    as 'amend' and the story goes back in the groom queue, where the next wake
    reads it as it is now — the one path where free detection turns into a
    deliberate, budgeted re-read.
    """
    esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    if not esc:
        raise Refused("no such escalation")
    if esc["resolved_at"]:
        raise Refused("already decided")
    if not esc["story_id"]:
        raise Refused("nothing to re-read — this question is not about a story")

    _record(conn, "note", "escalation", esc_id, "re-ask: brief changed under the question")
    conn.execute(
        """UPDATE escalations SET resolved_at = datetime('now','localtime'),
                  po_decision = 'amend' WHERE id = ?""",
        (esc_id,),
    )
    conn.execute(
        """UPDATE stories SET status = 'backlog', acceptance_criteria = NULL,
                  updated_at = datetime('now','localtime')
            WHERE id = ?""",
        (esc["story_id"],),
    )
    conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE story_id = ? AND title LIKE 'Groom:%' AND status <> 'wontfix'""",
        (esc["story_id"],),
    )
    _event(conn, esc["story_id"], "decided",
           "PO sent this back to Ordis — the brief changed after the question was written")
    return {"ok": True, "message": "back in the groom queue — Ordis re-reads it on the next wake"}


def _project_dirs() -> list[str]:
    from . import projects
    return projects.project_dirs()


SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


def _today(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT date('now','localtime') AS d").fetchone()["d"]


def create_project(conn: sqlite3.Connection, name: str, *, why: str = "") -> dict[str, Any]:
    r"""Make a new project folder, with the PROJECT.md the repo convention requires.

    This exists because the commonest answer to "which folder does this story
    belong to?" turned out to be one the dropdown could not express: *none of
    them yet*. A list of existing folders is only a question if the true answer
    is somewhere on the list.

    It is the one place the dashboard writes outside the ledger, and it is
    deliberately the smallest write that could be useful: `mkdir` plus a stub.
    The name is validated segment by segment against a whitelist rather than
    scanned for `..`, because a whitelist fails closed and a blacklist fails the
    day someone finds a spelling nobody thought of. Two levels at most, matching
    the only shape `project_dirs()` will ever report.
    """
    name = (name or "").strip().replace("\\", "/").strip("/")
    if not name:
        raise Refused("give the new project a folder name")

    parts = [p.strip() for p in name.split("/") if p.strip()]
    if len(parts) > 2:
        raise Refused("projects live one or two levels under the root, not deeper")
    for part in parts:
        if not SAFE_SEGMENT.match(part):
            raise Refused(f"{part!r} is not a folder name I will create — letters, "
                          "digits, spaces, dot, dash and underscore only")

    rel = "/".join(parts)
    path = db.PROJECTS_ROOT.joinpath(*parts)
    # Belt and braces: the whitelist above already makes traversal impossible,
    # but the resolved path is checked against the root anyway. Two independent
    # checks on the one operation that leaves the ledger is cheap.
    if db.PROJECTS_ROOT.resolve() not in path.resolve().parents:
        raise Refused("that path is outside the projects root")

    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    stub = path / "PROJECT.md"
    if not stub.exists():
        stub.write_text(
            f"# {parts[-1]}\n\n"
            f"**Status:** new — folder created from the Colony Dash Inbox on "
            f"{_today(conn)}.\n\n"
            f"{(why or 'No brief yet.').strip()}\n\n"
            "## Next\n\n- Say what this project is for.\n",
            encoding="utf-8",
        )
    _record(conn, "confirm-project", "story", None,
            f"created project folder {rel}" + ("" if not existed else " (already existed)"))
    return {"ok": True, "project": rel, "path": str(path), "created": not existed}


# ── talking back ──────────────────────────────────────────────────────────────


def reply(conn: sqlite3.Connection, *, escalation_id: int | None = None,
          story_id: int | None = None, body: str = "") -> dict[str, Any]:
    """Write a sentence to Ordis about one Inbox item.

    The message is queued, not delivered: nothing here spends a token, same as
    every other function in this module. The next wake picks up the unread rows,
    answers them, and writes the answer back into the same thread as an `ordis`
    message (`wake.answer_po`). Until then the tile says so — an Inbox that
    swallows what you typed and shows no sign of it is worse than one with no
    reply box at all.

    The escalation stays **open**. A reply is not a decision, which is the whole
    reason for having both.
    """
    body = (body or "").strip()
    if not body:
        raise Refused("nothing to send")
    if len(body) > 8000:
        raise Refused("that is longer than a work order — trim it to 8,000 characters")

    if escalation_id:
        esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
        if not esc:
            raise Refused("no such Inbox item")
        story_id = story_id or esc["story_id"]

    cur = conn.execute(
        "INSERT INTO po_messages (escalation_id, story_id, author, body) VALUES (?,?,'po',?)",
        (escalation_id, story_id, body),
    )
    _record(conn, "note", "escalation" if escalation_id else "story",
            escalation_id or story_id, body[:400])
    if story_id:
        _event(conn, story_id, "note", "PO wrote to Ordis about this", body[:2000])
    # A replied-to item stops shouting but stays open: waiting on an answer is
    # not the same as being answered.
    if escalation_id:
        conn.execute("UPDATE escalations SET snoozed_until = NULL WHERE id = ?", (escalation_id,))
    return {"ok": True, "message_id": cur.lastrowid, "queued": True}


def thread(conn: sqlite3.Connection, escalation_id: int | None = None,
           story_id: int | None = None) -> list[dict[str, Any]]:
    """The whole conversation about one item, oldest first."""
    if escalation_id:
        rows = conn.execute(
            "SELECT * FROM po_messages WHERE escalation_id = ? ORDER BY id", (escalation_id,)
        ).fetchall()
    elif story_id:
        rows = conn.execute(
            "SELECT * FROM po_messages WHERE story_id = ? ORDER BY id", (story_id,)
        ).fetchall()
    else:
        rows = []
    return [dict(r) for r in rows]


def unread_messages(conn: sqlite3.Connection, limit: int = 4) -> list[sqlite3.Row]:
    """What the PO has said that Ordis has not answered yet."""
    return conn.execute(
        "SELECT * FROM po_messages WHERE author = 'po' AND status = 'unread' "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


# ── staffing ──────────────────────────────────────────────────────────────────

DEFAULT_READ_SCOPE = ["D:/ALL STUFF/PROJECTS/**"]
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
WRITE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write"]


def propose_hire(conn: sqlite3.Connection, *, roster_slug: str, role: str,
                 project: str | None, reason: str, model: str = "claude-sonnet-5",
                 write_capable: bool = False, max_tokens_run: int = 120000,
                 story_id: int | None = None) -> int:
    """Raise a hire for approval. Does not hire anything.

    The proposal is written down on the escalation rather than reconstructed at
    approval time. An approval that has to re-derive what it is approving is an
    approval of something else — the roster can change between the two clicks.
    """
    persona = conn.execute("SELECT * FROM roster WHERE slug = ?", (roster_slug,)).fetchone()
    if not persona:
        raise Refused(f"no persona {roster_slug!r} in the roster")

    proposal = {
        "roster_slug": roster_slug, "role": role, "project": project, "model": model,
        "write_capable": write_capable, "max_tokens_run": max_tokens_run,
    }
    cur = conn.execute(
        """INSERT INTO escalations (story_id, kind, reason, recommendation, proposal, est_tokens)
           VALUES (?, 'hire', ?, ?, ?, ?)""",
        (story_id,
         f"Hire {persona['name']} as {role}" + (f" on {project}" if project else "") + "?",
         reason, json.dumps(proposal), max_tokens_run),
    )
    return cur.lastrowid


def hire(conn: sqlite3.Connection, *, roster_slug: str | None, role: str,
         project: str | None = None, model: str = "claude-sonnet-5",
         write_capable: bool = False, max_tokens_run: int = 120000,
         notes: str | None = None, _skip_record: bool = False) -> int:
    """Turn a persona into an agent with a contract.

    The persona says how to think; the contract says what may be touched
    (§2.1). Nothing is inherited from the persona file — not tools, not model,
    not scope — because those files carry no governance and never will.

    A write-capable contract without a project is refused. "Write, somewhere"
    is not a scope; it is the absence of one.
    """
    if write_capable and not project:
        raise Refused("a write-capable contract needs a project — 'write anywhere' is not a scope")

    persona = None
    if roster_slug:
        persona = conn.execute("SELECT * FROM roster WHERE slug = ?", (roster_slug,)).fetchone()
        if not persona:
            raise Refused(f"no persona {roster_slug!r} in the roster")

    existing = conn.execute(
        "SELECT id, status FROM agents WHERE role = ? AND project IS ?", (role, project)
    ).fetchone()
    if existing and existing["status"] != "retired":
        raise Refused(f"{role} is already hired" + (f" on {project}" if project else ""))

    tools = WRITE_TOOLS if write_capable else READ_ONLY_TOOLS
    write_scope = [f"D:/ALL STUFF/PROJECTS/{project}/**"] if write_capable else None
    seed = roster_slug or role

    if existing:
        conn.execute(
            """UPDATE agents SET roster_slug=?, model=?, write_capable=?, tools_allowed=?,
                      read_scope=?, write_scope=?, max_tokens_run=?, status='standby',
                      hired_at=datetime('now','localtime'), retired_at=NULL, notes=?,
                      hired_from='dashboard'
                WHERE id = ?""",
            (roster_slug, model, int(write_capable), json.dumps(tools),
             json.dumps(DEFAULT_READ_SCOPE), json.dumps(write_scope) if write_scope else None,
             max_tokens_run, notes, existing["id"]),
        )
        agent_id = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO agents (role, project, roster_slug, model, write_capable,
                                   tools_allowed, read_scope, write_scope, max_tokens_run,
                                   avatar_seed, status, notes, hired_from)
               VALUES (?,?,?,?,?,?,?,?,?,?,'standby',?, 'dashboard')""",
            (role, project, roster_slug, model, int(write_capable), json.dumps(tools),
             json.dumps(DEFAULT_READ_SCOPE), json.dumps(write_scope) if write_scope else None,
             max_tokens_run, seed, notes),
        )
        agent_id = cur.lastrowid

    if not _skip_record:
        _record(conn, "hire", "agent", agent_id,
                f"{role}" + (f" on {project}" if project else "")
                + (f" from {roster_slug}" if roster_slug else "")
                + (" · write-capable" if write_capable else " · read-only"))
    # A dict, like every other control here, so the caller never has to know
    # which of these returns an id and which returns a verdict.
    return {"ok": True, "agent_id": agent_id, "role": role, "project": project,
            "write_capable": bool(write_capable)}


def retire(conn: sqlite3.Connection, agent_id: int) -> dict[str, Any]:
    """Take an agent off the books. History stays — runs still point at the role."""
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        raise Refused("no such agent")
    if row["project"] is None:
        raise Refused(
            f"{row['role']} is structural — the colony needs it to groom and review. "
            f"Halt dispatch instead if you want it to stop."
        )
    _record(conn, "retire", "agent", agent_id, row["role"])
    conn.execute(
        "UPDATE agents SET status = 'retired', retired_at = datetime('now','localtime') "
        "WHERE id = ?", (agent_id,)
    )
    return {"ok": True, "role": row["role"]}


# ── dispatch ──────────────────────────────────────────────────────────────────


def dispatch(conn: sqlite3.Connection, story_id: int) -> dict[str, Any]:
    """Queue a story for real, write-capable work. Spends nothing.

    Four things have to be true, and each refusal names which one failed rather
    than saying "not allowed":

      * the story is `ready` — the PO accepted its criteria at the Inbox gate;
      * its project is *confirmed*, not inferred (§8.2);
      * an agent is hired with write scope on that project;
      * the colony is not halted.

    What this creates is a ticket, not a run. The next wake picks it up, opens a
    git worktree and spends the tokens — so the gate and the spender stay
    separate, and the budget guard still gets to refuse after you have approved.
    """
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")
    if is_halted():
        raise Refused("the colony is halted — resume dispatch first")
    if story["status"] != "ready":
        raise Refused(
            f"story is {story['status']}, not ready. Accept its acceptance criteria "
            f"in the Inbox first — that gate is what makes a story dispatchable."
        )
    if not story["project"] or story["project_source"] != "confirmed":
        raise Refused(
            "this story's project folder is still a guess. Confirm it first — an "
            "inference cannot authorise a write."
        )

    agent = conn.execute(
        "SELECT * FROM agents WHERE project = ? AND write_capable = 1 AND status != 'retired' "
        "ORDER BY id LIMIT 1",
        (story["project"],),
    ).fetchone()
    if not agent:
        raise Refused(
            f"nobody is hired to write in {story['project']}. Hire someone from Standby "
            f"with write scope on that folder."
        )

    open_ticket = conn.execute(
        "SELECT id FROM tickets WHERE story_id = ? AND intent = 'implement' "
        "AND status IN ('open','staffed','running')",
        (story_id,),
    ).fetchone()
    if open_ticket:
        raise Refused(f"ticket #{open_ticket['id']} is already queued for this story")

    _record(conn, "dispatch", "story", story_id, f"{story['project']} · {agent['role']}")
    cur = conn.execute(
        """INSERT INTO tickets (story_id, title, intent, role, status, write_scope,
                                requires_po, approved_at)
           VALUES (?,?,'implement',?,'staffed',?,1,datetime('now','localtime'))""",
        (story_id, f"Implement: {story['title']}"[:200], agent["role"],
         f"D:/ALL STUFF/PROJECTS/{story['project']}"),
    )
    ticket_id = cur.lastrowid
    conn.execute(
        "UPDATE stories SET status = 'in-progress', updated_at = datetime('now','localtime') "
        "WHERE id = ?", (story_id,)
    )
    _event(conn, story_id, "staffed",
           f"PO dispatched to {agent['role']} — worktree, write scope {story['project']}/",
           f"ticket #{ticket_id}")
    return {"ok": True, "ticket_id": ticket_id, "role": agent["role"]}


def cancel_ticket(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any]:
    """Pull a queued ticket back before it runs."""
    t = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not t:
        raise Refused("no such ticket")
    if t["status"] == "running":
        raise Refused("that ticket is already running — halt the colony to stop new work; "
                      "a run in flight is a child process and has to finish")
    _record(conn, "cancel", "ticket", ticket_id, t["title"])
    conn.execute("UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime') "
                 "WHERE id = ?", (ticket_id,))
    if t["story_id"]:
        conn.execute("UPDATE stories SET status = 'ready' WHERE id = ? AND status = 'in-progress'",
                     (t["story_id"],))
        _event(conn, t["story_id"], "note", "PO cancelled the queued ticket")
    return {"ok": True}


# ── the forge (M4) ────────────────────────────────────────────────────────────
#
# Three gates, and the middle one is the reason the other two are cheap. Asking
# for a draft costs nothing (the next wake pays); promoting writes a file; and
# retiring is how a skill that stopped earning its context window gets removed.
# Detection is not here at all — it is free, it runs on the pulse, and it needs
# no permission to notice something.


def request_draft(conn: sqlite3.Connection, skill_id: int) -> dict[str, Any]:
    """Ask Ordis to write this candidate up. Queued, not spent — see forge.py."""
    row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if not row:
        raise Refused("no such skill")
    if row["status"] != "candidate":
        raise Refused(f"that skill is already {row['status']} — only a candidate can be drafted")
    if row["draft_requested_at"]:
        raise Refused("already queued; the next wake will draft it")

    _record(conn, "draft-skill", "skill", skill_id, row["name"])
    conn.execute("UPDATE skills SET draft_requested_at = datetime('now','localtime') WHERE id = ?",
                 (skill_id,))
    return {"ok": True, "queued": True, "slug": row["slug"],
            "outcome": "Ordis drafts it on the next wake"}


def promote_skill(conn: sqlite3.Connection, skill_id: int,
                  roles: list[str] | None = None) -> dict[str, Any]:
    """Put a drafted skill on disk and attach it to the roles that will load it.

    The one PO action in this file with an effect outside the ledger. The order
    matters: the decision is recorded, the row is updated, and the file is
    written *last* — so a failed write rolls the whole transaction back and
    never leaves a `skills` row pointing at a path that does not exist. The
    reverse residue (a written file whose COMMIT then failed) is the lesser
    harm: an unreferenced SKILL.md is inert until something attaches it.
    """
    from . import forge

    row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if not row:
        raise Refused("no such skill")
    if row["status"] != "drafted":
        raise Refused(f"that skill is {row['status']} — only a drafted skill can be promoted. "
                      "Ask for a draft first.")
    if not (row["draft_md"] or "").strip():
        raise Refused("the draft is empty — there is nothing to promote")

    roles = [r.strip() for r in (roles or ["ordis"]) if r and r.strip()]
    if not roles:
        raise Refused("name at least one role to attach it to (ordis counts)")

    _record(conn, "promote-skill", "skill", skill_id, f"{row['name']} -> {', '.join(roles)}")
    try:
        path = forge.skill_path(row["slug"])
    except ValueError as exc:
        raise Refused(str(exc)) from exc

    conn.execute(
        """UPDATE skills SET status = 'active', path = ?, roles = ?,
                  promoted_at = datetime('now','localtime') WHERE id = ?""",
        (str(path), json.dumps(roles), skill_id),
    )
    attached = forge.attach(conn, row["slug"], roles)
    forge.write_skill_file(row["slug"], row["draft_md"])
    return {"ok": True, "slug": row["slug"], "path": str(path), "roles": attached,
            "outcome": f"active for {', '.join(attached) or 'nobody yet'}"}


def retire_skill(conn: sqlite3.Connection, skill_id: int, reason: str = "") -> dict[str, Any]:
    """Take a skill out of circulation. The file stays; nothing loads it.

    Deliberately not a delete. A retired skill is evidence about which detector
    keeps proposing things that do not work, and that question is only
    answerable if the retired rows are still there to count.
    """
    from . import forge

    row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if not row:
        raise Refused("no such skill")
    if row["status"] == "retired":
        raise Refused("already retired")

    reason = (reason or "").strip() or "no longer earning its context window"
    _record(conn, "retire-skill", "skill", skill_id, f"{row['name']}: {reason}"[:400])
    conn.execute(
        """UPDATE skills SET status = 'retired', retired_at = datetime('now','localtime'),
                  retire_reason = ?, draft_requested_at = NULL WHERE id = ?""",
        (reason[:400], skill_id),
    )

    # Detach from every contract that carried it, or the next run still loads a
    # procedure the PO just judged wrong.
    for agent_row in conn.execute(
        "SELECT id, skills FROM agents WHERE skills LIKE ?", (f'%"{row["slug"]}"%',)
    ).fetchall():
        kept = [s for s in json.loads(agent_row["skills"] or "[]") if s != row["slug"]]
        conn.execute("UPDATE agents SET skills = ? WHERE id = ?",
                     (json.dumps(kept), agent_row["id"]))

    return {"ok": True, "slug": row["slug"], "outcome": f"retired — {reason}"}


def skill_draft(conn: sqlite3.Connection, skill_id: int) -> dict[str, Any]:
    """Read one skill's draft, for the drawer. Read-only; not a PO action."""
    row = conn.execute(
        "SELECT id, name, slug, status, summary, detector, trigger_when, draft_md, path, "
        "roles, evidence_runs, baseline_tokens, times_used, wins, losses, tokens_saved "
        "FROM skills WHERE id = ?", (skill_id,)
    ).fetchone()
    if not row:
        raise Refused("no such skill")
    return dict(row)
