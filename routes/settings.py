"""Application preferences and the console overview tile."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

import db
from engine import engine
from model_registry import model_status
from schemas import SettingsPatch

router = APIRouter()


@router.get("/api/settings")
def get_settings() -> dict:
    return db.get_settings()


@router.patch("/api/settings")
def update_settings(req: SettingsPatch) -> dict:
    values = req.model_dump(exclude_unset=True)
    if values.get("default_voice_id") and not db.voice(values["default_voice_id"]):
        raise HTTPException(400, "默认音色不存在")
    result = db.update_settings(values)
    db.log("INFO", "settings", "更新系统设置", metadata=values)
    return result


@router.get("/api/overview")
def overview() -> dict:
    with db.connect() as conn:
        artifacts = conn.execute(
            "SELECT COUNT(*) n,COALESCE(SUM(size_bytes),0) bytes,"
            "COALESCE(SUM(audio_seconds),0) seconds FROM artifacts").fetchone()
        tasks = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status,COUNT(*) n FROM tasks GROUP BY status")}
        voice_count = conn.execute("SELECT COUNT(*) n FROM voices").fetchone()["n"]
    return {
        "artifacts": artifacts["n"], "storage_bytes": artifacts["bytes"],
        "audio_seconds": round(artifacts["seconds"], 2), "voices": voice_count,
        "tasks": tasks, "runtime": engine.resource_snapshot(),
        "models": model_status(),
        "recent": _recent_artifacts(limit=5),
    }


def _recent_artifacts(limit: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
