"""Screenshots and files the PO pastes into a reply.

  * Files live only under `.colony/attachments/`. `resolve` re-checks
    containment, since the stored name round-trips through the browser.
  * The stored name is random; the browser's name is only a label.
  * Not in `control.py`: storing a file decides nothing.

Agents open them with `Read` by absolute path from the work order rather
than receiving base64 in the prompt.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from pathlib import Path

from .db import ATTACHMENTS_DIR
from .prompt import render as render_prompt

# Roughly a 4K PNG screenshot. The body is base64 JSON, so fail early.
MAX_BYTES = 8 * 1024 * 1024
MAX_PER_MESSAGE = 6

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DATA_URL = re.compile(r"^data:([\w.+/-]*);base64,(.*)$", re.S)


class Rejected(ValueError):
    """The upload is not something we will store. Always says why."""


def save(label: str, data_url: str) -> dict:
    """Store one pasted file. Returns the row that goes on the message."""
    m = _DATA_URL.match((data_url or "").strip())
    if not m:
        raise Rejected("that did not arrive as a data: URL")
    mime, b64 = m.group(1) or "application/octet-stream", m.group(2)
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        raise Rejected("that file did not decode")
    if not blob:
        raise Rejected("that file was empty")
    if len(blob) > MAX_BYTES:
        raise Rejected(f"that file is {len(blob) // 1024}KB. The limit is "
                       f"{MAX_BYTES // 1024 // 1024}MB")

    label = (label or "file").strip()[:80] or "file"
    stem = _SAFE.sub("-", label).strip("-") or "file"
    name = f"{secrets.token_hex(6)}-{stem}"
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    (ATTACHMENTS_DIR / name).write_bytes(blob)
    return {"name": name, "label": label, "mime": mime,
            "bytes": len(blob), "kind": "image" if mime.startswith("image/") else "file"}


def for_story(conn, story_id: int | None) -> list[dict]:
    """Every file attached anywhere in one story's thread. Conversations are
    scoped to the story, and so is their evidence. Rows whose file is gone
    are dropped.
    """
    if not story_id:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in conn.execute(
        "SELECT at, attachments FROM po_messages "
        "WHERE story_id = ? AND attachments IS NOT NULL ORDER BY id",
        (story_id,),
    ):
        try:
            files = json.loads(row["attachments"] or "[]")
        except (TypeError, ValueError):
            continue
        for f in files or []:
            name = str(f.get("name") or "")
            if not name or name in seen:
                continue
            try:
                path = resolve(name)
            except Rejected:
                continue
            seen.add(name)
            out.append({**f, "at": row["at"], "path": str(path)})
    return out


def evidence(files: list[dict]) -> str:
    """The prompt section, or "" when there are none. Phrased as an
    instruction, because agents otherwise forget to open them.
    """
    if not files:
        return ""
    return render_prompt("attachments", files="\n".join(
        f"  {f['path']}   ({f.get('label') or 'file'}, "
        f"{f.get('kind') or 'file'}, pasted {f.get('at') or 'earlier'})"
        for f in files))


def resolve(name: str) -> Path:
    """Absolute path of a stored attachment, or `Rejected`. Containment is
    checked on the resolved path, so `..`, symlinks and absolute paths all
    fail.
    """
    root = ATTACHMENTS_DIR.resolve()
    try:
        path = (root / (name or "")).resolve()
    except OSError:
        raise Rejected("no such attachment")
    if path.parent != root or not path.is_file():
        raise Rejected("no such attachment")
    return path
