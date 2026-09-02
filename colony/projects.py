r"""What changed in the project folders — the file manager the PO asked for.

The colony's whole read scope is `db.PROJECTS_ROOT`, which is one git repo
containing every project. So "what changed in job-radar this week" is a
`git status`/`git log` question scoped to a path prefix, not a filesystem walk,
and asking git is both faster and truer: git already knows what is tracked, what
is ignored, and what moved since the last commit.

Two callers, one set of functions:

  * the **pulse** samples this every hour and records anything that moved into
    `project_changes`, so the log has something to say on an hour where Notion
    was quiet but you refactored for three hours;
  * the **dashboard** calls it live for the Projects panel and the diff drawer.

Everything here is read-only. `git status`, `git log` and `git diff` do not
write to the repository, and no command in this module takes an argument that
could turn into one.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from . import db, proc as proc_mod

ROOT = db.PROJECTS_ROOT
GIT_TIMEOUT_S = 25

# A diff can be arbitrarily large; the drawer is not a code editor. Truncating
# with an explicit marker is honest; silently rendering 40k lines is not.
DIFF_LIMIT_LINES = 1200

SEP = "\x02"
REC = "\x01"


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run one read-only git command. Returns '' rather than raising.

    A project that is not a git repo, or a git that is not installed, must
    degrade the panel — never take the pulse down with it.
    """
    try:
        proc = proc_mod.run(
            ["git", *args],
            cwd=str(cwd or ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def project_dirs() -> list[str]:
    """Every project folder, one and two levels deep.

    Same definition the pulse uses — a folder with a PROJECT.md in it — so the
    two never disagree about what counts as a project.
    """
    if not ROOT.is_dir():
        return []
    out: list[str] = []
    for top in sorted(ROOT.iterdir()):
        if not top.is_dir() or top.name.startswith((".", "_")):
            continue
        out.append(top.name)
        for child in sorted(top.iterdir()):
            if child.is_dir() and (child / "PROJECT.md").is_file():
                out.append(f"{top.name}/{child.name}")
    return out


# The word "modified" is only meaningful next to the thing it is modified
# *against*, and on this page that thing is one specific commit: every project
# folder lives inside a single git repo, so "modified" means "different from
# HEAD of that repo" for all sixty of them at once. The panel used to state the
# count and leave the baseline implicit, which is how a reader ends up asking
# "modified relative to what?" — a fair question with no answer on screen. The
# subject and date ride along with the sha so the baseline can be named in
# words rather than as seven hex digits nobody recognises.
def head() -> dict:
    """Branch and HEAD of the master projects repo — the baseline for "modified"."""
    line = _git("log", "-1", f"--pretty=format:%h{SEP}%s{SEP}%ad",
                "--date=format:%Y-%m-%d %H:%M").strip()
    parts = line.split(SEP) if line else []
    sha, subject, at = (parts + ["", "", ""])[:3]
    return {
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD").strip() or "?",
        "sha": sha or _git("rev-parse", "--short", "HEAD").strip() or "",
        "subject": subject,
        "at": at,
        "root": str(ROOT),
    }


# What each porcelain bucket is, in the words a person would use for it. `git`
# says "untracked", which is a statement about git's index and reads like an
# accusation; what it means to the PO is "a file that has never been committed"
# — which is exactly the category their mod loader keeps filling with folders they
# never typed. Naming them properly is most of the fix for "modifications I
# can't find".
KIND_SHORT = {"modified": "edited", "added": "added", "deleted": "deleted",
              "untracked": "new"}
KIND_LONG = {
    "modified":  "tracked files edited since that commit",
    "added":     "new files staged for the next commit",
    "deleted":   "tracked files removed from disk",
    "untracked": "files on disk git has never been told about",
}


def _status_porcelain() -> list[tuple[str, str]]:
    """(xy, path) for every changed file in the whole tree, in one call.

    One `git status` for the repo rather than one per project: sixty projects
    would be sixty subprocesses an hour and every answer comes out of the same
    index anyway. Bucketing by path prefix afterwards is free.
    """
    raw = _git("status", "--porcelain=v1", "-uall")
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:].strip()
        if " -> " in path:              # a rename belongs to its destination
            path = path.split(" -> ", 1)[1]
        out.append((xy, path.strip('"')))
    return out


def _bucket(path: str, projects: list[str]) -> str | None:
    """Which project a changed file belongs to.

    Longest prefix wins, so a file in `job-search/job-radar` is not filed under
    `job-search`.
    """
    norm = path.replace("\\", "/")
    best = None
    for p in projects:
        if norm == p or norm.startswith(p + "/"):
            if best is None or len(p) > len(best):
                best = p
    return best


def _blank(project: str) -> dict:
    return {"project": project, "dirty_files": 0, "added": 0, "modified": 0,
            "deleted": 0, "untracked": 0, "commits_since": 0, "files": [],
            "touched_at": None, "branch": "", "head_sha": ""}


def _mtime(path: str) -> str | None:
    """When the working tree last changed under a project, as a wall-clock string.

    Deliberately the file's own mtime rather than a `git log` date: these rows are
    *uncommitted* changes, so the last commit says nothing about when you last
    touched them. A deleted file cannot be stat'd and simply does not count — it
    is the one change whose time git alone would know, and one missing sample out
    of hundreds does not move a max.
    """
    try:
        ts = (ROOT / path).stat().st_mtime
    except OSError:
        return None
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def scan(since: str | None = None) -> list[dict]:
    """Per-project working-tree state, plus commits since a timestamp.

    `since` is a git date string — the previous pulse's time. Without it the
    commit counts are left at zero rather than computed over all history: a
    number nobody asked for is a number the pulse pays for every hour.
    """
    projects = project_dirs()
    if not projects:
        return []

    # Read once, stamped on every row. It is the same repo for all of them, and a
    # row that travels without its baseline is a row that cannot say what it is a
    # change *to* once it reaches the drawer.
    at_head = head()

    agg: dict[str, dict] = {}
    for xy, path in _status_porcelain():
        proj = _bucket(path, projects)
        if not proj:
            continue
        row = agg.setdefault(proj, _blank(proj))
        row["dirty_files"] += 1
        if xy == "??":
            row["untracked"] += 1
        elif "D" in xy:
            row["deleted"] += 1
        elif "A" in xy:
            row["added"] += 1
        else:
            row["modified"] += 1
        if len(row["files"]) < 40:
            row["files"].append({"xy": xy.strip() or "?", "path": path})
        # Every dirty path is stat'd, not just the forty the drawer shows: the
        # panel sorts on this, and a max taken over a truncated sample is a
        # timestamp that quietly lies about the busiest folders.
        seen = _mtime(path)
        if seen and (row["touched_at"] is None or seen > row["touched_at"]):
            row["touched_at"] = seen

    if since:
        raw = _git("log", f"--since={since}", "--name-only",
                   f"--pretty=format:{REC}%H{SEP}%s")
        per: dict[str, set] = {}
        sha = None
        for line in raw.splitlines():
            if line.startswith(REC):
                sha = line[1:].split(SEP, 1)[0]
                continue
            if not line.strip() or not sha:
                continue
            proj = _bucket(line.strip(), projects)
            if proj:
                per.setdefault(proj, set()).add(sha)
        for proj, shas in per.items():
            agg.setdefault(proj, _blank(proj))["commits_since"] = len(shas)

    for row in agg.values():
        row["branch"], row["head_sha"] = at_head["branch"], at_head["sha"]
        bits = []
        if row["commits_since"]:
            bits.append(f"{row['commits_since']} commit{'s' if row['commits_since'] != 1 else ''}")
        if row["dirty_files"]:
            parts = [f"{row[k]} {KIND_SHORT[k]}"
                     for k in ("modified", "added", "deleted", "untracked") if row[k]]
            bits.append(", ".join(parts))
        row["summary"] = " · ".join(bits) or "no change"

    return sorted(agg.values(),
                  key=lambda r: (-r["commits_since"], -r["dirty_files"], r["project"]))


def commits(project: str, limit: int = 12) -> list[dict]:
    """Recent commits touching one project."""
    raw = _git("log", f"-{limit}", f"--pretty=format:%h{SEP}%an{SEP}%ad{SEP}%s",
               "--date=format:%Y-%m-%d %H:%M", "--", project)
    out = []
    for line in raw.splitlines():
        parts = line.split(SEP)
        if len(parts) == 4:
            out.append({"sha": parts[0], "author": parts[1], "at": parts[2], "subject": parts[3]})
    return out


def diff(project: str, path: str | None = None) -> str:
    """The working-tree diff for a project, or for one file inside it.

    Untracked files have no diff — git will say nothing about them — so they are
    shown as their own first lines rather than as an empty change, which reads
    like a bug in the panel.
    """
    target = path or project
    stat = _git("diff", "--stat", "--", target) if path is None else ""
    body = _git("diff", "--", target)

    if not body.strip():
        full = ROOT / target
        if full.is_file():
            try:
                lines = full.read_text(encoding="utf-8", errors="replace").splitlines()[:200]
                return f"(untracked — new file, first {len(lines)} lines)\n\n" + "\n".join(lines)
            except OSError:
                pass
        return stat or "(no working-tree changes — everything here is committed)"

    lines = body.splitlines()
    if len(lines) > DIFF_LIMIT_LINES:
        lines = lines[:DIFF_LIMIT_LINES]
        lines.append(f"\n… truncated at {DIFF_LIMIT_LINES} lines. Full diff: git diff -- {target}")
    return (stat + "\n" if stat else "") + "\n".join(lines)


# ── the file tree ─────────────────────────────────────────────────────────────
#
# `scan()` answers "what moved", which is the right question for the pulse log
# and the wrong one for a file manager: a project with nothing uncommitted
# vanishes from it entirely, so a panel built on it looks empty exactly when the
# tree is tidy. This half answers "what is there", and hangs the change state off
# it as decoration rather than as the reason a row exists.
#
# It is a lazy tree — one directory per request — because the root has sixty
# projects under it and some of those have `node_modules`. Walking eagerly to
# render a collapsed row is how a file panel becomes the slowest thing on a page.

# Never listed, never read, at any depth. `.git` because its internals are not
# files anybody browses and one of them is a credential store; `.env` and its
# neighbours because the read scope in §8 excludes secrets from *every* tier,
# and a dashboard is a tier.
HIDDEN_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv",
                ".mypy_cache", ".pytest_cache", ".ruff_cache"}
