"""Voice chat: bootstrap payload, model settings, sessions and one spoken turn."""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import chat
import db
import llm
import stt
from config import (
    ASR_MODELS, CHAT_DEFAULTS, CHAT_MODEL_PRESETS, DEFAULT_ASR_MODEL,
    DEFAULT_SYSTEM_PROMPT, MAX_REFERENCE_BYTES, SAMPLE_RATE, STT_SAMPLE_RATE,
)
from deps import ensure_voice, synthesis_params
from engine import CancelledError, engine, stream_wav_header
from model_registry import model_status
from schemas import SynthesisRequest
from settings_store import QUEUE_TIMEOUT_SEC

router = APIRouter()

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


@router.get("/api/chat/bootstrap")
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


@router.post("/api/chat/warmup")
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


@router.get("/api/chat/settings")
def get_chat_settings_endpoint() -> dict:
    return db.get_chat_settings()


@router.patch("/api/chat/settings")
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


@router.post("/api/chat/settings/test")
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


@router.get("/api/chat/settings/models")
def list_provider_models(base_url: str, api_key: str = "") -> dict:
    try:
        items = llm.list_models(llm.ChatConfig(
            base_url=base_url, api_key=api_key.strip() or chat.stored_api_key()))
    except Exception as exc:
        raise HTTPException(400, f"读取模型列表失败：{exc}") from exc
    return {"items": items}


# ---------------------------------------------------------------- sessions

@router.get("/api/chat/sessions")
def list_chat_sessions() -> dict:
    return {"items": db.list_chat_sessions()}


@router.post("/api/chat/sessions", status_code=201)
def create_chat_session(req: ChatSessionCreate) -> dict:
    return db.create_chat_session(req.title)


@router.get("/api/chat/sessions/{session_id}")
def get_chat_session(session_id: str) -> dict:
    session = db.chat_session(session_id)
    if not session:
        raise HTTPException(404, "对话不存在")
    return {**session, "messages": db.chat_messages(session_id)}


@router.patch("/api/chat/sessions/{session_id}")
def rename_chat_session(session_id: str, req: ChatSessionPatch) -> dict:
    if not db.chat_session(session_id):
        raise HTTPException(404, "对话不存在")
    db.rename_chat_session(session_id, req.title)
    return db.chat_session(session_id)


@router.delete("/api/chat/sessions/{session_id}")
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


@router.post("/api/chat/turn")
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


@router.post("/api/chat/speak")
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
