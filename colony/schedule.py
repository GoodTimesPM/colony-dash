r"""The hourly pulse, installed as a Windows scheduled task that never shows itself.

The first version of this ran `cmd.exe /c pulse.cmd`, which is the obvious thing
and the wrong thing: an interactive scheduled task that launches a console
application gets a real console window on the user's desktop, once an hour, and
it stays up for as long as the pulse runs — which, on an hour that escalates to
a wake, is minutes. A background heartbeat that steals focus is not a background
heartbeat.

Three changes fix it, and all three are needed:

  * the task runs **pythonw.exe**, which has no console to show;
  * the pulse writes its own log through `--log`, because the redirect that
    `pulse.cmd` was there to provide is exactly what required a shell;
  * the task itself is marked hidden, so it does not flicker in the task list.

The child processes a pulse starts — git, and the `claude` CLI on a wake — are
suppressed separately in `proc.py`. Both halves are required: pythonw stops the
parent window, `CREATE_NO_WINDOW` stops the children.
"""

from __future__ import annotations

import getpass
from pathlib import Path

from . import db, proc, shortcut

TASK_NAME = "Colony Dash Pulse"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = db.RUNTIME_DIR / "pulse.log"


def _ps(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_ps(script: str) -> str:
    result = proc.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "the scheduler refused").strip())
    return result.stdout


def install(*, hour_interval: int = 1) -> str:
    """Create or replace the hourly task. Returns what the scheduler reports."""
    pythonw = shortcut.pythonw()
    args = f'-m colony pulse --log {LOG_PATH}'
    user = getpass.getuser()

    # Register-ScheduledTask over schtasks.exe: the XML dialect of schtasks is
    # unforgiving about paths with spaces, and this machine's project root has
    # two of them.
    script = f"""
$ErrorActionPreference = 'Stop'
$act = New-ScheduledTaskAction -Execute {_ps(pythonw)} -Argument {_ps(args)} -WorkingDirectory {_ps(PROJECT_ROOT)}
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(7) `
       -RepetitionInterval (New-TimeSpan -Hours {hour_interval})
$set = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries `
       -DontStopIfGoingOnBatteries -StartWhenAvailable `
       -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$prn = New-ScheduledTaskPrincipal -UserId {_ps(user)} -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName {_ps(TASK_NAME)} -Action $act -Trigger $trg `
    -Settings $set -Principal $prn -Force `
    -Description 'One colony heartbeat. Windowless: pythonw, hidden task, no console.' | Out-Null
(Get-ScheduledTask -TaskName {_ps(TASK_NAME)}).Actions[0].Execute
"""
    return _run_ps(script).strip()


def describe() -> dict:
    """What the scheduler currently holds, or `{}` when the task is absent."""
    script = f"""
$t = Get-ScheduledTask -TaskName {_ps(TASK_NAME)} -ErrorAction SilentlyContinue
if (-not $t) {{ 'missing'; exit 0 }}
$i = Get-ScheduledTaskInfo -TaskName {_ps(TASK_NAME)}
$t.Actions[0].Execute
$t.Actions[0].Arguments
[string]$t.Settings.Hidden
[string]$i.LastRunTime
[string]$i.NextRunTime
[string]$i.LastTaskResult
"""
    lines = [ln.strip() for ln in _run_ps(script).splitlines() if ln.strip()]
    if not lines or lines[0] == "missing":
        return {}
    keys = ["execute", "arguments", "hidden", "last_run", "next_run", "last_result"]
    return dict(zip(keys, lines + [""] * len(keys)))


def windowless() -> bool:
    """True when the installed task cannot put a console on the desktop."""
    task = describe()
    return bool(task) and task.get("execute", "").lower().endswith("pythonw.exe")
