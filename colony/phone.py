"""Phone access as one switch, instead of four things you do in order.

Everything this file needs already existed. `net.auto()` finds an address a
phone can reach, `access.mint()` makes a token, `autostart.install()` registers
the logon task, and `qr.svg()` draws the result. What did not exist was a way to
get all four without leaving the dashboard: the documented path was mint a
token, open `.env` in an editor, paste it, save, then run `py -m colony
autostart` in a terminal. Five steps, one of which is "edit a credential file
correctly", and all five happen at the desk — which is the machine you are
sitting at *because* you are about to walk away from it.

So: one button. `turn_on()` does the four things, `turn_off()` undoes the one
that matters, and `state()` answers "is this on, and what is the address" well
enough to draw the panel with no other call.

Two rules this module will not bend on, both about the credential file:

  * **It writes a token only when there is not one.** A dashboard request that
    can rewrite `.env` is a dashboard request that can lock a paired phone out
    of the ledger by accident, and the accident looks like the feature working.
    If a token is already configured, `turn_on()` uses it and leaves the file
    untouched.
  * **It appends one line and rewrites nothing.** No parse, no reformat, no
    round trip through a dict. `.env` here holds a Notion token beside the
    access token, and a file that is only ever appended to cannot lose the
    line above.

`rotate()` is the deliberate exception to the first rule and honours the second.
Changing the token by accident is the failure the rule guards against; changing
it on purpose is a thing a person needs to be able to do from the panel, because
the alternative is opening a credential file in an editor. It rewrites the one
line it is named after, leaves every other byte in place, and moves the file
into position atomically -- and it is behind its own button, so it is never
something the switch does on its way to somewhere else.

It does *show* the token, inside the URL and the QR code. That reads like it
contradicts the above and does not: the only callers are the CLI, which is a
terminal on the desk where `.env` already is, and one route behind the
dashboard's own guard, which is either loopback or a client that already holds
the token. Neither learns anything it could not read directly. What it will not
do is *change* the value out from under a device that is already using it.

Turning it off removes the logon task and stops there. The token stays in
`.env`, because deleting it would log out a phone that is paired and working,
and "off" here means "the server does not come up on the network any more",
not "forget everything".
"""

from __future__ import annotations

import os
import time

from . import access, autostart, db, firewall, net, qr, tailscale

# The same default the server, the shortcut and the logon task all use. It is a
# parameter everywhere rather than a constant because someone running two
# ledgers needs two ports, and a hardcoded one would be the thing that stopped
# them.
DEFAULT_PORT = 8787

ENV_PATH = db.PROJECT_DIR / ".env"

