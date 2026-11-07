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
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
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
            f"服务未运行（{base}）。先执行 `ethan-tts serve`，或加 --autostart。"
        )
    python = ROOT / ".venv" / "bin" / "python"
    interpreter = str(python) if python.exists() else sys.executable
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    port = str(base_port(base))
    with (log_dir / "server.log").open("ab") as log:
        subprocess.Popen(
            [interpreter, str(ROOT / "app.py"), "--host", "127.0.0.1", "--port", port],
            cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if is_up(base):
            return
        time.sleep(.3)
    raise ApiError(f"服务启动超时，请查看 {log_dir / 'server.log'}")


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
