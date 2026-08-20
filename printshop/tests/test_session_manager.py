import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from printshop.session_manager import SessionManager, SessionState
from printshop.config import Config


def _sm(cfg=None, store=None):
    sm = SessionManager(cfg or Config(), MagicMock(), store or MagicMock())
    return sm


def _idle(cfg=None, store=None):
    sm = _sm(cfg, store)
    sm.display = True
    return sm


@pytest.mark.asyncio
async def test_wake_refreshes_qr():
    sm = SessionManager(Config(), MagicMock(), MagicMock())
    sm._wx.get_qr = AsyncMock(return_value=(b"PNG", "https://login.weixin.qq.com/l/x"))
    await sm.wake()
    assert sm.display is True and sm.qr_content.endswith("/l/x") and sm.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_idle_sleep_after_timeout():
    cfg = Config(); cfg.idle_sleep_seconds = 180
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.display = True
    sm._last_event_ts -= 200          # 模拟 200 秒无事件
    sm._check_idle_sleep()
    assert sm.display is False


@pytest.mark.asyncio
async def test_finish_service_cleans_up(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    f = tmp_path / "a.pdf"; f.write_bytes(b"x")
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm._wx.logout = AsyncMock()
    sm.state = SessionState.CONNECTED; sm.file_count = 1
    await sm.finish_service()
    sm._wx.logout.assert_awaited_once()
    assert not f.exists() and sm.state == SessionState.IDLE and sm.display is False and sm.file_count == 0


@pytest.mark.asyncio
async def test_wake_when_screen_already_on_refreshes_qr():
    sm = _idle()
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "https://login.weixin.qq.com/l/w2"))
    await sm.wake()
    sm._wx.get_qr.assert_awaited_once()
    assert sm.display is True and sm.qr_content.endswith("/l/w2")


@pytest.mark.asyncio
async def test_wake_ignored_when_not_idle():
    sm = _sm()
    sm.state = SessionState.CONNECTED
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "u"))
    await sm.wake()
    sm._wx.get_qr.assert_not_awaited()
    assert sm.display is False


def test_no_idle_sleep_before_timeout():
    cfg = Config(); cfg.idle_sleep_seconds = 180
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.display = True
    sm._check_idle_sleep()
    assert sm.display is True


def test_no_idle_sleep_during_service():
    cfg = Config(); cfg.idle_sleep_seconds = 180
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm.display = True
    sm._last_event_ts -= 200
    sm._check_idle_sleep()
    assert sm.display is True


@pytest.mark.asyncio
async def test_poll_scanned_transitions_to_scanned():
    sm = _idle()
    await sm._handle_poll("scanned")
    assert sm.state == SessionState.SCANNED
    assert sm._scanned_ts is not None


@pytest.mark.asyncio
async def test_poll_confirmed_transitions_to_connected_without_complete_login():
    sm = _idle()
    sm._wx.complete_login = AsyncMock()
    await sm._handle_poll("confirmed")
    assert sm.state == SessionState.CONNECTED
    sm._wx.complete_login.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_expired_refreshes_qr():
    sm = _idle()
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "https://login.weixin.qq.com/l/new"))
    await sm._handle_poll("expired")
    sm._wx.get_qr.assert_awaited_once()
    assert sm.qr_content.endswith("/l/new")
    assert sm.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_poll_wait_scan_keeps_idle():
    sm = _idle()
    await sm._handle_poll("wait_scan")
    assert sm.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_scanned_timeout_returns_idle_with_fresh_qr():
    sm = _idle()
    sm._scanned_ts = time.monotonic() - 130
    sm._transition(SessionState.SCANNED)
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "https://login.weixin.qq.com/l/fresh"))
    await sm._check_scanned_timeout()
    assert sm.state == SessionState.IDLE
    assert sm.qr_content.endswith("/l/fresh")
    sm._wx.get_qr.assert_awaited_once()