SECRET_NAMES = {".env", ".env.local", ".env.production", "credentials.json",
                "token.json", "secrets.json", ".npmrc", ".netrc", "id_rsa"}
READ_LIMIT_BYTES = 400_000
TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".html",
                 ".css", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sql", ".sh",
                 ".ps1", ".cmd", ".bat", ".xml", ".csv", ".lua", ".c", ".h", ".cpp",
                 ".cs", ".java", ".rb", ".go", ".rs", ".gitignore", ".env.example"}


def is_secret(name: str) -> bool:
    low = name.lower()
    return low in SECRET_NAMES or low.startswith(".env")


def safe_path(rel: str) -> Path:
    """Resolve a browser-supplied path inside the root, or refuse.

    Everything the tree endpoints touch comes through here. The check is on the
    *resolved* path, so a symlink that points out of the tree fails the same way
    a `..` does.
    """
    rel = (rel or "").replace("\\", "/").strip("/")
    full = (ROOT / rel).resolve() if rel else ROOT.resolve()
    root = ROOT.resolve()
    if full != root and root not in full.parents:
        raise ValueError("outside the projects root")
    parts = [p for p in rel.split("/") if p]
    if any(p in HIDDEN_NAMES or is_secret(p) for p in parts):
        raise ValueError("not a path this dashboard will open")
    return full


