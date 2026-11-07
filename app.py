"""Local Qwen3-TTS management console and HTTP API.

Run with: .venv/bin/python app.py
Open http://127.0.0.1:8765 after model download.
"""
from __future__ import annotations

import os
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import db
from config import (
    ARTIFACT_DIR, DEFAULT_MODEL, DEFAULT_SETTINGS, MAX_REFERENCE_BYTES,
    MIN_REFERENCE_SECONDS, MODELS, ROOT, VOICE_DIR,
)
from engine import (
    CancelledError, SynthesisParams, apply_pronunciations, engine, model_installed,
    stream_wav_header,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    engine.start_worker()
    db.log("INFO", "system", "管理服务已启动（模型按需加载）")
    try:
        yield
    finally:
        engine.stop_worker()
        db.log("INFO", "system", "管理服务已停止")


app = FastAPI(title="ethan-tts", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice_id: str = "preset:Vivian"
    instruct: str | None = Field(default=None, max_length=300)
    temperature: float = Field(default=.7, ge=0, le=2)
    top_k: int = Field(default=50, ge=1, le=200)
    top_p: float = Field(default=1, ge=.01, le=1)
    repetition_penalty: float = Field(default=1.05, ge=.8, le=2)
    max_tokens: int | None = Field(default=None, ge=50, le=4096)
    sentence_chars: int | None = Field(default=None, ge=20, le=200)
    streaming_interval: float | None = Field(default=None, ge=.16, le=2)
    speed: float = Field(default=1.0, ge=.5, le=2.0)

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请输入需要合成的文本")
        return value


class VoiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["design", "clone"]
    description: str = Field(default="", max_length=300)
    instruct: str | None = Field(default=None, max_length=500)
    language: str = "Chinese"
    reference_text: str | None = Field(default=None, max_length=1000)


class VoicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=300)
    instruct: str | None = Field(default=None, max_length=500)
    language: str | None = None
    is_favorite: bool | None = None
    reference_text: str | None = Field(default=None, max_length=1000)


class SettingsPatch(BaseModel):
    default_voice_id: str | None = None
    max_chars: int | None = Field(default=None, ge=100, le=2000)
    sentence_chars: int | None = Field(default=None, ge=20, le=200)
    streaming_interval: float | None = Field(default=None, ge=.16, le=2)
    autoplay: bool | None = None
    theme: Literal["light", "dark"] | None = None


class ModelDownload(BaseModel):
    model_key: str


class PronunciationCreate(BaseModel):
    term: str = Field(min_length=1, max_length=60)
    say_as: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=200)
    match_case: bool = False
    is_enabled: bool = True

    @field_validator("term", "say_as")
    @classmethod
    def strip_value(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class PronunciationPatch(BaseModel):
    term: str | None = Field(default=None, min_length=1, max_length=60)
    say_as: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=200)
    match_case: bool | None = None
    is_enabled: bool | None = None


