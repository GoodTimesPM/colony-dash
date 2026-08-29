r"""The dashboard, already running when you pick up your phone.

Reaching the dashboard from a phone worked the moment the server could bind a
network address, and then did not work in practice, for a boring reason: it only
ran while a terminal was open on the desktop. The phone is the device you use
*because* you are not at the desk, so "first go to the desk and start it" is the
whole feature cancelling itself out.

So this registers a second scheduled task, beside the hourly pulse, that starts
the server at logon and leaves it up. It is deliberately the same shape as
`schedule.py` — pythonw so there is no console, hidden so it does not flicker in
the task list, `--log` because a background process with nowhere to print is a
process you debug by guessing — and it differs in exactly three ways, each of
which is a bug if you get it wrong:

  * **No execution time limit.** The default is three days, after which Task
    Scheduler kills the task. A server that stops on the third Tuesday and comes
    back at the next logon is worse than one that never started, because you
    will not notice until you are away from the machine.
  * **`--host auto`, not a literal address.** The task is written once and runs
    for months; the address is a fact about the network at boot. See `net.py`.
  * **Restart on failure**, three times, a minute apart. The one failure this
    actually covers is losing the race with the network at logon.

The task holds no secret. It names the project directory and a flag; the access
token stays in `.env`, read at startup by the process the task launches.
"""

from __future__ import annotations

import getpass
from pathlib import Path

from . import access, db, net, shortcut
from .schedule import PROJECT_ROOT, _ps, _run_ps

TASK_NAME = "Colony Dash Server"
LOG_PATH = db.RUNTIME_DIR / "dash.log"

# Long enough for Tailscale or wifi to have come up, short enough that the
# dashboard is there before you are. `--host auto` resolves after this delay,
# which is the entire reason the delay exists.
START_DELAY = "PT45S"


def preflight(host: str = "auto", port: int = 8787) -> tuple[str, str]:
    """Refuse now, in the terminal, rather than at 7am in a log nobody reads.

    Resolves what `--host auto` will resolve to and asks `access.check` the same
    question the server will ask. Returns the address and its kind. Raises
    `net.NoAddress` or `access.Unconfigured`, both of which carry the fix.
    """
    address, kind = net.resolve(host)
    access.check(address)
    return address, kind


def install(*, host: str = "auto", port: int = 8787) -> str:
    """Create or replace the logon task. Returns what the scheduler reports."""
    preflight(host, port)
    pythonw = shortcut.pythonw()
    # Quoted for the same reason as the pulse task: this machine's project root
    # has spaces in it, and unquoted the scheduler hands pythonw `--log D:\ALL`
    # plus two strays that argparse rejects.
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
    """True when the task will not be killed after three days.

    Task Scheduler spells 'no limit' as an empty limit or `PT0S`, and spells the
    dangerous default as `P3D`.
    """
    return task.get("time_limit", "") in ("", "PT0S")


def live(port: int = 8787) -> str | None:
    """The address a dashboard is actually answering on, or None."""
    from . import desktop

    return desktop._already_serving(port)
