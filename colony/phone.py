"""Phone access as one switch.

`turn_on()` mints a token if there is none, registers the logon task and
binds the network address. `turn_off()` removes the task. `state()` returns
everything the panel draws.

`.env` also holds the Notion token, so this module never rewrites it. A
token is appended only when none exists, and `rotate()`, behind its own
button, is the one path that replaces it. Turning phone access off leaves
the token in place so a paired phone keeps working when it is turned back
on.
"""

from __future__ import annotations

import os
import time

from . import access, autostart, db, firewall, net, qr, tailscale

DEFAULT_PORT = 8787

ENV_PATH = db.PROJECT_DIR / ".env"

# When this process started. An empty `arrivals` means different things a
# second after a restart and a day after one, so the panel gets both.
SINCE = time.time()


def _link(address: str, port: int, key: str, pairing: bool) -> str:
    """The phone URL. With `pairing`, a one-time code stands in for the token.

    Only the server process can redeem a code, so the CLI, which runs in its
    own process, still prints the token form.
    """
    if pairing:
        return f"http://{address}:{port}/?pair={access.pair_code()}"
    return f"http://{address}:{port}/?k={key}"


def url(port: int = DEFAULT_PORT, pairing: bool = False) -> str | None:
    """The address to open on the phone, or None if there is no reachable
    address or no token. `state()` says which.
    """
    key = access.token()
    if not key:
        return None
    try:
        address, _ = net.auto()
    except net.NoAddress:
        return None
    return _link(address, port, key, pairing)


def _ensure_token() -> tuple[str, bool]:
    """The access token, minting and appending one only if there is none.

    Returns the token and whether this call created it.
    """
    existing = access.token()
    if existing:
        return existing, False

    minted = access.mint()
    # Appended, never rewritten. The newline logic keeps the token off the end
    # of an unterminated last line without adding blank lines.
    body = ""
    try:
        body = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        pass
    lead = "" if (not body or body.endswith("\n")) else "\n"
    with ENV_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{lead}\n# Phone access. Written by the dashboard.\n"
                     f"{access.TOKEN_ENV}={minted}\n")

    # Not set in `os.environ`: the console passes its environment to `claude`,
    # which must not see credentials. `db._env_value` reads the file directly.
    return minted, True


def rotate(port: int = DEFAULT_PORT, pairing: bool = False) -> dict:
    """Mint a new access token, replace it in `.env`, and forget all pairings.

    Every paired device is logged out. The desktop page stays in because
    loopback is trusted by peer address (`server.TRUST_LOOPBACK`).
    """
    fresh = access.mint()
    db.set_env_value(access.TOKEN_ENV, fresh, ENV_PATH,
                     comment="Phone access. Written by the dashboard.")

    # `db._env_value` prefers the live environment, so update it if the old
    # token is there. Never add it: the console's `claude` subprocess inherits
    # `os.environ`.
    if os.environ.get(access.TOKEN_ENV):
        os.environ[access.TOKEN_ENV] = fresh

    access.forget_pairings()
    return {**state(port, pairing=pairing), "minted": True, "rotated": True}


def _reachable(address: str, port: int) -> bool:
    """Whether something is already answering on that address and port."""
    from .desktop import _port_is_free

    try:
        return not _port_is_free(address, port)
    except OSError:
        return False


def _neighbourhood(address: str | None) -> str:
    """The first three octets of an IPv4 address. A home router hands out a
    /24, and the panel phrases the comparison as a likelihood.
    """
    parts = (address or "").split(".")
    return ".".join(parts[:3]) + "." if len(parts) == 4 else ""


def state(port: int = DEFAULT_PORT, pairing: bool = False) -> dict:
    """Everything the panel draws, in one call. Never raises; no network is a
    normal state to report.
    """
    task = autostart.describe()
    key = access.token()

    address = kind = None
    problem = ""
    try:
        address, kind = net.auto()
    except net.NoAddress as exc:
        problem = str(exc)

    return {
        "on": bool(task),
        "port": port,
        "token_set": bool(key),
        "address": address,
        "kind": kind,
        "advice": net.advice(kind) if kind else "",
        "url": _link(address, port, key, pairing) if address and key else None,
        # A task Task Scheduler kills after three days is not really on.
        "unlimited": autostart.unlimited(task) if task else True,
        "state": task.get("state", "") if task else "",
        "last_run": task.get("last_run", "") if task else "",
        "serving": bool(address) and _reachable(address, port),
        # The probe above runs on this machine, which the firewall never
        # filters, so the firewall is checked separately.
        "firewall": firewall.state(port),
        "firewall_fix": firewall.rule_command(port),
        "problem": problem,
        # Whether anything off this machine has reached the server.
        "arrivals": access.arrivals(),
        "since": SINCE,
        "neighbourhood": _neighbourhood(address),
        "tailscale": tailscale.state(),
    }


def svg(port: int = DEFAULT_PORT, pairing: bool = False) -> str | None:
    """The URL as a QR code, or None when there is no URL to draw."""
    target = url(port, pairing)
    return qr.svg(target, ec="M") if target else None


def turn_on(port: int = DEFAULT_PORT) -> dict:
    """Mint a token if needed, register the logon task, start it now.

    Raises `net.NoAddress` before writing anything when there is nothing
    safe to bind.
    """
    address, kind = net.auto()               # raises before anything is written
    _, minted = _ensure_token()
    autostart.install(host="auto", port=port)
    autostart.start_now()

    # The task's process stands down when this one already holds the port, so
    # bind the network address here too. A no-op if the task got there first.
    served = _reachable(address, port)
    if not served:
        try:
            from . import server
            server.serve_extra(address, port)
            served = True
        except OSError:
            # Something else holds the address. The panel reports `serving:
            # false`.
            served = False

    return {**state(port), "minted": minted, "address": address, "kind": kind,
            "serving": served}


def turn_off(port: int = DEFAULT_PORT) -> dict:
    """Remove the logon task. The token and any paired phone survive."""
    autostart.remove()
    return {**state(port), "minted": False}
