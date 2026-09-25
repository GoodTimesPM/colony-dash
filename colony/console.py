r"""The PO's terminal, in the Ordis panel.

The colony's agents are locked down. This is the opposite, for the PO
editing the dashboard from inside it:

  * It opens only when a person types. Nothing in `pulse.py` or `wake.py`
    reaches it.
  * It is unrestricted: `--dangerously-skip-permissions`, no worktree, `cwd`
    at the projects root.
  * One turn at a time; a second send is refused, not queued.
  * Every turn's tokens and cost go in `console_turns`. Clearing starts a new
    epoch without deleting rows.

The server guards it: Host must name this machine, writes need `X-Colony`
and a same-host Origin, and `/api/console/*` serves only this machine unless
`COLONY_CONSOLE_REMOTE` is on.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import db, proc as proc_mod, voice
from .prompt import render as render_prompt

CLAUDE_BIN = "claude"

# Aliases so the list survives point releases. Sonnet on medium by default:
# most questions here are small, and opus on max costs about ten times more.
MODELS = [
    ("sonnet", "sonnet 5 - the default, fast and good enough for most of it"),
    ("opus", "opus 5 - slower and dearer, for the changes that are actually hard"),
    ("haiku", "haiku 4.5 - cheap, for a quick read or a one-line fix"),
    ("fable", "fable 5"),
]
EFFORTS = [
    ("low", "low - answer fast, do not deliberate"),
    ("medium", "medium - the default"),
    ("high", "high - think before acting"),
    ("xhigh", "xhigh"),
    ("max", "max - for a change that has to be right the first time"),
]
DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "medium"

# Slash commands verified to work through `claude -p`; skills on disk are
# appended below.
BUILTIN_COMMANDS = [
    ("/compact", "summarise the conversation so far and keep going in less context"),
    ("/context", "what is in the context window right now, by category"),
    ("/cost", "what this subscription window has been spent on"),
]

# Thirty minutes: a real request can take that long, and a hung child would
# otherwise hold the single turn slot forever.
TIMEOUT_S = 1800

# What a turn is allowed to be. A paste of a whole file is a legitimate message;
# a runaway loop POSTing into this endpoint is not.
MAX_CHARS = 60000

SYSTEM = render_prompt("console", root=db.PROJECTS_ROOT.as_posix())

# In-process lock for simultaneous requests; the DB check catches stale rows
# left by a crash.
_lock = threading.Lock()
_running = False


# -- what you can type ---------------------------------------------------------

_SKILL_DIRS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "commands",
]


def _skill_summary(path: Path) -> str:
    """The `description:` line out of a SKILL.md front-matter block."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:1200]
    except OSError:
        return ""
    for line in head.splitlines():
        if line.lower().startswith("description:"):
            return line.split(":", 1)[1].strip().strip("'\"")[:140]
    return ""


def _installed_plugin_dirs() -> list[Path]:
    """Installed plugin roots from the CLI's manifest. Not the marketplaces
    folder, which holds every marketplace ever browsed.
    """
    manifest = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Path] = []
    for installs in (data.get("plugins") or {}).values():
        for install in installs or []:
            where = install.get("installPath")
            if where:
                out.append(Path(where))
    return out


_cmd_cache: tuple[float, list[dict]] | None = None
_CMD_TTL_S = 120


def commands() -> list[dict]:
    """Every slash command this console can send: verified built-ins, then
    skills and commands on disk. Names only. Cached two minutes because the
    drawer polls every 1.5s.
    """
    global _cmd_cache
    if _cmd_cache and time.monotonic() - _cmd_cache[0] < _CMD_TTL_S:
        return _cmd_cache[1]

    found: dict[str, str] = {name: why for name, why in BUILTIN_COMMANDS}

    roots = list(_SKILL_DIRS) + [
        db.PROJECTS_ROOT / ".claude" / "skills",
        db.PROJECTS_ROOT / ".claude" / "commands",
    ] + _installed_plugin_dirs()
    for root in roots:
        if not root.is_dir():
            continue
        # SKILL.md one or more levels down for skills and plugins; bare .md
        # files for the older commands folder.
        for hit in list(root.glob("**/SKILL.md"))[:200]:
            name = "/" + hit.parent.name
            found.setdefault(name, _skill_summary(hit))
        # Bare `.md` files count only inside a `commands/` folder (not
        # README.md).
        if root.name == "commands":
            for hit in list(root.glob("*.md"))[:200]:
                found.setdefault("/" + hit.stem, "")

    out = [{"name": n, "why": found[n]} for n in sorted(found)]
    _cmd_cache = (time.monotonic(), out)
    return out


