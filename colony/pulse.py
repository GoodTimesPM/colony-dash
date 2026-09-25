"""The heartbeat, in two tiers so an idle hour is free (ARCHITECTURE.md §4).

    tick   pure Python, no model, ZERO tokens. Runs every hour, always.
    wake   Ordis actually runs. Only when the tick found something.

Every path writes a `pulses` row; a quiet hour writes `finding = 'clean'`
with `tokens = 0`, and a missing row is the alarm. Each row's `detail` is
the long account of what the tick looked at, including project folders that
moved.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import (control, db, forge as forge_mod, notion, outbox,
               projects as projects_mod, usage as usage_mod)
from .mirror import load_env

HALT_FILE = db.RUNTIME_DIR / "HALT"
# Aliases for names that moved to `usage.py`; tests and the CLI still use them.
USAGE_CACHE = usage_mod.CACHE
USAGE_STALE_AFTER = usage_mod.STALE_AFTER

PULSE_INTERVAL = timedelta(hours=1)
# How long a run may sit in 'running' before the next pulse declares it orphaned.
STALE_RUN_AFTER = timedelta(minutes=20)

# Folders the colony may be pointed at. Read is the whole tree; write is always
# one of these, per ticket, after approval. ARCHITECTURE.md §8.1.
PROJECTS_ROOT = db.PROJECTS_ROOT


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── tick steps ────────────────────────────────────────────────────────────────


def check_halt() -> bool:
    """A `.colony/HALT` file stops dispatch. Pulses keep logging. That's the point."""
    return HALT_FILE.exists()


