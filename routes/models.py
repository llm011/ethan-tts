"""Model inventory, downloads and memory release."""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

import model_registry as registry
import stt
from config import MODELS
from engine import engine, model_installed, model_size_bytes
from schemas import ModelDownload

router = APIRouter()


def _dir_bytes(path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        pass
    return total


@router.get("/api/models")
def models() -> dict:
    return {"items": registry.model_status()}


@router.get("/api/models/downloads")
def downloads() -> dict:
    """Which downloads are running/just finished, with bytes on disk."""
    active = registry.active_downloads()
    items = []
    for key, state in active.items():
        if key not in MODELS:
            size = 0
        elif MODELS[key]["role"] == "asr":
            size = stt.asr_model_size_bytes(key)
        else:
            size = model_size_bytes(key)
        target = (MODELS[key].get("size_gb") or 0) * 1e9
        items.append({
            "model_key": key,
            "status": state.get("status", "running"),   # running / done / error
            "downloaded": size,
            "total": int(target) or None,
            "percent": round(min(1.0, size / target) * 100, 1) if target else None,
            "message": state.get("message", ""),
        })
    return {"items": items}


@router.post("/api/models/download")
def download_model(req: ModelDownload) -> dict:
    if req.model_key not in MODELS:
        raise HTTPException(400, "不支持的模型")
    if model_installed(req.model_key):
        return {"ok": True, "installed": True, "message": "模型已经下载"}
    # Claim the slot before starting anything: two clicks (or two tabs) used to
    # race past this check and launch two downloaders for the same weights.
    busy = registry.claim_download(req.model_key)
    if busy:
        raise HTTPException(409, busy)
    registry.start_download(req.model_key)
    return {"ok": True, "installed": False, "message": "已开始后台下载", "status": "running"}


@router.post("/api/models/unload")
def unload_models() -> dict:
    engine.unload()
    return {"ok": True, **engine.resource_snapshot()}
