import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from printshop.jobs import JobStatus
from printshop.session_manager import SessionManager, SessionState, extract_public_urls
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
    cfg = Config(); cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
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
async def test_poll_confirmed_sends_login_greeting():
    cfg = Config()
    sm = _idle(cfg)
    sm._wx.save_session = AsyncMock(return_value=True)
    sm._wx.send_text = AsyncMock(return_value=True)
    await sm._handle_poll("confirmed")
    assert sm.state == SessionState.CONNECTED
    sm._wx.send_text.assert_awaited_once_with(cfg.login_greeting_text)
    assert sm._greeting_sent is True


@pytest.mark.asyncio
async def test_poll_confirmed_greeting_exception_keeps_connected():
    sm = _idle()
    sm._wx.save_session = AsyncMock(return_value=True)

    async def boom(message):
        raise RuntimeError("send failed")

    sm._wx.send_text = boom
    await sm._handle_poll("confirmed")
    assert sm.state == SessionState.CONNECTED
    assert sm._greeting_sent is True


@pytest.mark.asyncio
async def test_send_login_greeting_only_once_per_login_session():
    sm = _idle()
    sm._wx.send_text = AsyncMock(return_value=True)
    await sm._send_login_greeting()
    await sm._send_login_greeting()
    assert sm._wx.send_text.await_count == 1


@pytest.mark.asyncio
async def test_finish_service_resets_greeting_sent(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm._wx.logout = AsyncMock()
    sm._wx.send_text = AsyncMock(return_value=True)
    await sm._send_login_greeting()
    assert sm._greeting_sent is True
    await sm.finish_service()
    assert sm._greeting_sent is False


@pytest.mark.asyncio
async def test_send_login_greeting_disabled_by_config():
    cfg = Config(); cfg.send_login_greeting = False
    sm = _idle(cfg)
    sm._wx.save_session = AsyncMock(return_value=True)
    sm._wx.send_text = AsyncMock(return_value=True)
    await sm._handle_poll("confirmed")
    assert sm.state == SessionState.CONNECTED
    sm._wx.send_text.assert_not_awaited()
    assert sm._greeting_sent is False


@pytest.mark.asyncio
async def test_send_login_greeting_skips_when_wx_has_no_send_text():
    class _FakeWx:
        async def save_session(self):
            return True

        async def logout(self):
            pass

    sm = SessionManager(Config(), _FakeWx(), MagicMock())
    sm.display = True
    await sm._handle_poll("confirmed")
    assert sm.state == SessionState.CONNECTED
    assert sm._greeting_sent is True


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
async def test_text_note_fires_on_note_callback():
    sm = SessionManager(Config(), MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    seen = []
    sm.on_note = lambda: seen.append(sm._note)
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text", "text": "彩色", "is_mine": False}])
    await sm._process_messages()
    assert seen == ["彩色"]


@pytest.mark.asyncio
async def test_own_messages_accepted_filehelper_semantics(tmp_path):
    """文件传输助手是"自聊"：顾客消息 is_mine 恒为 True，必须照常接收。
    回归：2026-08-25 Task 9 实机缺陷——按 is_mine 过滤导致顾客消息全灭。"""
    cfg = Config(); cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "m1", "type": "file", "file_name": "a.pdf", "is_mine": True},
        {"id": "t1", "type": "text", "text": "hi", "is_mine": True}])
    sm._wx.download = AsyncMock(return_value=b"PDF")
    await sm._process_messages()
    sm._wx.download.assert_awaited_once()
    assert (tmp_path / "a.pdf").read_bytes() == b"PDF"
    assert sm.file_count == 1
    assert await sm.note_text() == "hi"


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
    assert set(s.keys()) == {"state", "display", "qr_content", "file_count", "message",
                             "no_file_reminder"}
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
async def test_wake_failure_clears_stale_qr_and_reports_message():
    """防回归（2026 废码残留实机缺陷）：取码失败时不得把过期旧二维码留在屏上。"""
    import httpx

    sm = _idle()
    sm.qr_content = "https://login.weixin.qq.com/l/expired"
    sm._wx.get_qr = AsyncMock(side_effect=httpx.ConnectError("All connection attempts failed"))
    ok = await sm.wake()
    assert ok is False
    assert sm.qr_content is None
    assert sm.screen_state()["message"] == "网络异常，暂无二维码，请稍后再试"
    # 屏已亮则保持亮：屏显提示文案而非黑屏


@pytest.mark.asyncio
async def test_wake_failure_keeps_display_off_and_retry_wakes_with_qr():
    import httpx

    sm = _sm()
    sm.qr_content = "https://login.weixin.qq.com/l/expired"
    sm._wx.get_qr = AsyncMock(side_effect=httpx.ConnectError("down"))
    assert await sm.wake() is False
    assert sm.display is False and sm.qr_content is None
    # 恢复后再次唤醒：出码成功，提示清掉
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "https://login.weixin.qq.com/l/new"))
    assert await sm.wake() is True
    assert sm.qr_content.endswith("/l/new") and sm.screen_state()["message"] == ""


