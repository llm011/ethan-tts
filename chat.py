"""Voice chat orchestration: one spoken turn from recording to speech.

The pipeline is deliberately three concurrent stages instead of one
request/response chain, because each local model is slow relative to a spoken
sentence:

  ASR (once)          LLM stream (token by token)        TTS (sentence by sentence)
  record -> text  ->  text deltas -> sentence queue  ->  PCM chunks -> player

The LLM is read incrementally and split as soon as a clause is complete, so
the first audio chunk usually arrives while the model is still generating the
second sentence. Nothing waits for the full reply, and a user pressing stop
cancels the model request and the synthesis still queued behind it.
"""
from __future__ import annotations

import base64
import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import db
import engine as engine_module
import llm
import stt
from config import ASR_MODELS, SAMPLE_RATE, TTS_MODELS

# ---------------------------------------------------------------- text cleanup
# Sentence ends that can be spoken without waiting for the next word.
_HARD_END = "。！？!?…\n"
# Softer clause ends: used only once speech has started, where a shorter
# clause is worth more than a perfect prosody break.
_SOFT_END = "，,、；;：:"
_BOUNDARIES = _HARD_END + _SOFT_END

# Markdown and markup the LLM may still emit despite the system prompt. It is
# stripped for speech because a model asked to read "**重点**" says "星号星号".
_MARKDOWN_PATTERNS = [
    (re.compile(r"```[\s\S]*?```"), ""),                          # fenced code
    (re.compile(r"`([^`]*)`"), r"\1"),                            # inline code
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),                    # images
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),                # links -> label
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE), ""),         # headings
    (re.compile(r"^\s*[-*+•]\s+", re.MULTILINE), ""),             # bullets
    (re.compile(r"^\s*>\s?", re.MULTILINE), ""),                  # quotes
    (re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"), r"\2"),        # bold
    (re.compile(r"(\*|_)(?=\S)(.+?)(?<=\S)\1"), r"\2"),           # italic
    (re.compile(r"~~(.+?)~~"), r"\1"),                            # strike
    (re.compile(r"<[^>]+>"), ""),                                 # stray html
    (re.compile(r"\|\s*:?-{2,}:?\s*"), " "),                      # table rules
    (re.compile(r"[ \t]+"), " "),
]
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE0F]+"
)


def strip_markdown(text: str) -> str:
    for pattern, replacement in _MARKDOWN_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def to_speech_text(text: str) -> str:
    """Text worth reading aloud: markdown removed, symbols and emoji dropped."""
    text = strip_markdown(text or "")
    text = _EMOJI.sub("", text)
    text = re.sub(r"[#*_`~|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_ready(text: str, *, first: bool, min_chars: int = 6, max_chars: int = 60,
                first_chars: int = 8) -> tuple[list[str], str]:
    """Split ``text`` into finished clauses plus the unfinished tail.

    ``first`` lowers the bar for the very first chunk so audio starts ASAP.
    A full stop always ends a clause, because waiting would only add latency;
    a comma is a weaker signal, so it is used only once the clause is long
    enough to sound natural on its own. The tail is what the next delta
    prepends to.
    """
    # A comma break under ~10 Chinese characters sounds clipped, so the soft
    # limit stays well above the minimum that makes a hard break worthwhile.
    soft_limit = first_chars if first else max(min_chars, 10)
    parts: list[str] = []
    start = 0
    index = 0
    total = len(text)
    while index < total:
        char = text[index]
        cut = None
        length = index + 1 - start
        if char in _HARD_END and length >= 2:
            cut = index + 1
        elif char in _SOFT_END and length >= soft_limit:
            cut = index + 1
        elif length >= max_chars:
            # No punctuation for a long stretch: cut at the last space, or hard
            # cut so one runaway sentence cannot delay all audio.
            space = text.rfind(" ", start, index + 1)
            cut = space + 1 if space + 1 - start >= 2 else index + 1
        if cut is None:
            index += 1
            continue
        parts.append(text[start:cut])
        start = cut
        index = cut
    return [p for p in parts if p.strip()], text[start:]


# ---------------------------------------------------------------- the turn
@dataclass
class ChatTurn:
    """Shared state for one turn; the page aborts its own HTTP request to stop."""
    session_id: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    # Sentences that already reached the player. Kept here (not in a local) so a
    # disconnected client can still leave a readable partial transcript.
    spoken: list[str] = field(default_factory=list)
    model: str = ""
    started: float = 0.0
    saved: bool = False

    def is_cancelled(self) -> bool:
        return self.cancelled.is_set()


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def stored_api_key() -> str:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='llm_api_key'").fetchone()
    return db.loads(row["value"], "") if row else ""


