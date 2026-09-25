r"""Whether Windows Firewall lets the phone's requests reach the server.

A dropped packet has no error at either end; the phone just spins. The trap
is that `python.exe` gets an "allow" prompt and a rule the first time it
binds, but the logon task and shortcut run `pythonw.exe`, a different file
with no window to prompt from. So it works from a terminal and fails
unattended.

Adding a rule needs admin, which the dashboard must never have. This module
reads unelevated and hands back the command; only the CLI's `allow()`
elevates. The rule is narrow: one TCP port, inbound, private profiles only.
"""

from __future__ import annotations

import subprocess as proc

from . import shortcut
from .schedule import _ps, _run_ps

RULE_NAME = "Colony Dash"


def _quiet_ps(script: str) -> str:
    """`_run_ps` without the raise. Unelevated reads can be refused, and the
    page must still draw.
    """
    try:
        return _run_ps(script)
    except (RuntimeError, OSError, proc.SubprocessError):
        return ""


def rule_command(port: int = 8787) -> str:
    """The command that opens the port, to be run in an admin PowerShell."""
    return (
        f"New-NetFirewallRule -DisplayName '{RULE_NAME}' -Direction Inbound "
        f"-Action Allow -Protocol TCP -LocalPort {port} -Profile Private,Domain"
    )


def state(port: int = 8787) -> str:
    """One of `open`, `blocked` or `unknown`. Unelevated callers cannot always
    read port filters, and `unknown` must not send someone to fix the wrong
    thing.
    """
    script = f"""
$ErrorActionPreference = 'Stop'
$r = Get-NetFirewallRule -DisplayName {_ps(RULE_NAME)} -ErrorAction SilentlyContinue |
     Where-Object {{ $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }}
if (-not $r) {{ 'blocked'; exit 0 }}
foreach ($rule in $r) {{
  $p = ($rule | Get-NetFirewallPortFilter).LocalPort
  if ($p -contains '{port}' -or $p -eq 'Any') {{ 'open'; exit 0 }}
}}
'blocked'
"""
    answer = _quiet_ps(script).strip().splitlines()
    if not answer:
        return "unknown"
    last = answer[-1].strip()
    return last if last in ("open", "blocked") else "unknown"


def allow(port: int = 8787) -> None:
    """Add the rule through one UAC prompt. Raises with the manual command on
    refusal. `-Wait` stops PowerShell returning success while the prompt is
    still showing.
    """
    inner = rule_command(port).replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$p = Start-Process powershell -Verb RunAs -Wait -PassThru "
        f"-ArgumentList '-NoProfile','-Command','{inner}'\n"
        "if ($p.ExitCode -ne 0) {{ throw 'the elevated command failed' }}"
    ).replace("{{", "{").replace("}}", "}")
    try:
        _run_ps(script)
    except (RuntimeError, OSError, proc.SubprocessError) as exc:
        raise RuntimeError(
            f"could not add the firewall rule ({exc}).\n\n"
            "Open PowerShell as administrator and run:\n\n"
            f"    {rule_command(port)}\n"
        ) from exc


def program() -> str:
    """The executable the rule must name. Only used in messages."""
    return str(shortcut.pythonw())
