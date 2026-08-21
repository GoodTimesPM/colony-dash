"""The heartbeat. Two tiers, so an idle hour is free. ARCHITECTURE.md §4.

    tick   pure Python, no model, ZERO tokens. Runs every hour, always.
    wake   Ordis actually runs. Only when the tick found something.

This module implements the tick in full, and decides — but does not yet spawn —
the wake. A wake's reasons are recorded on the pulse row so the escalation bar
can be calibrated against a week of real logs before anything is dispatched.

Every path writes a `pulses` row. A pulse that finds nothing writes
`finding = 'clean'` with `tokens = 0`; a *missing* row is the alarm.

Since M3 each row also carries a `detail` field: the long-form account of
everything the tick looked at, not just the one-line verdict. "clean" is a fine
summary and a useless log entry. The detail is what makes an hour worth reading
back a week later, and it includes the project folders that moved — the part of
the colony's world that changes most and that the log used to be blind to.
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
# Both moved to `usage.py`, which is now the one place that knows where the
# tray app writes and how long a read stays worth believing. Kept as aliases
# because the pulse's own tests and the CLI still name them.
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
    """A `.colony/HALT` file stops dispatch. Pulses keep logging — that's the point."""
    return HALT_FILE.exists()


def sample_usage(conn: sqlite3.Connection) -> dict | None:
    """Write the tray app's last good read into `usage_samples`.

    We never call the usage endpoint ourselves: it allows ~5 requests per rolling
    5 minutes per account and the Claude Code CLI spends from the same bucket, so
    a second poller would earn 429s for both. One poller, one cache file.

    This is the *history*, one row an hour. The live figure the dashboard shows
    comes straight from `usage.read()` on every snapshot, because a number that
    the tray app refreshes every five minutes should not be up to an hour old on
    screen — and a stalled hourly copy looks exactly like a counter that broke.
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
            # Stored as the local instant it actually happens at, not the UTC
            # string the cache carries. The dashboard was slicing that string
            # and printing "08:59" for a window that closes at five in the
            # morning.
            sample["seven_day_resets"].strftime("%Y-%m-%d %H:%M:%S")
            if sample["seven_day_resets"] else None,
            sample["mtime"].strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return sample


def align_sprint(conn: sqlite3.Connection) -> dict | None:
    """Keep the active sprint on the allowance week, and roll it when that turns.

    "my weekly token usage resets every friday at 5:00 AM. The weekly sprints
     and day count should abide by this range"

    Sprint 1 was seeded with today + 7 as an admitted placeholder, with a
    docstring promising the pulse would correct it on the first good sample.
    This is that correction, four weeks late: the window comes from the reset
    instant the API reports, and the sprint's dates follow it rather than the
    day of the week the ledger happened to be created on.

    Two outcomes, and the difference matters. Before the window turns, the
    sprint is *aligned* — same sprint, edges moved onto the real boundary. When
    it turns, the sprint is *closed* and the next one opens, because a sprint
    that silently extends past its own budget week is a budget that does not
    exist. The goal is not carried over: a new week is a new week, and a stale
    goal on it would read as a decision nobody made.
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
    """Every real project folder, one and two levels deep.

    A project is a folder with a `PROJECT.md` in it — that is the master
    CLAUDE.md's own definition, not a heuristic we invented, which is exactly why
    it is trustworthy here. Two levels because several projects are containers
    (`job-search/job-radar`) and a story names the sub-project, not the container.

    The first version of this walked every child directory instead, and matched
    "Run Hermes Agent alongside Claude Code" to `balatro-mod-loader/build` — a
    source folder is not a project, and a generic name like `build` or `data`
    will collide with ordinary English forever. Filtering on PROJECT.md removes
    the entire class of error rather than blacklisting names one at a time.
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
    """Guess which folder a story belongs to. Returns (project, confident).

    The Notion board has no Project property by design — the PO wants to keep it
    as a plain idea board. So the colony infers, and confirms once via the Inbox
    before anything is ever written. A guess is never enough to earn write scope.
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

    # The leaf name appearing as a phrase is a confident match; longest leaf
    # first so job-search/job-radar beats job-search. A softer word-overlap match
    # is a candidate that still has to be confirmed in the Inbox.
    for path in sorted(folders, key=lambda p: len(slug_of(p)), reverse=True):
        if norm(slug_of(path)) in haystack:
            return path, True
    for path in sorted(folders, key=lambda p: p.count("/"), reverse=True):
        # Every word of the name, not a subset of its longer ones. Matching a
        # subset let "…Claude Code" land on `job-search` off the bare word
        # "search"; requiring "job" too makes that impossible. A single-word
        # project can't reach here anyway — the phrase pass above already caught
        # it — so the soft match is always at least two words of real evidence.
        words = [w for w in norm(slug_of(path)).split() if len(w) >= 3]
        if len(words) < 2:
            continue
        if all(f" {w} " in haystack for w in words):
            return path, False
    return None, False


