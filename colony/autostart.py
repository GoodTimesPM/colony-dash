r"""A logon task that starts the dashboard server and keeps it up, so the phone
can reach it without a terminal open on the desk.

Same shape as the pulse task in `schedule.py` (pythonw, hidden, `--log`),
with three differences:

  * No execution time limit. The default kills the task after three days.
  * `--host auto`, since the address is a fact about the network at boot
    (see `net.py`).
  * Restart on failure, three times a minute apart, for losing the race with
    the network at logon.

The task holds no secret; the token stays in `.env`.
"""

from __future__ import annotations

import getpass

from . import access, db, net, shortcut
from .schedule import PROJECT_ROOT, _ps, _run_ps

TASK_NAME = "Colony Dash Server"
LOG_PATH = db.RUNTIME_DIR / "dash.log"

# Long enough for Tailscale or wifi to come up before `--host auto` resolves.
START_DELAY = "PT45S"


def preflight(host: str = "auto", port: int = 8787) -> tuple[str, str]:
    """Fail in the terminal now rather than in a log at 7am. Resolves the host
    and asks `access.check` what the server will ask. Raises `net.NoAddress`
    or `access.Unconfigured`.
    """
    address, kind = net.resolve(host)
    access.check(address)
    return address, kind


def install(*, host: str = "auto", port: int = 8787) -> str:
    """Create or replace the logon task. Returns what the scheduler reports."""
    preflight(host, port)
    pythonw = shortcut.pythonw()
    # Quoted because the project root has spaces in it.
    args = f'-m colony dash --serve --host {host} --port {port} --log "{LOG_PATH}"'
    user = getpass.getuser()

    script = f"""
$ErrorActionPreference = 'Stop'
$act = New-ScheduledTaskAction -Execute {_ps(pythonw)} -Argument {_ps(args)} -WorkingDirectory {_ps(PROJECT_ROOT)}
$trg = New-ScheduledTaskTrigger -AtLogOn -User {_ps(user)}
$trg.Delay = '{START_DELAY}'
$set = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries `
       -DontStopIfGoingOnBatteries -StartWhenAvailable `
       -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
       -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$prn = New-ScheduledTaskPrincipal -UserId {_ps(user)} -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName {_ps(TASK_NAME)} -Action $act -Trigger $trg `
    -Settings $set -Principal $prn -Force `
    -Description 'The dashboard server, up from logon. No window, no time limit.' | Out-Null
(Get-ScheduledTask -TaskName {_ps(TASK_NAME)}).Actions[0].Execute
"""
    return _run_ps(script).strip()


def start_now() -> None:
    """Run the task immediately, so installing it is also starting it."""
    _run_ps(f"Start-ScheduledTask -TaskName {_ps(TASK_NAME)}")


def remove() -> None:
    _run_ps(
        f"Unregister-ScheduledTask -TaskName {_ps(TASK_NAME)} "
        f"-Confirm:$false -ErrorAction SilentlyContinue"
    )


def describe() -> dict:
    """What the scheduler currently holds, or `{}` when the task is absent."""
    script = f"""
$t = Get-ScheduledTask -TaskName {_ps(TASK_NAME)} -ErrorAction SilentlyContinue
if (-not $t) {{ 'missing'; exit 0 }}
$i = Get-ScheduledTaskInfo -TaskName {_ps(TASK_NAME)}
$t.Actions[0].Execute
$t.Actions[0].Arguments
[string]$t.State
[string]$t.Settings.ExecutionTimeLimit
[string]$i.LastRunTime
[string]$i.LastTaskResult
"""
    lines = [ln.strip() for ln in _run_ps(script).splitlines() if ln.strip()]
    if not lines or lines[0] == "missing":
        return {}
    keys = ["execute", "arguments", "state", "time_limit", "last_run", "last_result"]
    return dict(zip(keys, lines + [""] * len(keys)))


def unlimited(task: dict) -> bool:
    """True when the task has no time limit (empty or `PT0S`; the default is
    `P3D`).
    """
    return task.get("time_limit", "") in ("", "PT0S")


def live(port: int = 8787) -> str | None:
    """The address a dashboard is actually answering on, or None."""
    from . import desktop

    return desktop._already_serving(port)
