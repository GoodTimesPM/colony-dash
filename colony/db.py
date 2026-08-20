"""The ledger: connection, pragmas, migrations.

Everything the colony knows lives in one SQLite file. There is no server to be
down when the pulse fires at 3am — see ARCHITECTURE.md §3.3.
"""

from __future__ import annotations

import hashlib
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

# Read scope for every agent, structural or hired. Write scope is always narrower
# and always set per ticket. ARCHITECTURE.md §8.1.
PROJECTS_ROOT = Path("D:/ALL STUFF/PROJECTS")


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
                raise RuntimeError(
                    f"{sql_file.name} changed after it was applied. Migrations are "
                    f"append-only — add a new file instead of editing this one."
                )
            continue

        conn.executescript(f"BEGIN;\n{text}\nCOMMIT;")
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
