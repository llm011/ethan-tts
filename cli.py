"""ethan-tts command line: parser and entry point.

Layout:
  cli.py          parser + main (this file)
  cli_client.py   HTTP calls, server autostart, voice resolution
  cli_helpers.py  input reading, playback, transcoding, output
  commands/       one function per command, grouped by area

Every command supports --json; exit code 0 on success, 2 on user/API errors.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from cli_client import (
    DEFAULT_URL, STARTUP_TIMEOUT, ApiError, base_port, ensure_server, is_local,
    is_up, request, _spawn_server, _startup_guard, _wait_until_up,
)
from cli_client import DATA_DIR, LOG_DIR  # re-exported: tests and callers patch these
from cli_helpers import (
    convert, emit, header, play_file, read_text, resolve_voice, write_stream_wav,
)
from commands import (
    cmd_artifacts, cmd_chat, cmd_clone, cmd_design, cmd_logs, cmd_models,
    cmd_pronounce, cmd_serve, cmd_server, cmd_say, cmd_status, cmd_stop,
    cmd_voices,
)
from setup_cmd import cmd_doctor, cmd_setup


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
