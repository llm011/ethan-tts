"""Synthesis endpoints: blocking WAV, streaming WAV, and chat re-speak."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

import db
from config import ARTIFACT_DIR
from deps import ensure_voice, synthesis_params
from engine import CancelledError, QueueTimeout, engine, stream_wav_header
from schemas import SynthesisRequest
from settings_store import QUEUE_TIMEOUT_SEC

router = APIRouter()


@router.post("/api/tts")
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


@router.post("/api/tts/stream")
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
