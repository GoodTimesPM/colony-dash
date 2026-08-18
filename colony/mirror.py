"""Nightly one-way mirror of the ledger into MySQL, for reporting.

SQLite stays the system of record; MySQL is a read-only warehouse you can point
a BI tool at and write real SQL against. Nothing in the colony ever reads back
from here, which is what keeps the mirror safe to blow away and rebuild.

Config comes from the environment (a .env in this folder, never committed):

    COLONY_MYSQL_HOST=127.0.0.1
    COLONY_MYSQL_PORT=3306
    COLONY_MYSQL_USER=colony_ro
    COLONY_MYSQL_PASSWORD=...
    COLONY_MYSQL_DB=colony_dash

Needs one of `PyMySQL` or `mysql-connector-python`; neither is imported unless
you actually run the mirror.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Tables worth reporting on. `_migrations` and the FTS shadow tables are
# machinery, not data.
MIRRORED_TABLES = [
    "sprints", "stories", "tickets", "runs", "pulses",
    "usage_samples", "escalations", "skills", "roster", "agents", "story_events",
]

# SQLite is loosely typed; MySQL is not. Map declared affinity to something
# reasonable and let everything unknown fall through to TEXT.
TYPE_MAP = {"INTEGER": "BIGINT", "REAL": "DOUBLE", "TEXT": "TEXT"}

# Columns that are short enough to index and long enough to matter.
SHORT_TEXT = {"status", "slug", "role", "division", "kind", "tier", "intent",
              "severity", "model", "project", "name", "emoji", "color"}


def load_env(path: Path | None = None) -> None:
    """Minimal .env reader — no dependency, and it never overwrites a real env var."""
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _connect_mysql():
    load_env()
    missing = [k for k in ("COLONY_MYSQL_USER", "COLONY_MYSQL_DB") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"MySQL mirror is not configured: missing {', '.join(missing)}")

    kwargs = dict(
        host=os.environ.get("COLONY_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("COLONY_MYSQL_PORT", "3306")),
        user=os.environ["COLONY_MYSQL_USER"],
        password=os.environ.get("COLONY_MYSQL_PASSWORD", ""),
        database=os.environ["COLONY_MYSQL_DB"],
    )
    try:
        import pymysql  # type: ignore

        return pymysql.connect(charset="utf8mb4", autocommit=False, **kwargs)
    except ImportError:
        pass
    try:
        import mysql.connector  # type: ignore

        return mysql.connector.connect(**kwargs)
    except ImportError as exc:
        raise RuntimeError(
            "No MySQL driver installed. `pip install PyMySQL` and re-run."
        ) from exc


def _mysql_type(column: sqlite3.Row) -> str:
    declared = (column["type"] or "TEXT").upper().split("(")[0]
    base = TYPE_MAP.get(declared, "TEXT")
    if base == "TEXT" and column["name"] in SHORT_TEXT:
        base = "VARCHAR(191)"
    return base


def mirror(conn: sqlite3.Connection, *, verbose: bool = True) -> dict:
    """Full refresh of every mirrored table. Returns row counts."""
    my = _connect_mysql()
    cur = my.cursor()
    counts: dict[str, int] = {}

    try:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in MIRRORED_TABLES:
            cols = list(conn.execute(f"PRAGMA table_info({table})"))
            if not cols:
                continue
            names = [c["name"] for c in cols]
            ddl_cols = ", ".join(f"`{c['name']}` {_mysql_type(c)}" for c in cols)

            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
            cur.execute(
                f"CREATE TABLE `{table}` ({ddl_cols}) "
                f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )

            rows = [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
            if rows:
                placeholders = ", ".join(["%s"] * len(names))
                collist = ", ".join(f"`{n}`" for n in names)
                cur.executemany(
                    f"INSERT INTO `{table}` ({collist}) VALUES ({placeholders})", rows
                )
            counts[table] = len(rows)
            if verbose:
                print(f"  {table:<14} {len(rows):>6} rows")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        my.commit()
    except Exception:
        my.rollback()
        raise
    finally:
        cur.close()
        my.close()

    return counts
