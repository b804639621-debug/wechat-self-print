"""Task 11 加固与恢复：断线重连（指数退避）、凭证恢复、看门狗、定时保存会话。"""
import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from printshop.config import Config
from printshop.session_manager import SessionManager, SessionState, _is_network_error


def _recorder(store):
    async def record(seconds):
        store.append(seconds)
    return record


async def _noop_receive():
    return {"type": "websocket.receive", "bytes": None, "text": None}


def _sm(cfg=None, store=None, **kw):
    cfg = cfg or Config()
    return SessionManager(cfg, MagicMock(), store or MagicMock(), **kw)


def test_config_recovery_defaults():
    c = Config()
    assert c.network_backoff == (5, 15, 60)
    assert c.network_max_failures == 3
    assert c.session_save_interval == 600
    assert c.office_convert_timeout == 120.0


def test_network_error_classification():
    assert _is_network_error(httpx.ConnectError("down")) is True
    assert _is_network_error(httpx.TimeoutException("slow")) is True
    req = httpx.Request("GET", "https://x/")
    resp = httpx.Response(400, request=req)
    assert _is_network_error(httpx.HTTPStatusError("400", request=req, response=resp)) is False
    assert _is_network_error(ValueError("nope")) is False


@pytest.mark.asyncio
async def test_restore_session_valid_goes_connected_no_scan():
    sm = _sm()
    sm._wx.restore_session = AsyncMock(return_value=True)
    ok = await sm.restore_session()
    assert ok is True
    assert sm.state == SessionState.CONNECTED
    assert sm.display is True


@pytest.mark.asyncio
async def test_restore_session_invalid_stays_idle():
    sm = _sm()
    sm._wx.restore_session = AsyncMock(return_value=False)
    ok = await sm.restore_session()
    assert ok is False
    assert sm.state == SessionState.IDLE
    assert sm.display is False


@pytest.mark.asyncio
async def test_restore_session_exception_falls_back_to_idle():
    sm = _sm()
    sm._wx.restore_session = AsyncMock(side_effect=RuntimeError("boom"))
    ok = await sm.restore_session()
    assert ok is False
    assert sm.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_network_failure_backoff_keeps_connected(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=3)
    sm.display = True
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(side_effect=httpx.ConnectError("network down"))
    sm._wx.logout = AsyncMock()
    sleeps = []
    sm._sleep = _recorder(sleeps)
    await sm.run_loop()
    assert [s for s in sleeps if s >= 1] == [5, 15, 60]
    assert sm._wx.fetch_messages.await_count == 3
    assert sm.state == SessionState.CONNECTED
    sm._wx.logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_backoff_exhausted_logout_fresh_qr(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001
    cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=4)
    sm.display = True
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(side_effect=httpx.ConnectError("network down"))
    sm._wx.logout = AsyncMock()
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "https://login.weixin.qq.com/l/fresh"))
    sleeps = []
    sm._sleep = _recorder(sleeps)
    await sm.run_loop()
    assert sm.state == SessionState.IDLE
    assert sm.display is True
    assert sm.qr_content.endswith("/l/fresh")
    sm._wx.logout.assert_awaited_once()
    assert sm._wx.fetch_messages.await_count == 4


