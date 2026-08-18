"""Seed the ledger with the two structural agents and the first sprint.

`investigator` and `reviewer` are hand-written rather than hired: they are the
loop's own safety machinery, not specialists, and their contracts must not drift
with an upstream persona repo. ARCHITECTURE.md §2.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta

READ_SCOPE = ["D:/ALL STUFF/PROJECTS/**"]

STRUCTURAL_AGENTS = [
    {
        "role": "investigator",
        "project": None,
        "roster_slug": None,
        "model": "claude-sonnet-5",
        "write_capable": 0,
        # No Edit, no Write, no Bash. The only agent the loop may spawn without a
        # human in the path, so it gets the narrowest toolset in the system.
        "tools_allowed": ["Read", "Grep", "Glob"],
        "tools_denied": ["Edit", "Write", "Bash", "WebFetch", "Artifact", "Agent"],
        "read_scope": READ_SCOPE,
        "write_scope": None,
        "skills": [],
        # Measured, not guessed: grooming one story costs ~61k chargeable tokens
        # (input + output + cache writes; cache reads are re-reads, see 002).
        "max_tokens_run": 100000,
        "definition_of_done": [
            "Findings written to the ticket in plain language",
            "Every claim cites a file:line",
            "No file on disk was modified",
        ],
    },
    {
        "role": "reviewer",
        "project": None,
        "roster_slug": None,
        "model": "claude-sonnet-5",
        "write_capable": 0,
        "tools_allowed": ["Read", "Grep", "Glob"],
        "tools_denied": ["Edit", "Write", "Bash", "WebFetch", "Artifact", "Agent"],
        "read_scope": READ_SCOPE,
        "write_scope": None,
        "skills": [],
        "max_tokens_run": 40000,
        "definition_of_done": [
            "A verdict of approve / revise / reject with reasons",
            "The verdict is advisory to the PO, never binding",
            "Reviewed the diff, not the author's summary of the diff",
        ],
    },
]


def avatar_seed(role: str, project: str | None) -> str:
    """Deterministic sprite seed, so an agent's face never changes between runs."""
    return hashlib.sha256(f"{role}@{project or '*'}".encode()).hexdigest()[:12]


def seed_agents(conn: sqlite3.Connection) -> int:
    added = 0
    for a in STRUCTURAL_AGENTS:
        row = dict(a)
        for key in ("tools_allowed", "tools_denied", "read_scope", "write_scope",
                    "skills", "definition_of_done"):
            row[key] = json.dumps(row[key]) if row[key] is not None else None
        row["avatar_seed"] = avatar_seed(a["role"], a["project"])
        cur = conn.execute(
            """
            INSERT INTO agents (role, project, roster_slug, model, write_capable,
                                tools_allowed, tools_denied, read_scope, write_scope,
                                skills, max_tokens_run, definition_of_done, avatar_seed)
            VALUES (:role, :project, :roster_slug, :model, :write_capable,
                    :tools_allowed, :tools_denied, :read_scope, :write_scope,
                    :skills, :max_tokens_run, :definition_of_done, :avatar_seed)
            -- No conflict target: structural agents are unique by the partial
            -- index on role (migration 003), hired agents by (role, project).
            -- Naming one of those constraints makes `init` crash on the other.
            ON CONFLICT DO NOTHING
            """,
            row,
        )
        added += cur.rowcount if cur.rowcount > 0 else 0
    return added


def seed_sprint(conn: sqlite3.Connection) -> int | None:
    """Open sprint 1 if there isn't an active one.

    Sprint boundaries want to align to the Anthropic 7-day reset; until a real
    `usage_samples` row tells us when that is, today + 7 is the placeholder and
    the pulse corrects it on the first good sample.
    """
    existing = conn.execute("SELECT id FROM sprints WHERE status = 'active'").fetchone()
    if existing:
        return None

    start = date.today()
    cur = conn.execute(
        """
        INSERT INTO sprints (name, goal, starts_on, ends_on, budget_pct, status)
        VALUES (?, ?, ?, ?, 35.0, 'active')
        """,
        (
            "Sprint 1",
            "Stand up the ledger and a week of honest pulse logs.",
            start.isoformat(),
            (start + timedelta(days=7)).isoformat(),
        ),
    )
    return cur.lastrowid


def seed(conn: sqlite3.Connection) -> dict:
    return {"agents": seed_agents(conn), "sprint_id": seed_sprint(conn)}
