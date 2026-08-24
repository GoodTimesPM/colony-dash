r"""The one thing a build agent cannot do, done by the colony instead.

A build agent gets Read, Grep, Glob, Edit and Write. It has no shell, no
network and no package manager, and that is not an oversight — an unattended
run with a shell is one bad line away from `git push`, `rm -rf` or `curl | sh`
(ARCHITECTURE.md §8.3). The price of the rule is that a criterion phrased "run
`py -m apply.main auto` and confirm the OG tracker row appears" was
unanswerable. The agent could only skip it.

So the agent stops trying and hands the command over. `needs_run` in its reply
raises a `run-request` card carrying the command as written, why it is needed,
and what the agent expects to see. Jordan reads the command and decides. If he
runs it, the colony runs it here and puts the output back on the story, where
the next build reads it.

What this module is not: a shell for agents. Nothing calls `execute` except a
PO decision on a card, one command at a time, and the command is the text the
PO read. The refusals below are a second line behind that, for the cases where
a plausible-looking command does something the colony is not allowed to do at
any tier.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from . import db, proc as proc_mod

ROOT = db.PROJECTS_ROOT

# Ninety seconds. Long enough for a test suite or an API round trip, short
# enough that a command waiting on a prompt nobody can answer gives up rather
# than holding the dashboard's worker thread until the process is killed.
TIMEOUT_S = 90

# Output kept per stream. Well past anything worth reading, and the cap exists
# only so a runaway loop cannot write a gigabyte into the ledger.
MAX_OUTPUT = 40000

# The things the colony may not do at any tier, whoever asks. These are not a
# security boundary — the PO can open a terminal and type any of them himself.
# They are here so that a command which *looks* routine on a card cannot turn
# out to have been one of these.
FORBIDDEN = [
    (re.compile(r"\bgit\s+push\b", re.I), "the colony never pushes"),
    (re.compile(r"\bgit\s+commit\b", re.I), "the colony never commits — you commit"),
    (re.compile(r"\bgit\s+(reset|checkout|restore|clean)\b.*(--hard|-f\b|-fd)", re.I),
     "that throws away working-tree changes"),
    (re.compile(r"\bgit\s+branch\s+-D\b|--force-with-lease|--force\b|(?<!\w)-f(?=\s|$)", re.I),
     "a forced git operation rewrites history"),
    (re.compile(r"\brm\s+-rf\b|\bRemove-Item\b.*-Recurse.*-Force", re.I),
     "a recursive force delete"),
    (re.compile(r"\|\s*(sh|bash|iex|Invoke-Expression)\b", re.I),
     "piping a download into a shell"),
    (re.compile(r"\b(shutdown|format|diskpart|reg\s+delete)\b", re.I),
     "that is not a project command"),
]


class RunRefused(RuntimeError):
    """The command will not be run, and the card says why."""


def check(command: str) -> str:
    """The command, cleaned up, or a refusal naming the rule it breaks."""
    command = (command or "").strip()
    if not command:
        raise RunRefused("there is no command on this request")
    if len(command) > 600:
        raise RunRefused("that is longer than a command and shorter than a script")
    if "\n" in command:
        raise RunRefused("one command per request — a script belongs in a file")
    for pattern, why in FORBIDDEN:
        if pattern.search(command):
            raise RunRefused(f"refused: {why}")
    return command


def check_folder(project: str) -> Path:
    """The folder the command runs in. Always one of Jordan's project folders."""
    name = (project or "").strip().replace("\\", "/").strip("/")
    if not name or ".." in name.split("/") or ":" in name:
        raise RunRefused(f"{project!r} is not a folder inside the projects directory")
    path = ROOT / name
    if not path.is_dir():
        raise RunRefused(f"there is no folder {name!r} to run it in")
    return path


def execute(command: str, project: str) -> dict:
    """Run one command in one project folder and bring back everything it said.

    Never raises for a command that fails. A failing command is an answer —
    often the answer the criterion was asking for — so the exit code and both
    streams come back and the caller writes them down.
    """
    command = check(command)
    cwd = check_folder(project)

    try:
        # shell=True: the commands on these cards are written the way Jordan
        # would type them (`py -m apply.main auto`), and splitting them by hand
        # would mean explaining to him why his own line did not work. The text
        # is one he read and approved, which is the whole control here.
        completed = proc_mod.run(
            command, cwd=str(cwd), shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
        )
    except Exception as exc:                       # timeout, or the shell refused
        return {"ok": False, "code": None, "command": command,
                "cwd": str(cwd), "out": "", "err": f"{type(exc).__name__}: {exc}",
                "timed_out": "Timeout" in type(exc).__name__}

    return {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "command": command,
        "cwd": str(cwd),
        "out": (completed.stdout or "")[:MAX_OUTPUT],
        "err": (completed.stderr or "")[:MAX_OUTPUT],
        "timed_out": False,
    }


def transcript(result: dict) -> str:
    """The run as the story will remember it, and as the next agent will read it."""
    head = f"$ {result['command']}\n  in {result['cwd']}"
    if result.get("timed_out"):
        return f"{head}\n\nGave up after {TIMEOUT_S}s. {result['err']}"
    head += f"\n  exit {result['code']}"
    parts = [head]
    if result.get("out", "").strip():
        parts.append("--- output ---\n" + result["out"].rstrip())
    if result.get("err", "").strip():
        parts.append("--- errors ---\n" + result["err"].rstrip())
    if len(parts) == 1:
        parts.append("(it printed nothing)")
    return "\n\n".join(parts)


def looks_shell_free(command: str) -> bool:
    """Would `shlex` read this the same way a shell does?

    Only used to decide whether the card can show a tidy argument list next to
    the raw text. Nothing depends on the answer.
    """
    try:
        shlex.split(command)
        return True
    except ValueError:
        return False
