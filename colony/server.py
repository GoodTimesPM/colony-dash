"""The dashboard's backend: FastAPI over the ledger.

Reads and writes are deliberately asymmetric.

**Reads** open the ledger with `read_only=True` — not a convention but an
enforcement. Every panel on the page comes through a connection that physically
cannot change anything, so no read path needs auditing for side effects.

**Writes** exist only under `/api/act/*`, and each one is a thin wrapper around
a single function in `control.py`. That module records the decision in
`po_actions` before the change lands and refuses anything the colony's own rules
forbid. The set of things this dashboard can do to the colony is the list of
routes in the "PO actions" section below, and it is meant to stay short enough
to read in one screen.

Two safeguards on the write door, since the server now has one:

  * it is bound to 127.0.0.1 and nothing else (`desktop.py`), so nothing off
    this machine can reach it at all;
  * every action requires an `X-Colony` header. A form on a web page can POST
    across origins without asking; it cannot set a custom header without a
    preflight the browser will refuse. That turns "any page you visit could
    click your HALT button" into "no page but this one can".

Live updates are server-sent events. The pulse writes hourly from a separate
process, so the server polls its own snapshot on a short timer and pushes only
when the fingerprint changes: SQL against a local SQLite file is free, and a
poll that finds nothing costs less than a websocket that has to stay honest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               Response, StreamingResponse)

from . import (access, attachments as attach, console as console_mod, control, db,
               forge, net, notion as notion_mod, outbox as outbox_mod,
               phone as phone_mod, projects as projects_mod, roster as roster_mod,
               usage as usage_mod)

UI_DIR = Path(__file__).resolve().parent / "ui"

# The scrum lifecycle, in order. The board renders these columns even when a
# column is empty — a board that hides its empty columns hides where work isn't.
BOARD_ORDER = [
    "backlog",
    "needs-info",
    "needs-criteria",
    "ready",
    "in-progress",
    "po-review",
    "accepted",
]

# Statuses that mean the PO has filed this one: finished, parked, or not begun.
# They are off the board rather than a column on it, because a column is a place
# work passes through and these are places work stops. They stay reachable — a
# board that can only show live work cannot answer "did I finish that?".
SETTLED_ORDER = ["done", "shelved", "not-started"]

# The pulse log scrolls inside its own panel now, so the limit is what the PO can
# scroll back through rather than what fits on screen. Five days of hourly beats.
PULSE_LIMIT = 120
SSE_INTERVAL_S = 2.0

# The project scan shells out to git, so it is cached rather than run on every
# SSE poll. Thirty seconds is well under how long it takes to notice a change
# and well over how often two seconds would fire.
PROJECT_TTL_S = 30.0
_project_cache: dict[str, Any] = {"at": 0.0, "rows": []}


def _conn() -> sqlite3.Connection:
    return db.connect(read_only=True)


def _rw() -> sqlite3.Connection:
    """A write connection. Only `/api/act/*` may call this."""
    return db.connect()


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


# ── the snapshot ──────────────────────────────────────────────────────────────


def snapshot() -> dict[str, Any]:
    """Everything the page renders, in one read. §9.2's panel table, in order."""
    conn = _conn()
    try:
        return {
            "sprint": _sprint(conn),
            "ordis": _ordis(conn),
            "colony": _colony(conn),
            "board": _board(conn),
            "inbox": _ready(conn) + _inbox(conn),
            "flight": _flight(conn),
            "pulses": _pulses(conn),
            "completed": _completed(conn),
            "forge": _forge(conn),
            "spend": _spend(conn),
            "roster": _roster_summary(conn),
            "controls": _controls(conn),
            "projects": _projects_cached(),
        }
    finally:
        conn.close()


