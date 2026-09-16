"""vendor download_message_content 的图片下载回归测试。

钉死 2026-08-31 实机缺陷：webwxgetmsgimg 带 type=slave 会返回缩略图，
导致打印模糊。图片消息必须省略 type 参数以下载原图。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from printshop.vendor.direct_bot import WeChatHelperBot


def _bot_with_raw(raw: dict) -> WeChatHelperBot:
    bot = WeChatHelperBot()
    bot.check_login_status = AsyncMock(return_value=True)
    bot.skey = "test-skey"
    bot._raw_by_id[str(raw["MsgId"])] = raw
    return bot


@pytest.mark.asyncio
async def test_image_download_url_requests_original_not_thumbnail(tmp_path):
    bot = _bot_with_raw({"MsgId": "img-1", "MsgType": 3})

    resp = MagicMock()
    resp.content = b"\xff\xd8\xff\xe0original-jpeg-bytes"
    resp.raise_for_status = MagicMock()
    bot.client = MagicMock()
    bot.client.get = AsyncMock(return_value=resp)

    save_path = tmp_path / "img.jpg"
    ok = await bot.download_message_content("img-1", str(save_path))

    assert ok
    assert save_path.read_bytes() == resp.content
    url = bot.client.get.await_args.args[0]
    assert "webwxgetmsgimg" in url
    assert "MsgID=img-1" in url
    # 缩略图参数必须不存在；原图即省略 type
    assert "type=" not in url


@pytest.mark.asyncio
async def test_file_download_url_unchanged(tmp_path):
    """文件消息（AppMsgType==6）走 webwxgetmedia，不受图片修复影响。"""
    bot = _bot_with_raw({
        "MsgId": "file-1", "MsgType": 49, "AppMsgType": 6,
        "FromUserName": "wxid_sender", "MediaId": "@mid", "EncryFileName": "@enc",
    })
    bot._get_cookie = MagicMock(return_value="ticket")
    bot.uin = "12345"
    bot.sid = "sid"
    bot.pass_ticket = "pt"

    resp = MagicMock()
    resp.content = b"%PDF-file-bytes"
    resp.raise_for_status = MagicMock()
    bot.client = MagicMock()
    bot.client.get = AsyncMock(return_value=resp)

    save_path = tmp_path / "doc.pdf"
    ok = await bot.download_message_content("file-1", str(save_path))

    assert ok
    assert save_path.read_bytes() == resp.content
    url = bot.client.get.await_args.args[0]
    assert "webwxgetmedia" in url
    assert "mediaid=%40mid" in url
