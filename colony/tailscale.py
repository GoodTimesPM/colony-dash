r"""The part of phone access that makes it work off your wifi.

`net.py` has preferred a tailnet address over a LAN one since the day it was
written, and that preference was the whole of this feature: if a `100.x.y.z`
address happened to exist, the dashboard bound it and the phone could reach the
ledger from anywhere. If it did not, the dashboard bound `192.168.x.x` instead
and the phone could reach the ledger from the kitchen. Nothing said which of
those two had happened, and nothing offered to change it.

That gap is the single most common way this feature disappoints. A LAN address
is not a lesser tailnet address; it is a different thing that stops existing the
moment you leave the building. Someone who turns phone access on, scans the
code, watches it work, walks to their car and watches it fail has not hit a bug
and has no way to find that out from the panel.

So this module answers four questions and offers a button for the two that have
one:

  * Is Tailscale installed on this machine? (`find`)
  * Is it signed in and connected? (`state`)
  * If it is not installed, is there an installer already sitting in Downloads?
    (`installer`) -- because the answer is usually yes, and hunting for it in a
    browser is the step people stop at.
  * If it is installed and signed out, what URL signs it in? (`login`)

What it will not do is install anything quietly. The Windows installer's silent
switch is not something this file can promise across versions, and an installer
that runs invisibly and fails invisibly is worse than one you have to click. So
`install()` launches the normal installer with its normal window, and the person
watching it is the person who asked for it.

The other half of the setup is on the phone, and nothing here can do it: the
Tailscale app has to be installed there and signed in to the same account. The
panel says so, because a machine that is perfectly configured and a phone that
is not looks identical from this side.
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

# Where the Windows package puts the CLI. Two entries because the installer
# changed layout: builds up to 1.32-ish used "Tailscale IPN" under the 32-bit
# Program Files, everything since uses "Tailscale" under the 64-bit one. Both
# are still on machines that have been upgraded rather than reinstalled.
EXE_CANDIDATES = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale IPN\tailscale.exe",
)

# `tailscale up` prints this and then blocks until the browser visit completes.
AUTH_URL = re.compile(r"https://login\.tailscale\.com/\S+")

# How the daemon describes itself. Only the first two are states a person can do
# anything about from here, which is why `state()` reduces the rest to "starting".
NEEDS_LOGIN = "NeedsLogin"
RUNNING = "Running"


class NotInstalled(RuntimeError):
    """Raised when a call needs the CLI and the CLI is not on this machine."""


def find() -> Path | None:
    """The `tailscale` CLI, or None.

    PATH is checked last rather than first. The installer does not add itself to
    PATH on every version, so a machine with Tailscale installed can still have
    nothing on PATH -- but a machine that has put a `tailscale` of its own on
    PATH has done so deliberately, and the fixed locations should not shadow it
    silently. In practice both find the same file.
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
    """The newest `tailscale-setup-*.exe` in the usual download folders.

    Looked for because it is usually already there. The install path people
    actually take is "download it, get distracted, come back to the dashboard",
    and at that point the file they need is one they have and cannot find. This
    turns the download step into a button that was already pressed.

    Newest by modification time, not by the version in the name: comparing
    `1.102.3` against `1.99.0` as strings gets the wrong answer, and the file
    someone just fetched is the one they meant.
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
    """`tailscale status --json`, parsed, or an empty dict.

    The exit code is ignored on purpose. A signed-out daemon exits non-zero and
    still prints the JSON that says so, and that JSON is exactly the answer this
    module wants -- treating the exit code as authority would turn the most
    common state into "cannot tell".
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
    """Everything the panel draws about Tailscale, in one call.

    Nothing here raises. "Not installed" is an ordinary answer, not a failure,
    and this is called every time the phone panel opens.
    """
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

    # MagicDNS hands back a trailing dot, the way a DNS name is properly
    # written. It is correct and it looks like a typo in a URL, so it goes.
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
    """Launch the installer, with its window, and return without waiting.

    Deliberately not silent and deliberately not awaited. Installing Tailscale
    restarts the network stack and asks for administrator, both of which are
    things a person should watch happen rather than discover afterwards -- and a
    dashboard request that blocked for the length of an MSI would time out long
    before the install finished anyway.

    So the panel's job after this is to say "come back and press refresh", which
    is honest about what it knows: this function's success means the installer
    started, and nothing more.
    """
    setup = installer()
    if not setup:
        raise NotInstalled(
            "no tailscale-setup-*.exe in Downloads.\n\n"
            f"Get one from {DOWNLOAD_PAGE}, then press refresh here.")

    # Not `proc.run`: hiding an installer's window is the opposite of what is
    # wanted, and this is the one child process in the codebase that has
    # something to say to the person who started it.
    subprocess.Popen([str(setup)], close_fds=True)
    return {"started": True, "installer": str(setup)}


_login_lock = threading.Lock()
_login: subprocess.Popen | None = None


def login(timeout: float = 25.0) -> dict:
    """Start `tailscale up` and hand back the sign-in URL it prints.

    The command blocks until the browser visit completes, so it is started and
    left running rather than waited on. Its output is read on a thread until
    either a login URL appears -- which is the answer, and which the panel can
    draw as a QR code so the phone can do the signing in -- or the process exits
    on its own, which means it was already signed in and simply reconnected.

    One at a time. Pressing the button twice should not leave two daemonised
    `tailscale up` processes fighting over the same login, so a previous one is
    killed first. Killing it is safe: it has done nothing yet except print a
    URL, and the URL it printed stops being the current one the moment a new
    command asks for another.
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
        # `for line in stream` buffers on Windows in a way that can hold the URL
        # back until the process exits, and the process does not exit until the
        # URL has been visited. `readline` does not.
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

    # No URL. Either it connected without needing one, or it is still thinking.
    # `state()` is the authority on which, and it reads the daemon rather than
    # this process's output.
    return {"url": None, "output": "".join(lines).strip(), **state()}
