"""Task queue, audio artifacts, system logs and the pronunciation dictionary."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

import db
from config import ARTIFACT_DIR
from deps import ensure_voice, synthesis_params
from engine import apply_pronunciations, engine, model_installed
from schemas import (
    PronunciationCreate, PronunciationPatch, PronunciationPreview, SynthesisRequest,
)

router = APIRouter()


@router.get("/api/tasks")
def list_tasks(status: str | None = None, limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0)) -> dict:
    clauses, values = ("status=?", [status]) if status else ("1=1", [])
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) n FROM tasks WHERE {clauses}", values).fetchone()["n"]
        rows = conn.execute(f"SELECT * FROM tasks WHERE {clauses} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                            (*values, limit, offset)).fetchall()
    items = []
    for row in rows:
        item = dict(row); item["params"] = db.loads(item["params"], {}); items.append(item)
    return {"items": items, "total": total}


@router.post("/api/tasks")
def create_task(req: SynthesisRequest) -> dict:
    if engine.resource_snapshot()["queue_size"] >= 20:
        raise HTTPException(429, "等待队列已满，请稍后提交")
    speaker = ensure_voice(req.voice_id)
    params = synthesis_params(req, speaker)
    task_id = db.create_task(req.text, speaker["id"], params.model_key, req.model_dump())
    engine.enqueue(task_id)
    return db.task(task_id)


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    item = db.task(task_id)
    if not item:
        raise HTTPException(404, "任务不存在")
    return item


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    item = db.task(task_id)
    if not item:
        raise HTTPException(404, "任务不存在")
    if item["status"] not in {"queued", "running"}:
        raise HTTPException(400, "仅等待中或运行中的任务可以取消")
    engine.cancel(task_id)
    return db.task(task_id)


@router.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str) -> dict:
    item = db.task(task_id)
    if not item:
        raise HTTPException(404, "任务不存在")
    if item["status"] not in {"failed", "cancelled", "interrupted"}:
        raise HTTPException(400, "仅失败或中断任务可以重试")
    new_id = db.create_task(item["text"], item["voice_id"], item["model_key"], item["params"])
    engine.enqueue(new_id)
    return db.task(new_id)


@router.get("/api/artifacts")
def list_artifacts(q: str = "", voice_id: str | None = None,
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
    clauses, values = ["1=1"], []
    if q.strip():
        clauses.append("(title LIKE ? OR text LIKE ? OR voice_name LIKE ?)")
        values.extend([f"%{q.strip()}%"] * 3)
    if voice_id:
        clauses.append("voice_id=?"); values.append(voice_id)
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) n FROM artifacts WHERE {' AND '.join(clauses)}", values).fetchone()["n"]
        rows = conn.execute(f"SELECT * FROM artifacts WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                            (*values, limit, offset)).fetchall()
    return {"items": [dict(row) for row in rows], "total": total}


def artifact_path(artifact_id: str) -> tuple[dict, Path]:
    with db.connect() as conn:
        item = db.row_dict(conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone())
    if not item:
        raise HTTPException(404, "音频产物不存在")
    path = (ARTIFACT_DIR / item["filename"]).resolve()
    if not path.is_relative_to(ARTIFACT_DIR) or not path.is_file():
        raise HTTPException(404, "音频文件已被移走或删除")
    return item, path


@router.get("/api/artifacts/{artifact_id}/audio")
def play_artifact(artifact_id: str) -> FileResponse:
    _, path = artifact_path(artifact_id)
    return FileResponse(path, media_type="audio/wav")


@router.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str) -> FileResponse:
    item, path = artifact_path(artifact_id)
    return FileResponse(path, media_type="audio/wav", filename=f"{item['title'][:30]}.wav")


@router.delete("/api/artifacts/{artifact_id}")
def delete_artifact(artifact_id: str) -> dict:
    item, path = artifact_path(artifact_id)
    path.unlink()
    with db.connect() as conn:
        conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
        conn.execute("UPDATE tasks SET artifact_id=NULL WHERE artifact_id=?", (artifact_id,))
    db.log("INFO", "artifact", f"删除音频产物 {item['title']}")
    return {"ok": True}


@router.get("/api/logs")
def list_logs(level: str | None = None, category: str | None = None, q: str = "",
              task_id: str | None = None, limit: int = Query(100, ge=1, le=500),
              offset: int = Query(0, ge=0)) -> dict:
    clauses, values = ["1=1"], []
    for key, val in (("level", level), ("category", category), ("task_id", task_id)):
        if val:
            clauses.append(f"{key}=?"); values.append(val.upper() if key == "level" else val)
    if q.strip():
        clauses.append("message LIKE ?"); values.append(f"%{q.strip()}%")
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) n FROM logs WHERE {' AND '.join(clauses)}", values).fetchone()["n"]
        rows = conn.execute(f"SELECT * FROM logs WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ? OFFSET ?",
                            (*values, limit, offset)).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        item["metadata"] = db.loads(item["metadata"], {})
    return {"items": items, "total": total}


@router.delete("/api/logs")
def clear_logs() -> dict:
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM logs").fetchone()["n"]
        conn.execute("DELETE FROM logs")
    return {"ok": True, "deleted": count}


@router.get("/api/pronunciations")
def list_pronunciations() -> dict:
    return {"items": db.list_pronunciations()}


@router.post("/api/pronunciations", status_code=201)
def create_pronunciation(req: PronunciationCreate) -> dict:
    # Case-sensitive rules may coexist with a case-insensitive one, because
    # that is exactly what match_case is for: Qwen -> 千问 plus qwen -> other.
    existing = db.list_pronunciations()
    for rule in existing:
        same_term = rule["term"] == req.term or (
            not rule["match_case"] and not req.match_case
            and rule["term"].lower() == req.term.lower()
        )
        if same_term:
            raise HTTPException(400, f"已存在「{req.term}」的替换规则，请直接修改")
    item = db.create_pronunciation(
        req.term, req.say_as, req.note, req.match_case, req.is_enabled,
    )
    db.log("INFO", "pronunciation", f"新增读音规则 {req.term} → {req.say_as}")
    return item


@router.patch("/api/pronunciations/{pron_id}")
def update_pronunciation(pron_id: str, req: PronunciationPatch) -> dict:
    if not db.pronunciation(pron_id):
        raise HTTPException(404, "读音规则不存在")
    values = req.model_dump(exclude_unset=True)
    for key in ("match_case", "is_enabled"):
        if key in values and values[key] is not None:
            values[key] = int(values[key])
    values = {k: v for k, v in values.items() if v is not None}
    return db.update_pronunciation(pron_id, **values)


@router.delete("/api/pronunciations/{pron_id}")
def delete_pronunciation(pron_id: str) -> dict:
    item = db.pronunciation(pron_id)
    if not item:
        raise HTTPException(404, "读音规则不存在")
    db.delete_pronunciation(pron_id)
    db.log("INFO", "pronunciation", f"删除读音规则 {item['term']}")
    return {"ok": True}


@router.post("/api/pronunciations/preview")
def preview_pronunciation(req: PronunciationPreview) -> dict:
    """Show exactly what the model would be asked to say, without synthesizing."""
    rules = db.list_pronunciations(enabled_only=True)
    spoken, used = apply_pronunciations(req.text, rules)
    by_id = {r["id"]: r for r in rules}
    return {
        "original": req.text,
        "spoken": spoken,
        "changed": spoken != req.text,
        "matches": [{"term": by_id[i]["term"], "say_as": by_id[i]["say_as"]}
                    for i in dict.fromkeys(used)],
    }
