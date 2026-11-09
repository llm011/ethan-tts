"""Local Qwen3-TTS management console and HTTP API.

Run with: .venv/bin/python app.py
Open http://127.0.0.1:8765 after model download.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import chat
import llm
import stt
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import db
from config import (
    ARTIFACT_DIR, ASR_MODELS, CHAT_DEFAULTS, CHAT_MODEL_PRESETS, DEFAULT_ASR_MODEL,
    DEFAULT_MODEL, DEFAULT_SETTINGS, DEFAULT_SYSTEM_PROMPT, MAX_REFERENCE_BYTES,
    MIN_REFERENCE_SECONDS, MODELS, ROOT, SAMPLE_RATE, STT_SAMPLE_RATE, STATIC_DIR,
    TTS_MODELS, VOICE_DIR,
)
from engine import (
    CancelledError, QueueTimeout, SynthesisParams, apply_pronunciations, engine,
    model_installed, model_path, model_size_bytes, resolve_model_key,
    stream_wav_header,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    # 上次进程异常退出（崩溃 / kill -9 / 机器重启）时，正在跑的任务会永远停在
    # running，队列也不会再推进。启动时统一标成 interrupted，避免它们看起来
    # 像"还在进行"，也避免前端一直转圈。
    orphaned = db.reap_orphan_tasks()
    if orphaned:
        db.log("WARNING", "task", f"启动时回收 {orphaned} 个中断任务")
    engine.start_worker()
    db.log("INFO", "system", "管理服务已启动（模型按需加载）")
    try:
        yield
    finally:
        engine.stop_worker()
        db.log("INFO", "system", "管理服务已停止")


# 同步合成的排队上限：超过就返回 503，而不是让客户端无限等。
#
# 这个值只管「卡死」不管「慢」：本地克隆音色单节约 60s，书稿类管线一次并发
# 3–10 节，串行队列里最后一个正常也要排几分钟。实测 180s 会把正常批量误杀
# （第 3 节等到 120s+ 就被判超时）。默认放到 900s（15 分钟），够覆盖正常排队；
# 真卡住（模型 hang / 死循环）仍能在 15 分钟内报出来。
# 需要更快失败的环境（CI）用 ETHAN_TTS_QUEUE_TIMEOUT 调小。
QUEUE_TIMEOUT_SEC = float(os.environ.get("ETHAN_TTS_QUEUE_TIMEOUT", "900"))

app = FastAPI(title="ethan-tts", lifespan=lifespan)


class NoCacheStatic(StaticFiles):
    """Local app: always revalidate, so an edited page is never served stale.

    StaticFiles sends ETag/Last-Modified and lets the browser reuse its copy;
    during development that meant a CSS fix looked like it never happened.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")


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



