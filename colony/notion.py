"""Read the Notion intake board, and, since M5, write back to it.

The MCP server Ordis uses interactively is not available to a scheduled Python
process, so the tick talks to the Notion REST API directly with an integration
token. Config (a .env in this folder, never committed):

    NOTION_TOKEN=ntn_...
    NOTION_DATABASE_ID=1d23280a-add3-41cb-bd14-67c771ee6d88

The write half is deliberately small. The colony may set a row's Status, tick a
checkbox it has verified as done, and leave a comment. It may not create rows,
delete rows, or edit the brief: the board is where the PO states intent, and a
loop that can rewrite its own instructions has no human gate in it. Nothing here
is called from control.py. Writes are queued into `notion_outbox` and flushed
by the tick, the same separation 007 drew for skill drafts.
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

# The other five options on the Notion select, and what they mean here.
#
# Only two of the seven statuses are an instruction to the colony. The rest are
# the PO filing something, finished, parked, or not begun, and a row being
# filed is the *absence* of a request. Treating them as work was the loop's
# loudest mistake: an idea the PO wrote down and left alone came back an hour
# later as a question in their Inbox asking which folder it belonged to, which is
# the colony inventing an obligation out of a note.
SETTLED_STATUS = {
    "Done":        "done",
    "Shipped":     "done",
    "Shelved":     "shelved",
    "New":         "not-started",
    "Not started": "not-started",
}


def ledger_status(notion_status: str | None) -> str:
    """Where in the colony's own workflow a Notion row lands.

    This is orthogonal to whether the row is filed, `stories.settled_as` holds
    that, so a story parked as Done and later reopened comes back to the
    workflow status it actually had rather than to a guess.
    """
    return "backlog" if notion_status == WORKABLE_STATUS else "needs-criteria"

# What may be set from this dashboard, in board order.
#
# "In Progress" is on the list now, at the PO's request. The rule it used to be
# kept off the list to enforce, the colony must never move a row into its own
# intake filter, or it can feed itself work it invented, is still the right
# rule and is still enforced, just somewhere better: the only two callers of
# `queue_notion` are a button in the story drawer and the drop dialog, and both
# of them are the PO's hand on a control. No agent, wake or tick queues a
# status. What the omission was actually preventing was *the PO* starting work
# from the dashboard, which was never the thing to prevent.
#
# "Archived" used to be in here and is not an option on the real Status select.
# Notion answers an unknown select option by **creating** it, so the one list
# whose whole job is to bound what the colony writes was the thing that would
# have added an eighth status to the board.
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

    Every field here is Notion's own prose about its own API. Nothing from the
    request, and so nothing from the Authorization header, can reach it.
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
    """Raise with Notion's own explanation from the response body.

    urllib drops the body, and a bare "403 Forbidden" reads the same for a
    read-only integration, a sharing problem and an expired token. The body's
    `restricted_resource: Insufficient permissions` names the fix, and it ends
    up on the outbox row where the person reads it.
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
    """The page body is the brief. But a brief has two halves.

    Returns the flattened text *and* the checklist split into what is already
    done and what is not. Flattening `- [x]` and `- [ ]` to the same kind of
    string is how the Inbox ended up asking the PO to decide things they had
    already decided: the ledger could see the sentence but not the checkbox.

    `blocks` carries the block id of every to-do, so a later tick can tick one
    without re-reading the whole page.
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

    This used to filter the query to `In Progress OR Exploring`, which was the
    right answer to "what may the colony work on" and the wrong answer to "what
    is on the board". And the sync needs the second. A row moved to Done simply
    vanished from the result set, so the sync never learned it had moved and the
    story sat in the ledger frozen at its last workable status forever. **A
    status change you filter out is a status change you cannot observe**, and
    filing only exists as a concept because the colony can see it happen.

    The cost of reading everything is bounded by not reading the *bodies* of
    rows the colony may not act on: one request for the page list, plus a body
    request only for rows that are In Progress or Exploring. That is the same
    number of body fetches the filtered version made.

    Returns dicts shaped for the `stories` table. `hash` covers everything the
    colony reads, so an unchanged row costs the wake tier nothing. And since
    M5 that includes the checklist, because a box getting ticked in Notion is
    exactly the kind of change the colony must notice. `body_fetched` says
    whether the body fields in the dict are real or placeholders, because
    writing a blank description over a real one is how a filed story loses its
    brief on the way to the shelf.
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
#
# Everything below is only ever reached from the tick, flushing `notion_outbox`.
# Each function does one API call and raises on anything unexpected, because the
# outbox row is what handles the retry. Swallowing the error here would mark a
# message sent that nobody ever received.


def status_property_kind(database_id: str | None = None) -> str:
    """Is `Status` a `select` or a `status` property on this database?

    Notion has both and they take different payloads. The read path guesses
    `select` in its filter and has been right since M0, but guessing wrong on a
    write is a 400 rather than a filter that quietly matches nothing, so the
    write path asks first. One call per flush, not one per message.
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
    """Say something on the page. How a question reaches the PO when they are out.

    Prefixed so a comment from the loop is never mistaken for one the PO left
    themselves. The board is shared with their own thinking, and an unattributed
    machine voice in the middle of it is worse than no comment at all.
    """
    body = f"Ordis · {text.strip()}"[:1900]
    return _request("/comments", {"parent": {"page_id": page_id},
                                  "rich_text": [{"text": {"content": body}}]})


def check_item(block_id: str, checked: bool = True) -> dict:
    """Tick a to-do the colony has verified as done.

    The narrowest write in the system and the one with the most trust in it:
    ticking a box is the colony asserting a fact about the world. It is only
    ever queued off an accepted story, never off an agent's own say-so.
    """
    return _request(f"/blocks/{block_id}",
                    {"to_do": {"checked": bool(checked)}}, method="PATCH")


def find_block(page_id: str, item_text: str) -> str | None:
    """The block id for one to-do, matched by its text.

    Text is a weak key and this knows it. The fallback is `None` and a skipped
    tick, never a guess at a neighbouring checkbox.
    """
    return fetch_page_content(page_id)["blocks"].get(item_text.strip())