def _live_usage(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The freshest usage figure there is, whoever wrote it last.

    "This is not updating live token usage."

    It was not. The dashboard read the newest row of `usage_samples`, and only
    the hourly pulse writes those, so a figure the tray app refreshes every five
    minutes could be fifty-five minutes old on screen. Worse, it was old in the
    way that is hardest to see: a percentage that has not moved is exactly what
    a working quiet week looks like.

    Reading the cache costs a stat and a small JSON parse, and it is the *same
    file* the pulse copies from, so this is not a second poller and earns nobody
    a 429. The ledger row stays as the fallback for a machine where the tray app
    has never run, and `sampled_at` reports which of the two is being shown.
    """
    row = one(conn, "SELECT * FROM usage_samples ORDER BY sampled_at DESC LIMIT 1")
    live = usage_mod.read()
    if live is None:
        return dict(row) if row else None
    return {
        "five_hour_pct": live["five_hour"],
        "seven_day_pct": live["seven_day"],
        "seven_day_resets_at": live["seven_day_resets"].strftime("%Y-%m-%d %H:%M:%S")
                               if live["seven_day_resets"] else None,
        "five_hour_resets_at": live["five_hour_resets"].strftime("%Y-%m-%d %H:%M:%S")
                               if live["five_hour_resets"] else None,
        "sampled_at": live["mtime"].strftime("%Y-%m-%d %H:%M:%S"),
        "source_mtime": live["mtime"].strftime("%Y-%m-%d %H:%M:%S"),
        "stale": live["stale"],
        "live": True,
    }


def _sprint(conn: sqlite3.Connection) -> dict[str, Any]:
    sprint = one(conn, "SELECT * FROM sprints WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    usage = _live_usage(conn)

    # The allowance week, from the reset instant the API reported. The sprint
    # row follows this — `pulse.align_sprint` moves it onto these edges — but the
    # window is computed here too, because the strip should be telling the truth
    # about the week within a second of a reset rather than within an hour of
    # one, and because a sprint that has not been aligned yet should still show
    # the right day.
    start_dt, end_dt = usage_mod.current_window()
    fmt = "%Y-%m-%d %H:%M:%S"
    week = {
        "starts_at": start_dt.strftime(fmt),
        "ends_at": end_dt.strftime(fmt),
        "day": usage_mod.day_of((start_dt, end_dt)),
        "days": 7,
        "aligned": bool(sprint and sprint["ends_at"] == end_dt.strftime(fmt)),
    }

    spent = {"tokens": 0, "usd": 0.0, "runs": 0}
    if sprint:
        # By run time inside the window, not by story.sprint_id: Notion stories
        # arrive with no sprint attached. Same query the CLI settled on.
        #
        # Half-open on timestamps rather than `date(started_at) BETWEEN`, which
        # counted the five hours before Friday's reset into the week that was
        # already over, and then counted the whole of the closing Friday as well
        # — eight days of runs against a seven-day budget, double-counted at
        # both seams.
        row = one(
            conn,
            """
            SELECT COALESCE(SUM(COALESCE(chargeable_tokens, total_tokens)), 0) AS tokens,
                   COALESCE(SUM(cost_usd), 0)                                 AS usd,
                   COUNT(*)                                                   AS runs
              FROM runs
             WHERE started_at >= ? AND started_at < ?
            """,
            (sprint["starts_at"] or week["starts_at"], sprint["ends_at"] or week["ends_at"]),
        )
        spent = row or spent

    band = control.effective_allowance(conn)
    # When the colony is standing down, the sprint total stops moving — which is
    # correct, and reads exactly like a number that has broken. Saying when the
    # last run ended is the cheapest way to tell those two apart.
    last_run = one(
        conn, "SELECT MAX(ended_at) AS at FROM runs WHERE ended_at IS NOT NULL"
    ) or {}
    return {
        "sprint": sprint,
        "usage": usage,
        "week": week,
        "spent": spent,
        "last_run_at": last_run.get("at"),
        # The bar measures the *allowance*, not the week: 35% of the window is
        # the colony's ceiling, so 35% consumed should read as full, not a third.
        "allowance_pct": band["effective"],
        "allowance": band,
    }


def _ordis(conn: sqlite3.Connection) -> dict[str, Any]:
    """The Scrum Master's own vitals.

    Ordis is not a row in `agents` — it is the loop itself, and it has no
    contract because it hires rather than being hired. But a colony dashboard
    that shows every colonist and not the thing running them is missing its own
    supervisor, so the loop reports here: when it last beat, when it beats next,
    what it decided, and what its judgment has cost so far.
    """
    last = one(conn, "SELECT * FROM pulses ORDER BY pulse_at DESC, id DESC LIMIT 1")
    totals = one(
        conn,
        "SELECT COUNT(*) beats, COALESCE(SUM(tokens),0) tokens, "
        "       SUM(CASE WHEN tier='wake' THEN 1 ELSE 0 END) wakes, "
        "       COALESCE(SUM(anomalies),0) anomalies FROM pulses",
    ) or {}
    # The same predicate the wake actually selects on, not a re-statement of it.
    # Two spellings of "groomable" drifted apart the moment the attempt cap was
    # added: the panel promised three stories the loop had already given up on.
    from . import wake as wake_mod
    groom = one(
        conn,
        f"SELECT COUNT(*) n FROM stories WHERE {wake_mod.GROOMABLE_WHERE}",
        {"max_attempts": wake_mod.MAX_ATTEMPTS},
    ) or {}
    queued = one(
        conn,
        "SELECT COUNT(*) n FROM tickets WHERE intent = 'implement' AND status = 'staffed'",
    ) or {}
    running = one(conn, "SELECT COUNT(*) n FROM runs WHERE status = 'running'") or {}
    return {
        "last": last,
        "beats": totals.get("beats", 0),
        "wakes": totals.get("wakes", 0),
        "tokens": totals.get("tokens", 0),
        "anomalies": totals.get("anomalies", 0),
        "groomable": groom.get("n", 0),
        "queued": queued.get("n", 0),
        "running": running.get("n", 0),
        "halted": control.is_halted(),
    }


def _colony(conn: sqlite3.Connection) -> dict[str, Any]:
    """Running agents first, then everyone on the books. §9.3's active rail."""
    running = rows(
        conn,
        """
        SELECT r.id, r.agent_role, r.model, r.status, r.ticket_id, r.started_at,
               r.worktree_path,
               COALESCE(r.chargeable_tokens, r.total_tokens) AS tokens,
               t.title AS ticket_title, t.intent,
               a.max_tokens_run, a.avatar_seed, a.project, a.write_capable,
               ro.color, ro.emoji
          FROM runs r
          LEFT JOIN tickets t ON t.id = r.ticket_id
          LEFT JOIN agents  a ON a.role = r.agent_role
          LEFT JOIN roster ro ON ro.slug = a.roster_slug
         WHERE r.status = 'running'
         ORDER BY r.started_at
        """,
    )
    hired = rows(
        conn,
        """
        SELECT a.id, a.role, a.project, a.model, a.write_capable, a.max_tokens_run,
               a.avatar_seed, a.status, a.roster_slug, a.notes, a.hired_at,
               ro.color, ro.emoji, ro.name AS persona, ro.description,
               (SELECT COUNT(*) FROM runs r WHERE r.agent_role = a.role) AS run_count,
               (SELECT COALESCE(SUM(COALESCE(r.chargeable_tokens, r.total_tokens)), 0)
                  FROM runs r WHERE r.agent_role = a.role)               AS lifetime_tokens
          FROM agents a
          LEFT JOIN roster ro ON ro.slug = a.roster_slug
         WHERE a.status != 'retired'
         ORDER BY a.project IS NOT NULL, a.role
        """,
    )
    return {"running": running, "standby": hired}


def _board(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = {r["status"]: r["n"] for r in rows(
        conn, "SELECT status, COUNT(*) n FROM stories "
              "WHERE settled_as IS NULL GROUP BY status"
    )}
    filed_counts = {r["settled_as"]: r["n"] for r in rows(
        conn, "SELECT settled_as, COUNT(*) n FROM stories "
              "WHERE settled_as IS NOT NULL AND dropped_at IS NULL GROUP BY settled_as"
    )}
    stories = rows(
        conn,
        """
        SELECT s.id, s.title, s.status, s.project, s.project_source, s.priority,
               s.est_tokens, s.blocked_reason, s.notion_status, s.updated_at,
               s.notion_page_id, s.done_items, s.open_items,
               (SELECT COUNT(*) FROM story_events e WHERE e.story_id = s.id)   AS events,
               (SELECT COALESCE(SUM(e.tokens), 0) FROM story_events e
                 WHERE e.story_id = s.id)                                      AS tokens
          FROM stories s
         WHERE s.status NOT IN ('rejected','archived') AND s.settled_as IS NULL
         ORDER BY s.priority, s.id
        """,
    )
    # Progress belongs on the card, not two clicks in. "4 of 11 done" is the
    # answer to the question the PO actually has when they look at the board —
    # and it is the same pair of columns that stops the loop re-raising finished
    # work, so the number on screen and the number in the prompt cannot drift.
    for s in stories:
        s["done_n"] = len(_json_list(s.pop("done_items", None)))
        s["open_n"] = len(_json_list(s.pop("open_items", None)))
    dropped = rows(
        conn,
        """SELECT id, title, project, priority, dropped_at, drop_reason, notion_page_id
             FROM stories WHERE dropped_at IS NOT NULL
            ORDER BY dropped_at DESC LIMIT 20""",
    )
    # Filed, not dropped, and the difference is who decided. A dropped story is
    # The PO overruling their own board from here; a settled one is the board
    # itself saying the work is done, shelved or not begun. Both are hidden by
    # default and both keep a count in the header, because the count is the only
    # thing that tells you there is anything behind the toggle.
    settled = rows(
        conn,
        """SELECT id, title, project, priority, status, settled_as, notion_status,
                  updated_at, notion_page_id
             FROM stories
            WHERE settled_as IS NOT NULL AND dropped_at IS NULL
            ORDER BY CASE settled_as WHEN 'done' THEN 0 WHEN 'not-started' THEN 1 ELSE 2 END,
                     updated_at DESC
            LIMIT 60""",
    )
    return {
        "columns": [{"status": s, "n": counts.get(s, 0)} for s in BOARD_ORDER],
        "settled_columns": [{"status": s, "n": filed_counts.get(s, 0)} for s in SETTLED_ORDER],
        "stories": stories,
        "dropped": dropped,
        "settled": settled,
    }


# Notes that only mirror a message. The message itself is already in the
# conversation with its whole body on it, so the mirror would double every
# exchange in the episode timeline.
MIRROR_NOTES = {"PO wrote to Ordis about this", "Ordis answered the PO"}

# What a story being "filed" has to say for the work to count as done. A story
# nobody started is filed too, and putting it in a list of finished work is how
# that list stops being worth reading.
COMPLETED_SETTLED = ("done", "shipped", "shelved")


def _findings(raw: Any) -> dict[str, Any] | None:
    """A ticket's findings. JSON when the agent wrote JSON, prose when it did not."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"summary": str(raw)}
    if isinstance(value, dict):
        return value
    return {"summary": str(value)}


def _episode_window(conn: sqlite3.Connection, story_id: int | None,
                    prev_at: str | None, fallback: str | None) -> str:
    """Where this episode starts: the last deliverable, or the story's first day.

    The obvious start is the moment the ticket was cut, and it is the wrong one.
    Most of what happened before a dispatch — the questions, the answers, the
    criteria being argued over — happened *before* the ticket existed, and those
    are the part the PO is looking for when they ask what took place. So an
    episode runs from the previous delivery on the same story to this one.
    """
    if prev_at:
        return prev_at
    if fallback:
        return fallback
    if story_id:
        row = one(conn, "SELECT created_at FROM stories WHERE id = ?", (story_id,))
        if row:
            return row["created_at"] or ""
    return ""


def _episode_counts(conn: sqlite3.Connection, story_id: int | None,
                    since: str, until: str) -> dict[str, int]:
    """How much conversation an episode holds, without loading any of it.

    These are the numbers on the tile. The tile is in the snapshot every panel
    shares, so it carries counts and the detail endpoint carries text.
    """
    if not story_id:
        return {"messages": 0, "questions": 0, "blockers": 0, "learnings": 0}
    p = (story_id, story_id, since, until)
    messages = conn.execute(
        """SELECT COUNT(*) FROM po_messages m
            WHERE (m.story_id = ?
                   OR m.escalation_id IN (SELECT id FROM escalations WHERE story_id = ?))
              AND m.at > ? AND m.at <= ?""", p).fetchone()[0]
    q = (story_id, since, until)
    questions = conn.execute(
        "SELECT COUNT(*) FROM escalations WHERE story_id = ? AND raised_at > ? "
        "AND raised_at <= ?", q).fetchone()[0]
    blockers = conn.execute(
        "SELECT COUNT(*) FROM escalations WHERE story_id = ? AND raised_at > ? "
        "AND raised_at <= ? AND kind = 'needs-info'", q).fetchone()[0]
    learnings = conn.execute(
        "SELECT COUNT(*) FROM story_events WHERE story_id = ? AND kind = 'learning' "
        "AND at > ? AND at <= ?", q).fetchone()[0]
    return {"messages": messages, "questions": questions,
            "blockers": blockers, "learnings": learnings}


def _completed(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    """Everything that finished, newest first.

    "I want there to be a 'completed dispatches' or 'completed stories'. That
    way i can keep track of progress and check on work that has been done so i
    dont accidentally work on the same thing just cause i forgot we worked on
    something."

    The Board's `filed` toggle answers "where does this story stand" and this
    answers a different question: what has this colony actually produced, in
    what order. A dispatch that delivered a patch belongs here whether or not
    the story it came off is finished, because the patch is the thing you would
    otherwise rebuild by hand next week.

    Nothing here is a status change and nothing here writes. It reads
    `settled_as`, which is the PO's word for a story, and `tickets.status`,
    which the ticket sets when its own run closes.
    """
    out: list[dict[str, Any]] = []

    for t in rows(conn, """
        SELECT t.id, t.story_id, t.title, t.role, t.artifact_path, t.findings,
               t.created_at, t.closed_at,
               s.title AS story_title, s.project, s.status AS story_status,
               s.settled_as, s.created_at AS story_created_at,
               t.status,
               (SELECT MAX(p.closed_at) FROM tickets p
                 WHERE p.story_id = t.story_id AND p.intent = 'implement'
                   AND p.status IN ('done','blocked')
                   AND p.closed_at < t.closed_at)                        AS prev_at,
               (SELECT COUNT(*) FROM runs r WHERE r.ticket_id = t.id)     AS runs,
               (SELECT COALESCE(SUM(COALESCE(r.chargeable_tokens, r.total_tokens)), 0)
                  FROM runs r WHERE r.ticket_id = t.id)                   AS tokens,
               (SELECT COALESCE(SUM(r.cost_usd), 0)
                  FROM runs r WHERE r.ticket_id = t.id)                   AS usd
          FROM tickets t
          LEFT JOIN stories s ON s.id = t.story_id
         -- A dispatch that wrote nothing still happened, still cost tokens and
         -- still has an answer worth reading before the same work is ordered a
         -- second time. It closed; it goes in the record. `outcome` below is
         -- what tells the two apart on the tile.
         WHERE t.intent = 'implement' AND t.closed_at IS NOT NULL
           AND t.status IN ('done','blocked')
         ORDER BY t.closed_at DESC LIMIT ?
    """, (limit,)):
        found = _findings(t.pop("findings", None)) or {}
        since = _episode_window(conn, t["story_id"], t.pop("prev_at", None),
                                t.pop("story_created_at", None))
        out.append({
            "kind": "dispatch", "id": t["id"], "ref": "ticket #%d" % t["id"],
            "outcome": "empty" if t["status"] == "blocked" else "delivered",
            "at": t["closed_at"], "since": since, "started_at": t["created_at"],
            "title": t["story_title"] or t["title"],
            "story_id": t["story_id"], "story_status": t["story_status"],
            "settled_as": t["settled_as"], "project": t["project"], "role": t["role"],
            "summary": found.get("summary") or "",
            "done_n": len(found.get("done") or []),
            "skipped_n": len(found.get("skipped") or []),
            "files_n": len(found.get("files") or []),
            "patch": bool(t["artifact_path"]),
            "runs": t["runs"], "tokens": t["tokens"], "usd": t["usd"],
            **_episode_counts(conn, t["story_id"], since, t["closed_at"] or ""),
        })

    for st in rows(conn, """
        SELECT s.id, s.title, s.project, s.status, s.settled_as, s.updated_at,
               s.created_at, s.notion_page_id, s.done_items, s.open_items,
               (SELECT MAX(p.closed_at) FROM tickets p
                 WHERE p.story_id = s.id AND p.intent = 'implement'
                   AND p.status = 'done')                                 AS prev_at,
               (SELECT COUNT(*) FROM tickets p
                 WHERE p.story_id = s.id AND p.intent = 'implement'
                   AND p.status = 'done')                                 AS dispatches,
               (SELECT COUNT(*) FROM runs r JOIN tickets p ON p.id = r.ticket_id
                 WHERE p.story_id = s.id)                                 AS runs,
               (SELECT COALESCE(SUM(COALESCE(r.chargeable_tokens, r.total_tokens)), 0)
                  FROM runs r JOIN tickets p ON p.id = r.ticket_id
                 WHERE p.story_id = s.id)                                 AS tokens,
               (SELECT COALESCE(SUM(r.cost_usd), 0)
                  FROM runs r JOIN tickets p ON p.id = r.ticket_id
                 WHERE p.story_id = s.id)                                 AS usd
          FROM stories s
         WHERE s.settled_as IN (%s) AND s.dropped_at IS NULL
         ORDER BY s.updated_at DESC LIMIT ?
    """ % ",".join("?" * len(COMPLETED_SETTLED)), COMPLETED_SETTLED + (limit,)):
        done_n = len(_json_list(st.pop("done_items", None)))
        open_n = len(_json_list(st.pop("open_items", None)))
        at = st["updated_at"] or st["created_at"]
        since = _episode_window(conn, st["id"], st.pop("prev_at", None),
                                st["created_at"])
        out.append({
            "kind": "story", "id": st["id"], "ref": "story #%d" % st["id"],
            "at": at, "since": since, "started_at": st["created_at"],
            "title": st["title"], "story_id": st["id"],
            "story_status": st["status"], "settled_as": st["settled_as"],
            "project": st["project"], "role": None,
            "summary": "", "done_n": done_n, "skipped_n": open_n, "files_n": 0,
            "patch": False, "dispatches": st["dispatches"],
            "runs": st["runs"], "tokens": st["tokens"], "usd": st["usd"],
            **_episode_counts(conn, st["id"], since, at or ""),
        })

    # One order for two kinds of thing, because the question is chronological.
    out.sort(key=lambda r: (r["at"] or "", r["kind"], r["id"]), reverse=True)
    return out[:limit]


def _json_list(raw: Any) -> list:
    """A JSON array column, or an empty list. Never an exception on the hot path."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _inbox(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows(
        conn,
        """
        SELECT e.*, s.title AS story_title, s.project, s.project_source, s.status AS story_status,
               t.title AS ticket_title,
               -- Scoped to the *story*, not to this escalation. A tile that
               -- counted only its own episode showed "reply to Ordis" on a
               -- story with six messages behind it, because the escalation it
               -- happened to be attached to was two minutes old. The counts
               -- belong to the conversation, and the conversation belongs to
               -- the story (see `control.thread`). The last answer itself is
               -- not here: the tile quoted it and the quote read as the card's
               -- own words, so the count on the reply button is the whole
               -- signal now.
               (SELECT COUNT(*) FROM po_messages m
                 WHERE m.escalation_id = e.id OR m.story_id = e.story_id)          AS messages,
               (SELECT COUNT(*) FROM po_messages m
                 WHERE (m.escalation_id = e.id OR m.story_id = e.story_id)
                   AND m.author = 'po' AND m.status = 'unread')                    AS awaiting_ordis,
               (SELECT m.at FROM po_messages m
                 WHERE m.escalation_id = e.id OR m.story_id = e.story_id
                 ORDER BY m.id DESC LIMIT 1)                                       AS last_message_at,
               CASE WHEN e.snoozed_until IS NOT NULL
                     AND e.snoozed_until > datetime('now','localtime')
                    THEN 1 ELSE 0 END                                             AS snoozed,
               CASE WHEN e.stale_at IS NOT NULL THEN 1 ELSE 0 END                 AS stale
          FROM escalations e
          LEFT JOIN stories s ON s.id = e.story_id
          LEFT JOIN tickets t ON t.id = e.ticket_id
         WHERE e.resolved_at IS NULL
         -- Snoozed and stale both sort to the back, and stale goes furthest.
         -- "Later" has to move something or it is a button that only logs; a
         -- question the brief has already outrun should never be the first
         -- thing on the page, because reading it wastes the one kind of
         -- attention this Inbox is spending.
         ORDER BY stale, snoozed,
                  CASE e.kind WHEN 'write-approval' THEN 0 WHEN 'hire' THEN 1
                              WHEN 'decision' THEN 2 WHEN 'brief-changed' THEN 3
                              ELSE 4 END,
                  e.raised_at DESC
        """,
    )


def _ready(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Stories that have cleared the criteria gate and are waiting to be started.

    **Not an escalation.** An escalation is an event: raised once, answered
    once, closed forever. Readiness is not an event, it is a state the story
    stays in until somebody dispatches it — so raising it as a question would
    make it dismissable while it was still true, which is the one failure this
    Inbox exists to prevent. Derived instead: the tile exists for exactly as
    long as the story is ready, and it is gone the moment the ticket is cut.

    It also carries what is still in the way. `control.dispatch` enforces three
    preconditions and the only way to discover which one you have failed was to
    press the button and read the refusal. Approving the criteria is the moment
    The PO thinks the work has started; a tile that says "ready — except nobody
    is hired to write in that folder" is the difference between a colony that is
    waiting on them and a colony they believe is working.
    """
    out: list[dict[str, Any]] = []
    halted = control.is_halted()
    for s in rows(conn, """
        SELECT s.id, s.title, s.project, s.project_source, s.updated_at,
               (SELECT COUNT(*) FROM agents a
                 WHERE a.project = s.project AND a.write_capable = 1
                   AND a.status <> 'retired')                            AS writers,
               (SELECT COUNT(*) FROM tickets t
                 WHERE t.story_id = s.id AND t.intent = 'implement'
                   AND t.status IN ('open','staffed','running'))         AS queued,
               (SELECT COUNT(*) FROM po_messages m WHERE m.story_id = s.id) AS messages,
               (SELECT COUNT(*) FROM po_messages m WHERE m.story_id = s.id
                  AND m.author = 'po' AND m.status = 'unread')           AS awaiting_ordis
          FROM stories s
         WHERE s.status = 'ready' AND s.dropped_at IS NULL AND s.settled_as IS NULL
         ORDER BY s.updated_at DESC
    """):
        if s["queued"]:
            continue           # already dispatched — the Ticket Queue has it now
        blockers = []
        if not s["project"] or s["project_source"] != "confirmed":
            blockers.append("its project folder is still a guess — confirm it here")
        if not s["writers"]:
            # Picking the person is the Scrum Master's job now (`wake.staff_stories`),
            # so this stopped being an instruction to the PO and became a status.
            # The old text sent them to browse 270 personas they have never read, which
            # is the single reason a story that had cleared every gate sat still.
            pending_hire = one(conn, """SELECT e.id, e.reason FROM escalations e
                                         WHERE e.story_id = ? AND e.kind = 'hire'
                                           AND e.resolved_at IS NULL LIMIT 1""", (s["id"],))
            blockers.append(
                (f"Ordis has proposed someone — {pending_hire['reason']} It is waiting "
                 f"in this Inbox as its own card.")
                if pending_hire else
                (f"nobody is hired to write in {s['project'] or 'that folder'} yet — "
                 f"Ordis picks a persona on the next pulse and brings you the name "
                 f"to approve. You can still hire someone yourself from Standby."))
        if halted:
            blockers.append("the colony is halted — resume it in Macros")
        out.append({
            "id": None, "kind": "ready", "story_id": s["id"], "story_title": s["title"],
            "project": s["project"], "project_source": s["project_source"],
            "reason": f"\u201c{s['title']}\u201d is ready to start.",
            "recommendation": ("Everything it was waiting on is answered. Dispatch cuts an "
                               "implement ticket; the next wake opens a git worktree and "
                               "writes, and the patch comes back for you to read."
                               if not blockers else
                               "Its criteria are accepted. " + blockers[0][:1].upper() + blockers[0][1:] + "."),
            "blockers": blockers, "raised_at": s["updated_at"],
            "messages": s["messages"], "awaiting_ordis": s["awaiting_ordis"],
            "snoozed": 0, "stale": 0, "est_tokens": None, "ticket_id": None,
        })
    return out


def _flight(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Work already in motion — next to the Inbox, because that is where it is decided.

    Two things belong here and they are not the same shape. A **ticket** is work
    the colony is doing or is staffed to do. A **push** is a change queued for
    the Notion board that has not left the machine yet. What they have in common
    is the only thing this panel is about: the PO pressed something, and it has
    not finished. Before this existed, both were two clicks deep in a story
    drawer, which meant "did that go through?" had no answer on the page — and a
    queued change you cannot see is indistinguishable from one that was dropped.

    Sorted by what is furthest along: running first, then staffed, then waiting.
    """
    tickets = rows(
        conn,
        """
        SELECT t.id, t.story_id, t.title, t.intent, t.role, t.status, t.severity,
               t.requires_po, t.est_tokens, t.write_scope, t.created_at,
               t.po_message_id, t.closed_at, substr(t.findings, 1, 2000) AS findings,
               -- The reply itself, not the work order. A reply ticket's work
               -- order is a placeholder until the wake claims it and a 6,000
               -- character prompt afterwards; what makes the tile readable in
               -- both states is the sentence the PO actually typed.
               substr(pm.body, 1, 400) AS po_message,
               s.title AS story_title, s.project,
               r.id           AS run_id,
               r.started_at   AS run_started_at,
               COALESCE(r.chargeable_tokens, r.total_tokens) AS run_tokens,
               ro.color, ro.emoji
          FROM tickets t
          LEFT JOIN stories s ON s.id = t.story_id
          LEFT JOIN po_messages pm ON pm.id = t.po_message_id
          LEFT JOIN runs r ON r.id = (SELECT r2.id FROM runs r2
                                       WHERE r2.ticket_id = t.id AND r2.status = 'running'
                                       ORDER BY r2.started_at DESC LIMIT 1)
          LEFT JOIN agents a ON a.role = t.role
          LEFT JOIN roster ro ON ro.slug = a.roster_slug
         WHERE t.status IN ('open','staffed','running')
            OR (t.status = 'blocked' AND t.closed_at IS NULL)
         ORDER BY CASE t.status WHEN 'running' THEN 0 WHEN 'blocked' THEN 1
                                WHEN 'staffed' THEN 2 ELSE 3 END, t.id DESC
        """,
    )
    pushes = rows(
        conn,
        """
        SELECT o.id, o.story_id, o.kind, o.payload, o.queued_at, o.attempts,
               o.last_error, o.source, s.title AS story_title
          FROM notion_outbox o
          LEFT JOIN stories s ON s.id = o.story_id
         WHERE o.sent_at IS NULL
         ORDER BY o.queued_at
        """,
    )
    out: list[dict[str, Any]] = []
    for t in tickets:
        # A blocked ticket used to sit here forever saying only "blocked", which
        # is a state and not a reason. Two identical ones sat in this rail for a
        # day with nothing on the page explaining either. A blocked ticket that
        # has closed is finished and belongs in Completed instead — the query
        # above no longer selects it — and one still open says why here.
        found = _findings(t.pop("findings", None)) or {}
        t["note"] = (found.get("summary") or "").strip() if found else ""
        out.append({"key": f"t{t['id']}", "kind": "ticket", **t})
    for p in pushes:
        try:
            payload = json.loads(p.pop("payload") or "{}")
        except ValueError:
            payload = {}
        out.append({
            **p,
            "key": f"p{p['id']}",
            "kind": "push",
            # The outbox's own `kind` becomes `verb`: this panel's `kind` says
            # which of the two shapes the row is, and the two must not collide.
            "verb": p["kind"],
            # What the push actually says, in the words the PO used to say it.
            "what": payload.get("status") or payload.get("item") or payload.get("text") or "",
            "checked": payload.get("checked"),
            "stuck": p["attempts"] >= outbox_mod.MAX_ATTEMPTS,
        })
    return out


def _pulses(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # `tier` records what the tick DECIDED, not what happened. An hour can be
    # stamped "wake" because the tick found a reason, and then cost nothing
    # because the wake looked at its job list and stood down. Both facts are
    # true and together they read like a contradiction on screen.
    #
    # `acted` is the second half, taken from the record rather than sniffed out
    # of the finding text: `actions.wake` is null when no wake was spawned at
    # all, and carries `skipped` when one was spawned and declined to spend.
    # Either way the hour was free, and the page labels it a tick.
    return rows(
        conn,
        "SELECT id, pulse_at, tier, finding, anomalies, tokens, duration_ms, "
        "       CASE WHEN tier <> 'wake' THEN 0 "
        "            WHEN json_extract(actions, '$.wake') IS NULL THEN 0 "
        "            WHEN json_extract(actions, '$.wake.skipped') IS NOT NULL THEN 0 "
        "            ELSE 1 END AS acted, "
        "       CASE WHEN detail IS NULL OR detail = '' THEN 0 ELSE 1 END AS has_detail, "
        "       COALESCE(json_extract(actions, '$.forced'), 0) AS forced "
        "FROM pulses ORDER BY pulse_at DESC, id DESC LIMIT ?",
        (PULSE_LIMIT,),
    )


def _forge(conn: sqlite3.Connection) -> dict[str, Any]:
    """The forge panel. Active skills are included, not just the pending ones.

    The M2 stub listed candidates and drafts only, which made the panel go empty
    exactly when the forge had succeeded — the same failure the Files panel had
    (§10.5). What the PO wants to see once a skill is promoted is what it has
    earned since.
    """
    return forge.board(conn)


def _spend(conn: sqlite3.Connection) -> dict[str, Any]:
    """The spend panel's breakdown by role.

    The time series used to live here too, as a fixed fourteen days. It moved to
    `/api/spend` when the chart grew a grain control: the snapshot is one payload
    for eleven panels, pushed on every fingerprint change, and there is no reason
    for the other ten to carry 48 hourly buckets so that one of them can draw a
    line the PO may not even be looking at.
    """
    by_role = rows(
        conn,
        """
        SELECT agent_role,
               COUNT(*)                                                    AS runs,
               COALESCE(SUM(COALESCE(chargeable_tokens, total_tokens)), 0) AS tokens,
               COALESCE(SUM(total_tokens), 0)                              AS total_tokens,
               COALESCE(SUM(cost_usd), 0)                                  AS usd
          FROM runs GROUP BY agent_role ORDER BY tokens DESC
        """,
    )
    return {"by_role": by_role}


# ── the spend series ────────────────────────────────────────────────────────
# Every timestamp in the ledger is `datetime('now','localtime')`, so there is no
# timezone to reconcile here: the strings are already in the wall-clock the PO
# reads them in, and the buckets are cut on the same clock.
#
# All five grains are rolled up in Python from one hourly query rather than five
# different `strftime` groupings. The hourly query returns one row per hour that
# actually had a run — bounded by real activity, not by the length of the window
# — so it is small however far back you look, and a week that starts on Monday
# is a line of Python instead of a nest of SQLite date modifiers.

SPAN = {"hour": 48, "day": 30, "week": 26, "month": 12, "year": 5}
LABEL = {"hour": "%H:00 %a", "day": "%a %d %b", "week": "w/c %d %b",
         "month": "%b %Y", "year": "%Y"}


def _floor(dt: datetime, grain: str) -> datetime:
    """The start of the bucket `dt` falls in."""
    if grain == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if grain == "day":
        return d
    if grain == "week":
        return d - timedelta(days=d.weekday())
    if grain == "month":
        return d.replace(day=1)
    return d.replace(month=1, day=1)


def _back(dt: datetime, grain: str) -> datetime:
    """One bucket earlier. Months and years are calendar steps, not 30 days."""
    if grain == "hour":
        return dt - timedelta(hours=1)
    if grain == "day":
        return dt - timedelta(days=1)
    if grain == "week":
        return dt - timedelta(days=7)
    if grain == "month":
        return (dt.replace(day=1) - timedelta(days=1)).replace(day=1)
    return dt.replace(year=dt.year - 1)


def _series(conn: sqlite3.Connection, grain: str, span: int,
            end: datetime | None = None) -> dict[str, Any]:
    """Spend per bucket over the `span` buckets ending with the one `end` falls in.

    Empty buckets are emitted with zeros rather than skipped. A chart that only
    plots the hours that had runs draws a continuous line across a quiet night
    and calls it steady spending; the flat stretch at zero *is* the information.

    `end` defaults to now, which is the live view. Any other value is the PO
    having paged back or picked a date, and the window is anchored there.
    """
    hourly = rows(
        conn,
        """
        SELECT strftime('%Y-%m-%d %H', started_at) AS h,
               COALESCE(SUM(COALESCE(chargeable_tokens, total_tokens)), 0) AS tokens,
               COALESCE(SUM(total_tokens), 0)                              AS total_tokens,
               COALESCE(SUM(cost_usd), 0)                                  AS usd,
               COUNT(*)                                                    AS runs
          FROM runs WHERE started_at IS NOT NULL GROUP BY h
        """,
    )

    starts: list[datetime] = []
    at = _floor(end or datetime.now(), grain)
    for _ in range(span):
        starts.append(at)
        at = _back(at, grain)
    starts.reverse()

    index = {d: i for i, d in enumerate(starts)}
    pts = [{"key": d.strftime("%Y-%m-%d %H:%M"),
            "label": d.strftime(LABEL[grain]),
            "tokens": 0, "total_tokens": 0, "usd": 0.0, "runs": 0}
           for d in starts]

    # Runs on either side of the window are counted, not merely dropped. Once the
    # window can be paged away from now, "0 runs" has two very different causes —
    # a quiet stretch, or a window pointed at the wrong end of the ledger — and
    # only a count in each direction tells them apart.
    before = after = 0
    first, last = starts[0], starts[-1]
    for r in hourly:
        try:
            when = datetime.strptime(r["h"], "%Y-%m-%d %H")
        except (TypeError, ValueError):
            continue
        bucket = _floor(when, grain)
        i = index.get(bucket)
        if i is None:
            if bucket < first:
                before += r["runs"]
            elif bucket > last:
                after += r["runs"]
            continue
        p = pts[i]
        p["tokens"] += r["tokens"]
        p["total_tokens"] += r["total_tokens"]
        p["usd"] += r["usd"]
        p["runs"] += r["runs"]

    return {
        "grain": grain,
        "span": span,
        "points": pts,
        "tokens": sum(p["tokens"] for p in pts),
        "usd": sum(p["usd"] for p in pts),
        "runs": sum(p["runs"] for p in pts),
        # What the window is not showing, so "0 runs" can be told apart from
        # "all of it happened on the other side of this window".
        "outside": before,
        "ahead": after,
        # Whether the window still ends at the present. The page uses it to grey
        # out the forward arrow rather than letting the PO page into next week.
        "live": last >= _floor(datetime.now(), grain),
    }


def _roster_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Divisions with their personas, so Standby can be browsed and not only searched.

    The whole roster is ~270 rows of short text — small enough to ship in the
    snapshot and let the browser open a division instantly, which is the point
    of a dropdown. The persona *body* is not included; that is a per-click read
    off disk, because 270 markdown files is a different order of payload.
    """
    people = rows(
        conn,
        "SELECT slug, name, division, description, emoji, color, vibe, source FROM roster "
        "ORDER BY division, name",
    )
    hired = {r["roster_slug"] for r in rows(
        conn, "SELECT roster_slug FROM agents WHERE roster_slug IS NOT NULL AND status != 'retired'"
    )}
    divisions: dict[str, list] = {}
    for p in people:
        p["hired"] = p["slug"] in hired
        divisions.setdefault(p["division"], []).append(p)
    return {
        "total": len(people),
        "mine": sum(1 for p in people if p["source"] == "local"),
        "divisions": [
            {"division": d, "n": len(v), "people": v,
             "mine": sum(1 for p in v if p["source"] == "local")}
            for d, v in sorted(divisions.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
    }


def _controls(conn: sqlite3.Connection) -> dict[str, Any]:
    band = control.effective_allowance(conn)
    recent = rows(
        conn,
        "SELECT id, at, action, target_kind, target_id, detail FROM po_actions "
        "ORDER BY at DESC, id DESC LIMIT 8",
    )
    return {
        "halted": control.is_halted(),
        "halt_reason": control.get_control(conn, "halt_reason", ""),
        "allowance": band,
        "allowance_min": control.MIN_ALLOWANCE_PCT,
        "allowance_max": control.MAX_ALLOWANCE_PCT,
        "recent": recent,
        # M5. Separate from HALT on purpose: HALT means "spend nothing", and a
        # comment on a Notion page is not a token. One can be on while the other
        # is off, and conflating them would make a paused colony look mute.
        "notion_write": control.get_control(conn, "notion_write", "1") == "1",
        # A forced beat runs on a thread and takes minutes when it wakes, so the
        # button has to be able to say "running" rather than sit there looking
        # unpressed. True for a scheduled beat as well — the lock is shared.
        "pulse_running": control.pulse_running(),
        "outbox": outbox_mod.depth(conn),
        "notion_statuses": list(notion_mod.WRITABLE_STATUS),
    }


def _projects_cached() -> list[dict[str, Any]]:
    import time as _time

    now = _time.monotonic()
    if now - _project_cache["at"] > PROJECT_TTL_S:
        try:
            _project_cache["rows"] = projects_mod.scan()
        except Exception:
            _project_cache["rows"] = []
        _project_cache["at"] = now
    return _project_cache["rows"]


def fingerprint(state: dict[str, Any]) -> str:
    """What "changed" means for the live feed.

    Elapsed time on a running agent changes every second and is computed in the
    browser from `started_at`; if it were part of this hash, every poll would
    push a frame and the feed would be a clock, not a change feed.
    """
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Colony Dash", docs_url=None, redoc_url=None)


def _guard(header: str | None) -> None:
    """The write door's lock. See the module docstring for why a header is enough."""
    if header != "1":
        raise HTTPException(403, "PO actions require the dashboard's own page")


def _act(fn, *args, **kwargs) -> dict[str, Any]:
    """Run one control function in its own transaction.

    `Refused` is a 409 with the reason attached, because every refusal in
    `control.py` is written to be shown to a person: "this story's project is
    still a guess" is more useful on screen than "forbidden".
    """
    conn = _rw()
    try:
        conn.execute("BEGIN")
        try:
            out = fn(conn, *args, **kwargs)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return {"ok": True, **(out if isinstance(out, dict) else {"result": out})}
    except control.Refused as exc:
        raise HTTPException(409, str(exc))
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((UI_DIR / "index.html").read_text(encoding="utf-8"))


# The stylesheet and the script used to live inside index.html, which made it a
# 6,957-line file that no editor would syntax-check and no diff would read.
# They are two more files off the same folder now.
#
# `no-store` on both, matching what the page already got by being re-read from
# disk on every request. This is a dashboard being edited while it is open;
# a cached `app.js` means a change that does not appear until a hard refresh,
# which is a debugging session spent on nothing.
def _asset(name: str, media_type: str) -> Response:
    return Response(
        (UI_DIR / name).read_text(encoding="utf-8"),
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/app.css")
def app_css() -> Response:
    return _asset("app.css", "text/css; charset=utf-8")


@app.get("/app.js")
def app_js() -> Response:
    return _asset("app.js", "text/javascript; charset=utf-8")


# ── the phone ────────────────────────────────────────────────────────────────
#
# The dashboard was already a web page; these four routes are what let a phone
# treat it as an app rather than as a tab. The manifest gives it a name and an
# icon on the home screen, the service worker is what makes the browser offer to
# put it there at all, and the icons are the mark from the desktop window.
#
# `sw.js` is served from the root on purpose. A service worker may only control
# pages at or below its own path, so one served from `/ui/sw.js` could not
# control `/` — the single most common way this is got wrong.

@app.get("/manifest.webmanifest")
def manifest() -> Response:
    return _asset("manifest.webmanifest", "application/manifest+json")


@app.get("/sw.js")
def service_worker() -> Response:
    return _asset("sw.js", "text/javascript; charset=utf-8")


# Icons are the one thing here worth caching: they are bytes that change when
# the project is rebranded and not before, and re-fetching them on every launch
# is the difference between a home-screen icon that appears and one that blinks.
@app.get("/icon-{name}.png")
def icon(name: str) -> Response:
    if name not in {"192", "512", "maskable-512"}:
        raise HTTPException(404, "no such icon")
    return Response(
        (UI_DIR / f"icon-{name}.png").read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(UI_DIR / "colony.ico", media_type="image/x-icon")


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    return snapshot()


@app.get("/api/story/{story_id}")
def api_story(story_id: int) -> dict[str, Any]:
    """The detail drawer: the story, its timeline, its tickets, its runs.

    `story_events` exists precisely so this view has something worth reading —
    not "where is this" but "what did we find out, and when" (§9.3).
    """
    conn = _conn()
    try:
        story = one(conn, "SELECT * FROM stories WHERE id = ?", (story_id,))
        if not story:
            raise HTTPException(404, "no such story")
        return {
            "story": story,
            "projects": projects_mod.project_dirs(),
            "events": rows(
                conn,
                "SELECT * FROM story_events WHERE story_id = ? ORDER BY at DESC, id DESC",
                (story_id,),
            ),
            "tickets": rows(
                conn,
                """
                SELECT t.*,
                       (SELECT COUNT(*) FROM runs r WHERE r.ticket_id = t.id) AS runs
                  FROM tickets t WHERE t.story_id = ? ORDER BY t.id
                """,
                (story_id,),
            ),
            "runs": rows(
                conn,
                """
                SELECT r.* FROM runs r
                  JOIN tickets t ON t.id = r.ticket_id
                 WHERE t.story_id = ? ORDER BY r.started_at DESC
                """,
                (story_id,),
            ),
        }
    finally:
        conn.close()


@app.get("/api/spend")
def api_spend(grain: str = Query("day"), span: int = Query(0),
              end: str = Query("")) -> dict[str, Any]:
    """The spend chart, at whichever grain the PO picked, ending wherever they put it."""
    if grain not in SPAN:
        raise HTTPException(400, f"grain must be one of {', '.join(SPAN)}")
    # An out-of-range span is clamped rather than swapped for the default: a
    # caller who asked for 9999 buckets wants "as far back as you go", not 30.
    span = max(2, min(400, span)) if span else SPAN[grain]

    # `end` is where the window stops. A bare date is enough for every grain
    # coarser than an hour, so both spellings are accepted and an unparseable
    # one is an error rather than a silent fall back to now — a date control
    # that quietly ignores you is worse than one that says no.
    at: datetime | None = None
    if end:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                at = datetime.strptime(end, fmt)
                break
            except ValueError:
                continue
        if at is None:
            raise HTTPException(400, "end must be YYYY-MM-DD or YYYY-MM-DD HH:MM")
        # A window anchored in the future is a paging overshoot, not a request to
        # chart tomorrow; it lands back on the live view.
        at = min(at, datetime.now())

    conn = _conn()
    try:
        return _series(conn, grain, span, at)
    finally:
        conn.close()


@app.get("/api/roster")
def api_roster(q: str = Query("", max_length=120), limit: int = 40) -> list[dict[str, Any]]:
    """The standby browser's search, over the FTS index that already exists."""
    conn = _conn()
    try:
        term = q.strip()
        if not term:
            return rows(
                conn,
                "SELECT slug, name, division, description, emoji, color, vibe, path "
                "FROM roster ORDER BY division, name LIMIT ?",
                (limit,),
            )
        # Quote the term: a stray ':' or '-' is a syntax error in FTS5, and a
        # search box that throws 500 on a hyphen is a search box nobody trusts.
        safe = '"' + term.replace('"', '""') + '"*'
        try:
            return rows(
                conn,
                """
                SELECT r.slug, r.name, r.division, r.description, r.emoji, r.color,
                       r.vibe, r.path
                  FROM roster_fts f JOIN roster r ON r.id = f.rowid
                 WHERE roster_fts MATCH ? ORDER BY rank LIMIT ?
                """,
                (safe, limit),
            )
        except sqlite3.OperationalError:
            like = f"%{term}%"
            return rows(
                conn,
                "SELECT slug, name, division, description, emoji, color, vibe, path "
                "FROM roster WHERE name LIKE ? OR description LIKE ? LIMIT ?",
                (like, like, limit),
            )
    finally:
        conn.close()


@app.get("/api/persona")
def api_persona(slug: str = Query(..., max_length=200)) -> dict[str, Any]:
    """One persona, read straight out of its file.

    The ledger stores the frontmatter; the *criteria* — how this persona works,
    what it refuses, what "done" means to it — live in the markdown body, and
    that is exactly what you need to read before hiring someone. So the body is
    read from disk on demand rather than duplicated into SQLite, where it would
    go stale the next time the agency-agents repo is pulled.
    """
    conn = _conn()
    try:
        row = one(conn, "SELECT * FROM roster WHERE slug = ?", (slug,))
        if not row:
            raise HTTPException(404, "no such persona")
        hired = one(
            conn, "SELECT id, role, project, status FROM agents WHERE roster_slug = ? "
                  "AND status != 'retired'", (slug,))
    finally:
        conn.close()

    path = Path(row["path"])
    body, err = "", None
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        # Strip the frontmatter we already have; keep everything after it.
        if text.startswith("---"):
            _, _, rest = text.partition("---\n")
            _, sep, after = rest.partition("\n---")
            body = after.lstrip("-\n") if sep else rest
        else:
            body = text
    else:
        err = f"the persona file is gone from disk: {row['path']}"

    return {**row, "body": body.strip(), "error": err, "sections": _sections(body),
            "hired": hired}


def _sections(markdown: str) -> list[dict[str, str]]:
    """Split a persona body on its headings, so the drawer can show structure.

    Persona files are not uniform — some use `##`, some `**Bold:**`, some
    neither. Anything that fails to split just comes back as one section, which
    renders as the whole file. Degrading to "show me the text" is the right
    failure for a document viewer.
    """
    out: list[dict[str, str]] = []
    title, buf = "", []
    for line in markdown.splitlines():
        if line.startswith("#"):
            if buf or title:
                out.append({"title": title, "body": "\n".join(buf).strip()})
            title, buf = line.lstrip("# ").strip(), []
        else:
            buf.append(line)
    if buf or title:
        out.append({"title": title, "body": "\n".join(buf).strip()})
    return [s for s in out if s["title"] or s["body"]]


@app.get("/api/projects")
def api_projects() -> dict[str, Any]:
    """Live working-tree state for every project. The file-manager panel."""
    return {"head": projects_mod.head(), "rows": projects_mod.scan(),
            "all": projects_mod.project_dirs()}


@app.get("/api/tree")
def api_tree(path: str = Query("", max_length=400)) -> dict[str, Any]:
    """One folder's children. The browsable half of the file panel.

    Lazy by design — the caller asks for the folder it is about to draw, and
    nothing else. `projects_mod.safe_path` is the only thing standing between a
    query string and the filesystem, so every refusal it raises becomes a 400
    rather than a stack trace.
    """
    try:
        return projects_mod.tree(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/file")
def api_file(path: str = Query(..., max_length=400)) -> dict[str, Any]:
    """One file's text. Read-only, size-capped, secrets excluded by name."""
    try:
        return projects_mod.read_file(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/project")
def api_project(name: str = Query(..., max_length=200)) -> dict[str, Any]:
    """One project: what is dirty in it, what landed recently, and the diff."""
    known = projects_mod.project_dirs()
    if name not in known:
        raise HTTPException(404, "no such project")
    rows_ = [r for r in projects_mod.scan() if r["project"] == name]
    conn = _conn()
    try:
        history = rows(
            conn,
            "SELECT * FROM project_changes WHERE project = ? ORDER BY seen_at DESC LIMIT 20",
            (name,),
        )
    finally:
        conn.close()
    return {
        "project": name,
        "state": rows_[0] if rows_ else None,
        # The baseline travels with the answer. Without it the drawer had a
        # branch and a sha to print and no source for either, which is how its
        # eyebrow came to read "UNDEFINED · UNDEFINED" on every project.
        "head": projects_mod.head(),
        "kinds": projects_mod.KIND_LONG,
        "commits": projects_mod.commits(name),
        "history": history,
    }


@app.get("/api/diff")
def api_diff(project: str = Query(..., max_length=200),
             path: str | None = Query(None, max_length=400)) -> dict[str, Any]:
    """A working-tree diff, for the project or one file inside it.

    `path` is checked to sit under `project` before it reaches git — not because
    git would do anything dangerous with it, but because a viewer that will
    render any path on the disk is a viewer that has stopped being scoped.
    """
    if project not in projects_mod.project_dirs():
        raise HTTPException(404, "no such project")
    if path:
        norm = path.replace("\\", "/")
        if not (norm == project or norm.startswith(project + "/")) or ".." in norm:
            raise HTTPException(400, "that path is not inside that project")
    return {"project": project, "path": path, "diff": projects_mod.diff(project, path)}


PATCH_MAX_CHARS = 400_000


def _diffstat(patch: str) -> dict[str, Any]:
    """Per-file adds and deletes, counted off the patch itself.

    `git diff --stat` was already captured into the escalation's
    recommendation, as text, at build time. This re-counts from the patch
    because the patch is the thing being approved and a summary of a *different*
    artifact is a summary you cannot check. They should agree; if they ever do
    not, the one on this side is the one describing what will land.

    Counted, not parsed: a line is an addition if it starts with a single "+",
    which is true of every added line and of no header, because "+++" is caught
    by the header test first.
    """
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            # "diff --git a/x b/x" {D} take the b-side, which is the path after
            # the change, so a rename reads as where the file ended up.
            parts = line.split(" b/", 1)
            cur = {"path": parts[1] if len(parts) > 1 else line[11:],
                   "added": 0, "removed": 0, "binary": False, "verb": "changed"}
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("new file"):
            cur["verb"] = "added"
        elif line.startswith("deleted file"):
            cur["verb"] = "deleted"
        elif line.startswith("rename to "):
            cur["verb"] = "renamed"
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        elif line.startswith("+"):
            cur["added"] += 1
        elif line.startswith("-"):
            cur["removed"] += 1
    return {
        "files": files,
        "total": {
            "files": len(files),
            "added": sum(f["added"] for f in files),
            "removed": sum(f["removed"] for f in files),
            "binary": sum(1 for f in files if f["binary"]),
        },
    }


@app.get("/api/patch")
def api_patch(escalation_id: int = Query(..., ge=1)) -> dict[str, Any]:
    """Everything there is to know about a patch before approving it.

    "once i get a write approval task on the patch page, give me a brief
     rundown of what was changed along with the details of changed file amounts
     and all the statistics"

    The drawer used to show the escalation's own two sentences and then a grey
    box reading "the patch is on disk at the path above", which is the dashboard
    telling the PO to go and open a file in another program {D} at the one gate
    where reading before deciding is the entire point.

    Four things come back, from three places, because no single one of them has
    the whole picture: what the build says it did (the ticket's findings JSON,
    written by the agent), what the diff actually contains (counted here), what
    it cost (the run row), and the patch text itself.
    """
    with _conn() as conn:
        esc = one(conn, "SELECT * FROM escalations WHERE id = ?", (escalation_id,))
        if not esc or esc["kind"] != "write-approval":
            raise HTTPException(404, "no patch waiting under that id")

        try:
            proposal = json.loads(esc["proposal"] or "{}")
        except json.JSONDecodeError:
            proposal = {}
        ticket_id = proposal.get("ticket_id") or esc["ticket_id"]
        ticket = one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,)) if ticket_id else None
        run = one(conn,
                  "SELECT * FROM runs WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
                  (ticket_id,)) if ticket_id else None

    # The agent's own account of the work. It is a claim, not a finding {D} the
    # diff below is what actually happened {D} but it is the only thing that can
    # say which acceptance criteria it believes it met and what it skipped.
    report: dict[str, Any] = {}
    if ticket and ticket.get("findings"):
        try:
            report = json.loads(ticket["findings"])
        except json.JSONDecodeError:
            report = {"summary": str(ticket["findings"])[:2000]}

    path = Path(proposal.get("patch") or (ticket or {}).get("artifact_path") or "")
    patch, error = "", None
    if path.is_file():
        try:
            patch = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            error = f"the patch file would not open: {exc}"
    elif str(path):
        error = "the patch file is no longer at the path the escalation recorded"
    else:
        error = "this escalation does not name a patch file"

    truncated = len(patch) > PATCH_MAX_CHARS
    return {
        "escalation": {"id": esc["id"], "reason": esc["reason"],
                       "recommendation": esc["recommendation"],
                       "story_id": esc["story_id"], "raised_at": esc["raised_at"]},
        "project": proposal.get("project"),
        "ticket": {"id": ticket_id, "role": (ticket or {}).get("role"),
                   "title": (ticket or {}).get("title")},
        "path": str(path) if str(path) else None,
        "report": report,
        "stat": _diffstat(patch),
        "run": {
            "model": (run or {}).get("model"),
            "status": (run or {}).get("status"),
            "started_at": (run or {}).get("started_at"),
            "ended_at": (run or {}).get("ended_at"),
            "chargeable_tokens": (run or {}).get("chargeable_tokens"),
            "total_tokens": (run or {}).get("total_tokens"),
            "cost_usd": (run or {}).get("cost_usd"),
        } if run else None,
        "diff": patch[:PATCH_MAX_CHARS],
        "truncated": truncated,
        "error": error,
    }


@app.get("/api/pulse/{pulse_id}")
def api_pulse(pulse_id: int) -> dict[str, Any]:
    """One heartbeat, in full. The long-form log."""
    conn = _conn()
    try:
        row = one(conn, "SELECT * FROM pulses WHERE id = ?", (pulse_id,))
        if not row:
            raise HTTPException(404, "no such pulse")
        row["changes"] = rows(
            conn, "SELECT * FROM project_changes WHERE pulse_id = ? ORDER BY project",
            (pulse_id,))
        try:
            row["actions_json"] = json.loads(row.get("actions") or "{}")
        except ValueError:
            row["actions_json"] = {}
        # Same distinction the list makes: did a wake actually spend this hour,
        # or did the tick merely decide one was warranted? See `_pulses`.
        wake = row["actions_json"].get("wake")
        row["acted"] = 1 if (row.get("tier") == "wake" and wake
                             and not wake.get("skipped")) else 0
        return row
    finally:
        conn.close()


@app.get("/api/agent/{agent_id}")
def api_agent(agent_id: int) -> dict[str, Any]:
    """A hired agent's contract and its record."""
    conn = _conn()
    try:
        row = one(conn, "SELECT * FROM agents WHERE id = ?", (agent_id,))
        if not row:
            raise HTTPException(404, "no such agent")
        row["runs"] = rows(
            conn,
            "SELECT r.*, t.title AS ticket_title FROM runs r LEFT JOIN tickets t "
            "ON t.id = r.ticket_id WHERE r.agent_role = ? ORDER BY r.started_at DESC LIMIT 20",
            (row["role"],),
        )
        row["scope_folders"] = control.scope_projects(row.get("write_scope"))
        row["all_projects"] = projects_mod.project_dirs()
        return row
    finally:
        conn.close()


# ── PO actions — the only writes ──────────────────────────────────────────────


@app.post("/api/act/decide")
def act_decide(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.decide, int(body["escalation_id"]), str(body["decision"]),
                str(body.get("note") or ""),
                # `or 8` would be wrong here: 0 hours is what "un-snooze" sends,
                # and it is falsy.
                float(8 if body.get("snooze_hours") is None else body["snooze_hours"]))


@app.post("/api/act/confirm-project")
def act_confirm(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Name the folder. Optionally create it first — see `control.create_project`."""
    _guard(x_colony)
    if body.get("create"):
        made = _act(control.create_project, str(body["project"]),
                    why=str(body.get("why") or ""))
        _project_cache["at"] = 0.0
        if not body.get("story_id"):
            return made
    return _act(control.confirm_project, int(body["story_id"]), str(body["project"]))


@app.post("/api/act/story")
def act_story(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """File a story without going through Notion — see `control.create_story`."""
    _guard(x_colony)
    return _act(
        control.create_story,
        title=str(body.get("title") or ""),
        description=str(body.get("description") or ""),
        project=str(body.get("project") or ""),
        priority=int(body.get("priority") or 3),
    )


@app.post("/api/act/reply")
def act_reply(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Say something to Ordis about an Inbox item. Queued for the next wake."""
    _guard(x_colony)
    return _act(
        control.reply,
        escalation_id=int(body["escalation_id"]) if body.get("escalation_id") else None,
        story_id=int(body["story_id"]) if body.get("story_id") else None,
        body=str(body.get("body") or ""),
        attachments=list(body.get("attachments") or []),
    )


@app.post("/api/upload")
def upload(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Take one pasted file and put it on disk. Not an action — nothing decided.

    Uploading is separate from replying so a paste can land the moment it
    happens: a screenshot appears in the composer as a thumbnail you can look at
    and remove, rather than as a promise that something got attached. An upload
    The PO then abandons leaves a file in `.colony/attachments/` and nothing in
    the ledger, which is the harmless direction for that trade to fail in.
    """
    _guard(x_colony)
    try:
        return {"ok": True, "file": attach.save(str(body.get("name") or "file"),
                                                str(body.get("data") or ""))}
    except attach.Rejected as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/attachment/{name}")
def attachment(name: str) -> FileResponse:
    """Serve one stored file back to the page, for the thumbnail in the thread."""
    try:
        return FileResponse(attach.resolve(name))
    except attach.Rejected as exc:
        raise HTTPException(404, str(exc))


def _thread_state(conn: sqlite3.Connection, escalation_id: int | None,
                  story_id: int | None) -> dict[str, Any] | None:
    """Whether the work this conversation is about can move, and what stops it.

    "Blockers need to be put within this chat window, obviously color coded,
    hard to know when there is something that needs to be changed."

    The thread showed what had been *said* and nothing about where the story
    stood, so the one fact that decides whether a reply matters — is this thing
    stuck, and on what — lived two panels away. It is a row at the top of the
    conversation now, and it carries its own severity so the page can colour it
    without re-deriving any of this in JavaScript.

    `moving` is returned as loudly as `blocked`, and that is deliberate. A
    banner that only appears when something is wrong teaches you to read its
    absence, and the absence of a banner is indistinguishable from a panel that
    failed to load.
    """
    if story_id is None and escalation_id:
        row = one(conn, "SELECT story_id FROM escalations WHERE id = ?", (escalation_id,))
        story_id = row["story_id"] if row else None
    if not story_id:
        return None
    s = one(conn, """SELECT id, title, status, blocked_reason, project, project_source,
                            dropped_at, settled_as
                       FROM stories WHERE id = ?""", (story_id,))
    if not s:
        return None

    open_qs = rows(conn, """SELECT id, kind, reason, recommendation, raised_at
                              FROM escalations
                             WHERE story_id = ? AND resolved_at IS NULL AND stale_at IS NULL
                             ORDER BY raised_at""", (story_id,))
    writers = 0
    if s["project"]:
        writers = one(conn, """SELECT COUNT(*) AS n FROM agents
                                WHERE project = ? AND write_capable = 1
                                  AND status <> 'retired'""", (s["project"],))["n"]

    # Worst first, because a colour has to mean the same thing every time it
    # appears. `blocked` is "nothing moves until you answer this". `waiting` is
    # "nothing moves until you decide, but nobody is stuck on you for words".
    # `moving` is the good state.
    if s["dropped_at"] or s["settled_as"]:
        level = "settled"
        headline = f"this story is {s['settled_as'] or 'dropped'} — nothing is running"
    elif s["status"] == "needs-info" or any(q["kind"] == "needs-info" for q in open_qs):
        level = "blocked"
        headline = "blocked — it cannot start until this is answered"
    elif s["status"] == "ready" and not writers:
        level = "waiting"
        headline = "criteria accepted — waiting on a writer to be hired"
    elif any(q["kind"] in ("decision", "hire", "write-approval", "brief-changed")
             for q in open_qs):
        level = "waiting"
        headline = "waiting on your decision"
    elif s["status"] in ("backlog", "needs-criteria"):
        level = "moving"
        headline = "in the groom queue — an agent picks it up on the next pulse"
    elif s["status"] == "in-progress":
        level = "moving"
        headline = "being built now"
    else:
        level = "moving"
        headline = f"status: {s['status']}"

    asks = [{"id": q["id"], "kind": q["kind"],
             "text": q["recommendation"] or q["reason"], "raised_at": q["raised_at"]}
            for q in open_qs]
    if s["blocked_reason"] and not any(a["kind"] == "needs-info" for a in asks):
        # Parked with the reason recorded on the story but no card standing for
        # it. `pulse.ensure_blocked_visible` repairs that on the next tick; until
        # it does, the reason is still the truth and belongs on screen.
        asks.insert(0, {"id": None, "kind": "needs-info",
                        "text": s["blocked_reason"], "raised_at": None})
    if s["project"] and s["project_source"] != "confirmed":
        asks.append({"id": None, "kind": "project",
                     "text": f"the folder {s['project']}/ is still a guess — an "
                             f"inference cannot authorise a write",
                     "raised_at": None})

    return {"story_id": story_id, "title": s["title"], "status": s["status"],
            "level": level, "headline": headline,
            "blocked_reason": s["blocked_reason"], "asks": asks,
            "project": s["project"], "project_source": s["project_source"]}


@app.get("/api/thread")
def thread(escalation_id: int | None = None, story_id: int | None = None) -> dict[str, Any]:
    """The conversation about one item. Read-only, like everything on this side."""
    conn = _conn()
    try:
        return {"messages": control.thread(conn, escalation_id, story_id),
                "entries": control.conversation(conn, escalation_id, story_id),
                "state": _thread_state(conn, escalation_id, story_id)}
    finally:
        conn.close()


def _episode(conn: sqlite3.Connection, story_id: int | None,
             since: str, until: str) -> list[dict[str, Any]]:
    """Everything that happened on a story between two moments, in one order.

    The conversation is `control.conversation` — what was said, what was asked,
    what was learned — and the rest of it is `story_events`: groomed, staffed,
    blocked, decided, synced. They are merged rather than listed separately
    because the order is the story, and reading a decision without the question
    that came two rows above it is the failure this panel exists to fix.
    """
    if not story_id:
        return []
    out = [r for r in control.conversation(conn, story_id=story_id)
           if since < (r.get("at") or "") <= (until or "9999")]
    for ev in rows(conn, """SELECT id, at, kind, summary, detail, tokens
                              FROM story_events
                             WHERE story_id = ? AND kind <> 'learning'
                               AND at > ? AND at <= ? ORDER BY at""",
                   (story_id, since, until or "9999")):
        if ev["kind"] == "note" and ev["summary"] in MIRROR_NOTES:
            continue
        out.append({"kind": "event", "ev_kind": ev["kind"], "at": ev["at"],
                    "id": ev["id"], "body": ev["summary"],
                    "detail": ev["detail"], "tokens": ev["tokens"] or 0})
    out.sort(key=lambda r: ((r["at"] or ""), 0 if r["kind"] == "question" else 1, r["id"]))
    return out


@app.get("/api/completed/detail")
def completed_detail(kind: str, id: int) -> dict[str, Any]:
    """One finished thing, with the whole episode behind it. Read-only."""
    conn = _conn()
    try:
        if kind == "dispatch":
            t = one(conn, """SELECT t.*, s.title AS story_title, s.project,
                                    s.status AS story_status, s.settled_as,
                                    s.created_at AS story_created_at
                               FROM tickets t LEFT JOIN stories s ON s.id = t.story_id
                              WHERE t.id = ?""", (id,))
            if not t:
                raise HTTPException(404, "no ticket %d" % id)
            prev = one(conn, """SELECT MAX(closed_at) AS at FROM tickets
                                 WHERE story_id = ? AND intent = 'implement'
                                   AND status = 'done' AND closed_at < ?""",
                       (t["story_id"], t["closed_at"]))
            since = _episode_window(conn, t["story_id"], (prev or {}).get("at"),
                                    t["story_created_at"])
            head = {"kind": "dispatch", "ref": "ticket #%d" % t["id"],
                    "title": t["story_title"] or t["title"],
                    "ticket_title": t["title"], "role": t["role"],
                    "project": t["project"], "story_id": t["story_id"],
                    "story_status": t["story_status"], "settled_as": t["settled_as"],
                    "at": t["closed_at"], "started_at": t["created_at"], "since": since,
                    "work_order": t["work_order"], "findings": _findings(t["findings"]),
                    "artifact_path": t["artifact_path"], "write_scope": t["write_scope"]}
            runs = rows(conn, """SELECT id, agent_role, model, started_at, ended_at, status,
                                        verdict, total_tokens, chargeable_tokens, cost_usd
                                   FROM runs WHERE ticket_id = ? ORDER BY id""", (id,))
            until = t["closed_at"] or ""
            story_id = t["story_id"]
        elif kind == "story":
            st = one(conn, "SELECT * FROM stories WHERE id = ?", (id,))
            if not st:
                raise HTTPException(404, "no story %d" % id)
            prev = one(conn, """SELECT MAX(closed_at) AS at FROM tickets
                                 WHERE story_id = ? AND intent = 'implement'
                                   AND status = 'done'""", (id,))
            since = _episode_window(conn, id, (prev or {}).get("at"), st["created_at"])
            head = {"kind": "story", "ref": "story #%d" % st["id"], "title": st["title"],
                    "ticket_title": None, "role": None, "project": st["project"],
                    "story_id": st["id"], "story_status": st["status"],
                    "settled_as": st["settled_as"], "at": st["updated_at"],
                    "started_at": st["created_at"], "since": since,
                    "work_order": None, "findings": None, "artifact_path": None,
                    "write_scope": None, "brief": st["description"],
                    "acceptance_criteria": st["acceptance_criteria"],
                    "done_items": _json_list(st["done_items"]),
                    "open_items": _json_list(st["open_items"])}
            runs = rows(conn, """SELECT r.id, r.agent_role, r.model, r.started_at, r.ended_at,
                                        r.status, r.verdict, r.total_tokens,
                                        r.chargeable_tokens, r.cost_usd
                                   FROM runs r JOIN tickets t ON t.id = r.ticket_id
                                  WHERE t.story_id = ? ORDER BY r.id""", (id,))
            until = st["updated_at"] or ""
            story_id = id
        else:
            raise HTTPException(400, "kind must be 'dispatch' or 'story'")

        tickets = rows(conn, """SELECT id, title, intent, role, status, created_at,
                                       closed_at, findings, decided_esc_id
                                  FROM tickets
                                 WHERE story_id = ? AND created_at > ? AND created_at <= ?
                                 ORDER BY id""", (story_id, since, until or "9999"))
        return {**head, "runs": runs, "tickets": tickets,
                "timeline": _episode(conn, story_id, since, until)}
    finally:
        conn.close()


@app.get("/api/phone")
def api_phone(port: int = 8787) -> dict[str, Any]:
    """Whether phone access is on, where it is, and the QR code for it.

    Not part of `/api/state`, on purpose. Answering this shells out to Task
    Scheduler and probes a socket, which is a tenth of a second the live feed
    polls for every few seconds and nobody reads. The panel asks when it opens.
    """
    out = phone_mod.state(port)
    out["svg"] = phone_mod.svg(port)
    return out


@app.post("/api/act/phone")
def act_phone(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Turn phone access on or off. Not `_act`: this touches no ledger row.

    `_act` opens a write transaction and holds it until the control function
    returns; this one registers a scheduled task, which takes seconds of
    PowerShell and would block every writer in the process for the duration.
    """
    _guard(x_colony)
    port = int(body.get("port") or 8787)
    try:
        if body.get("on"):
            return {"ok": True, **phone_mod.turn_on(port)}
        return {"ok": True, **phone_mod.turn_off(port)}
    except net.NoAddress as exc:
        # 409 rather than 500: nothing is broken, this machine is just not on a
        # network worth binding, and the message says what to do about it.
        raise HTTPException(409, str(exc))
    except access.Unconfigured as exc:
        raise HTTPException(409, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"could not set up phone access: {exc}")


@app.post("/api/act/phone-token")
def act_phone_token(request: Request, body: dict = Body(...),
                    x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Rotate the access token. Every paired device is logged out by this.

    Its own route rather than a flag on `act_phone`, for the same reason it is
    its own function in `phone.py`: rotating is destructive to anything already
    paired, and a destructive act reached by passing an extra key to the switch
    is one that eventually gets passed by accident.

    The QR code comes back with it, because the only sensible next action after
    rotating is scanning the new one, and making that a second request is making
    a person press two buttons to finish one thought.
    """
    _guard(x_colony)

    # Loopback only, and enforced here rather than only in the panel that hides
    # the button. A rotate from the network logs the caller out in the middle of
    # its own request: the write succeeds, the response comes back, and every
    # call after it is a 401 -- which reads as the feature being broken rather
    # than as it having worked. The desktop window is exempt for the same reason
    # it is exempt from the token: it is on the machine holding the file.
    peer = request.client.host if request.client else ""
    if not access.is_loopback(peer):
        raise HTTPException(403, "rotating the token is only allowed from the "
                                 "machine itself — doing it from here would log "
                                 "this device out mid-request. Use the dashboard "
                                 "on the desktop, or `py -m colony phone --rotate`.")

    port = int(body.get("port") or 8787)
    try:
        out = phone_mod.rotate(port)
    except OSError as exc:
        # The write is atomic, so this means `.env` is unchanged and the old
        # token still works. Say so, because "rotate failed" otherwise leaves
        # someone wondering whether their phone is about to stop working.
        raise HTTPException(500, f"could not write .env, so the token is "
                                 f"unchanged and paired devices still work: {exc}")
    out["svg"] = phone_mod.svg(port)
    return {"ok": True, **out}


@app.post("/api/act/halt")
def act_halt(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.halt, bool(body["on"]), str(body.get("reason") or ""))


@app.post("/api/act/allowance")
def act_allowance(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    # Two ways to say the same thing: a step off the baseline, or the number the
    # PO typed into the box. The box is the one that does not require them to
    # know what the baseline is.
    if "allowance" in body:
        return _act(control.set_allowance_pct, float(body["allowance"]))
    return _act(control.set_allowance, float(body["boost"]))


@app.post("/api/act/pulse")
def act_pulse(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Beat now. Not `_act`: the pulse opens its own transaction on its own thread.

    Running it inside `_act` would hold a write transaction open across a beat
    that takes minutes, and the pulse's own `BEGIN` would then sit behind it
    until the busy timeout gave up.
    """
    _guard(x_colony)
    conn = _rw()
    try:
        return {"ok": True, **control.force_pulse(conn, allow_wake=bool(body.get("wake", True)))}
    except control.Refused as exc:
        raise HTTPException(409, str(exc))
    finally:
        conn.close()


@app.post("/api/act/hire")
def act_hire(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(
        control.hire,
        roster_slug=body.get("roster_slug") or None,
        role=str(body["role"]).strip().lower().replace(" ", "-"),
        project=(body.get("project") or None),
        model=str(body.get("model") or "claude-sonnet-5"),
        write_capable=bool(body.get("write_capable")),
        max_tokens_run=int(body.get("max_tokens_run") or 400000),
        notes=body.get("notes") or None,
    )


@app.post("/api/act/scope")
def act_scope(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Widen or narrow one hired agent's write scope."""
    _guard(x_colony)
    return _act(
        control.set_write_scope,
        int(body["agent_id"]),
        [str(p) for p in (body.get("projects") or [])],
    )


@app.post("/api/act/secrets")
def act_secrets(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Decide whether this agent's checkout is given the credential files."""
    _guard(x_colony)
    return _act(control.set_secrets, int(body["agent_id"]), bool(body.get("on")))


@app.post("/api/act/retire")
def act_retire(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.retire, int(body["agent_id"]))


@app.post("/api/act/dispatch")
def act_dispatch(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.dispatch, int(body["story_id"]))


@app.post("/api/act/cancel")
def act_cancel(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.cancel_ticket, int(body["ticket_id"]))


@app.post("/api/act/draft-skill")
def act_draft_skill(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Ask for a candidate to be written up. Costs nothing now; the wake pays."""
    _guard(x_colony)
    return _act(control.request_draft, int(body["skill_id"]))


@app.post("/api/act/promote-skill")
def act_promote_skill(body: dict = Body(...),
                      x_colony: str | None = Header(None)) -> dict[str, Any]:
    """The third gate: put a drafted skill on disk and attach it to roles."""
    _guard(x_colony)
    roles = body.get("roles")
    if isinstance(roles, str):
        roles = [r for r in (part.strip() for part in roles.split(",")) if r]
    return _act(control.promote_skill, int(body["skill_id"]), roles or ["ordis"])


@app.post("/api/act/retire-skill")
def act_retire_skill(body: dict = Body(...),
                     x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.retire_skill, int(body["skill_id"]), str(body.get("reason") or ""))


# ── M5: dropping, and talking back to Notion ─────────────────────────────────


@app.post("/api/act/drop")
def act_drop(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Take a story off the board. `notion_status` optionally says so upward too."""
    _guard(x_colony)
    return _act(control.drop_story, int(body["story_id"]),
                reason=str(body.get("reason") or ""),
                notion_status=(body.get("notion_status") or None))


@app.post("/api/act/restore")
def act_restore(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.restore_story, int(body["story_id"]))


@app.post("/api/act/notion")
def act_notion(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Queue one write upward. The tick sends it; this only writes a row."""
    _guard(x_colony)
    kind = str(body.get("kind") or "comment")
    payload = {k: body[k] for k in ("status", "text", "item", "checked") if k in body}
    return _act(control.queue_notion, story_id=int(body["story_id"]), kind=kind,
                payload=payload)


@app.post("/api/act/notion-write")
def act_notion_write(body: dict = Body(...),
                     x_colony: str | None = Header(None)) -> dict[str, Any]:
    """The switch for the whole upward direction. Off holds the queue, never drops it."""
    _guard(x_colony)
    return _act(control.set_notion_write, bool(body["on"]))


@app.post("/api/act/reask")
def act_reask(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Send a stale question back to Ordis rather than answer the wrong question."""
    _guard(x_colony)
    return _act(control.reask, int(body["escalation_id"]))


@app.get("/api/outbox")
def api_outbox() -> dict[str, Any]:
    """What the colony has said upward lately, and what is still waiting."""
    conn = _conn()
    try:
        return {"rows": outbox_mod.recent(conn, 25), **outbox_mod.depth(conn)}
    finally:
        conn.close()


@app.get("/api/skill")
def skill(id: int) -> dict[str, Any]:
    """One skill, with its draft, for the drawer."""
    conn = _conn()
    try:
        return control.skill_draft(conn, id)
    except control.Refused as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()


@app.get("/api/roster/divisions")
def api_roster_divisions() -> dict[str, Any]:
    """The divisions that exist, so the import panel offers them before inventing.

    Counted from the roster rather than from the two directories, because the
    question the dropdown is answering is "where would this persona sit next to
    the others", and "the others" means what the scan actually found.
    """
    conn = _conn()
    try:
        return {
            "divisions": [dict(r) for r in rows(conn,
                "SELECT division, COUNT(*) n, "
                "       SUM(CASE WHEN source='local' THEN 1 ELSE 0 END) mine "
                "FROM roster GROUP BY division ORDER BY division")],
            "local_dir": str(roster_mod.LOCAL_ROSTER_DIR),
            "agency_dir": str(roster_mod.DEFAULT_ROSTER_DIR),
        }
    finally:
        conn.close()


@app.post("/api/act/persona")
def act_persona(body: dict = Body(...),
                x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Write one persona of this machine's own, then re-scan.

    Both halves matter. A write with no re-scan leaves a file on disk that the
    Standby panel cannot see, which reads as the button having done nothing --
    and the fix a person then reaches for is pressing it again, which now fails
    with "already exists".

    The file lands under `~/.colony-agents`, never in the agency-agents clone.
    `roster.write_persona` carries the argument for that; the short version is
    that the clone belongs to somebody else and `git pull` wins every argument.
    """
    _guard(x_colony)
    try:
        path = roster_mod.write_persona(
            division=str(body.get("division") or ""),
            slug=str(body.get("slug") or body.get("name") or ""),
            name=str(body.get("name") or ""),
            description=str(body.get("description") or ""),
            emoji=str(body.get("emoji") or ""),
            color=str(body.get("color") or ""),
            vibe=str(body.get("vibe") or ""),
            body=str(body.get("body") or ""),
            overwrite=bool(body.get("overwrite")),
        )
    except roster_mod.BadPersona as exc:
        raise HTTPException(409, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"could not write the persona file: {exc}")

    result = _rescan(f"persona written to {path.parent.name}/{path.stem}")
    return {"ok": True, "path": str(path), **result}


@app.post("/api/act/persona-delete")
def act_persona_delete(body: dict = Body(...),
                       x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Delete one of this machine's own personas. Refuses agency ones.

    Not guarded by loopback: unlike the console this destroys a text file that
    the person on the phone wrote in the first place, and it cannot reach
    anything the dashboard does not already show them.
    """
    _guard(x_colony)
    slug = str(body.get("slug") or "")
    conn = _rw()
    try:
        conn.execute("BEGIN")
        try:
            path = roster_mod.delete_persona(conn, slug)
            control._record(conn, "note", "colony", None, f"persona removed: {slug}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        _project_cache["at"] = 0.0
        return {"ok": True, "path": str(path)}
    except roster_mod.BadPersona as exc:
        raise HTTPException(409, str(exc))
    finally:
        conn.close()


def _rescan(why: str) -> dict[str, Any]:
    """Re-read both persona roots into the `roster` table.

    Factored out because two routes need it and both need it to be the same
    thing: the explicit rescan button, and the import panel, which would
    otherwise leave a file on disk the Standby panel cannot see.

    `why` prefixes the ledger note. The counts are appended here rather than
    passed in, so that the audit line says what actually changed rather than
    what the caller expected to change.
    """
    conn = _rw()
    try:
        conn.execute("BEGIN")
        try:
            result = roster_mod.sync(conn)
            control._record(conn, "note", "colony", None,
                            f"{why}: {result['total']} personas "
                            f"({result['local']} local), {len(result['added'])} added, "
                            f"{len(result['changed'])} changed, "
                            f"{len(result['removed'])} removed")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        _project_cache["at"] = 0.0
        return result
    finally:
        conn.close()


@app.post("/api/act/rescan")
def act_rescan(x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Re-read both persona roots. The one write that isn't a decision.

    It changes only the `roster` table — résumés, not employees — and a persona
    whose file changed upstream is something the PO should see rather than
    discover the next time they hire. Also the way a persona added by hand, in
    an editor, outside the dashboard, becomes visible without a restart.
    """
    _guard(x_colony)
    try:
        result = _rescan("roster rescan")
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, **result}


# -- the console ---------------------------------------------------------------
# The one part of this server that is not a window onto the colony. These four
# routes are the PO's own terminal, and `console.py` explains at length why they
# are allowed to do what every other route on this server is built to prevent.
#
# They do not go through `_act`. `_act` opens a transaction and holds it until
# the control function returns; `console.send` starts a thread that immediately
# wants the same write lock, so the two would sit waiting on each other for the
# five seconds of `busy_timeout` before one of them lost. The ledger connection
# is autocommit, so each statement here lands on its own.


# Whether the console answers from anywhere, or only from this machine. Read at
# import from `.env` as well as the environment, and changed at runtime by
# `act_console_remote` -- which is the only thing allowed to change it, and
# which writes the file back so the answer survives a restart.
#
# It is deliberately not re-read per request. A boundary that moves the moment
# a file changes on disk is one that can be moved by an editor left open in the
# background, with nothing anywhere to say when. Every change to it now goes
# through one route, and that route writes a ledger note.
CONSOLE_REMOTE = (db._env_value("COLONY_CONSOLE_REMOTE") or "").strip().lower() \
    in {"1", "true", "yes", "on"}


def _console_conn() -> sqlite3.Connection:
    return _rw()


def _at_the_desk(request: Request) -> bool:
    """True when the request came from the machine the colony runs on.

    `access.is_loopback("")` is True by design -- for the token gate, a peer the
    server cannot identify should fall back to *asking for a token*, which is
    the safe side there. Every caller here wants the other side, so the empty
    case is spelled out once rather than borrowed four times.
    """
    peer = request.client.host if request.client else ""
    return bool(peer) and access.is_loopback(peer)


def _desk_only(request: Request) -> None:
    """Refuse a console write that did not come from this machine.

    Every other route on this server is a window onto a ledger: the worst a
    stolen access token buys is reading the board and pressing approve. The
    console is not that. It spawns `claude` with no worktree, no tool
    restrictions and no PO gate -- it is deliberately a shell -- so the same
    stolen token buys arbitrary code execution on the machine holding `.env`,
    which holds the Notion token. The difference in blast radius between those
    two is the entire reason this function exists.

    That token crosses a home LAN over plain HTTP. It is in the first URL, and
    anything on the same network can read it off the wire. Which is an
    acceptable risk for a dashboard and not an acceptable one for a shell.

    So the shell is scoped to the peer address rather than to the token, the
    same way the token rotation route is (`act_phone_token`), and for the same
    reason: some actions should not be reachable by anything that can be copied.

    `act_console_remote` lifts it, for the person who has read this and wants
    the console on their phone anyway. That switch is itself desk-only in the
    direction that loosens, which is the property that makes it safe to be a
    button at all: a stolen token cannot turn off the thing that is stopping it.
    """
    if CONSOLE_REMOTE:
        return
    if not _at_the_desk(request):
        raise HTTPException(403,
            "the console is a real shell on the machine running the colony, so "
            "it only answers from that machine — a stolen access token should "
            "not be worth a command prompt. Everything else here works from your "
            "phone. To allow it from here, turn on 'answer from anywhere' in the "
            "console on the desktop.")


@app.post("/api/act/console-remote")
def act_console_remote(request: Request, body: dict = Body(...),
                       x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Move the console's address boundary, and write the move to `.env`.

    The asymmetry is the design, and it is the answer to the obvious objection
    to putting this behind a button at all -- that a switch which disables a
    security boundary is worthless if whoever gets past the boundary can flip
    it:

      * **Turning it on is desk-only.** Loosening the boundary is exactly the
        thing the boundary exists to prevent, so it can only be done from the
        machine that is already trusted. A token read off the wire buys nothing
        here either.
      * **Turning it off works from anywhere.** Tightening is always safe, and
        the moment you want it is the moment you are away from the desk and have
        realised the phone in your pocket can open a shell at home. Making that
        wait until you are back at the desk would be the wrong way round.

    The general rule, which is worth keeping if a third switch ever appears: you
    may tighten from anywhere and loosen only from the desk.

    `.env` is rewritten so the choice survives a restart, and the in-process
    value is set so it does not *need* one. `db.set_env_value` touches only the
    `COLONY_CONSOLE_REMOTE=` line, which matters because the Notion token is in
    the same file.
    """
    _guard(x_colony)
    on = bool(body.get("on"))
    if on and not _at_the_desk(request):
        raise HTTPException(403,
            "opening the console to the network can only be done from the "
            "machine itself — otherwise a stolen access token could switch off "
            "the check that is keeping it out. Turn it on from the dashboard on "
            "the desktop. Turning it back off works from anywhere.")

    global CONSOLE_REMOTE
    try:
        db.set_env_value("COLONY_CONSOLE_REMOTE", "1" if on else "0",
                         comment="Console reachable from the network. "
                                 "See README, 'How safe is this, honestly'.")
    except OSError as exc:
        # Nothing changed: the file write is atomic and it failed, so leaving
        # the live value alone keeps the process and the file agreeing.
        raise HTTPException(500, f"could not write .env, so the console is "
                                 f"unchanged: {exc}")
    CONSOLE_REMOTE = on

    conn = _rw()
    try:
        control._record(conn, "note", "colony", None,
                        "console opened to the network" if on
                        else "console restricted to this machine")
    finally:
        conn.close()
    return {"ok": True, "remote": on}


@app.get("/api/console")
def api_console(request: Request) -> dict[str, Any]:
    conn = _conn()
    try:
        desk = _at_the_desk(request)
        # Read is allowed from anywhere the token is: watching what the console
        # did is a window onto the machine, which is what the rest of this server
        # already is. Only sending is scoped. `writable` is how the panel knows
        # to draw an explanation instead of an input box -- see `_desk_only`.
        return {**console_mod.state(conn),
                "writable": CONSOLE_REMOTE or desk,
                "remote": CONSOLE_REMOTE,
                "desk": desk}
    finally:
        conn.close()


@app.post("/api/console/send")
def console_send(request: Request, body: dict = Body(...),
                 x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    _desk_only(request)
    conn = _console_conn()
    try:
        return {"ok": True, **console_mod.send(conn, str(body.get("text") or ""))}
    except console_mod.Busy as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.post("/api/console/clear")
def console_clear(request: Request, body: dict = Body(default={}),
                  x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    _desk_only(request)
    conn = _console_conn()
    try:
        return {"ok": True, **console_mod.clear(conn)}
    except console_mod.Busy as exc:
        raise HTTPException(409, str(exc))
    finally:
        conn.close()


@app.post("/api/console/options")
def console_options(request: Request, body: dict = Body(...),
                    x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    _desk_only(request)
    conn = _console_conn()
    try:
        return {"ok": True, **console_mod.set_options(
            conn, body.get("model") or None, body.get("effort") or None)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.post("/api/console/cwd")
def console_cwd(request: Request, body: dict = Body(...),
                x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    _desk_only(request)
    conn = _console_conn()
    try:
        return {"ok": True, **console_mod.set_cwd(conn, body.get("path") or None)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.get("/events")
async def events() -> StreamingResponse:
    async def stream():
        last = None
        while True:
            state = await asyncio.to_thread(snapshot)
            fp = fingerprint(state)
            if fp != last:
                last = fp
                yield f"event: state\ndata: {json.dumps(state, default=str)}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(SSE_INTERVAL_S)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── the gate ─────────────────────────────────────────────────────────────────
#
# Off by default and off forever on a loopback bind: `serve()` only turns this
# on when it is handed an address that is reachable from somewhere else, and
# `access.check` refuses that bind outright unless a token is configured. On the
# desktop, where this server has spent its whole life, nothing below runs.
#
# What it guards is everything. There is no useful public half of this page: the
# board names projects, the drawers hold run transcripts, and `/api/state` is
# the whole ledger in one response. The exceptions are the three files a phone
# needs in order to show the login at all, plus the icons, which are a logo.

PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/sw.js", "/favicon.ico",
                "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"}

# Set by `serve()`. A module-level flag rather than app state because the
# middleware has to be able to answer "am I on?" before anything else runs.
REQUIRE_TOKEN = False

# Set by `serve_extra`, and only there. It says something narrower than it
# sounds: this process is serving loopback *as well as* a network address, and
# the loopback socket was open, unguarded, before the network one existed.
# Demanding a token from it now would log out the desktop dashboard that the
# phone switch was pressed in -- and would be demanding a secret from a caller
# who can read the file the secret is in.
#
# It stays False for a plain `serve()`, where no loopback socket exists and a
# loopback peer therefore cannot arrive. That is not a technicality: it means a
# server bound only to the network never trusts an address, only the token.
TRUST_LOOPBACK = False

LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Colony Dash</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0B0F14">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>
  html { color-scheme: dark; }
  body { margin: 0; min-height: 100dvh; display: grid; place-items: center;
         background: #0B0F14; color: #DEE5EC; padding: 24px;
         font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  form { width: min(320px, 100%); display: flex; flex-direction: column; gap: 12px; }
  h1 { margin: 0; font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
       font-size: 13px; letter-spacing: .16em; text-transform: uppercase; }
  p { margin: 0; color: #6B7885; font-size: 13px; }
  input, button { font: inherit; border-radius: 6px; padding: 11px 12px;
                  border: 1px solid #2A3441; background: #121820; color: inherit; }
  input { font-size: 16px; }          /* under 16px and iOS zooms the page in */
  button { border-color: #0E8C74; color: #34D3AE; cursor: pointer; }
  .bad { color: #E06C5B; font-size: 13px; margin: 0; }
</style>
<form method="post" action="/login">
  <h1>Colony Dash</h1>
  <p>This dashboard is being served off this machine. Paste the access token
     from <code>.env</code>.</p>
  __ERROR__
  <input name="token" type="password" autocomplete="current-password"
         autofocus placeholder="access token">
  <input name="next" type="hidden" value="__NEXT__">
  <button type="submit">unlock</button>
</form>"""


def _login_page(*, error: str = "", next_path: str = "/") -> HTMLResponse:
    html = LOGIN_PAGE.replace(
        "__ERROR__", f'<p class="bad">{error}</p>' if error else "")
    # The only value that reaches the page is a path this server produced, but
    # it is still quoted rather than trusted -- a value interpolated into markup
    # is a value that gets escaped, every time, or the rule stops being a rule.
    return HTMLResponse(html.replace("__NEXT__", html_escape(next_path or "/")))


@app.middleware("http")
async def gate(request: Request, call_next):
    if not REQUIRE_TOKEN or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    # See `TRUST_LOOPBACK`. This is a peer address, not a header:
    # `X-Forwarded-For` is not consulted and must not be, because it is a claim
    # made by the caller and anyone could assert they were loopback.
    if TRUST_LOOPBACK and request.client and access.is_loopback(request.client.host):
        return await call_next(request)

    supplied = (request.cookies.get(access.COOKIE)
                # A query parameter so the first visit can be a link or a QR
                # code. It is swapped for the cookie immediately and the URL is
                # replaced client-side, because a token in an address bar is a
                # token in the browser history.
                or request.query_params.get("k"))
    ok = access.matches(supplied)

    # Recorded before the answer goes out, and on both paths. The Phone panel
    # reads this to say whether anything off this machine has arrived at all,
    # which is the one fact that separates a network dropping the packet from a
    # token being wrong. See `access.note_arrival`.
    access.note_arrival(request.client.host if request.client else "", ok)

    if ok:
        response = await call_next(request)
        if supplied != request.cookies.get(access.COOKIE):
            _set_cookie(response, supplied)
        return response

    # Only a navigation answers with the login page itself. Everything else --
    # an API call, a stylesheet, a script -- answers with a status the caller
    # can act on. Handing the login page's HTML to `fetch` shows up as a JSON
    # parse error three layers from the cause, and handing it to a
    # `<script src>` shows up as a syntax error on line 1 of a file that is
    # fine. A browser asks for `text/html` on a navigation and on nothing else,
    # which is a better test than a list of paths that would need maintaining.
    wants_page = ("text/html" in request.headers.get("accept", "")
                  and not request.url.path.startswith("/api/"))
    if wants_page:
        return _login_page(next_path=request.url.path)
    return Response('{"detail":"access token required"}', status_code=401,
                    media_type="application/json")


def _set_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        access.COOKIE, value, max_age=access.COOKIE_MAX_AGE_S,
        httponly=True, samesite="lax", path="/",
    )


@app.post("/login")
async def login(request: Request) -> Response:
    # Parsed by hand rather than with `await request.form()`, which pulls in
    # `python-multipart`. One login form is not worth a fifth runtime
    # dependency in a project whose whole install story is four packages.
    raw = (await request.body()).decode("utf-8", "replace")
    form = urllib.parse.parse_qs(raw, keep_blank_values=True)
    supplied = (form.get("token") or [""])[0]
    next_path = (form.get("next") or ["/"])[0]
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"          # never bounce off this server
    if not access.matches(supplied):
        return _login_page(error="that is not the token", next_path=next_path)

    response = RedirectResponse(next_path, status_code=303)
    _set_cookie(response, supplied)
    return response


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the dashboard. Non-loopback binds require an access token.

    `access.check` raises rather than returning False for the unsafe case, so
    there is no way to reach `uvicorn.run` with an open server.
    """
    global REQUIRE_TOKEN
    REQUIRE_TOKEN = access.check(host)

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")


def serve_extra(host: str, port: int) -> None:
    """Bind a second address in this process, on the app already running.

    The phone switch is pressed on a page served by a dashboard that is already
    up on loopback. Installing the logon task makes the network address work at
    the *next* logon, which is not what someone who just pressed a button means
    by on -- so the running process opens the second socket itself, and the QR
    code on screen works before it is scanned.

    One app, two sockets. Nothing is duplicated: no second ledger connection
    pool, no second event stream, and no second pulse, because the pulse is a
    scheduled task and has never lived in the server.

    Raises `access.Unconfigured` before binding anything if there is no token,
    and `OSError` if the address is taken.
    """
    import uvicorn

    from .desktop import _wait_for_port

    global REQUIRE_TOKEN, TRUST_LOOPBACK
    REQUIRE_TOKEN = access.check(host) or REQUIRE_TOKEN
    TRUST_LOOPBACK = True

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    def _run() -> None:
        try:
            server.run()
        except SystemExit:
            # A failed bind. uvicorn has already logged one ERROR line saying
            # which address and why; letting `SystemExit` escape a thread makes
            # the interpreter print an "Exception in thread" traceback on top of
            # it, which lands in the same log the person debugging this reads.
            # `_wait_for_port` below is what actually decides the outcome.
            pass

    # `Server.run` builds its own event loop, which is why this needs a thread
    # of its own rather than a task on the loop already running the first
    # socket. Uvicorn skips signal handling off the main thread on purpose.
    thread = threading.Thread(target=_run, daemon=True,
                              name=f"colony-serve-{host}")
    thread.start()

    # A bind that fails does so inside the thread, where uvicorn logs it and
    # exits -- and the caller, having started a thread successfully, would go on
    # to report the phone switch on. So the socket is proven from the outside
    # before this returns.
    if not _wait_for_port(host, port, timeout_s=8.0):
        raise OSError(f"could not start serving {host}:{port} — "
                      "something else is probably bound to it")
