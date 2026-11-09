"""`ethan-tts server ...`：把常驻服务托管起来。

为什么需要这一层：CLI 自己能按需拉起服务（`ensure_server`），但**浏览器页面和
curl 不经过 CLI**，服务没起就是打不开。所以要有一种「登录后自动在、崩了自动起」
的托管方式。

macOS 用 launchd（`~/Library/LaunchAgents`）：
- `RunAtLoad` → 登录即启动，网页随时能打开
- `KeepAlive` → 进程挂了 launchd 自动拉起
- 由系统管理，不依赖任何终端会话

其它平台（以及容器里）退化为 pid 文件托管：能 start/stop/status，但没有开机自启。
所有命令都只操作**本机默认端口**的那一个实例，不碰远端 `--base`。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import DATA_DIR, HOME_DIR, LOG_DIR

LABEL = "com.ethan-tts.server"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
PID_PATH = DATA_DIR / "server.pid"

# launchd 打不开某些卷上的日志路径（外部 APFS 卷实测报 EX_CONFIG 78），
# 托管模式的日志固定放家目录，和运行时数据分开。
LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "ethan-tts"
LAUNCHD_LOG = LAUNCHD_LOG_DIR / "server.log"
SERVER_LOG = LOG_DIR / "server.log"


# ------------------------------------------------------------------ 基础设施


def is_macos() -> bool:
    return sys.platform == "darwin"


def default_port() -> int:
    return int(os.environ.get("PORT", "8765"))


def _app_script() -> Path:
    """定位可运行的 app.py / ethan-tts 入口。

    从源码跑时用 ROOT/app.py；从 wheel 装出来时用当前解释器的 -m 入口，
    避免把 site-packages 里的绝对路径写死进 plist。
    """
    from config import ROOT

    app = ROOT / "app.py"
    return app


def _command() -> list[str]:
    """launchd / pid 托管实际执行的命令。"""
    app = _app_script()
    python = Path(sys.executable)
    if app.exists():
        return [str(python), str(app), "--host", "127.0.0.1", "--port", str(default_port())]
    return [str(python), "-m", "app", "--host", "127.0.0.1", "--port", str(default_port())]


def _environment() -> dict[str, str]:
    """把关键环境变量带进托管进程，避免装/跑环境不一致。"""
    keys = ("ETHAN_TTS_DIR", "ETHAN_TTS_DATA_DIR", "ETHAN_TTS_MODEL_DIR",
            "HF_ENDPOINT", "PATH", "HOME")
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


# ------------------------------------------------------------------ 健康探测


def probe(url: str, timeout: float = 2.0) -> dict | None:
    """返回 /health 内容；服务没起返回 None。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def wait_ready(url: str, seconds: float = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if probe(url) is not None:
            return True
        time.sleep(.3)
    return False


# ------------------------------------------------------------------ launchd


def plist_content() -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        f"  <key>Label</key><string>{LABEL}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
    ]
    for part in _command():
        lines.append(f"    <string>{_xml_escape(part)}</string>")
    # 固定到用户目录：不能随安装时的 cwd 走。实测在 /tmp 里安装会把
    # WorkingDirectory 固化成 /private/tmp，launchd 直接 bootstrap 失败。
    lines += ["  </array>",
              f"  <key>WorkingDirectory</key><string>{_xml_escape(str(HOME_DIR))}</string>"]
    env = _environment()
    if env:
        lines.append("  <key>EnvironmentVariables</key>")
        lines.append("  <dict>")
        for key, value in env.items():
            lines.append(f"    <key>{_xml_escape(key)}</key><string>{_xml_escape(value)}</string>")
        lines.append("  </dict>")
    lines += [
        "  <key>RunAtLoad</key><true/>",
        "  <key>KeepAlive</key>",
        "  <dict>",
        "    <key>SuccessfulExit</key><false/>",
        "  </dict>",
        "  <key>ProcessType</key><string>Interactive</string>",
        "  <key>ThrottleInterval</key><integer>10</integer>",
        f"  <key>StandardOutPath</key><string>{_xml_escape(str(LAUNCHD_LOG))}</string>",
        f"  <key>StandardErrorPath</key><string>{_xml_escape(str(LAUNCHD_LOG))}</string>",
        "</dict>",
        "</plist>",
        "",
    ]
    return "\n".join(lines)


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def launchd_loaded() -> bool:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                            capture_output=True, text=True)
    return result.returncode == 0


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install_launchd() -> tuple[bool, str]:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist_content(), encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # 幂等：先下掉旧实例
    result = _launchctl("bootstrap", domain, str(PLIST_PATH))
    if result.returncode:
        return False, (result.stderr or result.stdout).strip() or "launchctl bootstrap 失败"
    _launchctl("enable", f"{domain}/{LABEL}")
    return True, str(PLIST_PATH)


