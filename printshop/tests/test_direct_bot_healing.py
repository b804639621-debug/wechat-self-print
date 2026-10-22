"""断网自愈回归测试（2026 实机缺陷：长断网后老进程持续 ConnectError 只能重启恢复）。

直接构造 httpx.ConnectError 序列验证 _jslogin_get_uuid 的有限重试 + client 重建：
- 前几次 ConnectError、最后一次成功 → 重建过 client、uuid 正常取得；
- 连续失败到重试耗尽 → 原样抛出最后一次异常；
- 服务端 HTTP 4xx/5xx（HTTPStatusError，协议层错误）不重试。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from printshop.vendor.direct_bot import _JSLOGIN_BACKOFF, _JSLOGIN_RETRIES, WeChatHelperBot


def _bot() -> WeChatHelperBot:
    bot = WeChatHelperBot(entry_host="szfilehelper.weixin.qq.com")
    bot.client = MagicMock()
    bot.trace_enabled = False
    return bot


def _qr_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.text = 'window.QRLogin.uuid = "heal-uuid=="'
    return resp


async def _zero_sleep(_delay: float) -> None:
    return None


@pytest.mark.asyncio
async def test_jslogin_retries_and_rebuilds_client_on_connect_error(monkeypatch):
    """断网恢复后重建 client 即可继续取码，无需重启进程。"""
    monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
    bot = _bot()
    bot._rebuild_client = AsyncMock()
    bot.client.get = AsyncMock(
        side_effect=[httpx.ConnectError("All connection attempts failed"),
                     _qr_response()])

    await bot._jslogin_get_uuid()

    assert bot.client.get.await_count == 2
    bot._rebuild_client.assert_awaited_once()
    assert bot.uuid == "heal-uuid=="
    assert bot.uuid_ts > 0


@pytest.mark.asyncio
async def test_jslogin_raises_after_retries_exhausted(monkeypatch):
    """持续连不上：耗尽 _JSLOGIN_RETRIES 次后原样抛出，重建尝试 RE-1 次。"""
    monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
    bot = _bot()
    bot._rebuild_client = AsyncMock()
    bot.client.get = AsyncMock(
        side_effect=[httpx.ConnectError("All connection attempts failed")
                     for _ in range(_JSLOGIN_RETRIES)])

    with pytest.raises(httpx.ConnectError):
        await bot._jslogin_get_uuid()

    assert bot.client.get.await_count == _JSLOGIN_RETRIES
    assert bot._rebuild_client.await_count == _JSLOGIN_RETRIES - 1
    assert bot.uuid == ""


@pytest.mark.asyncio
async def test_jslogin_backoff_is_exponential(monkeypatch):
    delays: list[float] = []

    async def _capture_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _capture_sleep)
    bot = _bot()
    bot._rebuild_client = AsyncMock()
    bot.client.get = AsyncMock(
        side_effect=[httpx.ConnectError("down"), httpx.ConnectError("down"),
                     _qr_response()])

    await bot._jslogin_get_uuid()

    assert delays == [_JSLOGIN_BACKOFF * (2 ** i) for i in range(len(delays))]
    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
async def test_jslogin_http_status_error_not_retried():
    """协议层错误（服务端应答）不是断网，不重建不重试。"""
    bot = _bot()
    bot._rebuild_client = AsyncMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "server said no", request=MagicMock(), response=MagicMock()))
    bot.client.get = AsyncMock(return_value=resp)

    with pytest.raises(httpx.HTTPStatusError):
        await bot._jslogin_get_uuid()

    assert bot.client.get.await_count == 1
    bot._rebuild_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebuild_client_closes_old_and_keeps_hooks():
    bot = _bot()
    bot.trace_enabled = False
    old_client = bot.client
    old_client.aclose = AsyncMock()
    await bot._rebuild_client()
    assert bot.client is not old_client
    assert bot.client is not None
    old_client.aclose.assert_awaited_once()
