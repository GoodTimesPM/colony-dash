"""PO actions: the only writes the dashboard may make.

Nothing else in `server.py` takes a write connection, so what the dashboard
can do to the colony is the list of public functions here. Two rules hold
for each:

  1. **The action is recorded before it takes effect.** `po_actions` gets a
     row first, in the same transaction, so every change has its reason next
     to it.
  2. **Nothing here spends tokens.** Approving a story marks it dispatchable;
     the next wake decides. A mis-click costs nothing and the budget guard
     still gets its say (ARCHITECTURE.md §4.4, §6.2).

HALT writes a file *and* a control row, because a file on disk cannot be
blocked by a locked database or an unresponsive server.
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

from . import attachments as attach, db, proc

HALT_FILE = db.RUNTIME_DIR / "HALT"

# How much of a card's own text is kept. Long enough for a full list of
# acceptance criteria; a card cut mid-list reads as the whole ask.
CARD_TEXT = 8000


def card_text(text: str) -> str:
    """A card's text in full, cut only if it is absurd, and visibly when it is."""
    text = str(text or "")
    if len(text) <= CARD_TEXT:
        return text
    cut = text[:CARD_TEXT]
    space = cut.rfind(" ")
    return (cut[:space] if space > CARD_TEXT * 0.9 else cut).rstrip() + "\u2026"