def uninstall_launchd() -> tuple[bool, str]:
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    return True, "已移除 launchd 托管"


def start_launchd() -> tuple[bool, str]:
    domain = f"gui/{os.getuid()}"
    if not launchd_loaded():
        result = _launchctl("bootstrap", domain, str(PLIST_PATH))
        if result.returncode:
            return False, (result.stderr or result.stdout).strip()
    result = _launchctl("kickstart", "-k", f"{domain}/{LABEL}")
    if result.returncode:
        return False, (result.stderr or result.stdout).strip()
    return True, "已启动（launchd）"


def stop_launchd(kill: bool = False) -> tuple[bool, str]:
    """停掉服务但保留托管：KeepAlive 会让它自己起来，所以用 bootout 下掉。

    bootout 只是「让 launchd 别再管」，已经跑起来的子进程不一定立刻退出；
    如果它正卡在推理/初始化里，会变成 PPID=1 的孤儿继续占着 8765 端口。
    之后 restart 起的「新」进程绑定失败，`launchctl list` 里状态是 `- 2`，
    而 health 探测仍然成功——因为回话的是那个孤儿。

    实测踩过：restart 报「已重启，已就绪」，但 PID 一天没变，跑的还是旧代码。
    所以 bootout 之后要等端口真正释放；超时就用 SIGTERM 兜底。
    """
    domain = f"gui/{os.getuid()}/{LABEL}"
    if not launchd_loaded():
        return True, "服务未在运行"
    _launchctl("bootout", domain)
    if _wait_port_free(timeout=10.0):
        return True, "已停止（launchd）"
    # 还占着端口：说明有孤儿进程，按端口精确清理（只杀监听该端口的进程）。
    killed = _kill_port_listener()
    if _wait_port_free(timeout=10.0):
        return True, f"已停止（launchd，清理了 {killed} 个残留进程）"
    return False, (
        f"服务未能停止：端口 {default_port()} 仍被占用，请手动检查 "
        f"`lsof -i :{default_port()}`"
    )


def _wait_port_free(timeout: float) -> bool:
    """等监听端口释放；用于确认 bootout 真的把进程带走了。"""
    import socket
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", default_port())) != 0:
                return True
        _time.sleep(0.3)
    return False


def _kill_port_listener() -> int:
    """按端口找监听进程并发 SIGTERM，返回杀掉的数量。

    不用 pkill -f app.py：那会误杀别的 worktree / 别的实例。
    """
    import signal
    import subprocess

    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{default_port()}"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    killed = 0
    for raw in result.stdout.split():
        try:
            os.kill(int(raw), signal.SIGTERM)
            killed += 1
        except (ValueError, OSError):
            continue
    return killed


# ------------------------------------------------------------------ pid 退化方案


def read_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def start_pid() -> tuple[bool, str]:
    pid = read_pid()
    if pid:
        return True, f"已在运行（pid {pid}）"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with SERVER_LOG.open("ab") as log:
        process = subprocess.Popen(
            _command(), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True, cwd=str(HOME_DIR), env={**os.environ},
        )
    PID_PATH.write_text(str(process.pid))
    return True, f"已启动（pid {process.pid}）"


def stop_pid() -> tuple[bool, str]:
    pid = read_pid()
    if not pid:
        PID_PATH.unlink(missing_ok=True)
        return True, "服务未在运行"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        PID_PATH.unlink(missing_ok=True)
        return True, "服务未在运行"
    for _ in range(50):
        time.sleep(.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    PID_PATH.unlink(missing_ok=True)
    return True, "已停止"


# ------------------------------------------------------------------ 面向命令的封装


def backend() -> str:
    if is_macos() and PLIST_PATH.exists():
        return "launchd"
    return "pid"


def installed() -> bool:
    return is_macos() and PLIST_PATH.exists()


def status(url: str) -> dict:
    health = probe(url)
    info = {
        "url": url,
        "running": health is not None,
        "installed": installed(),
        "backend": backend(),
    }
    if is_macos():
        info["launchd_loaded"] = launchd_loaded()
        info["plist"] = str(PLIST_PATH)
    else:
        info["pid"] = read_pid()
    if health:
        runtime = health.get("models", {})
        info["models_loaded"] = runtime.get("loaded_models", [])
        info["metal_active_gb"] = runtime.get("metal_active_gb")
        info["queue_size"] = runtime.get("queue_size")
    return info