def ensure_blocked_visible(conn: sqlite3.Connection) -> int:
    """Re-raise the Inbox card for any blocked story that has lost one.

    A story in `needs-info` is a story the loop will not touch — GROOMABLE_WHERE
    excludes it, so nothing re-derives an answer the PO is supposed to give. That
    is right, and it is only right while the question is on the page. A blocked
    story with no open card is invisible and inert: it will never be groomed and
    it will never be asked about, which is the state the entire board reached.

    So the invariant is enforced here rather than trusted to the six paths that
    can close a card. Pure SQL, no model, runs every tick.
    """
    rows = conn.execute(
        """SELECT id, title, blocked_reason, project, project_source, notion_hash
             FROM stories
            WHERE status = 'needs-info' AND dropped_at IS NULL AND settled_as IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM escalations e
                   WHERE e.story_id = stories.id AND e.resolved_at IS NULL
              )"""
    ).fetchall()
    for r in rows:
        # A story parked with no recorded reason is the older shape of this bug:
        # it was blocked on naming its folder, that got answered elsewhere, and
        # the status never followed. Say the honest version rather than invent a
        # question — the card asks him to point it at a folder, which is the
        # only thing it can still be waiting on.
        ask = (r["blocked_reason"] or "").strip() or (
            "This is parked and no longer says why. Confirm the project folder "
            "it belongs to, or drop it, and the colony will re-read it from scratch."
        )
        conn.execute(
            """INSERT INTO escalations (story_id, kind, reason, recommendation, raised_hash)
               VALUES (?,'needs-info',?,?,?)""",
            (r["id"], f'"{r["title"]}" cannot start yet.', ask[:1000], r["notion_hash"]),
        )
    return len(rows)


