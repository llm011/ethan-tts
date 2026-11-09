"""HTTP client and server-lifecycle helpers for the CLI.

Everything that talks to the local service (or starts it) lives here so the
command handlers stay free of networking details.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台退化为无锁
    fcntl = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent
from config import DATA_DIR, LOG_DIR  # noqa: E402  (需在 ROOT 之后，避免循环)

DEFAULT_URL = os.environ.get("ETHAN_TTS_URL", "http://127.0.0.1:8765")
STARTUP_TIMEOUT = float(os.environ.get("ETHAN_TTS_STARTUP_TIMEOUT", "90"))



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
        # 必须等它就绪再放锁：锁一放，后面的进程会复查 is_up，而服务此刻
        # 还没 bind 端口，于是又各自 spawn 一份（实测 5 并发会起 5 个）。
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
