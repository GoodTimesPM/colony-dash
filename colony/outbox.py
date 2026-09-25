"""The Notion outbox: queue here, send from the tick (ARCHITECTURE.md §5.4).

Dashboard actions must be instant and must not fail because Notion is down,
so writes are queued as rows and the hourly tick sends them. That gives
retries, an audit trail, and a kill switch (`controls.notion_write = 0`).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import notion

# Per tick, so a backlog drains over time without stalling the heartbeat.
FLUSH_LIMIT = 12

# Stop retrying after this many failures so a bad row cannot eat every flush.
MAX_ATTEMPTS = 11


def _reason(exc: Exception) -> str:
    """The error text for a failed row's tile. Notion's refusals are already
    sentences; other errors keep their class name so their origin is clear.
    """
    if isinstance(exc, (notion.NotionError, notion.NotionRefused)):
        return str(exc)[:400]
    return f"{type(exc).__name__}: {exc}"[:400]


def queue(conn: sqlite3.Connection, *, story_id: int | None, page_id: str,
          kind: str, payload: dict, source: str = "dashboard") -> int:
    """Queue one write and return its row id. Validation happens at send time
    in `notion.py`, so a rule change cannot strand queued rows.
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
        # Resolve the block at send time; a deleted checkbox becomes a skip,
        # not a 404.
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
    """Send what is waiting and return a summary for the pulse log. Never
    raises.
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
