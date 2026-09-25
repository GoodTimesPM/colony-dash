r"""Write isolation: an agent never edits the tree you are working in.

A write-capable run gets its own `git worktree` at
`.colony/worktrees/ticket-<id>` on its own branch, and its write scope is
one project folder inside it. The result comes back as a patch in
`.colony/patches/` with a `write-approval` escalation. If approved, the
patch is applied to the live tree **uncommitted**, and the PO commits it.
The colony never commits to master, pushes or rewrites history
(ARCHITECTURE.md §8.3).

A worktree rather than a copy keeps the git context and makes the diff free.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import db, proc as proc_mod
from .secretfiles import is_secret

ROOT = db.PROJECTS_ROOT
WORKTREE_DIR = db.RUNTIME_DIR / "worktrees"
PATCH_DIR = db.RUNTIME_DIR / "patches"
BASE_DIR = db.RUNTIME_DIR / "bases"
GIT_TIMEOUT_S = 120

# Untracked files above this are not source and are not copied.
MAX_SEED_FILE_BYTES = 2 * 1024 * 1024
MAX_SEED_TOTAL_BYTES = 64 * 1024 * 1024

# Never copied into a worktree at any size: the colony's own runtime, which
# contains the worktree we are filling.
SEED_SKIP_DIRS = {".git", ".colony", "__pycache__", "node_modules", ".venv", "venv"}

# Git-ignored runtime state (e.g. a sync's dedupe cache) is copied so the agent
# can read it. Groups above this count are generated output, skipped and named
# in the report.
MAX_IGNORED_PER_GROUP = 12


class WorktreeError(RuntimeError):
    """git refused. The caller turns this into a blocked ticket, never a crash."""


class PatchConflict(WorktreeError):
    """The patch landed, but some files have conflict markers. Separate from
    other failures because most of the change is already on disk.
    """

    def __init__(self, message: str, paths: list[str]):
        super().__init__(message)
        self.paths = paths


class OutOfScope(WorktreeError):
    """The patch touches files outside the agent's write scope. Nothing was applied."""

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


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of a path with unusual characters in it."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        raw = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        return raw.encode("latin-1").decode("utf-8", "replace")
    return path


def _side(value: str, prefix: str) -> str | None:
    """A `---`/`+++` path with its a/ or b/ prefix removed, or None for /dev/null."""
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        return '"' + value[1:].removeprefix(prefix)
    return value.removeprefix(prefix)


def patch_files(text: str) -> list[str]:
    """Paths a patch touches, in order: `+++ b/`, `--- a/` for deletions, then
    `rename to`, then the header.
    """
    out: list[str] = []
    block: dict[str, str | None] | None = None

    def close() -> None:
        if block is not None:
            path = (block.get("new") or block.get("rename")
                    or block.get("old") or block["header"])
            out.append(_unquote(path))

    for line in text.splitlines():
        if line.startswith("diff --git "):
            close()
            rest = line[len("diff --git "):]
            half = (len(rest) - 1) // 2
            if rest[half:half + 1] == " " and rest[2:half] == rest[half + 3:]:
                header = rest[half + 3:]
            else:
                header = rest.split(" b/", 1)[-1]
            block = {"header": header}
        elif block is None:
            continue
        elif line.startswith("+++ "):
            block["new"] = _side(line[4:], "b/")
        elif line.startswith("--- "):
            block["old"] = _side(line[4:], "a/")
        elif line.startswith("rename to "):
            block["rename"] = line[len("rename to "):]
    close()
    return out


def outside_scope(files: list[str], scope: list[str]) -> list[str]:
    """The paths in `files` that no folder in `scope` covers."""
    return [f for f in files if not _in_scope(f, scope)]


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
    """Git-ignored runtime state in scope, and a description of each group
    skipped as generated. Credential files are never included;
    `sees_secrets` is their gate.
    """
    if not scope:
        return [], []
    raw = _git("ls-files", "--others", "--ignored", "--exclude-standard", "-z",
               "--", *scope)
    groups: dict[tuple[str, str], list[str]] = {}
    for rel in (p for p in raw.split("\0") if p):
        if SEED_SKIP_DIRS & set(rel.split("/")):
            continue
        if is_secret(rel):
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
    """Credential files for a contract allowed them, gathered from each scope's
    top-level project, since subprojects share their parent's `.env`.
    """
    roots = {f.split("/")[0] for f in scope}
    found: list[str] = []
    for top in sorted(roots):
        base = ROOT / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not is_secret(path.name) or not path.is_file():
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
    Credential files go in after the base tree, so they never appear in a
    patch.
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

    # Ignored state before the tree is written; `git add -A` skips it, so it
    # can be read but never shipped.
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
    """Open an isolated checkout for one ticket, branched from local HEAD. A
    stale one from an earlier attempt is removed first.
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
    """Remove the credential files before git looks, so a patch can never carry
    a key and edits to them are dropped.
    """
    path = path_for(ticket_id)
    for found in list(path.rglob("*")):
        if is_secret(found.name) and found.is_file():
            try:
                found.unlink()
            except OSError:
                pass


def diff(ticket_id: int) -> str:
    """Everything the run changed, as a patch against the seeded base, not
    HEAD, which would include the PO's own uncommitted edits.
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


def apply_patch(ticket_id: int, scope: list[str] | None = None) -> dict:
    """Land an approved patch in the live tree, uncommitted.

    Three passes, strictest first: `git apply --index`, then a worktree-only
    apply (for files with staged work), then `--3way`, which can leave
    conflict markers and raises `PatchConflict`. With `scope`, a patch
    touching anything outside it is refused before git runs.
    """
    patch = PATCH_DIR / f"ticket-{ticket_id}.patch"
    if not patch.is_file():
        raise WorktreeError("no saved patch for that ticket")
    text = patch.read_text(encoding="utf-8")
    if not text.strip():
        raise WorktreeError("the patch is empty, the run changed nothing")

    paths = patch_files(text)
    if scope:
        stray = outside_scope(paths, scope)
        if stray:
            raise OutOfScope(
                f"{len(stray)} file(s) fall outside the write scope "
                f"({', '.join(scope)}): " + ", ".join(stray), stray)
    result = {"applied": True, "patch": str(patch), "files": len(paths),
              "merged": False, "staged": True}

    try:
        _git("apply", "--index", "--check", str(patch))
        _git("apply", "--index", str(patch))
        return result
    except WorktreeError:
        pass

    # Index-aware applies refuse when a touched file has staged changes (`MM`),
    # even though the patch fits the worktree. Try the worktree alone before
    # merging.
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
    """Remove the checkout. The branch and saved patch stay as the record."""
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
