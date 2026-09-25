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


def alive(pid: int) -> bool:
    """Whether a process with this pid is still running."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied still means the process exists.
            return kernel.GetLastError() == 5
        try:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(pid: int) -> None:
    """End a process and everything it started.

    `Popen.kill` stops only the direct child. `claude` runs its work in node
    workers and `shell=True` puts cmd.exe in front of the real command, so
    killing the child alone leaves the work running.
    """
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, **hidden())
        return
    import os
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run(cmd, *, input=None, capture_output: bool = False, timeout: float | None = None,
        check: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` with the console suppressed, killing the whole tree on timeout."""
    for key, value in hidden().items():
        kwargs.setdefault(key, value)
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if not IS_WINDOWS:
        kwargs.setdefault("start_new_session", True)
    with subprocess.Popen(cmd, **kwargs) as child:
        try:
            out, err = child.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(child.pid)
            child.kill()
            out, err = child.communicate()
            raise subprocess.TimeoutExpired(child.args, timeout, output=out, stderr=err)
        except BaseException:
            kill_tree(child.pid)
            raise
    result = subprocess.CompletedProcess(child.args, child.returncode, out, err)
    if check:
        result.check_returncode()
    return result
