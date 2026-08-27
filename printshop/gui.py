"""GUI 控制器：用 pywebview 在原生窗口加载后台页面，并桥接系统托盘命令。

窗口关闭只隐藏到托盘；托盘“退出”才真正销毁窗口并结束 GUI 事件循环。
所有来自托盘线程的 UI 操作都通过 queue 投递，由 pywebview 启动的 worker
线程消费，避免跨线程直接操作 WinForms 控件。
"""
from __future__ import annotations

import ctypes
import queue
import threading

import webview

SHOW = "SHOW"
QUIT = "QUIT"

_WEBVIEW_START_ERROR_MESSAGE = (
    "GUI 启动失败：缺少 Microsoft Edge WebView2 Runtime 或初始化异常。\n"
    "请安装 WebView2 Runtime，或用 --web 模式启动。"
)


class GuiController:
    """管理 pywebview 窗口和 UI 命令队列。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._window = None
        self._allow_close = False
        self._finished = threading.Event()

    def show(self) -> None:
        """线程安全：请求显示/恢复窗口。"""
        self._queue.put(SHOW)

    def quit(self) -> None:
        """线程安全：请求销毁窗口并结束 GUI 循环。"""
        self._queue.put(QUIT)

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
            webview.start(self._command_worker, (self._queue,), gui="edgechromium")
        except Exception:
            self._show_webview_missing_error()
            raise
        finally:
            # 无论正常退出还是异常退出，都允许关闭并让 worker 尽快收尾。
            self._allow_close = True
            self._finished.set()

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

    def _command_worker(self, queue_: "queue.Queue[str]") -> None:
        """pywebview.start 的 func：在独立线程消费 UI 命令。"""
        while not self._allow_close and not self._finished.is_set():
            try:
                command = queue_.get(timeout=0.5)
            except queue.Empty:
                # 空闲时检查退出标志，避免 GUI 已结束而 worker 永久阻塞。
                continue

            if command == SHOW:
                self._show_window()
            elif command == QUIT:
                self._allow_close = True
                try:
                    if self._window is not None:
                        self._window.destroy()
                except Exception:
                    pass
                break

    def _show_window(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            try:
                window.restore()
            except Exception:
                pass
            window.show()
            # EdgeChromium/WinForms 支持 on_top；不支持时忽略即可。
            try:
                window.on_top = True
                window.on_top = False
            except Exception:
                pass
        except Exception:
            pass

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