# The whole dial. `allowance_boost` is a signed delta from the sprint's
# baseline, so the baseline stays visible. The quota is shared with the PO's
# own sessions and they know what they need, so the range is 0 to 100. Zero
# stops spending without the finality of HALT.
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
    """Record a PO call as a ticket born `done`, with `decided_esc_id` set, so
    the decision shows in the Ticket Queue next to the work it shaped. No
    work query picks it up: they all select `intent = 'implement'` or an
    open status.
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

    The pulse keeps logging, syncing and reaping under HALT; it only refuses
    to spend, so the dashboard stays useful for diagnosis. A run already in
    flight is not killed. The promise is "no new work".
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


# ── forcing a beat ────────────────────────────────────────────────────────────
# The heartbeat is a scheduled task at :07 every hour. A forced beat does not
# move it, but two pulses at once would both sync, reap and maybe dispatch the
# same story. So every pulse takes this lock first, and a second stands down
# rather than queueing.
PULSE_LOCK = db.RUNTIME_DIR / "pulse.lock"
# The scheduled task is killed at 30 minutes (schedule.py), so an older lock
# belongs to a dead process.
PULSE_LOCK_STALE = timedelta(minutes=35)


class Busy(Refused):
    """Another pulse holds the lock. A refusal, and a 409, not an error."""


def _lock_pid() -> int | None:
    """The pid written into the pulse lock, or None when it cannot be read."""
    try:
        first = PULSE_LOCK.read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(first) >= 2 and first[0] == "pid" and first[1].isdigit():
        return int(first[1])
    return None


def pulse_running() -> bool:
    """True when a pulse holds the lock, its process is alive, and it is not
    stale. The pid lets a crashed pulse's lock be detected at once.
    """
    try:
        held = datetime.fromtimestamp(PULSE_LOCK.stat().st_mtime)
    except OSError:
        return False
    if datetime.now() - held >= PULSE_LOCK_STALE:
        return False
    pid = _lock_pid()
    return pid is None or proc.alive(pid)


@contextlib.contextmanager
def pulse_lock():
    """Hold the one-pulse-at-a-time lock, or raise `Busy`. A plain O_EXCL file,
    because the callers are separate processes and share only the disk.
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

    The only control that can spend on its own, through the same wake as the
    hourly beat; HALT and the allowance still apply. A pulse can take
    minutes, so it runs on a thread with its own connection and returns
    "started". The pulse log records the finish.
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
        except Exception as exc:                      # noqa: BLE001 (a thread
            # has nowhere to raise to, so the failure goes to the pulse log).
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
    """Move the colony's share of the weekly window off its baseline. The delta
    is stored apart from `budget_pct`; clearing is the same call with 0.
    Clamped only to 0 to 100% of the week.
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
    """Set the allowance to a typed number rather than a step."""
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

    `po_decision` stays NULL for a dismissal: it records which of the four
    answers the PO gave, and they gave none. `dismissed_at` separates this
    from a filed story's moot questions.
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


# Past tense for the timeline; appending "d" gave "rejectd".
_PAST = {"approve": "approved", "reject": "rejected",
         "defer": "deferred", "amend": "amended", "dismiss": "dismissed"}


def clear_needs_info(conn: sqlite3.Connection, story_id: int) -> int:
    """Close the "cannot start yet" cards for a story that has started. An open
    needs-info card paints the reply drawer's blocked banner, so it must
    close when the story moves on. Called when criteria are drafted, and
    when a PO reply puts the story back in the groom queue.
    """
    return conn.execute(
        """UPDATE escalations
              SET resolved_at = datetime('now','localtime'), po_decision = 'amend'
            WHERE story_id = ? AND kind = 'needs-info' AND resolved_at IS NULL""",
        (story_id,),
    ).rowcount


def question_settled(conn: sqlite3.Connection, story_id: int, kind: str,
                     story_hash: str | None) -> bool:
    """Is this question already handled, either still open or dismissed?

    A dismissal is scoped to the brief version in `raised_hash`: edit the
    brief and the question may return. A story with no hash treats a
    dismissal as permanent, since the alternative is asking again
    immediately.
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

    What an approval means lives here, not in the UI, so no other surface
    can invent a different mapping:

      decision   → criteria accepted; the story becomes `ready`, the only state
                   dispatch staffs from.
      needs-info → dismissed; the story stays blocked until the answer arrives.
      hire       → the proposed contract is written into `agents`.
      write-     → the patch is applied uncommitted for the PO to review and
      approval     commit. Rejecting removes the worktree, returns the story to
                   `ready`, and keeps the patch file.
      cost       → acknowledged; a receipt, not a control.

    `dismiss` is the Inbox's "x": close the question and change nothing.
    """
    if decision not in ("approve", "reject", "defer", "amend", "dismiss"):
        raise Refused(f"unknown decision {decision!r}")

    esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    if not esc:
        raise Refused("no such Inbox item")
    if esc["resolved_at"]:
        raise Refused("already decided")
    if decision == "dismiss" and esc["kind"] == "write-approval":
        # A real patch and worktree sit behind this card; dismissing it would
        # strand both.
        raise Refused("a patch cannot be dismissed. Apply it or reject it")
    if esc["kind"] == "run-request" and decision == "approve" and esc["ticket_id"]:
        # A handed-over command usually verifies code that is still in the
        # unapplied patch; run first, it tests the old tree and reports a false
        # pass. So the patch goes first, and this card stays open until the
        # patch lands or is rejected.
        waiting = conn.execute(
            "SELECT id FROM escalations WHERE ticket_id = ? AND kind = 'write-approval' "
            "AND resolved_at IS NULL", (esc["ticket_id"],)).fetchone()
        if waiting:
            raise Refused(
                "the patch this command checks is still waiting. Decide on the "
                "patch first, then run it against the real tree")

    _record(conn, decision, "escalation", esc_id, note or esc["reason"][:400])
    kind = esc["kind"]
    outcome = decision

    if decision == "defer":
        # Deferring keeps the item open and visible, but `snoozed_until` grays
        # it out and sorts it last until the snooze ends.
        conn.execute(
            "UPDATE escalations SET raised_at = datetime('now','localtime'), "
            "snoozed_until = datetime('now','localtime', ?) WHERE id = ?",
            (f"+{float(snooze_hours):g} hours", esc_id),
        )
        deferred = f"snoozed {float(snooze_hours):g}h" if snooze_hours else "back in the Inbox"
        _decision_ticket(
            conn, story_id=esc["story_id"], title=f"Deferred: {esc['reason'][:140]}",
            question=_asked(esc), answer=f"PO deferred, {deferred}."
                                        + (f"\n\n{note}" if note else ""),
            esc_id=esc_id)
        return {"ok": True, "kind": kind, "outcome": deferred}

    # Any decision other than "later" wakes the item back up, so an approved
    # item never carries a stale snooze into the audit trail.
    conn.execute("UPDATE escalations SET snoozed_until = NULL WHERE id = ?", (esc_id,))

    _close_escalation(conn, esc_id, decision)

    if decision == "dismiss":
        # Before every kind-specific branch, and changing nothing on the story.
        # It stops the colony asking about this version only: `raised_hash`
        # lets an edited brief raise the question again.
        if esc["story_id"]:
            _event(conn, esc["story_id"], "decided",
                   f"PO dismissed the question: {esc['reason'][:200]}",
                   note or "no longer relevant")
        _decision_ticket(
            conn, story_id=esc["story_id"], title=f"Dismissed: {esc['reason'][:140]}",
            question=_asked(esc),
            answer="PO dismissed this. The question stopped mattering. Nothing on the "
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
               "PO accepted the acceptance criteria. Story is ready to staff", note or None)
        outcome = "story is ready"
    elif kind == "decision" and esc["story_id"] and decision == "reject":
        conn.execute(
            "UPDATE stories SET status = 'needs-criteria', acceptance_criteria = NULL, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (esc["story_id"],),
        )
        # Otherwise the story returns with its attempts spent and never
        # regrooms.
        regroom_budget(conn, esc["story_id"])
        _event(conn, esc["story_id"], "decided",
               "PO rejected the draft criteria. Back for re-grooming", note or None)
        outcome = "sent back for re-grooming"
    elif kind == "brief-changed" and esc["story_id"] and decision == "approve":
        # Same as `decision`/reject: the criteria describe a brief that no
        # longer exists, and clearing them (and the attempt budget) requeues
        # the groom.
        conn.execute(
            "UPDATE stories SET status = 'needs-criteria', acceptance_criteria = NULL, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (esc["story_id"],),
        )
        regroom_budget(conn, esc["story_id"])
        _event(conn, esc["story_id"], "decided",
               "PO reopened the story. The brief changed after grooming, "
               "criteria cleared for a re-read", note or None)
        outcome = "reopened for grooming"
    elif kind == "brief-changed" and esc["story_id"] and decision == "reject":
        # Does nothing to the story. "That edit was cosmetic" is recorded, and
        # `raised_hash` keeps the card from returning for this brief.
        _event(conn, esc["story_id"], "decided",
               "PO left the story as it stands. The edit did not change the work",
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
        answer=f"PO {_PAST.get(decision, decision)}. {outcome}."
                                    + (f"\n\n{note}" if note else ""),
        esc_id=esc_id)
    out = {"ok": True, "outcome": outcome, "kind": kind}
    if kind == "run-request" and decision == "approve":
        # The command runs after this transaction commits. See `_settle_run`.
        out["run_pending"] = esc_id
    return out


def _settle_run(conn: sqlite3.Connection, esc: sqlite3.Row, decision: str,
                note: str) -> str:
    """Decline the command a build agent asked for, or check it and queue it.

    An approved command does not run here: it could hold the write lock for
    `runner.TIMEOUT_S`. The caller runs it after commit with `run_command`
    and writes back with `record_run`. It is checked here so a refused
    command keeps its card open.
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

    try:
        runner.resolve_cd(runner.check(command), runner.check_folder(project))
    except runner.RunRefused as exc:
        raise Refused(str(exc))
    return "queued to run"


