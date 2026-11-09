"""Local speech-to-text for the voice chat page.

Both backends are MLX models loaded through ``mlx_audio.stt`` and run on the
same machine as synthesis, so recorded audio never leaves the Mac:

- ``qwen3-asr-0.6b-4bit`` (default): 0.6B multilingual ASR, strong on Chinese
  and English and the better fit for a short conversational turn.
- ``whisper-turbo-4bit``: Whisper large-v3 turbo, a solid fallback that keeps
  behaving predictably on noisy or silence-padded input.

Only one ASR model is kept resident at a time, mirroring the TTS engine: the
previous weight set is dropped before another one is loaded.
"""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path
from typing import Any

import db
from config import ASR_MODELS, MODEL_ROOTS, MODEL_DIR, STT_SAMPLE_RATE

# A browser may send a leading/silent-only turn; transcribing it would make the
# LLM answer a non-question, so turns below this length are refused.
MIN_TURN_SECONDS = .35


def asr_model_path(model_key: str) -> Path:
    """Same root search as the TTS models: an upgrade may leave the weights in
    either the user directory or the source checkout."""
    if model_key not in ASR_MODELS:
        raise ValueError("不支持的语音识别模型")
    name = ASR_MODELS[model_key]["directory"]
    for root in MODEL_ROOTS:
        candidate = root / name
        if any(candidate.glob("*.safetensors")):
            return candidate
    return MODEL_ROOTS[0] / name


# A weights-only checkout is not usable: every backend here also needs its
# tokenizer/feature extractor next to the safetensors file.
_REQUIRED_FILES = {
    "qwen3-asr": ("config.json", "vocab.json"),
    "whisper": ("config.json", "tokenizer.json", "preprocessor_config.json"),
}


def asr_model_installed(model_key: str) -> bool:
    if model_key not in ASR_MODELS:
        return False
    path = asr_model_path(model_key)
    if not (path.is_dir() and any(path.glob("*.safetensors"))):
        return False
    return all((path / name).is_file() for name in _REQUIRED_FILES[ASR_MODELS[model_key]["family"]])


def asr_model_size_bytes(model_key: str) -> int:
    """Real size on disk; downloader caches are excluded from the total."""
    path = asr_model_path(model_key)
    if not path.is_dir():
        return 0
    return sum(
        f.stat().st_size for f in path.rglob("*")
        if f.is_file() and ".cache" not in f.parts
    )


class STTError(RuntimeError):
    pass


def resample(samples, source_rate: int, target_rate: int):
    """Linear resampling is enough for recognition input."""
    import numpy as np

    if source_rate == target_rate or not len(samples):
        return samples
    duration = len(samples) / source_rate
    target = int(round(duration * target_rate))
    source_index = np.arange(target) * (source_rate / target_rate)
    return np.interp(source_index, np.arange(len(samples)), samples).astype(np.float32)


def read_wav_mono(path: Path) -> tuple[Any, float]:
    """Return (float32 samples in [-1, 1], seconds) from a 16 bit WAV."""
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise STTError("录音格式不受支持，请使用 16 bit PCM")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples = resample(samples, rate, STT_SAMPLE_RATE)
    return samples, len(samples) / STT_SAMPLE_RATE


class SpeechToText:
    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self._lock = threading.RLock()

    def loaded_key(self) -> str | None:
        return next(iter(self._models), None)

    def load(self, model_key: str):
        with self._lock:
            if model_key in self._models:
                return self._models[model_key]
            if not asr_model_installed(model_key):
                raise STTError(f"请先下载语音识别模型：{ASR_MODELS[model_key]['name']}")
            from mlx_audio.stt import utils as stt_utils

            self._models.clear()
            started = time.monotonic()
            model = stt_utils.load(str(asr_model_path(model_key)))
            self._models[model_key] = model
            db.log("INFO", "stt", "语音识别模型加载完成", metadata={
                "model": model_key, "seconds": round(time.monotonic() - started, 2),
            })
            return model

    def unload(self) -> None:
        with self._lock:
            if not self._models:
                return
            self._models.clear()
        import mlx.core as mx

        mx.clear_cache()
        db.log("INFO", "stt", "语音识别模型已卸载")

    def transcribe(self, path: Path, model_key: str, language: str | None = None,
                   hotwords: list[str] | None = None) -> dict:
        """Transcribe one recorded turn. Blocking; call it from a worker thread."""
        if model_key not in ASR_MODELS:
            raise ValueError("不支持的语音识别模型")
        if not path.is_file():
            raise STTError("录音文件不存在或不完整")
        samples, seconds = read_wav_mono(path)
        if seconds < MIN_TURN_SECONDS:
            return {"text": "", "seconds": round(seconds, 2), "language": None,
                    "model_key": model_key, "elapsed": 0.0, "skipped": True}
        started = time.monotonic()
        with self._lock:
            model = self.load(model_key)
            result = self._generate(model, samples, ASR_MODELS[model_key], language, hotwords)
        text = (getattr(result, "text", "") or "").strip()
        return {
            "text": text,
            "seconds": round(seconds, 2),
            "language": getattr(result, "language", None),
            "model_key": model_key,
            "elapsed": round(time.monotonic() - started, 2),
            "skipped": False,
        }

    @staticmethod
    def _generate(model, samples, spec: dict, language: str | None,
                  hotwords: list[str] | None):
        options: dict = {"hotwords": hotwords} if hotwords else {}
        if spec["family"] == "qwen3-asr":
            return model.generate(
                samples, language=language or spec.get("language"),
                min_chunk_duration=.5, verbose=False, **options,
            )
        return model.generate(
            samples, language=language, verbose=None, return_timestamps=False,
            condition_on_previous_text=False, **options,
        )


stt = SpeechToText()
