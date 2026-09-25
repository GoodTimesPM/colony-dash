"""The ledger: connection, pragmas, migrations.

Everything the colony knows lives in one SQLite file. There is no server to be
down when the pulse fires at 3am. See ARCHITECTURE.md §3.3.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
RUNTIME_DIR = PROJECT_DIR / ".colony"
LEDGER_PATH = RUNTIME_DIR / "ledger.db"
# Screenshots and files the PO pastes into a reply. Under `.colony/` with the
# ledger, because they are part of the same conversation and should be thrown
# away by the same `rm -rf`.
ATTACHMENTS_DIR = RUNTIME_DIR / "attachments"
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"


def _env_value(key: str) -> str | None:
    """One value out of the environment, falling back to one line of `.env`.

    Deliberately not `mirror.load_env`, which loads the *whole* file into
    `os.environ`. This module is imported by everything, and everything includes
    the console, which hands its environment to a `claude` subprocess. Loading
    `NOTION_TOKEN` here would put the token in front of an agent that is not
    allowed to read `.env`. So: read one key, mutate nothing.
    """
    live = os.environ.get(key)
    if live:
        return live
    return _env_file_values().get(key) or None


# `.env` parsed once and re-read only when its mtime or size moves.
_env_cache: dict = {"stamp": None, "values": {}}


def _env_file_values() -> dict[str, str]:
    env_file = PROJECT_DIR / ".env"
    try:
        st = env_file.stat()
    except OSError:
        return {}
    stamp = (str(env_file), st.st_mtime_ns, st.st_size)
    if _env_cache["stamp"] != stamp:
        values: dict[str, str] = {}
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        for line in lines:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            if name.strip() not in values:
                values[name.strip()] = value
        _env_cache.update(stamp=stamp, values=values)
    return _env_cache["values"]


def set_env_value(key: str, value: str, path: Path | None = None,
                  comment: str | None = None) -> bool:
    """Set one key in `.env`, in place. Returns True if a line was replaced.

    Factored out of `phone.rotate`, which held the only copy, once a second
    caller appeared. Duplicating it would have been the worse option by some
    distance: this file holds the Notion token, it is the only copy of a
    credential typed in by hand, and a second slightly-different rewrite of it
    is how one of the two eventually loses a line.

    Two properties, both of which are the point:

      * **Only lines starting `KEY=` change.** Every other line is written back
        byte for byte, in order, comments and blank lines included. The file is
        never parsed into a dict and re-serialised, because that is the step
        that reformats quoting, drops comments and reorders keys. CRLF endings
        survive for the same reason: a rewrite that reflows the whole file makes
        every future diff of a credential file unreadable, and an unreadable
        diff is one nobody checks.
      * **The replacement is atomic.** New text goes to a temporary file beside
        the target and is moved over it with `os.replace`, so a crash midway
        leaves the old file whole rather than a truncated one with a token cut
        in half.

    `os.environ` is deliberately untouched. `_env_value` prefers the live
    environment, so a caller that needs the new value to win inside this process
    has to say so itself -- and it should think about that first, because this
    process hands its environment to the `claude` subprocess the console spawns.
    """
    env_path = path or (PROJECT_DIR / ".env")
    prefix = key + "="
    try:
        body = env_path.read_text(encoding="utf-8")
    except OSError:
        body = ""

    # `keepends` so a CRLF file keeps CRLF, and a last line with no newline at
    # all stays that way.
    lines = body.splitlines(keepends=True)
    replaced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(prefix):
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = prefix + value + ending
            replaced = True

    text = "".join(lines)
    if not replaced:
        lead = "" if (not text or text.endswith("\n")) else "\n"
        note = ("\n" + "# " + comment + "\n") if comment else "\n"
        text += lead + note + prefix + value + "\n"

    temp = env_path.with_name(env_path.name + ".new")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, env_path)
    return replaced



# Read scope for every agent, structural or hired. Write scope is always narrower
# and always set per ticket. ARCHITECTURE.md §8.1.
#
# The default is the folder that *contains* this checkout, which is the shape the
# colony was built for: a directory of sibling projects with `colony-dash` as one
# of them. Set `COLONY_PROJECTS_ROOT` (environment or `.env`) to point it
# anywhere else. It was a literal path until 2026-08-27, which worked on exactly
# one machine.
PROJECTS_ROOT = Path(_env_value("COLONY_PROJECTS_ROOT") or PROJECT_DIR.parent)


def connect(path: Path | str = LEDGER_PATH, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the ledger with the pragmas this system depends on."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path, isolation_level=None)

    conn.row_factory = sqlite3.Row
    # WAL is what lets the dashboard read while the pulse writes.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# Hashes of earlier versions of applied migrations, accepted once and then
# replaced by the current hash.
#
# Every entry here is a comment rewrite. Preparing this repo to be published
# meant removing one person's name and gendered pronouns from every comment in
# it, and ten of those comments sit in migrations that had already run on a live
# ledger. The schema those files produce did not change; only the prose above it
# did. The check in `migrate` compares bytes, so it saw a schema fork that was
# not there.
#
# This forgives specific bytes, not a class of edit. A migration altered in any
# way that is not one of these exact earlier versions still stops the process,
# which is the behaviour worth keeping: two installs quietly disagreeing about
# what a table looks like is a much worse failure than a refusal to boot.
SUPERSEDED: dict[str, tuple[str, ...]] = {
    "001_initial.sql": (
        "69d9abf93aef4795a310082067bfba836f00c8a87ff5b8e890c7c0a5011f8626",),
    "005_po_replies.sql": (
        "822b29c40e68f5847809ca0e4ccf894bea937cbc7d4d5dbc1616e5f102078a4b",),
    "012_project_movement.sql": (
        "3bc61649b94741e0fb6d0784a4f4f3f48323dd0b09e4c10d5fd25184902fce80",),
    "013_po_answers.sql": (
        "17e9d2c1c3087c1b52a92407331ea32042f6b1bef515ca0d78087a99e98a6bbc",),
    "014_decisions_and_bias.sql": (
        "3d2eeea0bb00f27bd10e442907099a115320b0de027e8554c049c62649110d5e",),
    "015_decision_wording.sql": (
        "46d7bafc2f4a3f64f37076e482ca55d9105263f7de58e7dd6c034989abfcfff4",),
    "017_dismiss.sql": (
        "7cbd78bad502098ef9214384e14167c29b8f3d13d045bc48e139b76716e0d27d",),
    "022_agent_secrets.sql": (
        "c988fb5e989a2883c39b61135b7318ec3becc3f9d0a69efa2c3e006f0ff490d5",),
    "023_run_requests.sql": (
        "163aafba1ebce7f224769c2f9d29cbdd9b8fd2e7812814660f383cb4e6796d95",),
    "024_console.sql": (
        "889e04c9e6cdca93b27b878ff8c08d0c78c8c6a0a72eb0168abd3256db56378a",),
}


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
          filename   TEXT PRIMARY KEY,
          sha256     TEXT NOT NULL,
          applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )


def migrate(conn: sqlite3.Connection, *, verbose: bool = True) -> list[str]:
    """Apply every migration not yet recorded. Returns the ones applied.

    Each file runs inside its own transaction, so a broken migration leaves the
    ledger on the last good schema rather than half-way between two.
    """
    _ensure_migrations_table(conn)
    applied = {r["filename"]: r["sha256"] for r in conn.execute("SELECT * FROM _migrations")}
    ran: list[str] = []

    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = sql_file.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        if sql_file.name in applied:
            if applied[sql_file.name] != digest:
                if applied[sql_file.name] not in SUPERSEDED.get(sql_file.name, ()):
                    raise RuntimeError(
                        f"{sql_file.name} changed after it was applied. Migrations are "
                        f"append-only. Add a new file instead of editing this one."
                    )
                # A comment-only rewrite this file vouches for. Record the new
                # hash so the check is exact again from the next start onwards.
                conn.execute("UPDATE _migrations SET sha256 = ? WHERE filename = ?",
                             (digest, sql_file.name))
            continue

        # Foreign keys off for the duration. A migration that widens a CHECK
        # has to rebuild the table -- SQLite cannot alter a constraint in place
        # -- and dropping a table other tables point at trips the constraint
        # even though the rename puts every reference back. This is what
        # SQLite's own "making other kinds of table schema changes" procedure
        # says to do. `defer_foreign_keys` is not a substitute: it counts
        # violations rather than re-checking them, and a DROP raises a count
        # that recreating the parent never lowers.
        #
        # The pragma is a no-op inside a transaction, so it goes outside one.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(f"BEGIN;\n{text}\nCOMMIT;")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

        # And now check what the constraint would have checked. This is new:
        # before, a migration could leave a reference pointing at nothing and
        # the ledger would carry the damage silently.
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(
                f"{sql_file.name} left {len(broken)} dangling reference(s), "
                f"first in table {broken[0][0]!r}. The schema change is applied; "
                f"the ledger is not consistent."
            )

        conn.execute(
            "INSERT INTO _migrations (filename, sha256) VALUES (?, ?)", (sql_file.name, digest)
        )
        ran.append(sql_file.name)
        if verbose:
            print(f"  applied {sql_file.name}")

    return ran


def open_ledger(path: Path | str = LEDGER_PATH) -> sqlite3.Connection:
    """Connect and bring the schema up to date. The one entry point callers want."""
    conn = connect(path)
    migrate(conn, verbose=False)
    return conn
