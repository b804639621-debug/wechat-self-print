"""微信文件传输助手网页版协议适配层。包装 vendor/direct_bot.py，对上层屏蔽细节。"""
import tempfile
from pathlib import Path

import httpx

from printshop.vendor.direct_bot import WeChatHelperBot

# 以 M1 实测为准（docs/m1-verification.md）：uuid 为 base64 风格字符串（可含 "="）
QR_CONTENT_TEMPLATE = "https://login.weixin.qq.com/l/{uuid}"

# vendor _poll_login_once() 实际返回 int 状态码
_LOGIN_CODE_STATUS = {
    408: "wait_scan",
    201: "scanned",
    200: "confirmed",
    400: "expired",
    500: "expired",
    0: "expired",
}


class WeChatClient:
    def __init__(self, entry_host: str = "szfilehelper.weixin.qq.com"):
        self.entry_host = entry_host
        self._bot = WeChatHelperBot(entry_host=entry_host)

    async def _ensure_started(self) -> None:
        if self._bot.client is None:
            await self._bot.start()

    async def get_qr(self) -> tuple[bytes, str]:
        """返回 (二维码图片字节, 二维码内容字符串)。字节实际为 JPEG 编码，勿假定 PNG。"""
        await self._ensure_started()
        await self._bot._jslogin_get_uuid()
        uuid = self._bot.uuid
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
        bot.skey = bot.sid = bot.uin = bot.pass_ticket = ""
        bot.user_name = ""
        bot.uuid = ""
        bot.is_logged_in = False

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
        return bool(self._bot._has_auth())

    async def restore_session(self) -> bool:
        await self._ensure_started()
        await self._bot._load_session()
        return bool(self._bot._has_auth())
