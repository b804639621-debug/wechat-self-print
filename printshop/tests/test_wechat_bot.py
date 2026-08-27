import json
import time
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from printshop.wechat_bot import QR_CONTENT_TEMPLATE, QR_REUSE_SECONDS, WeChatClient


def _client(entry_host="szfilehelper.weixin.qq.com") -> WeChatClient:
    c = WeChatClient(entry_host=entry_host)
    c._bot = MagicMock()
    return c


@pytest.mark.asyncio
async def test_get_qr_returns_image_bytes_and_content():
    c = _client()
    c._bot._jslogin_get_uuid = AsyncMock()
    c._bot.uuid = "abc123=="
    c._bot.uuid_ts = 0.0  # 老码：越过复用窗口，必须重新 jslogin
    with patch("printshop.wechat_bot.httpx.AsyncClient") as mc:
        resp = MagicMock()
        resp.content = b"\xff\xd8\xff\xe0jpeg-bytes"
        resp.raise_for_status = MagicMock()
        mc.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
        qr_bytes, content = await c.get_qr()
    assert qr_bytes == b"\xff\xd8\xff\xe0jpeg-bytes"
    assert content == "https://login.weixin.qq.com/l/abc123=="
    c._bot._jslogin_get_uuid.assert_awaited_once()
    assert c._bot.uuid == "abc123=="


@pytest.mark.asyncio
async def test_get_qr_reuses_fresh_uuid_without_jslogin():
    """wake/重复唤醒复用未过期码：不发 jslogin，返回同一内容、空字节。

    回归钉死 2026-08-25 实机缺陷：重复 wake 换码导致显示码≠轮询 uuid，
    顾客确认登录后主控永远检测不到。
    """
    c = _client()
    c._bot._jslogin_get_uuid = AsyncMock()
    c._bot.uuid = "fresh-uuid=="
    c._bot.uuid_ts = time.time()
    qr_bytes, content = await c.get_qr()
    assert qr_bytes == b""
    assert content == "https://login.weixin.qq.com/l/fresh-uuid=="
    c._bot._jslogin_get_uuid.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_qr_force_bypasses_reuse():
    """force=True（扫码超时作废等）即使有新鲜码也必须重新 jslogin。"""
    c = _client()
    c._bot.uuid = "old-uuid=="
    c._bot.uuid_ts = time.time()

    async def fake_jslogin():
        c._bot.uuid = "new-uuid=="
        c._bot.uuid_ts = time.time()

    c._bot._jslogin_get_uuid = AsyncMock(side_effect=fake_jslogin)
    with patch("printshop.wechat_bot.httpx.AsyncClient") as mc:
        resp = MagicMock()
        resp.content = b"jpeg"
        resp.raise_for_status = MagicMock()
        mc.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
        qr_bytes, content = await c.get_qr(force=True)
    assert qr_bytes == b"jpeg"
    assert content == "https://login.weixin.qq.com/l/new-uuid=="
    c._bot._jslogin_get_uuid.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_qr_concurrent_calls_single_jslogin():
    """并发 wake（托盘+小屏触摸同刻触发）：锁内双重检查，只 jslogin 一次。"""
    import asyncio

    c = _client()
    c._bot.uuid = ""
    c._bot.uuid_ts = 0.0

    async def fake_jslogin():
        c._bot.uuid = "shared-uuid=="
        c._bot.uuid_ts = time.time()

    c._bot._jslogin_get_uuid = AsyncMock(side_effect=fake_jslogin)
    with patch("printshop.wechat_bot.httpx.AsyncClient") as mc:
        resp = MagicMock()
        resp.content = b"jpeg"
        resp.raise_for_status = MagicMock()
        mc.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
        r1, r2 = await asyncio.gather(c.get_qr(), c.get_qr())
    assert r1[1] == r2[1] == "https://login.weixin.qq.com/l/shared-uuid=="
    c._bot._jslogin_get_uuid.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_qr_raises_when_uuid_missing():
    c = _client()
    c._bot._jslogin_get_uuid = AsyncMock()
    c._bot.uuid = ""
    with pytest.raises(RuntimeError):
        await c.get_qr()


@pytest.mark.asyncio
async def test_poll_login_maps_vendor_codes():
    c = _client()
    for code, expect in [
        (408, "wait_scan"),
        (201, "scanned"),
        (200, "confirmed"),
        (400, "expired"),
        (500, "expired"),
        # 0 = uuid 空或网络异常（direct_bot.py:521/537），非过期：
        # 静默等下轮，不再触发换码（finish/启动后自动出码缺陷根因）
        (0, "wait_scan"),
        (999, "wait_scan"),
    ]:
        c._bot._poll_login_once = AsyncMock(return_value=code)
        assert await c.poll_login() == expect


