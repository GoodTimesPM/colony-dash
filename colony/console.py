r"""The PO's terminal, in the Ordis panel.

Colony Dash's whole design is a loop that cannot hurt anything. Agents get a
tool allowlist, `Bash` is denied at the top of `agent.py` under a comment
explaining that denying the shell is what makes "the colony cannot push" a
capability rather than a promise, writes land in a throwaway worktree, and
nothing reaches a real file without the PO approving a patch. That is right for
an autonomous loop. Nobody should own a program that can `git push` unattended
at 3am because a groom run misread a story.

It is exactly wrong for the case this module exists for: the PO sitting in front
of the dashboard wanting to change the dashboard. Every such change went out to
a separate terminal, and the program that is supposed to run itself could not
edit itself.

So: a second door, deliberately unlike the first one.

  * **It only opens when a person types.** There is no path from `pulse.py` or
    `wake.py` into this module. Nothing scheduled can reach it.
  * **It is unrestricted on purpose.** `--dangerously-skip-permissions`, no
    allowlist, no worktree, `cwd` at the projects root. It is the terminal.
  * **It is one conversation at a time.** A second send while a turn is in
    flight is refused rather than queued, because two shells writing the same
    tree is the failure this system exists to avoid.
  * **It bills itself out loud.** Every turn records its tokens and its cost in
    `console_turns`, and clearing the chat starts a new epoch rather than
    deleting the rows.

The guards that remain are on the server. Every request must name this machine
in its Host header (DNS rebinding), writes need `X-Colony` and a same-host
Origin, and `/api/console/*` answers only peers on this machine unless the PO
turns on `COLONY_CONSOLE_REMOTE` from the desk. When phone access is on the
server also listens on a LAN or tailnet address behind the access token.
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

CLAUDE_BIN = "claude"

# What the dropdowns offer. The CLI takes an alias or a full name; the aliases
# are used so this list does not go stale the week a point release ships.
#
# The default is sonnet on medium because most of what gets typed here is
# "why is this panel empty" rather than "redesign the scheduler". Opus on max
# is roughly an order of magnitude more expensive for the same question, which
# is a fine trade when the question is hard and a waste when it is not -- so it
# is a control on the bar rather than a constant in this file.
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

# Slash commands that were checked to actually work through `claude -p`. Most
# of the interactive ones do not (`/status` answers "isn't available in this
# environment"), so this is a verified list rather than a copy of the help
# screen. Skills are discovered from disk below and appended to it.
BUILTIN_COMMANDS = [
    ("/compact", "summarise the conversation so far and keep going in less context"),
    ("/context", "what is in the context window right now, by category"),
    ("/cost", "what this subscription window has been spent on"),
]

# Long, because this is a working shell and a real request ("run the test suite
# and fix what fails") is minutes of work, not seconds. It is still a ceiling:
# a wedged child process that never exits would otherwise hold the console shut
# forever, since only one turn may be in flight.
TIMEOUT_S = 1800

# What a turn is allowed to be. A paste of a whole file is a legitimate message;
# a runaway loop POSTing into this endpoint is not.
MAX_CHARS = 60000

SYSTEM = f"""You are Ordis, talking directly to the PO in the Colony Dash console.

This is not a colony ticket. There is no work order, no acceptance criteria and
no PO card to fill in. It is a terminal with full tool access, running at
{db.PROJECTS_ROOT.as_posix()}, and you are being asked to do things to this
machine the same way you would in the PO's own terminal.

Two rules that come from the colony and still apply here, because they are about
their data rather than about your permissions:

  * Never print, echo or commit the contents of a `.env` or any other credential
    file. Read one if a task genuinely needs it; do not put it in your reply.
  * Never `git push`, `git commit --amend`, force-push, or delete a branch. Ask
    first. Everything else -- edit, write, run, install, commit -- go ahead.

Say what you changed and where, by path. If you did not do the thing, say that
first instead of describing what you tried.
"""

# One turn at a time, enforced in the process as well as in the ledger. The DB
# check catches a stale `pending` row left by a crash; this catches two requests
# landing in the same millisecond.
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
    """Where the *installed* plugins live, per the CLI's own manifest.

    Not `~/.claude/plugins/marketplaces`. That directory is clones of every
    marketplace the PO has ever looked at, and globbing it offered a menu of
    thirty skills of which one was installed. A dropdown that lists commands
    that do not exist is worse than no dropdown.
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
    """Every slash command this console can actually send, read off disk.

    Hard-coding a menu of skills would mean the dropdown lies the first time
    the PO installs one. So: the verified built-ins, then whatever is on disk
    under the user's skills and commands folders, the project's `.claude`, and
    the installed plugin marketplaces. Names only -- the dropdown pastes text
    into the box, it does not run anything.

    Cached for two minutes. The drawer polls this every 1.5 seconds and walking
    the plugin marketplaces that often would be a directory scan per frame for
    a list that changes when the PO installs something, which is never during a
    conversation.
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
        # Bare `.md` files are commands only inside a `commands/` folder. A
        # plugin's install root has a README.md in it, and `/README` is not a
        # command.
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
    """Record the PO's message and start Ordis answering it in a thread.

    Returns immediately. The answer lands in the `pending` row this creates,
    which the page polls -- a shell command can take twenty minutes and an HTTP
    request that waits for one is a request that times out.
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

        # A fresh session for the first turn of an epoch, a resume for every turn
        # after it. Claimed here rather than in the thread so two sends can never
        # mint two UUIDs for the same conversation.
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
    """Change the model or the effort level for the turns after this one.

    Deliberately not refused mid-flight. The turn already running was launched
    with the old pair and keeps it; changing the dropdown while you wait means
    "the next one, please", which is what a person sitting there would mean.
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
    """Start a new conversation. The old turns stay, addressable by epoch.

    Refused while a turn is in flight: the thread is still holding the row it is
    going to write, and a "cleared" chat that grows an answer thirty seconds
    later is worse than a button that says no.
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
    """Run one turn to completion and write the result. Never raises.

    Its own connection: this is a different thread, and a SQLite handle belongs
    to the thread that opened it.
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
