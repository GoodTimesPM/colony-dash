"""The dashboard's backend: FastAPI over the ledger.

**Reads** open the ledger with `read_only=True`, so no read path can change
anything. **Writes** exist only under `/api/act/*`, each a thin wrapper
around one function in `control.py`, which records the decision in
`po_actions` and refuses what the colony's rules forbid.

The routes live in `colony/web/`, one router per area of the page. This
module builds the app, mounts them, and owns the gate and the process.

The server binds 127.0.0.1 by default. With phone access on it also binds a
LAN or tailnet address, and off-machine requests need the access token (see
`gate` and `access.py`). On top of that:

  * `same_host` refuses a Host header that is not this machine (DNS
    rebinding) and any write whose Origin is another site;
  * every action requires an `X-Colony` header, which a cross-site form cannot
    set without a preflight the browser refuses;
  * `/api/console/*` and token rotation answer only peers on this machine
    (`web.console._desk_only`), because the console is a real shell.

Live updates are server-sent events. The pulse writes from another process,
so the server polls its own snapshot on a short timer and pushes only when
the fingerprint changes.
"""

from __future__ import annotations

import threading
import urllib.parse
from html import escape as html_escape

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import access
from .web.common import PAGE_CSP
from .web import console, controls, pages, phone, reads, roster, state, stories

# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Colony Dash", docs_url=None, redoc_url=None)

# Mounted in page order. No two routers share a path, so the order only
# decides how /docs would list them, and /docs is off.
for area in (pages, state, roster, reads, stories, phone, controls, console):
    app.include_router(area.router)


# ── the gate ──────────────────────────────────────────────────────────────────
# Off by default and never on a loopback bind: `serve()` enables it only for a
# reachable address, and `access.check` refuses that bind without a token. It
# guards everything except what a phone needs to show the login, plus icons.

PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/sw.js", "/favicon.ico",
                "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"}

# Set by `serve()`. A module-level flag rather than app state because the
# middleware has to be able to answer "am I on?" before anything else runs.
REQUIRE_TOKEN = False

# Set only by `serve_extra`: this process serves loopback as well as a network
# address, and the loopback socket was open before. A token demanded there
# would log out the desktop window, a caller that can read the token's file.
#
# A plain `serve()` leaves it False, so a network-only server trusts only the
# token. `desktop.launch` calls `serve_extra(HOST, port)` after any network
# bind, so the logon task gets the same treatment.
TRUST_LOOPBACK = False

LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Colony Dash</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0B0F14">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>
  html { color-scheme: dark; }
  body { margin: 0; min-height: 100dvh; display: grid; place-items: center;
         background: #0B0F14; color: #DEE5EC; padding: 24px;
         font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  form { width: min(320px, 100%); display: flex; flex-direction: column; gap: 12px; }
  h1 { margin: 0; font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
       font-size: 13px; letter-spacing: .16em; text-transform: uppercase; }
  p { margin: 0; color: #6B7885; font-size: 13px; }
  input, button { font: inherit; border-radius: 6px; padding: 11px 12px;
                  border: 1px solid #2A3441; background: #121820; color: inherit; }
  input { font-size: 16px; }          /* under 16px and iOS zooms the page in */
  button { border-color: #0E8C74; color: #34D3AE; cursor: pointer; }
  .bad { color: #E06C5B; font-size: 13px; margin: 0; }
</style>
<form method="post" action="/login">
  <h1>Colony Dash</h1>
  <p>This dashboard is being served off this machine. Paste the access token
     from <code>.env</code>.</p>
  __ERROR__
  <input name="token" type="password" autocomplete="current-password"
         autofocus placeholder="access token">
  <input name="next" type="hidden" value="__NEXT__">
  <button type="submit">unlock</button>
</form>"""


def _login_page(*, error: str = "", next_path: str = "/") -> HTMLResponse:
    html = LOGIN_PAGE.replace(
        "__ERROR__", f'<p class="bad">{error}</p>' if error else "")
    # Escaped even though this server produced the path.
    return HTMLResponse(html.replace("__NEXT__", html_escape(next_path or "/")),
                        headers={"Content-Security-Policy": PAGE_CSP})


@app.middleware("http")
async def gate(request: Request, call_next):
    if not REQUIRE_TOKEN or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    # A peer address, not `X-Forwarded-For`, which the caller controls.
    if TRUST_LOOPBACK and request.client and access.is_loopback(request.client.host):
        return await call_next(request)

    # A pairing code or token in the URL is swapped for the cookie and
    # redirected away, so neither stays in the address bar or history.
    peer = request.client.host if request.client else ""
    pair = request.query_params.get("pair")
    via_url = request.query_params.get("k")
    if request.method == "GET" and (pair or via_url):
        key = access.token() if access.redeem(pair) else via_url
        if key and access.matches(key):
            access.note_arrival(peer, True)
            response = RedirectResponse(_without_key(request), status_code=303)
            _set_cookie(response, key, request)
            return response

    supplied = request.cookies.get(access.COOKIE) or via_url
    ok = access.matches(supplied)

    # Recorded on both paths, so the Phone panel can tell a dropped packet from
    # a wrong token. See `access.note_arrival`.
    access.note_arrival(peer, ok)

    if ok:
        response = await call_next(request)
        if supplied != request.cookies.get(access.COOKIE):
            _set_cookie(response, supplied, request)
        return response

    # Only a navigation (Accept: `text/html`) gets the login page. Anything
    # else gets a status, since login HTML handed to `fetch` or `<script src>`
    # surfaces as a confusing parse error.
    wants_page = ("text/html" in request.headers.get("accept", "")
                  and not request.url.path.startswith("/api/"))
    if wants_page:
        return _login_page(next_path=request.url.path)
    return Response('{"detail":"access token required"}', status_code=401,
                    media_type="application/json")


# Registered after `gate`, so it runs first. A Host that is not this machine is
# DNS rebinding; a write from another Origin is cross-site.
@app.middleware("http")
async def same_host(request: Request, call_next):
    host = request.headers.get("host")
    if not access.host_allowed(host):
        return Response('{"detail":"unknown host"}', status_code=403,
                        media_type="application/json")
    if (request.method not in ("GET", "HEAD", "OPTIONS")
            and not access.origin_matches(request.headers.get("origin"), host)):
        return Response('{"detail":"cross-origin request refused"}',
                        status_code=403, media_type="application/json")
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def _without_key(request: Request) -> str:
    """This request's path and query, minus the `k` and `pair` parameters."""
    rest = [(k, v) for k, v in request.query_params.multi_items() if k not in ("k", "pair")]
    query = urllib.parse.urlencode(rest)
    return request.url.path + (f"?{query}" if query else "")


def _is_https(request: Request) -> bool:
    """Whether the browser reached us over HTTPS, directly or via `tailscale
    serve`. The forwarded header only decides the cookie's Secure flag, so a
    false claim costs nothing.
    """
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https")


def _set_cookie(response: Response, value: str, request: Request) -> None:
    response.set_cookie(
        access.COOKIE, value, max_age=access.COOKIE_MAX_AGE_S,
        httponly=True, samesite="lax", path="/", secure=_is_https(request),
    )


@app.post("/login")
async def login(request: Request) -> Response:
    # Parsed by hand to avoid a `python-multipart` dependency.
    raw = (await request.body()).decode("utf-8", "replace")
    form = urllib.parse.parse_qs(raw, keep_blank_values=True)
    supplied = (form.get("token") or [""])[0]
    next_path = (form.get("next") or ["/"])[0]
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"          # never bounce off this server
    if not access.matches(supplied):
        return _login_page(error="that is not the token", next_path=next_path)

    response = RedirectResponse(next_path, status_code=303)
    _set_cookie(response, supplied, request)
    return response


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the dashboard. Non-loopback binds require an access token.
    `access.check` raises for the unsafe case, so `uvicorn.run` never starts
    an open server.
    """
    global REQUIRE_TOKEN
    REQUIRE_TOKEN = access.check(host)

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")


def serve_extra(host: str, port: int) -> None:
    """Bind a second address in this process, on the running app.

    The phone switch should work now, not at next logon, so the running
    process opens the second socket and the QR code works at once. One app,
    two sockets: no second connection pool, event stream or pulse.

    Raises `access.Unconfigured` before binding if there is no token, and
    `OSError` if the address is taken.
    """
    import uvicorn

    from .desktop import _wait_for_port

    global REQUIRE_TOKEN, TRUST_LOOPBACK
    REQUIRE_TOKEN = access.check(host) or REQUIRE_TOKEN
    TRUST_LOOPBACK = True

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    def _run() -> None:
        try:
            server.run()
        except SystemExit:
            # A failed bind; uvicorn has logged it. Swallowing `SystemExit`
            # avoids a second traceback. `_wait_for_port` decides the outcome.
            pass

    # `Server.run` builds its own event loop, so it needs its own thread.
    thread = threading.Thread(target=_run, daemon=True,
                              name=f"colony-serve-{host}")
    thread.start()

    # A failed bind dies inside the thread, so the socket is checked from
    # outside before reporting success.
    if not _wait_for_port(host, port, timeout_s=8.0):
        raise OSError(f"could not start serving {host}:{port}. "
                      "something else is probably bound to it")