def stale_escalations(conn: sqlite3.Connection, story_id: int, new_hash: str) -> int:
    """Flag every open question that was asked about an older version of a story.

    This is the fix for the Inbox's worst habit. An escalation is prose written
    at a moment — "this cannot start until you decide X" — and it stays on the
    page unchanged while Jordan goes away and decides X. The card kept asking
    for things that were already done, which teaches you to stop reading the
    Inbox, which is the only failure mode that actually matters here.

    Stale is a flag and not a delete. The question was genuinely asked, and
    erasing it would erase the fact that the colony was confused about this
    story once. A flagged card sorts to the back, says out loud that the brief
    moved under it, and can be re-asked for free — because clearing
    `acceptance_criteria` is what puts the story back in the groom queue.
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
    # A `needs-info` story is parked: GROOMABLE_WHERE excludes it on purpose, so
    # the loop does not re-derive an answer it already has. But the whole reason
    # it was parked has just changed, so un-park it and let the next wake read
    # the story as it is now rather than as it was.
    conn.execute(
        """UPDATE stories SET status = 'backlog', acceptance_criteria = NULL
            WHERE id = ? AND status = 'needs-info'""",
        (story_id,),
    )
    # And give it its grooming attempts back. Those runs answered a question
    # about a version of the story that no longer exists; counting them against
    # the new version would park the story permanently on its second edit.
    conn.execute(
        """UPDATE tickets SET status = 'wontfix', closed_at = datetime('now','localtime')
            WHERE story_id = ? AND title LIKE 'Groom:%' AND status <> 'wontfix'""",
        (story_id,),
    )
    return marked


def sync_notion(conn: sqlite3.Connection) -> dict:
    """Upsert the board into `stories`. Returns what actually changed."""
    load_env()
    result = {"configured": True, "seen": 0, "new": [], "changed": [], "error": None,
              "staled": 0, "ticked": [], "filed": [], "revived": []}
    try:
        rows = notion.fetch_board()
    except notion.NotionUnconfigured as exc:
        return {**result, "configured": False, "error": str(exc)}
    except Exception as exc:  # network, auth, schema drift — all reportable, none fatal
        return {**result, "error": f"{type(exc).__name__}: {exc}"}

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
        # Exploring rows are research-only and never reach 'ready'. In Progress
        # rows land in the backlog for grooming — the PO, not the loop, marks
        # them ready (gate one). Everything else is filed, not requested.
        status = notion.ledger_status(row["notion_status"])
        settled = notion.SETTLED_STATUS.get(row["notion_status"])

        if prev:
            # The body columns are only written when a body was actually read.
            # A filed row is fetched for its status alone, and copying its empty
            # placeholder over the real brief would mean a story came back from
            # the shelf with nothing written on it.
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
            kind, summary = "synced", "Notion row changed"

            # Filing is the one thing an hourly sync may change about a story's
            # standing. It deliberately does not touch `status`: the working
            # status is the colony's own, and an hourly sync must not reset
            # `po-review` to `backlog` every time Jordan edits a sentence.
            was_filed = prev["settled_as"]
            if settled:
                if was_filed != settled:
                    control.settle_story(conn, story_id, settled, row["notion_status"])
                    result["filed"].append(row["title"])
            elif was_filed:
                # Un-filing restores the story exactly as it was left, which is
                # the whole reason filing is a column of its own: the colony had
                # a view on this story before the PO parked it, and re-deriving
                # that view would cost a groom run to answer a question that was
                # already answered.
                control.revive_story(conn, story_id, row["notion_status"])
                result["revived"].append(row["title"])

            # Boxes ticked in Notion since the last sync. Worth naming in the
            # log on their own: "Jordan finished three things" is the single
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
            # A row that arrives already filed and has never been seen is not
            # news. Importing it would fill the board with every idea Jordan has
            # ever written down, and — worse — the insert path below raises a
            # "which folder is this?" escalation, which is precisely the
            # question a not-started row must never produce.
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
                     f'"{row["title"]}" — I think this belongs to {project}/, but I am guessing.',
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
                     f'"{row["title"]}" — I cannot tell which project folder this belongs to.',
                     "Name the folder under D:\\ALL STUFF\\PROJECTS, or say it is a new project.",
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
    """Project folders the colony itself wrote into during this window.

    The colony has exactly one route into Jordan's working tree — a patch he
    read and approved — so this set is normally empty, and that emptiness is the
    useful part. Everything moving outside it moved for a reason that is not the
    colony: an application rewriting its own config, a build step, an editor,
    him. A log that reports movement without saying that much invites the reading
    Jordan actually had, which was that the colony had been in his folders.
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
    """Keep only the projects whose state is different from the last time we looked.

    `git status` reports a folder as dirty for as long as it stays dirty, so the
    raw scan says "75 untracked" every hour forever and the log fills with an
    unchanging fact. What the pulse should report is *movement*: a file count
    that differs from the last recorded sample, or any commit inside the window.

    A project seen for the first time counts as movement — the first sample is
    news precisely because there is nothing to compare it against.

    Each surviving row also carries **how** it differs, not just that it does.
    The comparison happens here and nowhere else — this is the only place that
    still has the previous sample in hand — so the difference is attached to the
    row and stored with it, rather than left to be re-derived by a drawer opened
    six hours later against a ledger that has moved on.
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
        # A first sighting has no deltas, and says so with None rather than with
        # a zero — "unchanged" and "never seen before" are different facts, and
        # a folder's whole contents appearing as +0 would be the wrong one.
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
    """Runs that ended without their ticket being closed out.

    Not "ended since the last pulse" — that version counted the wake's own
    grooming run, which the wake had already harvested inline, so every wake
    manufactured a reason for the next one. An open ticket behind a finished run
    is the real signal: it means a run's result was never recorded, which is what
    a crash mid-harvest looks like.
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

    `run_ticket` opens the row *before* spawning, so the cost is recorded even if
    the machine dies mid-run — but the flip side is that a killed parent leaves a
    row that says `running` forever. That is exactly what happened at 22:00 on
    2026-08-17: Task Scheduler's 10-minute `ExecutionTimeLimit` was shorter than
    two grooms, so it Ctrl+C'd the pulse mid-agent and the dashboard's Colony
    panel showed an agent that had been working for hours. A dashboard that
    reports live work which isn't happening is worse than no dashboard.

    The threshold is generous on purpose: nothing the colony runs today comes
    near 20 minutes (a groom is capped at 7), and a manual `python -m colony
    pulse` alongside the scheduled one must never reap a run that is genuinely
    alive. Tokens already spent are left as recorded — an orphan is an unknown
    ending, not a refund.
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
    """Decisions the PO made since the last beat.

    This used to count every escalation he had *ever* decided, with nothing to
    clear it — so from his first approval onward the number only went up, every
    tick had a standing reason to wake, and the pulse reported "8 PO decision(s)
    to act on" at a PO whose Inbox held one item.

    Two separate things were wrong with that sentence. The count was unbounded,
    which the window fixes. And the wording had the direction backwards: these
    are decisions he already made, applied by `control.decide` at the moment he
    made them — an approval is not a thing waiting for him, it is a thing that
    already happened. What the wake picks up afterwards is the consequence.
    """
    return conn.execute(
        "SELECT COUNT(*) n FROM escalations "
        "WHERE po_decision IS NOT NULL AND resolved_at IS NOT NULL AND resolved_at > ?",
        (since,),
    ).fetchone()["n"]