# -- reading -------------------------------------------------------------------

def state(conn: sqlite3.Connection) -> dict:
    """The current conversation: its turns, its epoch, what it has cost."""
    row = conn.execute("SELECT * FROM console_state WHERE id = 1").fetchone()
    epoch = int(row["epoch"]) if row else 1
    turns = [dict(r) for r in conn.execute(
        "SELECT id, at, role, body, status, tokens, cost_usd, elapsed_s, error "
        "  FROM console_turns WHERE epoch = ? ORDER BY id", (epoch,))]
    spent = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) AS t, COALESCE(SUM(cost_usd),0) AS c "
        "  FROM console_turns WHERE epoch = ?", (epoch,)).fetchone()
    lifetime = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) AS t FROM console_turns").fetchone()
    return {
        "epoch": epoch,
        "cwd": (row["cwd"] if row else None) or str(db.PROJECTS_ROOT),
        "turns": turns,
        "busy": any(t["status"] == "pending" for t in turns),
        "tokens": int(spent["t"]), "cost_usd": float(spent["c"]),
        "lifetime_tokens": int(lifetime["t"]),
        "resuming": bool(row and row["session_id"]),
        "model": _opt(row, "model") or DEFAULT_MODEL,
        "effort": _opt(row, "effort") or DEFAULT_EFFORT,
        "models": [{"id": i, "why": w} for i, w in MODELS],
        "efforts": [{"id": i, "why": w} for i, w in EFFORTS],
        "commands": commands(),
    }


def _opt(row, key: str):
    """Read a column that may predate the row. 025 adds two of them."""
    try:
        return row[key] if row else None
    except (IndexError, KeyError):
        return None


# -- writing -------------------------------------------------------------------

class Busy(RuntimeError):
    """A turn is already in flight. Not an error worth a stack trace."""


def send(conn: sqlite3.Connection, text: str) -> dict:
    """Record the PO's message and answer it on a thread. Returns at once; the
    page polls the `pending` row.
    """
    global _running

    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to send")
    if len(text) > MAX_CHARS:
        raise ValueError(f"message is {len(text)} characters, the limit is {MAX_CHARS}")

    row = conn.execute("SELECT * FROM console_state WHERE id = 1").fetchone()
    epoch = int(row["epoch"])
    session_id = row["session_id"]
    cwd = row["cwd"] or str(db.PROJECTS_ROOT)
    model = _opt(row, "model") or DEFAULT_MODEL
    effort = _opt(row, "effort") or DEFAULT_EFFORT

    with _lock:
        if _running:
            raise Busy("Ordis is still working on the last message")
        stale = conn.execute(
            "SELECT id FROM console_turns WHERE epoch = ? AND status = 'pending'",
            (epoch,)).fetchone()
        if stale:
            raise Busy("a turn is still open in this chat - clear it or wait")
        _running = True

    try:
        conn.execute(
            "INSERT INTO console_turns (epoch, role, body, status) "
            "VALUES (?, 'po', ?, 'done')", (epoch, text))
        cur = conn.execute(
            "INSERT INTO console_turns (epoch, role, body, status) "
            "VALUES (?, 'ordis', '', 'pending')", (epoch,))
        turn_id = int(cur.lastrowid)

        # New session on an epoch's first turn, resume after. Decided here so
        # two sends cannot mint two session ids.
        resume = bool(session_id)
        if not resume:
            session_id = str(uuid.uuid4())
            conn.execute("UPDATE console_state SET session_id = ? WHERE id = 1",
                         (session_id,))
    except BaseException:
        with _lock:
            _running = False
        raise

    threading.Thread(
        target=_answer,
        args=(turn_id, text, session_id, resume, cwd, model, effort),
        name=f"console-turn-{turn_id}",
        daemon=True,
    ).start()
    return {"turn_id": turn_id, "epoch": epoch}


def set_options(conn: sqlite3.Connection, model: str | None,
                effort: str | None) -> dict:
    """Change model or effort for later turns. The running turn keeps its
    settings.
    """
    if model is not None:
        if model not in {i for i, _ in MODELS}:
            raise ValueError(f"{model!r} is not one of the models on offer")
        conn.execute("UPDATE console_state SET model = ? WHERE id = 1", (model,))
    if effort is not None:
        if effort not in {i for i, _ in EFFORTS}:
            raise ValueError(f"{effort!r} is not an effort level")
        conn.execute("UPDATE console_state SET effort = ? WHERE id = 1", (effort,))
    return state(conn)


