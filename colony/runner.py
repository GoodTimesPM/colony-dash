r"""The one thing a build agent cannot do, done by the colony instead.

A build agent gets Read, Grep, Glob, Edit and Write. It has no shell, no
network and no package manager, and that is not an oversight — an unattended
run with a shell is one bad line away from `git push`, `rm -rf` or `curl | sh`
(ARCHITECTURE.md §8.3). The price of the rule is that a criterion phrased "run
`py -m apply.main auto` and confirm the OG tracker row appears" was
unanswerable. The agent could only skip it.

So the agent stops trying and hands the command over. `needs_run` in its reply
raises a `run-request` card carrying the command as written, why it is needed,
and what the agent expects to see. The PO reads the command and decides. If they
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
# security boundary — the PO can open a terminal and type any of them themselves.
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
    """The folder the command runs in. Always one of the PO's project folders."""
    name = (project or "").strip().replace("\\", "/").strip("/")
    if not name or ".." in name.split("/") or ":" in name:
        raise RunRefused(f"{project!r} is not a folder inside the projects directory")
    path = ROOT / name
    if not path.is_dir():
        raise RunRefused(f"there is no folder {name!r} to run it in")
    return path


# `cd job-search/assisted-apply; py -m apply.main auto` — written by an agent
# that had no way to know the colony was going to put it in that folder already.
# The cd then resolves against the project folder, finds no
# assisted-apply/job-search/assisted-apply, and the whole run dies with "The
# system cannot find the path specified" before the real command is reached.
_LEADING_CD = re.compile(
    r"""^\s*cd\s+(?P<path>"[^"]+"|'[^']+'|[^\s;&|]+)\s*(?:;|&&)\s*""")


def _drop_leading_cd(command: str, cwd: Path) -> str:
    """Strip a leading `cd` that only asks for the folder we are already in.

    A cd somewhere else is a different thing and is refused: the folder a
    command runs in is the write scope the PO approved, and a command that
    starts by leaving it has not been approved for wherever it lands.
    """
    while True:
        found = _LEADING_CD.match(command)
        if not found:
            return command
        raw = found.group("path").strip("\"'").replace("\\", "/")
        here = Path(str(cwd)).resolve(strict=False)
        # Relative to the folder we are in, and relative to the projects root,
        # because an agent writing `cd job-search/assisted-apply` means the
        # second one and has no idea it is already there.
        landings = {(cwd / raw).resolve(strict=False),
                    (ROOT / raw.lstrip("/")).resolve(strict=False)}
        if here not in landings:
            raise RunRefused(
                f"the command starts by changing directory to {raw!r}, which is "
                "not the folder this run is scoped to")
        command = command[found.end():].strip()
        if not command:
            raise RunRefused("that command is a `cd` and nothing else")


def execute(command: str, project: str) -> dict:
    """Run one command in one project folder and bring back everything it said.

    Never raises for a command that fails. A failing command is an answer —
    often the answer the criterion was asking for — so the exit code and both
    streams come back and the caller writes them down.
    """
    command = check(command)
    cwd = check_folder(project)
    command = _drop_leading_cd(command, cwd)

    try:
        # shell=True: the commands on these cards are written the way the PO
        # would type them (`py -m apply.main auto`), and splitting them by hand
        # would mean explaining to them why their own line did not work. The text
        # is one they read and approved, which is the whole control here.
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
    """The run as the story will remember it, and as the next agent will read it.

    `expect` is the build agent's own sentence about what a correct result looks
    like. It is written down next to the output rather than left on the card,
    because the card is answered and gone by the time anyone reads the run back,
    and a transcript that records only what happened leaves the next agent to
    guess what was supposed to happen.
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


# Counts of zero are how a passing test suite reports itself. Blanking them
# before the scan below is the difference between reading "0 failed" as a pass
# and reading it as the word "failed".
_ZERO_COUNT = re.compile(r"\b0 (failed|failures|errors?|warnings?|skipped)\b", re.I)

# Exit 0 is a weak claim. `py -m apply.main auto` printed "Notion query failed
# (ConnectionError)" and returned 0, and the colony wrote that down as a clean
# run against a criterion that had asked for proof the sync worked. These
# patterns do not decide whether the criterion was met — nothing here can —
# they decide whether the run is allowed to look like it settled anything.
SUSPECT = [
    (re.compile(r"traceback \(most recent call last\)", re.I),
     "it printed a traceback"),
    (re.compile(r"\bno tests? (ran|were run|collected|found)\b", re.I),
     "no test ran"),
    # `0 newly-applied row(s) in the Job Radar Tracker` — the real output of
    # `py -m apply.main auto`, and the reason the noun is allowed to sit a few
    # words away from the zero and to be written `row(s)`. A command run to
    # prove a sync touched a row, reporting that it touched none, is the exact
    # thing this verdict exists to catch.
    (re.compile(r"\b0 (?:[a-z][\w'-]* ){0,3}"
                r"(?:pass(?:ed|es)?|tests?|rows?|files?|records?|items?|entries"
                r"|entry|matches|results?|jobs?|applications?)(?:\(s\))?\b", re.I),
     "it counted zero of the thing it was supposed to touch"),
    (re.compile(r"\bfail(ed|ure|ures|s)?\b", re.I),
     "the output says something failed"),
    (re.compile(r"\b\w*(error|exception)s?\b", re.I),
     "the output names an error"),
    (re.compile(r"\bnot set\b|\bmissing\b", re.I),
     "the output says something it needed was not there"),
    (re.compile(r"\bcould not\b|\bunable to\b|\brefused\b|\bdenied\b", re.I),
     "the output says it could not do something"),
]


def judge(result: dict) -> tuple[str, str]:
    """`clean`, `suspect` or `failed`, and the sentence that says which.

    `clean` means nothing in the output contradicts the request. It does not
    mean the criterion is met: only a person, or the next build agent reading
    the transcript against the expectation, can say that.
    """
    if result.get("timed_out"):
        return "failed", f"it never finished — gave up after {TIMEOUT_S}s"
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
    """Would `shlex` read this the same way a shell does?

    Only used to decide whether the card can show a tidy argument list next to
    the raw text. Nothing depends on the answer.
    """
    try:
        shlex.split(command)
        return True
    except ValueError:
        return False
