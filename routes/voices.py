"""Voice library: presets, designed voices and reference-audio clones."""
from __future__ import annotations

import subprocess
import uuid
import wave
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import db
from config import MAX_REFERENCE_BYTES, MIN_REFERENCE_SECONDS, VOICE_DIR
from schemas import VoiceCreate, VoicePatch

router = APIRouter()


@router.get("/api/voices")
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


@router.post("/api/voices", status_code=201)
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


@router.post("/api/voices/{voice_id}/reference")
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


@router.patch("/api/voices/{voice_id}")
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


@router.delete("/api/voices/{voice_id}")
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
