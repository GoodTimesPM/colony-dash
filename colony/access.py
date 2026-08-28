"""Who may reach the dashboard, once it stops being reachable only from here.

For its whole life this server bound `127.0.0.1` and that was the entire access
control story: the write door needed an `X-Colony` header, and the reason a
header was enough is written down in `server.py` — a form on another web page
can POST across origins but cannot set a custom header, and nothing off this
machine could open a socket to it in the first place.

The second half of that sentence is what a phone breaks. The moment the server
binds anything but loopback, "nothing off this machine" stops being true, and
the header stops being an access control and goes back to being what it always
was: CSRF protection. Both are still wanted; they answer different questions.

So there is exactly one new rule, and it fails closed:

    binding a non-loopback address requires COLONY_ACCESS_TOKEN to be set.

Not a warning, not a default that can be left in place — the server refuses to
start. A dashboard that quietly served the ledger, the project tree, every run
transcript and a HALT button to whoever else was on the coffee shop's wifi is
the one failure here that cannot be walked back.

The token is a bearer secret and is treated as one: compared in constant time,
carried in an HttpOnly cookie so no script on the page can read it back out, and
never logged. It is deliberately not a password and there are deliberately no
accounts — one operator, one secret, and a rotation is a new line in `.env`.

**What this is not.** This is not multi-tenancy. Every session that gets past
this file is the same PO looking at the same ledger on the same machine; the
token says "you are Jordan on his phone", not "you are some user". Real accounts
mean a per-user ledger, per-user projects on disk and a per-user `claude` login,
which is a different program (see PROJECT.md).

The intended transport is a tailnet — Tailscale, WireGuard, whatever puts the
phone and the desktop on one private network — not a port forwarded from a
router. On a tailnet the network is already doing the hard half of the work and
this token is the second lock; on the open internet it would be the only one,
in front of a page that can spend money.
"""

from __future__ import annotations

import ipaddress
import secrets

from . import db

TOKEN_ENV = "COLONY_ACCESS_TOKEN"

# HttpOnly, so `document.cookie` cannot read it and an XSS on the page cannot
# post it somewhere. SameSite=Lax, so another site cannot ride it. No `Secure`:
# a tailnet address is plain http, and setting Secure would mean the cookie is
# never stored and the login loops forever.
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
    """True for the addresses that mean 'this machine and nowhere else'.

    `localhost` is included by name because that is how people type it, and
    anything that will not parse as an address is treated as *not* loopback —
    the unknown case has to fail towards asking for a token.
    """
    host = (host or "").strip().strip("[]")
    if host.lower() in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def matches(supplied: str | None) -> bool:
    """Constant-time comparison against the configured token.

    False when nothing is configured, which is the safe answer: the only code
    path that reaches this has already decided a token is required.
    """
    real = token()
    if not real or not supplied:
        return False
    return secrets.compare_digest(supplied, real)


class Unconfigured(RuntimeError):
    """Raised when a non-loopback bind is asked for with no token set."""


def check(host: str) -> bool:
    """Decide whether this bind needs a token, refusing the unsafe combination.

    Returns True when the server should enforce the token, False when it is
    loopback-only and nothing changes. Raises `Unconfigured` rather than
    starting an open server.
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
            "can — this token is a second lock, not the only one."
        )
    return True
