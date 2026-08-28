"""GUI 控制器：用 pywebview 在原生窗口加载后台页面，并桥接系统托盘命令。

窗口关闭只隐藏到托盘；托盘“退出”才真正销毁窗口并结束 GUI 事件循环。
托盘线程只调用 Win32 窗口消息（ShowWindow / SetForegroundWindow /
PostMessage），不直接调用 pywebview / WinForms 方法，规避跨线程 Invoke
死锁。
"""
from __future__ import annotations

import ctypes
import logging
import threading

import webview

WM_CLOSE = 0x0010
SW_RESTORE = 9
SW_SHOW = 5

_WEBVIEW_START_ERROR_MESSAGE = (
    "GUI 启动失败：缺少 Microsoft Edge WebView2 Runtime 或初始化异常。\n"
    "请安装 WebView2 Runtime，或用 --web 模式启动。"
)

_log = logging.getLogger(__name__)


class GuiController:
    """管理 pywebview 窗口，并缓存 Win32 窗口句柄供托盘线程使用。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self._window = None
        self._hwnd: int | None = None
        self._allow_close = False
        self._finished = threading.Event()

    def show(self) -> None:
        """线程安全：通过 Win32 API 显示/恢复窗口，不触碰 WinForms 控件。"""
        try:
            hwnd = self._get_hwnd()
            if hwnd is None:
                _log.warning("无法显示 GUI 窗口：尚未取得窗口句柄")
                return

            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.ShowWindow(hwnd, SW_SHOW)
            user32.SetForegroundWindow(hwnd)
            _log.info("显示 GUI 窗口 hwnd=%s", hwnd)
        except Exception:
            _log.warning("显示 GUI 窗口失败", exc_info=True)

    def quit(self) -> None:
        """线程安全：通过 PostMessage 请求关闭窗口，不阻塞调用线程。"""
        self._allow_close = True
        try:
            hwnd = self._get_hwnd()
            if hwnd is None:
                _log.warning("无法关闭 GUI 窗口：尚未取得窗口句柄")
                return

            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            _log.info("请求关闭 GUI 窗口 hwnd=%s", hwnd)
        except Exception:
            _log.warning("请求关闭 GUI 窗口失败", exc_info=True)

    def start_blocking(self) -> None:
        """创建窗口并启动 pywebview 事件循环（必须在主线程调用）。"""
        self._window = webview.create_window(
            "打印管理后台",
            self.url,
            width=1100,
            height=760,
            min_size=(960, 640),
        )
        if self._window is not None:
            self._window.events.closing += self._on_closing
        try:
            webview.start(self._init_window, (), gui="edgechromium")
        except Exception:
            self._show_webview_missing_error()
            raise
        finally:
            # 无论正常退出还是异常退出，都允许关闭并让初始化线程尽快收尾。
            self._allow_close = True
            self._finished.set()

    def _init_window(self) -> None:
        """pywebview.start 的 func：初始化线程中缓存句柄后立即返回。"""
        if self._window is not None:
            self._window.events.shown.wait(15)
        hwnd = self._cache_hwnd()
        if hwnd is None:
            _log.warning("无法获取 GUI 窗口句柄")

    def _cache_hwnd(self) -> int | None:
        """从 WinForms BrowserView 实例缓存窗口句柄。"""
        try:
            window = self._window
            if window is None:
                return None

            instances = window.gui.BrowserView.instances
            form = instances[window.uid]
            handle = form.Handle
            try:
                hwnd = int(handle.ToInt64())
            except Exception:
                hwnd = int(handle)
            self._hwnd = hwnd
            return hwnd
        except Exception:
            _log.warning("缓存 GUI 窗口句柄失败", exc_info=True)
            return None

    def _get_hwnd(self) -> int | None:
        """返回缓存的窗口句柄；若尚未缓存则尝试立即获取。"""
        if self._hwnd is not None:
            return self._hwnd
        return self._cache_hwnd()

    def _on_closing(self) -> bool | None:
        """窗口关闭事件：未请求退出时隐藏到托盘并取消关闭。"""
        if self._allow_close:
            return None
        try:
            if self._window is not None:
                self._window.hide()
        except Exception:
            pass
        return False

    def _show_webview_missing_error(self) -> None:
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                _WEBVIEW_START_ERROR_MESSAGE,
                "打印管理后台",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
