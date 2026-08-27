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

import contextlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from . import attachments as attach, db

HALT_FILE = db.RUNTIME_DIR / "HALT"

# How much of a card's own text is kept. It was 1000 characters, a number
# nobody chose for a reason, and short enough to cut a list of acceptance
# criteria in half: the last bullet of story #1 reached the PO as "The weekly
# command runs and pr". A card cut in the middle reads as the whole ask, so
# the PO answers a question he has not seen the end of.
CARD_TEXT = 8000


def card_text(text: str) -> str:
    """A card's text in full, cut only if it is absurd, and visibly when it is."""
    text = str(text or "")
    if len(text) <= CARD_TEXT:
        return text
    cut = text[:CARD_TEXT]
    space = cut.rfind(" ")
    return (cut[:space] if space > CARD_TEXT * 0.9 else cut).rstrip() + "\u2026"

# The whole dial, end to end. `allowance_boost` is stored as a *signed* delta
# from the sprint's designed baseline, so the baseline stays visible next to
# whatever the PO has done to it — and the PO can take it to the whole week or
# down to nothing. It used to stop at +25 and refuse to go below the baseline
# at all, which made it a ratchet rather than a dial: every press raised the
# ceiling and the only way down was to drop the boost entirely and rebuild it.
#
# The cap is gone because the cap was a guess on the PO's behalf. The quota is
# shared with Jordan's own Claude Code sessions and he is the one who knows what
# he needs this week; the honest job of this control is to show him the number,
# not to hold it down. Zero is a real setting too — it stops the colony spending
# without the finality of HALT.
MAX_ALLOWANCE_PCT = 100.0
MIN_ALLOWANCE_PCT = 0.0


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


def _decision_ticket(conn: sqlite3.Connection, *, story_id: int | None, title: str,
                     question: str, answer: str, esc_id: int | None = None) -> int:
    """Write down a call the PO made, as a ticket, closed the second it is made.

    "Basically any call/choice can be made a ticket to ensure that it has been
    understood." Until now a decision produced an `escalations` row that went
    quiet and a line in `audit` that nothing renders. The Ticket Queue is where
    the PO watches work exist, and the most consequential thing he does all week
    — accepting a set of acceptance criteria — put nothing there.

    So the call gets a row of its own, holding the question on one side and his
    answer on the other. It is born `done`: this is not work to do, it is work
    that was done, by him. Nothing reads these to decide anything, and that is
    the point — a ticket that changed the loop's behaviour would make pressing
    Approve mean two things, and the second one would be invisible.
    """
    cur = conn.execute(
        """INSERT INTO tickets (story_id, title, intent, status, work_order,
                                findings, requires_po, closed_at, decided_esc_id)
           VALUES (?,?,'chore','done',?,?,1,datetime('now','localtime'),?)""",
        (story_id, title[:200], question[:8000], answer[:2000], esc_id),
    )
    return int(cur.lastrowid)


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


# ── forcing a beat ─────────────────────────────────────────────────────────────
#
# The heartbeat is a Windows scheduled task that fires at :07 every hour and
# knows nothing about this module. Forcing a pulse does not touch it: the
# 1:07 beat happens, a forced beat at 1:37 happens, and the 2:07 beat still
# happens on time. What a forced beat must not do is collide with a
# scheduled one, because two pulses running at once would both sync Notion,
# both reap the same orphaned runs, and possibly both dispatch the same
# story. So every pulse — scheduled, typed at the CLI, or forced from the
# dashboard — takes this lock first, and a second one stands down rather
# than queueing.
PULSE_LOCK = db.RUNTIME_DIR / "pulse.lock"
# The scheduled task is killed at 30 minutes (schedule.py sets
# ExecutionTimeLimit), so a lock older than that belongs to a process that no
# longer exists and holding the heartbeat off for it would be worse than the
# collision it guards against.
PULSE_LOCK_STALE = timedelta(minutes=35)


class Busy(Refused):
    """Another pulse holds the lock. A refusal, and a 409, not an error."""


def pulse_running() -> bool:
    """True when a pulse holds the lock and the lock is not stale."""
    try:
        held = datetime.fromtimestamp(PULSE_LOCK.stat().st_mtime)
    except OSError:
        return False
    return datetime.now() - held < PULSE_LOCK_STALE


