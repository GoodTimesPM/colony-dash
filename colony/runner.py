r"""Runs a command a build agent asked for, once the PO approves it.

Build agents have no shell (docs/design.md §8.3), so a criterion like "run
this and confirm the row appears" is out of their reach. They put the
command in `needs_run`, which raises a `run-request` card with the command,
why, and the expected result. If the PO approves, it runs here and the
output goes on the story for the next build to read.

This is not a shell for agents: only a PO decision calls `execute`. The
refusals below are a second line against commands that look routine but are
never allowed.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from . import db, proc as proc_mod

ROOT = db.PROJECTS_ROOT

# Enough for a test suite; short enough that a command stuck on a prompt gives
# up instead of holding the worker thread.
TIMEOUT_S = 90

# Output kept per stream. Well past anything worth reading, and the cap exists
# only so a runaway loop cannot write a gigabyte into the ledger.
MAX_OUTPUT = 40000

# Never allowed at any tier. Not a security boundary; the PO has a terminal.
# This stops a routine-looking card from hiding one of these.
FORBIDDEN = [
    (re.compile(r"\bgit\s+push\b", re.I), "the colony never pushes"),
    (re.compile(r"\bgit\s+commit\b", re.I), "the colony never commits. You commit"),
    (re.compile(r"\bgit\s+(reset|checkout|restore|clean)\b.*(--hard|-f\b|-fd)", re.I),
     "that throws away working-tree changes"),
    # Scoped to git. A lone `-f` means "follow" to tail, "file" to grep and
    # docker compose, and refusing those made the rule noise.
    (re.compile(r"\bgit\s+branch\s+-D\b|\bgit\b.*(--force\b|--force-with-lease|\s-f(?=\s|$))", re.I),
     "a forced git operation rewrites history"),
    (re.compile(r"\brm\s+(-\w*r\w*f|-\w*f\w*r|-[rR]\s+-f|-f\s+-[rR]|--recursive\s+--force|--force\s+--recursive)\b"
                r"|\bRemove-Item\b.*-Recurse.*-Force|\bRemove-Item\b.*-Force.*-Recurse"
                r"|\brmdir\s+/s\b|\brd\s+/s\b", re.I),
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
        raise RunRefused("one command per request. A script belongs in a file")
    for pattern, why in FORBIDDEN:
        if pattern.search(command):
            raise RunRefused(f"refused: {why}")
    return command


def check_folder(project: str) -> Path:
    """The folder the command runs in. Always one of the PO's project folders."""
    name = (project or "").strip().replace("\\", "/").strip("/")
    if not name or ".." in name.split("/") or ":" in name:
        raise RunRefused(f"{project!r} is not a folder inside the projects directory")
    path = ROOT / name
    if not path.is_dir():
        raise RunRefused(f"there is no folder {name!r} to run it in")
    return path


# Agents write `cd sub/dir; cmd` without knowing the start folder.
_LEADING_CD = re.compile(
    r"""^\s*cd\s+(?P<path>"[^"]+"|'[^']+'|[^\s;&|]+)\s*(?:;|&&)\s*""")


def resolve_cd(command: str, cwd: Path) -> tuple[str, Path]:
    """Strip a leading `cd` and return the folder the rest should run in.

    A `cd` deeper inside the write scope is honoured; packages often live in
    a subfolder. A `cd` that leaves the project is refused.
    """
    here = Path(str(cwd)).resolve(strict=False)
    landing = here
    while True:
        found = _LEADING_CD.match(command)
        if not found:
            return command, landing
        raw = found.group("path").strip("\"'").replace("\\", "/")
        # Try the path relative to here and to the projects root; the one that
        # exists on disk wins.
        inside = [c for c in ((landing / raw).resolve(strict=False),
                              (ROOT / raw.lstrip("/")).resolve(strict=False))
                  if c == here or here in c.parents]
        if not inside:
            raise RunRefused(
                f"the command starts by changing directory to {raw!r}, which is "
                "outside the folder this run is scoped to")
        real = [c for c in inside if c.is_dir()]
        if not real:
            raise RunRefused(f"there is no folder {raw!r} to run this in")
        landing = real[0]
        command = command[found.end():].strip()
        if not command:
            raise RunRefused("that command is a `cd` and nothing else")


