"""The window. A pywebview shell around the local server.

It runs as a real desktop window rather than a browser tab so it can live on the
second monitor without chrome around it (ARCHITECTURE.md §9.1) — the same trick
the tray app uses. `http://127.0.0.1:8787` still works if you'd rather, and
`--serve` gives you exactly that with no window at all, which is also the
fallback on a machine with no WebView2 runtime.

The server binds 127.0.0.1 unless it is told otherwise, and being told otherwise
is deliberately hard: `--host` on a non-loopback address requires an access
token to be configured or the process refuses to start (`access.py`). The ledger
holds project notes and run transcripts, so the default has to be the safe one
and the network has to be asked for out loud.
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

# Where a running server records the address it actually bound. `_port_is_free`
# can only ask about one address, and the autostart task binds a network address
# rather than loopback -- so a server started at logon is completely invisible to
# a shortcut that only probes 127.0.0.1, and double-clicking the icon would raise
# a second server on the same port on a different interface. Two dashboards, one
# ledger, and no error anywhere. The file is a hint and never a fact: it is only
# ever believed after the port behind it answers.
ADDRESS_PATH = db.RUNTIME_DIR / "dash.url"
WEBVIEW_PROFILE = db.RUNTIME_DIR / "webview"


def log(message: str) -> None:
    """Windowed launches have no console to print to, so the window keeps its own
    log. A GUI that dies silently is a GUI you debug by guessing.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
    except OSError:
        pass
    print(message)


# What counts as source for the purpose of "is the running dashboard this
# build". Python because it is the server, and the three web types because the
# page is served off disk and a change to `app.js` alone is a change a person
# would absolutely expect a restart to pick up.
SOURCE_SUFFIXES = (".py", ".js", ".css", ".html")