@pytest.mark.asyncio
async def test_fetch_messages_passthrough_vendor_normalized_list():
    c = _client()
    msgs = [
        {"id": "1", "type": "text", "text": "hello", "is_mine": False},
        {"id": "2", "type": "file", "text": "[File: a.pdf]",
         "file_name": "a.pdf", "is_mine": False},
    ]
    c._bot.get_latest_messages = AsyncMock(return_value=msgs)
    assert await c.fetch_messages() == msgs
    c._bot.get_latest_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_text_ensures_started_and_forwards_to_vendor():
    c = _client()
    c._bot.client = None
    c._bot.start = AsyncMock()
    c._bot.send_text = AsyncMock(return_value=True)
    assert await c.send_text("😊") is True
    c._bot.start.assert_awaited_once()
    c._bot.send_text.assert_awaited_once_with("😊")


@pytest.mark.asyncio
async def test_send_text_returns_vendor_false():
    c = _client()
    c._bot.client = AsyncMock()
    c._bot.send_text = AsyncMock(return_value=False)
    assert await c.send_text("x") is False
    c._bot.send_text.assert_awaited_once_with("x")


@pytest.mark.asyncio
async def test_download_returns_bytes_via_temp_file():
    c = _client()

    async def fake_download(msg_id, save_path):
        assert msg_id == "msg_42"
        Path(save_path).write_bytes(b"PDF_BYTES")
        return True

    c._bot.download_message_content = AsyncMock(side_effect=fake_download)
    data = await c.download("msg_42")
    assert data == b"PDF_BYTES"
    c._bot.download_message_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_raises_when_vendor_fails():
    c = _client()
    c._bot.download_message_content = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError):
        await c.download("nope")


@pytest.mark.asyncio
async def test_logout_posts_webwxlogout_and_clears_credentials(tmp_path):
    c = _client()
    c._bot.client = AsyncMock()
    c._bot.client.post = AsyncMock(return_value=MagicMock())
    c._bot.entry_host = "szfilehelper.weixin.qq.com"
    c._bot.state_path = tmp_path / "state.json"
    c._bot.skey = "SK"; c._bot.sid = "SI"; c._bot.uin = "U"
    c._bot.pass_ticket = "PT"; c._bot.user_name = "u"; c._bot.uuid = "u1"
    c._bot.is_logged_in = True

    await c.logout()

    url = c._bot.client.post.await_args.args[0]
    assert "/cgi-bin/mmwebwx-bin/webwxlogout" in url
    params = c._bot.client.post.await_args.kwargs["params"]
    assert params["skey"] == "SK" and params["sid"] == "SI"
    assert c._bot.skey == "" and c._bot.sid == "" and c._bot.pass_ticket == ""
    assert c._bot.uuid == "" and c._bot.is_logged_in is False


@pytest.mark.asyncio
async def test_logout_deletes_persisted_state_file(tmp_path):
    c = _client()
    state_path = tmp_path / "state.json"
    state_path.write_text('{"skey":"SK","sid":"SI"}', encoding="utf-8")
    c._bot.client = AsyncMock()
    c._bot.client.post = AsyncMock(return_value=MagicMock())
    c._bot.entry_host = "szfilehelper.weixin.qq.com"
    c._bot.state_path = state_path
    c._bot.skey = "SK"; c._bot.sid = "SI"; c._bot.uin = "U"; c._bot.pass_ticket = "PT"
    c._bot.is_logged_in = True

    await c.logout()

    assert not state_path.exists()


@pytest.mark.asyncio
async def test_logout_tolerates_missing_state_file(tmp_path):
    c = _client()
    state_path = tmp_path / "state.json"
    c._bot.client = AsyncMock()
    c._bot.client.post = AsyncMock(return_value=MagicMock())
    c._bot.entry_host = "szfilehelper.weixin.qq.com"
    c._bot.state_path = state_path
    c._bot.skey = "SK"; c._bot.sid = "SI"; c._bot.uin = "U"; c._bot.pass_ticket = "PT"

    await c.logout()  # 必须不抛异常

    assert not state_path.exists()


@pytest.mark.asyncio
async def test_restore_session_false_after_logout(tmp_path):
    from printshop.vendor.direct_bot import WeChatHelperBot

    bot = WeChatHelperBot(entry_host="szfilehelper.weixin.qq.com")
    bot.client = AsyncMock()
    bot.state_path = tmp_path / "state.json"
    bot.state_path.write_text(
        json.dumps({"skey": "SK", "sid": "SI", "uin": "U",
                    "pass_ticket": "PT", "cookies": []}),
        encoding="utf-8",
    )
    c = WeChatClient(entry_host="szfilehelper.weixin.qq.com")
    c._bot = bot
    assert await c.restore_session() is True

    bot.client.post = AsyncMock(return_value=MagicMock())
    await c.logout()

    assert not bot.state_path.exists()
    assert await c.restore_session() is False


def test_is_authed_delegates_to_vendor():
    c = _client()
    c._bot.is_logged_in = True
    assert c.is_authed() is True
    c._bot.is_logged_in = False
    assert c.is_authed() is False