def execute(command: str, project: str) -> dict:
    """Run one command in one project folder and return exit code and both
    streams. A failing command is an answer, so this never raises for one.
    """
    command = check(command)
    command, cwd = resolve_cd(command, check_folder(project))

    try:
        # shell=True: the PO approved this exact text as they would type it.
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


def transcript(result: dict, expect: str = "") -> str:
    """The run as the story records it, with the agent's `expect` beside the
    output so the next reader knows what should have happened.
    """
    head = f"$ {result['command']}\n  in {result['cwd']}"
    if result.get("timed_out"):
        return f"{head}\n\nGave up after {TIMEOUT_S}s. {result['err']}"
    head += f"\n  exit {result['code']}"
    parts = [head]
    verdict, why = judge(result)
    if verdict != "clean":
        parts.append("READ THIS BEFORE TRUSTING IT:\n  " + why)
    if (expect or "").strip():
        parts.append("--- it expected ---\n" + expect.strip())
    if result.get("out", "").strip():
        parts.append("--- output ---\n" + result["out"].rstrip())
    if result.get("err", "").strip():
        parts.append("--- errors ---\n" + result["err"].rstrip())
    if not result.get("out", "").strip() and not result.get("err", "").strip():
        parts.append("(it printed nothing)")
    return "\n\n".join(parts)


# Blank out zero counts so "0 failed" does not read as a failure.
_ZERO_COUNT = re.compile(r"\b(0|no) (failed|failures|errors?|warnings?|skipped)\b", re.I)

# Exit 0 is weak evidence; a command can print an error and still return 0.
# These decide only whether the run may look like it settled anything.
SUSPECT = [
    (re.compile(r"traceback \(most recent call last\)", re.I),
     "it printed a traceback"),
    (re.compile(r"\bno tests? (ran|were run|collected|found)\b", re.I),
     "no test ran"),
    # "0 newly-applied row(s) in ...": a sync that touched nothing.
    (re.compile(r"\b0 (?:[a-z][\w'-]* ){0,3}"
                r"(?:pass(?:ed|es)?|tests?|rows?|files?|records?|items?|entries"
                r"|entry|matches|results?|jobs?|applications?)(?:\(s\))?\b", re.I),
     "it counted zero of the thing it was supposed to touch"),
    (re.compile(r"\bfail(ed|ure|ures|s)?\b", re.I),
     "the output says something failed"),
    # An error as a program reports one, `ValueError: ...` or `error: ...`,
    # rather than any line that mentions the word.
    (re.compile(r"(?m)(?:^|\s)(?:\w*(?:Error|Exception)|error|ERROR|FATAL|fatal)\s*:"),
     "the output reports an error"),
    (re.compile(r"\bnot set\b|\b(?:is|are|was|were) missing\b|\bmissing\s*:", re.I),
     "the output says something it needed was not there"),
    (re.compile(r"\bcould not\b|\bunable to\b|\brefused\b|\bdenied\b", re.I),
     "the output says it could not do something"),
]


def judge(result: dict) -> tuple[str, str]:
    """`clean`, `suspect` or `failed`, with a sentence saying why. `clean`
    means nothing contradicts the request, not that the criterion is met.
    """
    if result.get("timed_out"):
        return "failed", f"it never finished. Gave up after {TIMEOUT_S}s"
    if result.get("code") is None:
        return "failed", "the shell would not start it"
    if result["code"] != 0:
        return "failed", f"it exited {result['code']}"
    text = _ZERO_COUNT.sub(" ", (result.get("out") or "")
                           + "\n" + (result.get("err") or ""))
    if not text.strip():
        return "suspect", "it exited 0 and printed nothing at all"
    for pattern, why in SUSPECT:
        if pattern.search(text):
            return "suspect", f"it exited 0, but {why}"
    return "clean", ""


def looks_shell_free(command: str) -> bool:
    """Would `shlex` split this as a shell would? Display only."""
    try:
        shlex.split(command)
        return True
    except ValueError:
        return False