def clear(conn: sqlite3.Connection) -> dict:
    """Start a new conversation; old turns stay by epoch. Refused mid-turn,
    since the running turn would still write into the cleared chat.
    """
    # Held through the update, so a turn cannot start between the check and
    # the new epoch.
    with _lock:
        row = conn.execute("SELECT * FROM console_state WHERE id = 1").fetchone()
        epoch = int(row["epoch"])
        if _running or conn.execute(
                "SELECT 1 FROM console_turns WHERE epoch = ? AND status = 'pending'",
                (epoch,)).fetchone():
            raise Busy("cannot clear while Ordis is still answering")
        conn.execute(
            "UPDATE console_state SET epoch = epoch + 1, session_id = NULL WHERE id = 1")
    return {"epoch": epoch + 1}


def set_cwd(conn: sqlite3.Connection, path: str | None) -> dict:
    """Where the shell runs. Defaults to the projects root."""
    if not path:
        conn.execute("UPDATE console_state SET cwd = NULL WHERE id = 1")
        return {"cwd": str(db.PROJECTS_ROOT)}
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = db.PROJECTS_ROOT / path
    target = target.resolve()
    if not target.is_dir():
        raise ValueError(f"{target} is not a directory")
    conn.execute("UPDATE console_state SET cwd = ? WHERE id = 1", (str(target),))
    return {"cwd": str(target)}


# -- the shell -----------------------------------------------------------------

def _answer(turn_id: int, prompt: str, session_id: str, resume: bool, cwd: str,
            model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT) -> None:
    """Run one turn and write the result. Never raises. Uses its own
    connection, since SQLite handles belong to their thread.
    """
    global _running
    started = time.monotonic()
    conn = db.connect()
    try:
        result = _invoke(prompt, session_id, resume, cwd, model, effort)
        conn.execute(
            "UPDATE console_turns SET body = ?, status = ?, session_id = ?, "
            "       tokens = ?, cost_usd = ?, elapsed_s = ?, error = ?, "
            "       at = datetime('now','localtime') "
            " WHERE id = ?",
            (result["text"], result["status"], result.get("session_id") or session_id,
             result["tokens"], result["cost_usd"], round(time.monotonic() - started, 1),
             result.get("error"), turn_id))
    except BaseException as exc:                       # never leave a pending row
        try:
            conn.execute(
                "UPDATE console_turns SET status = 'failed', error = ?, elapsed_s = ? "
                " WHERE id = ?",
                (f"{type(exc).__name__}: {exc}"[:500],
                 round(time.monotonic() - started, 1), turn_id))
        except Exception:
            pass
    finally:
        conn.close()
        with _lock:
            _running = False


def _invoke(prompt: str, session_id: str, resume: bool, cwd: str,
            model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT) -> dict:
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--model", model,
        "--effort", effort,
        "--dangerously-skip-permissions",
        "--append-system-prompt", SYSTEM + "\n" + voice.STYLE,
    ]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]

    try:
        out = proc_mod.run(
            cmd, input=prompt, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return _fail(f"no answer within {TIMEOUT_S // 60} minutes - the shell was killed")
    except FileNotFoundError:
        return _fail(f"{CLAUDE_BIN} is not on PATH for the dashboard's process")

    if out.returncode != 0 and not (out.stdout or "").strip():
        return _fail((out.stderr or "the CLI exited with no output").strip()[:1000])
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return _fail(f"unreadable CLI output: {(out.stdout or '')[:400]}")

    usage = payload.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens"))
    text = payload.get("result") or ""
    failed = bool(payload.get("is_error"))
    return {
        "status": "failed" if failed else "done",
        "text": "" if failed else text,
        "error": ((str(payload.get("error") or text) or "the run reported an error")[:1000]
                  if failed else None),
        "session_id": payload.get("session_id"),
        "tokens": tokens,
        "cost_usd": float(payload.get("total_cost_usd") or 0.0),
    }


def _fail(why: str) -> dict:
    return {"status": "failed", "text": "", "error": why,
            "session_id": None, "tokens": 0, "cost_usd": 0.0}
