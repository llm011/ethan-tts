"""Request/response models for the HTTP API.

Kept in one place so route modules never import each other just to reuse a
model, and so the API contract can be read top to bottom.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