@pytest.mark.asyncio
async def test_finish_service_archives_jobs(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
    store = MagicMock()
    sm = SessionManager(cfg, MagicMock(), store)
    sm._wx.logout = AsyncMock()
    sm.state = SessionState.CONNECTED
    await sm.finish_service()
    store.archive_all.assert_called_once()


@pytest.mark.asyncio
async def test_finish_service_order_logout_then_delete_then_archive(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
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
    cfg = Config(); cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
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


@pytest.mark.asyncio
async def test_run_loop_scanned_then_confirmed_reaches_connected(tmp_path):
    """C1 主流程：SCANNED 分支必须继续轮询登录，confirmed 才能被检测到。"""
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=2)
    sm.display = True
    sm._wx.poll_login = AsyncMock(side_effect=["scanned", "confirmed"])
    sm._wx.save_session = AsyncMock(return_value=True)
    await sm.run_loop()
    assert sm.state == SessionState.CONNECTED
    assert sm._wx.poll_login.await_count == 2


@pytest.mark.asyncio
async def test_run_loop_scanned_expired_returns_idle_fresh_qr(tmp_path):
    """C1：SCANNED 下轮询到 expired → 回 IDLE 出新码（保留 _check_scanned_timeout 兜底）。"""
    cfg = Config(); cfg.poll_interval = 0.001; cfg.inbox_dir = tmp_path
    sm = SessionManager(cfg, MagicMock(), MagicMock(), max_iterations=2)
    sm.display = True
    sm._wx.poll_login = AsyncMock(side_effect=["scanned", "expired"])
    sm._wx.get_qr = AsyncMock(return_value=(b"P", "https://login.weixin.qq.com/l/new"))
    await sm.run_loop()
    assert sm.state == SessionState.IDLE
    assert sm.qr_content.endswith("/l/new")
    sm._wx.get_qr.assert_awaited_once()


@pytest.mark.asyncio
async def test_finish_service_cleans_work_dir(tmp_path):
    """C2：finish_service 必须清理 work_dir 中间产物（顾客文件全量副本残留）。"""
    cfg = Config()
    cfg.inbox_dir = tmp_path / "inbox"; cfg.work_dir = tmp_path / "work"
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    (cfg.inbox_dir / "a.pdf").write_bytes(b"x")
    (cfg.work_dir / "job_1.pdf").write_bytes(b"%PDF")
    (cfg.work_dir / "x1.xps").write_bytes(b"x")
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm._wx.logout = AsyncMock()
    sm.state = SessionState.CONNECTED
    await sm.finish_service()
    assert not (cfg.inbox_dir / "a.pdf").exists()
    assert list(cfg.work_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_finish_service_reports_cleanup_failure(tmp_path):
    """I3：清理失败不静默——收集失败列表、返回结果、广播告警。"""
    cfg = Config()
    cfg.inbox_dir = tmp_path / "inbox"; cfg.work_dir = tmp_path / "work"
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    (cfg.inbox_dir / "a.pdf").write_bytes(b"x")
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm._wx.logout = AsyncMock()
    notices = []
    sm.on_admin_notice = lambda kind, text: notices.append((kind, text))
    with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
        result = await sm.finish_service()
    assert result["ok"] is False
    assert "a.pdf" in str(result["failures"])
    assert notices == [("cleanup_failed", "文件清理失败，请手动删除 inbox")]


def test_no_file_timeout_notifies_and_exposes_field():
    """I2：5 分钟无文件提醒闭环——置位时 notify 推 WS，screen_state 暴露字段。"""
    cfg = Config(); cfg.no_file_timeout = 300
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.state = SessionState.CONNECTED
    sm._last_file_ts = time.monotonic() - 400
    pushed = []
    sm.on_change = lambda: pushed.append(sm.screen_state()["no_file_reminder"])
    sm._check_no_file_timeout()
    assert sm._no_file_reminder is True
    assert pushed == [True]
    assert sm.screen_state()["no_file_reminder"] is True


@pytest.mark.asyncio
async def test_restore_session_restores_file_count(tmp_path):
    """M3：启动恢复时从 JobStore 数活跃任务置 file_count。"""
    cfg = Config(); cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
    store = MagicMock()
    store.list_active.return_value = [object(), object()]  # 2 个活跃任务
    sm = SessionManager(cfg, MagicMock(), store)
    sm._wx.restore_session = AsyncMock(return_value=False)
    await sm.restore_session()
    assert sm.file_count == 2


@pytest.mark.asyncio
async def test_receive_oversized_file_marked_failed_not_persisted(tmp_path):
    """I6：收件 >25MB 不入库内容、不落盘，标红任务注明原因。"""
    cfg = Config(); cfg.inbox_dir = tmp_path; cfg.work_dir = tmp_path
    store = MagicMock()
    store.add.return_value = 42
    sm = SessionManager(cfg, MagicMock(), store)
    sm._wx.download = AsyncMock(return_value=b"x" * (25 * 1024 * 1024 + 1))
    await sm._receive_file({"id": "big", "file_name": "big.docx"})
    assert list(tmp_path.iterdir()) == []  # 未落盘
    assert store.add.call_args.kwargs["category"] == "other"
    assert store.add.call_args.kwargs["file_path"] == ""
    store.set_status.assert_called_once_with(42, JobStatus.FAILED, error="文件超过 25MB，未接收")
    assert sm.file_count == 1


def test_extract_public_urls_strips_chinese_punctuation():
    assert extract_public_urls("请打开 https://example.com/a?x=1。") == ["https://example.com/a?x=1"]


def test_extract_public_urls_rejects_local_private_and_non_http():
    text = (
        "http://127.0.0.1:8000/api/... "
        "http://localhost/x "
        "http://foo.localhost/y "
        "http://192.168.1.5/x "
        "http://10.0.0.1/x "
        "http://[::1]/x "
        "http://224.0.0.1/x "
        "file:///etc/passwd "
        "weixin://abc"
    )
    assert extract_public_urls(text) == []


@pytest.mark.asyncio
async def test_text_message_opens_public_link_without_trailing_punctuation():
    opened = []
    sm = SessionManager(Config(), MagicMock(), MagicMock(), link_opener=lambda u: opened.append(u))
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text", "text": "请打开 https://example.com/a?x=1。", "is_mine": False}])
    await sm._process_messages()
    assert opened == ["https://example.com/a?x=1"]


@pytest.mark.asyncio
async def test_text_message_does_not_open_local_private_or_non_http():
    opened = []
    sm = SessionManager(Config(), MagicMock(), MagicMock(), link_opener=lambda u: opened.append(u))
    sm.state = SessionState.CONNECTED
    text = (
        "http://127.0.0.1:8000/api/... "
        "http://localhost/x "
        "http://foo.localhost/y "
        "http://192.168.1.5/x "
        "http://10.0.0.1/x "
        "http://[::1]/x "
        "http://224.0.0.1/x "
        "file:///etc/passwd "
        "weixin://abc"
    )
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text", "text": text, "is_mine": False}])
    await sm._process_messages()
    assert opened == []


@pytest.mark.asyncio
async def test_text_message_deduplicates_urls_in_same_message():
    opened = []
    sm = SessionManager(Config(), MagicMock(), MagicMock(), link_opener=lambda u: opened.append(u))
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text",
         "text": "https://example.com/a https://example.com/a https://example.com/b",
         "is_mine": False}])
    await sm._process_messages()
    assert opened == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_same_link_not_reopened_across_messages_in_session():
    opened = []
    sm = SessionManager(Config(), MagicMock(), MagicMock(), link_opener=lambda u: opened.append(u))
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text", "text": "https://example.com/a", "is_mine": False}])
    await sm._process_messages()
    assert opened == ["https://example.com/a"]

    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t2", "type": "text", "text": "https://example.com/a", "is_mine": False}])
    await sm._process_messages()
    assert opened == ["https://example.com/a"]