def _status_map() -> dict[str, str]:
    """Every changed path in the repo, forward-slashed, keyed to its git code."""
    return {path.replace("\\", "/"): (xy.strip() or "?")
            for xy, path in _status_porcelain()}


def _state_for(xy: str) -> str:
    if xy == "??":
        return "untracked"
    if "D" in xy:
        return "deleted"
    if "A" in xy:
        return "added"
    return "modified"


def tree(rel: str = "", *, status: dict[str, str] | None = None) -> dict:
    """One directory's children, folders first, with change state attached.

    A folder's `changed` count is how many changed files are anywhere beneath
    it, which is the number that makes a collapsed row worth expanding.
    """
    full = safe_path(rel)
    if not full.is_dir():
        raise ValueError("not a folder")
    status = _status_map() if status is None else status
    prefix = (rel.replace("\\", "/").strip("/") + "/") if rel else ""

    dirs, files = [], []
    for child in sorted(full.iterdir(), key=lambda c: c.name.lower()):
        name = child.name
        if name in HIDDEN_NAMES or is_secret(name):
            continue
        path = prefix + name
        if child.is_dir():
            beneath = sum(1 for p in status if p.startswith(path + "/"))
            dirs.append({
                "name": name, "path": path, "kind": "dir", "changed": beneath,
                "project": (child / "PROJECT.md").is_file(),
            })
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            xy = status.get(path)
            files.append({
                "name": name, "path": path, "kind": "file", "size": size,
                "state": _state_for(xy) if xy else None,
                "readable": child.suffix.lower() in TEXT_SUFFIXES or not child.suffix,
            })
    return {"path": rel, "dirs": dirs, "files": files,
            "changed": sum(1 for p in status if p.startswith(prefix)) if prefix else len(status)}


