"""The console routes. A real shell, so every write answers only from this
machine unless the PO has turned on 'answer from anywhere'."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Request

from .. import console as console_mod, control, db, net
from .common import _conn, _guard, _rw

router = APIRouter()

# -- the console ---------------------------------------------------------------
# These routes are the PO's own terminal; `console.py` explains why they may do
# what the rest of this server prevents.
#
# They skip `_act`: `console.send` starts a thread that wants the write lock
# `_act` would be holding. The ledger connection is autocommit.


# Whether the console answers from anywhere or only from this machine. Read at
# import, and changed only by `act_console_remote`, which writes `.env` and a
# ledger note. Not re-read per request, so an open editor cannot move the
# boundary silently.
CONSOLE_REMOTE = (db._env_value("COLONY_CONSOLE_REMOTE") or "").strip().lower() \
    in {"1", "true", "yes", "on"}


def _console_conn() -> sqlite3.Connection:
    return _rw()


def _at_the_desk(request: Request) -> bool:
    """True when the request came from the machine the colony runs on.

    Not the same as loopback: with `--host auto`, the desktop window
    connects to this machine's own tailnet or LAN address and arrives with
    that as its peer. See `net.is_this_machine`. The empty peer counts as
    not this machine, the opposite of `access.is_loopback("")`, which wants
    a token prompt there.
    """
    peer = request.client.host if request.client else ""
    return bool(peer) and net.is_this_machine(peer)


def _desk_only(request: Request) -> None:
    """Refuse a console write that did not come from this machine.

    A stolen access token elsewhere buys reading the board and pressing
    approve. Here it would buy a shell (`claude` with no worktree, tool
    limits or PO gate) on the machine holding `.env`. The token crosses the
    LAN over plain HTTP, so the shell is scoped to the peer address, like
    token rotation.

    `act_console_remote` lifts this, and only from the desk, so a stolen
    token cannot turn it off.
    """
    if CONSOLE_REMOTE:
        return
    if not _at_the_desk(request):
        raise HTTPException(403,
            "the console is a real shell on the machine running the colony, so "
            "it only answers from that machine. A stolen access token should "
            "not be worth a command prompt. Everything else here works from your "
            "phone. To allow it from here, turn on 'answer from anywhere' in the "
            "console on the desktop.")


@router.post("/api/act/console-remote")
def act_console_remote(request: Request, body: dict = Body(...),
                       x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Move the console's address boundary, and write the move to `.env`.

      * **Turning it on is desk-only**, since loosening is what the boundary
        prevents.
      * **Turning it off works from anywhere**, since tightening is always safe.

    The rule for any future switch: tighten from anywhere, loosen only from
    the desk. `db.set_env_value` touches only the `COLONY_CONSOLE_REMOTE=`
    line; the in-process value changes too, so no restart is needed.
    """
    _guard(x_colony)
    on = bool(body.get("on"))
    if on and not _at_the_desk(request):
        raise HTTPException(403,
            "opening the console to the network can only be done from the "
            "machine itself. Otherwise a stolen access token could switch off "
            "the check that is keeping it out. Turn it on from the dashboard on "
            "the desktop. Turning it back off works from anywhere.")

    global CONSOLE_REMOTE
    try:
        db.set_env_value("COLONY_CONSOLE_REMOTE", "1" if on else "0",
                         comment="Console reachable from the network. "
                                 "See README, 'How safe is this, honestly'.")
    except OSError as exc:
        # Nothing changed: the file write is atomic and it failed, so leaving
        # the live value alone keeps the process and the file agreeing.
        raise HTTPException(500, f"could not write .env, so the console is "
                                 f"unchanged: {exc}")
    CONSOLE_REMOTE = on

    conn = _rw()
    try:
        control._record(conn, "note", "colony", None,
                        "console opened to the network" if on
                        else "console restricted to this machine")
    finally:
        conn.close()
    return {"ok": True, "remote": on}


@router.get("/api/console")
def api_console(request: Request) -> dict[str, Any]:
    conn = _conn()
    try:
        desk = _at_the_desk(request)
        # Reading is allowed wherever the token is; only sending is scoped.
        # `writable` tells the panel to show an explanation instead of an
        # input.
        return {**console_mod.state(conn),
                "writable": CONSOLE_REMOTE or desk,
                "remote": CONSOLE_REMOTE,
                "desk": desk}
    finally:
        conn.close()


@router.post("/api/console/send")
def console_send(request: Request, body: dict = Body(...),
                 x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    _desk_only(request)
    conn = _console_conn()
    try:
        return {"ok": True, **console_mod.send(conn, str(body.get("text") or ""))}
    except console_mod.Busy as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@router.post("/api/console/clear")
def console_clear(request: Request, body: dict = Body(default={}),
                  x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    _desk_only(request)
    conn = _console_conn()
    try:
        return {"ok": True, **console_mod.clear(conn)}
    except console_mod.Busy as exc:
        raise HTTPException(409, str(exc))
    finally:
        conn.close()


@router.post("/api/console/options")
def console_options(request: Request, body: dict = Body(...),
                    x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    _desk_only(request)
    conn = _console_conn()
    try:
        return {"ok": True, **console_mod.set_options(
            conn, body.get("model") or None, body.get("effort") or None)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@router.post("/api/console/cwd")
def console_cwd(request: Request, body: dict = Body(...),
                x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    _desk_only(request)
    conn = _console_conn()
    try:
        return {"ok": True, **console_mod.set_cwd(conn, body.get("path") or None)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()
