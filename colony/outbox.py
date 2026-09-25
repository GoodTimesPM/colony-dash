"""The Notion outbox: queue here, send from the tick.

Two modules are not allowed to make network calls. `control.py` is one. A
dashboard button must be instant, must be transactional, and must not fail
because Notion had a bad minute. `wake.py` is the other in spirit: it spends
tokens and should spend them on thinking, not on HTTP.

So a write to Notion is queued as a row and flushed by the tick, which already
talks to Notion and already runs every hour for free. That buys three things a
direct call could not:

* **Retries.** A failed send stays queued with its error on it, and the next
  tick tries again. Nothing is lost because the wifi was out at 3am.
* **An audit trail.** "I marked that Done from my phone" is a row with a
  timestamp, not a memory.
* **A kill switch.** `controls.notion_write = 0` stops every upward write
  colony-wide without touching a line of code, and the queue simply grows until
  it is turned back on.

ARCHITECTURE.md §5.4.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import notion

# How many messages one tick will send. The tick is supposed to be quick and
# free; a backlog of two hundred comments should drain over an hour, not stall
# the heartbeat while it does.
FLUSH_LIMIT = 12

# After this many failures a row stops being retried. Something that has been
# refused eleven times is not going to succeed on the twelfth, and a permanently
# poisoned row would otherwise consume the whole flush budget forever.
MAX_ATTEMPTS = 11


def _reason(exc: Exception) -> str:
    """What to write on a failed row, for a person reading a tile at a glance.

    Notion's own refusals already read as sentences. "403 restricted_resource:
    Insufficient permissions for this endpoint" names both the problem and the
    fix. So stamping `NotionError:` in front of them only spends characters the
    tile does not have. Anything else keeps its class name, because a bare
    `[Errno 11001] getaddrinfo failed` needs the word that says it came from
    Python and not from Notion.
    """
    if isinstance(exc, (notion.NotionError, notion.NotionRefused)):
        return str(exc)[:400]
    return f"{type(exc).__name__}: {exc}"[:400]


def queue(conn: sqlite3.Connection, *, story_id: int | None, page_id: str,
          kind: str, payload: dict, source: str = "dashboard") -> int:
    """Write one intention down. Returns the outbox row id.

    Deliberately does no validation of `payload` beyond it being JSON-able: the
    validation that matters lives in `notion.py`, at the moment of the actual
    call, so a rule change never leaves a queue full of rows that were legal
    when they were written and are not now.
    """
    cur = conn.execute(
        """INSERT INTO notion_outbox (story_id, page_id, kind, payload, source)
           VALUES (?,?,?,?,?)""",
        (story_id, page_id, kind, json.dumps(payload), source),
    )
    return int(cur.lastrowid)


def pending(conn: sqlite3.Connection, limit: int = FLUSH_LIMIT) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM notion_outbox
            WHERE sent_at IS NULL AND attempts < ?
            ORDER BY queued_at ASC, id ASC LIMIT ?""",
        (MAX_ATTEMPTS, limit),
    ).fetchall()


def depth(conn: sqlite3.Connection) -> dict[str, int]:
    """How much is waiting, and how much has given up. Both belong on the page."""
    row = conn.execute(
        """SELECT COALESCE(SUM(sent_at IS NULL AND attempts <  ?), 0) AS waiting,
                  COALESCE(SUM(sent_at IS NULL AND attempts >= ?), 0) AS stuck
             FROM notion_outbox""",
        (MAX_ATTEMPTS, MAX_ATTEMPTS),
    ).fetchone()
    return {"waiting": int(row["waiting"]), "stuck": int(row["stuck"])}


def _send(row: sqlite3.Row, payload: dict, status_kind: str) -> None:
    kind = row["kind"]
    if kind == "status":
        notion.set_status(row["page_id"], payload["status"], kind=status_kind)
    elif kind == "comment":
        notion.add_comment(row["page_id"], payload["text"])
    elif kind == "check":
        # The block id is resolved at send time, not at queue time. A checkbox
        # the PO deleted between the click and the flush should be a skip, and
        # a stored id would instead be a 404 retried eleven times.
        block = payload.get("block_id") or notion.find_block(row["page_id"], payload["item"])
        if not block:
            raise notion.NotionRefused(
                f"no to-do on the page reads {payload.get('item', '')!r} any more"
            )
        notion.check_item(block, payload.get("checked", True))
    else:  # unreachable while the CHECK constraint holds; loud if it ever does not
        raise notion.NotionRefused(f"unknown outbox kind {kind!r}")


def flush(conn: sqlite3.Connection, *, enabled: bool = True,
          limit: int = FLUSH_LIMIT) -> dict[str, Any]:
    """Send what is waiting. Returns what the pulse log should say about it.

    Never raises. A tick that dies because Notion was slow is a tick that stops
    doing the eleven other free things it was going to do.
    """
    result: dict[str, Any] = {"sent": 0, "failed": 0, "held": 0, "error": None}
    rows = pending(conn, limit)
    if not rows:
        return result
    if not enabled:
        result["held"] = len(rows)
        return result

    try:
        status_kind = notion.status_property_kind()
    except notion.NotionUnconfigured as exc:
        result["held"] = len(rows)
        result["error"] = str(exc)
        return result
    except Exception as exc:
        result["held"] = len(rows)
        result["error"] = _reason(exc)
        return result

    for row in rows:
        payload = json.loads(row["payload"])
        try:
            _send(row, payload, status_kind)
        except Exception as exc:
            conn.execute(
                "UPDATE notion_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (_reason(exc), row["id"]),
            )
            result["failed"] += 1
            continue
        conn.execute(
            """UPDATE notion_outbox
                  SET sent_at = datetime('now','localtime'),
                      attempts = attempts + 1, last_error = NULL
                WHERE id = ?""",
            (row["id"],),
        )
        result["sent"] += 1
        if row["story_id"]:
            conn.execute(
                """INSERT INTO story_events (story_id, kind, summary, detail)
                   VALUES (?, 'note', ?, ?)""",
                (row["story_id"], f"pushed to Notion: {row['kind']}", row["payload"]),
            )
    return result


def recent(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    """The tail of the queue for the dashboard, newest first."""
    rows = conn.execute(
        """SELECT o.*, s.title AS story_title
             FROM notion_outbox o LEFT JOIN stories s ON s.id = o.story_id
            ORDER BY o.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            pass
        d["state"] = ("sent" if r["sent_at"]
                      else "stuck" if r["attempts"] >= MAX_ATTEMPTS
                      else "waiting")
        out.append(d)
    return out
