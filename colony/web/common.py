"""What every router shares: ledger connections, body parsing, and serving
files from `ui/` with an ETag."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response

from .. import db, desktop as desktop_mod

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

# Which build this process is, read once at import. `desktop.stamp()` reads the
# files on disk, so asking later would describe the checkout, not the running
# interpreter. See `desktop._stale`.
BUILD_STAMP = desktop_mod.stamp()
STARTED_AT = datetime.now().isoformat(timespec="seconds")


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


def _guard(header: str | None) -> None:
    """The write door's lock. See the module docstring for why a header is enough."""
    if header != "1":
        raise HTTPException(403, "PO actions require the dashboard's own page")


_REQUIRED = object()


def _need(body: dict, key: str) -> Any:
    """A required field of an action body. Missing is the caller's mistake, a 400."""
    if body.get(key) is None:
        raise HTTPException(400, f"missing field: {key}")
    return body[key]


def _num(body: dict, key: str, kind: type = int, default: Any = _REQUIRED) -> Any:
    """A numeric field of an action body, as `kind`, or a 400. `default`
    replaces a missing or empty field; 0 is kept, since un-snooze sends it.
    """
    value = body.get(key)
    if value is None or value == "":
        if default is _REQUIRED:
            raise HTTPException(400, f"missing field: {key}")
        return default
    if isinstance(value, bool):
        raise HTTPException(400, f"{key} must be a number")
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{key} must be a number") from None


# The page builds its DOM with textContent and loads only its own files, so the
# policy can be tight. Inline style attributes in index.html are the one
# allowance.
PAGE_CSP = ("default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "connect-src 'self'; font-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


# UI files, keyed on their mtime and size. `no-cache` makes the browser ask
# every load, so an edit shows on the next refresh, and the ETag turns an
# unchanged file into a 304 with no disk read.
_asset_cache: dict[str, tuple[tuple[int, int], bytes, str]] = {}


def _asset(name: str, media_type: str, request: Request,
           headers: dict[str, str] | None = None) -> Response:
    path = UI_DIR / name
    st = path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _asset_cache.get(name)
    if cached is None or cached[0] != stamp:
        body = path.read_bytes()
        cached = (stamp, body, '"%s"' % hashlib.sha256(body).hexdigest()[:16])
        _asset_cache[name] = cached
    _, body, etag = cached
    out = {"Cache-Control": "no-cache", "ETag": etag, **(headers or {})}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=out)
    return Response(body, media_type=media_type, headers=out)
