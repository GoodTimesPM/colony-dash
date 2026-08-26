r"""The PO's terminal, in the Ordis panel.

Colony Dash's whole design is a loop that cannot hurt anything. Agents get a
tool allowlist, `Bash` is denied at the top of `agent.py` under a comment
explaining that denying the shell is what makes "the colony cannot push" a
capability rather than a promise, writes land in a throwaway worktree, and
nothing reaches a real file without the PO approving a patch. That is right for
an autonomous loop. Nobody should own a program that can `git push` unattended
at 3am because a groom run misread a story.

It is exactly wrong for the case this module exists for: Jordan sitting in front
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

The guards that remain are the ones that were never about the agent: the server
binds to 127.0.0.1, and `/api/console/*` needs the `X-Colony` header like every
other write route. Nothing off this machine can knock on this door at all.
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
MODEL = "claude-sonnet-5"

# Long, because this is a working shell and a real request ("run the test suite
# and fix what fails") is minutes of work, not seconds. It is still a ceiling:
# a wedged child process that never exits would otherwise hold the console shut
# forever, since only one turn may be in flight.
TIMEOUT_S = 1800

# What a turn is allowed to be. A paste of a whole file is a legitimate message;
# a runaway loop POSTing into this endpoint is not.
MAX_CHARS = 60000

SYSTEM = """You are Ordis, talking directly to Jordan in the Colony Dash console.

This is not a colony ticket. There is no work order, no acceptance criteria and
no PO card to fill in. It is a terminal with full tool access, running at
D:/ALL STUFF/PROJECTS, and you are being asked to do things to this machine the
same way you would in Jordan's own terminal.

Two rules that come from the colony and still apply here, because they are about
his data rather than about your permissions:

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
    }


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
        args=(turn_id, text, session_id, resume, cwd),
        name=f"console-turn-{turn_id}",
        daemon=True,
    ).start()
    return {"turn_id": turn_id, "epoch": epoch}


def clear(conn: sqlite3.Connection) -> dict:
    """Start a new conversation. The old turns stay, addressable by epoch.

    Refused while a turn is in flight: the thread is still holding the row it is
    going to write, and a "cleared" chat that grows an answer thirty seconds
    later is worse than a button that says no.
    """
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

def _answer(turn_id: int, prompt: str, session_id: str, resume: bool, cwd: str) -> None:
    """Run one turn to completion and write the result. Never raises.

    Its own connection: this is a different thread, and a SQLite handle belongs
    to the thread that opened it.
    """
    global _running
    started = time.monotonic()
    conn = db.connect()
    try:
        result = _invoke(prompt, session_id, resume, cwd)
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


def _invoke(prompt: str, session_id: str, resume: bool, cwd: str) -> dict:
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--model", MODEL,
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
