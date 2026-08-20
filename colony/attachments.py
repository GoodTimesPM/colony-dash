"""Screenshots and files the PO pastes into a reply.

A screenshot is frequently the entire message. "The title formatting broke" and
a picture of the broken title are not the same sentence, and the second one is
the one that can be acted on — up to now the dashboard could only take the
first, so every reply that was really about something *visible* had to be
retyped into prose and lost most of what it was.

Three rules hold the feature down:

  * **Files land under `.colony/attachments/` and nowhere else.** `resolve`
    re-checks containment on the way back out rather than trusting the name it
    stored, because the name makes a round trip through the browser and a value
    that has left the process is an input again when it returns.
  * **The stored name is generated, never the one the browser sent.** A pasted
    screenshot is always called `image.png`, so the original name is decoration;
    keeping it as the *label* and a random token as the *filename* means two
    pastes never overwrite each other and nothing user-supplied ever reaches the
    filesystem as a path.
  * **Nothing here decides anything**, so it is not in `control.py`. Writing a
    file is bookkeeping; the decision is the reply that references it.

The agent reads them with the `Read` tool off an absolute path in its work
order, rather than being handed base64 in the prompt: `.colony/` is inside the
read scope already, an image costs the same either way, and a prompt that
carries its evidence by reference stays readable in the ticket.
"""

from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path

from .db import ATTACHMENTS_DIR

# Eight megabytes is roughly a 4K screenshot as PNG. The ceiling exists because
# the whole file arrives as base64 in one JSON body, and a request large enough
# to matter should fail at the door with a sentence rather than somewhere deep
# in the ledger.
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
        raise Rejected(f"that file is {len(blob) // 1024}KB — the limit is "
                       f"{MAX_BYTES // 1024 // 1024}MB")

    label = (label or "file").strip()[:80] or "file"
    stem = _SAFE.sub("-", label).strip("-") or "file"
    name = f"{secrets.token_hex(6)}-{stem}"
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    (ATTACHMENTS_DIR / name).write_bytes(blob)
    return {"name": name, "label": label, "mime": mime,
            "bytes": len(blob), "kind": "image" if mime.startswith("image/") else "file"}


def resolve(name: str) -> Path:
    """The absolute path of a stored attachment, or `Rejected`.

    Containment is checked against the resolved parent rather than by inspecting
    the string, so `..`, a symlink and an absolute path all fail the same way.
    """
    root = ATTACHMENTS_DIR.resolve()
    try:
        path = (root / (name or "")).resolve()
    except OSError:
        raise Rejected("no such attachment")
    if path.parent != root or not path.is_file():
        raise Rejected("no such attachment")
    return path
