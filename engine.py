"""MLX inference and a deliberately bounded, single-worker synthesis queue."""
from __future__ import annotations

import re
import struct
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Iterator

import mlx.core as mx
import numpy as np

import db
from config import ARTIFACT_DIR, DEFAULT_MODEL, MODEL_DIR, MODELS, SAMPLE_RATE, VOICE_DIR

_SPLIT_RE = re.compile(r"(?<=[。！？；!?;\n])")
_CANCEL_CHECK = re.compile(r"[^a-zA-Z0-9_-]")
_CJK = r"\u4e00-\u9fff"


def apply_pronunciations(text: str, rules: list[dict]) -> tuple[str, list[str]]:
    """Rewrite terms the model mispronounces, e.g. Qwen -> 千问.

    Rules are applied longest-term-first in a single left-to-right pass, so a
    replacement's output is never re-scanned by a later rule (no chains like
    Qwen -> 千问 -> 千问千问). Latin terms only match at word boundaries, which
    keeps "Qwen3" intact when the rule is plain "Qwen"... unless the term is
    itself followed by digits, in which case it matches exactly as written.
    """
    if not text or not rules:
        return text, []
    ordered = sorted(
        (r for r in rules if r.get("is_enabled", 1) and r.get("term")),
        key=lambda r: len(r["term"]), reverse=True,
    )
    if not ordered:
        return text, []
    parts: list[str] = []
    used: list[str] = []
    index = 0
    lowered = text.lower()
    while index < len(text):
        matched = None
        for rule in ordered:
            term = rule["term"]
            haystack = text if rule.get("match_case") else lowered
            needle = term if rule.get("match_case") else term.lower()
            if not haystack.startswith(needle, index):
                continue
            # Latin terms must not match inside a longer word: "Qwen" should not
            # fire on "QwenTTS" unless the rule itself says QwenTTS.
            if re.match(r"[A-Za-z0-9]", term[0]):
                before = text[index - 1] if index else ""
                after = text[index + len(term)] if index + len(term) < len(text) else ""
                if re.match(r"[A-Za-z0-9]", before) or re.match(r"[A-Za-z0-9]", after):
                    continue
            matched = rule
            break
        if matched:
            parts.append(matched["say_as"])
            used.append(matched["id"])
            index += len(matched["term"])
        else:
            parts.append(text[index])
            index += 1
    return "".join(parts), used


def split_sentences(text: str, max_len: int = 40) -> list[str]:
    """Keep generation chunks short without splitting a sentence mid-clause."""
    parts = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]
    out: list[str] = []
    for part in parts:
        if len(part) <= max_len:
            out.append(part)
            continue
        for segment in re.split(r"(?<=[，,、])", part):
            segment = segment.strip()
            while len(segment) > max_len:
                out.append(segment[:max_len])
                segment = segment[max_len:]
            if segment:
                out.append(segment)
    return out or [text.strip()]


def to_pcm16(audio: mx.array | np.ndarray) -> bytes:
    # Convert through NumPy; mlx.core.array itself has no .tobytes() method.
    samples = np.asarray(audio, dtype=np.float32)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    return pcm.tobytes()


def sentence_pause_seconds(sentence: str) -> float:
    """Natural join pause after removing the model's own edge padding."""
    if sentence.endswith(("，", ",", "、", "：", ":")):
        return .10
    if sentence.endswith(("。", "！", "？", "!", "?")):
        return .26
    if sentence.endswith(("；", ";")):
        return .20
    return .12


