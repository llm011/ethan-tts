"""Offline tests for the CLI: no model, no server, no network."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import cli


def fake_request_factory(responses):
    calls = []

    def fake(path, method="GET", body=None, raw=False, timeout=300, base=None):
        calls.append((path, method, body))
        for prefix, value in responses.items():
            if path.startswith(prefix):
                return value
        raise cli.ApiError(f"unexpected path {path}")

    return fake, calls


def test_resolve_voice_by_id_name_and_partial(monkeypatch):
    voices = {"items": [
        {"id": "preset:Vivian", "name": "薇薇安"},
        {"id": "voice:abc", "name": "老凌的声音"},
    ]}
    fake, _ = fake_request_factory({"/api/voices": voices})
    monkeypatch.setattr(cli, "request", fake)
    assert cli.resolve_voice("voice:abc", "http://x") == "voice:abc"
    assert cli.resolve_voice("薇薇安", "http://x") == "preset:Vivian"
    assert cli.resolve_voice("老凌", "http://x") == "voice:abc"
    with pytest.raises(cli.ApiError):
        cli.resolve_voice("不存在", "http://x")


def test_resolve_voice_uses_default_setting(monkeypatch):
    fake, _ = fake_request_factory({
        "/api/voices": {"items": [
            {"id": "preset:Vivian", "name": "薇薇安"},
            {"id": "preset:Serena", "name": "塞琳娜"},
        ]},
        "/api/settings": {"default_voice_id": "preset:Serena"},
    })
    monkeypatch.setattr(cli, "request", fake)
    assert cli.resolve_voice(None, "http://x") == "preset:Serena"


def test_say_writes_wav_and_reports_headers(monkeypatch, tmp_path):
    fake, calls = fake_request_factory({
        "/api/voices": {"items": [{"id": "preset:Vivian", "name": "薇薇安"}]},
        "/api/tts": (b"RIFF" + b"\0" * 40, {
            "x-artifact-id": "abc123", "x-audio-seconds": "1.5",
            "x-gen-seconds": "0.8", "x-rtf": "0.53",
        }),
    })
    monkeypatch.setattr(cli, "request", fake)
    monkeypatch.setattr(cli, "ensure_server", lambda base, autostart: None)
    target = tmp_path / "out.wav"
    args = argparse.Namespace(
        base="http://x", json=True, autostart=True, text="你好", file=None,
        voice="薇薇安", instruct=None, temperature=.7, speed=1.0, out=str(target),
        format="wav", keep_wav=False, stream=False, play=False,
    )
    assert cli.cmd_say(args) == 0
    assert target.exists()
    payload = calls[-1][2]
    assert payload["voice_id"] == "preset:Vivian"
    assert payload["text"] == "你好"


def test_say_reads_text_file(monkeypatch, tmp_path):
    source = tmp_path / "script.txt"
    source.write_text("  从文件读取的文本。  ", encoding="utf-8")
    args = argparse.Namespace(file=str(source), text=None)
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    assert cli.read_text(args) == "从文件读取的文本。"


def test_say_rejects_empty_text(monkeypatch):
    args = argparse.Namespace(file=None, text="   ")
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    with pytest.raises(cli.ApiError):
        cli.read_text(args)


def test_stream_wav_patches_placeholder_sizes(tmp_path):
    target = tmp_path / "stream.wav"
    payload = b"RIFF" + (0xFFFFFFFF).to_bytes(4, "little") + b"\0" * 32 + \
        (0xFFFFFFFF).to_bytes(4, "little") + b"\0" * 20
    cli.write_stream_wav(payload, target)
    data = target.read_bytes()
    assert int.from_bytes(data[4:8], "little") == len(data) - 8
    assert int.from_bytes(data[40:44], "little") == len(data) - 44


def test_json_error_shape(monkeypatch, capsys):
    def boom(args):
        raise cli.ApiError("服务未运行", 503)

    parser = cli.build_parser()
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setattr(parser, "parse_args", lambda argv=None: argparse.Namespace(
        func=boom, json=True,
    ))
    assert cli.main([]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_parser_accepts_common_agent_flags():
    args = cli.build_parser().parse_args([
        "say", "你好", "--json", "--voice", "薇薇安", "-o", "a.wav", "--no-autostart",
    ])
    assert args.json is True and args.autostart is False
    assert args.voice == "薇薇安" and args.out == "a.wav"


def test_startup_guard_serializes_concurrent_spawn(tmp_path, monkeypatch):
    """并发 CLI 不应该各自 Popen 一份 app.py。"""
    import threading
    from contextlib import contextmanager

    spawns = []
    lock = threading.Lock()

    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "is_up", lambda base: len(spawns) > 0)

    def fake_spawn(base):
        with lock:
            spawns.append(base)

    monkeypatch.setattr(cli, "_spawn_server", fake_spawn)
    monkeypatch.setattr(cli, "_wait_until_up", lambda base: None)

    threads = [
        threading.Thread(target=cli.ensure_server, args=("http://127.0.0.1:8765", True))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    # 只有一个线程真正去拉起进程，其余等到锁后看到服务已在运行。
    assert len(spawns) == 1
