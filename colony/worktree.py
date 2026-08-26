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
BASE_DIR = db.RUNTIME_DIR / "bases"
GIT_TIMEOUT_S = 120

# Files a contract with `sees_secrets` gets a copy of. Names, not patterns: a
# pattern eventually matches something nobody meant to hand over.
SECRET_NAMES = (".env", ".env.local", ".env.development", "credentials.json",
                "service-account.json", "secrets.toml")

# One untracked file bigger than this is not source, and copying it into every
# worktree costs more than it is worth. `personal-desktop-projects` holds
# 590 MB of untracked binaries; without a limit a build there would copy them.
MAX_SEED_FILE_BYTES = 2 * 1024 * 1024
MAX_SEED_TOTAL_BYTES = 64 * 1024 * 1024

# Never copied into a worktree at any size: the colony's own runtime, which
# contains the worktree we are filling.
SEED_SKIP_DIRS = {".git", ".colony", "__pycache__", "node_modules", ".venv", "venv"}

# Git-ignored files are not all build output. Some of them are the state the
# code reads at runtime: `data/notion_sync_state.json` is the dedupe cache
# `apply.main auto` consults before it syncs anything, and a build agent that
# cannot see it cannot say one true thing about what the sync has already
# consumed. Ticket #80's agent reported the file "isn't in this worktree" and
# stopped there, which is why this pass exists.
#
# Generated directories are still left out, and they are told apart by count
# rather than by name: `packets/` holds over two thousand ignored files and is
# plainly output, `data/` holds two and is plainly state. A group above the
# threshold is skipped whole and named in the report so the agent is told what
# it is missing rather than left to guess.
MAX_IGNORED_PER_GROUP = 12


class WorktreeError(RuntimeError):
    """git refused. The caller turns this into a blocked ticket, never a crash."""


class PatchConflict(WorktreeError):
    """The patch landed, but part of it needs the PO to finish the merge.

    A separate type because the caller has to say something different. Every
    other failure means nothing changed on disk; this one means most of the
    patch is already in the working tree and some files have conflict markers
    in them. Telling the PO "the patch would not apply" when this happens sends
    him back to an editor full of files he thinks are untouched.
    """

    def __init__(self, message: str, paths: list[str]):
        super().__init__(message)
        self.paths = paths


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


def _in_scope(rel: str, scope: list[str]) -> bool:
    """Is this repo-relative path inside one of the agent's folders?"""
    return any(rel == f or rel.startswith(f + "/") for f in scope)


def _copy_into(rel: str, dest_root: Path) -> int:
    """One live file into the checkout. Returns the bytes copied, 0 if skipped."""
    src = ROOT / rel
    if not src.is_file():
        return 0
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest.stat().st_size


def _dirty_tracked() -> list[str]:
    """Tracked files whose live content differs from HEAD, staged or not."""
    raw = _git("status", "--porcelain", "-z", "--untracked-files=no")
    out, parts = [], raw.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:      # rename/copy carries a second path
            i += 1
        if "D" in xy:                   # a deletion has nothing to copy
            continue
        out.append(path)
    return out


def _untracked_in(scope: list[str]) -> list[str]:
    """Untracked, non-ignored files inside the agent's folders."""
    if not scope:
        return []
    raw = _git("ls-files", "--others", "--exclude-standard", "-z", "--", *scope)
    return [p for p in raw.split("\0") if p]


def _ignored_in(scope: list[str]) -> tuple[list[str], list[str]]:
    """Git-ignored files in scope that are runtime state, not build output.

    Returns the files worth copying and a description of each group that was
    skipped for being too numerous to be anything but generated output.

    Credential files are excluded here at every size. They have their own gate
    — `sees_secrets` on the contract — and a second door into the same room
    would make that gate a decoration.
    """
    if not scope:
        return [], []
    raw = _git("ls-files", "--others", "--ignored", "--exclude-standard", "-z",
               "--", *scope)
    groups: dict[tuple[str, str], list[str]] = {}
    for rel in (p for p in raw.split("\0") if p):
        if SEED_SKIP_DIRS & set(rel.split("/")):
            continue
        if rel.rsplit("/", 1)[-1] in SECRET_NAMES:
            continue
        folder = next((f for f in scope if rel == f or rel.startswith(f + "/")), "")
        rest = rel[len(folder):].lstrip("/")
        head = rest.split("/")[0] if "/" in rest else ""
        groups.setdefault((folder, head), []).append(rel)

    keep: list[str] = []
    dropped: list[str] = []
    for (folder, head), found in sorted(groups.items()):
        if len(found) <= MAX_IGNORED_PER_GROUP:
            keep.extend(found)
            continue
        where = "/".join(p for p in (folder, head) if p) or folder
        dropped.append(f"{where}/ ({len(found)} ignored files)")
    return sorted(keep), dropped


