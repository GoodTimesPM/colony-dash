r"""Run child processes without flashing a console window.

On Windows a console program started from a GUI process gets a console
window of its own. `hidden()` returns the flags that suppress it, and `{}`
elsewhere, so every call site can pass `**proc.hidden()`. A child can still
inherit its parent's console, which is why the scheduled pulse runs under
pythonw.exe.
"""

from __future__ import annotations

import subprocess
import sys

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