def chat_config(settings: dict[str, Any] | None = None) -> llm.ChatConfig:
    settings = settings or db.get_chat_settings()
    return llm.ChatConfig(
        base_url=settings["llm_base_url"],
        api_key=stored_api_key(),
        model=settings["llm_model"],
        temperature=float(settings["llm_temperature"]),
        max_tokens=int(settings["llm_max_tokens"]),
        use_proxy=bool(settings.get("llm_use_proxy")),
        proxy_url=str(settings.get("llm_proxy_url") or ""),
    )


def build_messages(session_id: str, settings: dict[str, Any] | None = None) -> list[dict[str, str]]:
    settings = settings or db.get_chat_settings()
    prompt = (settings.get("system_prompt") or "").strip()
    history = db.chat_turns(session_id, limit=int(settings["history_turns"]))
    return ([{"role": "system", "content": prompt}] if prompt else []) + history


def run_turn(session_id: str, wav_path=None, transcript: str | None = None,
             input_mode: str = "voice",
             config: llm.ChatConfig | None = None) -> Iterator[bytes]:
    """Execute one turn and yield SSE frames.

    The page stops a turn by aborting its request, which surfaces here as
    ``GeneratorExit``. That must cancel the LLM thread immediately instead of
    letting it burn tokens, and it should still leave the partial reply saved.
    """
    turn = ChatTurn(session_id=session_id)
    try:
        yield from _run_turn(turn, session_id, wav_path, transcript, input_mode, config)
    except GeneratorExit:
        turn.cancelled.set()
        _save_partial(turn)
        raise


def _save_partial(turn: ChatTurn) -> None:
    """Persist what was already spoken when the client hangs up."""
    if turn.saved or not turn.spoken:
        return
    turn.saved = True
    try:
        db.add_chat_message(turn.session_id, "assistant", "".join(turn.spoken),
                            llm_model=turn.model,
                            elapsed=round(time.monotonic() - turn.started, 2))
        db.log("INFO", "chat", "对话被中断，已保存已朗读部分",
               metadata={"session": turn.session_id})
    except Exception as exc:  # never let bookkeeping break the response
        db.log("ERROR", "chat", f"保存中断对话失败：{exc}")


