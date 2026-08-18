"""Spawning a colonist — one `claude -p` invocation, recorded in the ledger.

This is the only place in Colony Dash that spends tokens. Everything else is
pure Python. So this is also the only place that needs the guards: a tool
allowlist, a working directory, a wall-clock timeout, and a `runs` row written
*before* the process starts, so a crash mid-run still leaves evidence.

The contract, from ARCHITECTURE.md §2.1 and §8: the persona says how to think,
Colony Dash says what may be touched. A persona file never supplies `tools:` or
`model:` — those come from the agent's contract in the `agents` table and are
passed here explicitly. Nothing is inherited, and nothing is implicit.
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db, proc as proc_mod

CLAUDE_BIN = "claude"

# Anything a run may never do, whatever its contract says. Belt-and-braces: the
# allowlist already excludes these. This list exists so a mistake in one row of
# the `agents` table cannot become a capability.
#
# Bash stays here even for write-capable runs, and that is the load-bearing
# entry. Edit and Write are bounded — they touch files inside a throwaway
# worktree. Bash is unbounded: it is `git push`, `rm -rf`, `curl | sh`, and the
# whole class of things §8.3 says the colony must never be able to do. Denying
# the shell is what makes "the colony cannot push" a capability statement rather
# than a promise the agents are asked to keep.
ALWAYS_DENIED = ["Bash", "WebFetch", "WebSearch", "Task", "KillShell", "BashOutput"]

# Additionally denied unless the contract is write-capable *and* the caller has
# opened a worktree for the run to write in.
WRITE_TOOLS = ["Edit", "Write", "NotebookEdit"]


@dataclass
class RunResult:
    status: str                     # ok | failed | timeout
    text: str = ""                  # the agent's final message
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0          # every token that moved, cache reads included
    chargeable_tokens: int = 0     # what the budget is actually spent against
    cost_usd: float = 0.0
    error: str | None = None
    over_budget: bool = False
    raw: dict = field(default_factory=dict)

    def json_payload(self) -> dict | None:
        """The agent's answer, when we asked it to reply with JSON.

        Models wrap JSON in prose or a fenced block often enough that parsing
        has to tolerate it. A failure here is not an error — it means the run
        said something we can't act on, which the caller reports as a finding
        rather than a crash.
        """
        text = self.text.strip()
        if "```" in text:
            blocks = text.split("```")
            for block in blocks[1:]:
                body = block.split("\n", 1)[-1] if block[:20].strip().lower() in ("json", "") else block
                try:
                    return json.loads(body.rsplit("```", 1)[0] if "```" in body else body)
                except json.JSONDecodeError:
                    continue
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _usage_from(payload: dict) -> dict:
    """Pull the four real counters out of `--output-format json`.

    Defensive on purpose: this is an internal output shape, not a contract we
    control, and a field rename must not take the whole pulse down with it.
    """
    usage = payload.get("usage") or {}
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cread = int(usage.get("cache_read_input_tokens") or 0)
    cwrite = int(usage.get("cache_creation_input_tokens") or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cread,
        "cache_write_tokens": cwrite,
        "total_tokens": inp + out + cread + cwrite,
        # Cache reads are the same context re-read each turn — already paid for
        # when written. Counting them against a ceiling makes the ceiling
        # meaningless: one grooming run read 333k of cache and 3k of new output.
        "chargeable_tokens": inp + out + cwrite,
        "cost_usd": float(payload.get("total_cost_usd") or 0.0),
    }


def invoke(
    prompt: str,
    *,
    model: str,
    tools_allowed: list[str],
    tools_denied: list[str] | None = None,
    cwd: Path | str | None = None,
    timeout_s: int = 600,
    allow_writes: bool = False,
) -> RunResult:
    """Run one agent to completion. Never raises for an agent-side failure.

    `allow_writes` is the M3 addition and the only way Edit/Write reach an
    agent. The caller must have opened a worktree first: the flag says "this run
    may write", the `cwd` says where, and nothing in the contract alone can
    produce both.
    """
    denied = set(ALWAYS_DENIED) | set(tools_denied or [])
    if not allow_writes:
        denied |= set(WRITE_TOOLS)
    denied = sorted(denied)
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--model", model,
        "--allowedTools", *tools_allowed,
        "--disallowedTools", *denied,
    ]

    try:
        proc = proc_mod.run(
            cmd,
            input=prompt,
            cwd=str(cwd or db.PROJECTS_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return RunResult(status="timeout", error=f"no result within {timeout_s}s")
    except FileNotFoundError:
        return RunResult(status="failed", error=f"{CLAUDE_BIN} not found on PATH")

    if proc.returncode != 0 and not proc.stdout.strip():
        return RunResult(status="failed", error=(proc.stderr or "").strip()[:500])

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return RunResult(status="failed", error=f"unparseable output: {proc.stdout[:300]}")

    counters = _usage_from(payload)
    return RunResult(
        status="failed" if payload.get("is_error") else "ok",
        text=payload.get("result") or "",
        session_id=payload.get("session_id"),
        error=payload.get("error") if payload.get("is_error") else None,
        raw=payload,
        **counters,
    )


def run_ticket(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    role: str,
    prompt: str,
    model: str,
    tools_allowed: list[str],
    tools_denied: list[str] | None = None,
    cwd: Path | str | None = None,
    timeout_s: int = 600,
    max_tokens: int | None = None,
    allow_writes: bool = False,
    worktree_path: str | None = None,
) -> RunResult:
    """Spawn for a ticket and record the run, whatever the outcome.

    The `runs` row is opened before the process starts. If the machine dies
    mid-run the ledger still shows a `running` row with a start time — an
    honest "we don't know how this ended" beats a gap that looks like it never
    happened.
    """
    cur = conn.execute(
        "INSERT INTO runs (ticket_id, agent_role, model, status, worktree_path) "
        "VALUES (?,?,?,'running',?)",
        (ticket_id, role, model, worktree_path),
    )
    run_id = cur.lastrowid
    conn.execute("UPDATE tickets SET status = 'running' WHERE id = ?", (ticket_id,))

    started = time.monotonic()
    result = invoke(
        prompt,
        model=model,
        tools_allowed=tools_allowed,
        tools_denied=tools_denied,
        cwd=cwd,
        timeout_s=timeout_s,
        allow_writes=allow_writes,
    )

    status = result.status
    # The ceiling can't stop a run mid-flight — `claude -p` has no turn budget we
    # can set from out here — so it is enforced as a recorded outcome. A role
    # that keeps breaching its ceiling is a contract to renegotiate, and the
    # ledger is where that argument gets its evidence.
    #
    # `over_budget` is kept separate from `status` on purpose. The first version
    # overwrote the status, and the caller's "did this succeed?" check then threw
    # away a completed, correct answer we had already paid for. A breach is a
    # billing fact, not a failure of the work.
    result.over_budget = bool(max_tokens and result.chargeable_tokens > max_tokens)
    if result.over_budget and result.status == "ok":
        status = "killed-over-budget"

    conn.execute(
        """
        UPDATE runs SET ended_at = datetime('now','localtime'), status = ?, session_id = ?,
               input_tokens = ?, output_tokens = ?, cache_read_tokens = ?,
               cache_write_tokens = ?, total_tokens = ?, chargeable_tokens = ?,
               cost_usd = ?, verdict = ?
         WHERE id = ?
        """,
        (
            status, result.session_id,
            result.input_tokens, result.output_tokens, result.cache_read_tokens,
            result.cache_write_tokens, result.total_tokens, result.chargeable_tokens,
            result.cost_usd,
            (result.error or result.text or "")[:2000],
            run_id,
        ),
    )
    result.raw["run_id"] = run_id
    result.raw["elapsed_s"] = round(time.monotonic() - started, 1)
    result.status = status
    return result
