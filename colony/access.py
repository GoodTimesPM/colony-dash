"""Who may reach the dashboard when it binds more than loopback.

On loopback the `X-Colony` header is enough (see `server.py`). Binding any
other address requires COLONY_ACCESS_TOKEN, and the server refuses to start
without one: an open dashboard on shared wifi would hand out the ledger, the
project tree and a HALT button.

The token is one bearer secret for one operator. It is compared in constant
time, carried in an HttpOnly cookie and never logged. There are no accounts;
multi-tenancy is a separate program (see docs/history/build-log.md §10.19). The intended transport
is a tailnet, where this token is the second lock rather than the only one.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

from . import db

TOKEN_ENV = "COLONY_ACCESS_TOKEN"

# HttpOnly so page script cannot read it, SameSite=Lax against cross-site use.
# No `Secure`: tailnet addresses are plain http and the cookie would never
# stick.
COOKIE = "colony_key"
COOKIE_MAX_AGE_S = 60 * 60 * 24 * 90


def token() -> str | None:
    """The configured access token, or None if there is not one."""
    value = db._env_value(TOKEN_ENV)
    return value.strip() if value and value.strip() else None


def mint() -> str:
    """A new token worth pasting into `.env`. 32 bytes, url-safe."""
    return secrets.token_urlsafe(32)


def is_loopback(host: str) -> bool:
    """True for addresses that mean 'this machine only'. Anything unparseable
    is treated as not loopback, so the unknown case asks for a token.
    """
    host = (host or "").strip().strip("[]")
    if host.lower() in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


ALLOWED_HOSTS_ENV = "COLONY_ALLOWED_HOSTS"


def _hostname(value: str) -> str:
    """The name part of a Host header or an origin's netloc, lowercased."""
    try:
        return (urlsplit("//" + value.strip()).hostname or "").rstrip(".")
    except ValueError:
        return ""


def host_allowed(host_header: str | None) -> bool:
    """True when the Host header names this machine.

    Guards against DNS rebinding. IP literals cannot be rebound; names must
    be localhost, this machine's name, a `ts.net` MagicDNS name or listed in
    COLONY_ALLOWED_HOSTS.
    """
    name = _hostname(host_header or "")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or name.endswith(".ts.net"):
        return True
    own = socket.gethostname().lower()
    if name == own or name.split(".", 1)[0] == own:
        return True
    extra = db._env_value(ALLOWED_HOSTS_ENV) or ""
    return name in {h.strip().lower() for h in extra.split(",") if h.strip()}


def origin_matches(origin: str | None, host_header: str | None) -> bool:
    """True when a request has no Origin, or one naming the same host.
    Non-browser clients send no Origin and the Host check has already judged
    them.
    """
    if origin is None:
        return True
    if origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == (host_header or "").strip().lower()


def matches(supplied: str | None) -> bool:
    """Constant-time comparison against the configured token. False when none
    is set.
    """
    real = token()
    if not real or not supplied:
        return False
    return secrets.compare_digest(supplied, real)


# One-time pairing codes for the Phone panel's QR, so the token never lands in
# a URL or browser history. In-process only; a restart voids them.
PAIR_TTL_S = 30 * 60
_PAIR_LOCK = threading.Lock()
_PAIRS: dict[str, float] = {}


def pair_code() -> str:
    """The live pairing code, minting one when none is left unexpired."""
    now = time.time()
    with _PAIR_LOCK:
        for code, expires in list(_PAIRS.items()):
            if expires <= now:
                del _PAIRS[code]
        if _PAIRS:
            return next(iter(_PAIRS))
        code = secrets.token_urlsafe(12)
        _PAIRS[code] = now + PAIR_TTL_S
        return code


def redeem(code: str | None) -> bool:
    """Spend a pairing code. True once per code, and only before it expires."""
    if not code:
        return False
    with _PAIR_LOCK:
        expires = _PAIRS.pop(code, None)
    return expires is not None and expires > time.time()


def forget_pairings() -> None:
    """Void every outstanding code. Called when the token rotates."""
    with _PAIR_LOCK:
        _PAIRS.clear()


class Unconfigured(RuntimeError):
    """Raised when a non-loopback bind is asked for with no token set."""


def check(host: str) -> bool:
    """Whether this bind needs a token. True to enforce, False for loopback.
    Raises `Unconfigured` rather than starting an open server.
    """
    if is_loopback(host):
        return False
    if not token():
        raise Unconfigured(
            f"binding {host} would put the dashboard on the network, and "
            f"{TOKEN_ENV} is not set.\n\n"
            f"Add this to colony-dash/.env and try again:\n\n"
            f"    {TOKEN_ENV}={mint()}\n\n"
            "Then open the dashboard once from the phone and paste the same "
            "value in. Serve on a tailnet address rather than 0.0.0.0 if you "
            "can. This token is a second lock, not the only one."
        )
    return True


# ── who has actually arrived ──────────────────────────────────────────────────
# Off-machine requests that reached the gate, since this server started. It
# splits "phone shows a blank tab" in two: none arrived means a network problem
# (subnet, client isolation, a phone VPN); arrived and refused means a bad
# token. Kept in memory so the hot path never writes to the ledger.

_ARRIVALS: dict[str, dict] = {}
ARRIVALS_MAX = 8


def note_arrival(host: str, accepted: bool) -> None:
    """Record an off-machine request. Keyed by address; loopback is skipped."""
    import time

    if not host or is_loopback(host):
        return
    seen = _ARRIVALS.get(host)
    if seen is None:
        # Evict the least recently seen. Eight covers a few devices without
        # being a log.
        if len(_ARRIVALS) >= ARRIVALS_MAX:
            oldest = min(_ARRIVALS, key=lambda k: _ARRIVALS[k]["at"])
            _ARRIVALS.pop(oldest, None)
        seen = _ARRIVALS[host] = {"host": host, "first": time.time(),
                                  "at": 0.0, "hits": 0, "accepted": 0,
                                  "refused": 0}
    seen["at"] = time.time()
    seen["hits"] += 1
    seen["accepted" if accepted else "refused"] += 1
    seen["ok"] = accepted


def arrivals() -> list[dict]:
    """Every device off this machine that has reached the gate, newest first."""
    return sorted(_ARRIVALS.values(), key=lambda r: r["at"], reverse=True)
