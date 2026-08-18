"""The heartbeat. Two tiers, so an idle hour is free. ARCHITECTURE.md §4.

    tick   pure Python, no model, ZERO tokens. Runs every hour, always.
    wake   Ordis actually runs. Only when the tick found something.

This module implements the tick in full, and decides — but does not yet spawn —
the wake. A wake's reasons are recorded on the pulse row so the escalation bar
can be calibrated against a week of real logs before anything is dispatched.

Every path writes a `pulses` row. A pulse that finds nothing writes
`finding = 'clean'` with `tokens = 0`; a *missing* row is the alarm.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import db, notion
from .mirror import load_env

HALT_FILE = db.RUNTIME_DIR / "HALT"
USAGE_CACHE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "claude-usage" / "usage.json"
USAGE_STALE_AFTER = timedelta(minutes=20)

PULSE_INTERVAL = timedelta(hours=1)

# Folders the colony may be pointed at. Read is the whole tree; write is always
# one of these, per ticket, after approval. ARCHITECTURE.md §8.1.
PROJECTS_ROOT = db.PROJECTS_ROOT


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── tick steps ────────────────────────────────────────────────────────────────


def check_halt() -> bool:
    """A `.colony/HALT` file stops dispatch. Pulses keep logging — that's the point."""
    return HALT_FILE.exists()