@pytest.mark.asyncio
async def test_scanned_before_timeout_keeps_scanned():
    sm = _idle()
    sm._scanned_ts = time.monotonic() - 10
    sm._transition(SessionState.SCANNED)
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "u"))
    await sm._check_scanned_timeout()
    assert sm.state == SessionState.SCANNED
    sm._wx.get_qr.assert_not_awaited()


@pytest.mark.asyncio
async def test_receive_file_message_creates_job(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    store = MagicMock()
    sm = SessionManager(cfg, MagicMock(), store)
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "m1", "type": "file", "file_name": "a.pdf",
         "text": "[File: a.pdf]", "is_mine": False}])
    sm._wx.download = AsyncMock(return_value=b"%PDF-bytes")
    await sm._process_messages()
    assert (tmp_path / "a.pdf").read_bytes() == b"%PDF-bytes"
    store.add.assert_called_once()
    kwargs = store.add.call_args.kwargs
    assert kwargs["msg_id"] == "m1"
    assert kwargs["file_name"] == "a.pdf"
    assert kwargs["file_path"] == str(tmp_path / "a.pdf")
    assert kwargs["category"] == "pdf"
    assert kwargs["size_bytes"] == len(b"%PDF-bytes")
    assert sm.file_count == 1
    assert sm.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_receive_image_message_creates_job(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    store = MagicMock()
    sm = SessionManager(cfg, MagicMock(), store)
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "m2", "type": "image", "file_name": "photo.png", "is_mine": False}])
    sm._wx.download = AsyncMock(return_value=b"IMG")
    await sm._process_messages()
    assert (tmp_path / "photo.png").exists()
    kwargs = store.add.call_args.kwargs
    assert kwargs["category"] == "image"
    assert sm.file_count == 1


@pytest.mark.asyncio
async def test_receiving_state_briefly_observed(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    seen = []
    sm.on_change = lambda: seen.append(sm.state)
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "m1", "type": "file", "file_name": "a.pdf", "is_mine": False}])
    sm._wx.download = AsyncMock(return_value=b"PDF")
    await sm._process_messages()
    assert SessionState.RECEIVING in seen
    assert seen[-1] == SessionState.CONNECTED
    assert sm.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_receive_text_message_sets_note():
    sm = SessionManager(Config(), MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text", "text": "打印3份", "is_mine": False},
        {"id": "t2", "type": "text", "text": "彩色", "is_mine": False}])
    await sm._process_messages()
    assert await sm.note_text() == "彩色"
    assert sm.file_count == 0
    assert sm.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_ignore_own_messages():
    sm = SessionManager(Config(), MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "m1", "type": "file", "file_name": "a.pdf", "is_mine": True},
        {"id": "t1", "type": "text", "text": "hi", "is_mine": True}])
    sm._wx.download = AsyncMock()
    await sm._process_messages()
    sm._wx.download.assert_not_awaited()
    assert sm.file_count == 0
    assert await sm.note_text() == ""


@pytest.mark.asyncio
async def test_duplicate_message_ignored(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    msg = {"id": "m1", "type": "file", "file_name": "a.pdf", "is_mine": False}
    sm._wx.fetch_messages = AsyncMock(return_value=[msg, msg])
    sm._wx.download = AsyncMock(return_value=b"PDF")
    await sm._process_messages()
    sm._wx.download.assert_awaited_once()
    sm._store.add.assert_called_once()
    assert sm.file_count == 1


@pytest.mark.asyncio
async def test_failed_download_retried_next_fetch(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    store = MagicMock()
    sm = SessionManager(cfg, MagicMock(), store)
    sm.state = SessionState.CONNECTED
    msg = {"id": "m1", "type": "file", "file_name": "a.pdf", "is_mine": False}
    sm._wx.fetch_messages = AsyncMock(return_value=[msg])
    sm._wx.download = AsyncMock(side_effect=[RuntimeError("network"), b"PDF"])
    await sm._process_messages()
    store.add.assert_not_called()
    assert sm.file_count == 0
    assert not (tmp_path / "a.pdf").exists()
    await sm._process_messages()
    store.add.assert_called_once()
    assert (tmp_path / "a.pdf").read_bytes() == b"PDF"
    assert sm.file_count == 1


@pytest.mark.asyncio
async def test_filename_conflict_adds_suffix(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    (tmp_path / "a.pdf").write_bytes(b"existing")
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "m1", "type": "file", "file_name": "a.pdf", "is_mine": False}])
    sm._wx.download = AsyncMock(return_value=b"PDF")
    await sm._process_messages()
    assert (tmp_path / "a_1.pdf").read_bytes() == b"PDF"
    assert (tmp_path / "a.pdf").read_bytes() == b"existing"
    assert sm._store.add.call_args.kwargs["file_name"] == "a_1.pdf"