# ── the pulse ─────────────────────────────────────────────────────────────────


def run(conn: sqlite3.Connection, *, dry_run: bool = False, allow_wake: bool = True) -> int:
    """One heartbeat. Zero tokens: everything here is pure Python.

    The *tick* is one transaction, so `dry_run` can roll it back and mean what it
    says. The connection is in autocommit, so without this the dry run's own
    `sample_usage` and `sync_notion` writes landed anyway — and the next real
    pulse then reported "0 new", hiding the six stories the dry run had quietly
    ingested. A preview that changes what it previews is worse than none.

    The **wake runs outside that transaction, deliberately.** It spawns agents
    that spend real tokens, and a rollback cannot un-spend them. If the machine
    dies mid-wake, the ledger must still show the run and what it cost. Evidence
    of spending is never allowed to be provisional.
    """
    conn.execute("BEGIN")
    try:
        ctx = _tick(conn)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")

    wake_report = None
    if ctx["tier"] == "wake" and not dry_run and allow_wake:
        from . import wake as wake_mod  # local: wake imports us back

        wake_report = wake_mod.run(conn, ctx["usage"])

    if not dry_run:
        _write_pulse_row(conn, ctx, wake_report)
    _report(ctx, wake_report, dry_run=dry_run)
    return 0


def _tick(conn: sqlite3.Connection) -> dict:
    """The free part: look at the world, decide whether it's worth a model."""
    started = time.monotonic()
    last = conn.execute("SELECT MAX(pulse_at) AS at FROM pulses").fetchone()["at"]
    window_start = last or (datetime.now() - PULSE_INTERVAL).strftime("%Y-%m-%d %H:%M:%S")
    window_end = now()

    halted = check_halt()
    usage = sample_usage(conn)
    sprint_move = align_sprint(conn)
    board = sync_notion(conn)

    # Push before pull would be tidier, but sync first is deliberate: a status
    # the PO set on his phone should land in the ledger before the colony
    # overwrites it with one queued yesterday. The outbox drains after the read
    # for the same reason a merge takes the newer side.
    #
    # HALT does not stop this. HALT means "spend nothing", and a comment is not
    # a token — a halted colony that also stops answering on Notion looks broken
    # rather than paused. `controls.notion_write` is the switch for this one.
    outbox_result = outbox.flush(
        conn, enabled=control.get_control(conn, "notion_write", "1") == "1"
    )

    # Free, and the rule the PO asked for in one line: if work cannot start, the
    # reason is a card in the Inbox. Not a sentence in a thread, not a
    # `blocked_reason` column nothing renders — a card, sitting there, naming
    # the decision. Every route into `needs-info` is supposed to raise one, and
    # every one of them had a way to lose it: a reply closing the card as
    # 'amend', a project confirmation resolving it, a stale flag. This is the
    # backstop, and it costs nothing to run every beat.
    ensure_blocked_visible(conn)

    orphans = reap_orphaned_runs(conn)
    # Free housekeeping, done before anything counts the queue: a groom ticket
    # whose question has been answered is a receipt, not work, and leaving it
    # `blocked` both duplicates it on the page and spends the story's last
    # grooming attempt on a version of it that no longer applies.
    swept = control.clear_spent_groom_tickets(conn)
    finished = collect_finished_runs(conn)
    decisions = decisions_since(conn, window_start)
    dispatched = pending_builds(conn)

    # What moved on disk since the last beat. Pure `git status` — free, and the
    # only part of the tick that watches the thing the colony exists to work on.
    # An hour where Notion was silent but three projects changed is not a quiet
    # hour, and before M3 the log called it "clean".
    changed = _moved_since_last(conn, projects_mod.scan(since=window_start),
                                window_start, window_end)

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

    # Work already sitting in the ledger counts too, not just news from Notion.
    # Without this the loop would only ever wake on the hour a story arrived, and
    # anything it couldn't finish that hour would wait forever.
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

    # The forge notices for free (§7 step 1). It runs in the tick rather than the
    # wake on purpose: detection reads runs the colony has already paid for, so
    # it costs nothing, and noticing is not doing — a HALTed colony should still
    # be able to see that a procedure is emerging. Nothing here spends, and a new
    # candidate is not a reason to wake: it waits for the PO to ask for a draft.
    candidates = forge_mod.detect(conn)

    # A drafted skill *is* worth waking for — the PO asked for it by hand.
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
        notes.append("HALT present — dispatch disabled")
    if not board["configured"]:
        notes.append("notion: unconfigured")
    elif board["error"]:
        notes.append(f"notion: {board['error']}")
        anomalies += 1
    if usage is None:
        notes.append("usage: no cache — is the tray app running?")
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
    # staled questions are the colony noticing that Jordan moved ahead of it.
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
    if outbox_result["sent"]:
        notes.append(f"notion: pushed {outbox_result['sent']}")
    if outbox_result["failed"]:
        notes.append(f"notion: {outbox_result['failed']} push(es) failed")
        anomalies += outbox_result["failed"]
    if outbox_result["held"]:
        notes.append(f"notion: {outbox_result['held']} push(es) held")

    tier = "wake" if (reasons and not halted) else "tick"
    finding = "; ".join(reasons + notes) if (reasons or notes) else "clean"

    ctx = {
        "started": started, "window_start": window_start, "window_end": window_end,
        "tier": tier, "finding": finding, "anomalies": anomalies,
        "usage": usage, "board": board, "reasons": reasons, "notes": notes,
        "sprint_move": sprint_move,
        "finished": len(finished), "halted": halted, "changed": changed,
        "orphans": len(orphans), "swept": swept, "dispatched": dispatched, "groomable": pending,
        "decisions": decisions, "candidates": candidates, "queued_drafts": queued_drafts,
        "decaying": [dict(r) for r in decayed], "outbox": outbox_result,
    }
    ctx["detail"] = _detail(ctx)
    return ctx


