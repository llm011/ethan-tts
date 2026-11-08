"""`ethan-tts server ...` 托管命令。

这些用例**不碰真实的 launchd**：plist 生成、命令组装、分支选择都是纯函数，
用 monkeypatch 断言行为；真实 bootout/bootstrap 只在手动验证时跑。
"""
from __future__ import annotations

import argparse
import json
import plistlib
from pathlib import Path

import pytest

import cli
import server_cmd as server


def test_plist_is_valid_and_has_keepalive(monkeypatch):
    monkeypatch.setattr(server, "_command", lambda: [
        "/usr/bin/python3", "/tmp/app.py", "--host", "127.0.0.1", "--port", "8765",
    ])
    monkeypatch.setattr(server, "_environment", lambda: {"ETHAN_TTS_DIR": "/tmp/x"})
    raw = server.plist_content()
    parsed = plistlib.loads(raw.encode())

    assert parsed["Label"] == server.LABEL
    # 不能随安装时的 cwd 走：在 /tmp 装过会把工作目录固化成 /private/tmp
    assert parsed["WorkingDirectory"] == str(server.HOME_DIR)
    assert parsed["ProgramArguments"][-2:] == ["--port", "8765"]
    # 登录自启 + 崩溃重启是这次的两个核心诉求
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["EnvironmentVariables"]["ETHAN_TTS_DIR"] == "/tmp/x"
    assert parsed["StandardErrorPath"].endswith("server.log")
    # 日志必须在 home 下：外部卷上 launchd 打不开日志会 EX_CONFIG 78
    assert "LaunchAgents" not in parsed["StandardErrorPath"]
    assert str(Path.home()) in parsed["StandardErrorPath"]


def test_plist_escapes_special_characters(monkeypatch):
    monkeypatch.setattr(server, "_command", lambda: ["/p y/app.py", "--x", "a&b<c>"])
    monkeypatch.setattr(server, "_environment", lambda: {})
    parsed = plistlib.loads(server.plist_content().encode())
    # XML 特殊字符必须被转义，否则 launchd 读不了整份配置
    assert parsed["ProgramArguments"] == ["/p y/app.py", "--x", "a&b<c>"]


def test_command_prefers_source_app_py(monkeypatch, tmp_path):
    fake_app = tmp_path / "app.py"
    fake_app.write_text("")
    monkeypatch.setattr(server, "_app_script", lambda: fake_app)
    cmd = server._command()
    assert str(fake_app) in cmd
    assert cmd[-4:] == ["--host", "127.0.0.1", "--port", str(server.default_port())]


def test_command_falls_back_to_module(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_app_script", lambda: tmp_path / "missing.py")
    cmd = server._command()
    assert "-m" in cmd and "app" in cmd


def test_status_reports_running_and_backend(monkeypatch):
    monkeypatch.setattr(server, "probe", lambda url: {
        "ok": True, "models": {"loaded_models": ["custom-0.6b-4bit"],
                               "metal_active_gb": 1.76, "queue_size": 0},
    })
    monkeypatch.setattr(server, "installed", lambda: True)
    monkeypatch.setattr(server, "backend", lambda: "launchd")
    monkeypatch.setattr(server, "launchd_loaded", lambda: True)
    info = server.status("http://127.0.0.1:8765")
    assert info["running"] is True
    assert info["installed"] is True
    assert info["backend"] == "launchd"
    assert info["models_loaded"] == ["custom-0.6b-4bit"]


def test_status_when_down(monkeypatch):
    monkeypatch.setattr(server, "probe", lambda url: None)
    monkeypatch.setattr(server, "installed", lambda: False)
    monkeypatch.setattr(server, "backend", lambda: "pid")
    info = server.status("http://127.0.0.1:8765")
    assert info["running"] is False
    assert info["installed"] is False


def test_server_install_refuses_non_macos(monkeypatch, capsys):
    # ApiError 由 main() 统一转成退出码 2 + JSON，所以走真实入口验证。
    monkeypatch.setattr(server, "is_macos", lambda: False)
    code = cli.main(["server", "install", "--json"])
    assert code == 2
    assert "仅支持 macOS" in json.loads(capsys.readouterr().out)["error"]


def test_server_start_and_stop_use_pid_backend_on_non_macos(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(server, "backend", lambda: "pid")
    monkeypatch.setattr(server, "start_pid", lambda: (calls.append("start"), (True, "已启动"))[1])
    monkeypatch.setattr(server, "stop_pid", lambda: (calls.append("stop"), (True, "已停止"))[1])
    monkeypatch.setattr(server, "wait_ready", lambda url, seconds=45: True)

    cli.cmd_server(argparse.Namespace(action="start", base="http://x", json=True, autostart=True))
    cli.cmd_server(argparse.Namespace(action="stop", base="http://x", json=True, autostart=True))
    assert calls == ["start", "stop"]


def test_server_restart_stops_then_starts(monkeypatch):
    order = []
    monkeypatch.setattr(server, "backend", lambda: "pid")
    monkeypatch.setattr(server, "stop_pid", lambda: (order.append("stop"), (True, ""))[1])
    monkeypatch.setattr(server, "start_pid", lambda: (order.append("start"), (True, ""))[1])
    monkeypatch.setattr(server, "wait_ready", lambda url, seconds=45: True)
    cli.cmd_server(argparse.Namespace(action="restart", base="http://x", json=True, autostart=True))
    assert order == ["stop", "start"]


def test_parser_exposes_server_subcommands():
    args = cli.build_parser().parse_args(["server", "install"])
    assert args.func is cli.cmd_server and args.action == "install"
    args = cli.build_parser().parse_args(["server", "status", "--json"])
    assert args.json is True and args.action == "status"
