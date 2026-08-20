"""服务入口：create_app 工厂组装 Config/JobStore/WeChatClient/SessionManager。

启动方式：`python -m printshop.main` 或 `python printshop/main.py`。
"""
import asyncio
import contextlib
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from printshop.config import Config
from printshop.jobs import JobStore
from printshop.routes import AppContext, build_router
from printshop.session_manager import SessionManager
from printshop.wechat_bot import WeChatClient

STATIC_DIR = Path(__file__).resolve().parent / "static"


@contextlib.asynccontextmanager
async def _lifespan(ctx: AppContext):
    ctx.loop = asyncio.get_running_loop()
    loop_task = asyncio.create_task(ctx.sm.run_loop())
    if ctx.wx is not None:
        try:
            await ctx.wx.restore_session()
        except Exception:
            pass  # 恢复失败不致命
    try:
        yield
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass


def create_app(cfg: Config | None = None, store: JobStore | None = None,
               wx: WeChatClient | None = None) -> FastAPI:
    cfg = cfg or Config()
    cfg.ensure_dirs()
    store = store or JobStore(cfg.db_path)
    wx = wx or WeChatClient()
    sm = SessionManager(cfg, wx, store)
    ctx = AppContext(cfg=cfg, store=store, wx=wx, sm=sm)
    sm.on_change = lambda: ctx.emit("session", **sm.screen_state())
    sm.on_note = lambda: ctx.emit("note", text=sm.screen_state()["message"])
    store.on_job_added = lambda jid: ctx.emit("new_job", id=jid)

    app = FastAPI(title="WeChat Self-Print", lifespan=lambda app: _lifespan(ctx))
    app.state.ctx = ctx
    app.include_router(build_router(ctx))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/screen")
    async def screen_page():
        return FileResponse(str(STATIC_DIR / "screen.html"))

    @app.get("/admin")
    async def admin_page():
        return FileResponse(str(STATIC_DIR / "screen.html"))

    return app


def main() -> None:
    cfg = Config()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
