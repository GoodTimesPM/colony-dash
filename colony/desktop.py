"""The window: a pywebview shell around the local server, so the dashboard can
live on a second monitor without browser chrome (docs/design.md §9.1).
`--serve` runs the server with no window, which is also the fallback without
WebView2. Binding beyond 127.0.0.1 requires an access token (`access.py`).
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import db, icon as icon_mod

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
LOG_PATH = db.RUNTIME_DIR / "dash.log"

# The address a running server actually bound. The logon task binds a network
# address, so a shortcut probing only loopback would start a second server on
# the same port. Only believed after the port behind it answers.
ADDRESS_PATH = db.RUNTIME_DIR / "dash.url"
WEBVIEW_PROFILE = db.RUNTIME_DIR / "webview"


def log(message: str) -> None:
    """Windowed launches have no console, so the window keeps its own log."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
    except OSError:
        pass
    print(message)


# Source files that mean "the running dashboard is stale" when they change.
SOURCE_SUFFIXES = (".py", ".js", ".css", ".html")


def stamp() -> str:
    """A short hash of path, size and mtime for every source file.

    The server captures it once at import and reports that value forever;
    computing it per request would always look current.
    """
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.suffix not in SOURCE_SUFFIXES or not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        rec = f"{f.relative_to(root).as_posix()}:{st.st_size}:{int(st.st_mtime)}"
        h.update(rec.encode("utf-8"))
    return h.hexdigest()[:16]


def _ask(address: str, port: int, path: str, method: str = "GET",
         timeout_s: float = 3.0) -> dict | None:
    """One local API call, or None on any failure. An unidentified dashboard
    must never be stopped.
    """
    req = urllib.request.Request(f"http://{address}:{port}{path}", method=method)
    req.add_header("x-colony", "1")
    if method != "GET":
        req.add_header("content-type", "application/json")
        req.data = b"{}"
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = f"HTTP {exc.code}"
        return {"status": exc.code, "error": detail}
    except Exception:
        return None


def _stale(address: str, port: int) -> bool:
    """Is the dashboard on this port running code that is no longer on disk?

    The page's scripts are read per request and Python is not, so a stale server serves
    new buttons wired to routes it lacks. Unreachable or unauthenticated
    answers False: killing a working dashboard is the worse mistake.
    """
    got = _ask(address, port, "/api/build")
    if not got:
        return False
    # A 404 means a build older than `/api/build` itself, so stale by
    # definition.
    if got.get("status") == 404:
        return True
    if not got.get("stamp"):
        return False
    return got["stamp"] != stamp()


def _stop(address: str, port: int, timeout_s: float = 12.0) -> bool:
    """Ask the dashboard on this port to exit, and wait for the port to free.

    Asking, not killing: the server knows its own pid, and it can refuse
    while an agent run is writing.
    """
    got = _ask(address, port, "/api/act/quit", method="POST", timeout_s=6.0)
    if got is None:
        log(f"the dashboard on {address}:{port} did not answer a request to stop")
        return False
    if got.get("error"):
        log(f"the dashboard on {address}:{port} would not stop: {got['error']}")
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _port_is_free(address, port):
            return True
        time.sleep(0.2)
    log(f"the dashboard on {address}:{port} said it would stop and did not")
    return False


def _wait_for_port(host: str, port: int, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.15)
    return False


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) != 0


def _mark(address: str, port: int) -> None:
    """Record the address this process is serving on, for the next launch."""
    try:
        ADDRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ADDRESS_PATH.write_text(f"{address}:{port}\n", encoding="utf-8")
    except OSError:
        pass


def _already_serving(port: int) -> str | None:
    """The address of a live dashboard on this machine, or None. The marker
    usually outlives its process, so the port is probed first; other ports
    are ignored.
    """
    try:
        recorded = ADDRESS_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    address, _, recorded_port = recorded.rpartition(":")
    if not address or recorded_port != str(port):
        return None
    return None if _port_is_free(address, port) else address


def _reusable(local: str, port: int) -> str | None:
    """The address of a live dashboard that can serve `local`, or None.

    The marker is consulted only for a loopback request, to find the logon
    task's network-bound server. A loopback server never satisfies a request
    for a network address; the phone needs something listening on the QR's
    address.
    """
    if not _port_is_free(local, port):
        return local
    return _already_serving(port) if local == HOST else None