def run_command(conn: sqlite3.Connection, esc_id: int) -> dict:
    """Execute an approved run-request. Takes no lock; call it outside a transaction."""
    from . import runner

    esc = conn.execute("SELECT proposal FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    proposal = json.loads((esc and esc["proposal"]) or "{}")
    try:
        return runner.execute(proposal.get("command") or "", proposal.get("project") or "")
    except runner.RunRefused as exc:
        return {"ok": False, "code": None, "command": proposal.get("command") or "",
                "cwd": "", "out": "", "err": str(exc), "timed_out": False}


def record_run(conn: sqlite3.Connection, esc_id: int, result: dict) -> str:
    """Write an executed run-request's result onto its story, pass or fail. A
    clean run goes back to `ready`, never to a finished lane; that call is
    the PO's.
    """
    from . import runner

    esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    proposal = json.loads(esc["proposal"] or "{}")
    story_id = esc["story_id"]
    # What ran, not what was asked: `execute` drops a leading `cd`.
    command = result["command"]
    expect = proposal.get("expect") or ""
    text = runner.transcript(result, expect)
    verdict, why = runner.judge(result)
    if story_id:
        # The headline says what the run is worth, since an exit code alone can
        # hide a failure the output reports.
        head = "ran `" + command + "`. "
        head += ("timed out" if result["timed_out"]
                 else "exit " + str(result["code"]))
        if verdict != "clean":
            head += " → " + why
        _event(conn, story_id, "finding" if verdict == "clean" else "blocked",
               head, text)
        # A clean run returns to the queue. A run that did not come back clean
        # moves the story to `needs-info` from any lane but `archived`, with
        # the reason, and `pulse.ensure_blocked_visible` keeps its card up.
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
            f"{command}. Exit " + str(result["code"]))
    if result["timed_out"]:
        return f"gave up after {runner.TIMEOUT_S}s"
    if verdict != "clean":
        # Say the lane it is actually in. The previous wording named a lane the
        # SQL above had declined to move it to.
        return (f"ran it. {why}. Nothing is settled; the story is parked in "
                "NEEDS INFO with the run on it and a card in your Inbox.")
    return (f"ran it. Exit {result['code']}, {len(result['out'])} characters of "
            "output. Nothing in it contradicts what the agent expected.")


