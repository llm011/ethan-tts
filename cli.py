"""ethan-tts command line interface, designed for agents and shell scripts.

Fast path: talk to the already-warm local server (no model reload).
If the server is not running, start it detached and wait for readiness.
Every command supports --json for machine-readable output.

    ethan-tts say "你好，世界" -o hello.wav
    ethan-tts say --file script.txt --voice 老凌的声音 --json
    ethan-tts voices list --json
    ethan-tts clone --audio ref.m4a --text "参考录音逐字稿" --name 我的声音
    ethan-tts artifacts list --json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台退化为无锁
    fcntl = None  # type: ignore[assignment]

# setup / doctor 必须能在依赖坏掉时仍然跑起来，所以它们不 import engine，
# 也就不能塞进这个已经依赖 httpx 的模块里。
from setup_cmd import cmd_doctor, cmd_setup

ROOT = Path(__file__).resolve().parent
from config import DATA_DIR, LOG_DIR  # noqa: E402  (需在 ROOT 之后，避免循环)

DEFAULT_URL = os.environ.get("ETHAN_TTS_URL", "http://127.0.0.1:8765")
STARTUP_TIMEOUT = float(os.environ.get("ETHAN_TTS_STARTUP_TIMEOUT", "90"))


# ------------------------------------------------------------------ HTTP


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def request(
    path: str, method: str = "GET", body: Any = None, raw: bool = False,
    timeout: float = 300, base: str | None = None,
) -> Any:
    url = (base or DEFAULT_URL).rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read()
            if raw:
                return payload, dict(response.headers)
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except json.JSONDecodeError:
            pass
        raise ApiError(str(detail), exc.code) from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"无法连接 {url}：{exc.reason}") from exc


def is_up(base: str | None = None) -> bool:
    try:
        request("/health", timeout=3, base=base)
        return True
    except ApiError:
        return False


def base_port(base: str) -> int:
    return urllib.parse.urlsplit(base).port or 8765


def is_local(base: str) -> bool:
    host = urllib.parse.urlsplit(base).hostname or ""
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def ensure_server(base: str, autostart: bool) -> None:
    if is_up(base):
        return
    if not is_local(base):
        raise ApiError(
            f"远端服务 {base} 不可达；本机无法为远端地址启动服务，请检查该服务是否运行。"
        )
    if not autostart:
        raise ApiError(
            f"服务未运行（{base}）。先执行 `ethan-tts server start`，或加 --autostart。"
        )
    # 并发调用时两个 CLI 都会在 is_up() 判定「没在跑」，然后各自 Popen。
    # 先抢一把「启动锁」把这段串行化：抢到的去拉起进程，没抢到的等它探活成功。
    # 这样不会再出现两份 app.py 同时初始化、其中一份卡在 MLX 里吃内存的情况。
    with _startup_guard():
        if is_up(base):  # 等锁期间可能已被别人拉起来了
            return
        _spawn_server(base)
    _wait_until_up(base)


@contextmanager
def _startup_guard():
    """独占的「正在启动」标记，避免并发 CLI 各自拉起一份 app.py。"""
    guard = DATA_DIR / "startup.lock"
    guard.parent.mkdir(parents=True, exist_ok=True)
    handle = open(guard, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # 阻塞等：别人正在启动就排队
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _spawn_server(base: str) -> None:
    python = ROOT / ".venv" / "bin" / "python"
    interpreter = str(python) if python.exists() else sys.executable
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    port = str(base_port(base))
    with (LOG_DIR / "server.log").open("ab") as log:
        subprocess.Popen(
            [interpreter, str(ROOT / "app.py"), "--host", "127.0.0.1", "--port", port],
            cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def _wait_until_up(base: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if is_up(base):
            return
        time.sleep(.3)
    raise ApiError(f"服务启动超时，请查看 {LOG_DIR / 'server.log'}")


# ------------------------------------------------------------------ helpers


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


def cmd_say(args: argparse.Namespace) -> int:
    base = args.base
    ensure_server(base, args.autostart)
    text = read_text(args)
    voice_id = resolve_voice(args.voice, base)
    payload = {
        "text": text, "voice_id": voice_id, "instruct": args.instruct,
        "temperature": args.temperature, "speed": args.speed,
    }
    target = Path(args.out or f"tts-{time.strftime('%Y%m%d-%H%M%S')}.wav").expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if args.stream:
        blob, headers = request("/api/tts/stream", "POST", payload, raw=True, base=base)
        write_stream_wav(blob, target)
        meta = {"path": str(target.resolve()), "streamed": True}
    else:
        blob, headers = request("/api/tts", "POST", payload, raw=True, base=base)
        target.write_bytes(blob)
        meta = {
            "path": str(target.resolve()),
            "artifact_id": header(headers, "X-Artifact-ID"),
            "task_id": header(headers, "X-Task-ID"),
            "audio_seconds": round(float(header(headers, "X-Audio-Seconds") or 0), 2),
            "generation_seconds": round(float(header(headers, "X-Gen-Seconds") or 0), 2),
            "realtime_factor": float(header(headers, "X-RTF") or 0),
        }
    meta["voice_id"] = voice_id
    meta["wall_seconds"] = round(time.monotonic() - started, 2)

    if args.format and args.format != "wav":
        converted = target.with_suffix(f".{args.format}")
        convert(target, converted)
        if not args.keep_wav:
            target.unlink(missing_ok=True)
        target = converted
        meta["path"] = str(target.resolve())

    if args.play:
        play_file(target)
    emit(meta, args.json, human=f"已保存 {target}（{meta.get('audio_seconds', '?')} 秒音频）")
    return 0


def cmd_voices(args: argparse.Namespace) -> int:
    ensure_server(args.base, args.autostart)
    if args.action == "list":
        query = "?" + urllib.parse.urlencode({k: v for k, v in {
            "kind": args.kind, "q": args.query,
        }.items() if v})
        items = request(f"/api/voices{query}", base=args.base)["items"]
        if args.json:
            emit([{
                "id": v["id"], "name": v["name"], "kind": v["kind"],
                "description": v["description"], "speaker": v["speaker"],
                "ready": bool(v["reference_path"]) if v["kind"] == "clone" else True,
            } for v in items], True)
        else:
            for v in items:
                flag = "" if v["kind"] == "custom" else (
                    " · 待上传录音" if v["kind"] == "clone" and not v["reference_path"] else ""
                )
                print(f"{v['id']:<22} {v['name']:<14} {v['kind']:<7}{flag} {v['description']}")
        return 0
    if args.action == "rm":
        voice_id = resolve_voice(args.name, args.base)
        request(f"/api/voices/{voice_id}", "DELETE", base=args.base)
        emit({"deleted": voice_id}, args.json, human=f"已删除 {voice_id}")
        return 0
    raise ApiError("未知操作")


def cmd_clone(args: argparse.Namespace) -> int:
    ensure_server(args.base, args.autostart)
    audio = Path(args.audio).expanduser()
    if not audio.is_file():
        raise ApiError(f"参考音频不存在：{audio}")
    created = request("/api/voices", "POST", {
        "name": args.name, "kind": "clone",
        "description": args.description or "", "reference_text": args.text,
    }, base=args.base)
    voice_id = created["id"]
    boundary = "----ethan-tts-boundary"
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="reference_text"\r\n\r\n'
    body += args.text.encode() + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += audio.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{args.base.rstrip('/')}/api/voices/{voice_id}/reference",
        data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            request_result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        request(f"/api/voices/{voice_id}", "DELETE", base=args.base)
        raise ApiError(str(exc.read().decode(errors="replace")[:300]), exc.code) from exc
    emit({"id": voice_id, "name": args.name, "ready": request_result["reference_path"]},
         args.json, human=f"已创建克隆音色 {voice_id}（{args.name}）")
    return 0


def cmd_design(args: argparse.Namespace) -> int:
    ensure_server(args.base, args.autostart)
    created = request("/api/voices", "POST", {
        "name": args.name, "kind": "design", "instruct": args.instruct,
        "description": args.description or "",
    }, base=args.base)
    emit({"id": created["id"], "name": args.name}, args.json,
         human=f"已创建设计音色 {created['id']}（{args.name}）")
    return 0


def cmd_artifacts(args: argparse.Namespace) -> int:
    ensure_server(args.base, args.autostart)
    if args.action == "list":
        items = request(f"/api/artifacts?limit={args.limit}", base=args.base)["items"]
        if args.json:
            emit([{k: a[k] for k in ("id", "title", "voice_name", "audio_seconds",
                                    "created_at")} for a in items], True)
        else:
            for a in items:
                print(f"{a['id']}  {a['audio_seconds']:>5.1f}s  {a['voice_name']:<12}  {a['title']}")
        return 0
    if args.action == "get":
        target = Path(args.out or f"{args.id}.wav")
        blob, _ = request(f"/api/artifacts/{args.id}/audio", raw=True, base=args.base)
        target.write_bytes(blob)
        emit({"path": str(target.resolve())}, args.json, human=f"已下载 {target}")
        return 0
    if args.action == "rm":
        request(f"/api/artifacts/{args.id}", "DELETE", base=args.base)
        emit({"deleted": args.id}, args.json, human=f"已删除 {args.id}")
        return 0
    raise ApiError("未知操作")


def cmd_models(args: argparse.Namespace) -> int:
    ensure_server(args.base, args.autostart)
    if args.action == "list":
        items = request("/api/models", base=args.base)["items"]
        if args.json:
            emit([{"key": m["key"], "name": m["name"], "installed": m["installed"],
                   "loaded": m["loaded"]} for m in items], True)
        else:
            for m in items:
                state = "运行中" if m["loaded"] else ("已下载" if m["installed"] else "未下载")
                print(f"{m['key']:<18} {m['name']:<28} {state}")
        return 0
    if args.action == "download":
        result = request("/api/models/download", "POST", {"model_key": args.key}, base=args.base)
        emit(result, args.json, human=result.get("message", "已开始下载"))
        return 0
    raise ApiError("未知操作")


def cmd_pronounce(args: argparse.Namespace) -> int:
    ensure_server(args.base, args.autostart)
    if args.action == "list":
        items = request("/api/pronunciations", base=args.base)["items"]
        if args.json:
            emit([{k: p[k] for k in ("id", "term", "say_as", "note",
                                    "match_case", "is_enabled", "hits")} for p in items], True)
        else:
            for p in items:
                state = "" if p["is_enabled"] else "（已停用）"
                print(f"{p['id']:<20} {p['term']:<14} → {p['say_as']:<14} "
                      f"命中 {p['hits']:<4}{state}{p['note']}")
        return 0
    if args.action == "add":
        item = request("/api/pronunciations", "POST", {
            "term": args.term, "say_as": args.say_as,
            "note": args.note or "", "match_case": args.match_case,
        }, base=args.base)
        emit(item, args.json, human=f"已添加 {item['term']} → {item['say_as']}")
        return 0
    if args.action == "rm":
        items = request("/api/pronunciations", base=args.base)["items"]
        target = next((p for p in items if p["id"] == args.term), None) or \
            next((p for p in items if p["term"].lower() == args.term.lower()), None)
        if not target:
            raise ApiError(f"找不到读音规则：{args.term}")
        request(f"/api/pronunciations/{target['id']}", "DELETE", base=args.base)
        emit({"deleted": target["id"]}, args.json,
             human=f"已删除 {target['term']} → {target['say_as']}")
        return 0
    if args.action == "preview":
        text = args.text if args.text != "-" else sys.stdin.read()
        result = request("/api/pronunciations/preview", "POST", {"text": text}, base=args.base)
        emit(result, args.json, human=f"原文：{result['original']}\n实际读：{result['spoken']}")
        return 0
    raise ApiError("未知操作")


def cmd_logs(args: argparse.Namespace) -> int:
    ensure_server(args.base, args.autostart)
    query = "?" + urllib.parse.urlencode({k: v for k, v in {
        "level": args.level, "category": args.category, "limit": args.limit,
    }.items() if v})
    items = request(f"/api/logs{query}", base=args.base)["items"]
    if args.json:
        emit(items, True)
    else:
        for item in reversed(items):
            print(f"{item['created_at']}  {item['level']:<5} {item['category']:<8} {item['message']}")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """One spoken turn through the same SSE endpoint the web page uses."""
    ensure_server(args.base, getattr(args, "autostart", True))
    text = args.text
    audio_bytes = None
    if text is None:
        if args.audio:
            audio_bytes = Path(args.audio).read_bytes()
        elif not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        else:
            print("错误：需要录音文件或 --text", file=sys.stderr)
            return 2

    if args.voice:
        voices = request("/api/voices", base=args.base)["items"]
        match = next((v for v in voices if args.voice in {v["id"], v["name"]}), None)
        if not match:
            raise ApiError(f"找不到音色：{args.voice}")
        request("/api/chat/settings", "PATCH", {"voice_id": match["id"]}, base=args.base)

    session_id = args.session
    if not session_id:
        session_id = request("/api/chat/sessions", "POST", {"title": "命令行对话"},
                             base=args.base)["id"]

    # Multipart so the recording path and the text path share one endpoint.
    boundary = "----ethan-tts-chat"
    parts: list[bytes] = []
    def field(name: str, value: str) -> None:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    field("session_id", session_id)
    if text is not None:
        field("transcript", text)
    if audio_bytes is not None:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; '
                      f'filename="turn.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
                     + audio_bytes + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        (args.base or DEFAULT_URL).rstrip("/") + "/api/chat/turn", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Accept": "text/event-stream"},
    )
    transcript, reply, audio = "", [], bytearray()
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = json.loads(line.split(":", 1)[1])
                    if event == "asr":
                        transcript = data["text"]
                    elif event == "sentence":
                        reply.append(data["text"])
                    elif event == "audio":
                        audio += base64.b64decode(data["pcm"])
                    elif event == "error":
                        raise ApiError(data["message"])
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.read().decode(errors="replace")[:400], exc.code) from exc

    text_reply = "".join(reply).strip()
    path = None
    if audio and not args.no_speak:
        path = Path(args.output) if args.output else Path(f"reply-{session_id.split(':')[-1]}.wav")
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
            wav.writeframes(bytes(audio))
    if args.json:
        print(json.dumps({"ok": True, "session": session_id, "transcript": transcript,
                          "reply": text_reply, "audio": str(path) if path else None},
                         ensure_ascii=False))
    else:
        if transcript:
            print(f"你说：{transcript}")
        print(f"回答：{text_reply}")
        if path:
            print(f"音频：{path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    up = is_up(args.base)
    payload: dict[str, Any] = {"url": args.base, "server_running": up}
    if up:
        health = request("/health", base=args.base)
        overview = request("/api/overview", base=args.base)
        payload.update({
            "models": overview["models"],
            "runtime": overview["runtime"],
            "artifacts": overview["artifacts"],
            "voices": overview["voices"],
        })
    payload["ffmpeg"] = bool(shutil.which("ffmpeg"))
    if args.json:
        emit(payload, True)
    else:
        print(f"服务：{'运行中' if up else '未运行'}  {args.base}")
        if up:
            print(f"已加载模型：{', '.join(payload['runtime']['loaded_models']) or '无'}")
            print(f"Metal 活跃内存：{payload['runtime']['metal_active_gb']} GB")
            print(f"音色 {payload['voices']} 个 · 产物 {payload['artifacts']} 个")
        print(f"ffmpeg：{'可用' if payload['ffmpeg'] else '不可用（仅支持 WAV 导出）'}")
    return 0 if up else 1


def cmd_serve(args: argparse.Namespace) -> int:
    if is_up(args.base):
        emit({"running": True, "url": args.base}, args.json, human=f"服务已在运行：{args.base}")
        return 0
    ensure_server(args.base, True)
    emit({"running": True, "url": args.base}, args.json, human=f"服务已启动：{args.base}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    import signal
    pattern = f"app.py --host 127.0.0.1 --port {base_port(args.base)}"
    killed = 0
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    for pid in result.stdout.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed += 1
        except OSError:
            pass
    emit({"stopped": killed}, args.json, human=f"已停止 {killed} 个服务进程")
    return 0


# ------------------------------------------------------------------ server

def cmd_server(args: argparse.Namespace) -> int:
    """托管模式：install 之后浏览器无需先跑 CLI 也能打开。"""
    import server_cmd as server

    action = args.action
    url = args.base
    if action == "install":
        if not server.is_macos():
            raise ApiError("自动托管（开机自启）目前仅支持 macOS；其它平台可用 "
                           "`ethan-tts server start` 的 pid 模式")
        ok, detail = server.install_launchd()
        if not ok:
            raise ApiError(f"安装失败：{detail}")
        ready = server.wait_ready(url, seconds=45)
        emit({"installed": True, "plist": detail, "running": ready}, args.json,
             human=f"已安装并注册开机自启：{detail}\n"
                   f"服务{'已就绪' if ready else '正在启动，稍后用 server status 确认'}")
        return 0

    if action == "uninstall":
        ok, detail = server.uninstall_launchd()
        if not ok:
            raise ApiError(detail)
        emit({"installed": False}, args.json, human=detail)
        return 0

    if action == "start":
        if server.backend() == "launchd":
            ok, detail = server.start_launchd()
        else:
            ok, detail = server.start_pid()
        if not ok:
            raise ApiError(detail)
        ready = server.wait_ready(url, seconds=45)
        emit({"running": ready, "detail": detail}, args.json, human=f"{detail}，{'已就绪' if ready else '启动中'}")
        return 0

    if action == "stop":
        if server.backend() == "launchd":
            ok, detail = server.stop_launchd()
        else:
            ok, detail = server.stop_pid()
        if not ok:
            raise ApiError(detail)
        emit({"running": False, "detail": detail}, args.json, human=detail)
        return 0

    if action == "restart":
        if server.backend() == "launchd":
            server.stop_launchd()
            time.sleep(.5)
            ok, detail = server.start_launchd()
        else:
            server.stop_pid()
            ok, detail = server.start_pid()
        if not ok:
            raise ApiError(detail)
        ready = server.wait_ready(url, seconds=45)
        emit({"running": ready, "detail": detail}, args.json,
             human=f"已重启，{'已就绪' if ready else '启动中'}")
        return 0

    # status
    info = server.status(url)
    if args.json:
        emit(info, True)
    else:
        print(f"地址：{info['url']}")
        print(f"运行：{'是' if info['running'] else '否'}")
        print(f"托管：{info['backend']}" + ("（已安装开机自启）" if info["installed"] else "（未安装）"))
        if info.get("plist"):
            print(f"配置：{info['plist']}")
        if info.get("launchd_loaded") is not None:
            print(f"launchd：{'已加载' if info['launchd_loaded'] else '未加载'}")
        if info.get("models_loaded") is not None:
            print(f"已加载模型：{', '.join(info['models_loaded']) or '无'}")
            print(f"Metal 活跃内存：{info.get('metal_active_gb')} GB")
    return 0 if info["running"] else 1


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", default=DEFAULT_URL, help="服务地址，默认 %(default)s")
    common.add_argument("--json", action="store_true", help="输出 JSON，便于程序解析")
    common.add_argument("--no-autostart", dest="autostart", action="store_false",
                        help="服务未运行时不要自动启动")
    common.set_defaults(autostart=True)

    parser = argparse.ArgumentParser(
        prog="ethan-tts", description="本地 Qwen3-TTS 命令行：合成、克隆、管理。",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="合成语音并保存", parents=[common])
    say.add_argument("text", nargs="?", help="要合成的文本；用 - 从标准输入读取")
    say.add_argument("--file", "-f", help="从文本文件读取内容")
    say.add_argument("--voice", "-v", help="音色名称或 id，默认使用设置中的发音人")
    say.add_argument("--instruct", "-i", help="语气指令，例如：轻快一点")
    say.add_argument("--temperature", "-t", type=float, default=.7)
    say.add_argument("--speed", "-s", type=float, default=1.0,
                     help="语速倍率 0.5–2.0（保持音高，使用 ffmpeg atempo）")
    say.add_argument("--out", "-o", help="输出文件，默认 tts-<时间>.wav")
    say.add_argument("--format", choices=["wav", "mp3", "m4a"], default="wav")
    say.add_argument("--keep-wav", action="store_true", help="转码后保留原始 WAV")
    say.add_argument("--stream", action="store_true", help="边生成边写入文件")
    say.add_argument("--play", action="store_true", help="合成后立即播放")
    say.set_defaults(func=cmd_say)

    voices = sub.add_parser("voices", help="音色管理", parents=[common])
    voice_sub = voices.add_subparsers(dest="action", required=True)
    vlist = voice_sub.add_parser("list", parents=[common], help="列出音色")
    vlist.add_argument("--kind", choices=["custom", "design", "clone"])
    vlist.add_argument("--query", "-q")
    vlist.set_defaults(func=cmd_voices, action="list")
    vrm = voice_sub.add_parser("rm", parents=[common], help="删除自定义音色")
    vrm.add_argument("name")
    vrm.set_defaults(func=cmd_voices, action="rm")

    clone = sub.add_parser("clone", help="用参考录音创建克隆音色", parents=[common])
    clone.add_argument("--audio", "-a", required=True, help="参考录音文件")
    clone.add_argument("--text", "-T", required=True, help="录音的准确逐字稿")
    clone.add_argument("--name", "-n", required=True)
    clone.add_argument("--description", "-d")
    clone.set_defaults(func=cmd_clone)

    design = sub.add_parser("design", help="用文字描述创建音色", parents=[common])
    design.add_argument("--name", "-n", required=True)
    design.add_argument("--instruct", "-i", required=True, help="声音描述")
    design.add_argument("--description", "-d")
    design.set_defaults(func=cmd_design)

    artifacts = sub.add_parser("artifacts", help="音频产物管理", parents=[common])
    art_sub = artifacts.add_subparsers(dest="action", required=True)
    alist = art_sub.add_parser("list", parents=[common])
    alist.add_argument("--limit", "-l", type=int, default=20)
    alist.set_defaults(func=cmd_artifacts, action="list")
    aget = art_sub.add_parser("get", parents=[common])
    aget.add_argument("id")
    aget.add_argument("--out", "-o")
    aget.set_defaults(func=cmd_artifacts, action="get")
    arm = art_sub.add_parser("rm", parents=[common])
    arm.add_argument("id")
    arm.set_defaults(func=cmd_artifacts, action="rm")

    models = sub.add_parser("models", help="模型管理", parents=[common])
    model_sub = models.add_subparsers(dest="action", required=True)
    mlist = model_sub.add_parser("list", parents=[common])
    mlist.set_defaults(func=cmd_models, action="list")
    mdownload = model_sub.add_parser("download", parents=[common])
    mdownload.add_argument("key", choices=["custom-0.6b-4bit", "design-1.7b-4bit", "base-0.6b-4bit"])
    mdownload.set_defaults(func=cmd_models, action="download")

    logs = sub.add_parser("logs", help="查看运行日志", parents=[common])
    logs.add_argument("--level", choices=["INFO", "WARNING", "ERROR"])
    logs.add_argument("--category")
    logs.add_argument("--limit", "-l", type=int, default=30)
    logs.set_defaults(func=cmd_logs)

    pronounce = sub.add_parser("pronounce", help="读音纠正规则", parents=[common])
    pron_sub = pronounce.add_subparsers(dest="action", required=True)
    plist = pron_sub.add_parser("list", parents=[common])
    plist.set_defaults(func=cmd_pronounce, action="list")
    padd = pron_sub.add_parser("add", parents=[common])
    padd.add_argument("term", help="原文，例如 Qwen")
    padd.add_argument("say_as", help="读作，例如 千问")
    padd.add_argument("--note", "-n")
    padd.add_argument("--match-case", action="store_true")
    padd.set_defaults(func=cmd_pronounce, action="add")
    prm = pron_sub.add_parser("rm", parents=[common])
    prm.add_argument("term", help="原文或规则 id")
    prm.set_defaults(func=cmd_pronounce, action="rm")
    pprev = pron_sub.add_parser("preview", parents=[common])
    pprev.add_argument("text", nargs="?", default="-")
    pprev.set_defaults(func=cmd_pronounce, action="preview")

    chat = sub.add_parser("chat", help="语音对话：说话、转录、让模型回答", parents=[common])
    chat.add_argument("audio", nargs="?", help="录音文件（wav/m4a/mp3）；省略则从 stdin 读文本")
    chat.add_argument("--text", help="直接给文本，跳过语音识别")
    chat.add_argument("--session", help="继续某个对话 ID")
    chat.add_argument("--voice", help="朗读音色名称或 ID")
    chat.add_argument("--no-speak", action="store_true", help="只取回答，不朗读")
    chat.add_argument("-o", "--output", help="把朗读音频存成 WAV")
    chat.set_defaults(func=cmd_chat)

    setup = sub.add_parser("setup", parents=[common],
                           help="首次安装自检：检查环境并下载默认模型")
    setup.add_argument("--download", action="store_true",
                       help="直接下载默认模型，不再询问")
    setup.add_argument("--yes", "-y", action="store_true",
                       help="对所有询问一律同意（等价于 --download）")
    setup.add_argument("--no-serve", action="store_true",
                       help="配好后不要顺手启动服务")
    setup.set_defaults(func=cmd_setup)

    doctor = sub.add_parser("doctor", parents=[common],
                            help="体检：环境、依赖、模型、服务是否正常")
    doctor.set_defaults(func=cmd_doctor)

    server = sub.add_parser("server", parents=[common],
                            help="托管服务：install/start/stop/restart/status/uninstall")
    server_sub = server.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("install", "安装开机自启（macOS launchd）"),
        ("uninstall", "移除开机自启"),
        ("start", "启动服务"),
        ("stop", "停止服务"),
        ("restart", "重启服务"),
        ("status", "查看托管状态"),
    ):
        sub_parser = server_sub.add_parser(name, parents=[common], help=help_text)
        sub_parser.set_defaults(func=cmd_server, action=name)

    status = sub.add_parser("status", help="查看服务与资源状态", parents=[common])
    status.set_defaults(func=cmd_status)
    serve = sub.add_parser("serve", help="后台启动服务", parents=[common])
    serve.set_defaults(func=cmd_serve)
    stop = sub.add_parser("stop", help="停止服务", parents=[common])
    stop.set_defaults(func=cmd_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ApiError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc), "status": exc.status},
                             ensure_ascii=False))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