@contextlib.contextmanager
def pulse_lock():
    """Hold the one-pulse-at-a-time lock, or raise `Busy`.

    A plain file, created O_EXCL, rather than anything cleverer: the three
    callers are separate processes and the only thing they reliably share is
    the disk.
    """
    PULSE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if not pulse_running():
        with contextlib.suppress(OSError):
            PULSE_LOCK.unlink()
    try:
        fd = os.open(str(PULSE_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Busy("a pulse is already running " + chr(8212) +
                   " this one stood down rather than beat twice at once")
    try:
        os.write(fd, ("pid %d at %s\n" % (
            os.getpid(), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            PULSE_LOCK.unlink()


def force_pulse(conn: sqlite3.Connection, *, allow_wake: bool = True) -> dict[str, Any]:
    """Beat now, in the background, without moving the schedule.

    This is the one control that can spend tokens on its own — the wake it
    escalates to is the same wake the hourly beat would run. HALT and the
    allowance still get their say, exactly as they do at :07, so the button
    is not a way around either of them; it only asks the question sooner.

    A pulse takes seconds when it is clean and minutes when it wakes, which
    is far too long to hold an HTTP request open, so it runs on a thread with
    its own connection. What comes back is "started", not "finished" — the
    pulse log is where it finishes.
    """
    if pulse_running():
        raise Busy("a pulse is already running")
    _record(conn, "pulse", "colony", None,
            "forced a beat" + ("" if allow_wake else " (tick only, no wake)"))

    def beat() -> None:
        from . import pulse as pulse_mod   # local: pulse imports this module

        own = db.connect()
        try:
            pulse_mod.run(own, allow_wake=allow_wake, forced=True)
        except Busy:
            pass          # the scheduled task got there first; it will log its own row
        except Exception as exc:                      # noqa: BLE001 — a thread
            # has nowhere to raise to, and a forced beat that dies silently is
            # exactly the "pulses are not happening" complaint this control
            # exists to answer.
            _log_pulse_error(exc)
        finally:
            own.close()

    threading.Thread(target=beat, name="forced-pulse", daemon=True).start()
    return {"started": True, "wake": allow_wake}


def _log_pulse_error(exc: BaseException) -> None:
    """Into the same file the scheduled task writes, so there is one place to look."""
    import traceback

    path = db.RUNTIME_DIR / "pulse.log"
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write("\n%s  forced pulse failed\n" %
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            fh.write("".join(traceback.format_exception(exc)))


def _baseline(conn: sqlite3.Connection) -> float:
    """The share of the week the active sprint was designed around."""
    row = conn.execute(
        "SELECT budget_pct FROM sprints WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return float(row["budget_pct"]) if row else 35.0


def set_allowance(conn: sqlite3.Connection, boost_points: float) -> dict[str, Any]:
    """Move the colony's share of the weekly window off its designed baseline.

    The sprint's `budget_pct` is left alone and a *delta* is stored separately,
    so the baseline the colony was designed around stays visible next to the
    exception. Clearing is the same call with 0 — there is no separate reset
    path to forget about.

    The delta is signed and the only clamp left is the range of the thing being
    described: an allowance below 0% or above 100% of the week is not a number,
    it is a typo.
    """
    base = _baseline(conn)
    boost = max(MIN_ALLOWANCE_PCT - base,
                min(MAX_ALLOWANCE_PCT - base, float(boost_points)))
    eff = base + boost
    _record(conn, "allowance", "sprint", None,
            f"allowance {eff:g}% of the week ({base:g}% baseline {boost:+g})" if boost
            else f"allowance back to the {base:g}% baseline")
    set_control(conn, "allowance_boost", f"{boost:g}",
                "how far the PO has moved the allowance off the sprint baseline")
    return {"boost": boost, "base": base, "effective": eff}


def set_allowance_pct(conn: sqlite3.Connection, pct: float) -> dict[str, Any]:
    """Set the allowance to a number the PO typed, rather than to a step.

    Same store, same clamp — this exists because "I need 80% this week" is a
    thing you know directly, and reaching it by counting +5s is arithmetic the
    dashboard should be doing rather than asking for.
    """
    return set_allowance(conn, float(pct) - _baseline(conn))


def effective_allowance(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the budget guard should actually compare against, base + delta."""
    base = _baseline(conn)
    try:
        boost = float(get_control(conn, "allowance_boost", "0") or 0)
    except ValueError:
        boost = 0.0
    boost = max(MIN_ALLOWANCE_PCT - base, min(MAX_ALLOWANCE_PCT - base, boost))
    return {"base": base, "boost": boost, "effective": base + boost,
            "min": MIN_ALLOWANCE_PCT, "max": MAX_ALLOWANCE_PCT}


# ── the Inbox gate ────────────────────────────────────────────────────────────


def _close_escalation(conn: sqlite3.Connection, esc_id: int, decision: str) -> None:
    """Close a card. A dismissal closes it without answering it.

    `po_decision` stays NULL for a dismissal on purpose. It is the column that
    says which of the four answers the PO gave, and he gave none of them; every
    query that asks "what did he decide" would otherwise count a shrug. That is
    the same shape `settle` already uses for a filed story's questions, so
    `dismissed_at` is what separates the two moots.
    """
    if decision == "dismiss":
        conn.execute(
            "UPDATE escalations SET resolved_at = datetime('now','localtime'), "
            "dismissed_at = datetime('now','localtime') "
            "WHERE id = ? AND resolved_at IS NULL",
            (esc_id,),
        )
        return
    conn.execute(
        "UPDATE escalations SET resolved_at = datetime('now','localtime'), po_decision = ? "
        "WHERE id = ? AND resolved_at IS NULL",
        (decision, esc_id),
    )


# "approve" + "d" is "approved" and every other one of these is not. The four
# decisions were being past-tensed by appending a letter, which put "PO rejectd"
# and "PO amendd" on the timeline and, once decisions became tickets, into the
# permanent record of what Jordan actually said.
_PAST = {"approve": "approved", "reject": "rejected",
         "defer": "deferred", "amend": "amended", "dismiss": "dismissed"}


def clear_needs_info(conn: sqlite3.Connection, story_id: int) -> int:
    """
    Close the "cannot start yet" cards for a story that has started.

    A needs-info card is the story saying it is blocked, and the reply drawer
    reads it exactly that way: one open card of that kind paints the banner
    coral and says nothing can start until it is answered. Nothing closed
    those cards on the happy path — only the branch that raised a *new*
    blocker superseded the old one. So story #1 answered its blocker at
    09:07, was groomed, drafted criteria and reached `po-review`, and the
    drawer still told the PO it could not start, while what it was actually
    waiting for was his approval of the criteria sitting under that banner.

    Called from the two places a story stops being blocked: criteria drafted,
    and a PO reply that put it back in the groom queue.
    """
    return conn.execute(
        """UPDATE escalations
              SET resolved_at = datetime('now','localtime'), po_decision = 'amend'
            WHERE story_id = ? AND kind = 'needs-info' AND resolved_at IS NULL""",
        (story_id,),
    ).rowcount


def question_settled(conn: sqlite3.Connection, story_id: int, kind: str,
                     story_hash: str | None) -> bool:
    """Is this question already handled — either still open, or dismissed?

    The raise paths already refused to ask twice while a card was open. They had
    no way to know a card had been *dismissed*, so the next groom re-derived the
    same missing information and put the same words back on the page. Jordan
    dismissed a question about screenshots the day after the screenshots were
    made readable, and the loop would have asked again on the next tick.

    A dismissal is scoped to the version of the story it was made against, which
    is what `raised_hash` records. Edit the brief and the question is allowed
    back, because the PO dismissed a question about *that* text and this is no
    longer that text. Leave the brief alone and it stays gone.

    A story with no hash gets the conservative answer: a dismissal that cannot
    be scoped to a version is treated as permanent for that question, since the
    alternative is asking again immediately and that is the behaviour being
    fixed.
    """
    open_now = conn.execute(
        "SELECT 1 FROM escalations WHERE story_id = ? AND kind = ? "
        "AND resolved_at IS NULL AND stale_at IS NULL",
        (story_id, kind),
    ).fetchone()
    if open_now:
        return True
    return bool(conn.execute(
        "SELECT 1 FROM escalations WHERE story_id = ? AND kind = ? "
        "AND dismissed_at IS NOT NULL AND (raised_hash IS ? OR raised_hash IS NULL)",
        (story_id, kind, story_hash),
    ).fetchone())


def _asked(esc: sqlite3.Row) -> str:
    """The question as it stood when it was answered, recommendation and all."""
    text = f"[{esc['kind']}] {esc['reason']}"
    if esc["recommendation"]:
        text += f"\n\nOrdis recommended: {esc['recommendation']}"
    return text


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

    And one non-answer. `dismiss` is the Inbox's "x": the question stopped
    mattering, close it and change nothing. It exists because the four
    decisions above are all answers, and the only control that could actually
    clear a tile without answering was "drop story" — which takes the whole
    story off the board. Tidying the Inbox should not be the most destructive
    thing you can do in it.
    """
    if decision not in ("approve", "reject", "defer", "amend", "dismiss"):
        raise Refused(f"unknown decision {decision!r}")

    esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    if not esc:
        raise Refused("no such Inbox item")
    if esc["resolved_at"]:
        raise Refused("already decided")
    if decision == "dismiss" and esc["kind"] == "write-approval":
        # There is a real patch on disk and a worktree still checked out behind
        # it. Closing that question without answering it would strand both, and
        # the story would sit in `po-review` with nothing left to review it.
        raise Refused("a patch cannot be dismissed — apply it or reject it")
    if esc["kind"] == "run-request" and decision == "approve" and esc["ticket_id"]:
        # The command a build agent hands over usually verifies the code that
        # build just wrote, and that code is in a patch, not in the tree. Run it
        # first and it tests the version of the project that existed before the
        # work: `py test_local.py weekly` came back "0 passed, 0 failed", exit 0,
        # against a tree with no `weekly` command in it. That is a green result
        # for a test that never ran, recorded on the story as proof.
        #
        # So the patch goes first. The card is not closed and not dismissed; it
        # stays in the Inbox and becomes answerable the moment the patch lands
        # or is rejected.
        waiting = conn.execute(
            "SELECT id FROM escalations WHERE ticket_id = ? AND kind = 'write-approval' "
            "AND resolved_at IS NULL", (esc["ticket_id"],)).fetchone()
        if waiting:
            raise Refused(
                "the patch this command checks is still waiting — decide on the "
                "patch first, then run it against the real tree")

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
        deferred = f"snoozed {int(snooze_hours)}h" if snooze_hours else "back in the Inbox"
        _decision_ticket(
            conn, story_id=esc["story_id"], title=f"Deferred: {esc['reason'][:140]}",
            question=_asked(esc), answer=f"PO deferred — {deferred}."
                                        + (f"\n\n{note}" if note else ""),
            esc_id=esc_id)
        return {"ok": True, "kind": kind, "outcome": deferred}

    # Any decision other than "later" wakes the item back up, so an approved
    # item never carries a stale snooze into the audit trail.
    conn.execute("UPDATE escalations SET snoozed_until = NULL WHERE id = ?", (esc_id,))

    _close_escalation(conn, esc_id, decision)

    if decision == "dismiss":
        # Deliberately before every kind-specific branch, and deliberately doing
        # nothing to the story. A dismissal is a statement about the *question*,
        # not about the work: the story keeps its status, its criteria and its
        # place on the board, and the only thing that changes is that the colony
        # stops asking.
        #
        # It stops asking about this version of the story, not forever. The row
        # keeps its `raised_hash`, and the raise paths consult it, so a brief
        # that gets edited afterwards is allowed to raise the question again
        # — which is right, because by then the answer might have changed.
        # Silence bought by a dismissal is silence about one particular fact.
        if esc["story_id"]:
            _event(conn, esc["story_id"], "decided",
                   f"PO dismissed the question: {esc['reason'][:200]}",
                   note or "no longer relevant")
        _decision_ticket(
            conn, story_id=esc["story_id"], title=f"Dismissed: {esc['reason'][:140]}",
            question=_asked(esc),
            answer="PO dismissed this — the question stopped mattering. Nothing on the "
                   "story was changed." + (f"\n\n{note}" if note else ""),
            esc_id=esc_id)
        return {"ok": True, "kind": kind, "outcome": "dismissed"}

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
        # Sending it back is only real if the loop is still allowed to pick it
        # up. Without this the story returns to `needs-criteria` having already
        # spent both its attempts, and sits there permanently.
        regroom_budget(conn, esc["story_id"])
        _event(conn, esc["story_id"], "decided",
               "PO rejected the draft criteria — back for re-grooming", note or None)
        outcome = "sent back for re-grooming"
    elif kind == "brief-changed" and esc["story_id"] and decision == "approve":
        # The same move `decision`/reject makes, and for the same reason: the
        # criteria on this story describe a brief that no longer exists, and
        # clearing them is what puts a story back in the groom queue. The
        # attempt budget goes with it — a story groomed twice months ago must
        # not be permanently unreadable because the PO rewrote it today.
        conn.execute(
            "UPDATE stories SET status = 'needs-criteria', acceptance_criteria = NULL, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (esc["story_id"],),
        )
        regroom_budget(conn, esc["story_id"])
        _event(conn, esc["story_id"], "decided",
               "PO reopened the story — the brief changed after grooming, "
               "criteria cleared for a re-read", note or None)
        outcome = "reopened for grooming"
    elif kind == "brief-changed" and esc["story_id"] and decision == "reject":
        # Deliberately does nothing to the story. "That edit was cosmetic" is a
        # real answer, and it is recorded rather than acted on. The card will
        # not return for this version of the brief because `raised_hash` already
        # names it.
        _event(conn, esc["story_id"], "decided",
               "PO left the story as it stands — the edit did not change the work",
               note or None)
        outcome = "left as it stands"
    elif kind == "hire" and decision == "approve":
        proposal = json.loads(esc["proposal"] or "{}")
        hired = hire(conn, **proposal, _skip_record=True)
        outcome = f"hired as agent #{hired['agent_id']}"
    elif kind == "write-approval":
        outcome = _settle_patch(conn, esc, decision, note)
    elif kind == "run-request":
        outcome = _settle_run(conn, esc, decision, note)
    elif esc["story_id"]:
        _event(conn, esc["story_id"], "decided",
               f"PO {_PAST.get(decision, decision)}: {esc['reason'][:200]}",
               note or None)

    _decision_ticket(
        conn, story_id=esc["story_id"], title=f"Decision: {esc['reason'][:140]}",
        question=_asked(esc),
        answer=f"PO {_PAST.get(decision, decision)} — {outcome}."
                                    + (f"\n\n{note}" if note else ""),
        esc_id=esc_id)
    return {"ok": True, "outcome": outcome, "kind": kind}


def _settle_run(conn: sqlite3.Connection, esc: sqlite3.Row, decision: str,
                note: str) -> str:
    """Run the command a build agent asked for, or decline it.

    The output goes on the story as an event whether the command succeeded or
    not. A failing command is usually the answer the criterion wanted, and a
    story whose history records what happened is one the next agent does not
    have to ask about.

    The story goes back to `ready` after a run. It does not go to `accepted`
    and it does not move to any lane that reads as finished: the run answered a
    question, and deciding whether that finishes the story is Jordan's.
    """
    from . import runner

    proposal = json.loads(esc["proposal"] or "{}")
    story_id = esc["story_id"]
    command = proposal.get("command") or ""
    project = proposal.get("project") or ""

    if decision != "approve":
        if story_id:
            _event(conn, story_id, "decided",
                   f"PO declined to run `{command}`", note or None)
        return "not run"

    result = runner.execute(command, project)
    # What ran, not what was asked for. `execute` drops a leading `cd` into the
    # folder it was going to use anyway, and a story that records the version
    # with the `cd` still on it sends the next reader looking for a path error
    # that was never there.
    command = result["command"]
    expect = proposal.get("expect") or ""
    text = runner.transcript(result, expect)
    verdict, why = runner.judge(result)
    if story_id:
        # The headline says what the run is worth, not just what it returned.
        # An exit code alone let "Notion query failed (ConnectionError)" go into
        # the record as `exit 0`, and the next agent read that as the criterion
        # being answered.
        head = "ran `" + command + "` — "
        head += ("timed out" if result["timed_out"]
                 else "exit " + str(result["code"]))
        if verdict != "clean":
            head += " → " + why
        _event(conn, story_id, "finding" if verdict == "clean" else "blocked",
               head, text)
        # Back to the queue, not forward. The command answered something; what
        # that means for the story is a decision, and decisions are the PO's.
        #
        # A run that did not come back clean is the harder case, and the old
        # guard got it wrong: it skipped `accepted` stories, so "15 Part Job
        # Search" was told its run had failed and sat in DELIVERED anyway, with
        # the card's own reply claiming it had gone back to the queue. A story
        # whose verification failed is not delivered. It goes to `needs-info`
        # from any lane but `archived`, carrying the reason, which also hands it
        # to `pulse.ensure_blocked_visible` — that invariant runs every tick and
        # re-raises the card for any blocked story that has lost one, so this
        # cannot go quiet.
        if verdict == "clean":
            conn.execute(
                "UPDATE stories SET status = 'ready', "
                "updated_at = datetime('now','localtime') "
                "WHERE id = ? AND status NOT IN ('archived','accepted')", (story_id,))
        else:
            conn.execute(
                "UPDATE stories SET status = 'needs-info', blocked_reason = ?, "
                "updated_at = datetime('now','localtime') "
                "WHERE id = ? AND status <> 'archived'",
                (f"`{command}` was run to settle a criterion and {why}. "
                 "Read the run on the story, then either fix what it found and "
                 "dispatch again, or say the criterion no longer needs it.",
                 story_id))
            _raise_failed_run(conn, story_id, command, why)
    _record(conn, "run", "escalation", esc["id"],
            f"{command} — exit " + str(result["code"]))
    if result["timed_out"]:
        return f"gave up after {runner.TIMEOUT_S}s"
    if verdict != "clean":
        # Say the lane it is actually in. The previous wording named a lane the
        # SQL above had declined to move it to.
        return (f"ran it — {why}. Nothing is settled; the story is parked in "
                "NEEDS INFO with the run on it and a card in your Inbox.")
    return (f"ran it — exit {result['code']}, {len(result['out'])} characters of "
            "output. Nothing in it contradicts what the agent expected.")


def _raise_failed_run(conn: sqlite3.Connection, story_id: int,
                      command: str, why: str) -> None:
    """Put a failed verification in front of the PO now, not eventually.

    Jordan's rule, after watching a run fail into silence: an error like this is
    escalated immediately. So the card goes up in the same transaction that
    parks the story, and it is a `needs-info` card on purpose — that is the
    kind `pulse.ensure_blocked_visible` guarantees for a blocked story, so if
    anything closes this one without the story moving, the next tick puts it
    back.
    """
    story = conn.execute("SELECT title, notion_hash FROM stories WHERE id = ?",
                         (story_id,)).fetchone()
    if not story:
        return
    open_card = conn.execute(
        "SELECT id FROM escalations WHERE story_id = ? AND kind = 'needs-info' "
        "AND resolved_at IS NULL", (story_id,)).fetchone()
    if open_card:
        return
    conn.execute(
        """INSERT INTO escalations (story_id, kind, reason, recommendation, raised_hash)
           VALUES (?,'needs-info',?,?,?)""",
        (story_id,
         f'"{story["title"]}" is stuck: the command it was verified with failed.',
         card_text(
             f"$ {command}\n\n{why.capitalize()}.\n\n"
             "The whole transcript is on the story, under the run that produced "
             "it. Nothing was accepted on the strength of that run. Fix what it "
             "found and dispatch again, or answer here that the criterion no "
             "longer needs the command."),
         story["notion_hash"]))


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
    except worktree.PatchConflict as exc:
        # Not a refusal. The files are already in his working tree, some of them
        # with conflict markers in them, and the worktree stays until the merge
        # is finished so the patch can be read against its source. Saying so in
        # the story history matters more than usual: the tree changed under him
        # and the card is about to tell him the opposite.
        conn.execute("UPDATE escalations SET resolved_at = NULL, po_decision = NULL "
                     "WHERE id = ?", (esc["id"],))
        if story_id:
            _event(conn, story_id, "note",
                   f"patch applied with {len(exc.paths)} conflict(s) — needs your merge",
                   "The rest of the patch is in your working tree already. These "
                   "files have conflict markers in them:\n\n"
                   + "\n".join(exc.paths)
                   + "\n\nA conflict here means the patch and your own uncommitted "
                     "work changed the same lines. Resolve them, then press Apply "
                     "again to close this card.")
        raise Refused(str(exc))
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
        how = ""
        if not applied.get("staged", True):
            # Worth saying out loud. It means a file the patch touched already
            # had staged work of his own on it, so `git diff --cached` is not
            # the whole picture of what just landed.
            how = ("\n\nThis one went in unstaged: something the patch touches "
                   "was already staged with different content in your working "
                   "tree, so git would not let the patch near the index. "
                   "`git diff` shows what landed.")
        _event(conn, story_id, "accepted",
               f"PO approved the patch — {applied['files']} file(s) applied, uncommitted",
               "Review and commit it yourself; the colony does not commit." + how)
    tail = "" if applied.get("staged", True) else " and unstaged (you had staged work on it)"
    return f"{applied['files']} file(s) applied to your working tree, uncommitted" + tail


def clear_spent_groom_tickets(conn: sqlite3.Connection) -> int:
    """Retire blocked groom tickets on stories that have nothing left to ask.

    A groom that ends in a question leaves a `blocked` ticket behind as its
    receipt. The receipt is useful exactly as long as the question is open —
    after that it is a duplicate row in the Ticket Queue saying BLOCKED about
    something already answered, and, worse, it still counts against
    `wake.MAX_ATTEMPTS`, so answering the question is what stopped the story
    from ever being re-groomed. Two identical BLOCKED tiles for one story is the
    visible symptom; a story that can never move again is the actual cost.

    Housekeeping rather than a migration, so rows already in this state heal on
    the next beat instead of needing a schema step to reach them.
    """
    return conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE status = 'blocked' AND title LIKE 'Groom:%'
              AND story_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM escalations e
                               WHERE e.story_id = tickets.story_id
                                 AND e.resolved_at IS NULL)""",
    ).rowcount


def regroom_budget(conn: sqlite3.Connection, story_id: int) -> int:
    """Give one story its grooming attempts back.

    `wake.MAX_ATTEMPTS` counts groom tickets that are not `wontfix`, and that is
    the right rule while the tickets still describe the story as it stands. The
    moment the PO answers the question those runs were asking, they describe a
    version of the story that is gone — so retiring them is not tidying up, it
    is the difference between a story that can be re-groomed with the new
    information and one that is stuck at two attempts forever.
    """
    return conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE story_id = ? AND title LIKE 'Groom:%' AND status <> 'wontfix'""",
        (story_id,),
    ).rowcount


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
    # The groom runs that raised those questions were reading a story with no
    # confirmed folder. That story no longer exists, so their blocked tickets
    # stop being evidence and start being duplicates in the Ticket Queue.
    retired = regroom_budget(conn, story_id)
    _event(conn, story_id, "decided", f"PO confirmed the project folder: {note}")
    _decision_ticket(
        conn, story_id=story_id, title=f"Decision: which folder does “{story['title'][:90]}” live in?",
        question=f"The colony had this story as {story['project'] or 'unplaced'} "
                 f"({story['project_source'] or 'unset'}). An inferred folder can never "
                 f"authorise a write, so the answer is the gate.",
        answer=f"PO confirmed {note}.")
    return {"ok": True, "project": project, "note": note, "retired": retired,
            "message": f"project confirmed: {note}"
                       + (f" · {retired} spent groom ticket(s) retired" if retired else "")}


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


def settle_story(conn: sqlite3.Connection, story_id: int, settled_as: str,
                 notion_status: str) -> None:
    """File a story the PO has marked Done, Shipped, Shelved, New or Not started.

    Filing is not archiving: the story stays on the board's books, keeps its
    events, its spend and the workflow status it had, and comes straight back to
    life the moment the Notion status moves again. What it stops doing is
    *asking*. Every open question about it is closed as moot and every open
    ticket goes wontfix, because a question about a shipped story is not a
    question — it is the colony still holding a conversation the PO walked away
    from.

    The distinction that matters: an escalation resolved with `po_decision` set
    is a decision the wake will act on. A moot one is closed with no decision,
    so nothing downstream treats the filing as an instruction to do work.
    """
    conn.execute(
        """UPDATE stories SET settled_as = ?, blocked_reason = NULL,
                  updated_at = datetime('now','localtime')
            WHERE id = ?""",
        (settled_as, story_id),
    )
    conn.execute(
        """UPDATE escalations SET resolved_at = datetime('now','localtime'),
                  po_decision = NULL
            WHERE story_id = ? AND resolved_at IS NULL""",
        (story_id,),
    )
    conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE story_id = ? AND status IN ('open','staffed','blocked')""",
        (story_id,),
    )
    # An unanswered reply on a story that has just been filed is a conversation
    # its subject walked out of. Leaving it unread would have the next wake buy
    # an answer to it — and the ticket carrying it was wontfixed a line ago, so
    # the answer would arrive with nowhere on the page to land.
    conn.execute(
        "UPDATE po_messages SET status = 'read' "
        " WHERE story_id = ? AND author = 'po' AND status = 'unread'",
        (story_id,),
    )
    conn.execute(
        """INSERT INTO story_events (story_id, kind, summary, detail)
           VALUES (?, 'decided', ?, ?)""",
        (story_id, f"filed as {settled_as}",
         f'Notion status is "{notion_status}" — the colony stops asking about this one.'),
    )


def revive_story(conn: sqlite3.Connection, story_id: int, notion_status: str | None) -> None:
    """Take a story back off the shelf, exactly where it was left.

    Deliberately touches nothing but `settled_as`. The workflow status, the
    acceptance criteria and the confirmed project all survived being filed, and
    re-deriving any of them would spend a groom run answering questions that
    were answered before the story was parked.
    """
    conn.execute(
        "UPDATE stories SET settled_as = NULL, updated_at = datetime('now','localtime') "
        "WHERE id = ?", (story_id,)
    )
    _event(conn, story_id, "decided", "back on the board",
           f'Notion status is "{notion_status or "unset"}" again.')


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
                f"{status or '(nothing)'} is not on the board: "
                f"{', '.join(notion_mod.WRITABLE_STATUS)}"
            )
        detail = f"Status -> {status}"
        # Apply the filing here rather than waiting for Notion to say it back.
        #
        # The push is a mirror of a decision that has already been made: Jordan
        # pressed Done, and the story should stop asking at that instant, not up
        # to an hour later when the next sync happens to read the row. Waiting
        # on the round trip also makes the ledger hostage to the network — with
        # `notion_write` off, or the token read-only, the button would appear to
        # do nothing at all. The sync stays the authority on what Notion says;
        # this is the colony agreeing with an instruction it was given directly.
        settled = notion_mod.SETTLED_STATUS.get(status)
        conn.execute("UPDATE stories SET notion_status = ? WHERE id = ?", (status, story_id))
        if settled and story["settled_as"] != settled:
            settle_story(conn, story_id, settled, status)
        elif not settled and story["settled_as"]:
            revive_story(conn, story_id, status)
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
          story_id: int | None = None, body: str = "",
          attachments: list[dict] | None = None) -> dict[str, Any]:
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
    files = list(attachments or [])[:attach.MAX_PER_MESSAGE]
    # A screenshot on its own is a complete message — "look at this" is the
    # whole sentence, and demanding prose to go with it would make the feature
    # useless for the case it was asked for.
    if not body and not files:
        raise Refused("nothing to send")
    for f in files:
        attach.resolve(f.get("name", ""))   # it is on disk, and it is ours
    if len(body) > 8000:
        raise Refused("that is longer than a work order — trim it to 8,000 characters")

    if escalation_id:
        esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
        if not esc:
            raise Refused("no such Inbox item")
        story_id = story_id or esc["story_id"]

    cur = conn.execute(
        "INSERT INTO po_messages (escalation_id, story_id, author, body, attachments) "
        "VALUES (?,?,'po',?,?)",
        (escalation_id, story_id, body, json.dumps(files) if files else None),
    )
    message_id = int(cur.lastrowid)

    # The reply becomes a ticket now, not when the wake gets to it. `answer_po`
    # used to create, run and close its own ticket inside a single wake, so the
    # row was born staffed and died done between two page loads and the Queue
    # never showed it — from the PO's side, answering a question sent it
    # nowhere. Queueing it here is what the outbox does for a Notion push, for
    # the same reason: **the wait is the thing worth showing.**
    #
    # `role` is left for the wake to fill. Which tier answers is a budget
    # decision made at wake time against the ceiling that applies then, and a
    # role written down an hour early is a guess wearing a fact's clothes.
    # `work_order` holds what he actually said, so the ticket carries its own
    # context while it waits; the wake overwrites it with the full prompt.
    conn.execute(
        """INSERT INTO tickets (story_id, title, intent, status, work_order,
                                requires_po, po_message_id)
           VALUES (?,?,'research','open',?,0,?)""",
        (story_id,
         f"Reply to Ordis: {(body or files[0]['label'])[:120]}",
         body + ("\n\n[" + str(len(files)) + " attached]" if files else ""),
         message_id),
    )

    said = body + (("\nattached: " + ", ".join(f["label"] for f in files))
                          if files else "")
    _record(conn, "note", "escalation" if escalation_id else "story",
            escalation_id or story_id, said[:400])
    if story_id:
        _event(conn, story_id, "note", "PO wrote to Ordis about this", said)
    # A replied-to item stops shouting but stays open: waiting on an answer is
    # not the same as being answered.
    if escalation_id:
        conn.execute("UPDATE escalations SET snoozed_until = NULL WHERE id = ?", (escalation_id,))
    return {"ok": True, "message_id": message_id, "queued": True}


def thread(conn: sqlite3.Connection, escalation_id: int | None = None,
           story_id: int | None = None) -> list[dict[str, Any]]:
    """Every message in one conversation, oldest first.

    Keyed on the **story** whenever there is one, not on the escalation. This
    used to be the other way around and it was quietly deleting Jordan's
    history: Ordis closes a question when he believes his answer resolved it,
    the next groom raises a fresh escalation about the same story an hour
    later, and a thread scoped to `escalation_id` opens *empty* on the new one.
    Everything either of them had said stopped existing from the PO's side, at
    the exact moment he went looking for it.

    An escalation is an episode. The story is the thread. Two people talking
    about one piece of work are having one conversation, however many times the
    colony re-raises its hand.
    """
    if story_id is None and escalation_id:
        row = conn.execute("SELECT story_id FROM escalations WHERE id = ?",
                           (escalation_id,)).fetchone()
        story_id = row["story_id"] if row else None
    if story_id:
        rows = conn.execute(
            "SELECT * FROM po_messages WHERE story_id = ? ORDER BY id", (story_id,)
        ).fetchall()
    elif escalation_id:
        rows = conn.execute(
            "SELECT * FROM po_messages WHERE escalation_id = ? ORDER BY id", (escalation_id,)
        ).fetchall()
    else:
        rows = []
    return [dict(r) for r in rows]


def conversation(conn: sqlite3.Connection, escalation_id: int | None = None,
                 story_id: int | None = None) -> list[dict[str, Any]]:
    """The thread as a timeline, with the things that are not messages in it.

    Four different things happen in a conversation with the colony and only one
    of them was ever on screen. The PO writes. Ordis answers. The colony raises
    a question — which is the thing that *starts* most of these conversations
    and was invisible inside them, so a reply arrived with no sign of what it
    was replying to. And Ordis records a learning, which is the only durable
    output of the whole exchange and lived two clicks away in the story
    timeline.

    They are returned as one list because they happened in one order, and the
    order is most of the meaning. What the page does with them is give each a
    colour, so the shape of the conversation can be read before any of it is.
    """
    if story_id is None and escalation_id:
        row = conn.execute("SELECT story_id FROM escalations WHERE id = ?",
                           (escalation_id,)).fetchone()
        story_id = row["story_id"] if row else None

    out: list[dict[str, Any]] = []
    for m in thread(conn, escalation_id, story_id):
        out.append({
            "kind": "po" if m["author"] == "po" else "ordis",
            "at": m["at"], "id": m["id"], "body": m["body"],
            "status": m["status"], "escalation_id": m["escalation_id"],
            "attachments": m.get("attachments"), "tokens": m.get("tokens") or 0,
        })

    if story_id:
        for e in conn.execute(
            "SELECT id, kind, reason, raised_at, resolved_at, po_decision "
            "FROM escalations WHERE story_id = ? ORDER BY raised_at", (story_id,)
        ):
            out.append({
                "kind": "question", "at": e["raised_at"], "id": e["id"],
                "body": e["reason"], "esc_kind": e["kind"],
                "closed_at": e["resolved_at"], "decision": e["po_decision"],
            })
        for ev in conn.execute(
            "SELECT id, at, summary, detail FROM story_events "
            "WHERE story_id = ? AND kind = 'learning' ORDER BY at", (story_id,)
        ):
            out.append({"kind": "learning", "at": ev["at"], "id": ev["id"],
                        "body": ev["summary"], "detail": ev["detail"]})

    # A question raised in the same second as the message that answers it sorts
    # first: the colony asks, then someone replies, never the other way round.
    out.sort(key=lambda r: ((r["at"] or ""), 0 if r["kind"] == "question" else 1, r["id"]))
    return out


def unread_messages(conn: sqlite3.Connection, limit: int = 4) -> list[sqlite3.Row]:
    """What the PO has said that Ordis has not answered yet."""
    return conn.execute(
        "SELECT * FROM po_messages WHERE author = 'po' AND status = 'unread' "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


# ── staffing ──────────────────────────────────────────────────────────────────

# Same shape as `seed.READ_SCOPE`, and derived the same way: read the whole
# projects directory, write nothing without a per-ticket scope on top.
DEFAULT_READ_SCOPE = [f"{db.PROJECTS_ROOT.as_posix()}/**"]
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
WRITE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write"]

# A write scope is a list of project folders, written down as globs because
# that is the shape the contract has always had. It started as exactly one
# folder, derived from the project an agent was hired on, and that turned out
# to be too narrow the first time real work needed it: a story filed under
# `job-search/assisted-apply` had criteria about `job-search/job-radar`, and
# the build agent skipped half its list rather than write outside its scope.
# Widening it is a decision the PO makes per agent, so these two functions are
# the only place globs and folder names are converted into each other.

ROOT_POSIX = db.PROJECTS_ROOT.as_posix()


def scope_globs(projects: list[str]) -> list[str]:
    """Folder names to the glob form stored on the contract."""
    return [f"{ROOT_POSIX}/{p}/**" for p in projects]


def scope_projects(raw: Any) -> list[str]:
    """The stored contract back to plain folder names, in order, deduplicated.

    Takes the JSON text off the row or an already-parsed list, so callers do
    not each have to remember which one they are holding.
    """
    if not raw:
        return []
    globs = json.loads(raw) if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for g in globs:
        name = str(g)
        if name.startswith(ROOT_POSIX + "/"):
            name = name[len(ROOT_POSIX) + 1:]
        name = name.rstrip("*").rstrip("/")
        if name and name not in out:
            out.append(name)
    return out


def _check_scope_folder(name: str) -> str:
    """One folder the PO picked, or a refusal saying which rule it broke."""
    name = str(name).strip().replace("\\", "/").strip("/")
    if not name or name == ".":
        raise Refused("a write scope needs a folder — 'write anywhere' is not a scope")
    if ".." in name.split("/") or ":" in name:
        raise Refused(f"{name!r} is not a folder inside the projects directory")
    if name.split("/")[0].startswith("."):
        raise Refused(f"{name!r} is a dot folder — the colony never writes in one")
    if not (db.PROJECTS_ROOT / name).is_dir():
        raise Refused(f"there is no folder {name!r} under {ROOT_POSIX}")
    return name


def set_write_scope(conn: sqlite3.Connection, agent_id: int,
                    projects: list[str]) -> dict[str, Any]:
    """Change which folders one hired agent may write in.

    Only the folders change. The agent keeps the project it was hired on,
    because that is what `dispatch` matches a story against and what
    `build.contract` looks the contract up by — widening the scope is not the
    same as moving the agent to a different project.
    """
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        raise Refused("no such agent")
    if row["status"] == "retired":
        raise Refused(f"{row['role']} is retired — hire it again before changing its scope")
    if not row["write_capable"]:
        raise Refused(
            f"{row['role']} is read-only. A read-only contract has no write scope to widen; "
            f"hire it again with write ticked if it needs one."
        )

    folders: list[str] = []
    for p in projects or []:
        name = _check_scope_folder(p)
        if name not in folders:
            folders.append(name)
    if not folders:
        raise Refused("pick at least one folder — an empty scope would block every build")

    before = scope_projects(row["write_scope"])
    conn.execute("UPDATE agents SET write_scope = ? WHERE id = ?",
                 (json.dumps(scope_globs(folders)), agent_id))
    _record(conn, "scope", "agent", agent_id,
            f"{row['role']}: " + ", ".join(folders)
            + (f" (was {', '.join(before)})" if before and before != folders else ""))
    return {"ok": True, "role": row["role"], "write_scope": folders}


def set_secrets(conn: sqlite3.Connection, agent_id: int, on: bool) -> dict[str, Any]:
    """Decide whether this agent's checkout gets the credential files.

    A build agent works in a checkout of git's contents, and `.env` is ignored
    by git in every project here, so by default the file is simply absent. That
    default is right for most work and wrong for the rest: an agent asked to
    confirm the OG-tracker sync can read NOTION_API_KEY cannot tell an unset key
    from a file it was never shown, and the run that prompted this reported the
    second as the first.

    Turning it on copies the credential files in after the diff base is taken
    and removes them again before the diff, so a key cannot reach a patch. What
    it cannot do is stop an agent repeating a value in its report, which is why
    this is the PO's decision and not a default.
    """
    row = conn.execute("SELECT id, role, status FROM agents WHERE id = ?",
                       (agent_id,)).fetchone()
    if not row:
        raise Refused("no such agent")
    if row["status"] == "retired":
        raise Refused(f"{row['role']} is retired — hire it again first")
    on = bool(on)
    conn.execute("UPDATE agents SET sees_secrets = ? WHERE id = ?", (int(on), agent_id))
    _record(conn, "scope", "agent", agent_id,
            f"{row['role']}: credentials " + ("visible in its checkout" if on else "hidden"))
    return {"ok": True, "role": row["role"], "sees_secrets": on}



def propose_hire(conn: sqlite3.Connection, *, roster_slug: str, role: str,
                 project: str | None, reason: str, model: str = "claude-sonnet-5",
                 write_capable: bool = False, max_tokens_run: int = 400000,
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
         write_capable: bool = False, max_tokens_run: int = 400000,
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
    write_scope = scope_globs([project]) if write_capable else None
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

    # The bias counter, kept at the one place a persona actually gets picked.
    # "I do not want to only see one agent being chosen over and over again just
    # because we found one that works" is not enforceable by asking nicely in a
    # prompt — the thing producing the preference would be the thing policing it.
    # A number in the ledger can be put in front of the next selection and can be
    # checked afterwards, which is the difference between a rule and a wish.
    if roster_slug:
        conn.execute(
            "UPDATE roster SET times_hired = times_hired + 1, "
            "last_hired_at = datetime('now','localtime') WHERE slug = ?",
            (roster_slug,),
        )

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

    # The ticket records the agent's scope, not the story's folder. They are the
    # same until the PO widens one, and after that the ticket has to say which
    # of the two the build actually ran under.
    folders = scope_projects(agent["write_scope"]) or [story["project"]]
    _record(conn, "dispatch", "story", story_id, f"{story['project']} · {agent['role']}")
    cur = conn.execute(
        """INSERT INTO tickets (story_id, title, intent, role, status, write_scope,
                                requires_po, approved_at)
           VALUES (?,?,'implement',?,'staffed',?,1,datetime('now','localtime'))""",
        (story_id, f"Implement: {story['title']}"[:200], agent["role"],
         json.dumps(scope_globs(folders))),
    )
    ticket_id = cur.lastrowid
    conn.execute(
        "UPDATE stories SET status = 'in-progress', updated_at = datetime('now','localtime') "
        "WHERE id = ?", (story_id,)
    )
    _event(conn, story_id, "staffed",
           f"PO dispatched to {agent['role']} — worktree, write scope "
           + ", ".join(f"{f}/" for f in folders),
           f"ticket #{ticket_id}")
    _decision_ticket(
        conn, story_id=story_id, title=f"Decision: start building “{story['title'][:90]}”?",
        question="Criteria accepted, folder confirmed, writer hired. Dispatch is the "
                 "last gate before the colony opens a worktree and spends real tokens.",
        answer=f"PO dispatched to {agent['role']} on {story['project']}/ "
               f"— implement ticket #{ticket_id}.")
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
