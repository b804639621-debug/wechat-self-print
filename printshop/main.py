"""服务入口：create_app 工厂组装 Config/JobStore/WeChatClient/SessionManager。

启动方式：`python -m printshop.main` 或 `python printshop/main.py`。
PyInstaller 冻结态：chdir 到 exe 同级目录，使 Config 的相对路径（inbox/work/db）
落在 exe 旁而非 _MEI 临时目录（见 printshop.spec）。
示例：
    python -m printshop.main [--web | --ble-selftest] [--port 18080] [--host 0.0.0.0]

端口覆盖优先级（高→低）：CLI --port/--host > 环境变量 PRINTSHOP_PORT/PRINTSHOP_HOST
> exe 同级 printshop.json > 默认 18080（见 docs/plan-port-conflict-solution.md）。
启动前做端口预检：被占则按"是否本服务在运行"区分提示后退出，
绝不静默换端口（ESP32 小屏 HTTP 轮询依赖编译期写死的端口）。
退出码：0 正常/让位；2 启动失败；3 端口被其他程序占用。
"""
import argparse
import asyncio
import contextlib
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from printshop.ble_link import BleScreenLink, run_ble_selftest
from printshop.config import Config
from printshop.jobs import JobStore
from printshop.logging_setup import (
    configure_file_logging,
    redirect_std_streams_to_logging,
    uvicorn_log_config,
)
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
                ctx.emit("device", **ctx.device_payload())
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

    @app.get("/api/screen/fingerprint")
    async def fingerprint():
        """双实例互斥探测指纹（无敏感信息）：第二份进程启动时 GET 本端点判定
        "端口被占"是否为本服务自身（_port_is_own_service）。"""
        return {"service": "WeChat Self-Print", "port": ctx.cfg.port}

    return app


def _run_web_mode(cfg: Config, app: FastAPI, ctx: AppContext,
               server: uvicorn.Server, log_path: Path,
               log_opener=None) -> None:
    """等价旧行为：托盘 + 主线程阻塞运行 uvicorn（--web 浏览器兜底模式）。"""
    if log_opener is None:
        log_opener = lambda: os.startfile(str(log_path))
    start_tray(base_url=f"http://127.0.0.1:{cfg.port}",
               get_state=lambda: ctx.sm.state, server=server,
               log_opener=log_opener)
    server.run()


def _run_gui_mode(cfg: Config, app: FastAPI, ctx: AppContext,
                  server: uvicorn.Server, log_path: Path,
                  log_opener=None) -> int:
    """默认 GUI 模式：后台 uvicorn + pywebview 原生窗口 + 系统托盘。"""
    from printshop.gui import GuiController

    server_thread = threading.Thread(target=server.run, name="uvicorn-gui", daemon=True)
    server_thread.start()

    deadline = time.monotonic() + 15.0
    while not server.started:
        if not server_thread.is_alive():
            _log.error("uvicorn 服务启动失败")
            return 2
        if time.monotonic() >= deadline:
            _log.error("uvicorn 服务启动超时（15s）")
            server.should_exit = True
            server_thread.join(timeout=10)
            return 2
        time.sleep(0.05)

    gui = GuiController(f"http://127.0.0.1:{cfg.port}/admin")
    start_tray(base_url=f"http://127.0.0.1:{cfg.port}",
               get_state=lambda: ctx.sm.state, server=server,
               admin_opener=gui.show, on_quit=gui.quit,
               log_opener=log_opener)
    try:
        gui.start_blocking()
    except Exception:
        _log.exception("GUI 启动失败")
        return 2
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
    return 0


def _port_free(host: str, port: int) -> bool:
    """端口预检：能否在本机 bind 该端口（host=0.0.0.0 时试通配地址）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("" if host == "0.0.0.0" else host, port))
            return True
        except OSError:  # WinError 10048(占用)/10013(权限) 等
            return False


def _port_is_own_service(port: int, timeout: float = 2.0) -> bool:
    """双实例互斥判定：GET /api/screen/fingerprint 端点返回本服务指纹。
    注意 HITRAN 全指纹是服务端响应包含 service 字段才判为自身，
    避免同端口误判其他软件。探测失败（无响应/超时）返回 False，
    按"被其他程序占用"处理——宁可报冲突也不静默吞掉真正的异常。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/screen/fingerprint",
                timeout=timeout) as resp:
            return '"WeChat Self-Print"' in resp.read().decode("utf-8", "replace")
    except Exception:
        return False


def _resolve_bind_conflict(cfg: Config) -> int:
    """启动端口预检（fail-fast，不静默换端口——ESP32 小屏靠编译期写死的端口轮询）：
    - 空闲 → 0 正常；被占且确认本服务在运行 → 让位退出 0（托盘日志有说明）；
    - 被占但非本服务 → 指引换端后退出码 3。"""
    if _port_free(cfg.host, cfg.port):
        return 0
    own = _port_is_own_service(cfg.port)
    _log.error(
        "端口 %d 被占用（%s）。可用 --port <新端口> / 环境变量 PRINTSHOP_PORT / "
        "exe 同级 printshop.json 更换（换端口后须同步固件 SERVER_PORT 重烧）",
        cfg.port, "本服务已在运行" if own else "其他程序占用")
    try:
        msg = ("printshop 已在运行（端口 %d），本次启动让位" if own
               else "端口 %d 被其他程序占用：请用 --port 指定其他端口，或编辑 "
               "exe 同级 printshop.json 后重试") % cfg.port
        print(msg, flush=True)
    except Exception:
        pass  # stdout 已被 redirect 到日志，理论上不会失败；防御性兜底
    return 0 if own else 3


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """解析 --port/--host 后从 argv 剥离，保证既有 `--web`/`--ble-selftest`
    用 `in sys.argv` 判断的分支逻辑不受影响。"""
    parser = argparse.ArgumentParser(prog="printshop", add_help=False)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    args, _ = parser.parse_known_args(argv)
    for flag in ("--port", "--host"):
        while flag in argv:
            i = argv.index(flag)
            del argv[i:i + 2]
    return args


def main() -> None:
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    log_path = configure_file_logging()
    redirect_std_streams_to_logging()
    _log.info("启动 printshop，日志文件：%s", log_path)

    if "--ble-selftest" in sys.argv:
        # BLE 自检模式：不起服务/托盘、不碰微信，直接跑真实小屏链路后退出
        sys.exit(asyncio.run(run_ble_selftest(Config())))
    args = _parse_args(sys.argv[1:])
    # 配置优先级（高→低）：CLI > env（__post_init__ 内处理）> exe 同级
    # printshop.json（冻结态已 chdir 到 exe 目录，与数据目录机制一致）> 默认值
    cfg = Config.from_json_file(Path("printshop.json"))
    if args.port is not None:
        cfg.port = args.port  # CLI 显式值最高优先级，压过 env
    if args.host is not None:
        cfg.host = args.host
    app = create_app(cfg)
    ctx = app.state.ctx
    rc = _resolve_bind_conflict(cfg)
    if rc != 0:
        sys.exit(rc)
    server = uvicorn.Server(uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        log_config=uvicorn_log_config(),
    ))
    log_opener = lambda: os.startfile(str(log_path))
    if "--web" in sys.argv:
        _run_web_mode(cfg, app, ctx, server, log_path, log_opener)
    else:
        sys.exit(_run_gui_mode(cfg, app, ctx, server, log_path, log_opener))


if __name__ == "__main__":
    main()
