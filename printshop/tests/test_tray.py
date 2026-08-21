from printshop.session_manager import SessionState
from printshop.tray import (
    COLOR_IDLE,
    COLOR_PRINTING,
    COLOR_SERVING,
    _icon,
    state_to_color,
)


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