@pytest.mark.asyncio
async def test_reconnect_counter_resets_on_success(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=4)
    sm.display = True
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(side_effect=[
        httpx.ConnectError("x"), httpx.ConnectError("x"), [], httpx.ConnectError("x")])
    sm._wx.is_authed = MagicMock(return_value=True)
    sm._wx.logout = AsyncMock()
    sleeps = []
    sm._sleep = _recorder(sleeps)
    await sm.run_loop()
    assert [s for s in sleeps if s >= 1] == [5, 15, 5]
    sm._wx.logout.assert_not_awaited()
    assert sm.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_non_network_exception_not_counted_as_failure(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=3)
    sm.display = True
    sm.state = SessionState.CONNECTED
    req = httpx.Request("GET", "https://login.weixin.qq.com/")
    resp = httpx.Response(400, request=req)
    exc = httpx.HTTPStatusError("400 Bad Request", request=req, response=resp)
    sm._wx.fetch_messages = AsyncMock(side_effect=exc)
    sm._wx.logout = AsyncMock()
    sleeps = []
    sm._sleep = _recorder(sleeps)
    await sm.run_loop()
    assert sm._wx.fetch_messages.await_count == 3
    assert [s for s in sleeps if s >= 1] == []
    assert sm.state == SessionState.CONNECTED
    sm._wx.logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_session_on_login_success():
    sm = _sm()
    sm.display = True
    sm._wx.save_session = AsyncMock()
    await sm._handle_poll("confirmed")
    assert sm.state == SessionState.CONNECTED
    sm._wx.save_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_periodic_save_after_interval(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = _sm(cfg)
    sm.state = SessionState.CONNECTED
    sm._wx.save_session = AsyncMock()
    sm._last_save_ts = time.monotonic() - 601
    await sm._maybe_periodic_save()
    sm._wx.save_session.assert_awaited_once()
    assert sm._last_save_ts > time.monotonic() - 1


@pytest.mark.asyncio
async def test_periodic_save_skipped_within_interval(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = _sm(cfg)
    sm.state = SessionState.CONNECTED
    sm._wx.save_session = AsyncMock()
    await sm._maybe_periodic_save()
    sm._wx.save_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_periodic_save_not_in_logged_in_state(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = _sm(cfg)
    sm.state = SessionState.IDLE
    sm._last_save_ts = time.monotonic() - 9999
    sm._wx.save_session = AsyncMock()
    await sm._maybe_periodic_save()
    sm._wx.save_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_periodic_save_in_run_loop(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=2)
    sm.display = True
    sm.state = SessionState.CONNECTED
    sm._last_save_ts = time.monotonic() - 601
    sm._wx.fetch_messages = AsyncMock(return_value=[])
    sm._wx.is_authed = MagicMock(return_value=True)
    sm._wx.save_session = AsyncMock(return_value=True)
    await sm.run_loop()
    sm._wx.save_session.assert_awaited_once()
    assert sm._last_save_ts > time.monotonic() - 1


@pytest.mark.asyncio
async def test_run_loop_watchdog_survives_iteration_error(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=3)
    sm.display = True
    sm._wx.poll_login = AsyncMock(side_effect=RuntimeError("boom"))
    await sm.run_loop()
    assert sm._wx.poll_login.await_count == 3


@pytest.mark.asyncio
async def test_invalid_session_logs_out_to_idle_fresh_qr(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001
    cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=1)
    sm.display = True
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[])
    sm._wx.is_authed = MagicMock(return_value=False)
    sm._wx.logout = AsyncMock()
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "https://login.weixin.qq.com/l/fresh"))
    await sm.run_loop()
    assert sm.state == SessionState.IDLE
    assert sm.display is True
    assert sm.qr_content.endswith("/l/fresh")
    sm._wx.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_fetch_with_valid_session_keeps_connected(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=2)
    sm.display = True
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[])
    sm._wx.is_authed = MagicMock(return_value=True)
    sm._wx.logout = AsyncMock()
    await sm.run_loop()
    assert sm.state == SessionState.CONNECTED
    sm._wx.logout.assert_not_awaited()
    assert sm._reconnect_failures == 0