def _raise_failed_run(conn: sqlite3.Connection, story_id: int,
                      command: str, why: str) -> None:
    """Put a failed verification in front of the PO now. The card goes up in
    the same transaction that parks the story, and as `needs-info`, the kind
    `pulse.ensure_blocked_visible` re-raises if it closes early.
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


def _write_scope(conn: sqlite3.Connection, ticket_id: Any) -> list[str] | None:
    """The ticket's agent's current write scope, read at apply time, so
    widening the contract lets a refused patch through on the next Apply.
    """
    row = conn.execute(
        "SELECT a.write_scope, s.project FROM tickets t "
        "JOIN stories s ON s.id = t.story_id "
        "JOIN agents a ON a.role = t.role AND a.project = s.project "
        "AND a.status != 'retired' WHERE t.id = ?", (ticket_id,)).fetchone()
    if not row:
        return None
    return scope_projects(row["write_scope"]) or [row["project"]]


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
                   "PO rejected the patch. Nothing was applied", note or None)
        return "patch discarded, nothing applied"

    try:
        applied = worktree.apply_patch(int(ticket_id), scope=_write_scope(conn, ticket_id))
    except worktree.OutOfScope as exc:
        conn.execute("UPDATE escalations SET resolved_at = NULL, po_decision = NULL "
                     "WHERE id = ?", (esc["id"],))
        raise Refused(str(exc))
    except worktree.PatchConflict as exc:
        # Not a refusal: the files are in the tree, some with conflict markers.
        # The worktree stays until the merge is finished, and the story history
        # says so.
        conn.execute("UPDATE escalations SET resolved_at = NULL, po_decision = NULL "
                     "WHERE id = ?", (esc["id"],))
        if story_id:
            _event(conn, story_id, "note",
                   f"patch applied with {len(exc.paths)} conflict(s). Needs your merge",
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
            # A touched file already had staged work, so `git diff --cached` is
            # not the whole picture.
            how = ("\n\nThis one went in unstaged: something the patch touches "
                   "was already staged with different content in your working "
                   "tree, so git would not let the patch near the index. "
                   "`git diff` shows what landed.")
        _event(conn, story_id, "accepted",
               f"PO approved the patch. {applied['files']} file(s) applied, uncommitted",
               "Review and commit it yourself; the colony does not commit." + how)
    tail = "" if applied.get("staged", True) else " and unstaged (you had staged work on it)"
    return f"{applied['files']} file(s) applied to your working tree, uncommitted" + tail


def clear_spent_groom_tickets(conn: sqlite3.Connection) -> int:
    """Retire blocked groom tickets on stories with nothing left to ask. Once
    the question is answered they are duplicates in the Queue and still
    count against `wake.MAX_ATTEMPTS`. Housekeeping, not a migration, so old
    rows heal on the next beat.
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
    """Give one story its grooming attempts back. Once the PO answers what
    those runs asked, they describe a version of the story that is gone.
    """
    return conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE story_id = ? AND title LIKE 'Groom:%' AND status <> 'wontfix'""",
        (story_id,),
    ).rowcount


def confirm_project(conn: sqlite3.Connection, story_id: int, project: str) -> dict[str, Any]:
    """Name the folder a story belongs to.

    `project_source` becomes 'confirmed', and only a confirmed project can
    become a write scope (§8.2). Every open escalation asking this question
    closes.
    """
    project = (project or "").strip().replace("\\", "/").strip("/")
    if not project:
        raise Refused("name a folder, or say it is a new project")

    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")

    known = set(_project_dirs())
    if project not in known:
        # A folder that does not exist yet is a valid answer, recorded as new.
        note = f"{project} (does not exist yet. New project)"
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
    # The groom runs that asked lacked a confirmed folder; their tickets are
    # now duplicates.
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
# Dropping is local and reversible: the story goes to 'archived' with a reason,
# its open questions close as 'reject', and the Notion change is offered, not
# assumed.


def drop_story(conn: sqlite3.Connection, story_id: int, *, reason: str = "",
               notion_status: str | None = None) -> dict[str, Any]:
    """Take a story off the board. Reversible; `restore_story` is the undo."""
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")
    if story["dropped_at"]:
        raise Refused("already dropped")
    if story["status"] == "in-progress":
        # A running ticket owns a worktree and budget; cancel it first.
        raise Refused("a ticket is running on this story. Cancel it first")

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
    """Undo a drop: back to the backlog, ungroomed. Criteria are cleared, since
    they were drafted against the old reading of the story.
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
    """File a story the PO has marked Done, Shipped, Shelved, New or Not
    started.

    Filing is not archiving: the story keeps its events, spend and workflow
    status, and returns when the Notion status moves. It stops asking. Open
    questions close as moot (no `po_decision`, so the wake does not act on
    them) and open tickets go wontfix.
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
    # A queued reply on a filed story would buy an answer with nowhere to land.
    conn.execute(
        "UPDATE po_messages SET status = 'read' "
        " WHERE story_id = ? AND author = 'po' AND status = 'unread'",
        (story_id,),
    )
    conn.execute(
        """INSERT INTO story_events (story_id, kind, summary, detail)
           VALUES (?, 'decided', ?, ?)""",
        (story_id, f"filed as {settled_as}",
         f'Notion status is "{notion_status}". The colony stops asking about this one.'),
    )


def revive_story(conn: sqlite3.Connection, story_id: int, notion_status: str | None) -> None:
    """Take a story back off the shelf. Only `settled_as` changes; its status,
    criteria and project survived filing.
    """
    conn.execute(
        "UPDATE stories SET settled_as = NULL, updated_at = datetime('now','localtime') "
        "WHERE id = ?", (story_id,)
    )
    _event(conn, story_id, "decided", "back on the board",
           f'Notion status is "{notion_status or "unset"}" again.')


def queue_notion(conn: sqlite3.Connection, *, story_id: int, kind: str,
                 payload: dict, record: bool = True) -> int:
    """Queue one upward write; the tick does the HTTP (see outbox.py)."""
    from . import notion as notion_mod
    from . import outbox

    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")
    if not story["notion_page_id"]:
        raise Refused("this story was written by the loop. It has no Notion page")

    if kind == "status":
        status = (payload or {}).get("status")
        if status not in notion_mod.WRITABLE_STATUS:
            raise Refused(
                f"{status or '(nothing)'} is not on the board: "
                f"{', '.join(notion_mod.WRITABLE_STATUS)}"
            )
        detail = f"Status -> {status}"
        # Apply the filing now rather than waiting for Notion to echo it: the
        # PO's decision is made, and with `notion_write` off the button would
        # otherwise do nothing. The sync remains the authority on what Notion
        # says.
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
    """The kill switch for upward writes. Off holds the queue rather than
    dropping it.
    """
    _record(conn, "note", "colony", None, f"notion_write -> {'on' if on else 'off'}")
    set_control(conn, "notion_write", "1" if on else "0",
                "the colony may push status, comments and checkboxes back to Notion")
    return {"ok": True, "on": bool(on),
            "message": "Notion writes on" if on else "Notion writes held. Nothing is lost"}


def reask(conn: sqlite3.Connection, esc_id: int) -> dict[str, Any]:
    """Send a stale question back to Ordis instead of answering it. Resolves as
    'amend' and requeues the groom so the next wake reads the story as it is
    now.
    """
    esc = conn.execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    if not esc:
        raise Refused("no such escalation")
    if esc["resolved_at"]:
        raise Refused("already decided")
    if not esc["story_id"]:
        raise Refused("nothing to re-read. This question is not about a story")

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
           "PO sent this back to Ordis, the brief changed after the question was written")
    return {"ok": True, "message": "back in the groom queue, Ordis re-reads it on the next wake"}


def rescope_story(conn: sqlite3.Connection, story_id: int, scope: str) -> str:
    """The PO changed what the work is: rewrite the job, not the history.

    Build agents read acceptance criteria as the job, so after a change of
    scope the old criteria would rebuild the old job. They are cleared and
    the story regrooms against `scope`, along with its open questions and
    undecided drafts. Nothing on disk is touched; the next groom reads the
    tree as it is.
    """
    scope = (scope or "").strip()
    if not scope:
        raise Refused("a rescope has to say what the work is now")
    _record(conn, "note", "story", story_id, f"rescoped: {scope[:200]}")
    conn.execute(
        """UPDATE stories SET status = 'needs-criteria', acceptance_criteria = NULL,
                  blocked_reason = NULL, updated_at = datetime('now','localtime')
            WHERE id = ?""",
        (story_id,),
    )
    # Draft criteria and questions about the old job are moot.
    closed = conn.execute(
        """UPDATE escalations SET resolved_at = datetime('now','localtime'),
                  po_decision = 'amend'
            WHERE story_id = ? AND resolved_at IS NULL
              AND kind IN ('decision','needs-info','run-request')""",
        (story_id,),
    ).rowcount
    # Unstarted build tickets only. A running one owns a worktree; it finishes,
    # and the next dispatch uses the new criteria.
    parked = conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE story_id = ? AND status = 'open'""",
        (story_id,),
    ).rowcount
    # The old grooms answered a question about a story that no longer exists,
    # so they must not count against the attempt ceiling for the new one.
    regroom_budget(conn, story_id)
    _event(conn, story_id, "decided",
           "The PO changed what this story is for. The criteria were written "
           "against the old scope and have been cleared; the next wake writes "
           "new ones from the line below. Nothing already built was undone.",
           scope)
    return ("rescoped " + chr(0x2192) + " criteria cleared, back in the groom queue"
            + (f" · {closed} question(s) closed" if closed else "")
            + (f" · {parked} unstarted ticket(s) parked" if parked else ""))