def _secret_files(scope: list[str]) -> list[str]:
    """Credential files worth handing to a contract that is allowed them.

    Gathered from the top-level project containing each scope folder, not from
    the scope folder alone. `job-search/assisted-apply` reads
    `job-search/job-radar/.env` on purpose -- same integration, same database --
    and a scope-only search would miss the file the code actually loads.
    """
    roots = {f.split("/")[0] for f in scope}
    found: list[str] = []
    for top in sorted(roots):
        base = ROOT / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.name not in SECRET_NAMES or not path.is_file():
                continue
            if SEED_SKIP_DIRS & set(path.relative_to(ROOT).parts):
                continue
            found.append(path.relative_to(ROOT).as_posix())
    return sorted(found)


def _base_file(ticket_id: int) -> Path:
    return BASE_DIR / f"ticket-{ticket_id}.tree"


def base_tree(ticket_id: int) -> str:
    """The tree the run started from. Falls back to HEAD for an older ticket."""
    path = _base_file(ticket_id)
    if path.is_file():
        sha = path.read_text(encoding="utf-8").strip()
        if sha:
            return sha
    return "HEAD"


def seed(ticket_id: int, scope: list[str], secrets: bool = False) -> dict:
    """Bring the checkout up to what is on disk, then record that as the base.

    Order matters. Everything git should diff against goes in first and gets
    written into a tree; the credential files go in after that, so they are not
    in the base and cannot appear in a patch as a deletion either -- `diff`
    removes them again before it looks.
    """
    path = path_for(ticket_id)
    scope = [f.strip("/") for f in (scope or []) if f.strip("/")]
    report = {"tracked": 0, "untracked": 0, "ignored": 0, "secrets": [],
              "skipped": [], "ignored_skipped": []}

    total = 0
    for rel in _dirty_tracked():
        if SEED_SKIP_DIRS & set(rel.split("/")):
            continue
        total += _copy_into(rel, path)
        report["tracked"] += 1

    for rel in _untracked_in(scope):
        if SEED_SKIP_DIRS & set(rel.split("/")):
            continue
        try:
            size = (ROOT / rel).stat().st_size
        except OSError:
            continue
        if size > MAX_SEED_FILE_BYTES or total + size > MAX_SEED_TOTAL_BYTES:
            report["skipped"].append(rel)
            continue
        total += _copy_into(rel, path)
        report["untracked"] += 1

    # Ignored state last of the three content passes, and still before the tree
    # is written. `git add -A` honours .gitignore, so none of this reaches the
    # base tree and none of it can turn up in a patch — the agent reads these
    # files and cannot ship them, which is exactly the arrangement `.env` has.
    keep, dropped = _ignored_in(scope)
    report["ignored_skipped"] = dropped
    for rel in keep:
        try:
            size = (ROOT / rel).stat().st_size
        except OSError:
            continue
        if size > MAX_SEED_FILE_BYTES or total + size > MAX_SEED_TOTAL_BYTES:
            report["skipped"].append(rel)
            continue
        total += _copy_into(rel, path)
        report["ignored"] += 1

    _git("add", "-A", cwd=path)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    _base_file(ticket_id).write_text(_git("write-tree", cwd=path).strip(),
                                     encoding="utf-8")

    if secrets:
        for rel in _secret_files(scope):
            if _copy_into(rel, path):
                report["secrets"].append(rel)

    return report


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
    _base_file(ticket_id).unlink(missing_ok=True)

    WORKTREE_DIR.mkdir(parents=True, exist_ok=True)
    branch = branch_for(ticket_id)
    # -B so a leftover branch from a reaped run does not block a retry. The
    # branch is ours, namespaced under colony/, and nothing else ever writes it.
    _git("worktree", "add", "-B", branch, str(path), "HEAD")
    return path


def _drop_secrets(ticket_id: int) -> None:
    """Take the credential files back out before git is allowed to look.

    They were copied in after the base tree was written, so git has never seen
    them. Removing them here keeps it that way whatever the agent did to them:
    a patch can never carry a key, and an agent that edited a `.env` finds the
    edit simply did not happen, which is the right answer.
    """
    path = path_for(ticket_id)
    for found in list(path.rglob("*")):
        if found.name in SECRET_NAMES and found.is_file():
            try:
                found.unlink()
            except OSError:
                pass