def test_no_file_timeout_sets_reminder_without_logout():
    cfg = Config(); cfg.no_file_timeout = 300
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm._last_file_ts = time.monotonic() - 400
    sm._check_no_file_timeout()
    assert sm._no_file_reminder is True
    assert sm.state == SessionState.CONNECTED


def test_no_file_timeout_not_triggered_recently():
    cfg = Config(); cfg.no_file_timeout = 300
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm._last_file_ts = time.monotonic() - 10
    sm._check_no_file_timeout()
    assert sm._no_file_reminder is False


@pytest.mark.asyncio
async def test_receiving_file_clears_no_file_reminder(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm._no_file_reminder = True
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "m1", "type": "file", "file_name": "a.pdf", "is_mine": False}])
    sm._wx.download = AsyncMock(return_value=b"PDF")
    await sm._process_messages()
    assert sm._no_file_reminder is False


def test_screen_state_contract():
    sm = _sm()
    sm.display = True
    sm.file_count = 3
    sm.qr_content = "https://login.weixin.qq.com/l/x"
    s = sm.screen_state()
    assert set(s.keys()) == {"state", "display", "qr_content", "file_count", "message"}
    assert s["display"] == "on"
    assert s["state"] == SessionState.IDLE
    assert s["qr_content"] == "https://login.weixin.qq.com/l/x"
    assert s["file_count"] == 3
    sm.display = False
    assert sm.screen_state()["display"] == "off"


def test_screen_state_message_reflects_note():
    sm = _sm()
    sm._note = "彩色"
    assert sm.screen_state()["message"] == "彩色"


@pytest.mark.asyncio
async def test_finish_service_archives_jobs(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    store = MagicMock()
    sm = SessionManager(cfg, MagicMock(), store)
    sm._wx.logout = AsyncMock()
    sm.state = SessionState.CONNECTED
    await sm.finish_service()
    store.archive_all.assert_called_once()


@pytest.mark.asyncio
async def test_finish_service_order_logout_then_delete_then_archive(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    f = tmp_path / "a.pdf"; f.write_bytes(b"x")
    events = []
    store = MagicMock()

    def archive_all():
        assert not f.exists(), "归档前必须已删除顾客文件"
        events.append("archive")
        return 0

    store.archive_all = archive_all
    sm = SessionManager(cfg, MagicMock(), store)
    sm.state = SessionState.CONNECTED

    async def logout():
        assert f.exists(), "登出时顾客文件应尚未删除"
        events.append("logout")

    sm._wx.logout = logout
    await sm.finish_service()
    assert events == ["logout", "archive"]


@pytest.mark.asyncio
async def test_finish_service_clears_note(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm._wx.logout = AsyncMock()
    sm.state = SessionState.CONNECTED
    sm._note = "彩色打印"
    await sm.finish_service()
    assert await sm.note_text() == ""


@pytest.mark.asyncio
async def test_run_loop_polls_login_in_idle():
    cfg = Config(); cfg.poll_interval = 0.001
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.display = True
    sm._wx.poll_login = AsyncMock(return_value="wait_scan")
    task = asyncio.create_task(sm.run_loop())
    for _ in range(5):
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert sm._wx.poll_login.await_count >= 1
