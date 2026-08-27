from unittest.mock import MagicMock, patch

import pytest

from printshop.gui import QUIT, SHOW, GuiController


class _ClosingEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeEvents:
    def __init__(self):
        self.closing = _ClosingEvent()


def test_show_and_quit_are_enqueued():
    gui = GuiController("http://127.0.0.1:8000/admin")
    assert gui._queue.empty()

    gui.show()
    assert gui._queue.get_nowait() == SHOW

    gui.quit()
    assert gui._queue.get_nowait() == QUIT


def test_start_blocking_creates_window_and_starts_edgechromium():
    gui = GuiController("http://127.0.0.1:8000/admin")
    fake_window = MagicMock()
    fake_window.events = _FakeEvents()
    captured = {}

    def fake_start(func, args, gui=None):
        captured["func"] = func
        captured["args"] = args
        captured["gui"] = gui

    with patch("printshop.gui.webview.create_window", return_value=fake_window) as create:
        with patch("printshop.gui.webview.start", side_effect=fake_start) as start:
            gui.start_blocking()

    create.assert_called_once_with(
        "打印管理后台",
        gui.url,
        width=1100,
        height=760,
        min_size=(960, 640),
    )
    assert fake_window.events.closing.handlers == [gui._on_closing]
    start.assert_called_once_with(gui._command_worker, (gui._queue,), gui="edgechromium")
    assert gui._window is fake_window


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


def test_command_worker_processes_show_then_quit():
    gui = GuiController("http://127.0.0.1:8000/admin")
    fake_window = MagicMock()
    fake_window.events.closed.is_set.return_value = False
    gui._window = fake_window
    gui._queue.put(SHOW)
    gui._queue.put(QUIT)

    gui._command_worker(gui._queue)

    fake_window.restore.assert_called_once_with()
    fake_window.show.assert_called_once_with()
    fake_window.destroy.assert_called_once_with()
    assert gui._allow_close is True


def test_command_worker_exits_when_finished_and_queue_idle():
    gui = GuiController("http://127.0.0.1:8000/admin")
    fake_window = MagicMock()
    gui._window = fake_window
    gui._finished.set()

    gui._command_worker(gui._queue)

    assert gui._allow_close is False


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
