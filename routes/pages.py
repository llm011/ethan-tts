"""HTML pages, LAN discovery and the tiny PWA manifest."""
from __future__ import annotations

import socket

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

import db
from config import STATIC_DIR
from engine import engine, model_installed
from web_assets import render_page

router = APIRouter()

NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(render_page("index.html"), headers=NO_CACHE)


@router.get("/chat", response_class=HTMLResponse)
@router.get("/chat/{session_id}", response_class=HTMLResponse)
def chat_page(session_id: str = "") -> HTMLResponse:
    """对话页。带 session_id 时前端会直接打开那段对话，所以刷新和分享都可用。"""
    return HTMLResponse(render_page("chat.html"), headers=NO_CACHE)


@router.get("/chat.webmanifest")
def chat_manifest() -> FileResponse:
    # Lets the page be added to a phone home screen and open standalone.
    return FileResponse(STATIC_DIR / "chat.webmanifest",
                        media_type="application/manifest+json")


@router.get("/api/network")
def network_addresses(request: Request) -> dict:
    """LAN URLs so a phone on the same Wi-Fi can open the voice page."""
    port = request.app.state.port
    bind_host = request.app.state.bind_host
    urls: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address.startswith("127."):
                continue
            urls.append(f"http://{address}:{port}")
    except OSError:
        pass
    # The primary interface is usually the one the default route uses.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            primary = probe.getsockname()[0]
        if primary not in {url.rsplit(":", 2)[-2] for url in urls}:
            urls.insert(0, f"http://{primary}:{port}")
    except OSError:
        pass
    return {"urls": sorted(set(urls)), "port": port,
            "loopback_only": bind_host in {"127.0.0.1", "localhost"}}


@router.get("/health")
def health() -> dict:
    return {"ok": True, "models": engine.resource_snapshot()}
