# -*- coding: utf-8 -*-
"""端口配置/预检/双实例互斥单测（docs/plan-port-conflict-solution.md §3.7）。"""
import socket
import urllib.error
from pathlib import Path

from printshop.config import Config
from printshop.main import _parse_args, _port_free, _port_is_own_service


def test_config_default_port_is_18080():
    c = Config()
    assert c.port == 18080


def test_config_port_env_override(monkeypatch):
    monkeypatch.setenv("PRINTSHOP_PORT", "19000")
    assert Config().port == 19000


def test_config_port_env_invalid_keeps_default(monkeypatch):
    monkeypatch.setenv("PRINTSHOP_PORT", "not-a-number")
    assert Config().port == 18080


def test_config_host_env_override(monkeypatch):
    monkeypatch.setenv("PRINTSHOP_HOST", "127.0.0.1")
    assert Config().host == "127.0.0.1"


def _write_json(p: Path, content: str) -> Path:
    p.write_text(content, encoding="utf-8")
    return p


def test_from_json_file_missing_is_default(tmp_path: Path):
    c = Config.from_json_file(tmp_path / "printshop.json")  # 文件不存在
    assert c.port == 18080 and c.host == "0.0.0.0"


def test_from_json_file_broken_json_is_default(tmp_path: Path):
    p = _write_json(tmp_path / "printshop.json", "{not json")
    c = Config.from_json_file(p)
    assert c.port == 18080 and c.host == "0.0.0.0"


def test_from_json_file_non_dict_is_default(tmp_path: Path):
    p = _write_json(tmp_path / "printshop.json", "[1,2]")
    c = Config.from_json_file(p)
    assert c.port == 18080


def test_from_json_file_port_only_port_updated(tmp_path: Path):
    p = _write_json(tmp_path / "printshop.json", '{"port": 19000}')
    c = Config.from_json_file(p)
    assert c.port == 19000 and c.host == "0.0.0.0"


def test_from_json_file_out_of_range_port_ignored(tmp_path: Path):
    p = _write_json(tmp_path / "printshop.json", '{"port": 99999, "host": "127.0.0.1"}')
    c = Config.from_json_file(p)
    assert c.port == 18080 and c.host == "127.0.0.1"


def test_from_json_file_full(tmp_path: Path):
    p = _write_json(tmp_path / "printshop.json",
                    '{"port": 20000, "host": "10.0.0.5"}')
    c = Config.from_json_file(p)
    assert c.port == 20000 and c.host == "10.0.0.5"


def test_precedence_cli_above_env_and_json(monkeypatch, tmp_path: Path):
    # CLI 最高：构造时传 port 压过 env；JSON 只在没有 CLI/时被 main() 读取
    monkeypatch.setenv("PRINTSHOP_PORT", "19000")
    c = Config(port=21000)
    assert c.port == 21000


def test_parse_args_strips_port_and_host():
    argv = ["--port", "19000", "--web", "--host", "127.0.0.1"]
    args = _parse_args(argv)
    assert args.port == 19000 and args.host == "127.0.0.1"
    assert "--web" in argv and "--port" not in argv and "--host" not in argv


def test_parse_args_none_when_absent():
    args = _parse_args(["--web"])
    assert args.port is None and args.host is None


def test_port_free_true_when_free():
    # 由内核随机分配的空闲端口（bind 0 递让内核挑选）
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _port_free("127.0.0.1", port) is True


def test_port_free_false_when_listening_occupied():
    # 先占住端口（内核分配），占用期间 _port_free 必须失败
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert _port_free("127.0.0.1", port) is False
    finally:
        s.close()


def test_port_is_own_service_true(monkeypatch):
    # mock urlopen 返回带指纹的响应（不真实连网）
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"service":"WeChat Self-Print","port":18080}'

    import printshop.main as m
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert _port_is_own_service(18080) is True


def test_port_is_own_service_foreign_http_false(monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>other service</html>"

    import printshop.main as m
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    # 无本服务指纹 → 非本服务
    assert _port_is_own_service(18080) is False


def test_port_is_own_service_conn_refused_false(monkeypatch):
    import printshop.main as m

    def raise_refused(*a, **k):
        raise urllib.error.URLError("Connection refused")
    monkeypatch.setattr(m.urllib.request, "urlopen", raise_refused)
    assert _port_is_own_service(18080) is False
