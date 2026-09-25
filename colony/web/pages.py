"""The page itself and the files a phone needs to install it."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from .common import PAGE_CSP, UI_DIR, _asset

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    return _asset("index.html", "text/html; charset=utf-8", request,
                  {"Content-Security-Policy": PAGE_CSP})


@router.get("/app.css")
def app_css(request: Request) -> Response:
    return _asset("app.css", "text/css; charset=utf-8", request)


# The page's ES modules. A bare name only, so the path cannot leave `ui/js/`.
_MODULE_NAME = re.compile(r"[a-z]+\.js")


@router.get("/js/{name}")
def module_js(name: str, request: Request) -> Response:
    if not _MODULE_NAME.fullmatch(name) or not (UI_DIR / "js" / name).is_file():
        raise HTTPException(404)
    return _asset("js/" + name, "text/javascript; charset=utf-8", request)


@router.get("/boot.js")
def boot_js(request: Request) -> Response:
    return _asset("boot.js", "text/javascript; charset=utf-8", request)


# ── the phone ─────────────────────────────────────────────────────────────────
# These routes let a phone install the dashboard as an app: a manifest, a
# service worker, and the icons. `sw.js` is served from the root because a
# service worker controls only pages at or below its own path.

@router.get("/manifest.webmanifest")
def manifest(request: Request) -> Response:
    return _asset("manifest.webmanifest", "application/manifest+json", request)


@router.get("/sw.js")
def service_worker(request: Request) -> Response:
    return _asset("sw.js", "text/javascript; charset=utf-8", request)


# Icons change only on a rebrand, so they are cached.
@router.get("/icon-{name}.png")
def icon(name: str) -> Response:
    if name not in {"192", "512", "maskable-512"}:
        raise HTTPException(404, "no such icon")
    return Response(
        (UI_DIR / f"icon-{name}.png").read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(UI_DIR / "colony.ico", media_type="image/x-icon")
