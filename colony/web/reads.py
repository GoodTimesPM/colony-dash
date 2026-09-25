"""Detail views the drawer opens: a story, the spend chart, the file tree, a
project, a diff or patch, a pulse, an agent."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import control, projects as projects_mod
from .common import _conn, one, rows
from .state import SPAN, _series

router = APIRouter()

@router.get("/api/story/{story_id}")
def api_story(story_id: int) -> dict[str, Any]:
    """The detail drawer: the story, its timeline, its tickets, its runs
    (§9.3).
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
            # Everyone on this story, lead first.
            "crew": [
                {"id": a["id"], "role": a["role"], "roster_slug": a["roster_slug"],
                 "seat": a["seat"], "status": a["status"], "model": a["model"],
                 "story_id": a["story_id"], "max_tokens_run": a["max_tokens_run"],
                 "name": (one(conn, "SELECT name FROM roster WHERE slug = ?",
                              (a["roster_slug"],)) or {}).get("name") if a["roster_slug"]
                         else None}
                for a in control.team(conn, story_id)
            ],
        }
    finally:
        conn.close()


@router.get("/api/spend")
def api_spend(grain: str = Query("day"), span: int = Query(0),
              end: str = Query("")) -> dict[str, Any]:
    """The spend chart, at whichever grain the PO picked, ending wherever they put it."""
    if grain not in SPAN:
        raise HTTPException(400, f"grain must be one of {', '.join(SPAN)}")
    # An out-of-range span is clamped rather than swapped for the default: a
    # caller who asked for 9999 buckets wants "as far back as you go", not 30.
    span = max(2, min(400, span)) if span else SPAN[grain]

    # `end` accepts a bare date or a datetime. An unparseable one is a 400, not
    # a silent fall back to now.
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


@router.get("/api/projects")
def api_projects() -> dict[str, Any]:
    """Live working-tree state for every project. The file-manager panel."""
    return {"head": projects_mod.head(), "rows": projects_mod.scan(),
            "all": projects_mod.project_dirs()}


@router.get("/api/tree")
def api_tree(path: str = Query("", max_length=400)) -> dict[str, Any]:
    """One folder's children, loaded lazily. `projects_mod.safe_path` guards
    the filesystem, and its refusals become 400s.
    """
    try:
        return projects_mod.tree(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/file")
def api_file(path: str = Query(..., max_length=400)) -> dict[str, Any]:
    """One file's text. Read-only, size-capped, secrets excluded by name."""
    try:
        return projects_mod.read_file(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/project")
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
        # The baseline branch and sha for the drawer's header.
        "head": projects_mod.head(),
        "kinds": projects_mod.KIND_LONG,
        "commits": projects_mod.commits(name),
        "history": history,
    }


@router.get("/api/diff")
def api_diff(project: str = Query(..., max_length=200),
             path: str | None = Query(None, max_length=400)) -> dict[str, Any]:
    """A working-tree diff for the project or one file. `path` must sit under
    `project`, so the viewer stays scoped.
    """
    if project not in projects_mod.project_dirs():
        raise HTTPException(404, "no such project")
    if path:
        norm = path.replace("\\", "/")
        if not (norm == project or norm.startswith(project + "/")) or ".." in norm:
            raise HTTPException(400, "that path is not inside that project")
    return {"project": project, "path": path, "diff": projects_mod.diff(project, path)}


PATCH_MAX_CHARS = 400_000


def _diffstat(patch: str, scope: list[str] | None = None) -> dict[str, Any]:
    """Per-file adds and deletes, counted off the patch being approved rather
    than the stored `--stat` text, so the numbers describe what will land. A
    line starting with a single "+" is an addition; "+++" headers are caught
    first. Paths come from `worktree.patch_files`, the parse `apply_patch`
    checks scope against.
    """
    from .. import worktree

    names = worktree.patch_files(patch)
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            path = names[len(files)] if len(files) < len(names) else line[11:]
            cur = {"path": path, "added": 0, "removed": 0, "binary": False,
                   "verb": "changed",
                   "outside": bool(scope) and bool(worktree.outside_scope([path], scope))}
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
            "outside": sum(1 for f in files if f["outside"]),
        },
    }


@router.get("/api/patch")
def api_patch(escalation_id: int = Query(..., ge=1)) -> dict[str, Any]:
    """Everything the approval drawer shows about a patch: the agent's
    findings, the diff counts, the run's cost, and the patch text.
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

    # The agent's own account of the work. The diff below is what happened; this
    # is the only place that says which criteria it believes it met.
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
        "stat": _diffstat(patch, proposal.get("scope")),
        "scope": proposal.get("scope"),
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


@router.get("/api/pulse/{pulse_id}")
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


@router.get("/api/agent/{agent_id}")
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
