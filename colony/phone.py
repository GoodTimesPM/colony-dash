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

from . import access, autostart, db, firewall, net, qr

# The same default the server, the shortcut and the logon task all use. It is a
# parameter everywhere rather than a constant because someone running two
# ledgers needs two ports, and a hardcoded one would be the thing that stopped
# them.
DEFAULT_PORT = 8787

ENV_PATH = db.PROJECT_DIR / ".env"


def url(port: int = DEFAULT_PORT) -> str | None:
    """The address to open on the phone, token and all, or None.

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
    return f"http://{address}:{port}/?k={key}"


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


def _reachable(address: str, port: int) -> bool:
    """Whether something is already answering on that address and port."""
    from .desktop import _port_is_free

    try:
        return not _port_is_free(address, port)
    except OSError:
        return False


def state(port: int = DEFAULT_PORT) -> dict:
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
        "url": f"http://{address}:{port}/?k={key}" if address and key else None,
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
    }


def svg(port: int = DEFAULT_PORT) -> str | None:
    """The URL as a QR code, or None when there is no URL to draw."""
    target = url(port)
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
