"""Scan the agency-agents install into the `roster` table.

These files are résumés, not employees. They carry `name`/`description`/`color`/
`emoji`/`vibe` and nothing else — no `tools:`, no `model:`. Persona without
governance. Scanning them here makes them browsable and searchable; it does not
install anything and does not make anything runnable. See ROSTER.md §2.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

DEFAULT_ROSTER_DIR = Path(os.path.expanduser("~")) / ".agency-agents"

# Directories in the install that are not divisions of the agency.
NON_DIVISIONS = {"examples", "scripts", "integrations", ".git", ".github", "docs"}

FRONTMATTER_KEYS = ("name", "description", "color", "emoji", "vibe")


def parse_persona(path: Path) -> dict | None:
    """Pull the YAML frontmatter out of a persona file.

    Deliberately a five-key line reader rather than a YAML dependency: these files
    have a fixed, flat shape, and if one ever grows a `tools:` key we want the
    scan to ignore it rather than quietly honour it.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None

    _, _, rest = text.partition("---\n")
    front, sep, body = rest.partition("\n---")
    if not sep:
        return None

    meta: dict[str, str] = {}
    for line in front.splitlines():
        key, colon, value = line.partition(":")
        if not colon:
            continue
        key = key.strip().lower()
        if key in FRONTMATTER_KEYS:
            meta[key] = value.strip().strip("\"'")

    if "name" not in meta:
        return None

    meta["body_hash"] = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]
    return meta


def scan(root: Path = DEFAULT_ROSTER_DIR) -> list[dict]:
    """Walk the install and return one dict per persona found."""
    if not root.is_dir():
        raise FileNotFoundError(
            f"No agency-agents install at {root}. Pass --roster-dir if it lives elsewhere."
        )

    personas: list[dict] = []
    for division_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        division = division_dir.name
        if division in NON_DIVISIONS or division.startswith("."):
            continue
        for md in sorted(division_dir.rglob("*.md")):
            meta = parse_persona(md)
            if not meta:
                continue
            personas.append(
                {
                    "slug": f"{division}/{md.stem}",
                    "name": meta["name"],
                    "division": division,
                    "description": meta.get("description"),
                    "emoji": meta.get("emoji"),
                    "color": meta.get("color"),
                    "vibe": meta.get("vibe"),
                    "path": str(md),
                    "body_hash": meta["body_hash"],
                }
            )
    return personas


def sync(conn: sqlite3.Connection, root: Path = DEFAULT_ROSTER_DIR) -> dict:
    """Upsert the scan into `roster`. Reports what changed upstream.

    A changed `body_hash` means a `git pull` in the agency-agents repo rewrote a
    persona we may already have hired. That is a fact the PO should see, not a
    silent overwrite.
    """
    personas = scan(root)
    before = {r["slug"]: r["body_hash"] for r in conn.execute("SELECT slug, body_hash FROM roster")}

    added, changed = [], []
    for p in personas:
        prev = before.get(p["slug"])
        if prev is None:
            added.append(p["slug"])
        elif prev != p["body_hash"]:
            changed.append(p["slug"])

        conn.execute(
            """
            INSERT INTO roster (slug, name, division, description, emoji, color, vibe,
                                path, body_hash, scanned_at)
            VALUES (:slug, :name, :division, :description, :emoji, :color, :vibe,
                    :path, :body_hash, datetime('now','localtime'))
            ON CONFLICT(slug) DO UPDATE SET
              name = excluded.name, division = excluded.division,
              description = excluded.description, emoji = excluded.emoji,
              color = excluded.color, vibe = excluded.vibe, path = excluded.path,
              body_hash = excluded.body_hash, scanned_at = excluded.scanned_at
            """,
            p,
        )

    seen = {p["slug"] for p in personas}
    removed = [s for s in before if s not in seen]
    for slug in removed:
        conn.execute("DELETE FROM roster WHERE slug = ?", (slug,))

    return {
        "total": len(personas),
        "added": added,
        "changed": changed,
        "removed": removed,
        "divisions": sorted({p["division"] for p in personas}),
    }


def digest(conn: sqlite3.Connection, *, desc_chars: int = 200) -> str:
    """The whole roster, grouped by division, with how often each was picked.

    All 270 of them, deliberately. The obvious economy is to search the roster
    with terms from the story and show the top twenty — and that economy is the
    bias. A search over the story text can only ever return personas whose
    description already sounds like the story, which is how a colony ends up
    with four engineers and no one who has ever thought about a user. Jordan
    asked for the opposite: "this environment needs to be diverse."

    Roughly 70k characters, so about 18k tokens. That is a third of one grooming
    run, paid once per hire, to make the choice from the actual field instead of
    from a shortlist someone else drew.

    `hired` is the count that makes the diversity rule checkable rather than
    aspirational — it goes in front of the chooser, and it is still there
    afterwards when someone asks why the same name keeps coming up.
    """
    out: list[str] = []
    division = None
    for r in conn.execute(
        "SELECT slug, name, division, description, times_hired, last_hired_at "
        "FROM roster ORDER BY division, name"
    ):
        if r["division"] != division:
            division = r["division"]
            out.append("")
            out.append(f"[{division}]")
        mark = ""
        if r["times_hired"]:
            mark = f"  <<hired {r['times_hired']}x, last {r['last_hired_at'] or '?'}>>"
        desc = " ".join((r["description"] or "").split())[:desc_chars]
        out.append(f"  {r['slug']}  ({r['name']}){mark}")
        if desc:
            out.append(f"      {desc}")
    return "\n".join(out).strip()


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    """Roster search, for the standby browser's search bar."""
    if not query.strip():
        return list(conn.execute("SELECT * FROM roster ORDER BY division, name LIMIT ?", (limit,)))
    # FTS5 prefix match on each term; quoted so punctuation in a query can't
    # become MATCH syntax.
    expr = " ".join(f'"{term}"*' for term in query.split())
    return list(
        conn.execute(
            """
            SELECT r.* FROM roster_fts f
            JOIN roster r ON r.id = f.rowid
            WHERE roster_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (expr, limit),
        )
    )