class PronunciationPreview(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def ensure_voice(voice_id: str) -> dict:
    speaker = db.voice_for_engine(voice_id)
    if not speaker:
        raise HTTPException(404, "音色不存在")
    if speaker["kind"] == "clone" and not db.reference_ready(speaker["reference_path"]):
        raise HTTPException(400, f"参考录音为空或不足 {MIN_REFERENCE_SECONDS} 秒，请编辑音色并重新上传 10–20 秒的有效录音")
    if speaker["kind"] == "design" and not speaker["instruct"]:
        raise HTTPException(400, "请先填写声音描述")
    return speaker


def synthesis_params(req: SynthesisRequest, speaker: dict) -> SynthesisParams:
    settings = db.get_settings()
    if len(req.text) > settings["max_chars"]:
        raise HTTPException(400, f"当前设置最多允许 {settings['max_chars']} 字")
    model_key = {"custom": "custom-0.6b-4bit", "design": "design-1.7b-4bit",
                 "clone": "base-0.6b-4bit"}[speaker["kind"]]
    if not model_installed(model_key):
        raise HTTPException(409, f"请先下载 {MODELS[model_key]['name']} 模型")
    # Pronunciation rules rewrite only what the model is asked to say; the
    # artifact keeps the original text so the library stays searchable.
    spoken, used = apply_pronunciations(req.text, db.list_pronunciations(enabled_only=True))
    return SynthesisParams(
        text=req.text, voice=speaker, model_key=model_key,
        instruct=req.instruct or speaker["instruct"], temperature=req.temperature,
        top_k=req.top_k, top_p=req.top_p, repetition_penalty=req.repetition_penalty,
        max_tokens=req.max_tokens, sentence_chars=req.sentence_chars or settings["sentence_chars"],
        streaming_interval=req.streaming_interval or settings["streaming_interval"],
        speed=req.speed, spoken_text=spoken, pronunciation_ids=used,
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "models": engine.resource_snapshot()}


@app.get("/api/overview")
def overview() -> dict:
    with db.connect() as conn:
        artifacts = conn.execute("SELECT COUNT(*) n,COALESCE(SUM(size_bytes),0) bytes,COALESCE(SUM(audio_seconds),0) seconds FROM artifacts").fetchone()
        tasks = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM tasks GROUP BY status")}
        voice_count = conn.execute("SELECT COUNT(*) n FROM voices").fetchone()["n"]
    return {
        "artifacts": artifacts["n"], "storage_bytes": artifacts["bytes"],
        "audio_seconds": round(artifacts["seconds"], 2), "voices": voice_count,
        "tasks": tasks, "runtime": engine.resource_snapshot(),
        "models": model_status(), "recent": list_artifacts(limit=5, offset=0)["items"],
    }


@app.get("/api/voices")
def list_voices(kind: str | None = None, q: str = "", favorite: bool = False) -> dict:
    clauses, params = ["1=1"], []
    if kind:
        clauses.append("kind=?"); params.append(kind)
    if q.strip():
        clauses.append("(name LIKE ? OR description LIKE ? OR speaker LIKE ?)")
        params.extend([f"%{q.strip()}%"] * 3)
    if favorite:
        clauses.append("is_favorite=1")
    with db.connect() as conn:
        rows = conn.execute(f"SELECT * FROM voices WHERE {' AND '.join(clauses)} ORDER BY is_preset DESC,is_favorite DESC,created_at", params).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["reference_path"] = db.reference_ready(item["reference_path"])
        items.append(item)
    return {"items": items}


@app.post("/api/voices", status_code=201)
def create_voice(req: VoiceCreate) -> dict:
    if req.kind == "design" and not (req.instruct or "").strip():
        raise HTTPException(400, "文字设计音色需要描述")
    if req.kind == "clone" and not (req.reference_text or "").strip():
        raise HTTPException(400, "克隆音色需要参考音频逐字稿")
    voice_id = f"voice:{uuid.uuid4().hex[:12]}"
    stamp = db.now()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO voices(id,name,kind,language,description,instruct,reference_text,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (voice_id, req.name.strip(), req.kind, req.language, req.description,
             req.instruct, req.reference_text, stamp, stamp),
        )
    db.log("INFO", "voice", f"创建音色 {req.name}", metadata={"id": voice_id, "kind": req.kind})
    return db.voice(voice_id)


@app.post("/api/voices/{voice_id}/reference")
async def upload_reference(
    voice_id: str,
    file: Annotated[UploadFile, File()],
    reference_text: Annotated[str, Form(min_length=1, max_length=1000)],
) -> dict:
    speaker = ensure_voice_for_upload(voice_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".wav", ".mp3", ".m4a", ".flac", ".ogg"}:
        raise HTTPException(400, "仅支持 WAV、MP3、M4A、FLAC、OGG 音频")
    content = await file.read(MAX_REFERENCE_BYTES + 1)
    if len(content) > MAX_REFERENCE_BYTES:
        raise HTTPException(413, "参考音频不能超过 20 MB")
    if len(content) < 100:
        raise HTTPException(400, "音频文件为空或损坏")
    filename = f"{voice_id.removeprefix('voice:')}.wav"
    source_path = VOICE_DIR / f".upload-{uuid.uuid4().hex}{suffix}"
    converted_path = VOICE_DIR / f".converted-{uuid.uuid4().hex}.wav"
    source_path.write_bytes(content)
    import subprocess
    try:
        # M4A/MP4 may have a moov atom that requires seeking. Feeding bytes
        # through pipe:0 can make ffmpeg exit 0 while producing an empty WAV.
        process = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_path),
             "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(converted_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        if process.returncode or not converted_path.is_file():
            db.log("ERROR", "voice", "参考录音解码失败", metadata={"stderr": process.stderr.decode(errors="replace")[-800:]})
            raise HTTPException(400, "无法解码音频，请换一段有效录音")
        try:
            with wave.open(str(converted_path), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
        except (EOFError, wave.Error) as exc:
            raise HTTPException(400, "转码后的音频无效，请重新上传") from exc
        if duration < MIN_REFERENCE_SECONDS:
            raise HTTPException(
                400,
                f"有效录音只有 {duration:.1f} 秒，至少需要 {MIN_REFERENCE_SECONDS} 秒；请上传 10–20 秒清晰人声",
            )
        converted_path.replace(VOICE_DIR / filename)
    finally:
        source_path.unlink(missing_ok=True)
        converted_path.unlink(missing_ok=True)
    with db.connect() as conn:
        conn.execute("UPDATE voices SET reference_path=?,reference_text=?,updated_at=? WHERE id=?",
                     (filename, reference_text.strip(), db.now(), voice_id))
    db.log("INFO", "voice", "更新参考录音", metadata={"voice_id": voice_id, "filename": filename})
    return db.voice(voice_id)


def ensure_voice_for_upload(voice_id: str) -> dict:
    speaker = db.voice_for_engine(voice_id)
    if not speaker:
        raise HTTPException(404, "音色不存在")
    if speaker["is_preset"] or speaker["kind"] != "clone":
        raise HTTPException(400, "仅自定义克隆音色可上传参考音频")
    return speaker


@app.patch("/api/voices/{voice_id}")
def update_voice(voice_id: str, req: VoicePatch) -> dict:
    speaker = db.voice_for_engine(voice_id)
    if not speaker:
        raise HTTPException(404, "音色不存在")
    values = req.model_dump(exclude_unset=True)
    if speaker["is_preset"]:
        for key in ("name", "description", "instruct", "language", "reference_text"):
            values.pop(key, None)
    values["updated_at"] = db.now()
    if values:
        with db.connect() as conn:
            conn.execute(f"UPDATE voices SET {','.join(f'{k}=?' for k in values)} WHERE id=?",
                         (*values.values(), voice_id))
    return db.voice(voice_id)


@app.delete("/api/voices/{voice_id}")
def delete_voice(voice_id: str) -> dict:
    speaker = db.voice_for_engine(voice_id)
    if not speaker:
        raise HTTPException(404, "音色不存在")
    if speaker["is_preset"]:
        raise HTTPException(400, "内置音色不可删除")
    if speaker["reference_path"]:
        db.delete_files([str(VOICE_DIR / speaker["reference_path"])])
    with db.connect() as conn:
        conn.execute("DELETE FROM voices WHERE id=?", (voice_id,))
    db.log("INFO", "voice", f"删除音色 {speaker['name']}")
    return {"ok": True}


@app.post("/api/tts")
def synthesize(req: SynthesisRequest) -> Response:
    import time
    speaker = ensure_voice(req.voice_id)
    params = synthesis_params(req, speaker)
    task_id = db.create_task(req.text, speaker["id"], params.model_key, req.model_dump())
    db.update_task(task_id, status="running", started_at=db.now())
    started = time.monotonic()
    try:
        result = engine.generate_file(params, task_id)
        db.add_artifact(result)
        db.update_task(task_id, status="succeeded", artifact_id=result["id"], finished_at=db.now(),
                       duration_seconds=result["generation_seconds"])
        db.log("INFO", "task", "同步合成完成", task_id, {"artifact_id": result["id"]})
        db.bump_pronunciation_hits(params.pronunciation_ids)
        data = (ARTIFACT_DIR / result["filename"]).read_bytes()
        return Response(data, media_type="audio/wav", headers={
            "X-Task-ID": task_id, "X-Artifact-ID": result["id"],
            "X-Gen-Seconds": f"{result['generation_seconds']:.2f}",
            "X-Audio-Seconds": f"{result['audio_seconds']:.2f}",
            "X-RTF": f"{result['generation_seconds']/max(result['audio_seconds'], .001):.2f}",
            "X-Elapsed-Seconds": f"{time.monotonic()-started:.2f}",
        })
    except Exception as exc:
        db.update_task(task_id, status="failed", error=str(exc)[:1000], finished_at=db.now())
        db.log("ERROR", "task", f"同步合成失败：{exc}", task_id)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/tts/stream")
def synthesize_stream(req: SynthesisRequest) -> StreamingResponse:
    speaker = ensure_voice(req.voice_id)
    params = synthesis_params(req, speaker)
    params.stream = True

    def generate():
        import time
        started = time.monotonic()
        yield stream_wav_header()
        try:
            for chunk in engine.synthesize(params):
                yield chunk
            db.log("INFO", "task", "流式试听完成", metadata={
                "seconds": round(time.monotonic()-started, 2), "voice_id": speaker["id"],
            })
        except (CancelledError, GeneratorExit):
            return
        except Exception as exc:
            db.log("ERROR", "task", f"流式试听失败：{exc}")
            return

    return StreamingResponse(generate(), media_type="audio/wav")


@app.get("/api/tasks")
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


@app.post("/api/tasks")
def create_task(req: SynthesisRequest) -> dict:
    if engine.resource_snapshot()["queue_size"] >= 20:
        raise HTTPException(429, "等待队列已满，请稍后提交")
    speaker = ensure_voice(req.voice_id)
    params = synthesis_params(req, speaker)
    task_id = db.create_task(req.text, speaker["id"], params.model_key, req.model_dump())
    engine.enqueue(task_id)
    return db.task(task_id)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    item = db.task(task_id)
    if not item:
        raise HTTPException(404, "任务不存在")
    return item


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    item = db.task(task_id)
    if not item:
        raise HTTPException(404, "任务不存在")
    if item["status"] not in {"queued", "running"}:
        raise HTTPException(400, "仅等待中或运行中的任务可以取消")
    engine.cancel(task_id)
    return db.task(task_id)


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str) -> dict:
    item = db.task(task_id)
    if not item:
        raise HTTPException(404, "任务不存在")
    if item["status"] not in {"failed", "cancelled", "interrupted"}:
        raise HTTPException(400, "仅失败或中断任务可以重试")
    new_id = db.create_task(item["text"], item["voice_id"], item["model_key"], item["params"])
    engine.enqueue(new_id)
    return db.task(new_id)


