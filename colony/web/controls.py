"""Colony-wide controls: halt, allowance, pulses, hiring, the build stamp and
quit, second opinions, scope and secrets, dispatch, and skills."""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Request

from .. import access, control, desktop as desktop_mod
from .common import BUILD_STAMP, STARTED_AT, _conn, _guard, _need, _num, _rw, one
from .state import _act

router = APIRouter()

@router.post("/api/act/halt")
def act_halt(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.halt, bool(_need(body, "on")), str(body.get("reason") or ""))


@router.post("/api/act/allowance")
def act_allowance(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    # A step off the baseline, or a typed number.
    if "allowance" in body:
        return _act(control.set_allowance_pct, _num(body, "allowance", float))
    return _act(control.set_allowance, _num(body, "boost", float))


@router.post("/api/act/pulse")
def act_pulse(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Beat now. Not `_act`: a write transaction held across a minutes-long
    beat would block the pulse's own `BEGIN`.
    """
    _guard(x_colony)
    conn = _rw()
    try:
        return {"ok": True, **control.force_pulse(conn, allow_wake=bool(body.get("wake", True)))}
    except control.Refused as exc:
        raise HTTPException(409, str(exc))
    finally:
        conn.close()


@router.post("/api/act/hire")
def act_hire(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(
        control.hire,
        roster_slug=body.get("roster_slug") or None,
        role=str(_need(body, "role")).strip().lower().replace(" ", "-"),
        project=(body.get("project") or None),
        model=str(body.get("model") or "claude-sonnet-5"),
        write_capable=bool(body.get("write_capable")),
        max_tokens_run=_num(body, "max_tokens_run", int, 400000),
        notes=body.get("notes") or None,
    )


@router.get("/api/build")
def api_build(request: Request) -> dict[str, Any]:
    """Which build is answering, and from which process. Read by a second
    launch deciding whether to attach to the dashboard on the port.

    Behind the token gate, not the public allowlist. That costs nothing: any
    non-loopback bind goes through `desktop.launch`, which also binds
    loopback and sets `TRUST_LOOPBACK`, so the asking launch always passes
    the gate.
    """
    out: dict[str, Any] = {"stamp": BUILD_STAMP}
    peer = request.client.host if request.client else ""
    if access.is_loopback(peer):
        out.update(pid=os.getpid(), started_at=STARTED_AT,
                   on_disk=desktop_mod.stamp())
    return out


@router.post("/api/act/quit")
def act_quit(request: Request, x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Stop this dashboard so a newer one can have the port.

    Loopback only, since a phone that pressed it could not bring the
    dashboard back. Refused while an agent run is open, which would leave a
    worktree and a half-written ticket.
    """
    _guard(x_colony)
    peer = request.client.host if request.client else ""
    if not access.is_loopback(peer):
        raise HTTPException(403, "stopping the dashboard is only allowed from the "
                                 "machine running it. From here there would be "
                                 "nothing left to press to start it again.")

    conn = _conn()
    try:
        busy = one(conn, "SELECT COUNT(*) AS n FROM runs WHERE ended_at IS NULL")["n"]
    finally:
        conn.close()
    if busy:
        raise HTTPException(409,
                            f"{busy} agent run{'s' if busy > 1 else ''} still going. "
                            f"Stopping now would abandon the work mid-write. Wait for "
                            f"it to land, or halt production first.")

    # After the response. `os._exit` skips shutdown (atexit, thread joins), so
    # the port comes free at once.
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return {"ok": True, "pid": os.getpid(), "stamp": BUILD_STAMP}


@router.post("/api/act/second-opinion")
def act_second_opinion(body: dict = Body(...),
                       x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Ask the orchestrator persona to audit one pending hire. Decides nothing.

    Kept off `/api/act/decide`, so "the second opinion cannot approve
    anything" holds in code, not only in the prompt. It costs a full roster
    digest, so it runs only on the PO's button.
    """
    _guard(x_colony)
    # Local import: `wake` pulls in the agent runner, which startup should not
    # pay for.
    from .. import wake as wake_mod

    conn = _rw()
    try:
        terms = wake_mod.contract(conn, "investigator")
        if terms is None:
            raise HTTPException(409, "no active investigator contract. Run "
                                     "`python -m colony init`")
        # No BEGIN: the run takes minutes and would block every writer. Each
        # statement commits alone; the worst case is a research ticket whose
        # paragraph never landed.
        return wake_mod.second_opinion(conn, _num(body, "escalation_id", int), terms)
    except control.Refused as exc:
        raise HTTPException(409, str(exc))
    finally:
        conn.close()


@router.post("/api/act/scope")
def act_scope(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Widen or narrow one hired agent's write scope."""
    _guard(x_colony)
    return _act(
        control.set_write_scope,
        _num(body, "agent_id", int),
        [str(p) for p in (body.get("projects") or [])],
    )


@router.post("/api/act/secrets")
def act_secrets(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Decide whether this agent's checkout is given the credential files."""
    _guard(x_colony)
    return _act(control.set_secrets, _num(body, "agent_id", int), bool(body.get("on")))


@router.post("/api/act/retire")
def act_retire(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.retire, _num(body, "agent_id", int))


@router.post("/api/act/dispatch")
def act_dispatch(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.dispatch, _num(body, "story_id", int))


@router.post("/api/act/cancel")
def act_cancel(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.cancel_ticket, _num(body, "ticket_id", int))


@router.post("/api/act/draft-skill")
def act_draft_skill(body: dict = Body(...), x_colony: str | None = Header(None)) -> dict[str, Any]:
    """Ask for a candidate to be written up. Costs nothing now; the wake pays."""
    _guard(x_colony)
    return _act(control.request_draft, _num(body, "skill_id", int))


@router.post("/api/act/promote-skill")
def act_promote_skill(body: dict = Body(...),
                      x_colony: str | None = Header(None)) -> dict[str, Any]:
    """The third gate: put a drafted skill on disk and attach it to roles."""
    _guard(x_colony)
    roles = body.get("roles")
    if isinstance(roles, str):
        roles = [r for r in (part.strip() for part in roles.split(",")) if r]
    return _act(control.promote_skill, _num(body, "skill_id", int), roles or ["ordis"])


@router.post("/api/act/retire-skill")
def act_retire_skill(body: dict = Body(...),
                     x_colony: str | None = Header(None)) -> dict[str, Any]:
    _guard(x_colony)
    return _act(control.retire_skill, _num(body, "skill_id", int), str(body.get("reason") or ""))


@router.get("/api/skill")
def skill(id: int) -> dict[str, Any]:
    """One skill, with its draft, for the drawer."""
    conn = _conn()
    try:
        return control.skill_draft(conn, id)
    except control.Refused as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()
