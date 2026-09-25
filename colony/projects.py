r"""What changed in the project folders.

Every project lives in one git repo under `db.PROJECTS_ROOT`, so change
tracking is `git status` and `git log` scoped by path prefix. Two callers:
the pulse records movement hourly in `project_changes`, and the dashboard
calls it live for the Projects panel and diff drawer. Everything here is
read-only.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from . import db, proc as proc_mod
from .secretfiles import is_secret

ROOT = db.PROJECTS_ROOT
GIT_TIMEOUT_S = 25

# A diff can be arbitrarily large; the drawer is not a code editor. Truncating
# with an explicit marker is honest; silently rendering 40k lines is not.
DIFF_LIMIT_LINES = 1200

SEP = "\x02"
REC = "\x01"


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run one read-only git command. Returns '' instead of raising, so a
    missing git degrades the panel rather than the pulse.
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
    """Every folder with a PROJECT.md, one and two levels deep (the pulse's
    rule).
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


# The commit "modified" is measured against, with subject and date so the panel
# can name it.
def head() -> dict:
    """Branch and HEAD of the master projects repo. The baseline for "modified"."""
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


# Plain-language names for porcelain buckets ("never committed", not
# "untracked").
KIND_SHORT = {"modified": "edited", "added": "added", "deleted": "deleted",
              "untracked": "new"}
KIND_LONG = {
    "modified":  "tracked files edited since that commit",
    "added":     "new files staged for the next commit",
    "deleted":   "tracked files removed from disk",
    "untracked": "files on disk git has never been told about",
}


def _status_porcelain() -> list[tuple[str, str]]:
    """(xy, path) for every changed file, from one `git status` for the whole
    repo.
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
    """The project a changed file belongs to. Longest prefix wins."""
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
    """When the working tree last changed under a project, from file mtimes,
    since these changes are uncommitted. Deleted files are skipped.
    """
    try:
        ts = (ROOT / path).stat().st_mtime
    except OSError:
        return None
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def scan(since: str | None = None) -> list[dict]:
    """Per-project working-tree state, plus commits since `since` (a git date).
    No `since` means zero commit counts rather than a full-history walk.
    """
    projects = project_dirs()
    if not projects:
        return []

    # The baseline, stamped on every row so each can say what it differs from.
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
        # Stat every dirty path; the panel sorts on the max.
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
    """The working-tree diff for a project or one file. Untracked files are
    listed first, since git shows no diff for them.
    """
    target = path or project
    stat = _git("diff", "--stat", "--", target) if path is None else ""
    body = _git("diff", "--", target)

    if not body.strip():
        full = ROOT / target
        if full.is_file():
            try:
                lines = full.read_text(encoding="utf-8", errors="replace").splitlines()[:200]
                return f"(untracked, new file, first {len(lines)} lines)\n\n" + "\n".join(lines)
            except OSError:
                pass
        return stat or "(no working-tree changes, everything here is committed)"

    lines = body.splitlines()
    if len(lines) > DIFF_LIMIT_LINES:
        lines = lines[:DIFF_LIMIT_LINES]
        lines.append(f"\n… truncated at {DIFF_LIMIT_LINES} lines. Full diff: git diff -- {target}")
    return (stat + "\n" if stat else "") + "\n".join(lines)


# ── the file tree ─────────────────────────────────────────────────────────────
# The browsable tree: what is there, with change state attached, so a tidy
# project still shows. Lazy, one directory per request.

# Never listed or read. `.git` holds a credential store, and §8 keeps secrets
# from every tier, the dashboard included.
HIDDEN_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv",
                ".mypy_cache", ".pytest_cache", ".ruff_cache"}
READ_LIMIT_BYTES = 400_000
TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".html",
                 ".css", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sql", ".sh",
                 ".ps1", ".cmd", ".bat", ".xml", ".csv", ".lua", ".c", ".h", ".cpp",
                 ".cs", ".java", ".rb", ".go", ".rs", ".gitignore", ".env.example"}


def safe_path(rel: str) -> Path:
    """Resolve a browser-supplied path inside the root, or refuse. Checked on
    the resolved path, so symlinks fail like `..` does.
    """
    rel = (rel or "").replace("\\", "/").strip("/")
    full = (ROOT / rel).resolve() if rel else ROOT.resolve()
    root = ROOT.resolve()
    if full != root and root not in full.parents:
        raise ValueError("outside the projects root")
    parts = [p for p in rel.split("/") if p]
    if full != root:
        # The resolved parts too, so a symlink named notes.txt that points at a
        # .env is refused like the .env itself.
        parts += full.relative_to(root).parts
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
    """One directory's children, folders first. A folder's `changed` counts
    every changed file beneath it.
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
                "why": f"{size:,} bytes. Too big to open here. It is on disk at {full}."}
    try:
        raw = full.read_bytes()
    except OSError as exc:
        return {"path": rel, "size": size, "text": None, "why": str(exc)}
    if b"\x00" in raw[:4096]:
        return {"path": rel, "size": size, "text": None,
                "why": "binary. Nothing useful to show as text."}
    return {"path": rel, "size": size,
            "text": raw.decode("utf-8", errors="replace"), "why": None}


# Files listed per folder in the log; beyond this the diff view reads better.
FILES_LOGGED = 12


def record(conn, pulse_id: int | None, rows: list[dict]) -> int:
    """Write this pulse's findings into `project_changes`. Deltas, file lists
    and `moved_by` come from the pulse and are stored as they were at the
    time.
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