@app.get("/api/artifacts")
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


@app.get("/api/artifacts/{artifact_id}/audio")
def play_artifact(artifact_id: str) -> FileResponse:
    _, path = artifact_path(artifact_id)
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str) -> FileResponse:
    item, path = artifact_path(artifact_id)
    return FileResponse(path, media_type="audio/wav", filename=f"{item['title'][:30]}.wav")


@app.delete("/api/artifacts/{artifact_id}")
def delete_artifact(artifact_id: str) -> dict:
    item, path = artifact_path(artifact_id)
    path.unlink()
    with db.connect() as conn:
        conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
        conn.execute("UPDATE tasks SET artifact_id=NULL WHERE artifact_id=?", (artifact_id,))
    db.log("INFO", "artifact", f"删除音频产物 {item['title']}")
    return {"ok": True}


@app.get("/api/logs")
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


@app.delete("/api/logs")
def clear_logs() -> dict:
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM logs").fetchone()["n"]
        conn.execute("DELETE FROM logs")
    return {"ok": True, "deleted": count}


@app.get("/api/pronunciations")
def list_pronunciations() -> dict:
    return {"items": db.list_pronunciations()}


@app.post("/api/pronunciations", status_code=201)
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


@app.patch("/api/pronunciations/{pron_id}")
def update_pronunciation(pron_id: str, req: PronunciationPatch) -> dict:
    if not db.pronunciation(pron_id):
        raise HTTPException(404, "读音规则不存在")
    values = req.model_dump(exclude_unset=True)
    for key in ("match_case", "is_enabled"):
        if key in values and values[key] is not None:
            values[key] = int(values[key])
    values = {k: v for k, v in values.items() if v is not None}
    return db.update_pronunciation(pron_id, **values)


