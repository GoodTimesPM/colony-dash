"""Read the Notion intake board, and write a little back to it.

The tick uses the REST API with an integration token from `.env`:

    NOTION_TOKEN=ntn_...
    NOTION_DATABASE_ID=1d23280a-add3-41cb-bd14-67c771ee6d88

Writes are narrow: set a row's Status, tick a verified checkbox, comment. No
creating, deleting or editing the brief, since the board is where the PO
states intent. Writes go through `notion_outbox` and the tick, never
control.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from .mirror import load_env

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DEFAULT_DATABASE_ID = "1d23280a-add3-41cb-bd14-67c771ee6d88"

# Status = "In Progress" is the only signal to work a row. ARCHITECTURE.md §5.1.
WORKABLE_STATUS = "In Progress"
RESEARCH_STATUS = "Exploring"

# The other five statuses file a row (finished, parked, not begun). A filed row
# is the absence of a request and must not become work.
SETTLED_STATUS = {
    "Done":        "done",
    "Shipped":     "done",
    "Shelved":     "shelved",
    "New":         "not-started",
    "Not started": "not-started",
}


def ledger_status(notion_status: str | None) -> str:
    """The colony workflow status for a Notion row. Independent of filing,
    which `stories.settled_as` records, so a reopened story gets its old
    status back.
    """
    return "backlog" if notion_status == WORKABLE_STATUS else "needs-criteria"

# Statuses the dashboard may set, in board order. Only the PO's own buttons
# queue a status, so no agent can move a row into its own intake filter. Every
# entry must exist on the real select: Notion creates unknown options.
WRITABLE_STATUS = ("In Progress", "Exploring", "Done", "Shipped", "Shelved",
                   "New", "Not started")

PRIORITY_RANK = {"High": 1, "Medium": 2, "Low": 3}

# Notion rate-limits at about three requests a second and answers 429 with a
# Retry-After header. A few short waits keep a busy flush from failing rows.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_MAX_WAIT_S = 10.0


class NotionUnconfigured(RuntimeError):
    """No token. The tick reports this and carries on. It is not a failure."""


class NotionRefused(RuntimeError):
    """The colony asked Notion for something its own rules forbid."""


class NotionError(RuntimeError):
    """An API call was refused, carrying Notion's own explanation."""


def _why(exc: urllib.error.HTTPError) -> str:
    """Notion's `code` and `message` for a failed call, or the bare status.
    Nothing from the request, including the Authorization header, can reach
    it.
    """
    try:
        body = json.loads(exc.read())
    except Exception:
        body = {}
    code = body.get("code") or ""
    message = (body.get("message") or "").strip()
    if not code and not message:
        return f"HTTP {exc.code} {exc.reason}"
    return f"{exc.code} {code}: {message}".strip()


def _request(path: str, payload: dict | None = None, *, method: str | None = None) -> dict:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise NotionUnconfigured("NOTION_TOKEN is not set")

    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < RATE_LIMIT_RETRIES:
                time.sleep(_retry_after(exc))
                continue
            _raise_for(exc)
    raise AssertionError("unreachable")


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """Seconds Notion asked us to wait, capped so a pulse never stalls long."""
    try:
        wait = float(exc.headers.get("Retry-After") or 1)
    except (TypeError, ValueError):
        wait = 1.0
    return max(0.0, min(wait, RATE_LIMIT_MAX_WAIT_S))


def _raise_for(exc: urllib.error.HTTPError) -> None:
    """Raise with Notion's explanation from the response body, which urllib
    drops; a bare 403 cannot tell permissions from sharing from an expired
    token.
    """
    raise NotionError(_why(exc)) from None


def _plain(rich: list[dict] | None) -> str:
    return "".join(part.get("plain_text", "") for part in (rich or [])).strip()


def _prop(props: dict, name: str) -> object:
    """Flatten one Notion property into something a SQL column can hold."""
    prop = props.get(name)
    if not prop:
        return None
    kind = prop.get("type")
    if kind == "title":
        return _plain(prop["title"])
    if kind == "rich_text":
        return _plain(prop["rich_text"])
    if kind in ("select", "status"):
        return (prop[kind] or {}).get("name")
    if kind == "multi_select":
        return [opt["name"] for opt in prop["multi_select"]]
    if kind == "url":
        return prop["url"]
    return None