@pytest.mark.asyncio
async def test_restore_session_returns_auth_state():
    c = _client()
    c._bot._load_session = AsyncMock()
    c._bot._has_auth = MagicMock(return_value=True)
    assert await c.restore_session() is True
    c._bot._load_session.assert_awaited_once()
    c._bot._has_auth = MagicMock(return_value=False)
    assert await c.restore_session() is False


def test_qr_content_template_matches_m1_verified_format():
    assert QR_CONTENT_TEMPLATE == "https://login.weixin.qq.com/l/{uuid}"
    assert QR_CONTENT_TEMPLATE.format(uuid="obyDMELwoQ==") == \
        "https://login.weixin.qq.com/l/obyDMELwoQ=="


def test_wechat_client_trace_disabled_by_default():
    c = WeChatClient(entry_host="szfilehelper.weixin.qq.com")
    assert c._bot.trace_enabled is False


@pytest.mark.asyncio
async def test_logout_cleans_trace_dir(tmp_path):
    c = _client()
    trace_dir = tmp_path / "trace_logs"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "wechat_http_trace.jsonl").write_text(
        '{"url":"https://x/","body":"nickname"}', encoding="utf-8")
    c._bot.trace_dir = trace_dir
    c._bot.client = AsyncMock()
    c._bot.client.post = AsyncMock(return_value=MagicMock())
    c._bot.entry_host = "szfilehelper.weixin.qq.com"
    c._bot.state_path = tmp_path / "state.json"
    c._bot.skey = "SK"; c._bot.sid = "SI"; c._bot.uin = "U"; c._bot.pass_ticket = "PT"
    c._bot.is_logged_in = True

    await c.logout()

    assert list(trace_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_logout_clears_vendor_message_cache(tmp_path):
    """C3 跨顾客会话隔离：A 会话消息缓存必须随登出清空，B 会话 fetch 不得再见 A 的 msg_id。"""
    from printshop.vendor.direct_bot import WeChatHelperBot

    bot = WeChatHelperBot(entry_host="szfilehelper.weixin.qq.com")
    bot.client = AsyncMock()
    bot.client.post = AsyncMock(return_value=MagicMock())
    bot.state_path = tmp_path / "state.json"
    bot.trace_dir = tmp_path / "trace_logs"
    bot.skey = "SK"; bot.sid = "SI"; bot.uin = "U"; bot.pass_ticket = "PT"
    bot.is_logged_in = True
    bot._seen_msg_ids.add("A1")
    bot._msg_cache.append({"id": "A1", "type": "file", "file_name": "a.pdf", "is_mine": False})
    bot._raw_by_id["A1"] = {"MsgId": "A1"}
    bot._send_msg_ids.add("A2")
    c = WeChatClient(entry_host="szfilehelper.weixin.qq.com")
    c._bot = bot

    await c.logout()

    assert not bot._seen_msg_ids
    assert not bot._msg_cache
    assert not bot._raw_by_id
    assert not bot._send_msg_ids
    assert await bot.get_latest_messages() == []


def test_vendor_normalize_filehelper_self_chat_marks_is_mine():
    """文件传输助手"自聊"：from=登录账号自身、to=filehelper 的消息 is_mine=True，
    且必须保留在归一化结果中（下游不得再按 is_mine 过滤）。
    回归钉死：2026-08-25 Task 9 实机缺陷根因（is_mine 误比较发送目标恒为 filehelper）。"""
    from printshop.vendor.direct_bot import WeChatHelperBot
    bot = WeChatHelperBot(entry_host="szfilehelper.weixin.qq.com")
    bot.user_name = "@abc123"   # webwxinit 写入的登录账号 wxid
    items = [
        {"MsgId": "1", "FromUserName": "@abc123", "ToUserName": "filehelper",
         "MsgType": 1, "Content": "测试"},
        {"MsgId": "2", "FromUserName": "@abc123", "ToUserName": "filehelper",
         "MsgType": 3, "FileName": "photo.jpg"},
        {"MsgId": "3", "FromUserName": "@abc123", "ToUserName": "filehelper",
         "MsgType": 49, "AppMsgType": 6, "FileName": "a.pdf"},
        # 与 filehelper 无关的会话：被现有会话过滤器丢弃
        {"MsgId": "4", "FromUserName": "@other", "ToUserName": "@abc123",
         "MsgType": 1, "Content": "非filehelper会话"},
    ]
    out = bot._normalize_messages(items)
    assert [m["id"] for m in out] == ["1", "2", "3"]
    assert all(m["is_mine"] is True for m in out)
    assert out[0]["type"] == "text" and out[0]["text"] == "测试"
    assert out[1]["type"] == "image" and out[1]["file_name"] == "photo.jpg"
    assert out[2]["type"] == "file" and out[2]["file_name"] == "a.pdf"
