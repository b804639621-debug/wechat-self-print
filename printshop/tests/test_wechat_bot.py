import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from printshop.wechat_bot import QR_CONTENT_TEMPLATE, WeChatClient


def _client(entry_host="szfilehelper.weixin.qq.com") -> WeChatClient:
    c = WeChatClient(entry_host=entry_host)
    c._bot = MagicMock()
    return c


@pytest.mark.asyncio
async def test_get_qr_returns_image_bytes_and_content():
    c = _client()
    c._bot._jslogin_get_uuid = AsyncMock()
    c._bot.uuid = "abc123=="
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
        (0, "expired"),
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
    c._bot._has_auth = MagicMock(return_value=True)
    assert c.is_authed() is True
    c._bot._has_auth = MagicMock(return_value=False)
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
