from unittest.mock import MagicMock, patch

import pytest

from printshop.gui import GuiController


class _ClosingEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _ShownEvent:
    def wait(self, timeout):
        return True


class _FakeEvents:
    def __init__(self):
        self.closing = _ClosingEvent()
        self.shown = _ShownEvent()


def test_start_blocking_creates_window_and_starts_edgechromium():
    gui = GuiController("http://127.0.0.1:8000/admin")
    fake_window = MagicMock()
    fake_window.uid = "win-1"
    fake_window.events = _FakeEvents()
    fake_form = MagicMock()
    fake_form.Handle.ToInt64.return_value = 456
    fake_window.gui.BrowserView.instances = {"win-1": fake_form}
    captured = {}

    def fake_start(func, args, gui=None):
        captured["func"] = func
        captured["args"] = args
        captured["gui"] = gui

    with patch("printshop.gui.webview.create_window", return_value=fake_window) as create:
        with patch("printshop.gui.webview.start", side_effect=fake_start) as start:
            gui.start_blocking()
            # 模拟 pywebview 启动后在线程中执行初始化回调。
            captured["func"]()

    create.assert_called_once_with(
        "打印管理后台",
        gui.url,
        width=1100,
        height=760,
        min_size=(960, 640),
    )
    assert fake_window.events.closing.handlers == [gui._on_closing]
    start.assert_called_once_with(gui._init_window, (), gui="edgechromium")
    assert gui._window is fake_window
    assert gui._hwnd == 456


def test_on_closing_hides_by_default_and_allows_after_quit():
    gui = GuiController("http://127.0.0.1:8000/admin")
    fake_window = MagicMock()
    gui._window = fake_window

    assert gui._on_closing() is False
    fake_window.hide.assert_called_once_with()

    gui._allow_close = True
    fake_window.hide.reset_mock()
    assert gui._on_closing() is None
    fake_window.hide.assert_not_called()


def test_show_uses_win32_apis():
    gui = GuiController("http://127.0.0.1:8000/admin")
    gui._hwnd = 123

    with patch("printshop.gui.ctypes.windll", create=True) as windll:
        gui.show()

    windll.user32.ShowWindow.assert_any_call(123, 9)
    windll.user32.ShowWindow.assert_any_call(123, 5)
    windll.user32.SetForegroundWindow.assert_called_once_with(123)


def test_show_without_hwnd_does_not_call_win32():
    gui = GuiController("http://127.0.0.1:8000/admin")
    gui._window = None

    with patch("printshop.gui.ctypes.windll", create=True) as windll:
        gui.show()

    windll.user32.ShowWindow.assert_not_called()
    windll.user32.SetForegroundWindow.assert_not_called()


def test_quit_posts_wm_close():
    gui = GuiController("http://127.0.0.1:8000/admin")
    gui._hwnd = 123

    with patch("printshop.gui.ctypes.windll", create=True) as windll:
        gui.quit()

    assert gui._allow_close is True
    windll.user32.PostMessageW.assert_called_once_with(123, 0x10, 0, 0)


def test_quit_without_hwnd_does_not_post_message():
    gui = GuiController("http://127.0.0.1:8000/admin")
    gui._window = None

    with patch("printshop.gui.ctypes.windll", create=True) as windll:
        gui.quit()

    assert gui._allow_close is True
    windll.user32.PostMessageW.assert_not_called()


def test_start_blocking_shows_error_and_reraises_on_webview_start_failure():
    gui = GuiController("http://127.0.0.1:8000/admin")
    fake_window = MagicMock()
    fake_window.events = _FakeEvents()

    with patch("printshop.gui.webview.create_window", return_value=fake_window):
        with patch(
            "printshop.gui.webview.start",
            side_effect=RuntimeError("WebView2 missing"),
        ):
            with patch.object(gui, "_show_webview_missing_error") as show_error:
                with pytest.raises(RuntimeError):
                    gui.start_blocking()

    show_error.assert_called_once_with()


def test_show_webview_missing_error_uses_message_box():
    gui = GuiController("http://127.0.0.1:8000/admin")
    message_box = MagicMock()

    with patch("printshop.gui.ctypes.windll", create=True) as windll:
        windll.user32.MessageBoxW = message_box
        gui._show_webview_missing_error()

    message_box.assert_called_once()
    text = message_box.call_args[0][1]
    assert "缺少 Microsoft Edge WebView2 Runtime" in text
    assert "--web" in text
