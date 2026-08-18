"""The dashboard's backend: FastAPI over the ledger, read-only.

M2 is a *read view*. Every endpoint here opens the ledger with `read_only=True`,
which is not a convention but an enforcement — the dashboard can never be the
reason state changed (ARCHITECTURE.md §9.2). The approval controls that do write
arrive in M3, behind the Inbox gate, and they will be the only exception.

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

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from . import db

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
PULSE_LIMIT = 40
SSE_INTERVAL_S = 2.0


def _conn() -> sqlite3.Connection:
    return db.connect(read_only=True)


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _json_col(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


# ── the snapshot ──────────────────────────────────────────────────────────────


def snapshot() -> dict[str, Any]:
    """Everything the page renders, in one read. §9.2's panel table, in order."""
    conn = _conn()
    try:
        return {
            "sprint": _sprint(conn),
            "colony": _colony(conn),
            "board": _board(conn),
            "inbox": _inbox(conn),
            "pulses": _pulses(conn),
            "forge": _forge(conn),
            "spend": _spend(conn),
            "roster": _roster_summary(conn),
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

    return {
        "sprint": sprint,
        "usage": usage,
        "spent": spent,
        # The bar measures the *allowance*, not the week: 35% of the window is
        # the colony's ceiling, so 35% consumed should read as full, not a third.
        "allowance_pct": (sprint or {}).get("budget_pct", 35.0),
    }


def _colony(conn: sqlite3.Connection) -> dict[str, Any]:
    """Running agents first, then everyone on the books. §9.3's active rail."""
    running = rows(
        conn,
        """
        SELECT r.id, r.agent_role, r.model, r.status, r.ticket_id, r.started_at,
               COALESCE(r.chargeable_tokens, r.total_tokens) AS tokens,
               t.title AS ticket_title, t.intent,
               a.max_tokens_run, a.avatar_seed, a.project,
               ro.color, ro.emoji
          FROM runs r
          LEFT JOIN tickets t ON t.id = r.ticket_id
          LEFT JOIN agents  a ON a.role = r.agent_role
          LEFT JOIN roster ro ON ro.slug = a.roster_slug
         WHERE r.status = 'running'
         ORDER BY r.started_at
        """,
    )
    standby = rows(
        conn,
        """
        SELECT a.id, a.role, a.project, a.model, a.write_capable, a.max_tokens_run,
               a.avatar_seed, a.status, ro.color, ro.emoji,
               (SELECT COUNT(*) FROM runs r WHERE r.agent_role = a.role) AS run_count,
               (SELECT COALESCE(SUM(COALESCE(r.chargeable_tokens, r.total_tokens)), 0)
                  FROM runs r WHERE r.agent_role = a.role)               AS lifetime_tokens
          FROM agents a
          LEFT JOIN roster ro ON ro.slug = a.roster_slug
         WHERE a.status != 'retired'
         ORDER BY a.project IS NOT NULL, a.role
        """,
    )
    return {"running": running, "standby": standby}


def _board(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = {r["status"]: r["n"] for r in rows(
        conn, "SELECT status, COUNT(*) n FROM stories GROUP BY status"
    )}
    stories = rows(
        conn,
        """
        SELECT s.id, s.title, s.status, s.project, s.project_source, s.priority,
               s.est_tokens, s.blocked_reason, s.notion_status, s.updated_at,
               (SELECT COUNT(*) FROM story_events e WHERE e.story_id = s.id)   AS events,
               (SELECT COALESCE(SUM(e.tokens), 0) FROM story_events e
                 WHERE e.story_id = s.id)                                      AS tokens
          FROM stories s
         WHERE s.status NOT IN ('rejected','archived')
         ORDER BY s.priority, s.id
        """,
    )
    return {
        "columns": [{"status": s, "n": counts.get(s, 0)} for s in BOARD_ORDER],
        "stories": stories,
    }


def _inbox(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows(
        conn,
        """
        SELECT e.*, s.title AS story_title, s.project, t.title AS ticket_title
          FROM escalations e
          LEFT JOIN stories s ON s.id = e.story_id
          LEFT JOIN tickets t ON t.id = e.ticket_id
         WHERE e.resolved_at IS NULL
         ORDER BY e.raised_at DESC
        """,
    )


def _pulses(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows(
        conn,
        "SELECT id, pulse_at, tier, finding, anomalies, tokens, duration_ms "
        "FROM pulses ORDER BY pulse_at DESC, id DESC LIMIT ?",
        (PULSE_LIMIT,),
    )


def _forge(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows(
        conn,
        "SELECT id, name, slug, status, summary, times_used, wins, losses, tokens_saved "
        "FROM skills WHERE status IN ('candidate','drafted') ORDER BY tokens_saved DESC",
    )


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
    divisions = rows(
        conn,
        "SELECT division, COUNT(*) n FROM roster GROUP BY division ORDER BY n DESC",
    )
    total = one(conn, "SELECT COUNT(*) n FROM roster")
    return {"divisions": divisions, "total": (total or {}).get("n", 0)}


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
