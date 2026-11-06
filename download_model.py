"""Download MLX Qwen3-TTS weights; model files are never committed to Git.

    .venv/bin/python download_model.py
    .venv/bin/python download_model.py --model design-1.7b-4bit
    .venv/bin/python download_model.py --model base-0.6b-4bit
    HTTPS_PROXY=http://127.0.0.1:7890 .venv/bin/python download_model.py

The downloader tries the configured proxy, a local :7890 proxy, hf-mirror,
and Hugging Face directly. A partial download is safely resumable.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from config import DEFAULT_MODEL, MODEL_DIR, MODELS
from engine import model_installed, model_path


def try_download(model_key: str) -> bool:
    from huggingface_hub import snapshot_download

    repo = MODELS[model_key]["repo"]
    destination = model_path(model_key)
    try:
        snapshot_download(repo, local_dir=str(destination), max_workers=4)
        return model_installed(model_key)
    except Exception as exc:  # network errors vary by platform/provider
        print(f"  × {type(exc).__name__}: {str(exc)[:180]}", flush=True)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="下载 Qwen3-TTS MLX 权重")
    parser.add_argument("--model", choices=list(MODELS), default=DEFAULT_MODEL)
    args = parser.parse_args()
    if model_installed(args.model):
        print(f"已存在：{model_path(args.model)}")
        return
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    configured_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    attempts = []
    if configured_proxy:
        attempts.append(("当前环境代理", {}))
    else:
        attempts.append(("本地代理 127.0.0.1:7890", {
            "HTTPS_PROXY": "http://127.0.0.1:7890", "HTTP_PROXY": "http://127.0.0.1:7890",
        }))
    attempts.extend([
        ("HF 镜像 hf-mirror.com", {"HF_ENDPOINT": "https://hf-mirror.com"}),
        ("直连 huggingface.co", {}),
    ])
    for name, env in attempts:
        print(f"[{time.strftime('%H:%M:%S')}] {MODELS[args.model]['name']} · {name}", flush=True)
        os.environ.update(env)
        if try_download(args.model):
            size = sum(f.stat().st_size for f in model_path(args.model).rglob("*") if f.is_file()) / 1e9
            print(f"✓ 完成：{model_path(args.model)}（{size:.2f} GB）")
            return
    print("全部下载方式均失败，请检查网络后重试；已下载分片会保留。", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
