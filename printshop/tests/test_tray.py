import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from printshop.session_manager import SessionState
from printshop.tray import (
    COLOR_IDLE,
    COLOR_PRINTING,
    COLOR_SERVING,
    _icon,
    state_to_color,
)


class _FakeIcon:
    def __init__(self):
        self._icon = None
        self.assignments = 0
        self.stopped = False

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value):
        self._icon = value
        self.assignments += 1

    def stop(self):
        self.stopped = True


@pytest.fixture
def tray_factory():
    def make(base="http://127.0.0.1:8000", get_state=lambda: SessionState.IDLE,
             server=None):
        with patch("printshop.tray.pystray.Icon", return_value=_FakeIcon()):
            from printshop.tray import TrayApp
            return TrayApp(base_url=base, get_state=get_state, server=server)
    return make


def _run_poll_loop(tray, seconds=0.05):
    tray._stop.clear()
    t = threading.Thread(target=tray._poll_loop, daemon=True)
    t.start()
    time.sleep(seconds)
    tray._stop.set()
    t.join(timeout=2)


def test_state_to_color_idle():
    assert state_to_color(SessionState.IDLE) == COLOR_IDLE


def test_state_to_color_serving_states():
    for s in (SessionState.SCANNED, SessionState.CONNECTED, SessionState.RECEIVING):
        assert state_to_color(s) == COLOR_SERVING


def test_state_to_color_printing():
    assert state_to_color(SessionState.PRINTING) == COLOR_PRINTING


def test_state_to_color_unknown_falls_back_idle():
    assert state_to_color("UNKNOWN") == COLOR_IDLE


def test_icon_geometry():
    img = _icon(COLOR_SERVING)
    assert img.size == (64, 64)
    assert img.mode == "RGB"
    assert img.getpixel((32, 32)) == (76, 175, 80)
    assert img.getpixel((2, 2)) == (0, 0, 0)


def test_poll_loop_falls_back_idle_when_get_state_raises(tray_factory):
    def boom():
        raise RuntimeError("boom")

    tray = tray_factory(get_state=boom)
    with patch("printshop.tray.POLL_INTERVAL", 0.005):
        _run_poll_loop(tray)
    assert tray.icon.assignments == 1
    assert tray.icon.icon.getpixel((32, 32)) == (158, 158, 158)


def test_poll_loop_skips_unchanged_color(tray_factory):
    state = {"v": SessionState.IDLE}
    tray = tray_factory(get_state=lambda: state["v"])
    with patch("printshop.tray.POLL_INTERVAL", 0.005):
        _run_poll_loop(tray)
    assert tray.icon.assignments == 1
    state["v"] = SessionState.PRINTING
    with patch("printshop.tray.POLL_INTERVAL", 0.005):
        _run_poll_loop(tray)
    assert tray.icon.assignments == 2


def test_poll_loop_exits_when_stop_preset(tray_factory):
    tray = tray_factory()
    tray._stop.set()
    tray._poll_loop()
    assert tray.icon.assignments == 0


def test_wake_is_non_blocking_and_spawns_worker(tray_factory):
    tray = tray_factory()
    captured = {}

    def fake_start(self):
        captured["target"] = self._target

    with patch("printshop.tray.threading.Thread.start", fake_start):
        tray.wake()
    assert captured["target"] == tray._wake_worker


def test_wake_worker_swallows_error_and_skips_proxy(tray_factory):
    tray = tray_factory()
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.side_effect = httpx.ConnectError("proxy hole")
    with patch("printshop.tray.httpx.Client", return_value=client) as cls:
        tray._wake_worker()
    cls.assert_called_once_with(trust_env=False)
    client.post.assert_called_once_with(
        "http://127.0.0.1:8000/api/screen/wake", timeout=5)


def test_quit_sets_server_should_exit_and_stops_icon(tray_factory):
    server = MagicMock()
    tray = tray_factory(server=server)
    tray.quit()
    assert server.should_exit is True
    assert tray.icon.stopped is True
