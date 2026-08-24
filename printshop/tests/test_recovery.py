"""Task 11 加固与恢复：断线重连（指数退避）、凭证恢复、看门狗、定时保存会话。"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from printshop.config import Config
from printshop.session_manager import SessionManager, SessionState, _is_network_error


def _recorder(store):
    async def record(seconds):
        store.append(seconds)
    return record


def _sm(cfg=None, store=None, **kw):
    cfg = cfg or Config()
    return SessionManager(cfg, MagicMock(), store or MagicMock(), **kw)


def test_config_recovery_defaults():
    c = Config()
    assert c.network_backoff == (5, 15, 60)
    assert c.network_max_failures == 3
    assert c.session_save_interval == 600


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
async def test_three_consecutive_failures_logout_fresh_qr(tmp_path):
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
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
    sm._wx.save_session = AsyncMock()
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


def test_lifespan_restore_session_enters_service_state():
    from fastapi.testclient import TestClient
    from printshop.main import create_app

    wx = AsyncMock()
    wx.restore_session = AsyncMock(return_value=True)
    wx.fetch_messages = AsyncMock(return_value=[])
    app = create_app(cfg=Config(), store=MagicMock(), wx=wx)
    with TestClient(app):
        sm = app.state.ctx.sm
        assert sm.state == SessionState.CONNECTED
        assert sm.display is True
