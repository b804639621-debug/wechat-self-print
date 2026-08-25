"""微信文件传输助手网页版协议适配层。包装 vendor/direct_bot.py，对上层屏蔽细节。"""
import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

import httpx

from printshop.vendor.direct_bot import WeChatHelperBot

_log = logging.getLogger("printshop.wechat_bot")

# 以 M1 实测为准（docs/m1-verification.md）：uuid 为 base64 风格字符串（可含 "="）
QR_CONTENT_TEMPLATE = "https://login.weixin.qq.com/l/{uuid}"

# vendor _poll_login_once() 实际返回 int 状态码
# 0 的含义是"uuid 为空"或"本次请求网络异常"（direct_bot.py:521/537），并非码过期：
# 必须映射 wait_scan 静默等下轮。若按 expired 处理，finish_service/启动后 uuid 被清空时
# run_loop 会在无人亮屏的情况下反复 jslogin 自动出码（违背"息屏待唤醒"模型），且与
# wake 并发换码会造成显示码≠轮询码（2026-08-25 实机"扫码确认后检测不到登录"根因）。
_LOGIN_CODE_STATUS = {
    408: "wait_scan",
    201: "scanned",
    200: "confirmed",
    400: "expired",
    500: "expired",
    0: "wait_scan",
}

# 码复用窗口：vendor 以 240s 判 uuid 过期（direct_bot.py:248），留 60s 余量，
# 避免复用一张即将过期的码（用户扫到一半码被判 400）。
QR_REUSE_SECONDS = 180


class WeChatClient:
    def __init__(self, entry_host: str = "szfilehelper.weixin.qq.com"):
        self.entry_host = entry_host
        self._bot = WeChatHelperBot(entry_host=entry_host)
        # get_qr 并发互斥：托盘/小屏唤醒与 run_loop 换码可能同刻触发，锁内双重检查
        # 保证并发唤醒只产生一次 jslogin（显示码与轮询 uuid 不错位）
        self._qr_lock = asyncio.Lock()
        # I4：本项目强制默认关闭 HTTP trace（vendor 默认开）。trace 会把顾客昵称/备注/
        # 文件名写进 trace_logs/*.jsonl，属 PII。默认无条件置 False；仅排障时可显式设
        # 环境变量 WECHAT_TRACE_ENABLED=1 临时开启，用后须清理 trace_logs 目录
        # （logout 已配套自动清理）。
        self._bot.trace_enabled = os.environ.get("WECHAT_TRACE_ENABLED") == "1"

    @property
    def trace_dir(self) -> Path:
        """vendor trace 日志目录（登出清理 PII 用）。"""
        return Path(self._bot.trace_dir)

    async def _ensure_started(self) -> None:
        if self._bot.client is None:
            await self._bot.start()

    async def get_qr(self, force: bool = False) -> tuple[bytes, str]:
        """返回 (二维码图片字节, 二维码内容字符串)。字节实际为 JPEG 编码，勿假定 PNG。

        force=False 且现有 uuid 年龄 < QR_REUSE_SECONDS 时复用当前码（不发 jslogin、
        返回空字节）：wake/重复唤醒必须复用同一张码，否则用户扫的码与主控轮询的
        uuid 错位，确认登录后主控永远检测不到。force=True（扫码超时作废等）或无码/
        码老时重新 jslogin。
        """
        await self._ensure_started()
        async with self._qr_lock:
            bot = self._bot
            if not force and bot.uuid and (time.time() - bot.uuid_ts) < QR_REUSE_SECONDS:
                return b"", QR_CONTENT_TEMPLATE.format(uuid=bot.uuid)
            await bot._jslogin_get_uuid()
            uuid = bot.uuid
            if not uuid:
                raise RuntimeError("获取登录二维码 uuid 失败")
            async with httpx.AsyncClient(timeout=15) as cli:
                resp = await cli.get(f"https://login.weixin.qq.com/qrcode/{uuid}")
                resp.raise_for_status()
            return resp.content, QR_CONTENT_TEMPLATE.format(uuid=uuid)

    async def poll_login(self) -> str:
        await self._ensure_started()
        code = await self._bot._poll_login_once()
        return _LOGIN_CODE_STATUS.get(code, "wait_scan")

    async def fetch_messages(self) -> list[dict]:
        await self._ensure_started()
        return await self._bot.get_latest_messages()

    async def download(self, msg_id: str) -> bytes:
        await self._ensure_started()
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_path = Path(tmp_dir) / f"dl_{msg_id}"
            ok = await self._bot.download_message_content(str(msg_id), str(save_path))
            if not ok:
                raise RuntimeError(f"下载消息 {msg_id} 内容失败")
            return save_path.read_bytes()

    async def logout(self) -> None:
        await self._ensure_started()
        bot = self._bot
        if bot.client is not None:
            url = f"https://{bot.entry_host}/cgi-bin/mmwebwx-bin/webwxlogout"
            params = {"redirect": "1", "type": "1", "skey": bot.skey, "sid": bot.sid}
            try:
                resp = await bot.client.post(url, params=params)
                resp.raise_for_status()
            except Exception:
                pass
        self._delete_persisted_session(bot)
        self._clean_traces(bot)
        self._clear_message_cache(bot)
        bot.skey = bot.sid = bot.uin = bot.pass_ticket = ""
        bot.user_name = ""
        bot.uuid = ""
        bot.is_logged_in = False

    @staticmethod
    def _clean_traces(bot) -> None:
        """清空 trace_logs 目录内容（历史残留 PII，防隐私泄漏）。"""
        trace_dir = getattr(bot, "trace_dir", None)
        if trace_dir is None:
            return
        try:
            for p in Path(trace_dir).glob("*"):
                if p.is_file():
                    p.unlink()
        except Exception:
            pass

    @staticmethod
    def _clear_message_cache(bot) -> None:
        """跨顾客会话隔离：登出时清空 vendor 内存消息缓存。

        有 clear_message_cache()（本项目本地新增）则调用；测试 mock bot 无此方法时兜底。
        """
        method = getattr(bot, "clear_message_cache", None)
        if callable(method):
            method()

    @staticmethod
    def _delete_persisted_session(bot) -> None:
        """删除 vendor 持久化的 state.json，防止登出后 restore_session() 恢复旧凭证。

        路径与 vendor _load_session()/save_session() 一致（bot.state_path）。
        删除失败（文件不存在、路径非法等）不应抛出，登出本身不因此中断。
        """
        state_path = getattr(bot, "state_path", None)
        if state_path is None:
            return
        try:
            path = Path(state_path)
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def is_authed(self) -> bool:
        """会话是否仍有效（服务端确认态）。

        用 vendor 的 is_logged_in 而非 _has_auth()：vendor 在 synccheck 检测到
        loginout（retcode!=0，direct_bot.py:646-647）时只置 is_logged_in=False，
        凭证字段（skey/sid/uin/pass_ticket）并未清空，只有 is_logged_in 能反映
        服务端失效。
        """
        return bool(self._bot.is_logged_in)

    async def save_session(self) -> bool:
        """持久化当前登录态到 vendor state.json，成功返回 True（供定时保存推进时间戳）。"""
        await self._ensure_started()
        return bool(await self._bot.save_session())

    async def restore_session(self) -> bool:
        await self._ensure_started()
        await self._bot._load_session()
        return bool(self._bot._has_auth())
