r"""Write isolation — an agent never edits the tree you are working in.

A write-capable run gets its own `git worktree`: a full checkout at
`.colony/worktrees/ticket-<id>`, on its own branch, sharing the object store but
nothing else. The agent's write scope is one project folder *inside* that
checkout, so the worst thing a runaway run can do is make a mess in a directory
we are about to delete.

The handoff back is a **patch, not a merge**. When the run finishes we take a
diff, park it in `.colony/patches/`, and raise a `write-approval` escalation. If
the PO approves, the patch is applied to the live tree and left **uncommitted** —
Jordan reviews it in his own editor and commits it himself. The colony never
runs `git commit` on master, never pushes, and never rewrites history. That is
not a policy the agents are asked to follow; it is a capability they were not
given (ARCHITECTURE.md §8.3).

Why a worktree and not a copy: a copy loses the git context an agent needs to
work sensibly, and a copy's changes cannot be turned into a reviewable diff
without reinventing diff. The worktree gives isolation and reviewability from
the same mechanism.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import db, proc as proc_mod

ROOT = db.PROJECTS_ROOT
WORKTREE_DIR = db.RUNTIME_DIR / "worktrees"
PATCH_DIR = db.RUNTIME_DIR / "patches"
GIT_TIMEOUT_S = 120


class WorktreeError(RuntimeError):
    """git refused. The caller turns this into a blocked ticket, never a crash."""


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = proc_mod.run(
        ["git", *args],
        cwd=str(cwd or ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=GIT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise WorktreeError((proc.stderr or proc.stdout or "git failed").strip()[:600])
    return proc.stdout


def branch_for(ticket_id: int) -> str:
    return f"colony/ticket-{ticket_id}"


def path_for(ticket_id: int) -> Path:
    return WORKTREE_DIR / f"ticket-{ticket_id}"


def create(ticket_id: int) -> Path:
    """Open an isolated checkout for one ticket.

    Branched from the current HEAD rather than from a remote: the colony works
    on what is on this machine right now, which is what the PO can actually
    review. A stale worktree from a previous attempt is torn down first — a
    half-finished checkout is not evidence worth keeping.
    """
    path = path_for(ticket_id)
    if path.exists():
        remove(ticket_id)

    WORKTREE_DIR.mkdir(parents=True, exist_ok=True)
    branch = branch_for(ticket_id)
    # -B so a leftover branch from a reaped run does not block a retry. The
    # branch is ours, namespaced under colony/, and nothing else ever writes it.
    _git("worktree", "add", "-B", branch, str(path), "HEAD")
    return path


def diff(ticket_id: int) -> str:
    """Everything the run changed, as a patch against the branch point."""
    path = path_for(ticket_id)
    if not path.is_dir():
        return ""
    # Stage into the index first: `git diff` alone cannot see files the agent
    # created, and a new file is the most common thing a build ticket produces.
    _git("add", "-A", cwd=path)
    return _git("diff", "--cached", "--binary", cwd=path)


def stat(ticket_id: int) -> str:
    path = path_for(ticket_id)
    if not path.is_dir():
        return ""
    try:
        return _git("diff", "--cached", "--stat", cwd=path)
    except WorktreeError:
        return ""


def save_patch(ticket_id: int, text: str) -> Path:
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    out = PATCH_DIR / f"ticket-{ticket_id}.patch"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def apply_patch(ticket_id: int) -> dict:
    """Land an approved patch in the live tree, uncommitted.

    `--3way` so a patch that was written against a slightly older tree still
    applies cleanly where it can. `--check` runs first: a patch that will not
    apply must be reported as a refusal, not discovered halfway through with
    half the files written.
    """
    patch = PATCH_DIR / f"ticket-{ticket_id}.patch"
    if not patch.is_file():
        raise WorktreeError("no saved patch for that ticket")
    text = patch.read_text(encoding="utf-8")
    if not text.strip():
        raise WorktreeError("the patch is empty — the run changed nothing")

    _git("apply", "--3way", "--check", str(patch))
    _git("apply", "--3way", str(patch))
    return {"applied": True, "patch": str(patch),
            "files": text.count("\ndiff --git ") + text.startswith("diff --git ")}


def remove(ticket_id: int) -> None:
    """Tear down the checkout. The branch and the saved patch survive.

    The patch is the record of what was proposed and has to outlive the
    scaffolding that produced it — a rejected change you can no longer read is a
    decision you cannot revisit.
    """
    path = path_for(ticket_id)
    try:
        _git("worktree", "remove", "--force", str(path))
    except (WorktreeError, subprocess.SubprocessError, OSError):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    try:
        _git("worktree", "prune")
    except (WorktreeError, subprocess.SubprocessError, OSError):
        pass


def live() -> list[dict]:
    """Worktrees git currently knows about. The panel's "is anything isolated?"."""
    try:
        raw = _git("worktree", "list", "--porcelain")
    except (WorktreeError, subprocess.SubprocessError, OSError):
        return []
    out, cur = [], {}
    for line in raw.splitlines():
        if not line.strip():
            if cur.get("branch", "").startswith("refs/heads/colony/"):
                out.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    if cur.get("branch", "").startswith("refs/heads/colony/"):
        out.append(cur)
    return out
