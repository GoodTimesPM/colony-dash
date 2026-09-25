"""The PO's side of a story: decisions, replies, attachments, the thread,
dropping and restoring, and the Notion write-back."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import FileResponse

from .. import attachments as attach, control, outbox as outbox_mod
from .common import _conn, _guard, _need, _num, _rw, one, rows
from .state import (MIRROR_NOTES, _act, _episode_window, _findings, _json_list,
                    _project_cache)

router = APIRouter()

# ── deciding and replying ─────────────────────────────────────────────────────


@router.post("/api/act/decide")
def act_decide(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    out = _act(control.decide, _num(body, "escalation_id", int), str(_need(body, "decision")),
               str(body.get("note") or ""),
               _num(body, "snooze_hours", float, 8))
    esc_id = out.pop("run_pending", None)
    if esc_id:
        # Run with no transaction open, then record in a short one, so a
        # 90-second command does not hold the ledger's write lock.
        conn = _rw()
        try:
            result = control.run_command(conn, esc_id)
        finally:
            conn.close()
        out["outcome"] = _act(control.record_run, esc_id, result)["result"]
    return out


@router.post("/api/act/confirm-project")
def act_confirm(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Name the folder. Optionally create it first. See `control.create_project`."""
    _guard(x_colony)
    if body.get("create"):
        made = _act(control.create_project, str(_need(body, "project")),
                    why=str(body.get("why") or ""))
        _project_cache["at"] = 0.0
        if not body.get("story_id"):
            return made
    return _act(control.confirm_project, _num(body, "story_id", int), str(_need(body, "project")))


@router.post("/api/act/story")
def act_story(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """File a story without going through Notion. See `control.create_story`."""
    _guard(x_colony)
    return _act(
        control.create_story,
        title=str(body.get("title") or ""),
        description=str(body.get("description") or ""),
        project=str(body.get("project") or ""),
        priority=_num(body, "priority", int, 3),
    )


@router.post("/api/act/reply")
def act_reply(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Say something to Ordis about an Inbox item. Queued for the next wake."""
    _guard(x_colony)
    return _act(
        control.reply,
        escalation_id=_num(body, "escalation_id", int) if body.get("escalation_id") else None,
        story_id=_num(body, "story_id", int) if body.get("story_id") else None,
        body=str(body.get("body") or ""),
        attachments=list(body.get("attachments") or []),
    )


@router.post("/api/upload")
def upload(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Take one pasted file and put it on disk. Decides nothing.

    Separate from replying so a paste shows as a thumbnail at once. An
    abandoned upload leaves a file in `.colony/attachments/` and nothing in
    the ledger.
    """
    _guard(x_colony)
    try:
        return {"ok": True, "file": attach.save(str(body.get("name") or "file"),
                                                str(body.get("data") or ""))}
    except attach.Rejected as exc:
        raise HTTPException(400, str(exc))


# Types a browser may show inline. SVG is left out because it can carry script.
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
                "application/pdf", "text/plain"}


@router.get("/api/attachment/{name}")
def attachment(name: str) -> FileResponse:
    """Serve one stored file back to the page, for the thumbnail in the thread.

    Anything outside `INLINE_TYPES` downloads instead of rendering, and
    every response is sandboxed, so an uploaded .html or .svg cannot run on
    the dashboard's origin.
    """
    import mimetypes

    try:
        path = attach.resolve(name)
    except attach.Rejected as exc:
        raise HTTPException(404, str(exc))
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    inline = media in INLINE_TYPES
    return FileResponse(
        path,
        media_type=media if inline else "application/octet-stream",
        content_disposition_type="inline" if inline else "attachment",
        filename=path.name,
        headers={"Content-Security-Policy": "default-src 'none'; sandbox",
                 "X-Content-Type-Options": "nosniff"},
    )


def _thread_state(conn: sqlite3.Connection, escalation_id: int | None,
                  story_id: int | None) -> dict[str, Any] | None:
    """Whether the work this conversation is about can move, and what stops it.

    A row at the top of the conversation, with its own severity so the page
    can colour it without re-deriving this. `moving` is returned too, so a
    missing banner never has to mean "all clear".
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
                                  AND status <> 'retired'
                                  AND (story_id = ? OR story_id IS NULL)""",
                      (s["project"], story_id))["n"]

    # Worst first: `blocked` means nothing moves until the PO answers;
    # `waiting` means it waits on a decision; `moving` is the good state.
    if s["dropped_at"] or s["settled_as"]:
        level = "settled"
        headline = f"this story is {s['settled_as'] or 'dropped'}. Nothing is running"
    elif s["status"] == "needs-info" or any(q["kind"] == "needs-info" for q in open_qs):
        level = "blocked"
        headline = "blocked. It cannot start until this is answered"
    elif s["status"] == "ready" and not writers:
        level = "waiting"
        headline = "criteria accepted. Waiting on a writer to be hired"
    elif any(q["kind"] in ("decision", "hire", "write-approval", "brief-changed")
             for q in open_qs):
        level = "waiting"
        headline = "waiting on your decision"
    elif s["status"] in ("backlog", "needs-criteria"):
        level = "moving"
        headline = "in the groom queue. An agent picks it up on the next pulse"
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
        # Parked with a reason but no card; `pulse.ensure_blocked_visible`
        # repairs that next tick. The reason is shown until then.
        asks.insert(0, {"id": None, "kind": "needs-info",
                        "text": s["blocked_reason"], "raised_at": None})
    if s["project"] and s["project_source"] != "confirmed":
        asks.append({"id": None, "kind": "project",
                     "text": f"the folder {s['project']}/ is still a guess. An "
                             f"inference cannot authorise a write",
                     "raised_at": None})

    return {"story_id": story_id, "title": s["title"], "status": s["status"],
            "level": level, "headline": headline,
            "blocked_reason": s["blocked_reason"], "asks": asks,
            "project": s["project"], "project_source": s["project_source"]}