@pytest.mark.asyncio
async def test_finish_service_resets_reconnect_counter(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
    sm = _sm(cfg)
    sm._wx.logout = AsyncMock()
    sm.state = SessionState.CONNECTED
    sm._reconnect_failures = 3
    await sm.finish_service()
    assert sm._reconnect_failures == 0


@pytest.mark.asyncio
async def test_periodic_save_retries_next_round_on_failure(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = _sm(cfg)
    sm.state = SessionState.CONNECTED
    sm._last_save_ts = time.monotonic() - 601
    old = sm._last_save_ts
    sm._wx.save_session = AsyncMock(side_effect=RuntimeError("disk full"))
    await sm._maybe_periodic_save()
    assert sm._last_save_ts == old  # 失败不推进 → 下轮即重试
    sm._wx.save_session = AsyncMock(return_value=True)
    await sm._maybe_periodic_save()
    sm._wx.save_session.assert_awaited_once()
    assert sm._last_save_ts > time.monotonic() - 1


@pytest.mark.asyncio
async def test_watchdog_logs_rate_limited(tmp_path, caplog):
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=25)
    sm.display = True
    sm._wx.poll_login = AsyncMock(side_effect=RuntimeError("boom"))
    with caplog.at_level(logging.WARNING, logger="printshop.session_manager"):
        await sm.run_loop()
    records = [r for r in caplog.records if r.name == "printshop.session_manager"]
    errors = [r for r in records if r.levelno == logging.ERROR]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(errors) == 3      # n=1、10、20 记完整 traceback
    assert len(warnings) == 22   # 其余一行摘要


@pytest.mark.asyncio
async def test_invalid_session_detected_with_real_is_authed(tmp_path):
    """防回归：真 WeChatClient.is_authed 为 sync 方法（纯属性读取），不得 await。

    若 session_manager 误用 await 调用 sync is_authed，运行期抛 TypeError（bool 不可 await）
    被看门狗吞掉，登出不触发、state 停留 CONNECTED —— 本测试将变红。
    """
    from printshop.wechat_bot import WeChatClient

    cfg = Config(); cfg.poll_interval = 0.001
    cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
    wx = WeChatClient(entry_host="szfilehelper.weixin.qq.com")
    wx._bot = MagicMock()
    wx._bot.is_logged_in = False
    wx.fetch_messages = AsyncMock(return_value=[])
    wx.logout = AsyncMock()
    wx.get_qr = AsyncMock(return_value=(b"P", "https://login.weixin.qq.com/l/fresh"))
    sm = SessionManager(cfg, wx, MagicMock(), max_iterations=1)
    sm.display = True
    sm.state = SessionState.CONNECTED
    await sm.run_loop()
    assert sm.state == SessionState.IDLE
    assert sm.display is True
    assert sm.qr_content.endswith("/l/fresh")
    wx.logout.assert_awaited_once()


def test_lifespan_restore_session_enters_service_state(tmp_path):
    from fastapi.testclient import TestClient
    from printshop.main import create_app

    cfg = Config()
    cfg.inbox_dir = tmp_path / "inbox"
    cfg.work_dir = tmp_path / "work"
    cfg.db_path = tmp_path / "t.db"
    wx = AsyncMock()
    wx.restore_session = AsyncMock(return_value=True)
    wx.fetch_messages = AsyncMock(return_value=[])
    wx.is_authed = MagicMock(return_value=True)
    app = create_app(cfg=cfg, store=MagicMock(), wx=wx)
    with TestClient(app):
        sm = app.state.ctx.sm
        assert sm.state == SessionState.CONNECTED
        assert sm.display is True


def test_lifespan_shutdown_cleans_service_state(tmp_path):
    """I7：服务态优雅停机——shutdown 执行 finish_service 同语义清理（logout 必被调用）。"""
    from fastapi.testclient import TestClient
    from printshop.main import create_app

    cfg = Config()
    cfg.inbox_dir = tmp_path / "inbox"; cfg.work_dir = tmp_path / "work"
    cfg.db_path = tmp_path / "t.db"
    wx = AsyncMock()
    wx.restore_session = AsyncMock(return_value=True)
    wx.fetch_messages = AsyncMock(return_value=[])
    wx.is_authed = MagicMock(return_value=True)
    app = create_app(cfg=cfg, store=MagicMock(), wx=wx)
    with TestClient(app):
        assert app.state.ctx.sm.state == SessionState.CONNECTED
    wx.logout.assert_awaited_once()


def test_lifespan_shutdown_cleans_state_when_idle(tmp_path):
    """I7：IDLE 优雅停机——至少调用 wx.logout() 清 state.json 凭证与消息缓存。"""
    from fastapi.testclient import TestClient
    from printshop.main import create_app

    cfg = Config()
    cfg.inbox_dir = tmp_path / "inbox"; cfg.work_dir = tmp_path / "work"
    cfg.db_path = tmp_path / "t.db"
    wx = AsyncMock()
    wx.restore_session = AsyncMock(return_value=False)
    app = create_app(cfg=cfg, store=MagicMock(), wx=wx)
    with TestClient(app):
        assert app.state.ctx.sm.state == SessionState.IDLE
    wx.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_loopback_guard_blocks_non_loopback_admin(tmp_path):
    """I8：非回环访问管理端点 403；/screen、/api/screen/* 开放 LAN。"""
    import httpx
    from printshop.main import create_app

    cfg = Config()
    cfg.inbox_dir = tmp_path / "inbox"; cfg.work_dir = tmp_path / "work"
    cfg.db_path = tmp_path / "t.db"
    app = create_app(cfg=cfg, store=MagicMock(), wx=AsyncMock())
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/admin")).status_code == 403
        assert (await c.get("/api/printers")).status_code == 403
        assert (await c.get("/api/jobs")).status_code == 403
        assert (await c.get("/api/screen/state")).status_code == 200
        assert (await c.get("/screen")).status_code == 200


def test_loopback_guard_helpers():
    from printshop.main import _is_admin_path, _is_loopback_host

    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("testclient") is True
    assert _is_loopback_host(None) is True
    assert _is_loopback_host("192.168.1.50") is False
    assert _is_admin_path("/admin") is True
    assert _is_admin_path("/api/jobs") is True
    assert _is_admin_path("/api/service/finish") is True
    assert _is_admin_path("/api/screen/state") is False
    assert _is_admin_path("/api/screen/wake") is False
    assert _is_admin_path("/screen") is False
    assert _is_admin_path("/static/admin.html") is False


@pytest.mark.asyncio
async def test_ws_loopback_guard_rejects_non_loopback():
    """阻塞项：WS 握手非回环 host → close(1008) 拒绝，不进入广播集。"""
    from printshop.routes import _ws_loopback_guard

    ws = MagicMock()
    ws.client.host = "192.168.1.50"
    ws.close = AsyncMock()
    assert await _ws_loopback_guard(ws) is False
    ws.close.assert_awaited_once_with(code=1008)


@pytest.mark.asyncio
async def test_ws_loopback_guard_allows_loopback():
    from printshop.routes import _ws_loopback_guard

    for host in ("127.0.0.1", "::1", "localhost", "testclient", None):
        ws = MagicMock()
        ws.client.host = host
        ws.close = AsyncMock()
        assert await _ws_loopback_guard(ws) is True
        ws.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_admin_rejects_non_loopback_handshake(tmp_path):
    """阻塞项集成：非回环 client 连 /ws/admin，ASGI 层仅收到 close(1008)、无 accept。"""
    from printshop.main import create_app

    cfg = Config()
    cfg.inbox_dir = tmp_path / "inbox"; cfg.work_dir = tmp_path / "work"
    cfg.db_path = tmp_path / "t.db"
    app = create_app(cfg=cfg, store=MagicMock(), wx=AsyncMock())
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("192.168.1.50", 50000),
        "root_path": "",
        "path": "/ws/admin",
        "raw_path": b"/ws/admin",
        "query_string": b"",
        "headers": [],
        "subprotocols": [],
        "state": {},
    }
    await app(scope, lambda: _noop_receive(), send)
    close_msgs = [m for m in sent if m["type"] == "websocket.close"]
    assert close_msgs and close_msgs[0].get("code") == 1008
    assert not any(m["type"] == "websocket.accept" for m in sent)
