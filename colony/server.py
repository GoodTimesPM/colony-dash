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
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from . import (attachments as attach, control, db, forge, notion as notion_mod,
               outbox as outbox_mod, projects as projects_mod, roster as roster_mod)

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
            "forge": _forge(conn),
            "spend": _spend(conn),
            "roster": _roster_summary(conn),
            "controls": _controls(conn),
            "projects": _projects_cached(),
        }
    finally:
        conn.close()


def _sprint(conn: sqlite3.Connection) -> dict[str, Any]:
    sprint = one(conn, "SELECT * FROM sprints WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    usage = one(conn, "SELECT * FROM usage_samples ORDER BY sampled_at DESC LIMIT 1")

    spent = {"tokens": 0, "usd": 0.0, "runs": 0}
    if sprint:
        # By run date inside the window, not by story.sprint_id: Notion stories
        # arrive with no sprint attached. Same query the CLI settled on.
        row = one(
            conn,
            """
            SELECT COALESCE(SUM(COALESCE(chargeable_tokens, total_tokens)), 0) AS tokens,
                   COALESCE(SUM(cost_usd), 0)                                 AS usd,
                   COUNT(*)                                                   AS runs
              FROM runs
             WHERE date(started_at) BETWEEN ? AND ?
            """,
            (sprint["starts_on"], sprint["ends_on"]),
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
    # answer to the question the PO actually has when he looks at the board —
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
    # the PO overruling his own board from here; a settled one is the board
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
               -- and the last answer belong to the conversation, and the
               -- conversation belongs to the story (see `control.thread`).
               (SELECT COUNT(*) FROM po_messages m
                 WHERE m.escalation_id = e.id OR m.story_id = e.story_id)          AS messages,
               (SELECT COUNT(*) FROM po_messages m
                 WHERE (m.escalation_id = e.id OR m.story_id = e.story_id)
                   AND m.author = 'po' AND m.status = 'unread')                    AS awaiting_ordis,
               (SELECT m.body FROM po_messages m
                 WHERE (m.escalation_id = e.id OR m.story_id = e.story_id)
                   AND m.author = 'ordis' ORDER BY m.id DESC LIMIT 1)              AS last_reply,
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
                              WHEN 'decision' THEN 2 ELSE 3 END,
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
    the PO thinks the work has started; a tile that says "ready — except nobody
    is hired to write in that folder" is the difference between a colony that is
    waiting on him and a colony he believes is working.
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
            blockers.append(
                f"nobody is hired to write in {s['project'] or 'that folder'} — open "
                f"Standby, pick a persona and hire them with write scope on it")
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
               t.po_message_id,
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
         WHERE t.status IN ('open','staffed','running','blocked')
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
    return rows(
        conn,
        "SELECT id, pulse_at, tier, finding, anomalies, tokens, duration_ms, "
        "       CASE WHEN detail IS NULL OR detail = '' THEN 0 ELSE 1 END AS has_detail "
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
    by_day = rows(
        conn,
        """
        SELECT date(started_at) AS day,
               COALESCE(SUM(COALESCE(chargeable_tokens, total_tokens)), 0) AS tokens,
               COALESCE(SUM(cost_usd), 0)                                  AS usd,
               COUNT(*)                                                    AS runs
          FROM runs
         GROUP BY day ORDER BY day DESC LIMIT 14
        """,
    )
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
    return {"by_day": list(reversed(by_day)), "by_role": by_role}


def _roster_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Divisions with their personas, so Standby can be browsed and not only searched.

    The whole roster is ~270 rows of short text — small enough to ship in the
    snapshot and let the browser open a division instantly, which is the point
    of a dropdown. The persona *body* is not included; that is a per-click read
    off disk, because 270 markdown files is a different order of payload.
    """
    people = rows(
        conn,
        "SELECT slug, name, division, description, emoji, color, vibe FROM roster "
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
        "divisions": [
            {"division": d, "n": len(v), "people": v}
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
    the PO then abandons leaves a file in `.colony/attachments/` and nothing in
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


@app.get("/api/thread")
def thread(escalation_id: int | None = None, story_id: int | None = None) -> dict[str, Any]:
    """The conversation about one item. Read-only, like everything on this side."""
    conn = _conn()
    try:
        return {"messages": control.thread(conn, escalation_id, story_id),
                "entries": control.conversation(conn, escalation_id, story_id)}
    finally:
        conn.close()


@app.post("/api/act/halt")
def act_halt(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.halt, bool(body["on"]), str(body.get("reason") or ""))


@app.post("/api/act/allowance")
def act_allowance(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    # Two ways to say the same thing: a step off the baseline, or the number the
    # PO typed into the box. The box is the one that does not require him to
    # know what the baseline is.
    if "allowance" in body:
        return _act(control.set_allowance_pct, float(body["allowance"]))
    return _act(control.set_allowance, float(body["boost"]))


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
        max_tokens_run=int(body.get("max_tokens_run") or 120000),
        notes=body.get("notes") or None,
    )


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


@app.post("/api/act/rescan")
def act_rescan(x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Re-read the agency-agents install. The one write that isn't a decision.

    It changes only the `roster` table — résumés, not employees — and a persona
    whose file changed upstream is something the PO should see rather than
    discover the next time they hire.
    """
    _guard(x_colony)
    conn = _rw()
    try:
        conn.execute("BEGIN")
        try:
            result = roster_mod.sync(conn)
            control._record(conn, "note", "colony", None,
                            f"roster rescan: {result['total']} personas, "
                            f"{len(result['added'])} added, {len(result['changed'])} changed")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        _project_cache["at"] = 0.0
        return {"ok": True, **result}
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc))
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


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