def _asset_versioned(html: str) -> str:
    """给页面引用的静态资源拼内容指纹，彻底绕开浏览器缓存。

    ``no-cache`` 只是让浏览器「去问服务端」，实测仍有浏览器复用内存里的旧
    文件；把 ?v=<内容哈希> 拼到 URL 上最干脆——内容一变 URL 就变。
    """
    import hashlib

    def add_version(match: re.Match) -> str:
        attr, path = match.group(1), match.group(2)
        target = STATIC_DIR / path.removeprefix("/static/")
        try:
            digest = hashlib.md5(target.read_bytes()).hexdigest()[:10]
        except OSError:
            return match.group(0)
        return f'{attr}="{path}?v={digest}"'

    return re.sub(r'(href|src)="(/static/[^"?]+)"', add_version, html)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTMLResponse(
        _asset_versioned((STATIC_DIR / "index.html").read_text(encoding="utf-8")),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/chat", response_class=HTMLResponse)
@app.get("/chat/{session_id}", response_class=HTMLResponse)
def chat_page(session_id: str = "") -> str:
    """对话页。带 session_id 时前端会直接打开那段对话，所以刷新和分享都可用。"""
    return HTMLResponse(
        _asset_versioned((STATIC_DIR / "chat.html").read_text(encoding="utf-8")),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/api/network")
def network_addresses() -> dict:
    """LAN URLs so a phone on the same Wi-Fi can open the voice page."""
    import socket

    urls: list[str] = []
    hostname = socket.gethostname()
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if address.startswith("127."):
                continue
            urls.append(f"http://{address}:{_PORT}")
    except OSError:
        pass
    # The primary interface is usually the one the default route uses.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            primary = probe.getsockname()[0]
        if primary not in {url.rsplit(":", 2)[-2] for url in urls}:
            urls.insert(0, f"http://{primary}:{_PORT}")
    except OSError:
        pass
    return {"urls": sorted(set(urls)), "port": _PORT,
            "loopback_only": _BIND_HOST in {"127.0.0.1", "localhost"}}


@app.get("/chat.webmanifest")
def chat_manifest() -> FileResponse:
    # Lets the page be added to a phone home screen and open standalone.
    return FileResponse(STATIC_DIR / "chat.webmanifest", media_type="application/manifest+json")


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
        result = engine.generate_file(params, task_id, queue_timeout=QUEUE_TIMEOUT_SEC)
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
        if isinstance(exc, QueueTimeout):
            # 503 而非 500：这是「忙」不是「坏」，客户端可安全重试。
            raise HTTPException(503, str(exc)) from exc
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
    asr_loaded = stt.stt.loaded_key()
    items = []
    for key, value in MODELS.items():
        if value["role"] == "asr":
            installed, loaded_now = stt.asr_model_installed(key), asr_loaded == key
            size = stt.asr_model_size_bytes(key)
        else:
            installed, loaded_now = model_installed(key), key in loaded
            size = model_size_bytes(key)
        items.append({**value, "key": key, "installed": installed, "loaded": loaded_now,
                      "size_bytes": size, "size_label": _resolve_size_label(key, size)})
    # Probe every model whose repository size is not cached yet, including the
    # ones currently showing a config estimate: the probe replaces the guess with
    # the real repository size.
    stale = [item["key"] for item in items if not item["size_bytes"]
             and repo_size_gb(item["key"]) is None]
    if stale:
        probe_sizes_in_background(stale)
    return items


def _resolve_size_label(model_key: str, disk_bytes: int) -> str:
    if disk_bytes > 0:
        return _size_label(disk_bytes)
    probed = repo_size_gb(model_key)
    if probed:
        return _size_label_from_gb(probed)
    estimate = MODELS[model_key].get("size_gb") or 0
    return f"约 {_size_label_from_gb(estimate)}" if estimate else ""


def _size_label(size: int) -> str:
    if size <= 0:
        return ""
    gb = size / 1e9
    return f"{gb:.1f} GB" if gb >= .95 else f"{size / 1e6:.0f} MB"


def _size_label_from_gb(gb: float) -> str:
    return f"{gb:.1f} GB" if gb >= .95 else f"{gb * 1000:.0f} MB" if gb else ""


# Repo sizes for models that are not on disk. Asking the hub costs a network
# round trip, so answers are cached in SQLite and only refreshed on misses.
_size_probe_lock = threading.Lock()
_size_probe_running: set[str] = set()


def repo_size_gb(model_key: str) -> float | None:
    """Cached repository size in GB, or None when it was never probed."""
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (f"model_size:{model_key}",)).fetchone()
        return db.loads(row["value"], None) if row else None
    except sqlite3.Error:
        return None


def _store_repo_size(model_key: str, gb: float) -> None:
    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"model_size:{model_key}", db.dumps(round(gb, 3))),
            )
    except sqlite3.Error:
        pass


def _probe_repo_size(model_key: str) -> None:
    import os as _os

    from huggingface_hub import HfApi

    spec = MODELS[model_key]
    endpoint = _os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        api = HfApi(endpoint=endpoint)
        total = sum(f.size or 0 for f in api.model_info(
            spec["repo"], files_metadata=True).siblings)
        extra = spec.get("extra_repo")
        if extra:
            total += sum(f.size or 0 for f in api.model_info(
                extra["repo"], files_metadata=True).siblings
                if any(f.rfilename.endswith(p.replace("*", "")) or f.rfilename.startswith(p.split("*")[0])
                       for p in extra["files"]))
        if total:
            _store_repo_size(model_key, total / 1e9)
            db.log("INFO", "model", f"探测到模型体积 {model_key}：{total / 1e9:.2f} GB")
    except Exception as exc:
        db.log("WARNING", "model", f"探测模型体积失败 {model_key}：{exc}")
    finally:
        with _size_probe_lock:
            _size_probe_running.discard(model_key)


def probe_sizes_in_background(keys: list[str]) -> None:
    with _size_probe_lock:
        todo = [k for k in keys if k not in _size_probe_running]
        _size_probe_running.update(todo)
    for key in todo:
        threading.Thread(target=_probe_repo_size, args=(key,), daemon=True,
                         name=f"size-probe-{key}").start()