def stamp() -> str:
    """A short hash of the code this process would run right now.

    Path, size and mtime of every source file in the package. Contents are not
    read: this runs on every launch, and no editor writes a change that leaves
    both the size and the mtime alone.

    The server captures this once, at import, and answers with the captured
    value forever. That is the entire trick. Computing it per request would read
    the files as they are now and every server would always look current, which
    is the bug this exists to catch.
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
    """One local API call, or None if the server would not answer it.

    None is deliberately the answer to every failure, including a 403 and a
    machine that has no such route. A dashboard that cannot be identified is one
    this process must not stop, so every unknown collapses to "leave it alone".
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

    A server older than the checkout is the reason "I restarted it" and "it
    picked up my change" stopped being the same sentence. The window is a shell
    around whatever process owns the port, `app.js` is read off disk on every
    request, and Python is not — so a stale server serves a new page wired to
    routes it has never heard of, and the only symptom is a 404 on a button that
    visibly exists.

    Unreachable, unauthenticated or too old to have the route all answer False.
    Not knowing is not the same as knowing it is stale, and the cost of guessing
    wrong here is killing a working dashboard.
    """
    got = _ask(address, port, "/api/build")
    if not got:
        return False
    # No such route. Every build that can answer this question has one, so a
    # dashboard that 404s here is by definition older than the build asking --
    # and this is the only signal available for exactly the upgrade that
    # introduces the route. Without it the first restart after installing this
    # change is the one restart that still silently does nothing.
    if got.get("status") == 404:
        return True
    if not got.get("stamp"):
        return False
    return got["stamp"] != stamp()


def _stop(address: str, port: int, timeout_s: float = 12.0) -> bool:
    """Ask the dashboard on this port to exit, and wait until the port frees.

    Asking rather than killing. The server knows its own pid for certain and
    this process would be guessing at one, and a guess that lands on a recycled
    pid kills something that has nothing to do with the colony. It also lets the
    server refuse: an agent run in flight is a write in progress, and dropping
    the interpreter under it is exactly the sort of thing a restart should not
    quietly do.
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
    """The address of a live dashboard on this machine, or None.

    Stale markers are the normal case -- the file outlives the process that
    wrote it every single time -- so the port is always probed before the file
    is believed. A marker for a different port is ignored rather than trusted,
    because two dashboards on two ports is a thing someone may have meant.
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
    """The address of a live dashboard that satisfies a request to serve `local`.

    The port is asked about directly first, which settles it whenever the answer
    is yes. The marker file is a fallback for one specific case and only that
    one: a desktop shortcut probes loopback, while the logon task may have put
    the dashboard on a network address, and starting a second server on the same
    port on a different interface would leave two dashboards on one ledger.

    Consulting the marker in the *other* direction was a bug, and a silent one.
    The logon task asked for 10.0.0.57, found the desktop dashboard answering on
    127.0.0.1, concluded it was already serving and exited. The log said the
    dashboard was up. The dashboard was up. And the phone spun on a blank tab
    forever, because nothing had ever listened on the address in the QR code.

    A loopback server does not satisfy a request for a network address. It is
    the whole point of the request.
    """
    if not _port_is_free(local, port):
        return local
    return _already_serving(port) if local == HOST else None


def _set_window_icon(title: str, tries: int = 40) -> None:
    """Hang the colony's mark on the window frame.

    pywebview only accepts an `icon=` on its GTK and Qt backends; on Windows the
    frame takes whatever icon the host process has, which is `pythonw.exe` — the
    same generic snake as every other Python program on this machine, which is
    the collision that started this. So the icon is set the Windows way, by
    finding the window once it exists and sending it WM_SETICON.

    Entirely cosmetic, and it runs on its own thread polling for the window,
    because `webview.start()` blocks and the window does not exist until it
    does. Every failure path is a silent return: a dashboard that will not open
    because its icon did not load would be a much worse bug than a plain icon.
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
    """Hold the process open for the daemon server thread it owns.

    The server runs on a daemon thread, so returning from `launch` would take it
    down with the interpreter. Only a process that actually bound the port has a
    reason to sit here.
    """
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def launch(port: int = DEFAULT_PORT, *, host: str = HOST, window: bool = True,
           replace: bool = False) -> int:
    from . import server

    # The window always points at loopback even when the server is bound wider.
    # `0.0.0.0` is an address to listen on, not one to connect to, and the
    # desktop shell is on the machine doing the listening either way.
    local = HOST if host in ("0.0.0.0", "::") else host

    # Whether *this* process ended up owning the port. It decides what happens
    # when there is no window to hold the process open: an owner has a server
    # thread to keep alive, and a non-owner has nothing left to do.
    serving = False

    live = _reusable(local, port)

    # Reuse is right when the server on the port is this build. It is wrong when
    # it is not, and it was wrong silently: launching the app found a live
    # dashboard, pointed a window at it and reported success, so the one thing a
    # person means by "restart it" -- run the code I just changed -- was the one
    # thing a restart could not do. Stopping the old process and taking the port
    # is what that sentence has to mean.
    if live and (replace or _stale(live, port)):
        why = "asked to restart" if replace else "running code older than this checkout"
        log(f"the dashboard on {live}:{port} is {why} — stopping it")
        if _stop(live, port):
            live = None
        else:
            # It would not go, and it still answers. Attaching to it is better
            # than leaving nothing up, but the log has to say which build is
            # actually being served or the next hour is spent debugging a fix
            # that is on disk and not in the process.
            #
            # The usual reason is a dashboard started before `/api/act/quit`
            # existed, which is a one-time problem per machine and worth naming
            # precisely: "it will not stop" sends someone reading logs, and
            # "close the window, then start it again" ends it.
            log("could not stop it — the dashboard now up is NOT running your "
                "latest changes. Close the Colony Dash window (or end the "
                f"pythonw.exe serving port {port}) and start it again; from then "
                "on a restart replaces it on its own.")

    if live:
        # Someone already has it — almost always a dashboard you forgot was open,
        # or the one the logon task started on a network address. Opening a
        # second server would leave two windows claiming to be the dashboard, so
        # point at the live one instead.
        local = live
        log(f"already serving on http://{local}:{port} — reusing it")
    else:
        def _serve():
            try:
                server.serve(host=host, port=port)
            except SystemExit:
                # uvicorn's answer to a failed bind: one ERROR line naming the
                # address, then `sys.exit(3)`. Letting that reach the handler
                # below wrote forty lines of asyncio internals into the log for
                # an event that is usually not a failure at all, and buried the
                # ones that are. Whether it mattered is decided by the caller,
                # who is the only party that can ask whether the port answers.
                pass
            except BaseException:
                log("server thread died:\n" + traceback.format_exc())

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        if not _wait_for_port(local, port):
            log(f"server did not come up on {port}; see {LOG_PATH}")
            return 1
        _mark(local, port)

        # The port answers. That is not the same as "this process is serving
        # it": `_reusable` asked whether the address was free and the bind
        # happened a moment later, and two callers aim straight at that window
        # -- the logon task, and the phone switch binding the address from the
        # process it was pressed in. One of them loses the race, and losing is
        # the correct outcome, because there is one dashboard on one ledger
        # either way. A dead serving thread is how this process learns it was
        # the loser, and the right response is the one `_reusable` would have
        # given a second earlier: use theirs, and say so in a single line.
        thread.join(timeout=0.5)
        if not thread.is_alive():
            log(f"another dashboard bound {local}:{port} first — reusing it")
        else:
            serving = True
            log(f"serving http://{host}:{port}")
            if host != HOST:
                log("this is reachable from the network — the access token is "
                    "required on every request that is not the login page")

                # And loopback as well, so this machine can always reach its own
                # dashboard by the name that means "here". `--host auto` binds
                # one address and one only, which left the logon task serving a
                # tailnet address with nothing on 127.0.0.1: the window then had
                # to open the tailnet address, every request from it arrived
                # looking like it came off the network, and the console -- which
                # is scoped to the machine rather than to the token -- refused
                # the desktop it was running on.
                #
                # A failure here is not fatal. The dashboard on the network
                # address is up and works; what is lost is the shortcut being
                # able to point at loopback, so it is logged and carried on
                # from rather than raised.
                try:
                    server.serve_extra(HOST, port)
                    local = HOST
                    # Re-recorded, because `_mark` ran a few lines up with the
                    # network address and the marker is what the *next* launch
                    # reuses. Leaving it would point tomorrow's window back at
                    # the tailnet address and undo this on the next restart.
                    _mark(local, port)
                    log(f"also serving http://{HOST}:{port} for this machine")
                except Exception as exc:
                    log(f"could not also bind {HOST}:{port} ({exc}) — the "
                        f"dashboard is still up on {host}")

    if not window:
        if not serving:
            # Losing the race is the correct outcome, but for a headless launch
            # it used to be an outcome with no exit. The process logged "reusing
            # it", fell into the sleep below and stayed there, holding a console
            # and a Python interpreter for a server it did not own. Nine of them
            # had piled up before anyone looked. There is nothing to keep alive
            # here: the dashboard that answers on this port lives in another
            # process, and this one is done.
            return 0
        return _idle()

    try:
        import webview
    except ImportError:
        log("pywebview not installed — running headless; open the URL above")
        return _idle() if serving else 0

    webview.create_window(
        "Colony Dash",
        f"http://{local}:{port}",
        width=1500,
        height=940,
        min_size=(960, 640),
        background_color="#0B0F14",
        # pywebview defaults `text_select` to False and enforces it by injecting
        # `user-select: none` over the entire document — so nothing on the page
        # could be highlighted or copied, including the one thing a dashboard
        # exists to produce: a number or a sentence you want to paste somewhere
        # else. It is a kiosk default living in a tool, and it made a read-only
        # panel of findings unreadable in the only way that matters.
        text_select=True,
    )
    threading.Thread(target=_set_window_icon, args=("Colony Dash",), daemon=True).start()
    try:
        # pywebview defaults to `private_mode=True`, which hands WebView2 an
        # incognito profile: every localStorage key the page writes is thrown
        # away when the window closes. The theme, the text size, the palette and
        # the tile layout all live there — deliberately, because how you read the
        # page is not the colony's business (ARCHITECTURE.md §9.2) — so the
        # default silently reset the dashboard's whole appearance on every
        # launch, and looked like a bug in the picker rather than in the shell.
        # The profile goes next to the ledger, under .colony/, so it is scoped to
        # this project and disappears with it.
        webview.start(private_mode=False, storage_path=str(WEBVIEW_PROFILE))
    except Exception:  # no WebView2 runtime, no display, etc.
        log("could not open a window; falling back to the browser URL\n"
            + traceback.format_exc())
        return _idle() if serving else 0
    return 0
