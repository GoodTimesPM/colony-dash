"""The window. A pywebview shell around the local server.

It runs as a real desktop window rather than a browser tab so it can live on the
second monitor without chrome around it (ARCHITECTURE.md §9.1) — the same trick
the tray app uses. `http://127.0.0.1:8787` still works if you'd rather, and
`--serve` gives you exactly that with no window at all, which is also the
fallback on a machine with no WebView2 runtime.

The server is bound to 127.0.0.1 and nothing else. The ledger holds project
notes and run transcripts; it has no business being reachable from the network.
"""

from __future__ import annotations

import socket
import threading
import time
import traceback
from datetime import datetime

from . import db

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
LOG_PATH = db.RUNTIME_DIR / "dash.log"


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


def launch(port: int = DEFAULT_PORT, *, window: bool = True) -> int:
    from . import server

    if not _port_is_free(HOST, port):
        # Someone already has it — almost always a dashboard you forgot was open.
        # Opening a second server on a second port would leave two windows
        # claiming to be the dashboard, so point at the live one instead.
        log(f"already serving on http://{HOST}:{port} — reusing it")
    else:
        def _serve():
            try:
                server.serve(host=HOST, port=port)
            except BaseException:
                log("server thread died:\n" + traceback.format_exc())

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        if not _wait_for_port(HOST, port):
            log(f"server did not come up on {port}; see {LOG_PATH}")
            return 1
        log(f"serving http://{HOST}:{port}")

    if not window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    try:
        import webview
    except ImportError:
        log("pywebview not installed — running headless; open the URL above")
        return launch(port, window=False)

    webview.create_window(
        "Colony Dash",
        f"http://{HOST}:{port}",
        width=1500,
        height=940,
        min_size=(960, 640),
        background_color="#0B0F14",
    )
    try:
        webview.start()
    except Exception:  # no WebView2 runtime, no display, etc.
        log("could not open a window; falling back to the browser URL\n"
            + traceback.format_exc())
        return launch(port, window=False)
    return 0
