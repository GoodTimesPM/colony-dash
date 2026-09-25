"""The desktop shortcut. `python -m colony shortcut`.

A `.lnk` is a COM object, so this goes through `WScript.Shell` in
PowerShell. The target is pythonw.exe (no console behind the window), and
the working directory is the project root, where the ledger and `.env` are
resolved.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import icon as icon_mod, proc as proc_mod

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NAME = "Colony Dash"


def pythonw() -> Path:
    """The windowed interpreter beside whichever python is running us."""
    here = Path(sys.executable)
    candidate = here.with_name("pythonw.exe")
    return candidate if candidate.is_file() else here


def desktop_dir() -> Path:
    """Where the shortcut goes: the OneDrive-redirected Desktop when it exists,
    since the original folder is often left empty.
    """
    home = Path(os.path.expanduser("~"))
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive and (Path(onedrive) / "Desktop").is_dir():
        return Path(onedrive) / "Desktop"
    return home / "Desktop"


def create(directory: Path | None = None, *, port: int = 8787) -> Path:
    """Write (or overwrite) the shortcut and return its path."""
    target = directory or desktop_dir()
    target.mkdir(parents=True, exist_ok=True)
    link = target / f"{NAME}.lnk"

    ico = icon_mod.ensure() or ""
    args = f'-m colony dash --port {port}'

    # Single-quoted PowerShell strings with quotes doubled, for paths
    # containing an apostrophe.
    def ps(value) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = f"""
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut({ps(link)})
$sc.TargetPath = {ps(pythonw())}
$sc.Arguments = {ps(args)}
$sc.WorkingDirectory = {ps(PROJECT_ROOT)}
$sc.Description = 'The colony: sprint, board, PO inbox, and the halt switch.'
{f"$sc.IconLocation = {ps(str(ico) + ',0')}" if ico else ""}
$sc.WindowStyle = 1
$sc.Save()
"""
    result = proc_mod.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0 or not link.is_file():
        raise RuntimeError((result.stderr or result.stdout or "the shell refused").strip())
    return link