def _set_window_icon(title: str, tries: int = 40) -> None:
    """Set the colony's icon on the window via WM_SETICON.

    pywebview only takes `icon=` on GTK and Qt; on Windows the frame shows
    pythonw's icon. Runs on its own thread polling for the window, and every
    failure is a silent return.
    """
    path = icon_mod.ensure()
    if not path:
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        for _ in range(tries):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                for which, size in ((ICON_SMALL, 16), (ICON_BIG, 32)):
                    handle = user32.LoadImageW(None, str(path), IMAGE_ICON, size, size,
                                               LR_LOADFROMFILE | LR_DEFAULTSIZE)
                    if handle:
                        user32.SendMessageW(hwnd, WM_SETICON, which, handle)
                return
            time.sleep(0.25)
    except Exception:
        pass


def _idle() -> int:
    """Keep the process alive for the daemon server thread it owns."""
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def launch(port: int = DEFAULT_PORT, *, host: str = HOST, window: bool = True,
           replace: bool = False) -> int:
    from . import server

    # The window always connects via loopback; `0.0.0.0` is not a connect
    # address.
    local = HOST if host in ("0.0.0.0", "::") else host

    # Whether this process owns the port. An owner stays alive for its server
    # thread; a non-owner has nothing to do without a window.
    serving = False

    live = _reusable(local, port)

    # Replace the live server when it is stale, so "restart it" runs the new
    # code.
    if live and (replace or _stale(live, port)):
        why = "asked to restart" if replace else "running code older than this checkout"
        log(f"the dashboard on {live}:{port} is {why}. Stopping it")
        if _stop(live, port):
            live = None
        else:
            # It would not stop and still answers. Attach, but say which build
            # is served; the usual cause is a dashboard older than
            # `/api/act/quit`.
            log("could not stop it. The dashboard now up is NOT running your "
                "latest changes. Close the Colony Dash window (or end the "
                f"pythonw.exe serving port {port}) and start it again; from then "
                "on a restart replaces it on its own.")

    if live:
        # Already served, usually a forgotten window or the logon task. Reuse
        # it.
        local = live
        log(f"already serving on http://{local}:{port}. Reusing it")
    else:
        def _serve():
            try:
                server.serve(host=host, port=port)
            except SystemExit:
                # uvicorn exits 3 on a failed bind. Swallowed here; the caller
                # checks whether the port answers, which decides whether it
                # mattered.
                pass
            except BaseException:
                log("server thread died:\n" + traceback.format_exc())

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        if not _wait_for_port(local, port):
            log(f"server did not come up on {port}; see {LOG_PATH}")
            return 1
        _mark(local, port)

        # The port answers but our thread died: we lost the bind race with
        # another launch. Use theirs and say so in one line.
        thread.join(timeout=0.5)
        if not thread.is_alive():
            log(f"another dashboard bound {local}:{port} first. Reusing it")
        else:
            serving = True
            log(f"serving http://{host}:{port}")
            if host != HOST:
                log("this is reachable from the network. The access token is "
                    "required on every request that is not the login page")

                # Also bind loopback, so the window can reach its own dashboard
                # as local. With only the tailnet address, the console treated
                # the desktop as remote. Not fatal if it fails.
                try:
                    server.serve_extra(HOST, port)
                    local = HOST
                    # Re-mark with loopback so the next launch reuses it.
                    _mark(local, port)
                    log(f"also serving http://{HOST}:{port} for this machine")
                except Exception as exc:
                    log(f"could not also bind {HOST}:{port} ({exc}). The "
                        f"dashboard is still up on {host}")

    if not window:
        if not serving:
            # Headless and not the owner: exit rather than sleep holding an
            # interpreter.
            return 0
        return _idle()

    try:
        import webview
    except ImportError:
        log("pywebview not installed. Running headless; open the URL above")
        return _idle() if serving else 0

    webview.create_window(
        "Colony Dash",
        f"http://{local}:{port}",
        width=1500,
        height=940,
        min_size=(960, 640),
        background_color="#0B0F14",
        # pywebview disables text selection by default; a dashboard must allow
        # copying.
        text_select=True,
    )
    threading.Thread(target=_set_window_icon, args=("Colony Dash",), daemon=True).start()
    try:
        # pywebview's default private mode discards localStorage, where theme,
        # text size and layout live (docs/design.md §9.2). The profile sits
        # under .colony/.
        webview.start(private_mode=False, storage_path=str(WEBVIEW_PROFILE))
    except Exception:  # no WebView2 runtime, no display, etc.
        log("could not open a window; falling back to the browser URL\n"
            + traceback.format_exc())
        return _idle() if serving else 0
    return 0
