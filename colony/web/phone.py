"""Phone access: the switch, the token, and Tailscale."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Request

from .. import access, net, phone as phone_mod, qr, tailscale
from .common import _guard, _num

router = APIRouter()

@router.get("/api/phone")
def api_phone(port: int = 8787) -> dict[str, Any]:
    """Whether phone access is on, where, and the QR code. Not in `/api/state`,
    because it shells out to Task Scheduler and probes a socket; the panel
    asks when it opens.
    """
    out = phone_mod.state(port, pairing=True)
    out["svg"] = phone_mod.svg(port, pairing=True)
    return out


@router.post("/api/act/phone")
def act_phone(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Turn phone access on or off. Not `_act`: registering the task takes
    seconds of PowerShell and would hold the write lock that long.
    """
    _guard(x_colony)
    port = _num(body, "port", int, 8787)
    try:
        if body.get("on"):
            return {"ok": True, **phone_mod.turn_on(port)}
        return {"ok": True, **phone_mod.turn_off(port)}
    except net.NoAddress as exc:
        # 409 rather than 500: nothing is broken, this machine is just not on a
        # network worth binding, and the message says what to do about it.
        raise HTTPException(409, str(exc))
    except access.Unconfigured as exc:
        raise HTTPException(409, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"could not set up phone access: {exc}")


@router.post("/api/act/phone-token")
def act_phone_token(request: Request, body: dict = Body(...),
                    x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Rotate the access token, logging out every paired device.

    Its own route so a destructive act cannot ride along as an extra key on
    the switch. Returns the new QR code, since scanning it is the next step.
    """
    _guard(x_colony)

    # Loopback only, enforced here and not just in the panel. From the network,
    # a rotate logs its caller out mid-request and every later call is a 401.
    # The desktop window is exempt, as it is from the token.
    peer = request.client.host if request.client else ""
    if not access.is_loopback(peer):
        raise HTTPException(403, "rotating the token is only allowed from the "
                                 "machine itself. Doing it from here would log "
                                 "this device out mid-request. Use the dashboard "
                                 "on the desktop, or `py -m colony phone --rotate`.")

    port = _num(body, "port", int, 8787)
    try:
        out = phone_mod.rotate(port, pairing=True)
    except OSError as exc:
        # The write is atomic, so `.env` and the old token are unchanged.
        raise HTTPException(500, f"could not write .env, so the token is "
                                 f"unchanged and paired devices still work: {exc}")
    out["svg"] = phone_mod.svg(port, pairing=True)
    return {"ok": True, **out}


@router.post("/api/act/tailscale")
def act_tailscale(request: Request, body: dict = Body(...),
                  x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Run the Tailscale installer, or start a sign-in. Loopback only.

    Both act on the machine, and launching an executable behind a UAC prompt
    is not something a network request may do, token or not. `do` is
    `install` or `login`; `login` returns a URL the panel draws as a QR
    code, so the phone can sign itself in.
    """
    _guard(x_colony)

    peer = request.client.host if request.client else ""
    if not access.is_loopback(peer):
        raise HTTPException(403, "setting up Tailscale is only allowed from the "
                                 "machine itself. It runs an installer and asks "
                                 "for administrator. Open the dashboard on the "
                                 "desktop, or run `py -m colony tailscale --install`.")

    what = str(body.get("do") or "")
    try:
        if what == "install":
            return {"ok": True, **tailscale.install()}
        if what == "login":
            out = tailscale.login()
            # Drawn as a code as well as a link, because the device that most
            # needs to be signed in to the tailnet is the one holding a camera.
            out["svg"] = qr.svg(out["url"], ec="M") if out.get("url") else None
            return {"ok": True, **out}
    except tailscale.NotInstalled as exc:
        raise HTTPException(409, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"could not start Tailscale: {exc}")
    raise HTTPException(400, "do must be 'install' or 'login'")
