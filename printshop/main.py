"""服务入口：create_app 工厂组装 Config/JobStore/WeChatClient/SessionManager。

启动方式：`python -m printshop.main` 或 `python printshop/main.py`。
PyInstaller 冻结态：chdir 到 exe 同级目录，使 Config 的相对路径（inbox/work/db）
落在 exe 旁而非 _MEI 临时目录（见 printshop.spec）。
"""
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from printshop.ble_link import BleScreenLink
from printshop.config import Config
from printshop.jobs import JobStore
from printshop.routes import AppContext, _is_loopback_host, build_router
from printshop.session_manager import SessionManager, SessionState
from printshop.tray import start_tray
from printshop.wechat_bot import WeChatClient

_log = logging.getLogger(__name__)

STATIC_DIR = (Path(sys._MEIPASS) / "printshop" / "static"
              if getattr(sys, "frozen", False)
              else Path(__file__).resolve().parent / "static")

_SERVICE_STATES = (SessionState.SCANNED, SessionState.CONNECTED,
                   SessionState.RECEIVING, SessionState.PRINTING)

# I8：开放 LAN 的 HTTP 白名单前缀（ESP32 小屏 + 调试页 + 静态资源）。
# 注意：/ws 不在其中——HTTP 中间件本就不拦截 websocket scope（无效果的死配置），
# WS 连接由 routes.ws_admin 端点内的 _ws_loopback_guard 单独把关。
_PUBLIC_PREFIXES = ("/screen", "/api/screen", "/static")


def _is_admin_path(path: str) -> bool:
    return not any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES)


@contextlib.asynccontextmanager
async def _lifespan(ctx: AppContext):
    ctx.loop = asyncio.get_running_loop()
    loop_task = asyncio.create_task(ctx.sm.run_loop())
    if ctx.ble is not None:
        try:
            ctx.ble.start()
        except Exception:
            _log.exception("BLE 链路启动失败")
    if ctx.wx is not None:
        try:
            await ctx.sm.restore_session()
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
        if ctx.ble is not None:
            try:
                await ctx.ble.stop()
            except Exception:
                _log.exception("BLE 链路停止失败")
        await _shutdown_cleanup(ctx)


async def _shutdown_cleanup(ctx: AppContext) -> None:
    """I7 优雅停机：服务态执行完整收尾语义（logout+inbox/work/trace+archive）；
    IDLE 也至少清理凭证（state.json）与消息缓存，防下轮启动残留。"""
    try:
        if ctx.sm.state in _SERVICE_STATES:
            await ctx.sm.finish_service()
        elif ctx.wx is not None:
            await ctx.wx.logout()
    except Exception:
        _log.exception("优雅停机清理失败")


def create_app(cfg: Config | None = None, store: JobStore | None = None,
               wx: WeChatClient | None = None) -> FastAPI:
    cfg = cfg or Config()
    cfg.ensure_dirs()
    store = store or JobStore(cfg.db_path)
    wx = wx or WeChatClient()
    sm = SessionManager(cfg, wx, store)
    ctx = AppContext(cfg=cfg, store=store, wx=wx, sm=sm)

    # 小屏 BLE Central 链路（空地址=禁用，回滚 WiFi 轮询）；ctx 先构造再后置赋值
    ble = None
    if cfg.screen_ble_address:
        def on_ble_battery(report: dict) -> None:
            if ctx.update_battery(report):
                ctx.emit("device", battery=ctx.battery_payload())
        ble = BleScreenLink(cfg, on_battery=on_ble_battery)
    ctx.ble = ble

    def on_change() -> None:
        state = sm.screen_state()
        ctx.emit("session", **state)
        if ble is not None:
            ble.push_state(state)
    sm.on_change = on_change
    sm.on_note = lambda: ctx.emit("note", text=sm.screen_state()["message"])
    sm.on_admin_notice = lambda kind, text: ctx.emit(kind, text=text)
    store.on_job_added = lambda jid: ctx.emit("new_job", id=jid)

    app = FastAPI(title="WeChat Self-Print", lifespan=lambda app: _lifespan(ctx))
    app.state.ctx = ctx

    @app.middleware("http")
    async def loopback_guard(request, call_next):
        """I8：管理端点仅允许本机（tray/TestClient 走 127.0.0.1/testclient 不受影响）。"""
        host = request.client.host if request.client else None
        if _is_loopback_host(host):
            return await call_next(request)
        if _is_admin_path(request.url.path):
            return JSONResponse(status_code=403, content={"error": "管理端点仅允许本机访问"})
        return await call_next(request)

    app.include_router(build_router(ctx))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/screen")
    async def screen_page():
        return FileResponse(str(STATIC_DIR / "screen.html"))

    @app.get("/admin")
    async def admin_page():
        return FileResponse(str(STATIC_DIR / "admin.html"))

    return app


def main() -> None:
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    cfg = Config()
    app = create_app(cfg)
    ctx = app.state.ctx
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port))
    start_tray(base_url=f"http://127.0.0.1:{cfg.port}",
               get_state=lambda: ctx.sm.state, server=server)
    server.run()


if __name__ == "__main__":
    main()
