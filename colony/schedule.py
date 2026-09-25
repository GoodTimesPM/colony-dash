r"""The hourly pulse as a hidden Windows scheduled task.

  * pythonw.exe, so no console window appears each hour;
  * `--log` for output, since there is no shell to redirect;
  * the task is marked hidden.

Child processes (git, `claude`) are hidden separately by `proc.py`.
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
    # Quoted because the project root has spaces in it.
    args = f'-m colony pulse --log "{LOG_PATH}"'
    user = getpass.getuser()

    # Register-ScheduledTask rather than schtasks.exe, whose XML chokes on
    # spaces in paths.
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