def sample_usage(conn: sqlite3.Connection) -> dict | None:
    """Copy the tray app's last good read into `usage_samples`, one row an
    hour.

    The usage endpoint allows ~5 requests per 5 minutes, shared with the
    CLI, so the tray app is the only poller. The dashboard's live figure
    reads the cache directly via `usage.read()`.
    """
    sample = usage_mod.read()
    if sample is None:
        return None

    conn.execute(
        """
        INSERT INTO usage_samples (five_hour_pct, seven_day_pct, seven_day_resets_at, source_mtime)
        VALUES (?, ?, ?, ?)
        """,
        (
            sample["five_hour"],
            sample["seven_day"],
            # Stored as the local instant, not the cache's UTC string.
            sample["seven_day_resets"].strftime("%Y-%m-%d %H:%M:%S")
            if sample["seven_day_resets"] else None,
            sample["mtime"].strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return sample


def align_sprint(conn: sqlite3.Connection) -> dict | None:
    """Keep the active sprint on the allowance week, and roll it when that
    turns.

    The window comes from the reset instant the API reports. Before it
    turns, the sprint's edges are aligned to it; after, the sprint closes
    and a new one opens with no goal carried over, since a new week is a new
    decision.
    """
    start, end = usage_mod.current_window()
    sprint = conn.execute(
        "SELECT * FROM sprints WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not sprint:
        return None

    fmt = "%Y-%m-%d %H:%M:%S"
    if sprint["ends_at"] and sprint["ends_at"] <= now():
        conn.execute("UPDATE sprints SET status = 'closed' WHERE id = ?", (sprint["id"],))
        cur = conn.execute(
            """INSERT INTO sprints (name, goal, starts_on, ends_on, starts_at, ends_at,
                                    budget_pct, status)
               VALUES (?, NULL, ?, ?, ?, ?, ?, 'active')""",
            (f"Sprint {sprint['id'] + 1}", start.date().isoformat(), end.date().isoformat(),
             start.strftime(fmt), end.strftime(fmt), sprint["budget_pct"]),
        )
        control._record(conn, "note", "sprint", cur.lastrowid,
                        f"{sprint['name']} closed; Sprint {sprint['id'] + 1} opens "
                        f"{start.strftime(fmt)} and runs to {end.strftime(fmt)}")
        return {"action": "rolled", "closed": sprint["id"], "opened": cur.lastrowid,
                "starts_at": start.strftime(fmt), "ends_at": end.strftime(fmt)}

    if sprint["starts_at"] == start.strftime(fmt) and sprint["ends_at"] == end.strftime(fmt):
        return None

    conn.execute(
        """UPDATE sprints SET starts_on = ?, ends_on = ?, starts_at = ?, ends_at = ?
            WHERE id = ?""",
        (start.date().isoformat(), end.date().isoformat(),
         start.strftime(fmt), end.strftime(fmt), sprint["id"]),
    )
    return {"action": "aligned", "sprint": sprint["id"],
            "starts_at": start.strftime(fmt), "ends_at": end.strftime(fmt)}


def candidate_projects() -> list[str]:
    """Every folder with a `PROJECT.md`, one and two levels deep (the master
    CLAUDE.md's definition). Two levels because containers like `job-search`
    hold the real projects. Any other directory walk matches words like
    `build`.
    """
    if not PROJECTS_ROOT.is_dir():
        return []

    folders: list[str] = []
    for top in sorted(PROJECTS_ROOT.iterdir()):
        if not top.is_dir() or top.name.startswith((".", "_")):
            continue
        folders.append(top.name)
        for child in sorted(top.iterdir()):
            if child.is_dir() and (child / "PROJECT.md").is_file():
                folders.append(f"{top.name}/{child.name}")
    return folders


def infer_project(title: str, description: str | None) -> tuple[str | None, bool]:
    """Guess a story's folder. Returns (project, confident). The board has no
    Project field by design, so the guess is confirmed in the Inbox before
    any write scope depends on it.
    """
    if not PROJECTS_ROOT.is_dir():
        return None, False

    folders = candidate_projects()

    def norm(text: str) -> str:
        """Flatten punctuation so 'Job Radar:' and 'job-radar' are the same string."""
        return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "

    haystack = norm(f"{title} {description or ''}")

    def slug_of(path: str) -> str:
        return path.rsplit("/", 1)[-1]

    # A leaf name appearing as a phrase is confident; longest first. A word
    # overlap is only a candidate.
    for path in sorted(folders, key=lambda p: len(slug_of(p)), reverse=True):
        if norm(slug_of(path)) in haystack:
            return path, True
    for path in sorted(folders, key=lambda p: p.count("/"), reverse=True):
        # Every word of the name must appear, so "search" alone cannot match
        # `job-search`. Single-word names were caught above.
        words = [w for w in norm(slug_of(path)).split() if len(w) >= 3]
        if len(words) < 2:
            continue
        if all(f" {w} " in haystack for w in words):
            return path, False
    return None, False


def ensure_blocked_visible(conn: sqlite3.Connection) -> int:
    """Re-raise the Inbox card for any `needs-info` story that has lost one.

    Nothing grooms a blocked story, so without a card it is invisible for
    good. Six paths can close a card; this enforces the invariant every tick
    for free.
    """
    rows = conn.execute(
        """SELECT id, title, blocked_reason, project, project_source, notion_hash
             FROM stories
            WHERE status = 'needs-info' AND dropped_at IS NULL AND settled_as IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM escalations e
                   WHERE e.story_id = stories.id AND e.resolved_at IS NULL
              )
              -- ...and not one the PO dismissed against this exact brief. This
              -- invariant is what would otherwise make dismissal meaningless:
              -- close the card, and the very next tick notices a blocked story
              -- with no card and puts it back. Edit the story in Notion and the
              -- hash moves, the dismissal stops matching, and the card returns,
              -- which is the behaviour you want, because the thing it was
              -- dismissed about has changed.
              AND NOT EXISTS (
                  SELECT 1 FROM escalations e
                   WHERE e.story_id = stories.id AND e.dismissed_at IS NOT NULL
                     AND (e.raised_hash IS stories.notion_hash OR e.raised_hash IS NULL)
              )"""
    ).fetchall()
    for r in rows:
        # No recorded reason: the only thing left to ask is which folder.
        ask = (r["blocked_reason"] or "").strip() or (
            "This is parked and no longer says why. Confirm the project folder "
            "it belongs to, or drop it, and the colony will re-read it from scratch."
        )
        conn.execute(
            """INSERT INTO escalations (story_id, kind, reason, recommendation, raised_hash)
               VALUES (?,'needs-info',?,?,?)""",
            (r["id"], f'"{r["title"]}" cannot start yet.', control.card_text(ask), r["notion_hash"]),
        )
    return len(rows)


# Stories past grooming whose brief changed. Re-grooming on every typo fix
# would be expensive, so the tick asks the PO, once per brief version via
# `raised_hash`.
BRIEF_CHANGED_WHERE = """
    id = :story_id
    AND dropped_at IS NULL
    AND settled_as IS NULL
    -- Not the delivered case. `resume_delivered` has that one, and it does not
    -- ask: on a running project more scope is the norm, not an event.
    AND status <> 'accepted'
    -- The colony has a considered view of this story already. That is the whole
    -- precondition: there is something for the change to invalidate.
    AND acceptance_criteria IS NOT NULL AND acceptance_criteria <> ''
    -- Not while a question about it is already open. The PO is looking at a
    -- card about this story; a second one asking whether the first is still
    -- current is noise.
    AND NOT EXISTS (
        SELECT 1 FROM escalations e
         WHERE e.story_id = stories.id AND e.resolved_at IS NULL
           AND e.dismissed_at IS NULL
    )
    -- And not one already asked, or dismissed, about this exact brief. The hash
    -- moves when the prose does, so the next real edit is allowed to ask again.
    AND NOT EXISTS (
        SELECT 1 FROM escalations e
         WHERE e.story_id = stories.id AND e.kind = 'brief-changed'
           AND e.raised_hash IS stories.notion_hash
    )
"""


# An accepted story whose Notion brief has grown. `accepted` means one batch
# landed, not that the project is finished, so new brief text on an In Progress
# row is the next batch. This clears the criteria and attempt budget so the
# next wake regrooms the whole brief.
#
# Guards: the Notion row must still be In Progress, and `notion_hash` must have
# moved (the caller passes only changed ids). It never writes to Notion and
# never moves a story into a finished lane.
RESUME_DELIVERED_WHERE = """
    id = :story_id
    AND status = 'accepted'
    AND dropped_at IS NULL
    AND settled_as IS NULL
    AND notion_status = :workable
"""


def resume_delivered(conn: sqlite3.Connection, story_ids: list[int]) -> list[str]:
    """Requeue delivered stories whose brief grew. Free; the regroom happens on
    the next wake if the budget allows. Returns the titles for the log.
    """
    from . import control

    resumed = []
    for story_id in story_ids:
        row = conn.execute(
            f"SELECT id, title FROM stories WHERE {RESUME_DELIVERED_WHERE}",
            {"story_id": story_id, "workable": notion.WORKABLE_STATUS},
        ).fetchone()
        if not row:
            continue
        conn.execute(
            "UPDATE stories SET status = 'needs-criteria', acceptance_criteria = NULL, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (row["id"],),
        )
        control.regroom_budget(conn, row["id"])
        conn.execute(
            """INSERT INTO story_events (story_id, kind, summary, detail)
               VALUES (?, 'note', ?, ?)""",
            (row["id"],
             "brief grew after delivery. Back in the groom queue",
             "The last batch was delivered and the Notion row still says "
             "In Progress, so this edit is more scope on a running project "
             "rather than a new one. The criteria written against the older "
             "brief are cleared; the next wake re-reads the whole page.\n\n"
             "Nothing already built was touched. File the row as Done or "
             "Shelved in Notion when you want this to stop."),
        )
        resumed.append(row["title"])
    return resumed


def brief_changed(conn: sqlite3.Connection, story_ids: list[int]) -> list[str]:
    """Ask about stories edited after the colony finished reading them. Free.
    Returns the titles for the log.
    """
    asked = []
    for story_id in story_ids:
        row = conn.execute(
            f"SELECT id, title, status, notion_hash FROM stories WHERE {BRIEF_CHANGED_WHERE}",
            {"story_id": story_id},
        ).fetchone()
        if not row:
            continue
        conn.execute(
            """INSERT INTO escalations (story_id, kind, reason, recommendation, raised_hash)
               VALUES (?,'brief-changed',?,?,?)""",
            (
                row["id"],
                f'You edited "{row["title"]}" after the colony finished reading it.',
                "Its acceptance criteria were written against the older version of "
                "the brief, so nothing in the loop will pick this edit up on its "
                "own. A story with criteria is not in the groom queue.\n\n"
                "Reopen it if the edit is new scope: the criteria are cleared and "
                "the next wake re-reads the whole brief from Notion. Leave it if "
                "you were tidying prose. Either way this is asked once per version, "
                "edit the page again and it comes back.",
                row["notion_hash"],
            ),
        )
        asked.append(row["title"])
    return asked


def stale_escalations(conn: sqlite3.Connection, story_id: int, new_hash: str) -> int:
    """Flag every open question asked about an older version of a story.

    Cards are prose from one moment, and one that keeps asking for something
    already done trains the PO to stop reading the Inbox. Flagged, not
    deleted: it sorts last, says the brief moved, and clearing
    `acceptance_criteria` requeues the groom for free.
    """
    marked = conn.execute(
        """UPDATE escalations
              SET stale_at = datetime('now','localtime')
            WHERE story_id = ? AND resolved_at IS NULL AND stale_at IS NULL
              AND kind IN ('needs-info','decision')
              AND (raised_hash IS NULL OR raised_hash <> ?)""",
        (story_id, new_hash),
    ).rowcount
    if not marked:
        return 0
    # Un-park `needs-info`: the reason it was parked has just changed.
    conn.execute(
        """UPDATE stories SET status = 'backlog', acceptance_criteria = NULL
            WHERE id = ? AND status = 'needs-info'""",
        (story_id,),
    )
    # Restore its grooming attempts; those runs answered an older brief.
    conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE story_id = ? AND title LIKE 'Groom:%' AND status <> 'wontfix'""",
        (story_id,),
    )
    return marked


def fetch_board_rows() -> tuple[list[dict] | None, dict]:
    """The network half of `sync_notion`: the rows, or None and what went wrong."""
    load_env()
    try:
        return notion.fetch_board(), {}
    except notion.NotionUnconfigured as exc:
        return None, {"configured": False, "error": str(exc)}
    except Exception as exc:  # network, auth, schema drift: reportable, not fatal
        return None, {"error": f"{type(exc).__name__}: {exc}"}


def sync_notion(conn: sqlite3.Connection,
                fetched: tuple[list[dict] | None, dict] | None = None) -> dict:
    """Upsert the board into `stories` and return what changed. `fetched` is
    read before the transaction opens, so no request holds the write lock.
    """
    result = {"configured": True, "seen": 0, "new": [], "changed": [], "error": None,
              "staled": 0, "ticked": [], "filed": [], "revived": [], "changed_ids": []}
    rows, problem = fetched if fetched is not None else fetch_board_rows()
    if rows is None:
        return {**result, **problem}

    existing = {
        r["notion_page_id"]: r
        for r in conn.execute(
            "SELECT notion_page_id, notion_hash, id, project, done_items, settled_as "
            "FROM stories "
            "WHERE notion_page_id IS NOT NULL"
        )
    }
    result["seen"] = len(rows)

    for row in rows:
        prev = existing.get(row["notion_page_id"])
        if prev and prev["notion_hash"] == row["hash"]:
            conn.execute(
                "UPDATE stories SET notion_synced_at = ? WHERE id = ?", (now(), prev["id"])
            )
            continue

        project, confident = infer_project(row["title"], row["description"])
        # In Progress lands in backlog for grooming, Exploring is
        # research-only, and only the PO marks ready. Everything else is filed.
        status = notion.ledger_status(row["notion_status"])
        settled = notion.SETTLED_STATUS.get(row["notion_status"])

        if prev:
            # Body columns only when a body was read, so a filed row's
            # placeholder never overwrites its brief.
            cols = ["title=?", "notion_status=?", "category=?", "related_link=?",
                    "priority=?", "notion_hash=?", "notion_synced_at=?", "updated_at=?"]
            vals = [row["title"], row["notion_status"], row["category"],
                    row["related_link"], row["priority"], row["hash"], now(), now()]
            if row.get("body_fetched", True):
                cols += ["description=?", "done_items=?", "open_items=?"]
                vals += [row["description"], row["done_items"], row["open_items"]]
            conn.execute(
                f"UPDATE stories SET {', '.join(cols)} WHERE id = ?", (*vals, prev["id"])
            )
            story_id = prev["id"]
            result["changed"].append(row["title"])
            # Ids as well as titles: `brief_changed` needs to look these rows up
            # in the ledger, and two stories are allowed to share a name.
            result["changed_ids"].append(story_id)
            kind, summary = "synced", "Notion row changed"

            # The sync may change filing but never the colony's own `status`.
            was_filed = prev["settled_as"]
            if settled:
                if was_filed != settled:
                    control.settle_story(conn, story_id, settled, row["notion_status"])
                    result["filed"].append(row["title"])
            elif was_filed:
                # Un-filing restores the story as it was, without a new groom.
                control.revive_story(conn, story_id, row["notion_status"])
                result["revived"].append(row["title"])

            # Boxes ticked in Notion since the last sync. Worth naming in the
            # log on their own: "the PO finished three things" is the single
            # most useful sentence an idle hour can produce.
            was = set(json.loads(prev["done_items"] or "[]"))
            newly = ([i for i in json.loads(row["done_items"] or "[]") if i not in was]
                     if row.get("body_fetched", True) else [])
            if newly:
                result["ticked"].extend(newly)
                conn.execute(
                    """INSERT INTO story_events (story_id, kind, summary, detail)
                       VALUES (?, 'note', ?, ?)""",
                    (story_id, f"{len(newly)} item(s) ticked off in Notion",
                     "\n".join(f"- [x] {i}" for i in newly)),
                )

            result["staled"] += stale_escalations(conn, story_id, row["hash"])
        elif settled:
            # A row first seen already filed is not news; importing it would
            # raise a "which folder?" card for an idea nobody started.
            continue
        else:
            cur = conn.execute(
                """
                INSERT INTO stories (notion_page_id, title, description, notion_status,
                                     category, related_link, priority, project,
                                     project_source, status, settled_as, notion_hash,
                                     notion_synced_at, done_items, open_items)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (row["notion_page_id"], row["title"], row["description"], row["notion_status"],
                 row["category"], row["related_link"], row["priority"], project,
                 "inferred", status, settled, row["hash"], now(),
                 row["done_items"], row["open_items"]),
            )
            story_id = cur.lastrowid
            result["new"].append(row["title"])
            kind, summary = "created", f"picked up from Notion ({row['notion_status']})"

            if project and not confident:
                conn.execute(
                    """
                    INSERT INTO escalations (story_id, kind, reason, recommendation, raised_hash)
                    VALUES (?, 'decision', ?, ?, ?)
                    """,
                    (story_id,
                     f'"{row["title"]}". I think this belongs to {project}/, but I am guessing.',
                     f"Confirm {project}/ so write scope is unambiguous before any ticket is staffed.",
                     row["hash"]),
                )
            elif not project:
                conn.execute(
                    """
                    INSERT INTO escalations (story_id, kind, reason, recommendation, raised_hash)
                    VALUES (?, 'needs-info', ?, ?, ?)
                    """,
                    (story_id,
                     f'"{row["title"]}". I cannot tell which project folder this belongs to.',
                     f"Name the folder under {db.PROJECTS_ROOT}, or say it is a new project.",
                     row["hash"]),
                )

        conn.execute(
            "INSERT INTO story_events (story_id, kind, summary, detail) VALUES (?,?,?,?)",
            (story_id, kind, summary,
             row["description"] if row.get("body_fetched", True) else None),
        )

    return result


KINDS = ("added", "modified", "deleted", "untracked")


def _colony_writes(conn: sqlite3.Connection, start: str, end: str) -> set[str]:
    """Project folders the colony itself wrote to in this window, via approved
    patches. Usually empty, which tells the log reader that other movement
    was not the colony.
    """
    return {
        r["project"] for r in conn.execute(
            """SELECT DISTINCT s.project AS project
                 FROM escalations e JOIN stories s ON s.id = e.story_id
                WHERE e.kind = 'write-approval' AND e.po_decision = 'approve'
                  AND e.resolved_at > ? AND e.resolved_at <= ?
                  AND s.project IS NOT NULL""",
            (start, end),
        )
    }


def _moved_since_last(conn: sqlite3.Connection, rows: list[dict],
                      start: str = "", end: str = "") -> list[dict]:
    """Only projects whose state differs from the last sample: a changed file
    count or a commit in the window. First sightings count. Each row carries
    how it differs, computed here while the previous sample is at hand.
    """
    writers = _colony_writes(conn, start, end) if start and end else set()
    out = []
    for r in rows:
        prev = conn.execute(
            "SELECT dirty_files, added, modified, deleted, untracked FROM project_changes "
            "WHERE project = ? ORDER BY seen_at DESC, id DESC LIMIT 1",
            (r["project"],),
        ).fetchone()
        moved = prev is None or bool(r["commits_since"]) or any(
            r[k] != prev[k] for k in ("dirty_files", *KINDS))
        if not moved:
            continue
        # None, not zero, for a first sighting: "never seen" is not
        # "unchanged".
        for k in KINDS:
            r["d_" + k] = None if prev is None else r[k] - prev[k]
        r["moved_by"] = "colony" if r["project"] in writers else "outside"
        out.append(r)
    return out


def pending_builds(conn: sqlite3.Connection) -> int:
    """Tickets the PO dispatched that no wake has run yet."""
    return conn.execute(
        "SELECT COUNT(*) n FROM tickets WHERE intent = 'implement' AND status = 'staffed'"
    ).fetchone()["n"]


def collect_finished_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Runs that ended with their ticket still open: a result that was never
    recorded, as a crash mid-harvest leaves.
    """
    return list(
        conn.execute(
            """
            SELECT r.* FROM runs r JOIN tickets t ON t.id = r.ticket_id
             WHERE r.status != 'running' AND r.ended_at IS NOT NULL AND t.status = 'running'
            """
        )
    )


def reap_orphaned_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Close run rows whose process is gone.

    `run_ticket` opens the row before spawning, so a killed parent leaves a
    row reading `running` forever. The threshold is generous (a groom is
    capped at 7 minutes) so a manual pulse never reaps a live run. Recorded
    tokens stay.
    """
    cutoff = (datetime.now() - STALE_RUN_AFTER).strftime("%Y-%m-%d %H:%M:%S")
    orphans = list(
        conn.execute(
            "SELECT * FROM runs WHERE status = 'running' AND started_at < ?", (cutoff,)
        )
    )
    for r in orphans:
        conn.execute(
            "UPDATE runs SET status = 'timeout', ended_at = ?, "
            "verdict = COALESCE(verdict, 'orphaned: parent process died mid-run') "
            "WHERE id = ?",
            (now(), r["id"]),
        )
        conn.execute(
            "UPDATE tickets SET status = 'blocked' WHERE id = ? AND status = 'running'",
            (r["ticket_id"],),
        )
    return orphans


def decisions_since(conn: sqlite3.Connection, since: str) -> int:
    """Decisions the PO made since the last beat, already applied by
    `control.decide`. Windowed, so the count does not grow forever.
    """
    return conn.execute(
        "SELECT COUNT(*) n FROM escalations "
        "WHERE po_decision IS NOT NULL AND resolved_at IS NOT NULL AND resolved_at >= ?",
        (since,),
    ).fetchone()["n"]


# ── the pulse ─────────────────────────────────────────────────────────────────


def run(conn: sqlite3.Connection, *, dry_run: bool = False, allow_wake: bool = True,
        forced: bool = False) -> int:
    """One heartbeat. Zero tokens in the tick.

    The tick is one transaction, so `dry_run` rolls it back and skips the
    outbox. The wake runs outside it, because spent tokens cannot be rolled
    back and the ledger must keep the evidence. `forced` marks an
    out-of-turn beat and leaves the schedule alone. `control.pulse_lock`
    allows one pulse at a time across the task, CLI and dashboard; a second
    raises `control.Busy`.
    """
    with control.pulse_lock():
        return _beat(conn, dry_run=dry_run, allow_wake=allow_wake, forced=forced)


def _beat(conn: sqlite3.Connection, *, dry_run: bool, allow_wake: bool, forced: bool) -> int:
    # Notion and git are read before the transaction and the outbox sent after,
    # so the write lock covers only ledger work.
    pre = _gather(conn)
    conn.execute("BEGIN")
    try:
        ctx = _tick(conn, pre)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")

    if dry_run:
        # Sending here and rolling back the sent_at stamps would post every
        # queued write now and again on the next real pulse.
        pushed = {"sent": 0, "failed": 0, "held": 0, "error": None,
                  "would_send": len(outbox.pending(conn))}
    else:
        pushed = outbox.flush(
            conn, enabled=control.get_control(conn, "notion_write", "1") == "1")
    _note_outbox(ctx, pushed)

    wake_report = None
    if ctx["tier"] == "wake" and not dry_run and allow_wake:
        from . import wake as wake_mod  # local: wake imports us back

        wake_report = wake_mod.run(conn, ctx["usage"])

    if not dry_run:
        _write_pulse_row(conn, ctx, wake_report, forced=forced)
    _report(ctx, wake_report, dry_run=dry_run)
    return 0


def _gather(conn: sqlite3.Connection) -> dict:
    """The slow reads a tick needs, done with no transaction open."""
    last = conn.execute("SELECT MAX(pulse_at) AS at FROM pulses").fetchone()["at"]
    window_start = last or (datetime.now() - PULSE_INTERVAL).strftime("%Y-%m-%d %H:%M:%S")
    window_end = now()
    return {
        "started": time.monotonic(),
        "window_start": window_start,
        "window_end": window_end,
        "board": fetch_board_rows(),
        "scan": projects_mod.scan(since=window_start),
    }


def _note_outbox(ctx: dict, pushed: dict) -> None:
    """Add the outbox result to a finished tick. It drains after the sync so a
    fresh phone edit lands before an older queued write. HALT does not stop
    it.
    """
    notes = ctx["notes"]
    if pushed.get("would_send"):
        notes.append(f"notion: would push {pushed['would_send']}")
    if pushed["sent"]:
        notes.append(f"notion: pushed {pushed['sent']}")
    if pushed["failed"]:
        notes.append(f"notion: {pushed['failed']} push(es) failed")
        ctx["anomalies"] += pushed["failed"]
    if pushed["held"]:
        notes.append(f"notion: {pushed['held']} push(es) held")
    ctx["outbox"] = pushed
    ctx["finding"] = "; ".join(ctx["reasons"] + notes) or "clean"
    ctx["detail"] = _detail(ctx)


def _tick(conn: sqlite3.Connection, pre: dict) -> dict:
    """The free part: look at the world, decide whether it's worth a model."""
    started = pre["started"]
    window_start, window_end = pre["window_start"], pre["window_end"]

    halted = check_halt()
    usage = sample_usage(conn)
    sprint_move = align_sprint(conn)
    board = sync_notion(conn, pre["board"])

    # If work cannot start, the reason must be a card in the Inbox. Every route
    # to `needs-info` should raise one; this is the free backstop.
    ensure_blocked_visible(conn)

    # Delivered stories are resumed, not asked about; `brief_changed` excludes
    # them.
    resumed = resume_delivered(conn, board.get("changed_ids", []))
    outrun = brief_changed(conn, board.get("changed_ids", []))

    orphans = reap_orphaned_runs(conn)
    # A groom ticket whose question was answered is a receipt, not work.
    swept = control.clear_spent_groom_tickets(conn)
    finished = collect_finished_runs(conn)
    decisions = decisions_since(conn, window_start)
    dispatched = pending_builds(conn)

    # What moved on disk since the last beat. An hour with quiet Notion but
    # busy projects is not "clean".
    changed = _moved_since_last(conn, pre["scan"], window_start, window_end)

    # What makes this hour worth spending tokens on. Anything in this list means
    # a wake; an empty list means the tick already did the whole job for free.
    reasons: list[str] = []
    if board["new"]:
        reasons.append(f"{len(board['new'])} new stor{'y' if len(board['new']) == 1 else 'ies'}")
    if board["changed"]:
        reasons.append(f"{len(board['changed'])} story edit(s)")
    if finished:
        reasons.append(f"{len(finished)} finished run(s) to harvest")
    if decisions:
        reasons.append(f"{decisions} decision(s) you made since the last beat")

    # Work already in the ledger counts too, not just Notion news.
    from . import wake as wake_mod  # local: wake imports us back

    pending = wake_mod.groomable_count(conn)
    if pending:
        reasons.append(f"{pending} stor{'y' if pending == 1 else 'ies'} to groom")
    if dispatched:
        reasons.append(f"{dispatched} dispatched ticket(s) to build")

    # A reply the PO typed into the Inbox is the highest-value reason to wake:
    # it is the one input that arrived because a person deliberately sent it.
    unanswered = wake_mod.unanswered_count(conn)
    if unanswered:
        reasons.append(f"{unanswered} PO repl{'y' if unanswered == 1 else 'ies'} to answer")

    # Forge detection is free (§7 step 1), so it runs in the tick, even under
    # HALT. A new candidate is not a reason to wake.
    candidates = forge_mod.detect(conn)

    # A drafted skill *is* worth waking for. The PO asked for it by hand.
    queued_drafts = len(forge_mod.pending_drafts(conn))
    if queued_drafts:
        reasons.append(f"{queued_drafts} skill draft(s) requested")

    anomalies = 0
    notes: list[str] = []
    if candidates:
        notes.append(f"forge: {len(candidates)} new candidate(s)")
    decayed = forge_mod.decaying(conn)
    if decayed:
        notes.append(f"forge: {len(decayed)} skill(s) below win rate")
        anomalies += len(decayed)
    if orphans:
        # An anomaly, not a reason to wake: the run is already over, and paying a
        # model to look at a process that no longer exists buys nothing.
        notes.append(f"reaped {len(orphans)} orphaned run(s)")
        anomalies += len(orphans)
    if halted:
        notes.append("HALT present. Dispatch disabled")
    if not board["configured"]:
        notes.append("notion: unconfigured")
    elif board["error"]:
        notes.append(f"notion: {board['error']}")
        anomalies += 1
    if usage is None:
        notes.append("usage: no cache. Is the tray app running?")
    elif usage["stale"]:
        notes.append("usage: cache stale")
        anomalies += 1
    elif usage["seven_day"] is not None and usage["seven_day"] >= 80:
        notes.append(f"usage: week at {usage['seven_day']}%")

    if changed:
        moved = sum(1 for c in changed if c["commits_since"])
        outside = sum(1 for c in changed if c.get("moved_by") == "outside")
        notes.append(f"projects: {len(changed)} moved"
                     + (f", {moved} committed" if moved else "")
                     + (f", {outside} not by the colony" if outside else ""))

    # The two halves of M5, both free, both worth a line. Ticked boxes and
    # staled questions are the colony noticing that the PO moved ahead of it.
    if resumed:
        notes.append(f"board: {len(resumed)} delivered story(s) grew. Back in the groom queue")
    if outrun:
        notes.append(f"board: {len(outrun)} brief(s) changed after grooming")
    if board.get("ticked"):
        notes.append(f"notion: {len(board['ticked'])} item(s) ticked off")
    if board.get("staled"):
        notes.append(f"inbox: {board['staled']} question(s) went stale")
    if board.get("filed"):
        notes.append(f"board: {len(board['filed'])} stor(y/ies) filed")
    if board.get("revived"):
        notes.append(f"board: {len(board['revived'])} back on the board")
    if swept:
        notes.append(f"queue: {swept} answered groom ticket(s) retired")

    tier = "wake" if (reasons and not halted) else "tick"
    finding = "; ".join(reasons + notes) if (reasons or notes) else "clean"

    ctx = {
        "started": started, "window_start": window_start, "window_end": window_end,
        "tier": tier, "finding": finding, "anomalies": anomalies,
        "usage": usage, "board": board, "reasons": reasons, "notes": notes,
        "sprint_move": sprint_move, "outrun": outrun, "resumed": resumed,
        "finished": len(finished), "halted": halted, "changed": changed,
        "orphans": len(orphans), "swept": swept, "dispatched": dispatched, "groomable": pending,
        "decisions": decisions, "candidates": candidates, "queued_drafts": queued_drafts,
        "decaying": [dict(r) for r in decayed], "outbox": {},
    }
    ctx["detail"] = _detail(ctx)
    return ctx


def _detail(ctx: dict) -> str:
    """The long form of one heartbeat, as plain text for the pulse drawer.
    `actions` holds the machine-readable copy.
    """
    board, usage, changed = ctx["board"], ctx["usage"], ctx["changed"]
    lines = [f"window   {ctx['window_start']} -> {ctx['window_end']}"]

    if not board["configured"]:
        lines.append("notion   unconfigured (no NOTION_TOKEN)")
    elif board["error"]:
        lines.append(f"notion   ERROR {board['error']}")
    else:
        lines.append(f"notion   {board['seen']} rows · {len(board['new'])} new · "
                     f"{len(board['changed'])} changed")
        for title in board["new"]:
            lines.append(f"         + {title}")
        for title in board["changed"]:
            lines.append(f"         ~ {title}")
        for item in (board.get("ticked") or [])[:10]:
            lines.append(f"         [x] {item[:90]}")
        if board.get("staled"):
            lines.append(f"         {board['staled']} open question(s) marked stale. "
                         f"the brief moved under them")
        for title in board.get("filed") or []:
            lines.append(f"         filed   {title}. The colony stops asking about it")
        for title in board.get("revived") or []:
            lines.append(f"         revived {title}. Back on the board")

    ob = ctx.get("outbox") or {}
    if any(ob.get(k) for k in ("sent", "failed", "held")):
        lines.append(f"outbox   {ob.get('sent', 0)} sent · {ob.get('failed', 0)} failed · "
                     f"{ob.get('held', 0)} held"
                     + (f"  ({ob['error']})" if ob.get("error") else ""))

    if usage:
        lines.append(f"usage    5h {usage['five_hour']}% · 7d {usage['seven_day']}%"
                     + ("  (STALE cache)" if usage["stale"] else ""))
    else:
        lines.append("usage    no cache. Is the tray app running?")

    # A sprint that quietly runs past its own budget week is a budget that does
    # not exist, so the tick says out loud whenever it moved the edges.
    move = ctx.get("sprint_move")
    if move:
        lines.append("sprint   " + (
            f"rolled: #{move['closed']} closed, #{move['opened']} opens {move['starts_at']}"
            if move["action"] == "rolled" else
            f"aligned to the allowance week: {move['starts_at']} → {move['ends_at']}"))

    lines.append(f"ledger   {ctx['groomable']} groomable · {ctx['dispatched']} dispatched · "
                 f"{ctx['finished']} unharvested run(s) · {ctx['orphans']} orphan(s) reaped"
                 + (f" · {ctx['swept']} spent groom ticket(s) retired" if ctx.get("swept") else ""))

    if changed:
        lines.append(f"projects {len(changed)} folder(s) moved since the last sample "
                     f"(measured against commit {changed[0].get('head_sha') or '?'})")
        for c in changed[:14]:
            # The delta first, the level in brackets: "+3 new (14 new, 2
            # edited)".
            delta = ", ".join(
                f"{c['d_' + k]:+d} {projects_mod.KIND_SHORT[k]}"
                for k in projects_mod.KIND_SHORT if c.get("d_" + k)
            ) or ("first sighting" if c.get("d_modified") is None else "same files, new commit")
            lines.append(f"         {c['project']:<34} {delta}  [{c['summary']}]"
                         + ("" if c.get("moved_by") == "colony" else "  not the colony"))
        if len(changed) > 14:
            lines.append(f"         ... and {len(changed) - 14} more")
    else:
        lines.append("projects nothing moved on disk")

    if ctx.get("candidates") or ctx.get("queued_drafts") or ctx.get("decaying"):
        lines.append(f"forge    {len(ctx.get('candidates') or [])} new candidate(s) · "
                     f"{ctx.get('queued_drafts', 0)} draft(s) queued")
        for cand in (ctx.get("candidates") or [])[:6]:
            lines.append(f"         + {cand['slug']:<38} [{cand['detector']}]")
        for row in (ctx.get("decaying") or [])[:6]:
            lines.append(f"         ! {row['slug']:<38} win rate "
                         f"{row['wins']}/{row['wins'] + row['losses']}")

    if ctx["halted"]:
        lines.append("HALT     present. Dispatch disabled, heartbeat still logging")
    lines.append("decision " + ("wake: " + "; ".join(ctx["reasons"]) if ctx["reasons"]
                                else "tick. Nothing worth a model this hour"))
    return chr(10).join(lines)


def _write_pulse_row(conn: sqlite3.Connection, ctx: dict, wake_report: dict | None,
                     *, forced: bool = False) -> None:
    """Close the hour. Written after the wake so `tokens` is what was really spent."""
    board, usage = ctx["board"], ctx["usage"]
    finding = ctx["finding"]
    detail = ctx["detail"]
    nl = chr(10)
    if wake_report:
        if wake_report.get("answered"):
            finding += f"; answered {len(wake_report['answered'])} PO repl" + (
                "y" if len(wake_report["answered"]) == 1 else "ies")
        if wake_report["groomed"]:
            finding += f"; groomed {len(wake_report['groomed'])}"
        if wake_report.get("built"):
            finding += f"; built {len(wake_report['built'])}"
        if wake_report.get("forged"):
            finding += f"; drafted {len(wake_report['forged'])} skill(s)"
        if wake_report.get("staffed"):
            finding += f"; proposed {len(wake_report['staffed'])} hire(s)"
        if wake_report.get("stopped"):
            finding += f"; {wake_report['stopped']}"
        if wake_report["skipped"] and not (wake_report["groomed"] or wake_report.get("built")
                                           or wake_report.get("answered")
                                           or wake_report.get("forged")
                                           or wake_report.get("staffed")):
            # "Stood down", not "skipped": an empty job list is the loop
            # working.
            finding += f". Wake stood down: {wake_report['skipped']}"
        detail += nl + nl + "WAKE"
        if wake_report["skipped"]:
            detail += nl + f"         stood down: {wake_report['skipped']}"
        for item in wake_report.get("answered", []):
            detail += (nl + f"  reply  msg #{item['message_id']} -> {item['verdict']}"
                       f" ({item['tokens']:,} tok)")
        for item in wake_report["groomed"]:
            detail += (nl + f"  groom  #{item['story_id']} {item['title'][:44]} -> "
                       f"{item['verdict']} ({item['tokens']:,} tok)")
        for item in wake_report.get("built", []):
            detail += (nl + f"  build  #{item['story_id']} {item['title'][:44]} -> "
                       f"{item['verdict']} ({item['tokens']:,} tok)")
        for item in wake_report.get("forged", []):
            detail += (nl + f"  forge  {item.get('slug', '?')[:44]} -> "
                       f"{item['verdict']} ({item['tokens']:,} tok)")
        for item in wake_report.get("staffed", []):
            detail += (nl + f"  staff  #{item['story_id']} -> {item['verdict']}"
                       f" ({item['tokens']:,} tok)")

    # A forced beat keeps the last scheduled `next_pulse_at`; the task still
    # fires on its own schedule.
    next_at = (datetime.now() + PULSE_INTERVAL).strftime("%Y-%m-%d %H:%M:%S")
    if forced:
        prev = conn.execute(
            "SELECT next_pulse_at FROM pulses ORDER BY pulse_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if prev and prev["next_pulse_at"] and prev["next_pulse_at"] > now():
            next_at = prev["next_pulse_at"]

    cur = conn.execute(
        """
        INSERT INTO pulses (pulse_at, tier, window_start, window_end, actions,
                            finding, anomalies, tokens, duration_ms, next_pulse_at, detail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            # Stamped now, after the wake's runs, so the next pulse does not
            # count them as outstanding.
            now(), ctx["tier"], ctx["window_start"], ctx["window_end"],
            json.dumps({
                "notion": {k: board[k] for k in ("configured", "seen", "new", "changed", "error")},
                # Not `usage` itself: it carries datetimes, and this is JSON.
                "usage": usage_mod.json_safe(usage),
                "finished_runs": ctx["finished"],
                "halted": ctx["halted"],
                "wake_reasons": ctx["reasons"],
                "projects": ctx["changed"],
                "wake": wake_report,
                "forced": forced,
            }),
            finding[:1000], ctx["anomalies"],
            (wake_report or {}).get("tokens", 0),
            int((time.monotonic() - ctx["started"]) * 1000),
            next_at,
            detail,
        ),
    )
    # Attributed to the pulse that saw them, so the Projects panel can answer
    # "when did this start moving?" and not merely "is it dirty right now?".
    projects_mod.record(conn, cur.lastrowid, ctx["changed"])


def _report(ctx: dict, wake_report: dict | None, *, dry_run: bool) -> None:
    board, usage = ctx["board"], ctx["usage"]
    tier, finding, reasons = ctx["tier"], ctx["finding"], ctx["reasons"]
    window_start, window_end = ctx["window_start"], ctx["window_end"]

    if board["configured"]:
        board_line = (
            f"{board['seen']} rows · {len(board['new'])} new · {len(board['changed'])} changed"
        )
    else:
        board_line = "unconfigured (set NOTION_TOKEN in colony-dash/.env)"

    if usage:
        usage_line = f"5h {usage['five_hour']}%  ·  7d {usage['seven_day']}%"
        if usage["stale"]:
            usage_line += "  (stale)"
    else:
        usage_line = "no sample"

    prefix = "DRY " if dry_run else ""
    print(f"{prefix}PULSE {window_end}  ·  {tier}  ·  window {window_start} → {window_end}")
    print(f"  notion    {board_line}")
    print(f"  usage     {usage_line}")
    for row in (ctx.get("changed") or [])[:6]:
        print(f"  project   {row['project']:<38} {row['summary']}")
    print(f"  finding   {finding}")

    if wake_report is None:
        print("  tokens    0   (the heartbeat is free; only the work costs)")
        if tier == "wake" and dry_run:
            print(f"  wake      would wake: {', '.join(reasons)}")
        return

    if wake_report["skipped"]:
        print(f"  wake      stood down: {wake_report['skipped']}")
    for item in wake_report.get("answered", []):
        print(f"  answered  msg #{item['message_id']} → {item['verdict']}"
              f"  ({item['tokens']:,} tok)")
        if item.get("answer"):
            print(f"            {item['answer']}")
    for item in wake_report["groomed"]:
        flag = "  ⚠ over ceiling" if item.get("over_budget") else ""
        print(f"  groomed   #{item['story_id']} \"{item['title'][:40]}\" → {item['verdict']}"
              f"  ({item['tokens']:,} tok{flag})")
    for item in wake_report.get("built", []):
        print(f"  built     #{item['story_id']} \"{item['title'][:40]}\" -> {item['verdict']}"
              f"  ({item['tokens']:,} tok)")
    for item in wake_report.get("forged", []):
        print(f"  forged    {item.get('slug', '?')} -> {item['verdict']}"
              f"  ({item['tokens']:,} tok)")
    for item in wake_report.get("staffed", []):
        print(f"  staffed   #{item['story_id']} → {item['verdict']}"
              f"  ({item['tokens']:,} tok)")
        if item.get("division"):
            print(f"            {item['slug']} · {item['division']} division")
    raw = sum(i.get("raw_tokens", 0)
              for i in (wake_report["groomed"] + wake_report.get("built", [])
                        + wake_report.get("answered", []) + wake_report.get("forged", [])
                        + wake_report.get("staffed", [])))
    print(f"  tokens    {wake_report['tokens']:,} chargeable"
          + (f"  ·  {raw:,} incl. cache reads" if raw else ""))