@app.delete("/api/pronunciations/{pron_id}")
def delete_pronunciation(pron_id: str) -> dict:
    item = db.pronunciation(pron_id)
    if not item:
        raise HTTPException(404, "读音规则不存在")
    db.delete_pronunciation(pron_id)
    db.log("INFO", "pronunciation", f"删除读音规则 {item['term']}")
    return {"ok": True}


@app.post("/api/pronunciations/preview")
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


@app.get("/api/settings")
def get_settings() -> dict:
    return db.get_settings()


@app.patch("/api/settings")
def update_settings(req: SettingsPatch) -> dict:
    values = req.model_dump(exclude_unset=True)
    if values.get("default_voice_id") and not db.voice(values["default_voice_id"]):
        raise HTTPException(400, "默认音色不存在")
    result = db.update_settings(values)
    db.log("INFO", "settings", "更新系统设置", metadata=values)
    return result


def model_status() -> list[dict]:
    loaded = set(engine.resource_snapshot()["loaded_models"])
    return [{**value, "key": key, "installed": model_installed(key), "loaded": key in loaded}
            for key, value in MODELS.items()]


@app.get("/api/models")
def models() -> dict:
    return {"items": model_status()}


@app.post("/api/models/download")
def download_model(req: ModelDownload) -> dict:
    if req.model_key not in MODELS:
        raise HTTPException(400, "不支持的模型")
    if model_installed(req.model_key):
        return {"ok": True, "installed": True, "message": "模型已经下载"}
    if not model_download_lock.acquire(blocking=False):
        raise HTTPException(409, "已有模型正在下载，请稍候")
    import subprocess
    import sys
    import threading

    def run_download() -> None:
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "download_model.py"), "--model", req.model_key],
                cwd=ROOT, capture_output=True, text=True, timeout=1800,
                env={**os.environ, "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")},
            )
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout)[-1500:])
            db.log("INFO", "model", f"模型下载完成：{req.model_key}")
        except Exception as exc:
            db.log("ERROR", "model", f"模型下载失败：{exc}")
        finally:
            model_download_lock.release()

    threading.Thread(target=run_download, name="model-download", daemon=True).start()
    return {"ok": True, "installed": False, "message": "已开始后台下载，请留意日志"}


@app.post("/api/models/unload")
def unload_models() -> dict:
    engine.unload()
    return {"ok": True, **engine.resource_snapshot()}


import threading
model_download_lock = threading.Lock()


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="ethan-tts 本地语音管理台")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