def trim_edge_silence(
    audio: mx.array | np.ndarray,
    sentence: str,
    trim_start: bool = True,
    trim_end: bool = True,
) -> np.ndarray:
    """Trim only generated edge padding, never silence inside speech.

    Qwen3-TTS adds roughly 300 ms at both ends of every independent segment.
    Concatenating segments otherwise produces an artificial 600 ms hole.
    A short pre-roll protects quiet consonants; punctuation controls the join.
    """
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not len(samples):
        return samples
    frame = max(1, int(SAMPLE_RATE * .01))
    frame_count = (len(samples) + frame - 1) // frame
    padded = np.pad(samples, (0, frame_count * frame - len(samples)))
    rms = np.sqrt(np.mean(padded.reshape(frame_count, frame) ** 2, axis=1))
    active = np.flatnonzero(rms >= 10 ** (-45 / 20))
    if not len(active):
        return np.zeros(int(SAMPLE_RATE * sentence_pause_seconds(sentence)), dtype=np.float32) if trim_end else samples[:0]

    start = 0
    end = len(samples)
    if trim_start:
        first_active = int(active[0] * frame)
        start = max(0, first_active - int(SAMPLE_RATE * .03))
    if trim_end:
        last_active = min(len(samples), int((active[-1] + 1) * frame))
        end = last_active
    trimmed = samples[start:end].copy()
    if trim_end:
        # A tiny fade prevents clicks when the active frame ends non-zero.
        fade = min(len(trimmed), int(SAMPLE_RATE * .005))
        if fade:
            trimmed[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        pause = np.zeros(int(SAMPLE_RATE * sentence_pause_seconds(sentence)), dtype=np.float32)
        trimmed = np.concatenate((trimmed, pause))
    return trimmed


def stream_wav_header() -> bytes:
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
        + b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


def change_speed(samples: np.ndarray, speed: float) -> np.ndarray:
    """Change tempo without changing pitch.

    Qwen3-TTS has no native speed parameter (its `speed` argument is documented
    as "not directly supported yet"). Resampling the token stream would also
    distort timbre, so tempo is adjusted after generation with ffmpeg atempo,
    which preserves pitch and formants.
    """
    if speed <= 1.0 + 1e-3 and speed >= 1.0 - 1e-3:
        return samples
    speed = max(.5, min(2.0, float(speed)))
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("ffmpeg"):
        raise RuntimeError("语速调整需要 ffmpeg；请先安装，或使用 speed=1.0")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.wav"
        target = Path(tmp) / "out.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        # atempo accepts 0.5–2.0 per instance; chain filters outside that range.
        filters = []
        remaining = speed
        while remaining > 2.0:
            filters.append("atempo=2.0")
            remaining /= 2.0
        while remaining < .5:
            filters.append("atempo=0.5")
            remaining /= .5
        filters.append(f"atempo={remaining:.6f}")
        process = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-filter:a", ",".join(filters), str(target)],
            capture_output=True, text=True,
        )
        if process.returncode:
            raise RuntimeError(f"语速调整失败：{process.stderr.strip()[-400:]}")
        with wave.open(str(target), "rb") as wav:
            return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768


def model_path(model_key: str) -> Path:
    if model_key not in MODELS:
        raise ValueError("不支持的模型")
    return MODEL_DIR / MODELS[model_key]["directory"]


def model_installed(model_key: str) -> bool:
    path = model_path(model_key)
    return path.is_dir() and any(path.glob("*.safetensors"))


class CancelledError(RuntimeError):
    pass


@dataclass
class SynthesisParams:
    text: str
    voice: dict
    model_key: str
    instruct: str | None = None
    temperature: float = 0.7
    top_k: int = 50
    top_p: float = 1.0
    repetition_penalty: float = 1.05
    max_tokens: int | None = None
    sentence_chars: int = 40
    streaming_interval: float = 0.32
    stream: bool = False
    speed: float = 1.0
    spoken_text: str | None = None
    pronunciation_ids: list[str] = field(default_factory=list)

    def speakable_text(self) -> str:
        return self.spoken_text if self.spoken_text is not None else self.text