def _detail(ctx: dict) -> str:
    """The long form of one heartbeat.

    Plain text rather than more JSON, because it is read by a person in the
    pulse drawer and `actions` already holds the machine-readable copy. Two
    formats, two audiences, one tick.
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
            lines.append(f"         {board['staled']} open question(s) marked stale — "
                         f"the brief moved under them")
        for title in board.get("filed") or []:
            lines.append(f"         filed   {title} — the colony stops asking about it")
        for title in board.get("revived") or []:
            lines.append(f"         revived {title} — back on the board")

    ob = ctx.get("outbox") or {}
    if any(ob.get(k) for k in ("sent", "failed", "held")):
        lines.append(f"outbox   {ob.get('sent', 0)} sent · {ob.get('failed', 0)} failed · "
                     f"{ob.get('held', 0)} held"
                     + (f"  ({ob['error']})" if ob.get("error") else ""))

    if usage:
        lines.append(f"usage    5h {usage['five_hour']}% · 7d {usage['seven_day']}%"
                     + ("  (STALE cache)" if usage["stale"] else ""))
    else:
        lines.append("usage    no cache — is the tray app running?")

    # A sprint that quietly runs past its own budget week is a budget that does
    # not exist, so the tick says out loud whenever it moved the edges.
    move = ctx.get("sprint_move")
    if move:
        lines.append("sprint   " + (
            f"rolled — #{move['closed']} closed, #{move['opened']} opens {move['starts_at']}"
            if move["action"] == "rolled" else
            f"aligned to the allowance week — {move['starts_at']} → {move['ends_at']}"))

    lines.append(f"ledger   {ctx['groomable']} groomable · {ctx['dispatched']} dispatched · "
                 f"{ctx['finished']} unharvested run(s) · {ctx['orphans']} orphan(s) reaped"
                 + (f" · {ctx['swept']} spent groom ticket(s) retired" if ctx.get("swept") else ""))

    if changed:
        lines.append(f"projects {len(changed)} folder(s) moved since the last sample "
                     f"(measured against commit {changed[0].get('head_sha') or '?'})")
        for c in changed[:14]:
            # The delta first, because it is the thing that made this row exist;
            # the level second, in brackets, because it is the thing that made
            # the old log unreadable. "+3 new (14 new, 2 edited)" says both what
            # happened this hour and what the folder looks like now.
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
        lines.append("HALT     present — dispatch disabled, heartbeat still logging")
    lines.append("decision " + ("wake: " + "; ".join(ctx["reasons"]) if ctx["reasons"]
                                else "tick — nothing worth a model this hour"))
    return chr(10).join(lines)


def _write_pulse_row(conn: sqlite3.Connection, ctx: dict, wake_report: dict | None) -> None:
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
        if wake_report["skipped"] and not (wake_report["groomed"] or wake_report.get("built")
                                           or wake_report.get("answered")
                                           or wake_report.get("forged")
                                           or wake_report.get("staffed")):
            finding += f"; wake skipped: {wake_report['skipped']}"
        detail += nl + nl + "WAKE"
        if wake_report["skipped"]:
            detail += nl + f"         stood down — {wake_report['skipped']}"
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

    cur = conn.execute(
        """
        INSERT INTO pulses (pulse_at, tier, window_start, window_end, actions,
                            finding, anomalies, tokens, duration_ms, next_pulse_at, detail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            # Stamped now, not at tick time: a wake takes minutes, and a pulse_at
            # from before its own runs finished makes the next pulse think those
            # runs are still outstanding.
            now(), ctx["tier"], ctx["window_start"], ctx["window_end"],
            json.dumps({
                "notion": {k: board[k] for k in ("configured", "seen", "new", "changed", "error")},
                "usage": usage,
                "finished_runs": ctx["finished"],
                "halted": ctx["halted"],
                "wake_reasons": ctx["reasons"],
                "projects": ctx["changed"],
                "wake": wake_report,
            }),
            finding[:1000], ctx["anomalies"],
            (wake_report or {}).get("tokens", 0),
            int((time.monotonic() - ctx["started"]) * 1000),
            (datetime.now() + PULSE_INTERVAL).strftime("%Y-%m-%d %H:%M:%S"),
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
        print(f"  wake      stood down — {wake_report['skipped']}")
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