def _run_turn(turn: ChatTurn, session_id: str, wav_path, transcript: str | None,
              input_mode: str, config: llm.ChatConfig | None) -> Iterator[bytes]:
    """The actual pipeline; ``run_turn`` owns cancellation and cleanup."""
    settings = db.get_chat_settings()
    config = config or chat_config(settings)
    if not config.configured:
        yield sse("error", {"message": "还没有配置模型，请先到设置页的「模型设置」填写",
                            "action": "open_settings"})
        return
    turn.model = config.model
    turn.started = started = time.monotonic()

    # --- 1. speech -> text ------------------------------------------------
    asr_model = asr_seconds = asr_elapsed = None
    if transcript is None:
        asr_key = settings["asr_model"]
        if asr_key not in ASR_MODELS:
            yield sse("error", {"message": "语音识别模型配置无效，请在设置页重新选择"})
            return
        if not stt.asr_model_installed(asr_key):
            yield sse("error", {
                "message": f"还没有下载语音识别模型（{ASR_MODELS[asr_key]['name']}）",
                "action": "download_model", "model_key": asr_key,
            })
            return
        yield sse("status", {"stage": "asr"})
        try:
            result = stt.stt.transcribe(wav_path, asr_key)
        except Exception as exc:  # model load / decode failures reach the page
            db.log("ERROR", "chat", f"语音识别失败：{exc}")
            yield sse("error", {"message": f"语音识别失败：{exc}"})
            return
        transcript = result["text"].strip()
        asr_model, asr_seconds, asr_elapsed = (
            asr_key, result["seconds"], result["elapsed"])
        yield sse("asr", {"text": transcript, "seconds": asr_seconds,
                          "elapsed": asr_elapsed, "model_key": asr_model})
        if not transcript:
            yield sse("done", {"empty": True, "message": "没有听清，再说一次？"})
            return
    transcript = transcript.strip()
    if not transcript:
        yield sse("error", {"message": "内容是空的"})
        return

    user_message = db.add_chat_message(
        session_id, "user", transcript, input_mode=input_mode,
        asr_model=asr_model, asr_seconds=asr_seconds, asr_elapsed=asr_elapsed,
    )
    yield sse("user", user_message)
    _maybe_title(session_id, transcript)

    # --- 2. LLM -> sentence queue ----------------------------------------
    sentences: queue.Queue[str | None] = queue.Queue()
    failure: list[str] = []
    produced = threading.Event()

    def produce() -> None:
        buffer = ""
        first = True
        try:
            for delta in config.generate(build_messages(session_id, settings),
                                         cancel=turn.is_cancelled):
                if turn.is_cancelled():
                    break
                buffer += delta
                parts, buffer = split_ready(
                    buffer, first=first,
                    min_chars=int(settings["tts_min_chars"]),
                    max_chars=int(settings["tts_max_chars"]),
                    first_chars=int(settings["tts_first_chars"]),
                )
                for part in parts:
                    sentences.put(part)
                    first = False
            if buffer.strip() and not turn.is_cancelled():
                sentences.put(buffer)
        except Exception as exc:  # reported after the queue drains
            failure.append(str(exc))
        finally:
            produced.set()
            sentences.put(None)

    threading.Thread(target=produce, name="chat-llm", daemon=True).start()
    yield sse("status", {"stage": "thinking"})

    # --- 3. TTS, one sentence at a time ----------------------------------
    voice = db.voice_for_engine(settings["voice_id"])
    if voice is None:
        yield sse("error", {"message": "朗读音色不存在，请在设置页重新选择"})
        return
    model_key = engine_module.resolve_model_key(voice)
    speech_ready = engine_module.model_installed(model_key)
    if not speech_ready:
        yield sse("error", {
            "message": f"还没有下载朗读模型（{TTS_MODELS.get(model_key, {}).get('name', model_key)}）",
            "action": "download_model", "model_key": model_key,
        })
        # Keep draining the LLM so the text still lands in the transcript.
    params = engine_module.speech_params(
        voice, sentence_chars=int(settings["tts_max_chars"]),
        speed=float(settings["speech_rate"]),
    )

    collected = turn.spoken
    index = 0
    first_audio = True
    if speech_ready:
        # 第一句要等 TTS 模型加载（冷启动可达 10 秒），这一步必须让页面知道，
        # 否则用户只看到图标转圈，不知道是在加载还是在卡死。
        yield sse("status", {"stage": "tts"})
    while True:
        if turn.is_cancelled():
            break
        try:
            sentence = sentences.get(timeout=.25)
        except queue.Empty:
            continue
        if sentence is None:
            break
        spoken = to_speech_text(sentence)
        if not spoken:
            continue
        collected.append(spoken)
        yield sse("sentence", {"index": index, "text": spoken})
        if speech_ready:
            try:
                for chunk in engine_module.speak_stream(params, spoken, turn.is_cancelled):
                    if first_audio:
                        first_audio = False
                        yield sse("first_audio", {"ms": int((time.monotonic() - started) * 1000)})
                    yield sse("audio", {"index": index, "rate": SAMPLE_RATE,
                                        "pcm": base64.b64encode(chunk).decode()})
            except engine_module.CancelledError:
                break
            except Exception as exc:
                db.log("ERROR", "chat", f"朗读失败：{exc}")
                yield sse("warn", {"message": f"朗读失败：{exc}"})
            if turn.is_cancelled():
                break
            yield sse("pause", {"index": index,
                                "seconds": engine_module.sentence_pause_seconds(spoken)})
        index += 1

    # Drain the LLM thread: on stop it must not keep burning tokens, and a
    # partially spoken reply should still be saved.
    if turn.is_cancelled():
        produced.wait(timeout=2)
    while not sentences.empty():
        try:
            sentences.get_nowait()
        except queue.Empty:
            break

    reply = "".join(turn.spoken).strip()
    if failure:
        db.log("ERROR", "chat", f"模型调用失败：{failure[0]}")
        turn.saved = True
        db.add_chat_message(session_id, "assistant", reply or "（模型没有返回内容）",
                            llm_model=config.model, error=failure[0],
                            elapsed=round(time.monotonic() - started, 2))
        yield sse("error", {"message": failure[0]})
        return

    turn.saved = True
    assistant = db.add_chat_message(
        session_id, "assistant", reply or "（模型没有返回内容）",
        llm_model=config.model, elapsed=round(time.monotonic() - started, 2),
    )
    db.log("INFO", "chat", "语音对话完成", metadata={
        "session": session_id, "model": config.model, "sentences": index,
        "seconds": assistant["elapsed"], "cancelled": turn.is_cancelled(),
    })
    yield sse("done", {"message": assistant, "sentences": index,
                       "seconds": assistant["elapsed"], "cancelled": turn.is_cancelled()})


def _maybe_title(session_id: str, transcript: str) -> None:
    """Name a new conversation after its first question."""
    session = db.chat_session(session_id)
    if not session or session["title"] != "新的对话":
        return
    title = re.sub(r"\s+", " ", transcript).strip()
    db.rename_chat_session(session_id, title[:24] + ("…" if len(title) > 24 else ""))