# When this process came up. `arrivals` is empty both when nothing has ever
# connected and when the server restarted a second ago, and those two readings
# call for opposite reactions -- so the panel is given the time to say which.
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
    """The address to open on the phone, or None.

    None means one of the two halves is missing -- no reachable address, or no
    token -- and `state()` says which.
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
    # Appended, never rewritten -- see the module docstring. The leading newline
    # is conditional because a file that already ends in one would otherwise
    # grow a blank line every time this ran, and a file that does *not* end in
    # one would otherwise get the token glued to the end of the last value.
    body = ""
    try:
        body = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        pass
    lead = "" if (not body or body.endswith("\n")) else "\n"
    with ENV_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{lead}\n# Phone access. Written by the dashboard.\n"
                     f"{access.TOKEN_ENV}={minted}\n")

    # Deliberately not also set in `os.environ`. `db._env_value` reads the file
    # on every call, so this is visible immediately without it -- and the
    # console hands its environment to a `claude` subprocess that is not allowed
    # to see credentials. See `db._env_value`.
    return minted, True


def rotate(port: int = DEFAULT_PORT, pairing: bool = False) -> dict:
    """Mint a new access token, replace the old one in `.env`, redraw the QR.

    This is the one write in this module that is not an append, and the rule it
    appears to break -- never change a token out from under a device that is
    using it -- is the rule it exists to serve. Rotating *is* the act of logging
    every paired device out at once, and it is what you want the moment a token
    has been read over your shoulder, pasted into a chat window, or carried out
    of the house on a phone that is not coming back. So it is its own function
    behind its own button, and never a side effect of the switch: `turn_on()`
    still uses an existing token exactly as it finds it.

    The rewrite itself is `db.set_env_value`, which only ever touches lines
    starting `COLONY_ACCESS_TOKEN=` and moves the result into place
    atomically -- both of which matter because the file it is pointed at also
    holds the Notion token, and that is the only copy.

    The desktop dashboard survives this and the phone does not, which is the
    whole point: loopback on a dual-bound process is trusted by peer address
    rather than by token (see `server.TRUST_LOOPBACK`), so the page the button
    was pressed in stays logged in while every network client is turned away.
    """
    fresh = access.mint()
    db.set_env_value(access.TOKEN_ENV, fresh, ENV_PATH,
                     comment="Phone access. Written by the dashboard.")

    # `db._env_value` prefers the live environment over the file, so a token
    # exported into this process would otherwise outlive the rotation and the
    # new QR code would be for a token nothing accepts. Only updated when it was
    # already there: putting it in `os.environ` that was not would hand it to
    # the `claude` subprocess the console spawns, which is not allowed to read
    # credentials. See `db._env_value`.
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
    """The first three octets of an IPv4 address, as a prefix to compare against.

    Deliberately not a subnet mask. Reading the real prefix length means asking
    Windows, which is a PowerShell call this panel does not need, and getting it
    wrong in the other direction is worse: telling someone their phone is on the
    wrong network when it is not sends them to reconfigure a router that was
    fine. Three octets is what a home router hands out and the panel says so as
    a likelihood rather than a rule.
    """
    parts = (address or "").split(".")
    return ".".join(parts[:3]) + "." if len(parts) == 4 else ""


def state(port: int = DEFAULT_PORT, pairing: bool = False) -> dict:
    """Everything the panel draws, in one call.

    Nothing here raises. A machine with no tailnet and no LAN is a normal
    machine on a plane, and the panel's job in that case is to say so rather
    than to be an error.
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
        # A task that Task Scheduler will kill after three days is installed but
        # not doing the job, and the panel should not call that "on" without
        # saying so. See `autostart.unlimited`.
        "unlimited": autostart.unlimited(task) if task else True,
        "state": task.get("state", "") if task else "",
        "last_run": task.get("last_run", "") if task else "",
        "serving": bool(address) and _reachable(address, port),
        # The half `serving` cannot see. That probe runs on this machine, and a
        # packet from this machine never meets the firewall -- so a port that
        # answers here can still be a port the phone's request dies in front of,
        # with no error at either end. See `firewall.py`.
        "firewall": firewall.state(port),
        "firewall_fix": firewall.rule_command(port),
        "problem": problem,
        # Every other field here is a fact about this machine, and this machine
        # being healthy is exactly the state a phone that cannot connect leaves
        # it in. This one is a fact about the phone: has anything off this
        # machine reached the server at all. See `access.note_arrival`.
        "arrivals": access.arrivals(),
        "since": SINCE,
        "neighbourhood": _neighbourhood(address),
        # Which of the two networks the address above is on decides whether this
        # feature works in the car park, and the address alone does not say so
        # to anyone who does not already know what 100.x means. See
        # `tailscale.py` for why that distinction is worth a whole panel.
        "tailscale": tailscale.state(),
    }


def svg(port: int = DEFAULT_PORT, pairing: bool = False) -> str | None:
    """The URL as a QR code, or None when there is no URL to draw."""
    target = url(port, pairing)
    return qr.svg(target, ec="M") if target else None


def turn_on(port: int = DEFAULT_PORT) -> dict:
    """Mint a token if needed, register the logon task, start it now.

    Raises `net.NoAddress` when there is nothing safe to bind, which is the one
    failure worth stopping for: everything else this does would succeed and
    produce a switch that is on and unreachable.
    """
    address, kind = net.auto()               # raises before anything is written
    _, minted = _ensure_token()
    autostart.install(host="auto", port=port)
    # Installing without starting means phone access begins at the next logon,
    # which is not what pressing a button means.
    autostart.start_now()

    # And the task alone is not enough either. It launches a *second* process,
    # which finds this one already holding the port and stands down -- correct
    # behaviour, and it leaves the network address unbound until the next time
    # this process is not running. So the process that was asked bind it itself.
    # Harmless when the task did win the race: the address is already up, the
    # probe below says so, and nothing is started.
    served = _reachable(address, port)
    if not served:
        try:
            from . import server
            server.serve_extra(address, port)
            served = True
        except OSError:
            # Something else has the address. The task is installed and will try
            # again at the next logon; the panel reports `serving: false` and the
            # switch does not claim to be working.
            served = False

    return {**state(port), "minted": minted, "address": address, "kind": kind,
            "serving": served}


def turn_off(port: int = DEFAULT_PORT) -> dict:
    """Remove the logon task. The token and any paired phone survive."""
    autostart.remove()
    return {**state(port), "minted": False}
