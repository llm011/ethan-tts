"""`ethan-tts setup` / `doctor`：一键把本机环境配好并自检。

设计原则（都是实际踩过的坑）：

1. **模型下载必须先问**。默认权重 1.6 GB，不能偷偷下。交互环境里明确告诉用户
   体积和位置再问；非交互（CI、agent、管道）默认**不下载**，除非显式 `--yes`
   或 `--with-model`。给用户反悔的余地，别让一次 `install` 吃掉几个 G 流量。
2. **一切检查都要能定位到具体哪一环坏了**。doctor 按「平台 → Python → 依赖 →
   模型 → 服务 → ffmpeg → 权限」顺序逐项报 OK/警告/错误，并给出下一步该敲什么。
3. **只读为主**。doctor 默认不改任何东西（除了读状态），修不了的地方给命令让
   用户自己决定。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from config import (
    DATA_DIR,
    DEFAULT_MODEL,
    HOME_DIR,
    MODEL_DIR,
    MODEL_ROOTS,
    MODELS,
    ROOT,
)

# 默认权重体积，写在提示里让用户心里有数
DEFAULT_SIZE_GB = MODELS[DEFAULT_MODEL]["size_gb"]


# ------------------------------------------------------------------ 环境探测


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _python_ok() -> tuple[bool, str]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 10):
        return False, f"Python {version} 太旧，需要 3.10–3.12"
    if sys.version_info >= (3, 13):
        return False, f"Python {version} 超出支持范围，请用 3.12"
    return True, version


def _mlx_ok() -> tuple[bool, str]:
    try:
        import mlx.core  # noqa: F401
    except Exception as exc:  # 依赖没装或平台不支持
        return False, f"mlx 导入失败：{type(exc).__name__}: {exc}"
    try:
        import mlx_audio  # noqa: F401
    except Exception as exc:
        return False, f"mlx-audio 导入失败：{type(exc).__name__}: {exc}"
    return True, "mlx + mlx-audio 可用"


def _writable(path: Path) -> tuple[bool, str]:
    """能不能在目标目录里真的写文件（父目录可能还不存在）。"""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        return False, f"{probe} 不是目录"
    return os.access(probe, os.W_OK), f"{probe} {'可写' if os.access(probe, os.W_OK) else '不可写'}"


def _model_location(model_key: str) -> Path | None:
    """这个模型实际在哪（在任一候选根目录里找到就返回），没有则 None。

    刻意**不 import engine**：engine 在模块顶层 import mlx，而 doctor 存在的意义
    恰恰是「依赖坏了的时候」——一 import 就抛，自检等于没得跑。
    判断标准与 engine.model_path 保持一致：目录里有 *.safetensors。

    必须遍历 MODEL_ROOTS 而不是只看 MODEL_DIR：从旧版（clone 安装）升级上来的
    用户，权重还在源码旁，只看用户目录会全部报「未下载」，安装流程会白下一份。
    """
    name = MODELS[model_key]["directory"]
    for root in MODEL_ROOTS:
        candidate = root / name
        if any(candidate.glob("*.safetensors")):
            return candidate
    return None


def _has_legacy_models() -> bool:
    """源码目录里是不是还留着老版本下的权重（升级用户常见）。"""
    legacy = ROOT / "models"
    if legacy in MODEL_ROOTS and not legacy.is_dir():
        return False
    try:
        return any(legacy.iterdir())
    except OSError:
        return False


def _installed_models() -> list[str]:
    return [key for key in MODELS if _model_location(key) is not None]


# ------------------------------------------------------------------ setup


def _ask(prompt: str, default: bool, *, assume_yes: bool | None) -> bool:
    """问一个是/否。非交互环境一律按默认，不阻塞。"""
    if assume_yes is not None:
        return assume_yes
    if not sys.stdin.isatty():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {hint} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in {"y", "yes", "是", "好", "1"}


def cmd_setup(args) -> int:
    """装完之后跑一次：检查环境、按需拉模型、可选拉起服务。"""
    from cli import ApiError, ensure_server, is_up

    steps: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    if not args.json:
        print("检查运行环境…")

    # 1. 平台
    if not _is_apple_silicon():
        message = (
            f"当前平台是 {sys.platform}/{platform.machine()}，MLX 只在 Apple Silicon 上可用。"
            "Intel Mac、Linux、Windows 都跑不起来。"
        )
        record("平台", False, message)
        if args.json:
            print(json.dumps({"ok": False, "steps": steps}, ensure_ascii=False, indent=2))
        else:
            print(f"  ✗ {message}")
        return 2
    record("平台", True, f"{platform.system()} {platform.machine()}")

    # 2. Python
    ok, detail = _python_ok()
    record("Python", ok, detail)
    if not ok and not args.json:
        print(f"  ✗ {detail}")
        return 2

    # 3. 依赖
    ok, detail = _mlx_ok()
    record("依赖", ok, detail)
    if not ok:
        hint = "uv pip install -r requirements.txt" if (ROOT / "requirements.txt").exists() else None
        if args.json:
            print(json.dumps({"ok": False, "steps": steps,
                              "hint": hint}, ensure_ascii=False, indent=2))
        else:
            print(f"  ✗ {detail}")
            if hint:
                print(f"    修复：{hint}")
        return 2

    # 4. 目录可写
    for label, path in (("数据目录", DATA_DIR), ("模型目录", MODEL_DIR)):
        ok, detail = _writable(path)
        record(label, ok, f"{path}（{detail}）")
        if not ok:
            if args.json:
                print(json.dumps({"ok": False, "steps": steps}, ensure_ascii=False, indent=2))
            else:
                print(f"  ✗ {label}不可写：{path}\n    设置 ETHAN_TTS_DIR 换个位置再试")
            return 2

    # 5. ffmpeg（可选）
    has_ffmpeg = shutil.which("ffmpeg") is not None
    record("ffmpeg", has_ffmpeg, "已安装" if has_ffmpeg else "未安装（非 WAV 导出与音色克隆需要）")

    # 6. 模型 —— 这里必须问
    installed = _installed_models()
    have_default = DEFAULT_MODEL in installed
    if args.json:
        print(json.dumps({"ok": True, "steps": steps, "models_installed": installed},
                         ensure_ascii=False, indent=2))
    else:
        for item in steps:
            mark = "✓" if item["ok"] else ("–" if item["step"] == "ffmpeg" else "✗")
            print(f"  {mark} {item['step']}：{item['detail']}")

    if not have_default:
        if not args.json:
            print()
            print(f"默认模型还没下载：{MODELS[DEFAULT_MODEL]['name']}")
            print(f"  · 体积约 {DEFAULT_SIZE_GB:g} GB")
            print(f"  · 位置 {MODEL_DIR}")
            if _has_legacy_models():
                print(f"  · 检测到源码目录里已有权重：{ROOT / 'models'}（会被自动复用，不必重下）")
            print("  · 从 Hugging Face 下载（本机代理、hf-mirror、直连会依次尝试）")
        want = _ask(
            "现在下载吗？",
            default=False,
            assume_yes=args.yes if args.yes else (None if not args.download else True),
        ) if not args.download else True
        if want:
            if not args.json:
                print("开始下载，可以中断，之后重跑会断点续传。")
            code = subprocess.call([sys.executable, str(ROOT / "download_model.py")])
            if code != 0:
                if not args.json:
                    print("下载失败。检查网络后重跑：ethan-tts setup", file=sys.stderr)
                return 2
        else:
            if not args.json:
                print(f"已跳过。需要时随时执行：ethan-tts models download {DEFAULT_MODEL}")
            # 没模型就不启动服务，避免用户以为装好了却点不动
            return 0

    # 7. 服务
    from cli import DEFAULT_URL

    if is_up(DEFAULT_URL):
        record("服务", True, "已在运行")
        if not args.json:
            print("  ✓ 服务已在运行")
    elif args.no_serve:
        record("服务", True, "已跳过（--no-serve）")
    else:
        try:
            ensure_server(DEFAULT_URL, True)
            record("服务", True, "已启动")
            if not args.json:
                print(f"  ✓ 服务已启动：{DEFAULT_URL}")
        except ApiError as exc:
            record("服务", False, str(exc))
            if not args.json:
                print(f"  ✗ 服务启动失败：{exc}", file=sys.stderr)
            return 2

    if not args.json:
        print()
        print("搞定。试试：")
        print('  ethan-tts say "你好，这是本地合成的语音。" -o hello.wav')
        print(f"  或者打开 {DEFAULT_URL} 用图形界面")
    return 0


# ------------------------------------------------------------------ doctor


def cmd_doctor(args) -> int:
    """只读自检：把每一环的真实状态摊开，指出哪一环坏了该修什么。"""
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    # 平台 / Python
    add("平台", "ok" if _is_apple_silicon() else "error",
        f"{platform.system()} {platform.machine()}",
        "" if _is_apple_silicon() else "MLX 仅支持 Apple Silicon，其他平台无法运行本地推理")
    ok, detail = _python_ok()
    add("Python", "ok" if ok else "error", detail, "" if ok else "用 Python 3.12 重装（uv tool install --python 3.12）")

    # 依赖
    ok, detail = _mlx_ok()
    add("依赖", "ok" if ok else "error", detail,
        "" if ok else "uv tool install --force .  或重装 ethan-tts")

    # 安装形态与路径（这次踩的核心坑之一）
    installed_via_tool = "site-packages" in str(ROOT)
    add("安装位置", "ok", f"{ROOT}" + ("（uv tool / pip 安装）" if installed_via_tool else "（源码目录）"))
    add("数据目录", "ok" if _writable(DATA_DIR)[0] else "error", str(DATA_DIR),
        "" if _writable(DATA_DIR)[0] else "设置 ETHAN_TTS_DIR 指向可写目录")
    add("模型目录", "ok" if _writable(MODEL_DIR)[0] else "error", str(MODEL_DIR),
        "" if _writable(MODEL_DIR)[0] else "设置 ETHAN_TTS_MODEL_DIR 指向可写目录")

    # 模型
    installed = _installed_models()
    if DEFAULT_MODEL in installed:
        where = _model_location(DEFAULT_MODEL)
        add("默认模型", "ok", f"{MODELS[DEFAULT_MODEL]['name']} 已就绪（{where}）")
    else:
        add("默认模型", "warn", "未下载",
            f"ethan-tts models download {DEFAULT_MODEL}（约 {DEFAULT_SIZE_GB:g} GB）")

    # 服务
    from cli import DEFAULT_URL, is_up

    if is_up(DEFAULT_URL):
        add("服务", "ok", f"{DEFAULT_URL} 可访问")
    else:
        add("服务", "warn", f"{DEFAULT_URL} 未运行", "ethan-tts serve")

    # ffmpeg / 播放器
    add("ffmpeg", "ok" if shutil.which("ffmpeg") else "warn",
        shutil.which("ffmpeg") or "未安装",
        "" if shutil.which("ffmpeg") else "brew install ffmpeg（非 WAV 导出与音色克隆需要）")
    player = shutil.which("afplay") or shutil.which("ffplay")
    add("播放器", "ok" if player else "warn", player or "未找到 afplay/ffplay",
        "" if player else "macOS 自带 afplay，异常时检查系统")

    # PATH 可见性 —— 服务/守护进程里找不到命令的经典原因
    cli_path = shutil.which("ethan-tts")
    add("PATH", "ok" if cli_path else "warn", cli_path or "当前环境里找不到 ethan-tts",
        "" if cli_path else "把 uv tool 的 bin 目录（通常 ~/.local/bin）加进 PATH")
    if not os.environ.get("PATH", "").count(str(Path.home() / ".local" / "bin")):
        add("PATH 提示", "warn", "~/.local/bin 不在 PATH 里",
            "launchd/systemd 拉起的进程拿不到用户 PATH，守护进程里调用外部 CLI 会失败")

    errors = [c for c in checks if c["status"] == "error"]
    warns = [c for c in checks if c["status"] == "warn"]

    if args.json:
        print(json.dumps({"ok": not errors, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        icon = {"ok": "✓", "warn": "–", "error": "✗"}
        for check in checks:
            print(f"{icon[check['status']]} {check['name']}：{check['detail']}")
            if check["fix"]:
                print(f"     → {check['fix']}")
        print()
        if errors:
            print(f"{len(errors)} 项需要处理，{len(warns)} 项建议优化。")
        elif warns:
            print(f"核心功能可用，{len(warns)} 项建议优化。")
        else:
            print("一切正常。")
    return 0 if not errors else 1
