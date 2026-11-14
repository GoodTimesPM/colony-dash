"""Which address on this machine a phone can reach, resolved at every launch so
the logon task never holds a stale literal.

`--host auto` accepts a tailnet address (`100.64.0.0/10`), where the token is
a second lock, and nothing else. A LAN address must be passed by hand. If there
is no tailnet, `auto()` raises rather than guess.

`0.0.0.0` must be spelled out; "every interface" is a decision, not a
discovery.
"""

from __future__ import annotations

import ipaddress
import socket
import time

TAILNET = ipaddress.ip_network("100.64.0.0/10")

# Not `.is_private`, which also counts documentation and benchmarking ranges.
RFC1918 = tuple(ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class NoAddress(RuntimeError):
    """Raised when nothing on this machine is a safe thing to bind."""


def _candidates() -> list[str]:
    """Every IPv4 address this machine answers to, best effort.

    `getaddrinfo` often misses the Tailscale interface; the UDP-connect
    trick finds only the default route. Together they cover it. No packet is
    sent.
    """
    found: list[str] = []

    def add(value: str) -> None:
        if value and value not in found:
            found.append(value)

    try:
        for *_, sockaddr in socket.getaddrinfo(socket.gethostname(), None,
                                               socket.AF_INET):
            add(sockaddr[0])
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("10.255.255.255", 1))
            add(s.getsockname()[0])
    except OSError:
        pass

    return found


def tailnet() -> str | None:
    """This machine's tailnet address, or None if it is not on one."""
    for value in _candidates():
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.version == 4 and address in TAILNET:
            return value
    return None


def lan() -> str | None:
    """This machine's RFC 1918 address, or None."""
    for value in _candidates():
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.version == 4 and any(address in net for net in RFC1918):
            return value
    return None


# Cached for a minute: the console polls a route that calls this every 1.5s,
# and the answer changes only when an interface does.
_LOCAL_CACHE: tuple[float, frozenset[str]] | None = None
_LOCAL_TTL_S = 60.0


def local_addresses() -> frozenset[str]:
    """Every address this machine answers to, cached for a minute."""
    global _LOCAL_CACHE
    now = time.monotonic()
    if _LOCAL_CACHE is not None and now - _LOCAL_CACHE[0] < _LOCAL_TTL_S:
        return _LOCAL_CACHE[1]
    found = frozenset(_candidates())
    _LOCAL_CACHE = (now, found)
    return found


def is_this_machine(host: str) -> bool:
    """True when a peer address belongs to this machine.

    A connection from here to our own tailnet or LAN address arrives with
    that address as its source, not 127.0.0.1. A remote machine cannot spoof
    it and still complete the handshake. Unparseable addresses are not this
    machine.
    """
    host = (host or "").strip().strip("[]")
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        return False
    return host in local_addresses()


def auto(wait_s: float = 0.0) -> tuple[str, str]:
    """The tailnet address to bind: `("100.1.2.3", "tailnet")`.

    Tailnet only. A LAN address used to be the fallback, and at logon, when
    Tailscale had not come up yet, that fallback won: the server sat on the
    home wifi over plain HTTP with the token crossing it unencrypted. A LAN
    bind now has to be asked for by address with `--host`.

    `wait_s` polls for Tailscale that long before giving up, for the logon
    task that starts before Tailscale does. Raises `NoAddress` after that.
    """
    deadline = time.monotonic() + wait_s
    while True:
        address = tailnet()
        if address:
            return address, "tailnet"
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    raise NoAddress(
        "no tailnet address on this machine, so there is nothing safe to "
        "bind for the phone.\n\n"
        "Start Tailscale and run this again. To use the home wifi instead, "
        "pass --host with this machine's LAN address; the token then crosses "
        "the wifi unencrypted."
    )


def resolve(host: str | None, wait_s: float = 0.0) -> tuple[str, str]:
    """Turn `--host` into an address and a label. Anything but `auto` passes
    through; `access.check` still decides whether the bind is allowed.
    """
    if host is None:
        return "127.0.0.1", "loopback"
    if host.strip().lower() == "auto":
        return auto(wait_s)
    host = host.strip()
    if host in ("0.0.0.0", "::"):
        return host, "every interface"
    return host, "given"


def advice(kind: str) -> str:
    """One line about what the chosen network means. Empty when it means nothing."""
    # Silent for a tailnet bind: callers print this as a warning, and a good
    # result behind a warning sign teaches people to ignore it.
    if kind == "lan":
        return ("this address only exists inside your building. The phone can "
                "reach it on the same wifi and nowhere else. Not on cellular, "
                "not from work. It is also your local network rather than a "
                "private one, so the token is the only lock. Tailscale fixes "
                "both.")
    if kind == "every interface":
        return ("0.0.0.0 is every network this machine ever joins, including "
                "ones you did not choose. Prefer a tailnet address.")
    return ""
