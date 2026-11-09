"""Model inventory, repository size probing and download bookkeeping.

`model_status()` is used by the console, the chat bootstrap and the settings
page, so it lives in its own module rather than inside a router.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import db
import stt
from config import MODELS, ROOT
from engine import model_installed, model_size_bytes


def model_status() -> list[dict]:
    """Every known model with installed/loaded state and a human size label."""
    loaded = set(engine_snapshot()["loaded_models"])
    asr_loaded = stt.stt.loaded_key()
    items = []
    for key, value in MODELS.items():
        if value["role"] == "asr":
            installed, loaded_now = stt.asr_model_installed(key), asr_loaded == key
            size = stt.asr_model_size_bytes(key)
        else:
            installed, loaded_now = model_installed(key), key in loaded
            size = model_size_bytes(key)
        items.append({**value, "key": key, "installed": installed, "loaded": loaded_now,
                      "size_bytes": size, "size_label": size_label_for(key, size)})
    # Probe every model whose repository size is not cached yet, including the
    # ones currently showing a config estimate: the probe replaces the guess with
    # the real repository size.
    stale = [item["key"] for item in items if not item["size_bytes"]
             and repo_size_gb(item["key"]) is None]
    if stale:
        probe_sizes_in_background(stale)
    return items


def engine_snapshot() -> dict:
    """Imported lazily to avoid an import cycle with engine's own helpers."""
    from engine import engine
    return engine.resource_snapshot()


def size_label_for(model_key: str, disk_bytes: int) -> str:
    if disk_bytes > 0:
        return size_label(disk_bytes)
    probed = repo_size_gb(model_key)
    if probed:
        return size_label_from_gb(probed)
    estimate = MODELS[model_key].get("size_gb") or 0
    return f"约 {size_label_from_gb(estimate)}" if estimate else ""


def size_label(size: int) -> str:
    if size <= 0:
        return ""
    gb = size / 1e9
    return f"{gb:.1f} GB" if gb >= .95 else f"{size / 1e6:.0f} MB"


def size_label_from_gb(gb: float) -> str:
    return f"{gb:.1f} GB" if gb >= .95 else f"{gb * 1000:.0f} MB" if gb else ""


# Repo sizes for models that are not on disk. Asking the hub costs a network
# round trip, so answers are cached in SQLite and only refreshed on misses.
_size_probe_lock = threading.Lock()
_size_probe_running: set[str] = set()


def repo_size_gb(model_key: str) -> float | None:
    """Cached repository size in GB, or None when it was never probed."""
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (f"model_size:{model_key}",)).fetchone()
        return db.loads(row["value"], None) if row else None
    except sqlite3.Error:
        return None


def store_repo_size(model_key: str, gb: float) -> None:
    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"model_size:{model_key}", db.dumps(round(gb, 3))),
            )
    except sqlite3.Error:
        pass


def probe_repo_size(model_key: str) -> None:
    from huggingface_hub import HfApi

    spec = MODELS[model_key]
    endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        api = HfApi(endpoint=endpoint)
        total = sum(f.size or 0 for f in api.model_info(
            spec["repo"], files_metadata=True).siblings)
        extra = spec.get("extra_repo")
        if extra:
            total += sum(f.size or 0 for f in api.model_info(
                extra["repo"], files_metadata=True).siblings
                if any(f.rfilename.endswith(p.replace("*", "")) or
                       f.rfilename.startswith(p.split("*")[0]) for p in extra["files"]))
        if total:
            store_repo_size(model_key, total / 1e9)
            db.log("INFO", "model", f"探测到模型体积 {model_key}：{total / 1e9:.2f} GB")
    except Exception as exc:
        db.log("WARNING", "model", f"探测模型体积失败 {model_key}：{exc}")
    finally:
        with _size_probe_lock:
            _size_probe_running.discard(model_key)


def probe_sizes_in_background(keys: list[str]) -> None:
    with _size_probe_lock:
        todo = [k for k in keys if k not in _size_probe_running]
        _size_probe_running.update(todo)
    for key in todo:
        threading.Thread(target=probe_repo_size, args=(key,), daemon=True,
                         name=f"size-probe-{key}").start()


# Download bookkeeping lives in memory: a download belongs to this process, and
# the page polls it to show progress instead of guessing from a toast.
_downloads: dict[str, dict] = {}
_download_lock = threading.Lock()


def download_state(model_key: str) -> dict:
    with _download_lock:
        return dict(_downloads.get(model_key) or {})


def active_downloads() -> dict[str, dict]:
    """Snapshot of every tracked download, safe to read from any thread."""
    with _download_lock:
        return {key: dict(value) for key, value in _downloads.items()}


def start_download(model_key: str) -> None:
    """Run the download in a thread, recording progress for the page to poll."""

    def run() -> None:
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "download_model.py"), "--model", model_key],
                cwd=ROOT, capture_output=True, text=True, timeout=1800,
                env={**os.environ,
                     "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")},
            )
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout)[-1500:])
            db.log("INFO", "model", f"模型下载完成：{model_key}")
            with _download_lock:
                _downloads[model_key] = {"status": "done", "message": "下载完成"}
        except Exception as exc:
            db.log("ERROR", "model", f"模型下载失败：{exc}")
            with _download_lock:
                _downloads[model_key] = {"status": "error", "message": str(exc)[:300]}

    threading.Thread(target=run, name="model-download", daemon=True).start()


def claim_download(model_key: str) -> str | None:
    """Reserve the download slot; returns an error message when it is busy."""
    with _download_lock:
        running = [k for k, v in _downloads.items() if v.get("status") == "running"]
        if model_key in running:
            return "这个模型正在下载中，请稍候"
        if running:
            return f"{MODELS[running[0]]['name']} 正在下载，同一时间只下载一个模型"
        _downloads[model_key] = {"status": "running", "message": "正在下载"}
    return None