@app.get("/api/models")
def models() -> dict:
    return {"items": model_status()}


# Download bookkeeping lives in memory: a download belongs to this process, and
# the page polls it to show progress instead of guessing from a toast.
_downloads: dict[str, dict] = {}
_download_lock_guard = threading.Lock()


def _dir_bytes(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        pass
    return total


def download_state(model_key: str) -> dict:
    with _download_lock_guard:
        state = dict(_downloads.get(model_key) or {})
    return state


@app.get("/api/models/downloads")
def downloads() -> dict:
    """Which downloads are running/just finished, with bytes on disk."""
    with _download_lock_guard:
        active = {k: dict(v) for k, v in _downloads.items()}
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


@app.post("/api/models/download")
def download_model(req: ModelDownload) -> dict:
    if req.model_key not in MODELS:
        raise HTTPException(400, "不支持的模型")
    if model_installed(req.model_key):
        return {"ok": True, "installed": True, "message": "模型已经下载"}
    # Claim the slot before starting anything: two clicks (or two tabs) used to
    # race past this check and launch two downloaders for the same weights.
    with _download_lock_guard:
        running = [k for k, v in _downloads.items() if v.get("status") == "running"]
        if req.model_key in running:
            raise HTTPException(409, "这个模型正在下载中，请稍候")
        if running:
            raise HTTPException(409, f"{MODELS[running[0]]['name']} 正在下载，同一时间只下载一个模型")
        _downloads[req.model_key] = {"status": "running", "message": "正在下载"}

    import subprocess
    import sys

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
            with _download_lock_guard:
                _downloads[req.model_key] = {"status": "done", "message": "下载完成"}
        except Exception as exc:
            db.log("ERROR", "model", f"模型下载失败：{exc}")
            with _download_lock_guard:
                _downloads[req.model_key] = {"status": "error", "message": str(exc)[:300]}

    threading.Thread(target=run_download, name="model-download", daemon=True).start()
    return {"ok": True, "installed": False, "message": "已开始后台下载", "status": "running"}


@app.post("/api/models/unload")
def unload_models() -> dict:
    engine.unload()
    return {"ok": True, **engine.resource_snapshot()}



# Overwritten by the CLI entry point; the API reports these back to the page.
_BIND_HOST = os.environ.get("HOST", "127.0.0.1")
_PORT = int(os.environ.get("PORT", "8765"))




# ---------------------------------------------------------------- voice chat
# The chat page is its own entry point (/chat) so the voice app can be opened
# directly from a phone home screen without the rest of the console.

class ChatSettingsPatch(BaseModel):
    llm_base_url: str | None = Field(default=None, max_length=300)
    llm_api_key: str | None = Field(default=None, max_length=300)
    llm_model: str | None = Field(default=None, max_length=120)
    llm_temperature: float | None = Field(default=None, ge=0, le=2)
    llm_use_proxy: bool | None = None
    llm_proxy_url: str | None = Field(default=None, max_length=200)
    llm_max_tokens: int | None = Field(default=None, ge=64, le=2048)
    system_prompt: str | None = Field(default=None, max_length=4000)
    voice_id: str | None = None
    asr_model: str | None = None
    tts_min_chars: int | None = Field(default=None, ge=2, le=40)
    tts_max_chars: int | None = Field(default=None, ge=20, le=200)
    tts_first_chars: int | None = Field(default=None, ge=4, le=40)
    speech_rate: float | None = Field(default=None, ge=.5, le=2)
    auto_send: bool | None = None
    history_turns: int | None = Field(default=None, ge=0, le=40)


class ChatSessionCreate(BaseModel):
    title: str = Field(default="新的对话", max_length=60)


class ChatSessionPatch(BaseModel):
    title: str = Field(min_length=1, max_length=60)


class ModelTestRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


# 开发自测时常把地址写成这些，一旦残留在用户库里，报错会变成
# 「无法连接模型服务：nodename nor servname」，很难联想到是配置问题。
# 只匹配「整个主机名就是占位词」的情况：子串匹配会误伤
# localhost:11434（Ollama）和 test-api.company.com 这类真实地址。
_FAKE_HOSTS = {"fake", "fake.local", "example.com", "example.org", "test", "placeholder"}


def config_warning(base_url: str) -> str:
    """给前端一句人话，解释这个地址为什么不可用。"""
    url = (base_url or "").strip().lower()
    if not url:
        return ""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    # 去掉端口再看，localhost:11434 属于正常用法
    bare = host.rsplit(":", 1)[0] if ":" in host else host
    if bare in _FAKE_HOSTS or bare.startswith("fake."):
        return f"模型地址看起来是测试用的占位值（{host}），请在设置页改成真实的服务地址"
    return ""


def public_model_status() -> dict:
    return {"tts": [item for item in model_status() if item["role"] == "tts"],
            "asr": [item for item in model_status() if item["role"] == "asr"]}


@app.get("/api/chat/bootstrap")
def chat_bootstrap() -> dict:
    """Everything the chat page needs on first paint, in one round trip."""
    settings = db.get_chat_settings()
    system_prompt = settings.pop("system_prompt")
    warning = config_warning(settings.get("llm_base_url", ""))
    return {
        "settings": settings,
        "config_warning": warning,
        "models": public_model_status(),
        "voices": [dict(row) for row in _voice_rows()],
        "presets": CHAT_MODEL_PRESETS,
        "system_prompt": system_prompt,
        "default_prompt": DEFAULT_SYSTEM_PROMPT,
        "audio_rate": SAMPLE_RATE,
        "stt_rate": STT_SAMPLE_RATE,
        "sessions": db.list_chat_sessions(),
    }


def _voice_rows():
    with db.connect() as conn:
        return conn.execute(
            "SELECT id,name,kind,description,speaker,language FROM voices "
            "ORDER BY is_preset DESC, is_favorite DESC, created_at",
        ).fetchall()


@app.post("/api/chat/warmup")
def chat_warmup() -> dict:
    """Preload the chat models while the user is still reading the page.

    Loading TTS takes ~11 s cold; doing it on the first reply means the user
    stares at a spinner right after speaking. Warming up on page open moves
    that cost to a moment where nothing is expected yet.
    """
    settings = db.get_chat_settings()
    voice = db.voice_for_engine(settings["voice_id"])

    def warm() -> None:
        if voice:
            try:
                engine.load(resolve_model_key(voice))
            except Exception as exc:
                db.log("WARNING", "chat", f"预热合成模型失败：{exc}")
        key = settings["asr_model"]
        if stt.asr_model_installed(key):
            try:
                stt.stt.load(key)
            except Exception as exc:
                db.log("WARNING", "chat", f"预热识别模型失败：{exc}")

    threading.Thread(target=warm, name="chat-warmup", daemon=True).start()
    return {"ok": True}


@app.get("/api/chat/settings")
def get_chat_settings_endpoint() -> dict:
    return db.get_chat_settings()


@app.patch("/api/chat/settings")
def patch_chat_settings(req: ChatSettingsPatch) -> dict:
    values = req.model_dump(exclude_unset=True)
    if values.get("llm_base_url") is not None:
        try:
            values["llm_base_url"] = llm.normalize_base_url(values["llm_base_url"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if values.get("voice_id") and not db.voice(values["voice_id"]):
        raise HTTPException(400, "朗读音色不存在")
    if values.get("asr_model") and values["asr_model"] not in ASR_MODELS:
        raise HTTPException(400, "不支持的语音识别模型")
    if (values.get("tts_min_chars") and values.get("tts_max_chars")
            and values["tts_min_chars"] >= values["tts_max_chars"]):
        raise HTTPException(400, "最短句长需要小于最长句长")
    result = db.update_chat_settings(values)
    db.log("INFO", "settings", "更新语音对话设置",
           metadata={k: ("已更新" if k == "llm_api_key" else v) for k, v in values.items()})
    return result


@app.post("/api/chat/settings/test")
def test_chat_model(req: ModelTestRequest) -> dict:
    """One tiny completion proves the URL, key and model name all work."""
    key = req.api_key.strip() or chat.stored_api_key()
    config = llm.ChatConfig(base_url=req.base_url, api_key=key, model=req.model,
                            max_tokens=16, temperature=0)
    started = time.monotonic()
    try:
        reply = "".join(llm.stream_chat(config, [
            {"role": "user", "content": "只回复两个字：你好"},
        ]))
    except llm.LLMError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "message": str(exc)})
    return {"ok": True, "reply": reply.strip()[:60],
            "seconds": round(time.monotonic() - started, 2)}


@app.get("/api/chat/settings/models")
def list_provider_models(base_url: str, api_key: str = "") -> dict:
    try:
        items = llm.list_models(llm.ChatConfig(
            base_url=base_url, api_key=api_key.strip() or chat.stored_api_key()))
    except Exception as exc:
        raise HTTPException(400, f"读取模型列表失败：{exc}") from exc
    return {"items": items}


# ---------------------------------------------------------------- sessions

@app.get("/api/chat/sessions")
def list_chat_sessions() -> dict:
    return {"items": db.list_chat_sessions()}


@app.post("/api/chat/sessions", status_code=201)
def create_chat_session(req: ChatSessionCreate) -> dict:
    return db.create_chat_session(req.title)


@app.get("/api/chat/sessions/{session_id}")
def get_chat_session(session_id: str) -> dict:
    session = db.chat_session(session_id)
    if not session:
        raise HTTPException(404, "对话不存在")
    return {**session, "messages": db.chat_messages(session_id)}


@app.patch("/api/chat/sessions/{session_id}")
def rename_chat_session(session_id: str, req: ChatSessionPatch) -> dict:
    if not db.chat_session(session_id):
        raise HTTPException(404, "对话不存在")
    db.rename_chat_session(session_id, req.title)
    return db.chat_session(session_id)


@app.delete("/api/chat/sessions/{session_id}")
def delete_chat_session(session_id: str) -> dict:
    if not db.chat_session(session_id):
        raise HTTPException(404, "对话不存在")
    db.delete_chat_session(session_id)
    db.log("INFO", "chat", "删除对话记录", metadata={"session": session_id})
    return {"ok": True}


# ---------------------------------------------------------------- one turn

def _save_turn_audio(content: bytes) -> Path:
    """Persist the uploaded recording just long enough to transcribe it."""
    suffix = ".wav"
    path = Path(tempfile.mkdtemp(prefix="chat-turn-")) / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(content)
    return path


@app.post("/api/chat/turn")
async def chat_turn(
    request: Request,
    session_id: Annotated[str, Form()] = "",
    transcript: Annotated[str, Form()] = "",
    audio: Annotated[UploadFile | None, File()] = None,
) -> StreamingResponse:
    """Run one spoken turn and stream the reply as SSE.

    Events: status / asr / user / sentence / audio / pause / warn / done / error.
    Audio is raw PCM16 mono at ``sample_rate`` of the settings, base64 encoded,
    so the page can schedule chunks while the reply is still generating.
    """
    session = db.chat_session(session_id) if session_id else None
    if not session:
        session = db.create_chat_session()
        session_id = session["id"]

    text = transcript.strip() or None
    path: Path | None = None
    if text is None:
        if audio is None:
            raise HTTPException(400, "请提供录音或文本")
        content = await audio.read(MAX_REFERENCE_BYTES + 1)
        if len(content) > MAX_REFERENCE_BYTES:
            raise HTTPException(413, "录音过长，请分成几句说")
        if len(content) < 100:
            raise HTTPException(400, "录音为空，请重新录一次")
        path = _save_turn_audio(content)

    def generate():
        try:
            yield from chat.run_turn(session_id, wav_path=path, transcript=text,
                                     input_mode="voice" if text is None else "text")
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
                try:
                    path.parent.rmdir()
                except OSError:
                    pass

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Chat-Session": session_id},
    )


@app.post("/api/chat/speak")
def chat_speak(req: SynthesisRequest) -> StreamingResponse:
    """Re-read one saved message aloud (the page's 重新朗读 button)."""
    speaker = ensure_voice(req.voice_id)
    params = synthesis_params(req, speaker)
    params.stream = True

    def generate():
        yield stream_wav_header()
        try:
            for chunk in engine.synthesize(params):
                yield chunk
        except (CancelledError, GeneratorExit):
            return

    return StreamingResponse(generate(), media_type="audio/wav")


if __name__ == "__main__":
    import argparse
    import uvicorn

    from config import PID_FILE
    from single_instance import acquire_or_exit

    # 必须在 import 重量级模块 / 加载模型之前拿锁：并发拉起的第二份进程
    # 会在这里立刻退出，而不是先吃满内存再争端口。
    _instance_lock = acquire_or_exit(PID_FILE)

    parser = argparse.ArgumentParser(description="ethan-tts 本地语音管理台")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="用 0.0.0.0 让同一局域网的手机访问")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    _BIND_HOST = args.host
    _PORT = args.port
    if args.host in {"0.0.0.0", "::"}:
        db.log("INFO", "system", "服务已监听局域网，手机可用 /chat 打开语音对话")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
