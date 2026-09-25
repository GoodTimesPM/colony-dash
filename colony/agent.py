"""Spawning a colonist: one `claude -p` invocation, recorded in the ledger.

The only place Colony Dash spends tokens, so the guards live here: a tool
allowlist, a working directory, a timeout, and a `runs` row written before
the process starts. Tools and model come from the `agents` contract, never
from the persona (docs/design.md §2.1, §8).
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db, proc as proc_mod, secretfiles

CLAUDE_BIN = "claude"

# Denied whatever the contract says, so one bad `agents` row cannot grant them.
# Bash matters most: Edit and Write are confined to a worktree, while a shell
# can push, delete or download (§8.3).
ALWAYS_DENIED = ["Bash", "WebFetch", "WebSearch", "Task", "KillShell", "BashOutput"]

# Additionally denied unless the contract is write-capable *and* the caller has
# opened a worktree for the run to write in.
WRITE_TOOLS = ["Edit", "Write", "NotebookEdit"]


# Home-directory folders that hold credentials for other tools.
HOME_SECRET_DIRS = (".ssh", ".claude", ".aws", ".azure", ".docker", ".gnupg",
                    ".config/gcloud", ".kube")


def _read_denials() -> list[str]:
    """Deny rules that keep Read, Grep and Glob off credential files, enforced
    by the CLI against prompt injection. Anchored at the projects root
    because builds run in worktrees outside it.
    """
    root = db.PROJECTS_ROOT.resolve().as_posix()
    if len(root) > 1 and root[1] == ":":
        root = "/" + root[0].lower() + root[2:]
    names = sorted(secretfiles.EXACT) + list(secretfiles.PATTERNS)
    rules = [f"Read(**/{n})" for n in names]
    rules += [f"Read(/{root}/**/{n})" for n in names]
    rules += [f"Read(~/{d}/**)" for d in HOME_SECRET_DIRS]
    return rules


def settings_file() -> Path:
    """The settings file every run is started with. Rewritten when it changes."""
    body = json.dumps({"permissions": {"defaultMode": "default",
                                       "deny": _read_denials()}}, indent=2)
    path = db.RUNTIME_DIR / "agent-settings.json"
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != body:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return path


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
        """The agent's JSON answer, tolerating prose or a fenced block around
        it. None means the run said nothing actionable, reported as a
        finding.
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
    """The four token counters from `--output-format json`, read defensively
    since the shape is not ours.
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
        # Cache reads are excluded: context already paid for, and counting them
        # made ceilings meaningless (one groom read 333k cached, 3k new).
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
    `allow_writes` is the only way Edit/Write reach an agent, and the caller
    must have opened a worktree for `cwd`.
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
        # Pin the permission mode and load no MCP servers, so user-level
        # settings never reach an agent.
        "--permission-mode", "default",
        "--strict-mcp-config",
        "--settings", str(settings_file()),
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
    """Spawn for a ticket and record the run. The `runs` row opens before the
    process starts, so a crash leaves a `running` row as evidence.
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
    # `claude -p` has no settable turn budget, so the ceiling is a recorded
    # outcome. `over_budget` is separate from `status`: a breach is a billing
    # fact, and the answer is still usable.
    result.over_budget = bool(max_tokens and result.chargeable_tokens > max_tokens)
    if result.over_budget and result.status == "ok":
        status = "over-budget"

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