def fetch_page_content(page_id: str) -> dict:
    """The page body as the brief: flattened text plus the checklist split into
    done and not done, so the Inbox never asks about a ticked box. `blocks`
    maps each to-do to its block id for later ticking.
    """
    lines: list[str] = []
    done: list[str] = []
    todo: list[str] = []
    blocks: dict[str, str] = {}
    cursor = None
    while True:
        suffix = f"?start_cursor={cursor}" if cursor else ""
        data = _request(f"/blocks/{page_id}/children{suffix}")
        for block in data.get("results", []):
            kind = block.get("type")
            content = block.get(kind, {})
            text = _plain(content.get("rich_text"))
            if not text:
                continue
            if kind == "heading_1":
                lines.append(f"# {text}")
            elif kind == "heading_2":
                lines.append(f"## {text}")
            elif kind == "heading_3":
                lines.append(f"### {text}")
            elif kind == "bulleted_list_item":
                lines.append(f"- {text}")
            elif kind == "numbered_list_item":
                lines.append(f"1. {text}")
            elif kind == "to_do":
                checked = bool(content.get("checked"))
                mark = "x" if checked else " "
                lines.append(f"- [{mark}] {text}")
                (done if checked else todo).append(text)
                blocks[text] = block["id"]
            else:
                lines.append(text)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return {"body": "\n".join(lines), "done": done, "open": todo, "blocks": blocks}


def fetch_page_body(page_id: str) -> str:
    """Back-compat: just the text. Kept because the mirror and the CLI want it."""
    return fetch_page_content(page_id)["body"]


def fetch_board(database_id: str | None = None, *, with_bodies: bool = True) -> list[dict]:
    """Every row on the board, whatever its status.

    Unfiltered so the sync sees a row move to Done; a filtered query cannot
    observe a status change. Bodies are fetched only for In Progress and
    Exploring rows. `hash` covers everything the colony reads, checklist
    included. `body_fetched` says whether body fields are real, so a
    placeholder never overwrites a filed story's brief.
    """
    load_env()
    database_id = database_id or os.environ.get("NOTION_DATABASE_ID", DEFAULT_DATABASE_ID)

    rows: list[dict] = []
    cursor = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = _request(f"/databases/{database_id}/query", payload)

        for page in data.get("results", []):
            props = page.get("properties", {})
            categories = _prop(props, "Category") or []
            status = _prop(props, "Status")
            live = status in (WORKABLE_STATUS, RESEARCH_STATUS)
            content = (fetch_page_content(page["id"]) if with_bodies and live
                       else {"body": "", "done": [], "open": []})
            row = {
                "notion_page_id": page["id"],
                "title": _prop(props, "Idea") or "(untitled)",
                "notion_status": status,
                "body_fetched": bool(with_bodies and live),
                "priority": PRIORITY_RANK.get(_prop(props, "Priority") or "", 3),
                "category": json.dumps(categories),
                "related_link": _prop(props, "Related Link"),
                "description": content["body"],
                "done_items": json.dumps(content["done"]),
                "open_items": json.dumps(content["open"]),
                "last_edited": page.get("last_edited_time"),
            }
            row["hash"] = hashlib.sha256(
                json.dumps(
                    {k: row[k] for k in ("title", "notion_status", "priority", "category",
                                         "related_link", "description",
                                         "done_items", "open_items")},
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:16]
            rows.append(row)

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return rows


# ── the write half ────────────────────────────────────────────────────────────
# Called only from the tick when flushing `notion_outbox`. Each does one call
# and raises on anything unexpected; the outbox row handles retries.


def status_property_kind(database_id: str | None = None) -> str:
    """Is `Status` a `select` or a `status` property? They take different write
    payloads, so ask once per flush.
    """
    load_env()
    database_id = database_id or os.environ.get("NOTION_DATABASE_ID", DEFAULT_DATABASE_ID)
    db = _request(f"/databases/{database_id}")
    prop = (db.get("properties") or {}).get("Status") or {}
    kind = prop.get("type")
    return kind if kind in ("select", "status") else "select"


def set_status(page_id: str, status: str, *, kind: str = "select") -> dict:
    """Move a row on the board. The one property the colony may set."""
    if status not in WRITABLE_STATUS:
        raise NotionRefused(
            f"{status!r} is not a status the colony may set "
            f"({', '.join(WRITABLE_STATUS)}). Nothing else is an option on the board."
        )
    return _request(
        f"/pages/{page_id}",
        {"properties": {"Status": {kind: {"name": status}}}},
        method="PATCH",
    )


def add_comment(page_id: str, text: str) -> dict:
    """Comment on the page, prefixed "Ordis ·" so it is never mistaken for the
    PO's.
    """
    body = f"Ordis · {text.strip()}"[:1900]
    return _request("/comments", {"parent": {"page_id": page_id},
                                  "rich_text": [{"text": {"content": body}}]})


def check_item(block_id: str, checked: bool = True) -> dict:
    """Tick a to-do. Only queued from an accepted story, never on an agent's
    word.
    """
    return _request(f"/blocks/{block_id}",
                    {"to_do": {"checked": bool(checked)}}, method="PATCH")


def find_block(page_id: str, item_text: str) -> str | None:
    """The block id for a to-do, matched by text. None means skip, never a
    guess.
    """
    return fetch_page_content(page_id)["blocks"].get(item_text.strip())
