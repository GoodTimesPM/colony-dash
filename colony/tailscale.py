r"""Tailscale status and setup for the phone panel.

`net.py` prefers a tailnet address, but a LAN bind stops working the moment
the phone leaves the building, and the panel said nothing about which one it
got. This module answers:

  * installed? (`find`)
  * signed in and connected? (`state`)
  * an installer already in Downloads? (`installer`)
  * the sign-in URL, when signed out (`login`)

It never installs silently: `install()` opens the normal installer window.
The phone also needs Tailscale on the same account, which nothing here can
check, so the panel says so.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import proc

DOWNLOAD_PAGE = "https://tailscale.com/download/windows"

# The CLI's install paths, old ("Tailscale IPN", 32-bit) and current layout.
EXE_CANDIDATES = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale IPN\tailscale.exe",
)

# `tailscale up` prints this and then blocks until the browser visit completes.
AUTH_URL = re.compile(r"https://login\.tailscale\.com/\S+")

# Daemon states. Only the first two are actionable; `state()` folds the rest
# into "starting".
NEEDS_LOGIN = "NeedsLogin"
RUNNING = "Running"


class NotInstalled(RuntimeError):
    """Raised when a call needs the CLI and the CLI is not on this machine."""


def find() -> Path | None:
    """The `tailscale` CLI, or None. Fixed install paths first (the installer
    does not always touch PATH), then PATH.
    """
    for candidate in EXE_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory.strip():
            continue
        try:
            path = Path(directory) / "tailscale.exe"
            if path.exists():
                return path
        except OSError:
            continue
    return None


def installer() -> Path | None:
    """The newest `tailscale-setup-*.exe` in the download folders, by mtime;
    version strings do not compare correctly as text.
    """
    roots = []
    profile = os.environ.get("USERPROFILE")
    if profile:
        roots.append(Path(profile) / "Downloads")
    roots.append(Path.home() / "Downloads")

    found: list[Path] = []
    for root in roots:
        try:
            found.extend(p for p in root.glob("tailscale-setup-*.exe")
                         if p.is_file())
        except OSError:
            continue
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def _status() -> dict:
    """`tailscale status --json`, parsed, or {}. The exit code is ignored: a
    signed-out daemon exits non-zero but still prints the JSON.
    """
    exe = find()
    if not exe:
        return {}
    try:
        done = proc.run([str(exe), "status", "--json"], capture_output=True,
                        text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        return json.loads(done.stdout or "{}")
    except ValueError:
        return {}


def state() -> dict:
    """Everything the panel draws about Tailscale. Never raises."""
    exe = find()
    if not exe:
        setup = installer()
        return {
            "installed": False,
            "backend": "",
            "connected": False,
            "address": None,
            "name": "",
            "installer": str(setup) if setup else None,
            "download": DOWNLOAD_PAGE,
        }

    raw = _status()
    backend = raw.get("BackendState") or ""
    self_node = raw.get("Self") or {}

    address = None
    for value in self_node.get("TailscaleIPs") or []:
        if ":" not in value:              # IPv4, which is what the server binds
            address = value
            break

    # Drop MagicDNS's trailing dot so the URL looks right.
    name = (self_node.get("DNSName") or "").rstrip(".")

    return {
        "installed": True,
        "exe": str(exe),
        "backend": backend,
        "connected": backend == RUNNING and bool(address),
        "needs_login": backend == NEEDS_LOGIN,
        "address": address,
        "name": name,
        "installer": None,
        "download": DOWNLOAD_PAGE,
    }


def install() -> dict:
    """Launch the installer with its window and return at once. Success means
    only that it started; the panel asks the user to refresh afterwards.
    """
    setup = installer()
    if not setup:
        raise NotInstalled(
            "no tailscale-setup-*.exe in Downloads.\n\n"
            f"Get one from {DOWNLOAD_PAGE}, then press refresh here.")

    # Not `proc.run`, which hides the window.
    subprocess.Popen([str(setup)], close_fds=True)
    return {"started": True, "installer": str(setup)}


_login_lock = threading.Lock()
_login: subprocess.Popen | None = None


def login(timeout: float = 25.0) -> dict:
    """Start `tailscale up` and return the sign-in URL it prints.

    It blocks until the browser visit completes, so its output is read on a
    thread until a URL appears (drawn as a QR for the phone) or it exits,
    meaning it reconnected without one. A previous login process is killed
    first.
    """
    global _login

    exe = find()
    if not exe:
        raise NotInstalled("Tailscale is not installed on this machine.")

    with _login_lock:
        if _login is not None and _login.poll() is None:
            try:
                _login.kill()
            except OSError:
                pass

        _login = subprocess.Popen(
            [str(exe), "up"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, **proc.hidden())
        child = _login

    lines: list[str] = []

    def pump() -> None:
        # `readline`, since iterating the stream can buffer the URL on Windows.
        try:
            while True:
                line = child.stdout.readline()
                if not line:
                    return
                lines.append(line)
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in lines:
            match = AUTH_URL.search(line)
            if match:
                return {"url": match.group(0), "output": "".join(lines).strip()}
        if child.poll() is not None:
            break
        time.sleep(0.2)

    # No URL: connected without one, or still working. `state()` knows which.
    return {"url": None, "output": "".join(lines).strip(), **state()}