def _project_dirs() -> list[str]:
    from . import projects
    return projects.project_dirs()


SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


def _today(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT date('now','localtime') AS d").fetchone()["d"]


def create_project(conn: sqlite3.Connection, name: str, *, why: str = "") -> dict[str, Any]:
    r"""Make a new project folder with the PROJECT.md the repo convention
    requires, for the answer "none of them yet" to "which folder?".

    The one dashboard write outside the ledger: `mkdir` plus a stub. The
    name is checked per segment against a whitelist, which fails closed, and
    limited to two levels, the only shape `project_dirs()` reports.
    """
    name = (name or "").strip().replace("\\", "/").strip("/")
    if not name:
        raise Refused("give the new project a folder name")

    parts = [p.strip() for p in name.split("/") if p.strip()]
    if len(parts) > 2:
        raise Refused("projects live one or two levels under the root, not deeper")
    for part in parts:
        if not SAFE_SEGMENT.match(part):
            raise Refused(f"{part!r} is not a folder name I will create. Letters, "
                          "digits, spaces, dot, dash and underscore only")

    rel = "/".join(parts)
    path = db.PROJECTS_ROOT.joinpath(*parts)
    # A second, independent check against the root.
    if db.PROJECTS_ROOT.resolve() not in path.resolve().parents:
        raise Refused("that path is outside the projects root")

    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    stub = path / "PROJECT.md"
    if not stub.exists():
        stub.write_text(
            f"# {parts[-1]}\n\n"
            f"**Status:** new. Folder created from the Colony Dash Inbox on "
            f"{_today(conn)}.\n\n"
            f"{(why or 'No brief yet.').strip()}\n\n"
            "## Next\n\n- Say what this project is for.\n",
            encoding="utf-8",
        )
    _record(conn, "confirm-project", "story", None,
            f"created project folder {rel}" + ("" if not existed else " (already existed)"))
    return {"ok": True, "project": rel, "path": str(path), "created": not existed}


# ── filing work without Notion ────────────────────────────────────────────────
# In-house intake beside Notion. A story filed here has a NULL
# `notion_page_id`; the sync only touches rows it can match to a page, and
# neither path deletes the other's rows.

MAX_TITLE = 200
MAX_BRIEF = 20_000

# Covers a phone's double tap; short enough that filing a title twice on
# purpose still works.
DUPLICATE_WINDOW_S = 60


def create_story(conn: sqlite3.Connection, *, title: str, description: str = "",
                 project: str = "", priority: int = 3) -> dict[str, Any]:
    """File a story straight into the ledger.

    A named folder is **confirmed**, not inferred: the PO typed it, the same
    act `confirm_project` records (§8.2). No folder raises the same
    needs-info card the sync would. The story lands in `backlog`.
    """
    title = " ".join((title or "").split())[:MAX_TITLE]
    if not title:
        raise Refused("a story needs a title")

    description = (description or "").strip()[:MAX_BRIEF]

    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 3
    if priority not in (1, 2, 3):
        raise Refused("priority is 1 (high), 2 (medium) or 3 (low)")

    project = (project or "").strip().replace("\\", "/").strip("/")
    if project and project not in set(_project_dirs()):
        # Unlike `confirm_project`, an unknown folder is refused: here a typo
        # would become the confirmed write scope with nothing left to catch it.
        raise Refused(f"no folder named {project!r} under the projects root. "
                      "create the project first, or leave it blank and answer in the Inbox")

    dupe = conn.execute(
        """SELECT id FROM stories
            WHERE title = ? AND dropped_at IS NULL
              AND created_at >= datetime('now','localtime',?)""",
        (title, f"-{DUPLICATE_WINDOW_S} seconds"),
    ).fetchone()
    if dupe:
        raise Refused(f"just filed that one. Story #{dupe['id']}")

    cur = conn.execute(
        """INSERT INTO stories (notion_page_id, title, description, project,
                                project_source, priority, status)
           VALUES (NULL, ?, ?, ?, ?, ?, 'backlog')""",
        (title, description or None, project or None,
         "confirmed" if project else "inferred", priority),
    )
    story_id = cur.lastrowid

    _record(conn, "story", "story", story_id, title)
    _event(conn, story_id, "created", "filed by the PO in the dashboard",
           description or None)

    if not project:
        conn.execute(
            """INSERT INTO escalations (story_id, kind, reason, recommendation)
               VALUES (?, 'needs-info', ?, ?)""",
            (story_id,
             f'"{title}". Filed here with no project folder named.',
             f"Name the folder under {db.PROJECTS_ROOT.as_posix()}, or say it is "
             "a new project."),
        )

    return {"ok": True, "story_id": story_id, "project": project or None,
            "message": f"filed story #{story_id}"
                       + (f" · {project}" if project else " · needs a folder")}


# ── talking back ──────────────────────────────────────────────────────────────


def reply(conn: sqlite3.Connection, *, escalation_id: int | None = None,
          story_id: int | None = None, body: str = "",
          attachments: list[dict] | None = None) -> dict[str, Any]:
    """Queue a message to Ordis about one Inbox item. Spends nothing; the next
    wake answers it in the same thread (`wake.answer_po`). The escalation
    stays **open**: a reply is not a decision.
    """
    body = (body or "").strip()
    files = list(attachments or [])[:attach.MAX_PER_MESSAGE]
    # A screenshot alone is a complete message.
    if not body and not files:
        raise Refused("nothing to send")
    for f in files:
        attach.resolve(f.get("name", ""))   # it is on disk, and it is ours
    if len(body) > 8000:
        raise Refused("that is longer than a work order. Trim it to 8,000 characters")

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

    # The reply becomes a queued ticket now, so the Queue shows the wait.
    # `role` is left for the wake, which picks the tier against the ceiling at
    # that time. `work_order` holds the message until the wake writes the full
    # prompt.
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

    Keyed on the story when there is one, not the escalation: a re-raised
    card would otherwise open with an empty thread. The story is the thread;
    an escalation is an episode.
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
    """The thread as one ordered timeline: PO messages, Ordis replies, the
    questions the colony raised, and the learnings Ordis recorded. The page
    colours each kind.
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

# A write scope is a list of project folders stored as globs. The PO can widen
# it per agent; these two functions are the only place globs and folder names
# convert.

ROOT_POSIX = db.PROJECTS_ROOT.as_posix()


def scope_globs(projects: list[str]) -> list[str]:
    """Folder names to the glob form stored on the contract."""
    return [f"{ROOT_POSIX}/{p}/**" for p in projects]


def scope_projects(raw: Any) -> list[str]:
    """The stored contract as plain folder names, ordered and deduplicated.
    Takes the JSON text or a parsed list.
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
        raise Refused("a write scope needs a folder. 'write anywhere' is not a scope")
    if ".." in name.split("/") or ":" in name:
        raise Refused(f"{name!r} is not a folder inside the projects directory")
    if name.split("/")[0].startswith("."):
        raise Refused(f"{name!r} is a dot folder. The colony never writes in one")
    if not (db.PROJECTS_ROOT / name).is_dir():
        raise Refused(f"there is no folder {name!r} under {ROOT_POSIX}")
    return name


def set_write_scope(conn: sqlite3.Connection, agent_id: int,
                    projects: list[str]) -> dict[str, Any]:
    """Change which folders one hired agent may write in. The agent keeps its
    hired project, which `dispatch` and `build.contract` match on.
    """
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        raise Refused("no such agent")
    if row["status"] == "retired":
        raise Refused(f"{row['role']} is retired. Hire it again before changing its scope")
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
        raise Refused("pick at least one folder. An empty scope would block every build")

    before = scope_projects(row["write_scope"])
    conn.execute("UPDATE agents SET write_scope = ? WHERE id = ?",
                 (json.dumps(scope_globs(folders)), agent_id))
    _record(conn, "scope", "agent", agent_id,
            f"{row['role']}: " + ", ".join(folders)
            + (f" (was {', '.join(before)})" if before and before != folders else ""))
    return {"ok": True, "role": row["role"], "write_scope": folders}


def set_secrets(conn: sqlite3.Connection, agent_id: int, on: bool) -> dict[str, Any]:
    """Decide whether this agent's checkout gets the credential files.

    `.env` is git-ignored, so a checkout lacks it by default, and an agent
    cannot tell an unset key from an unseen file. When on, the files are
    copied in after the diff base and removed before the diff, so no key
    reaches a patch. An agent can still repeat a value in its report, which
    is why this is the PO's call.
    """
    row = conn.execute("SELECT id, role, status FROM agents WHERE id = ?",
                       (agent_id,)).fetchone()
    if not row:
        raise Refused("no such agent")
    if row["status"] == "retired":
        raise Refused(f"{row['role']} is retired. Hire it again first")
    on = bool(on)
    conn.execute("UPDATE agents SET sees_secrets = ? WHERE id = ?", (int(on), agent_id))
    _record(conn, "scope", "agent", agent_id,
            f"{row['role']}: credentials " + ("visible in its checkout" if on else "hidden"))
    return {"ok": True, "role": row["role"], "sees_secrets": on}



def team(conn: sqlite3.Connection, story_id: int) -> list[sqlite3.Row]:
    """Everyone hired to write on one story, lead first.

    Agents hired for this story come first, lowest seat as lead; agents with
    no story (hired by hand) sort last. An agent hired for a different story
    on the same project is excluded. The project must still match, so a
    moved story does not carry its old contract.
    """
    story = conn.execute("SELECT project FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story or not story["project"]:
        return []
    return list(conn.execute(
        """SELECT * FROM agents
            WHERE project = ? AND write_capable = 1 AND status != 'retired'
              AND (story_id = ? OR story_id IS NULL)
            ORDER BY (story_id IS NULL), seat, id""",
        (story["project"], story_id),
    ))


def propose_hire(conn: sqlite3.Connection, *, roster_slug: str, role: str,
                 project: str | None, reason: str, model: str = "claude-sonnet-5",
                 write_capable: bool = False, max_tokens_run: int = 400000,
                 story_id: int | None = None, seat: int = 0) -> int:
    """Raise a hire for approval. Hires nothing.

    The proposal is stored on the escalation, since the roster can change
    between the two clicks. `story_id` is in the proposal too: it is the
    exact argument set `hire` gets. Older proposals without it hire
    project-scoped.
    """
    persona = conn.execute("SELECT * FROM roster WHERE slug = ?", (roster_slug,)).fetchone()
    if not persona:
        raise Refused(f"no persona {roster_slug!r} in the roster")

    proposal = {
        "roster_slug": roster_slug, "role": role, "project": project, "model": model,
        "write_capable": write_capable, "max_tokens_run": max_tokens_run,
        "story_id": story_id, "seat": int(seat),
    }
    seat_note = "" if not seat else f" (seat {seat})"
    cur = conn.execute(
        """INSERT INTO escalations (story_id, kind, reason, recommendation, proposal, est_tokens)
           VALUES (?, 'hire', ?, ?, ?, ?)""",
        (story_id,
         f"Hire {persona['name']} as {role}" + (f" on {project}" if project else "")
         + seat_note + "?",
         reason, json.dumps(proposal), max_tokens_run),
    )
    return cur.lastrowid


def hire(conn: sqlite3.Connection, *, roster_slug: str | None, role: str,
         project: str | None = None, model: str = "claude-sonnet-5",
         write_capable: bool = False, max_tokens_run: int = 400000,
         notes: str | None = None, story_id: int | None = None, seat: int = 0,
         _skip_record: bool = False) -> int:
    """Turn a persona into an agent with a contract.

    The persona says how to think; the contract says what may be touched
    (§2.1). Nothing is inherited from the persona file. A write-capable
    contract without a project is refused. `story_id` and `seat` place the
    agent on a team: seat 0 is the lead and gets the implement ticket. Both
    are optional.
    """
    if write_capable and not project:
        raise Refused("a write-capable contract needs a project. 'write anywhere' is not a scope")

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
                      hired_from='dashboard', story_id=?, seat=?
                WHERE id = ?""",
            (roster_slug, model, int(write_capable), json.dumps(tools),
             json.dumps(DEFAULT_READ_SCOPE), json.dumps(write_scope) if write_scope else None,
             max_tokens_run, notes, story_id, int(seat), existing["id"]),
        )
        agent_id = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO agents (role, project, roster_slug, model, write_capable,
                                   tools_allowed, read_scope, write_scope, max_tokens_run,
                                   avatar_seed, status, notes, hired_from, story_id, seat)
               VALUES (?,?,?,?,?,?,?,?,?,?,'standby',?, 'dashboard',?,?)""",
            (role, project, roster_slug, model, int(write_capable), json.dumps(tools),
             json.dumps(DEFAULT_READ_SCOPE), json.dumps(write_scope) if write_scope else None,
             max_tokens_run, seed, notes, story_id, int(seat)),
        )
        agent_id = cur.lastrowid

    # The bias counter, kept where a persona is picked, so the next selection
    # can see it and it can be checked afterwards.
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
            "write_capable": bool(write_capable), "story_id": story_id, "seat": int(seat)}


