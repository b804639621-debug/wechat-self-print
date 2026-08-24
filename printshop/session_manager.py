"""会话状态机：编排微信登录→收文件→打印→收尾全流程，管理小屏亮屏/息屏。

含加固与恢复：断线指数退避重连、启动时凭证恢复、run_loop 看门狗、会话定时保存。
"""
import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

import httpx

from printshop.config import Config
from printshop.print_engine import classify_file

_log = logging.getLogger(__name__)

SCANNED_TIMEOUT_SECONDS = 120


def _is_network_error(exc: BaseException) -> bool:
    """判定异常是否属于网络类故障。

    协议层错误（服务端明确返回错误状态，如 HTTP 400）不算网络故障，
    按现有错误处理语义（吞掉继续按 poll_interval 轮询）走；传输层/超时/
    连接类错误才算网络故障，走指数退避重连。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return False
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, (ConnectionError, asyncio.TimeoutError, OSError)):
        return True
    return False


class SessionState:
    IDLE = "IDLE"
    SCANNED = "SCANNED"
    CONNECTED = "CONNECTED"
    RECEIVING = "RECEIVING"
    PRINTING = "PRINTING"


class SessionManager:
    def __init__(self, cfg: Config, wx, store, *,
                 sleep=asyncio.sleep, max_iterations: int | None = None):
        self._cfg = cfg
        self._wx = wx
        self._store = store
        self._sleep = sleep
        # max_iterations 仅测试用途：限定 run_loop 迭代次数，便于确定性退出，生产为 None
        self.max_iterations = max_iterations

        self.state = SessionState.IDLE
        self.display = False
        self.file_count = 0
        self.qr_png: bytes | None = None
        self.qr_content: str | None = None
        self.on_change: Callable[[], None] | None = None
        self.on_note: Callable[[], None] | None = None
        self.on_admin_notice: Callable[[str, str], None] | None = None

        self._note = ""
        self._last_event_ts = time.monotonic()
        self._scanned_ts: float | None = None
        self._last_file_ts = time.monotonic()
        self._no_file_reminder = False
        self._seen_msg_ids: set[str] = set()
        self._reconnect_failures = 0
        self._last_save_ts = time.monotonic()

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    def _set_display(self, value: bool) -> None:
        if self.display != value:
            self.display = value
            self._notify()

    def _transition(self, new_state: str) -> None:
        if new_state != self.state:
            self.state = new_state
            self._notify()

    async def _refresh_qr(self) -> None:
        self.qr_png, self.qr_content = await self._wx.get_qr()
        self._notify()

    async def wake(self) -> None:
        if self.state != SessionState.IDLE:
            return
        self._set_display(True)
        self._last_event_ts = time.monotonic()
        await self._refresh_qr()

    async def _handle_poll(self, status: str) -> None:
        if self.state not in (SessionState.IDLE, SessionState.SCANNED):
            return
        if status == "scanned":
            self._scanned_ts = time.monotonic()
            self._last_event_ts = time.monotonic()
            self._transition(SessionState.SCANNED)
        elif status == "confirmed":
            self._last_event_ts = time.monotonic()
            self._last_file_ts = time.monotonic()
            self._transition(SessionState.CONNECTED)
            # 保留此处保存：SessionManager 走 wx.poll_login()→vendor _poll_login_once()
            # 直连路径，不会触发 vendor check_login_status() 内部的 save_session
            # （direct_bot.py:324 仅在 check_login_status 路径生效），故必须显式保存。
            await self._save_session()
        elif status == "expired":
            if self.state == SessionState.SCANNED:
                self._transition(SessionState.IDLE)
            await self._refresh_qr()

    def _check_idle_sleep(self) -> None:
        if self.state == SessionState.IDLE and self.display:
            if time.monotonic() - self._last_event_ts >= self._cfg.idle_sleep_seconds:
                self._set_display(False)

    async def _check_scanned_timeout(self) -> None:
        if self.state != SessionState.SCANNED or self._scanned_ts is None:
            return
        if time.monotonic() - self._scanned_ts >= SCANNED_TIMEOUT_SECONDS:
            self._transition(SessionState.IDLE)
            await self._refresh_qr()

    def _check_no_file_timeout(self) -> None:
        if self.state == SessionState.CONNECTED and not self._no_file_reminder:
            if time.monotonic() - self._last_file_ts >= self._cfg.no_file_timeout:
                self._no_file_reminder = True

    def _unique_path(self, name: str) -> Path:
        p = self._cfg.inbox_dir / name
        if not p.exists():
            return p
        stem = Path(name).stem
        suffix = Path(name).suffix
        i = 1
        while True:
            cand = self._cfg.inbox_dir / f"{stem}_{i}{suffix}"
            if not cand.exists():
                return cand
            i += 1

    async def _receive_file(self, m: dict) -> None:
        mid = m.get("id")
        file_name = m.get("file_name") or str(mid)
        data = await self._wx.download(mid)
        path = self._unique_path(Path(file_name).name)
        path.write_bytes(data)
        self._store.add(
            msg_id=mid,
            file_name=path.name,
            file_path=str(path),
            category=classify_file(path),
            size_bytes=len(data),
            page_count=0,
        )
        self.file_count += 1

    async def _process_messages(self) -> None:
        if self.state != SessionState.CONNECTED:
            return
        msgs = await self._wx.fetch_messages()
        if not msgs:
            # 空列表 + 服务端已判失效（is_logged_in=False，如他端登出/凭证过期）→
            # 登出回 IDLE 出新码，避免 CONNECTED 亮屏空转；凭证仍有效 = 正常无消息。
            # is_authed 是 sync 纯属性读取（WeChatClient.is_authed），勿 await。
            if not self._wx.is_authed():
                await self._logout_to_idle_with_fresh_qr()
            return
        pending = [m for m in msgs if m.get("id") not in self._seen_msg_ids]
        if not pending:
            return
        new_msgs = []
        batch_seen = set()
        for m in pending:
            mid = m.get("id")
            if mid in batch_seen:
                continue
            batch_seen.add(mid)
            new_msgs.append(m)
        files = [m for m in new_msgs
                 if not m.get("is_mine", False) and m.get("type") in ("file", "image")]
        if files:
            self._transition(SessionState.RECEIVING)
        received_any = False
        try:
            for m in new_msgs:
                mid = m.get("id")
                if m.get("is_mine", False):
                    self._seen_msg_ids.add(mid)
                    continue
                mtype = m.get("type")
                if mtype in ("file", "image"):
                    try:
                        await self._receive_file(m)
                    except Exception:
                        continue
                    self._seen_msg_ids.add(mid)
                    received_any = True
                elif mtype == "text":
                    self._note = m.get("text", "")
                    self._seen_msg_ids.add(mid)
                    self._notify()
                    if self.on_note:
                        self.on_note()
        finally:
            if files:
                self._transition(SessionState.CONNECTED)
        if received_any:
            self._no_file_reminder = False
            self._last_file_ts = time.monotonic()

    async def note_text(self) -> str:
        return self._note

    def screen_state(self) -> dict:
        return {
            "state": self.state,
            "display": "on" if self.display else "off",
            "qr_content": self.qr_content,
            "file_count": self.file_count,
            "message": self._note,
        }

    def _clean_dir(self, directory: Path | None) -> list[str]:
        """删除目录下所有文件，返回失败路径列表（不抛异常）。

        防御：仅清理具名的真实目录，空路径/当前目录直接跳过，避免误删 CWD。
        """
        if directory is None:
            return []
        directory = Path(directory)
        if not directory.name or directory in (Path("."), Path("..")):
            return []
        if not directory.exists() or not directory.is_dir():
            return []
        failed: list[str] = []
        for p in directory.iterdir():
            try:
                if p.is_file():
                    p.unlink()
            except Exception as exc:
                _log.warning("删除文件失败: %s (%s)", p, exc)
                failed.append(str(p))
        return failed

    def _trace_dir(self) -> Path | None:
        """trace 日志目录（登出时清理 PII 残留）；无法解析（mock wx）返回 None。"""
        td = getattr(self._wx, "trace_dir", None)
        if td is None:
            return None
        try:
            path = Path(td)
        except Exception:
            return None
        return path if path.name else None

    async def finish_service(self) -> dict:
        """登出 + 清理（inbox/work/trace 顾客产物）并归档，返回清理结果供界面告警。

        I3：清理失败不静默——收集失败列表、日志记录、通过 on_admin_notice 广播告警。
        """
        failures: list[str] = []
        try:
            await self._wx.logout()
        except Exception:
            _log.exception("登出微信失败（继续清理本地状态）")
            failures.append("logout")
        finally:
            failures += self._clean_dir(self._cfg.inbox_dir)
            failures += self._clean_dir(self._cfg.work_dir)
            failures += self._clean_dir(self._trace_dir())
            self._store.archive_all()
            self.file_count = 0
            self._note = ""
            self._no_file_reminder = False
            self._seen_msg_ids.clear()
            self._scanned_ts = None
            self._last_event_ts = time.monotonic()
            self._reconnect_failures = 0
            self._transition(SessionState.IDLE)
            self._set_display(False)
        result = {"ok": not failures, "failures": failures}
        if failures:
            _log.error("服务收尾清理失败：%s", failures)
            if self.on_admin_notice:
                self.on_admin_notice("cleanup_failed", "文件清理失败，请手动删除 inbox")
        return result

    async def _logout_to_idle_with_fresh_qr(self) -> None:
        """登出回到 IDLE 并刷新二维码（断线重试耗尽/服务端凭证失效共用）。"""
        await self.finish_service()
        self._set_display(True)
        await self._refresh_qr()

    async def _save_session(self) -> bool:
        try:
            return bool(await self._wx.save_session())
        except Exception:
            _log.exception("保存微信会话失败（忽略，不影响服务）")
            return False

    async def restore_session(self) -> bool:
        """启动时恢复凭证：有效→直接进服务态（免扫码）；无效/异常→保持 IDLE 正常出码。"""
        try:
            ok = await self._wx.restore_session()
        except Exception:
            _log.exception("恢复微信会话失败，走正常出码流程")
            ok = False
        if ok:
            self._last_event_ts = time.monotonic()
            self._last_file_ts = time.monotonic()
            self._transition(SessionState.CONNECTED)
            self._set_display(True)
        return ok

    def _network_backoff_delay(self, n: int) -> float:
        seq = self._cfg.network_backoff
        return float(seq[min(n - 1, len(seq) - 1)])

    async def _handle_network_failure(self) -> None:
        self._reconnect_failures += 1
        n = self._reconnect_failures
        if n > self._cfg.network_max_failures:
            _log.warning("fetch_messages 连续网络失败 %d 次，登出并刷新二维码", n)
            self._reconnect_failures = 0
            await self._logout_to_idle_with_fresh_qr()
            return
        delay = self._network_backoff_delay(n)
        _log.warning("fetch_messages 网络异常（第 %d 次），%.0fs 后重试", n, delay)
        await self._sleep(delay)

    async def _maybe_periodic_save(self) -> None:
        if self.state not in (SessionState.CONNECTED, SessionState.RECEIVING, SessionState.PRINTING):
            return
        if time.monotonic() - self._last_save_ts < self._cfg.session_save_interval:
            return
        # 仅保存成功后推进时间戳：失败则下轮即重试，缩短重试周期
        if await self._save_session():
            self._last_save_ts = time.monotonic()

    async def _run_once(self) -> None:
        if self.state == SessionState.IDLE:
            if self.display:
                self._check_idle_sleep()
            result = await self._wx.poll_login()
            await self._handle_poll(result)
        elif self.state == SessionState.SCANNED:
            await self._check_scanned_timeout()
            if self.state == SessionState.SCANNED:
                # C1：SCANNED 必须继续轮询，否则顾客点【登录】永远检测不到
                result = await self._wx.poll_login()
                await self._handle_poll(result)
        elif self.state == SessionState.CONNECTED:
            try:
                if self.display:
                    await self._process_messages()
                self._check_no_file_timeout()
                self._reconnect_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_network_error(exc):
                    await self._handle_network_failure()
                else:
                    raise
        await self._maybe_periodic_save()

    def _log_watchdog(self, n: int) -> None:
        if n == 1:
            _log.exception("run_loop 迭代异常（看门狗兜底），继续下一轮")
        elif n % 10 == 0:
            _log.exception("run_loop 迭代异常（看门狗兜底）已连续 %d 次，继续下一轮", n)
        else:
            _log.warning("run_loop 迭代异常第 %d 次（首次及每 10 次记一次完整 traceback），继续下一轮", n)

    async def run_loop(self) -> None:
        iterations = 0
        consecutive = 0
        while True:
            iterations += 1
            if self.max_iterations is not None and iterations > self.max_iterations:
                return
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive += 1
                self._log_watchdog(consecutive)
            else:
                consecutive = 0
            await self._sleep(self._cfg.poll_interval)