def diff(ticket_id: int) -> str:
    """Everything the run changed, as a patch against the seeded base.

    Against the base and not HEAD: the checkout was brought up to Jordan's
    uncommitted state before the agent started, so diffing against HEAD would
    hand back his own edits as though the agent had written them.
    """
    path = path_for(ticket_id)
    if not path.is_dir():
        return ""
    _drop_secrets(ticket_id)
    # Stage into the index first: `git diff` alone cannot see files the agent
    # created, and a new file is the most common thing a build ticket produces.
    _git("add", "-A", cwd=path)
    return _git("diff", "--cached", "--binary", base_tree(ticket_id), cwd=path)


def stat(ticket_id: int) -> str:
    path = path_for(ticket_id)
    if not path.is_dir():
        return ""
    try:
        return _git("diff", "--cached", "--stat", base_tree(ticket_id), cwd=path)
    except WorktreeError:
        return ""


def save_patch(ticket_id: int, text: str) -> Path:
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    out = PATCH_DIR / f"ticket-{ticket_id}.patch"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def conflicted() -> list[str]:
    """Paths git has left in a conflicted state in the live tree."""
    try:
        raw = _git("diff", "--name-only", "--diff-filter=U")
    except (WorktreeError, subprocess.SubprocessError, OSError):
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def apply_patch(ticket_id: int) -> dict:
    """Land an approved patch in the live tree, uncommitted.

    Two passes, and the order matters.

    The strict `git apply --index` goes first. It is all-or-nothing: either
    every hunk lands exactly as written or nothing is touched. When it succeeds
    the PO is looking at the patch he approved and nothing was guessed.

    Only when strict refuses do we fall back to `--3way`, which merges a patch
    written against a slightly older tree. That fallback is not a safe retry,
    and the old code treated it as one. `--3way --check` reports success when a
    merge is *possible*, not when it is clean, and a three-way apply that hits a
    conflict writes the files anyway: conflict markers in the working tree,
    stages 1/2/3 in the index, and a non-zero exit. The old code caught that
    exit and said "the patch would not apply", which was wrong twice — most of
    the patch had applied, and the PO was sent back to a tree with conflict
    markers in it that nothing had told him about.

    Between the two sits a plain worktree apply, for the common case where the
    only thing wrong is that the PO has staged work of his own on a file the
    patch touches. See the comment on it below.

    So a conflict is now reported as a conflict, by name, and the caller keeps
    the escalation open because finishing the merge is the PO's job.
    """
    patch = PATCH_DIR / f"ticket-{ticket_id}.patch"
    if not patch.is_file():
        raise WorktreeError("no saved patch for that ticket")
    text = patch.read_text(encoding="utf-8")
    if not text.strip():
        raise WorktreeError("the patch is empty — the run changed nothing")

    files = text.count("\ndiff --git ") + text.startswith("diff --git ")
    result = {"applied": True, "patch": str(patch), "files": files,
              "merged": False, "staged": True}

    try:
        _git("apply", "--index", "--check", str(patch))
        _git("apply", "--index", str(patch))
        return result
    except WorktreeError:
        pass

    # Both index-aware passes refuse with "does not match index" as soon as one
    # file the patch touches has staged work sitting on top of different
    # worktree content — `MM` in `git status`. That is Jordan's ordinary state
    # in assisted-apply, and it says nothing about whether the patch fits: the
    # same patch that git called unappliable passed `git apply --check` against
    # the worktree on the first try.
    #
    # So try the worktree on its own before reaching for the merge. This is
    # still all-or-nothing and still exact — every context line has to match
    # what is on disk — it just leaves the result unstaged, which is where an
    # applied patch was going to sit anyway.
    try:
        _git("apply", "--check", str(patch))
        _git("apply", str(patch))
        result["staged"] = False
        return result
    except WorktreeError:
        pass

    try:
        _git("apply", "--3way", str(patch))
    except WorktreeError as exc:
        stuck = conflicted()
        if not stuck:
            # Nothing on disk moved, so this really is a refusal.
            raise
        raise PatchConflict(
            f"{len(stuck)} file(s) need you to finish the merge: "
            + ", ".join(stuck), stuck) from exc

    result["merged"] = True
    return result


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