@router.get("/api/thread")
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
    """Everything that happened on a story between two moments, in one order:
    the conversation from `control.conversation` merged with `story_events`,
    because a decision reads wrong without the question before it.
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


@router.get("/api/completed/detail")
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


# ── dropping, and talking back to Notion ──────────────────────────────────────


@router.post("/api/act/drop")
def act_drop(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Take a story off the board. `notion_status` optionally says so upward too."""
    _guard(x_colony)
    return _act(control.drop_story, _num(body, "story_id", int),
                reason=str(body.get("reason") or ""),
                notion_status=(body.get("notion_status") or None))


@router.post("/api/act/restore")
def act_restore(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.restore_story, _num(body, "story_id", int))


@router.post("/api/act/notion")
def act_notion(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Queue one write upward. The tick sends it; this only writes a row."""
    _guard(x_colony)
    kind = str(body.get("kind") or "comment")
    payload = {k: body[k] for k in ("status", "text", "item", "checked") if k in body}
    return _act(control.queue_notion, story_id=_num(body, "story_id", int), kind=kind,
                payload=payload)


@router.post("/api/act/notion-write")
def act_notion_write(body: dict = Body(...),
                     x_colony: str | None = Header(None)) -> dict[str, Any]:
    """The switch for the whole upward direction. Off holds the queue, never drops it."""
    _guard(x_colony)
    return _act(control.set_notion_write, bool(_need(body, "on")))


@router.post("/api/act/reask")
def act_reask(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Send a stale question back to Ordis rather than answer the wrong question."""
    _guard(x_colony)
    return _act(control.reask, _num(body, "escalation_id", int))


@router.get("/api/outbox")
def api_outbox() -> dict[str, Any]:
    """What the colony has said upward lately, and what is still waiting."""
    conn = _conn()
    try:
        return {"rows": outbox_mod.recent(conn, 25), **outbox_mod.depth(conn)}
    finally:
        conn.close()