@pytest.mark.asyncio
async def test_auto_open_max_links_stops_after_limit():
    cfg = Config(); cfg.auto_open_max_links = 2
    opened = []
    sm = SessionManager(cfg, MagicMock(), MagicMock(), link_opener=lambda u: opened.append(u))
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text",
         "text": "https://a.com https://b.com https://c.com",
         "is_mine": False}])
    await sm._process_messages()
    assert opened == ["https://a.com", "https://b.com"]


@pytest.mark.asyncio
async def test_auto_open_links_false_disables_opening():
    cfg = Config(); cfg.auto_open_links = False
    opened = []
    sm = SessionManager(cfg, MagicMock(), MagicMock(), link_opener=lambda u: opened.append(u))
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text", "text": "https://example.com/a", "is_mine": False}])
    await sm._process_messages()
    assert opened == []


@pytest.mark.asyncio
async def test_opener_exception_does_not_break_file_receive(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    store = MagicMock()

    def bad_opener(url):
        raise RuntimeError("boom")

    sm = SessionManager(cfg, MagicMock(), store, link_opener=bad_opener)
    sm.state = SessionState.CONNECTED
    sm._wx.fetch_messages = AsyncMock(return_value=[
        {"id": "t1", "type": "text", "text": "https://example.com/a", "is_mine": False},
        {"id": "f1", "type": "file", "file_name": "a.pdf", "is_mine": False}])
    sm._wx.download = AsyncMock(return_value=b"PDF")
    await sm._process_messages()
    assert (tmp_path / "a.pdf").read_bytes() == b"PDF"
    assert sm.file_count == 1
    store.add.assert_called_once()
    assert sm._opened_links == set()


@pytest.mark.asyncio
async def test_poll_confirmed_resets_opened_links():
    sm = _idle()
    sm._opened_links.add("https://example.com/a")
    sm._wx.save_session = AsyncMock(return_value=True)
    await sm._handle_poll("confirmed")
    assert sm.state == SessionState.CONNECTED
    assert sm._opened_links == set()
