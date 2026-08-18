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
from fastapi.responses import HTMLResponse, StreamingResponse

from . import control, db, projects as projects_mod, roster as roster_mod

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


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict[str, Any] | None:
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
            "inbox": _inbox(conn),
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
    return {
        "sprint": sprint,
        "usage": usage,
        "spent": spent,
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
    groom = one(
        conn,
        "SELECT COUNT(*) n FROM stories WHERE status IN ('backlog','needs-criteria') "
        "AND (acceptance_criteria IS NULL OR acceptance_criteria = '')",
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
        SELECT e.*, s.title AS story_title, s.project, s.project_source, s.status AS story_status,
               t.title AS ticket_title,
               (SELECT COUNT(*) FROM po_messages m WHERE m.escalation_id = e.id)  AS messages,
               (SELECT COUNT(*) FROM po_messages m WHERE m.escalation_id = e.id
                  AND m.author = 'po' AND m.status = 'unread')                    AS awaiting_ordis,
               (SELECT m.body FROM po_messages m WHERE m.escalation_id = e.id
                  AND m.author = 'ordis' ORDER BY m.id DESC LIMIT 1)              AS last_reply,
               (SELECT m.at FROM po_messages m WHERE m.escalation_id = e.id
                  ORDER BY m.id DESC LIMIT 1)                                     AS last_message_at,
               CASE WHEN e.snoozed_until IS NOT NULL
                     AND e.snoozed_until > datetime('now','localtime')
                    THEN 1 ELSE 0 END                                             AS snoozed
          FROM escalations e
          LEFT JOIN stories s ON s.id = e.story_id
          LEFT JOIN tickets t ON t.id = e.ticket_id
         WHERE e.resolved_at IS NULL
         -- A snoozed item sorts to the back whatever its kind: "later" has to
         -- move something, or it is a button that does nothing but log.
         ORDER BY snoozed,
                  CASE e.kind WHEN 'write-approval' THEN 0 WHEN 'hire' THEN 1
                              WHEN 'decision' THEN 2 ELSE 3 END,
                  e.raised_at DESC
        """,
    )


def _pulses(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows(
        conn,
        "SELECT id, pulse_at, tier, finding, anomalies, tokens, duration_ms, "
        "       CASE WHEN detail IS NULL OR detail = '' THEN 0 ELSE 1 END AS has_detail "
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
        "max_boost": control.MAX_BOOST_POINTS,
        "recent": recent,
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
    )


@app.get("/api/thread")
def thread(escalation_id: int | None = None, story_id: int | None = None) -> dict[str, Any]:
    """The conversation about one item. Read-only, like everything on this side."""
    conn = _conn()
    try:
        return {"messages": control.thread(conn, escalation_id, story_id)}
    finally:
        conn.close()


@app.post("/api/act/halt")
def act_halt(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.halt, bool(body["on"]), str(body.get("reason") or ""))


@app.post("/api/act/allowance")
def act_allowance(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
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
