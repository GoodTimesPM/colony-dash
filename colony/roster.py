"""Scan persona files into the `roster` table.

Personas carry name, description, color, emoji and vibe; no tools, no model.
Scanning makes them searchable, not runnable (ROSTER.md §2). Two roots:

  `~/.agency-agents`  someone else's git clone, read only so `git pull` works.
  `~/.colony-agents`  this machine's own personas, written by the dashboard.

Neither is in this repo. A fresh install with no personas is normal and
shows an empty Standby panel.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

DEFAULT_ROSTER_DIR = Path(os.path.expanduser("~")) / ".agency-agents"
LOCAL_ROSTER_DIR = Path(os.path.expanduser("~")) / ".colony-agents"

# Directories in the install that are not divisions of the agency.
NON_DIVISIONS = {"examples", "scripts", "integrations", ".git", ".github", "docs"}

FRONTMATTER_KEYS = ("name", "description", "color", "emoji", "vibe")


def parse_persona(path: Path) -> dict | None:
    """Read a persona's frontmatter with a flat key reader, not a YAML library,
    so a stray `tools:` key is ignored rather than honoured.
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


def scan_root(root: Path, source: str) -> list[dict]:
    """One dict per persona under `root`. A missing directory is an empty list.
    """
    if not root.is_dir():
        return []

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
                    "source": source,
                }
            )
    return personas


def scan(root: Path = DEFAULT_ROSTER_DIR,
         local: Path | None = LOCAL_ROSTER_DIR) -> list[dict]:
    """Both roots in one list. Local personas win a slug collision, which is
    how to override an upstream one. Raises `FileNotFoundError` only when
    neither root exists, so the panel can say so instead of showing zero.
    """
    agency = scan_root(root, "agency")
    mine = scan_root(local, "local") if local else []
    if not agency and not mine:
        raise FileNotFoundError(
            f"No personas found. Expected an agency-agents clone at {root}, "
            f"or personas of your own under {local}. Add one from the dashboard "
            f"(Standby → +) or pass --roster-dir if the clone lives elsewhere."
        )

    by_slug = {p["slug"]: p for p in agency}
    by_slug.update({p["slug"]: p for p in mine})
    return sorted(by_slug.values(), key=lambda p: (p["division"], p["name"]))


def sync(conn: sqlite3.Connection, root: Path = DEFAULT_ROSTER_DIR,
         local: Path | None = LOCAL_ROSTER_DIR) -> dict:
    """Upsert the scan into `roster` and report personas whose `body_hash`
    changed upstream, since one may already be hired.
    """
    personas = scan(root, local)
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
                                path, body_hash, source, scanned_at)
            VALUES (:slug, :name, :division, :description, :emoji, :color, :vibe,
                    :path, :body_hash, :source, datetime('now','localtime'))
            ON CONFLICT(slug) DO UPDATE SET
              name = excluded.name, division = excluded.division,
              description = excluded.description, emoji = excluded.emoji,
              color = excluded.color, vibe = excluded.vibe, path = excluded.path,
              body_hash = excluded.body_hash, source = excluded.source,
              scanned_at = excluded.scanned_at
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
        "local": sum(1 for p in personas if p["source"] == "local"),
    }


def digest(conn: sqlite3.Connection, *, desc_chars: int = 200) -> str:
    """The whole roster by division, with hire counts.

    All of it, not a search-ranked shortlist: searching on story text only
    finds personas that already sound like the story, which narrows the
    colony. About 18k tokens per hire. `hired` makes the diversity rule
    checkable.
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


# -- writing a persona ---------------------------------------------------------
# These write only under `LOCAL_ROSTER_DIR`, enforced by `_safe_name`.

SAFE = "abcdefghijklmnopqrstuvwxyz0123456789-"


class BadPersona(ValueError):
    """A persona the dashboard will not write. The message is shown to the PO."""


def _safe_name(raw: str, what: str) -> str:
    """Fold a typed name to `[a-z0-9-]`, or refuse an empty result. It is a
    path component from a web request, so traversal is the threat.
    """
    name = "".join(c if c in SAFE else "-" for c in (raw or "").strip().lower())
    while "--" in name:
        name = name.replace("--", "-")
    name = name.strip("-")
    if not name:
        raise BadPersona(f"{what} needs at least one letter or digit")
    if len(name) > 60:
        raise BadPersona(f"{what} is too long (60 characters max)")
    if name in NON_DIVISIONS:
        raise BadPersona(f"{name!r} is a reserved folder name and the scanner skips it")
    return name


def persona_path(division: str, slug: str, root: Path = LOCAL_ROSTER_DIR) -> Path:
    """The file for a local persona, re-checked against the root after
    resolving.
    """
    path = (root / _safe_name(division, "division") /
            (_safe_name(slug, "file name") + ".md"))
    try:
        inside = path.resolve().is_relative_to(root.resolve())
    except OSError:
        inside = False
    if not inside:
        raise BadPersona("that name does not resolve to a file inside the personas folder")
    return path


def write_persona(*, division: str, slug: str, name: str, description: str = "",
                  emoji: str = "", color: str = "", vibe: str = "", body: str = "",
                  overwrite: bool = False, root: Path = LOCAL_ROSTER_DIR) -> Path:
    """Write one persona file and return its path.

    Frontmatter is rebuilt from the fields, dropping any `tools:` or
    `model:`. `overwrite=False` so a dropped import cannot replace a hired
    persona.
    """
    if not (name or "").strip():
        raise BadPersona("a persona needs a name")

    # Filename defaults to the name; handled here so every caller gets it.
    path = persona_path(division, slug or name, root)
    if path.exists() and not overwrite:
        raise BadPersona(f"{path.name} already exists in {path.parent.name}. "
                         f"Rename it, or tick replace.")

    def one(key: str, value: str) -> str:
        # One line, because `parse_persona` reads frontmatter line by line.
        return f"{key}: {' '.join((value or '').split())}\n" if (value or "").strip() else ""

    front = ("---\n"
             + one("name", name)
             + one("description", description)
             + one("color", color)
             + one("emoji", emoji)
             + one("vibe", vibe)
             + "---\n")
    text = front + "\n" + (body or "").strip() + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".new")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
    return path


def delete_persona(conn: sqlite3.Connection, slug: str,
                   root: Path = LOCAL_ROSTER_DIR) -> Path:
    """Remove a local persona file and its row. Agency personas are refused:
    they live in someone else's clone and the next scan would restore them
    anyway.
    """
    row = conn.execute("SELECT path, source FROM roster WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise BadPersona(f"no persona {slug!r} in the roster")
    if row["source"] != "local":
        raise BadPersona(f"{slug} came from the agency-agents clone, which this "
                         f"dashboard only ever reads. Delete it with git, or "
                         f"shadow it with a persona of your own in the same division.")

    path = Path(row["path"])
    # Re-derived rather than trusted: `path` is a column, and a column is a
    # thing that can be wrong. The delete only ever happens under the local root.
    if not path.resolve().is_relative_to(root.resolve()):
        raise BadPersona(f"{slug} is not inside {root}")
    path.unlink(missing_ok=True)
    conn.execute("DELETE FROM roster WHERE slug = ?", (slug,))
    return path