def sample_usage(conn: sqlite3.Connection) -> dict | None:
    """Copy the tray app's last good read into `usage_samples`.

    We never call the usage endpoint ourselves: it allows ~5 requests per rolling
    5 minutes per account and the Claude Code CLI spends from the same bucket, so
    a second poller would earn 429s for both. One poller, one cache file.
    """
    if not USAGE_CACHE.is_file():
        return None

    mtime = datetime.fromtimestamp(USAGE_CACHE.stat().st_mtime)
    try:
        data = json.loads(USAGE_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    conn.execute(
        """
        INSERT INTO usage_samples (five_hour_pct, seven_day_pct, seven_day_resets_at, source_mtime)
        VALUES (?, ?, ?, ?)
        """,
        (
            data.get("five_hour"),
            data.get("seven_day"),
            data.get("seven_day_resets"),
            mtime.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return {
        "five_hour": data.get("five_hour"),
        "seven_day": data.get("seven_day"),
        "stale": datetime.now() - mtime > USAGE_STALE_AFTER,
    }


def candidate_projects() -> list[str]:
    """Every real project folder, one and two levels deep.

    A project is a folder with a `PROJECT.md` in it — that is the master
    CLAUDE.md's own definition, not a heuristic we invented, which is exactly why
    it is trustworthy here. Two levels because several projects are containers
    (`job-search/job-radar`) and a story names the sub-project, not the container.

    The first version of this walked every child directory instead, and matched
    "Run Hermes Agent alongside Claude Code" to `balatro-mod-loader/build` — a
    source folder is not a project, and a generic name like `build` or `data`
    will collide with ordinary English forever. Filtering on PROJECT.md removes
    the entire class of error rather than blacklisting names one at a time.
    """
    if not PROJECTS_ROOT.is_dir():
        return []

    folders: list[str] = []
    for top in sorted(PROJECTS_ROOT.iterdir()):
        if not top.is_dir() or top.name.startswith((".", "_")):
            continue
        folders.append(top.name)
        for child in sorted(top.iterdir()):
            if child.is_dir() and (child / "PROJECT.md").is_file():
                folders.append(f"{top.name}/{child.name}")
    return folders


def infer_project(title: str, description: str | None) -> tuple[str | None, bool]:
    """Guess which folder a story belongs to. Returns (project, confident).

    The Notion board has no Project property by design — the PO wants to keep it
    as a plain idea board. So the colony infers, and confirms once via the Inbox
    before anything is ever written. A guess is never enough to earn write scope.
    """
    if not PROJECTS_ROOT.is_dir():
        return None, False

    folders = candidate_projects()

    def norm(text: str) -> str:
        """Flatten punctuation so 'Job Radar:' and 'job-radar' are the same string."""
        return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "

    haystack = norm(f"{title} {description or ''}")

    def slug_of(path: str) -> str:
        return path.rsplit("/", 1)[-1]

    # The leaf name appearing as a phrase is a confident match; longest leaf
    # first so job-search/job-radar beats job-search. A softer word-overlap match
    # is a candidate that still has to be confirmed in the Inbox.
    for path in sorted(folders, key=lambda p: len(slug_of(p)), reverse=True):
        if norm(slug_of(path)) in haystack:
            return path, True
    for path in sorted(folders, key=lambda p: p.count("/"), reverse=True):
        # Every word of the name, not a subset of its longer ones. Matching a
        # subset let "…Claude Code" land on `job-search` off the bare word
        # "search"; requiring "job" too makes that impossible. A single-word
        # project can't reach here anyway — the phrase pass above already caught
        # it — so the soft match is always at least two words of real evidence.
        words = [w for w in norm(slug_of(path)).split() if len(w) >= 3]
        if len(words) < 2:
            continue
        if all(f" {w} " in haystack for w in words):
            return path, False
    return None, False


def sync_notion(conn: sqlite3.Connection) -> dict:
    """Upsert the board into `stories`. Returns what actually changed."""
    load_env()
    result = {"configured": True, "seen": 0, "new": [], "changed": [], "error": None}
    try:
        rows = notion.fetch_board()
    except notion.NotionUnconfigured as exc:
        return {**result, "configured": False, "error": str(exc)}
    except Exception as exc:  # network, auth, schema drift — all reportable, none fatal
        return {**result, "error": f"{type(exc).__name__}: {exc}"}

    existing = {
        r["notion_page_id"]: r
        for r in conn.execute(
            "SELECT notion_page_id, notion_hash, id, project FROM stories "
            "WHERE notion_page_id IS NOT NULL"
        )
    }
    result["seen"] = len(rows)

    for row in rows:
        prev = existing.get(row["notion_page_id"])
        if prev and prev["notion_hash"] == row["hash"]:
            conn.execute(
                "UPDATE stories SET notion_synced_at = ? WHERE id = ?", (now(), prev["id"])
            )
            continue

        project, confident = infer_project(row["title"], row["description"])
        # Exploring rows are research-only and never reach 'ready'. In Progress
        # rows land in the backlog for grooming — the PO, not the loop, marks
        # them ready (gate one).
        status = "backlog" if row["notion_status"] == notion.WORKABLE_STATUS else "needs-criteria"

        if prev:
            conn.execute(
                """
                UPDATE stories SET title=?, description=?, notion_status=?, category=?,
                       related_link=?, priority=?, notion_hash=?, notion_synced_at=?,
                       updated_at=?
                WHERE id = ?
                """,
                (row["title"], row["description"], row["notion_status"], row["category"],
                 row["related_link"], row["priority"], row["hash"], now(), now(), prev["id"]),
            )
            story_id = prev["id"]
            result["changed"].append(row["title"])
            kind, summary = "synced", "Notion row changed"
        else:
            cur = conn.execute(
                """
                INSERT INTO stories (notion_page_id, title, description, notion_status,
                                     category, related_link, priority, project,
                                     project_source, status, notion_hash, notion_synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (row["notion_page_id"], row["title"], row["description"], row["notion_status"],
                 row["category"], row["related_link"], row["priority"], project,
                 "inferred", status, row["hash"], now()),
            )
            story_id = cur.lastrowid
            result["new"].append(row["title"])
            kind, summary = "created", f"picked up from Notion ({row['notion_status']})"

            if project and not confident:
                conn.execute(
                    """
                    INSERT INTO escalations (story_id, kind, reason, recommendation)
                    VALUES (?, 'decision', ?, ?)
                    """,
                    (story_id,
                     f'"{row["title"]}" — I think this belongs to {project}/, but I am guessing.',
                     f"Confirm {project}/ so write scope is unambiguous before any ticket is staffed."),
                )
            elif not project:
                conn.execute(
                    """
                    INSERT INTO escalations (story_id, kind, reason, recommendation)
                    VALUES (?, 'needs-info', ?, ?)
                    """,
                    (story_id,
                     f'"{row["title"]}" — I cannot tell which project folder this belongs to.',
                     "Name the folder under D:\\ALL STUFF\\PROJECTS, or say it is a new project."),
                )

        conn.execute(
            "INSERT INTO story_events (story_id, kind, summary, detail) VALUES (?,?,?,?)",
            (story_id, kind, summary, row["description"]),
        )

    return result


def collect_finished_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Runs that ended since the last pulse. Empty until M3 dispatches anything."""
    return list(
        conn.execute(
            "SELECT * FROM runs WHERE status != 'running' AND ended_at IS NOT NULL "
            "AND ended_at > COALESCE((SELECT MAX(pulse_at) FROM pulses), '1970-01-01')"
        )
    )


def open_po_decisions(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM escalations "
        "WHERE resolved_at IS NOT NULL AND po_decision IS NOT NULL"
    ).fetchone()["n"]


# ── the pulse ─────────────────────────────────────────────────────────────────


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> int:
    """One heartbeat. Zero tokens: everything here is pure Python.

    The whole pulse is one transaction, so `dry_run` can roll it back and mean
    what it says. The connection is in autocommit, so without this the dry run's
    own `sample_usage` and `sync_notion` writes landed anyway — and the *next*
    real pulse then reported "0 new", hiding the six stories the dry run had
    quietly ingested. A preview that changes what it previews is worse than none.
    """
    conn.execute("BEGIN")
    try:
        code = _run(conn, dry_run=dry_run)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")
    return code


def _run(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    started = time.monotonic()
    last = conn.execute("SELECT MAX(pulse_at) AS at FROM pulses").fetchone()["at"]
    window_start = last or (datetime.now() - PULSE_INTERVAL).strftime("%Y-%m-%d %H:%M:%S")
    window_end = now()

    halted = check_halt()
    usage = sample_usage(conn)
    board = sync_notion(conn)
    finished = collect_finished_runs(conn)
    decisions = open_po_decisions(conn)

    # What makes this hour worth spending tokens on. Anything in this list means
    # a wake; an empty list means the tick already did the whole job for free.
    reasons: list[str] = []
    if board["new"]:
        reasons.append(f"{len(board['new'])} new stor{'y' if len(board['new']) == 1 else 'ies'}")
    if board["changed"]:
        reasons.append(f"{len(board['changed'])} story edit(s)")
    if finished:
        reasons.append(f"{len(finished)} finished run(s) to harvest")
    if decisions:
        reasons.append(f"{decisions} PO decision(s) to act on")

    anomalies = 0
    notes: list[str] = []
    if halted:
        notes.append("HALT present — dispatch disabled")
    if not board["configured"]:
        notes.append("notion: unconfigured")
    elif board["error"]:
        notes.append(f"notion: {board['error']}")
        anomalies += 1
    if usage is None:
        notes.append("usage: no cache — is the tray app running?")
    elif usage["stale"]:
        notes.append("usage: cache stale")
        anomalies += 1
    elif usage["seven_day"] is not None and usage["seven_day"] >= 80:
        notes.append(f"usage: week at {usage['seven_day']}%")

    tier = "wake" if (reasons and not halted) else "tick"
    finding = "; ".join(reasons + notes) if (reasons or notes) else "clean"

    if not dry_run:
        conn.execute(
            """
            INSERT INTO pulses (pulse_at, tier, window_start, window_end, actions,
                                finding, anomalies, tokens, duration_ms, next_pulse_at)
            VALUES (?,?,?,?,?,?,?,0,?,?)
            """,
            (
                window_end, tier, window_start, window_end,
                json.dumps({
                    "notion": {k: board[k] for k in ("configured", "seen", "new", "changed", "error")},
                    "usage": usage,
                    "finished_runs": len(finished),
                    "halted": halted,
                    "wake_reasons": reasons,
                }),
                finding, anomalies,
                int((time.monotonic() - started) * 1000),
                (datetime.now() + PULSE_INTERVAL).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    if board["configured"]:
        board_line = (
            f"{board['seen']} rows · {len(board['new'])} new · {len(board['changed'])} changed"
        )
    else:
        board_line = "unconfigured (set NOTION_TOKEN in colony-dash/.env)"

    if usage:
        usage_line = f"5h {usage['five_hour']}%  ·  7d {usage['seven_day']}%"
        if usage["stale"]:
            usage_line += "  (stale)"
    else:
        usage_line = "no sample"

    prefix = "DRY " if dry_run else ""
    print(f"{prefix}PULSE {window_end}  ·  {tier}  ·  window {window_start} → {window_end}")
    print(f"  notion    {board_line}")
    print(f"  usage     {usage_line}")
    print(f"  finding   {finding}")
    print("  tokens    0   (the heartbeat is free; only the work costs)")
    if tier == "wake":
        print(f"  wake      queued — dispatch lands in M3: {', '.join(reasons)}")
    return 0
