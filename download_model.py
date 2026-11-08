"""Download MLX weights for this app; model files are never committed to Git.

    .venv/bin/python download_model.py                          # default TTS
    .venv/bin/python download_model.py --model design-1.7b-4bit
    .venv/bin/python download_model.py --model qwen3-asr-0.6b-4bit
    .venv/bin/python download_model.py --role asr               # all ASR
    .venv/bin/python download_model.py --all
    HTTPS_PROXY=http://127.0.0.1:7890 .venv/bin/python download_model.py

The downloader tries the configured proxy, a local :7890 proxy, hf-mirror and
Hugging Face directly. A partial download is safely resumable.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from config import DEFAULT_ASR_MODEL, DEFAULT_MODEL, MODELS, MODEL_DIR
from engine import model_installed, model_path


def _human(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:180]}"


def try_download(model_key: str) -> bool:
    from huggingface_hub import snapshot_download

    spec = MODELS[model_key]
    try:
        snapshot_download(spec["repo"], local_dir=str(model_path(model_key)), max_workers=4)
        # mlx-community's whisper MLX repos ship weights only; mlx_audio needs
        # the tokenizer/feature extractor from the original OpenAI checkpoint.
        extra = spec.get("extra_repo")
        if extra:
            snapshot_download(extra["repo"], local_dir=str(model_path(model_key)),
                              allow_patterns=extra["files"], max_workers=4)
        return model_installed(model_key)
    except Exception as exc:  # network errors vary by platform/provider
        print(f"  × {_human(exc)}", flush=True)
        return False


def download(model_key: str) -> bool:
    if model_installed(model_key):
        print(f"已存在：{model_path(model_key)}")
        return True
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
        print(f"[{time.strftime('%H:%M:%S')}] {MODELS[model_key]['name']} · {name}", flush=True)
        os.environ.update(env)
        if try_download(model_key):
            size = sum(f.stat().st_size for f in model_path(model_key).rglob("*") if f.is_file()) / 1e9
            print(f"✓ 完成：{model_path(model_key)}（{size:.2f} GB）")
            return True
    print(f"× {model_key} 全部下载方式均失败，请检查网络后重试；已下载分片会保留。", file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="下载 ethan-tts 所需的 MLX 权重")
    parser.add_argument("--model", action="append", default=[],
                        help="模型 key，可重复指定；默认下载默认合成模型")
    parser.add_argument("--role", choices=["tts", "asr"], help="按用途批量下载（tts / asr）")
    parser.add_argument("--all", action="store_true", help="下载全部模型")
    args = parser.parse_args()

    if args.all:
        keys = list(MODELS)
    elif args.role:
        keys = [k for k, v in MODELS.items() if v["role"] == args.role]
    elif args.model:
        unknown = [k for k in args.model if k not in MODELS]
        if unknown:
            parser.error(f"未知模型：{', '.join(unknown)}；可选：{', '.join(MODELS)}")
        keys = args.model
    else:
        keys = [DEFAULT_MODEL, DEFAULT_ASR_MODEL]

    failed = [key for key in keys if not download(key)]
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