class TTSEngine:
    def __init__(self) -> None:
        self._models: dict[str, object] = {}
        self._lock = threading.RLock()
        self._queue: Queue[str | None] = Queue()
        self._cancelled: set[str] = set()
        self._worker: threading.Thread | None = None
        self.default_model = DEFAULT_MODEL

    # ----- models and system resources ---------------------------------
    def load(self, model_key: str):
        with self._lock:
            if model_key in self._models:
                return self._models[model_key]
            if not model_installed(model_key):
                raise FileNotFoundError(f"模型未下载：{MODELS[model_key]['name']}")
            # Keep only the selected model resident. Switching models must not
            # temporarily hold two weight sets in the 24 GB machine's memory.
            self._models.clear()
            mx.clear_cache()
            from mlx_audio.tts.utils import load_model

            started = time.monotonic()
            model = load_model(str(model_path(model_key)))
            self._models[model_key] = model
            db.log("INFO", "model", "模型加载完成", metadata={
                "model": model_key, "seconds": round(time.monotonic() - started, 2),
            })
            return model

    def unload(self) -> None:
        with self._lock:
            self._models.clear()
            mx.clear_cache()
        db.log("INFO", "model", "模型已卸载，缓存已清理")

    def resource_snapshot(self) -> dict:
        return {
            "loaded_models": list(self._models),
            "metal_active_gb": round(mx.get_active_memory() / 1e9, 2),
            "metal_peak_gb": round(mx.get_peak_memory() / 1e9, 2),
            "queue_size": self._queue.qsize(),
            "sample_rate": SAMPLE_RATE,
        }

    # ----- synthesis ----------------------------------------------------
    def _generate(self, model, p: SynthesisParams, sentence: str, stream: bool):
        options = dict(
            language=p.voice.get("language") or "Chinese", instruct=p.instruct or None,
            temperature=p.temperature, top_k=p.top_k, top_p=p.top_p,
            repetition_penalty=p.repetition_penalty, max_tokens=p.max_tokens,
            stream=stream, streaming_interval=p.streaming_interval,
        )
        if p.voice["kind"] == "design":
            options["text"] = sentence
            return model.generate_voice_design(**options)
        if p.voice["kind"] == "clone":
            options.pop("instruct")
            options["lang_code"] = options.pop("language")
            return model.generate(
                text=sentence, ref_audio=str(VOICE_DIR / p.voice["reference_path"]),
                ref_text=p.voice["reference_text"], split_pattern="\n", **options,
            )
        options["text"] = sentence
        options["speaker"] = p.voice["speaker"]
        return model.generate_custom_voice(**options)

    def synthesize(self, p: SynthesisParams, cancelled=None) -> Iterator[bytes]:
        """Yield complete audio per sentence in sync mode, small chunks in stream mode."""
        # MLX generation and the model registry are not safe for concurrent
        # requests. A single inference at a time also bounds peak memory.
        with self._lock:
            model = self.load(p.model_key)
            for sentence in split_sentences(p.speakable_text(), p.sentence_chars):
                if cancelled and cancelled():
                    raise CancelledError("任务已取消")
                results = self._generate(model, p, sentence, p.stream)
                if not p.stream:
                    for result in results:
                        trimmed = trim_edge_silence(result.audio, sentence)
                        if p.speed != 1.0:
                            # Keep the join pause intact; only the speech is
                            # time-stretched, so the pause is re-appended after.
                            pause = int(SAMPLE_RATE * sentence_pause_seconds(sentence))
                            speech = trimmed[:-pause] if pause and len(trimmed) > pause else trimmed
                            stretched = change_speed(speech, p.speed)
                            trimmed = np.concatenate((
                                stretched, np.zeros(pause, dtype=np.float32),
                            ))
                        yield to_pcm16(trimmed)
                else:
                    # One-chunk lookahead lets us trim only the first and last
                    # chunks without cutting natural pauses inside the segment.
                    pending: np.ndarray | None = None
                    for result in results:
                        if pending is None:
                            first = trim_edge_silence(result.audio, sentence, trim_end=False)
                            if not len(first):
                                continue
                            pending = first
                            continue
                        yield to_pcm16(pending)
                        pending = np.asarray(result.audio, dtype=np.float32).reshape(-1)
                    if pending is not None:
                        yield to_pcm16(trim_edge_silence(
                            pending, sentence, trim_start=False, trim_end=True,
                        ))
                if cancelled and cancelled():
                    raise CancelledError("任务已取消")

    def generate_file(self, p: SynthesisParams, task_id: str | None = None) -> dict:
        started = time.monotonic()
        chunks: list[bytes] = []
        for chunk in self.synthesize(p, lambda: self.is_cancelled(task_id)):
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            raise RuntimeError("模型没有生成音频")
        artifact_id = uuid.uuid4().hex[:12]
        filename = f"{artifact_id}.wav"
        path = ARTIFACT_DIR / filename
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(raw)
        generation = time.monotonic() - started
        audio_seconds = len(raw) / 2 / SAMPLE_RATE
        return {
            "id": artifact_id, "task_id": task_id, "filename": filename,
            "title": p.text[:28] + ("…" if len(p.text) > 28 else ""),
            "text": p.text, "voice_id": p.voice["id"], "voice_name": p.voice["name"],
            "model_key": p.model_key, "audio_seconds": round(audio_seconds, 3),
            "generation_seconds": round(generation, 3), "size_bytes": path.stat().st_size,
            "sample_rate": SAMPLE_RATE, "params": db.dumps({
                "instruct": p.instruct, "temperature": p.temperature, "top_k": p.top_k,
                "top_p": p.top_p, "repetition_penalty": p.repetition_penalty,
                "speed": p.speed,
            }), "created_at": db.now(),
        }

    # ----- bounded background queue ------------------------------------
    def start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="tts-worker", daemon=True)
        self._worker.start()

    def stop_worker(self) -> None:
        self._queue.put(None)

    def enqueue(self, task_id: str) -> None:
        self._queue.put(task_id)

    def cancel(self, task_id: str | None) -> None:
        if task_id:
            self._cancelled.add(task_id)
            current = db.task(task_id)
            if current and current["status"] == "queued":
                db.update_task(task_id, status="cancelled", finished_at=db.now())

    def is_cancelled(self, task_id: str | None) -> bool:
        return bool(task_id and task_id in self._cancelled)

    def _run(self) -> None:
        while True:
            task_id = self._queue.get()
            if task_id is None:
                return
            current = db.task(task_id)
            if not current or current["status"] == "cancelled":
                continue
            try:
                started = time.monotonic()
                db.update_task(task_id, status="running", started_at=db.now())
                speaker = db.voice_for_engine(current["voice_id"])
                if not speaker:
                    raise ValueError("音色不存在或已被删除")
                settings = db.get_settings()
                params = current["params"]
                spoken, used = apply_pronunciations(
                    current["text"], db.list_pronunciations(enabled_only=True),
                )
                result = self.generate_file(SynthesisParams(
                    text=current["text"], voice=speaker, model_key=current["model_key"],
                    instruct=params.get("instruct"), temperature=params.get("temperature", .7),
                    top_k=params.get("top_k", 50), top_p=params.get("top_p", 1),
                    repetition_penalty=params.get("repetition_penalty", 1.05),
                    max_tokens=params.get("max_tokens"),
                    sentence_chars=settings["sentence_chars"],
                    streaming_interval=settings["streaming_interval"],
                    speed=params.get("speed", 1.0),
                    spoken_text=spoken, pronunciation_ids=used,
                ), task_id)
                db.add_artifact(result)
                db.bump_pronunciation_hits(used)
                db.update_task(task_id, status="succeeded", artifact_id=result["id"],
                               finished_at=db.now(), duration_seconds=round(time.monotonic() - started, 3))
                db.log("INFO", "task", "合成任务完成", task_id, {
                    "artifact_id": result["id"], "audio_seconds": result["audio_seconds"],
                    "generation_seconds": result["generation_seconds"],
                })
            except CancelledError:
                db.update_task(task_id, status="cancelled", finished_at=db.now())
                db.log("INFO", "task", "合成任务已取消", task_id)
            except Exception as exc:  # preserve worker for subsequent jobs
                db.update_task(task_id, status="failed", error=str(exc)[:1000], finished_at=db.now())
                db.log("ERROR", "task", f"合成任务失败：{exc}", task_id)
            finally:
                self._cancelled.discard(task_id)


engine = TTSEngine()
