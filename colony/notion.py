"""Read the Notion intake board. Stdlib only — the pulse must run headless.

The MCP server Ordis uses interactively is not available to a scheduled Python
process, so the tick talks to the Notion REST API directly with an integration
token. Config (a .env in this folder, never committed):

    NOTION_TOKEN=ntn_...
    NOTION_DATABASE_ID=1d23280a-add3-41cb-bd14-67c771ee6d88

Read-only: this module never writes to Notion. Pushing questions back up as page
comments is M5.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request

from .mirror import load_env

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DEFAULT_DATABASE_ID = "1d23280a-add3-41cb-bd14-67c771ee6d88"

# Status = "In Progress" is the only signal to work a row. ARCHITECTURE.md §5.1.
WORKABLE_STATUS = "In Progress"
RESEARCH_STATUS = "Exploring"

PRIORITY_RANK = {"High": 1, "Medium": 2, "Low": 3}


class NotionUnconfigured(RuntimeError):
    """No token. The tick reports this and carries on — it is not a failure."""


def _request(path: str, payload: dict | None = None) -> dict:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise NotionUnconfigured("NOTION_TOKEN is not set")

    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


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


def fetch_page_body(page_id: str) -> str:
    """The page body is the brief. Flatten the top-level blocks to markdown-ish text."""
    lines: list[str] = []
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
                mark = "x" if content.get("checked") else " "
                lines.append(f"- [{mark}] {text}")
            else:
                lines.append(text)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return "\n".join(lines)


def fetch_board(database_id: str | None = None, *, with_bodies: bool = True) -> list[dict]:
    """Every row the colony cares about: In Progress and Exploring.

    Returns dicts shaped for the `stories` table. `hash` covers everything the
    colony reads, so an unchanged row costs the wake tier nothing.
    """
    load_env()
    database_id = database_id or os.environ.get("NOTION_DATABASE_ID", DEFAULT_DATABASE_ID)

    rows: list[dict] = []
    cursor = None
    while True:
        payload: dict = {
            "page_size": 100,
            "filter": {
                "or": [
                    {"property": "Status", "select": {"equals": WORKABLE_STATUS}},
                    {"property": "Status", "select": {"equals": RESEARCH_STATUS}},
                ]
            },
        }
        if cursor:
            payload["start_cursor"] = cursor
        data = _request(f"/databases/{database_id}/query", payload)

        for page in data.get("results", []):
            props = page.get("properties", {})
            categories = _prop(props, "Category") or []
            body = fetch_page_body(page["id"]) if with_bodies else ""
            row = {
                "notion_page_id": page["id"],
                "title": _prop(props, "Idea") or "(untitled)",
                "notion_status": _prop(props, "Status"),
                "priority": PRIORITY_RANK.get(_prop(props, "Priority") or "", 3),
                "category": json.dumps(categories),
                "related_link": _prop(props, "Related Link"),
                "description": body,
                "last_edited": page.get("last_edited_time"),
            }
            row["hash"] = hashlib.sha256(
                json.dumps(
                    {k: row[k] for k in ("title", "notion_status", "priority", "category",
                                         "related_link", "description")},
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:16]
            rows.append(row)

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return rows
