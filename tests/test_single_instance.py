"""单实例锁与孤儿任务回收。

回归背景：CLI 的 ensure_server() 是「探测 /health → Popen」两段式，两个 CLI
并发调用时都能探测到「没在跑」，各自拉起一个 app.py。先绑端口的赢，但后到的
进程会在 MLX 初始化阶段卡住很久，表现为「两个服务同时在跑」并拖垮整机。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import single_instance as si  # noqa: E402


def test_second_acquire_in_same_process_is_refused(tmp_path):
    """同一进程重复 acquire 也要被拒（flock 对同进程再开一个 fd 不共享锁）。"""
    pid_file = tmp_path / "server.lock"
    lock = si.SingleInstanceLock(pid_file)
    lock.acquire()
    try:
        other = si.SingleInstanceLock(pid_file)
        with pytest.raises(si.AlreadyRunningError):
            other.acquire()
    finally:
        lock.release()


def test_lock_released_allows_reacquire(tmp_path):
    """release 之后必须能重新拿到（否则重启服务会一直被拒）。"""
    pid_file = tmp_path / "server.lock"
    first = si.SingleInstanceLock(pid_file)
    first.acquire()
    first.release()

    second = si.SingleInstanceLock(pid_file)
    second.acquire()
    second.release()


def test_pid_file_records_owner(tmp_path):
    pid_file = tmp_path / "server.lock"
    lock = si.SingleInstanceLock(pid_file)
    lock.acquire()
    try:
        assert pid_file.read_text(encoding="utf-8").strip() == str(__import__("os").getpid())
    finally:
        lock.release()


def test_second_app_process_exits_with_code_2(tmp_path):
    """端到端：第二个 app.py 进程应直接退出（码 2）并打印可读原因。

    用子进程真跑，因为 flock 是跨进程语义，同进程测试覆盖不到。
    必须用独立的 ETHAN_TTS_DIR：共用用户数据目录的话，会和真实在跑的服务
    抢同一把锁（测试里第一个进程根本起不来），而且可能干扰线上实例。
    """
    py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip("需要项目 venv 才能起服务进程")

    env = {**__import__("os").environ, "ETHAN_TTS_DIR": str(tmp_path / "data")}
    first = subprocess.Popen(
        [str(py), str(ROOT / "app.py"), "--host", "127.0.0.1", "--port", "8811"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # 先确认第一个进程真的起来了（锁已拿到、在监听）。
        # 不确认就探测的话，探测进程可能抢在第一个之前拿到锁、自己变成服务，
        # 然后 subprocess.run 就会一直等它退出——这正是要防的那个竞态。
        import urllib.request
        deadline = time.monotonic() + 120
        up = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8811/health", timeout=2):
                    up = True
                    break
            except Exception:
                time.sleep(1)
        assert up, "第一个进程没能起来"

        probe = subprocess.run(
            [str(py), str(ROOT / "app.py"), "--host", "127.0.0.1", "--port", "8812"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        assert probe.returncode == 2, probe.stderr
        assert "只能起一个实例" in (probe.stderr or "")
    finally:
        first.terminate()
        try:
            first.wait(timeout=30)
        except subprocess.TimeoutExpired:
            first.kill()
