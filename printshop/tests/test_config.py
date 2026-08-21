from printshop.config import Config


def test_config_defaults():
    c = Config()
    assert c.port == 8000
    assert c.idle_sleep_seconds == 180
    assert c.inbox_dir.name == "inbox"


def test_config_render_dpi_default():
    c = Config()
    assert c.render_dpi == 300
