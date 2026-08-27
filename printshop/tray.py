"""系统托盘：左键点亮小屏+刷新二维码；右键菜单；图标随会话状态变色。"""
import os
import threading
from typing import Callable

import httpx
import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from printshop.session_manager import SessionState

COLOR_IDLE = "#9E9E9E"
COLOR_SERVING = "#4CAF50"
COLOR_PRINTING = "#F44336"

POLL_INTERVAL = 2.0

_STATE_COLORS = {
    SessionState.IDLE: COLOR_IDLE,
    SessionState.SCANNED: COLOR_SERVING,
    SessionState.CONNECTED: COLOR_SERVING,
    SessionState.RECEIVING: COLOR_SERVING,
    SessionState.PRINTING: COLOR_PRINTING,
}


def state_to_color(state: str) -> str:
    return _STATE_COLORS.get(state, COLOR_IDLE)


def _icon(color: str) -> Image.Image:
    img = Image.new("RGB", (64, 64), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


class TrayApp:
    def __init__(self, base_url: str, get_state: Callable[[], str], server=None,
                 admin_opener: Callable[[], None] | None = None):
        self._base = base_url.rstrip("/")
        self._get_state = get_state
        self._server = server
        self._admin_opener = admin_opener
        self._stop = threading.Event()
        self.icon = pystray.Icon(
            "printshop",
            _icon(COLOR_IDLE),
            "自助打印",
            Menu(
                MenuItem("点亮屏幕", self.wake, default=True),
                MenuItem("打开管理界面", self.open_admin),
                MenuItem("退出", self.quit),
            ),
        )

    def wake(self, *_args) -> None:
        # 菜单回调运行在 pystray 的消息循环线程：HTTP 必须丢到一次性 daemon
        # 线程，否则最长 5s 的超时会把托盘交互冻死。
        threading.Thread(target=self._wake_worker, daemon=True).start()

    def _wake_worker(self) -> None:
        try:
            # trust_env=False：回环请求不受 HTTP_PROXY 等环境代理影响，
            # 避免公司代理把 127.0.0.1 请求黑洞化。
            with httpx.Client(trust_env=False) as client:
                client.post(f"{self._base}/api/screen/wake", timeout=5)
        except Exception:
            pass  # 主控未就绪等：静默容错，托盘不得崩溃
        self.open_admin()  # 点亮后同步打开后台；POST 异常也继续打开

    def open_admin(self, *_args) -> None:
        try:
            if self._admin_opener is not None:
                self._admin_opener()
            else:
                os.startfile(f"{self._base}/admin")
        except Exception:
            pass

    def quit(self, *_args) -> None:
        self._stop.set()
        if self._server is not None:
            # uvicorn 标准退出机制：main_loop 每 0.1s 轮询 should_exit，
            # 置位后走优雅关闭（收尾连接 + lifespan 停机）。
            self._server.should_exit = True
        self.icon.stop()

    def _poll_loop(self) -> None:
        # 已知边界：icon.icon setter 在调用线程执行 _update_icon（写临时 ICO、
        # LoadImage、Shell_NotifyIcon），与 pystray 内部主循环存在理论竞争。
        # 接受此风险：更新幂等，最坏情况是图标闪烁一次。
        last_color = None
        while not self._stop.wait(POLL_INTERVAL):
            try:
                color = state_to_color(self._get_state())
            except Exception:
                color = COLOR_IDLE
            if color == last_color:
                # 颜色未变则跳过：win32 setter 每次都会重建图标句柄，
                # 无意义刷新会造成磁盘 I/O 与 GDI 抖动。
                continue
            try:
                self.icon.icon = _icon(color)
            except Exception:
                pass  # 单次更新失败：保持 last_color 不变，下一轮重试
            else:
                last_color = color

    def run_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.icon.run, daemon=True)
        t.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        return t


def start_tray(base_url: str, get_state: Callable[[], str], server=None,
               admin_opener: Callable[[], None] | None = None) -> TrayApp:
    tray = TrayApp(base_url=base_url, get_state=get_state, server=server,
                   admin_opener=admin_opener)
    tray.run_thread()
    return tray
