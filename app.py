"""Local Qwen3-TTS management console and HTTP API.

This module is the assembly point only: it creates the FastAPI app, installs
middleware and lifespan hooks, mounts the routers from ``routes/`` and starts
uvicorn. Domain logic lives in ``engine`` (inference), ``db`` (storage),
``model_registry`` (model inventory) and the routers themselves.

Run with: .venv/bin/python app.py
Open http://127.0.0.1:8765 after model download.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import db
from config import STATIC_DIR
from engine import engine, model_installed  # re-exported for existing callers
from routes import ALL_ROUTERS
from routes.chat import config_warning  # re-exported for existing callers
from settings_store import QUEUE_TIMEOUT_SEC  # re-exported for existing callers
from routes.settings import overview  # noqa: F401  (kept on the app for /api/overview)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    # 上次进程异常退出（崩溃 / kill -9 / 机器重启）时，正在跑的任务会永远停在
    # running，队列也不会再推进。启动时统一标成 interrupted，避免它们看起来
    # 像"还在进行"，也避免前端一直转圈。
    orphaned = db.reap_orphan_tasks()
    if orphaned:
        db.log("WARNING", "task", f"启动时回收 {orphaned} 个中断任务")
    engine.start_worker()
    db.log("INFO", "system", "管理服务已启动（模型按需加载）")
    try:
        yield
    finally:
        engine.stop_worker()
        db.log("INFO", "system", "管理服务已停止")


app = FastAPI(title="ethan-tts", lifespan=lifespan)

# Overwritten by the CLI entry point; the pages router reports these back.
app.state.bind_host = os.environ.get("HOST", "127.0.0.1")
app.state.port = int(os.environ.get("PORT", "8765"))


class NoCacheStatic(StaticFiles):
    """Local app: always revalidate, so an edited page is never served stale."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")

for router in ALL_ROUTERS:
    app.include_router(router)


if __name__ == "__main__":
    import argparse

    import uvicorn

    from config import PID_FILE
    from single_instance import acquire_or_exit

    # Claim the instance lock before uvicorn starts: a second process launched
    # concurrently exits here instead of competing for the port afterwards.
    _instance_lock = acquire_or_exit(PID_FILE)

    parser = argparse.ArgumentParser(description="ethan-tts 本地语音管理台")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="用 0.0.0.0 让同一局域网的手机访问")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    app.state.bind_host = args.host
    app.state.port = args.port
    if args.host in {"0.0.0.0", "::"}:
        db.log("INFO", "system", "服务已监听局域网，手机可用 /chat 打开语音对话")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
