"""单实例锁：保证同一个数据目录只跑一个 ethan-tts 服务进程。

背景：CLI 的 `ensure_server()` 是「先探测 /health 再 Popen」的两段式，
两个 CLI 并发调用时都能探测到「没在跑」，然后各自拉起一个 app.py。
先绑定端口的赢，后到的 uvicorn 报 Errno 48 退出——但如果第二个进程在
MLX 初始化/加载模型阶段卡住（例如系统内存紧张、swap 打满），它会在
*bind 之前* 长时间存活，看起来就像「两个服务同时在跑」，并继续持有
内存、拖慢整机，形成雪崩。

这里用文件锁做进程级互斥：拿不到锁就直接退出，给出明确提示，
而不是静默地多跑一份模型。
"""
from __future__ import annotations

import atexit
import errno
import os
import sys
from pathlib import Path

# fcntl 是 POSIX 专有；Windows 上退化为无锁（该项目的目标平台是 Apple Silicon）。
try:
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台
    fcntl = None  # type: ignore[assignment]


class AlreadyRunningError(RuntimeError):
    """同一数据目录已有服务实例在运行。"""


class SingleInstanceLock:
    """基于 flock 的单实例锁，进程退出（含崩溃）时由内核自动释放。"""

    def __init__(self, pid_file: Path) -> None:
        self.pid_file = pid_file
        self._handle = None

    def acquire(self) -> None:
        """独占获取锁；已被占用时抛 AlreadyRunningError。

        flock 的好处是进程被 kill -9 也不会留下需要手工清理的陈旧锁——
        内核在 fd 关闭时释放，不会出现「PID 文件残留导致再也起不来」。
        """
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.pid_file, "a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise AlreadyRunningError(self._describe_holder()) from exc
                raise
        # 拿到锁：写入自己的 PID，方便 ps / 排查时对照
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        atexit.register(self.release)

    def _describe_holder(self) -> str:
        try:
            pid = self.pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            pid = ""
        where = f"（pid={pid}）" if pid else ""
        return (
            f"已有 ethan-tts 服务在运行{where}，同一个数据目录只能起一个实例。\n"
            f"  查看状态：ethan-tts status\n"
            f"  停止服务：ethan-tts stop\n"
            f"  锁文件：  {self.pid_file}"
        )

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            self._handle.truncate()
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def acquire_or_exit(pid_file: Path) -> SingleInstanceLock:
    """获取单实例锁，失败时打印原因并退出（码 2，与 CLI 的参数错误一致）。"""
    lock = SingleInstanceLock(pid_file)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return lock