def retire(conn: sqlite3.Connection, agent_id: int) -> dict[str, Any]:
    """Take an agent off the books. History stays. Runs still point at the role."""
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        raise Refused("no such agent")
    if row["project"] is None:
        raise Refused(
            f"{row['role']} is structural. The colony needs it to groom and review. "
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
    """Queue a story for write-capable work. Spends nothing.

    Each refusal names which condition failed:

      * the story is `ready` (the PO accepted its criteria);
      * its project is *confirmed*, not inferred (§8.2);
      * an agent is hired with write scope on that project;
      * the colony is not halted.

    This creates a ticket, not a run; the budget guard can still refuse at
    wake.
    """
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        raise Refused("no such story")
    if is_halted():
        raise Refused("the colony is halted. Resume dispatch first")
    if story["status"] != "ready":
        raise Refused(
            f"story is {story['status']}, not ready. Accept its acceptance criteria "
            f"in the Inbox first. That gate is what makes a story dispatchable."
        )
    if not story["project"] or story["project_source"] != "confirmed":
        raise Refused(
            "this story's project folder is still a guess. Confirm it first. An "
            "inference cannot authorise a write."
        )

    crew = team(conn, story_id)
    agent = crew[0] if crew else None
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

    # The ticket records the agent's scope, which may be wider than the story's
    # folder.
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
           f"PO dispatched to {agent['role']}. Worktree, write scope "
           + ", ".join(f"{f}/" for f in folders),
           f"ticket #{ticket_id}")
    _decision_ticket(
        conn, story_id=story_id, title=f"Decision: start building “{story['title'][:90]}”?",
        question="Criteria accepted, folder confirmed, writer hired. Dispatch is the "
                 "last gate before the colony opens a worktree and spends real tokens.",
        answer=f"PO dispatched to {agent['role']} on {story['project']}/, "
               f"implement ticket #{ticket_id}.")
    return {"ok": True, "ticket_id": ticket_id, "role": agent["role"]}