def read_file(rel: str) -> dict:
    """A file's text, for the drawer. Read-only, capped, text only."""
    full = safe_path(rel)
    if not full.is_file():
        raise ValueError("not a file")
    size = full.stat().st_size
    if size > READ_LIMIT_BYTES:
        return {"path": rel, "size": size, "text": None,
                "why": f"{size:,} bytes — too big to open here. It is on disk at {full}."}
    try:
        raw = full.read_bytes()
    except OSError as exc:
        return {"path": rel, "size": size, "text": None, "why": str(exc)}
    if b"\x00" in raw[:4096]:
        return {"path": rel, "size": size, "text": None,
                "why": "binary — nothing useful to show as text."}
    return {"path": rel, "size": size,
            "text": raw.decode("utf-8", errors="replace"), "why": None}


# How many of a folder's changed files get written into the log. The drawer lists
# them, and a list is only readable while it is a list — past a dozen it is a
# wall, and the diff view next to it is the better place to read a wall.
FILES_LOGGED = 12


def record(conn, pulse_id: int | None, rows: list[dict]) -> int:
    """Write this pulse's findings into `project_changes`. Returns rows written.

    The deltas, the file list and `moved_by` are attached upstream by the pulse,
    which is the only caller that knows what the previous sample was and what the
    colony did in the window. They are stored rather than recomputed because the
    drawer is reading a beat that happened hours ago: by the time anybody opens
    it, "the previous sample" is a different row and the files have moved on.
    """
    written = 0
    for r in rows:
        if not (r["dirty_files"] or r["commits_since"]):
            continue
        conn.execute(
            """INSERT INTO project_changes (pulse_id, project, branch, head_sha,
                                            dirty_files, added, modified,
                                            deleted, untracked, commits_since, summary,
                                            d_added, d_modified, d_deleted, d_untracked,
                                            files, moved_by, touched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pulse_id, r["project"], r.get("branch"), r.get("head_sha"),
             r["dirty_files"], r["added"], r["modified"],
             r["deleted"], r["untracked"], r["commits_since"], r["summary"],
             r.get("d_added"), r.get("d_modified"), r.get("d_deleted"), r.get("d_untracked"),
             json.dumps(r.get("files", [])[:FILES_LOGGED]), r.get("moved_by"), r.get("touched_at")),
        )
        written += 1
    return written
