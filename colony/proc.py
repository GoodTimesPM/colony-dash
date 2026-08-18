r"""Run child processes without flashing a console window.

Every subprocess this codebase starts is a headless helper — `git status` for
the Projects panel, `git worktree add` for a build, the `claude` CLI for a run,
`WScript.Shell` for the desktop shortcut. None of them has anything to say to a
terminal. But on Windows a console application launched from a GUI process gets
a console *allocated for it*, and the result is a black window that appears and
vanishes several times a minute for as long as the dashboard is open. The
dashboard samples the projects every thirty seconds; that alone is two flashes a
minute, forever.

`CREATE_NO_WINDOW` is the fix, and it is the only thing in this module. It is a
Windows-only flag, so it resolves to an empty dict everywhere else and every
call site can pass `**proc.hidden()` unconditionally.

One thing it does *not* do: hide a console that the process was launched with.
If the parent has a console, the child can still inherit it — which is why the
scheduled pulse runs under `pythonw.exe` rather than `cmd.exe`. Suppressing the
window has to happen on both sides of the launch.
"""

from __future__ import annotations

import subprocess
import sys

# 0x08000000. Named rather than spelled, because a bare hex constant in a
# subprocess call is the kind of thing that gets deleted by someone tidying up.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

IS_WINDOWS = sys.platform == "win32"


def hidden() -> dict:
    """Keyword arguments for `subprocess.run`/`Popen` that suppress the console."""
    if not IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}


def run(cmd, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` with the console suppressed. Same signature otherwise."""
    for key, value in hidden().items():
        kwargs.setdefault(key, value)
    return subprocess.run(cmd, **kwargs)