def cancel_ticket(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any]:
    """Pull a queued ticket back before it runs."""
    t = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not t:
        raise Refused("no such ticket")
    if t["status"] == "running":
        raise Refused("that ticket is already running. Halt the colony to stop new work; "
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
# Asking for a draft is free (the next wake pays), promoting writes a file, and
# retiring removes a skill. Detection runs free on the pulse.


def request_draft(conn: sqlite3.Connection, skill_id: int) -> dict[str, Any]:
    """Ask Ordis to write this candidate up. Queued, not spent. See forge.py."""
    row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if not row:
        raise Refused("no such skill")
    if row["status"] != "candidate":
        raise Refused(f"that skill is already {row['status']}. Only a candidate can be drafted")
    if row["draft_requested_at"]:
        raise Refused("already queued; the next wake will draft it")

    _record(conn, "draft-skill", "skill", skill_id, row["name"])
    conn.execute("UPDATE skills SET draft_requested_at = datetime('now','localtime') WHERE id = ?",
                 (skill_id,))
    return {"ok": True, "queued": True, "slug": row["slug"],
            "outcome": "Ordis drafts it on the next wake"}


def promote_skill(conn: sqlite3.Connection, skill_id: int,
                  roles: list[str] | None = None) -> dict[str, Any]:
    """Put a drafted skill on disk and attach it to the roles that load it.

    The file is written last, so a failed write rolls the transaction back
    without leaving a `skills` row pointing nowhere. A written file whose
    commit failed is inert until something attaches it.
    """
    from . import forge

    row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if not row:
        raise Refused("no such skill")
    if row["status"] != "drafted":
        raise Refused(f"that skill is {row['status']}. Only a drafted skill can be promoted. "
                      "Ask for a draft first.")
    if not (row["draft_md"] or "").strip():
        raise Refused("the draft is empty. There is nothing to promote")

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
    """Retire a skill. The file stays and the row stays, as evidence of which
    detectors propose skills that do not work.
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

    return {"ok": True, "slug": row["slug"], "outcome": f"retired. {reason}"}


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
