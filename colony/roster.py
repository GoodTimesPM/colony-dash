"""Scan persona files into the `roster` table.

These files are résumés, not employees. They carry `name`/`description`/`color`/
`emoji`/`vibe` and nothing else. No `tools:`, no `model:`. Persona without
governance. Scanning them here makes them browsable and searchable; it does not
install anything and does not make anything runnable. See ROSTER.md §2.

Two roots are scanned, and the split is the whole point of this module:

  `~/.agency-agents`. Somebody else's git clone. Read only, always. Nothing
                        here ever writes into it, because the next `git pull`
                        in that clone would either clobber the write or refuse
                        to fast-forward past it.
  `~/.colony-agents`. This machine's own personas, written by the dashboard.
                        Upstream has never heard of it, so it survives.

Neither is inside this repository and neither is ever committed. A persona is a
machine's furniture; shipping a stranger's agent library inside a project that
merely reads it would be redistributing their work and would make the clone a
dependency of `git clone` rather than of first run. The dashboard offers an
import panel instead, and a fresh install with no personas at all is a normal
install with an empty Standby panel.
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


def scan_root(root: Path, source: str) -> list[dict]:
    """Walk one install and return one dict per persona found.

    A missing directory is an empty list, not an error. Only one of the two
    roots is ever guaranteed to exist -- a machine with no agency-agents clone
    and three hand-written personas is a perfectly ordinary machine -- so
    "nothing here" has to be a normal answer rather than something the caller
    has to catch.
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
    """Both roots, flattened into one list.

    Local personas are appended after the agency ones and win a slug collision,
    because a `slug` is `division/filename` and the person who wrote a file on
    this machine outranks a clone they did not write. That is also the only
    supported way to override an upstream persona: put a file with the same name
    in the same division under `~/.colony-agents`, and leave the clone alone.

    An entirely missing agency clone is fine and always has been the likely case
    for anyone who is not the PO. What is not fine is *silently* empty, so the
    caller gets `FileNotFoundError` only when neither root exists -- at which
    point the roster genuinely has nowhere to come from and the panel should say
    so rather than show zero.
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
    """Upsert the scan into `roster`. Reports what changed upstream.

    A changed `body_hash` means a `git pull` in the agency-agents repo rewrote a
    persona we may already have hired. That is a fact the PO should see, not a
    silent overwrite.
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
    """The whole roster, grouped by division, with how often each was picked.

    All 270 of them, deliberately. The obvious economy is to search the roster
    with terms from the story and show the top twenty. And that economy is the
    bias. A search over the story text can only ever return personas whose
    description already sounds like the story, which is how a colony ends up
    with four engineers and no one who has ever thought about a user. The PO
    asked for the opposite: "this environment needs to be diverse."

    Roughly 70k characters, so about 18k tokens. That is a third of one grooming
    run, paid once per hire, to make the choice from the actual field instead of
    from a shortlist someone else drew.

    `hired` is the count that makes the diversity rule checkable rather than
    aspirational. It goes in front of the chooser, and it is still there
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


# -- writing a persona ---------------------------------------------------------
# Everything above this line reads. These three write, and they write only ever
# under `LOCAL_ROSTER_DIR` -- never into the agency clone, never anywhere else on
# the disk. `_safe_name` is the entire enforcement of that, so it is stricter
# than it needs to be rather than cleverer.

SAFE = "abcdefghijklmnopqrstuvwxyz0123456789-"


class BadPersona(ValueError):
    """A persona the dashboard will not write. The message is shown to the PO."""


def _safe_name(raw: str, what: str) -> str:
    """Fold a typed-in name down to `[a-z0-9-]`, or refuse.

    This is a path component that arrives from a web request, so the failure
    mode being designed against is `../../.ssh/authorized_keys`, not a stray
    capital letter. Folding rather than rejecting keeps "Data Engineering" from
    being an error the user has to solve; refusing the empty result keeps
    "../.." from folding down to something that still traverses.
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
    """The file a local persona lives at, with both components made safe.

    Resolved and re-checked against the root afterwards. `_safe_name` already
    makes traversal impossible, and this is the assertion that says so out loud
    -- the cost of being wrong here is a web request writing anywhere on the
    disk, which is worth two checks.
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
    """Write one persona file. Returns the path it landed at.

    The frontmatter is rebuilt from the fields rather than passed through, so a
    file that arrives with a `tools:` or `model:` key loses it here instead of
    at scan time. `parse_persona` already ignores those keys, but a persona file
    on disk claiming tool access it does not have is a document that will
    eventually be believed by a person.

    `overwrite=False` by default because the panel's import path takes a
    dropped file, and a drop that silently replaces a persona already hired into
    a running contract is the kind of loss nobody notices for a week.
    """
    if not (name or "").strip():
        raise BadPersona("a persona needs a name")

    # The file name is optional in the panel, because "what should this file be
    # called" is a question about the filesystem and the person filling the form
    # is thinking about a colleague. Falling back here rather than only at the
    # route keeps the two callers -- the panel and anything later -- from each
    # needing to remember it.
    path = persona_path(division, slug or name, root)
    if path.exists() and not overwrite:
        raise BadPersona(f"{path.name} already exists in {path.parent.name}. "
                         f"Rename it, or tick replace.")

    def one(key: str, value: str) -> str:
        # Folded to one line: the frontmatter reader in `parse_persona` is a
        # line reader, so a description with a newline in it would truncate the
        # block and take the rest of the file with it.
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
    """Remove a local persona file and its roster row.

    Refuses anything whose `source` is not 'local'. Deleting an agency persona
    would delete a file out of somebody else's git clone, where the loss shows
    up as a dirty working tree in a repo the user did not think they were
    editing. The scan would also put it straight back on the next `git pull`,
    so the button would look broken on top of being wrong.
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
