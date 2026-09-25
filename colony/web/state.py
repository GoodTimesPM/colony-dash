"""The snapshot the page paints from, the spend series, and the live feed.

`frame()` builds the snapshot at most once per `SSE_INTERVAL_S` and every
open page shares it. `/events` pushes it only when its fingerprint moves.
`_act` runs a write and expires the frame, so the next read shows it."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from .. import (control, forge, notion as notion_mod, outbox as outbox_mod,
                projects as projects_mod, usage as usage_mod)
from .common import _conn, _rw, one, rows

router = APIRouter()

# The scrum lifecycle, in order. The board renders these columns even when a
# column is empty. A board that hides its empty columns hides where work isn't.
BOARD_ORDER = [
    "backlog",
    "needs-info",
    "needs-criteria",
    "ready",
    "in-progress",
    "po-review",
    "accepted",
]

# Statuses meaning the PO has filed the story: finished, parked or not begun.
# Off the board but reachable behind a toggle.
SETTLED_ORDER = ["done", "shelved", "not-started"]

# The pulse log scrolls inside its own panel now, so the limit is what the PO can
# scroll back through rather than what fits on screen. Five days of hourly beats.
PULSE_LIMIT = 120
SSE_INTERVAL_S = 2.0

# The project scan shells out to git, so it is cached rather than run on every
# SSE poll.
PROJECT_TTL_S = 30.0
_project_cache: dict[str, Any] = {"at": 0.0, "rows": []}


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
            "roster": _roster_head(conn),
            "controls": _controls(conn),
            "projects": _projects_cached(),
        }
    finally:
        conn.close()


def _live_usage(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The freshest usage figure, from the tray app's cache.

    The pulse writes `usage_samples` only hourly, so the ledger row can be
    an hour stale. Reading the cache is a stat and a small parse of the same
    file the pulse copies, so it adds no API calls. The ledger row is the
    fallback, and `sampled_at` says which is shown.
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

    # The allowance week from the reset instant the API reported. Computed here
    # as well as by `pulse.align_sprint`, so the strip is right within a second
    # of a reset.
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
        # By run time inside the window, not `story.sprint_id`: Notion stories
        # arrive with no sprint. Half-open on timestamps, since `date(...)
        # BETWEEN` counted runs at both edges twice.
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
    # A standing-down colony's total stops moving, which looks broken; the last
    # run's end time tells the two apart.
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
    """The Scrum Master's own vitals. Ordis is the loop, not a row in `agents`,
    so it reports here: last beat, next beat, what it decided, and what it
    has cost.
    """
    last = one(conn, "SELECT * FROM pulses ORDER BY pulse_at DESC, id DESC LIMIT 1")
    totals = one(
        conn,
        "SELECT COUNT(*) beats, COALESCE(SUM(tokens),0) tokens, "
        "       SUM(CASE WHEN tier='wake' THEN 1 ELSE 0 END) wakes, "
        "       COALESCE(SUM(anomalies),0) anomalies FROM pulses",
    ) or {}
    # The predicate the wake actually selects on, so the panel cannot promise
    # stories the loop has given up on.
    from .. import wake as wake_mod
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
    # Progress on the card ("4 of 11 done"), from the same columns the prompt
    # uses, so the two cannot drift.
    for s in stories:
        s["done_n"] = len(_json_list(s.pop("done_items", None)))
        s["open_n"] = len(_json_list(s.pop("open_items", None)))
    dropped = rows(
        conn,
        """SELECT id, title, project, priority, dropped_at, drop_reason, notion_page_id
             FROM stories WHERE dropped_at IS NOT NULL
            ORDER BY dropped_at DESC LIMIT 20""",
    )
    # Settled stories are the board saying the work is done, shelved or not
    # begun; dropped ones are the PO overruling it from here. Both are hidden
    # by default with a count in the header.
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


# Notes that only mirror a message, which is already in the conversation.
MIRROR_NOTES = {"PO wrote to Ordis about this", "Ordis answered the PO"}

# Filed statuses that count as finished work. "Not started" is filed but not
# done.
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
    """Where this episode starts: the last deliverable, or the story's first
    day. Most of an episode (questions, answers, criteria) happens before
    its ticket is cut, so it runs from the previous delivery on the same
    story.
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
    """How much conversation an episode holds, as counts. The tile is in the
    shared snapshot; the detail endpoint carries the text.
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
    """Everything that finished, newest first, so the PO can see what the
    colony has produced and not redo it.

    A dispatch that delivered a patch belongs here whether or not its story
    is finished. Read-only: it reads `settled_as` (the PO's word on a story)
    and `tickets.status` (set when the ticket's run closes).
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
               CASE WHEN e.stale_at IS NOT NULL THEN 1 ELSE 0 END                 AS stale,
               e.second_opinion, e.second_opinion_at
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
    """Stories past the criteria gate and waiting to be started.

    **Not an escalation.** Readiness is a state, not an event, so it is
    derived: the tile exists while the story is ready and goes when the
    ticket is cut. It also shows which of `control.dispatch`'s preconditions
    still fail, so the PO need not press the button to find out.
    """
    out: list[dict[str, Any]] = []
    halted = control.is_halted()
    for s in rows(conn, """
        SELECT s.id, s.title, s.project, s.project_source, s.updated_at,
               (SELECT COUNT(*) FROM agents a
                 WHERE a.project = s.project AND a.write_capable = 1
                   AND a.status <> 'retired'
                   AND (a.story_id = s.id OR a.story_id IS NULL))        AS writers,
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
            continue           # already dispatched. The Ticket Queue has it now
        blockers = []
        if not s["project"] or s["project_source"] != "confirmed":
            blockers.append("its project folder is still a guess. Confirm it here")
        if not s["writers"]:
            # Staffing is the Scrum Master's job (`wake.staff_stories`), so
            # this is a status, not an instruction to the PO.
            pending = rows(conn, """SELECT e.id, e.reason FROM escalations e
                                      WHERE e.story_id = ? AND e.kind = 'hire'
                                        AND e.resolved_at IS NULL
                                      ORDER BY e.id""", (s["id"],))
            blockers.append(
                (f"Ordis has proposed {len(pending)} people for this story. Each is "
                 f"waiting in this Inbox as its own card and you can take some and "
                 f"refuse others. The first one you approve is the lead.")
                if len(pending) > 1 else
                (f"Ordis has proposed someone. {pending[0]['reason']} It is waiting "
                 f"in this Inbox as its own card.")
                if pending else
                (f"nobody is hired to write in {s['project'] or 'that folder'} yet. "
                 f"Ordis picks a persona on the next pulse and brings you the name "
                 f"to approve. You can still hire someone yourself from Standby."))
        if halted:
            blockers.append("the colony is halted. Resume it in Macros")
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
    """Work in motion, next to the Inbox: tickets the colony is doing or
    staffed to do, and Notion pushes that have not left the machine. Both
    answer "did that go through?". Sorted running, then staffed, then
    waiting.
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
        # An open blocked ticket shows its reason here; a closed one is
        # finished and belongs in Completed.
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
    # `tier` records what the tick decided. `acted` records what happened:
    # `actions.wake` is null when no wake ran and carries `skipped` when one
    # stood down. Either way the hour was free, and the page labels it a tick.
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
    """The forge panel, including active skills and what they have earned."""
    return forge.board(conn)


def _spend(conn: sqlite3.Connection) -> dict[str, Any]:
    """The spend panel's breakdown by role. The time series is on `/api/spend`,
    so the shared snapshot does not carry chart buckets.
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


# ── the spend series ──────────────────────────────────────────────────────────
# Ledger timestamps are `datetime('now','localtime')`, so buckets are cut on
# the PO's wall clock. All grains roll up in Python from one hourly query,
# which returns only hours that had runs, so it stays small for any window.

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
    """Spend per bucket over the `span` buckets ending with the one `end` falls
    in. Empty buckets are zeros, not skipped; a quiet stretch is
    information. `end` defaults to now.
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

    # Runs on either side of the window are counted, so "0 runs" can be told
    # apart from a window pointed at the wrong period.
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
    """Divisions with their personas, so Standby can be browsed. The roster's
    short text ships in the snapshot; persona bodies are read from disk per
    click.
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


# The roster is two thirds of the state payload and changes only on a hire,
# retire or rescan. The snapshot carries its hash, and the page fetches
# `/api/roster/summary` when it moves.
_roster_cache: dict[str, Any] = {"rev": None, "text": "{}"}
_roster_lock = threading.Lock()


def _roster_head(conn: sqlite3.Connection) -> dict[str, Any]:
    """The roster's counts and revision, for the snapshot."""
    summary = _roster_summary(conn)
    text = json.dumps(summary, sort_keys=True, default=str)
    rev = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    with _roster_lock:
        _roster_cache.update(rev=rev, text=text)
    return {"total": summary["total"], "mine": summary["mine"], "rev": rev}


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
        # Separate from HALT: a Notion comment is not a token.
        "notion_write": control.get_control(conn, "notion_write", "1") == "1",
        # A forced beat runs on a thread for minutes, so the button can show
        # "running". The lock is shared with the scheduled beat.
        "pulse_running": control.pulse_running(),
        "outbox": outbox_mod.depth(conn),
        "notion_statuses": list(notion_mod.WRITABLE_STATUS),
    }


def _projects_cached() -> list[dict[str, Any]]:
    now = time.monotonic()
    if now - _project_cache["at"] > PROJECT_TTL_S:
        try:
            _project_cache["rows"] = projects_mod.scan()
        except Exception:
            _project_cache["rows"] = []
        _project_cache["at"] = now
    return _project_cache["rows"]


def fingerprint(state: dict[str, Any]) -> str:
    """What "changed" means for the live feed. Running time is computed in the
    browser from `started_at`; hashing it would push a frame every poll.
    """
    return _serialize(state)[0]


def _serialize(state: dict[str, Any]) -> tuple[str, str]:
    """The state as JSON, and that JSON's hash. One dump serves both."""
    text = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), text


# One snapshot per interval, shared by every client. `seq` grows with each
# change so the page can drop a response older than its newest frame.
_frame: dict[str, Any] = {"at": 0.0, "fp": None, "text": "", "seq": 0}
_frame_lock = threading.Lock()


def frame() -> tuple[str, str]:
    """The current state's (hash, JSON), rebuilt at most once per SSE interval."""
    with _frame_lock:
        if _frame["fp"] is None or time.monotonic() - _frame["at"] >= SSE_INTERVAL_S * 0.9:
            state = snapshot()
            fp, text = _serialize(state)
            if fp != _frame["fp"]:
                _frame["seq"] += 1
                _frame["fp"] = fp
                _frame["text"] = json.dumps({**state, "seq": _frame["seq"]},
                                            sort_keys=True, default=str)
            _frame["at"] = time.monotonic()
        return _frame["fp"], _frame["text"]


def _expire_frame() -> None:
    """Make the next `frame()` rebuild. Called after an action changes state."""
    with _frame_lock:
        _frame["at"] = 0.0


def _act(fn, *args, **kwargs) -> dict[str, Any]:
    """Run one control function in its own transaction. `Refused` becomes a 409
    with its reason, which is written to be shown to a person.
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
        _expire_frame()
        return {"ok": True, **(out if isinstance(out, dict) else {"result": out})}
    except control.Refused as exc:
        raise HTTPException(409, str(exc))
    finally:
        conn.close()


@router.get("/api/state")
def api_state() -> Response:
    return Response(frame()[1], media_type="application/json")


@router.get("/events")
async def events() -> StreamingResponse:
    async def stream():
        last = None
        while True:
            fp, text = await asyncio.to_thread(frame)
            if fp != last:
                last = fp
                yield f"event: state\ndata: {text}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(SSE_INTERVAL_S)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
