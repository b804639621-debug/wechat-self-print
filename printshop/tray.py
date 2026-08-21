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
    def __init__(self, base_url: str, get_state: Callable[[], str]):
        self._base = base_url.rstrip("/")
        self._get_state = get_state
        self._stop = threading.Event()
        self._lock = threading.Lock()
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
        try:
            httpx.post(f"{self._base}/api/screen/wake", timeout=5)
        except Exception:
            pass  # 主控未就绪等：静默容错，托盘不得崩溃

    def open_admin(self, *_args) -> None:
        try:
            os.startfile(f"{self._base}/admin")
        except Exception:
            pass

    def quit(self, *_args) -> None:
        self._stop.set()
        self.icon.stop()

    def _poll_loop(self) -> None:
        while not self._stop.wait(POLL_INTERVAL):
            try:
                color = state_to_color(self._get_state())
            except Exception:
                color = COLOR_IDLE
            try:
                with self._lock:
                    self.icon.icon = _icon(color)
            except Exception:
                pass

    def run_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.icon.run, daemon=True)
        t.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        return t


def start_tray(base_url: str, get_state: Callable[[], str]) -> TrayApp:
    tray = TrayApp(base_url=base_url, get_state=get_state)
    tray.run_thread()
    return tray
