from printshop.config import Config


def test_config_defaults():
    c = Config()
    assert c.port == 8000
    assert c.idle_sleep_seconds == 180
    assert c.screen_online_threshold_seconds == 12.0
    assert c.inbox_dir.name == "inbox"


def test_config_render_dpi_default():
    c = Config()
    assert c.render_dpi == 300


def test_config_max_render_pages_default():
    c = Config()
    assert c.max_render_pages == 200


def test_config_screen_online_threshold_custom():
    c = Config(screen_online_threshold_seconds=9.5)
    assert c.screen_online_threshold_seconds == 9.5


def test_config_screen_ble_address_default_empty():
    c = Config()
    assert c.screen_ble_address == ""


def test_config_screen_ble_address_env_off(monkeypatch):
    monkeypatch.setenv("PRINTSHOP_SCREEN_BLE_ADDRESS", "off")
    assert Config().screen_ble_address == ""


def test_config_screen_ble_address_env_mac(monkeypatch):
    monkeypatch.setenv("PRINTSHOP_SCREEN_BLE_ADDRESS", "20:6E:F1:16:D9:2E")
    assert Config().screen_ble_address == "20:6E:F1:16:D9:2E"
