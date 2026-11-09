"""Shared request-time helpers used by several route modules.

These existed as private functions inside app.py; they are pulled out so the
voice and chat routers can reuse the same validation instead of duplicating
it (an earlier version had two subtly different copies).
"""
from __future__ import annotations

from fastapi import HTTPException

import db
from config import MIN_REFERENCE_SECONDS, MODELS
from engine import (
    SynthesisParams, apply_pronunciations, model_installed,
)
from schemas import SynthesisRequest


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
