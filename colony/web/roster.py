"""The roster: browsing divisions and personas, writing and deleting one, and
rescanning the agency folder."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import Response

from .. import control, roster as roster_mod
from .common import _conn, _guard, _rw, one, rows
from .state import _project_cache, _roster_cache, _roster_lock, frame

router = APIRouter()

@router.get("/api/roster/summary")
def api_roster_summary(request: Request) -> Response:
    """Every division and persona. Sent in full only when its ETag has moved."""
    with _roster_lock:
        rev, text = _roster_cache["rev"], _roster_cache["text"]
    if rev is None:
        frame()
        with _roster_lock:
            rev, text = _roster_cache["rev"], _roster_cache["text"]
    etag = f'"{rev}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(text, media_type="application/json",
                    headers={"ETag": etag, "Cache-Control": "no-cache"})


@router.get("/api/roster")
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


@router.get("/api/persona")
def api_persona(slug: str = Query(..., max_length=200)) -> dict[str, Any]:
    """One persona, read from its file. The ledger stores the frontmatter; the
    body (how the persona works and what it refuses) is read on demand so it
    cannot go stale after a pull.
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
    """Split a persona body on its headings. Files are not uniform, so anything
    that fails to split comes back as one section.
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


@router.get("/api/roster/divisions")
def api_roster_divisions() -> dict[str, Any]:
    """The divisions the roster scan found, for the import panel's dropdown."""
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


@router.post("/api/act/persona")
def act_persona(body: dict = Body(...),
                x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Write one of this machine's own personas, then re-scan, so Standby sees
    it at once. It lands under `~/.colony-agents`, never in the
    agency-agents clone (see `roster.write_persona`).
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


@router.post("/api/act/persona-delete")
def act_persona_delete(body: dict = Body(...),
                       x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Delete one of this machine's own personas; agency ones are refused. Not
    loopback-guarded: it only removes a text file the user wrote.
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
    """Re-read both persona roots into `roster`. Shared by the rescan button
    and the import panel. `why` prefixes the ledger note, and the counts
    appended are the actual changes.
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


@router.post("/api/act/rescan")
def act_rescan(x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Re-read both persona roots. Changes only `roster`, so upstream edits and
    hand-added personas show without a restart.
    """
    _guard(x_colony)
    try:
        result = _rescan("roster rescan")
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, **result}
