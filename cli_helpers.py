"""Small CLI utilities: input reading, playback, transcoding, output."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from cli_client import ApiError, request


def resolve_voice(name_or_id: str | None, base: str) -> str:
    voices = request("/api/voices", base=base)["items"]
    if not name_or_id:
        settings = request("/api/settings", base=base)
        voice_id = settings["default_voice_id"]
        if voice_id and any(v["id"] == voice_id for v in voices):
            return voice_id
        return voices[0]["id"]
    for voice in voices:
        if voice["id"] == name_or_id:
            return voice["id"]
    exact = [v for v in voices if v["name"] == name_or_id]
    if len(exact) == 1:
        return exact[0]["id"]
    partial = [v for v in voices if name_or_id in v["name"]]
    if len(partial) == 1:
        return partial[0]["id"]
    if partial:
        names = "、".join(v["name"] for v in partial)
        raise ApiError(f"音色名称不唯一，请使用更完整的名称或 id：{names}")
    raise ApiError(f"找不到音色：{name_or_id}（用 `ethan-tts voices list` 查看）")


def read_text(args: argparse.Namespace) -> str:
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text == "-" or (args.text is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
    else:
        text = args.text or ""
    text = text.strip()
    if not text:
        raise ApiError("文本为空。传入文本、--file，或用 `-` 从标准输入读取。")
    return text


def play_file(path: Path) -> None:
    player = shutil.which("afplay") or shutil.which("ffplay")
    if not player:
        raise ApiError("找不到播放器（afplay / ffplay），已保存文件，请手动播放。")
    command = [player, str(path)] if player.endswith("afplay") else [
        player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path),
    ]
    subprocess.run(command, check=False)


def convert(source: Path, target: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise ApiError("需要 ffmpeg 才能导出非 WAV 格式。")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise ApiError(f"转码失败：{result.stderr.strip()[-500:]}")


def write_stream_wav(payload: bytes, target: Path) -> None:
    """Patch the placeholder RIFF sizes of a streamed WAV before saving."""
    data = bytearray(payload)
    if len(data) >= 44 and data[:4] == b"RIFF":
        data[4:8] = (len(data) - 8).to_bytes(4, "little")
        data[40:44] = (len(data) - 44).to_bytes(4, "little")
    target.write_bytes(data)


def header(headers: dict[str, str], name: str) -> str | None:
    """HTTP header lookup that ignores the case used by the server."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def emit(payload: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif human:
        print(human)


# ------------------------------------------------------------------ commands
