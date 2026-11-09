"""Implementation of the user-facing `ethan-tts <command>` handlers.

Split out of the original monolithic cli.py: the parser and the HTTP client
helpers live in `cli.py`, while each command family is grouped here.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cli_client import (
    ApiError, base_port, ensure_server, is_local, is_up, request,
)
from cli_helpers import (
    convert, emit, header, play_file, read_text, resolve_voice, write_stream_wav,
)


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
