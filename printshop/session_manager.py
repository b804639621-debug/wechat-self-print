"""会话状态机：编排微信登录→收文件→打印→收尾全流程，管理小屏亮屏/息屏。

含加固与恢复：断线指数退避重连、启动时凭证恢复、run_loop 看门狗、会话定时保存。
"""
import asyncio
import ipaddress
import logging
import re
import time
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from printshop.config import Config
from printshop.jobs import JobStatus
from printshop.print_engine import classify_file

_log = logging.getLogger(__name__)

SCANNED_TIMEOUT_SECONDS = 120
MAX_FILE_BYTES = 25 * 1024 * 1024
# 取码失败时经 message 字段（screen_state/_note 通道）显示给顾客的提示
QR_UNAVAILABLE_MESSAGE = "网络异常，暂无二维码，请稍后再试"


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


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = "。，！？；、,.;:!?)]}>\"'）】》」』…"


def _is_public_url(url: str) -> bool:
    """仅接受可安全交给系统浏览器打开的全局公网 http/https URL。"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
    except ValueError:
        return False

    host = host.rstrip(".").lower()
    # 域名不做 DNS 解析：只拒绝明确的本地主机名/本地链路域名形态。
    if host == "localhost" or host.endswith(".localhost"):
        return False
    if host.endswith(".local"):
        return False

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global and not ip.is_multicast


def extract_public_urls(text: str) -> list[str]:
    """从文本提取有序、去重且仅含公网 http/https 的 URL。"""
    urls: list[str] = []
    seen: set[str] = set()
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(_URL_TRAILING_PUNCTUATION)
        if not url or url in seen or not _is_public_url(url):
            continue
        seen.add(url)
        urls.append(url)
    return urls


class SessionState:
    IDLE = "IDLE"
    SCANNED = "SCANNED"
    CONNECTED = "CONNECTED"
    RECEIVING = "RECEIVING"
    PRINTING = "PRINTING"


class SessionManager:
    def __init__(self, cfg: Config, wx, store, *,
                 sleep=asyncio.sleep, max_iterations: int | None = None,
                 link_opener: Callable[[str], object] | None = None):
        self._cfg = cfg
        self._wx = wx
        self._store = store
        self._sleep = sleep
        # max_iterations 仅测试用途：限定 run_loop 迭代次数，便于确定性退出，生产为 None
        self.max_iterations = max_iterations
        self._link_opener = link_opener if link_opener is not None else webbrowser.open

        self.state = SessionState.IDLE
        self.display = False
        self.file_count = 0
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
        self._opened_links: set[str] = set()
        self._greeting_sent = False
        self._reconnect_failures = 0
        self._last_save_ts = time.monotonic()
        # finish_service 与 run_loop 掉线路径（_logout_to_idle_with_fresh_qr）互斥
        self._finish_lock = asyncio.Lock()

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

    async def _refresh_qr(self, *, force: bool = False) -> None:
        _, self.qr_content = await self._wx.get_qr(force=force)
        self._notify()

    async def wake(self) -> bool:
        """触摸/托盘唤醒：先取码成功再亮屏。

        防废码残留（实机缺陷）：原先先亮屏（显示内存残留旧 qr_content）再取码，
        取码失败时旧二维码留在屏上被顾客扫到（微信 uuid ~240s 过期，报"查找失败"）。
        现改为取码失败时清空旧码、屏显网络异常提示，并返回 False 供路由层返回明确错误。

        Returns: True=唤醒成功（新码或复用未过期码）；False=IDLE 以外状态或取码失败。
        """
        if self.state != SessionState.IDLE:
            return False
        self._last_event_ts = time.monotonic()
        # 复用未过期码（get_qr 默认 force=False）：重复唤醒/触摸不得换码，
        # 否则用户扫的码与轮询 uuid 错位，确认登录也检测不到
        try:
            await self._refresh_qr()
        except Exception:
            _log.warning("唤醒取码失败，清空屏上旧二维码（避免过期码被扫）", exc_info=True)
            self._clear_stale_qr()
            return False
        self._set_display(True)
        if self._note == QR_UNAVAILABLE_MESSAGE:
            # 上次唤醒失败留下的提示，取码恢复后清掉，避免误导
            self._note = ""
            self._notify()
        return True

    def _clear_stale_qr(self) -> None:
        """清空旧二维码并经 message 字段提示网络异常（display 保持当前值：
        已亮屏则屏显提示文案，顾客可见；息屏则下轮触摸重新整个流程）。"""
        self.qr_content = None
        self._note = QR_UNAVAILABLE_MESSAGE
        self._notify()

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
            self._opened_links.clear()
            # 保留此处保存：SessionManager 走 wx.poll_login()→vendor _poll_login_once()
            # 直连路径，不会触发 vendor check_login_status() 内部的 save_session
            # （direct_bot.py:324 仅在 check_login_status 路径生效），故必须显式保存。
            # _save_session 内部已捕获异常并返回 False；这里再加一层防御，
            # 确保保存失败也不会跳过登录成功后的欢迎表情发送。
            try:
                await self._save_session()
            except Exception:
                pass
            await self._send_login_greeting()
        elif status == "expired":
            if self.state == SessionState.SCANNED:
                self._transition(SessionState.IDLE)
            await self._refresh_qr()

    async def _send_login_greeting(self) -> None:
        """扫码确认登录后，主动向文件传输助手发送一条欢迎表情。

        发送失败/超时只记录警告，绝不影响登录状态流转；同一登录会话只尝试一次。
        """
        if self._greeting_sent:
            return
        if not self._cfg.send_login_greeting:
            return
        message = self._cfg.login_greeting_text
        if not message:
            return
        # 先置位：无论发送成功/失败，一次登录最多尝试一次
        self._greeting_sent = True
        send_text = getattr(self._wx, "send_text", None)
        if send_text is None:
            return
        try:
            await asyncio.wait_for(send_text(message), timeout=10)
        except Exception as exc:
            _log.warning("发送登录欢迎表情失败: %s", exc)

    def _check_idle_sleep(self) -> None:
        if self.state == SessionState.IDLE and self.display:
            if time.monotonic() - self._last_event_ts >= self._cfg.idle_sleep_seconds:
                self._set_display(False)

    async def _check_scanned_timeout(self) -> None:
        if self.state != SessionState.SCANNED or self._scanned_ts is None:
            return
        if time.monotonic() - self._scanned_ts >= SCANNED_TIMEOUT_SECONDS:
            self._transition(SessionState.IDLE)
            # force 换新码：旧码处于"已扫待确认"(201) 态，必须作废，
            # 复用它会让下一位顾客扫到一张已被占用、无法再次确认的码
            await self._refresh_qr(force=True)

    def _check_no_file_timeout(self) -> None:
        if self.state == SessionState.CONNECTED and not self._no_file_reminder:
            if time.monotonic() - self._last_file_ts >= self._cfg.no_file_timeout:
                self._no_file_reminder = True
                self._notify()

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
        if len(data) > MAX_FILE_BYTES:
            # I6：超限不落盘、不入库内容——标红任务并注明原因（微信侧本应拦截 >25MB）
            jid = self._store.add(msg_id=mid, file_name=Path(file_name).name, file_path="",
                                  category="other", size_bytes=len(data), page_count=0)
            self._store.set_status(jid, JobStatus.FAILED, error="文件超过 25MB，未接收")
            self.file_count += 1
            return
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

    async def _open_links(self, text: str) -> None:
        """打开顾客文本消息中的公网链接；任何异常仅记录，不影响收文件流程。"""
        if not self._cfg.auto_open_links:
            return
        try:
            urls = extract_public_urls(text)
        except Exception:
            _log.warning("提取链接失败（忽略）", exc_info=True)
            return
        for url in urls:
            if len(self._opened_links) >= self._cfg.auto_open_max_links:
                break
            if url in self._opened_links:
                continue
            try:
                await asyncio.to_thread(self._link_opener, url)
            except Exception as exc:
                _log.warning("打开链接失败: %s (%s)", url, exc)
                continue
            self._opened_links.add(url)
            if len(self._opened_links) >= self._cfg.auto_open_max_links:
                break

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
        # 文件传输助手是"自聊"：顾客从手机发来的消息 is_mine 恒为 True（from_user 即
        # 登录账号自身），绝不能按通用机器人惯例过滤 is_mine（2026-08-25 Task 9 实机
        # 缺陷：全过滤导致顾客消息全灭）。本应用从不主动发消息，无自收风险。
        files = [m for m in new_msgs if m.get("type") in ("file", "image")]
        if files:
            self._transition(SessionState.RECEIVING)
        received_any = False
        try:
            for m in new_msgs:
                mid = m.get("id")
                mtype = m.get("type")
                if mtype in ("file", "image"):
                    try:
                        await self._receive_file(m)
                    except Exception:
                        continue
                    self._seen_msg_ids.add(mid)
                    received_any = True
                elif mtype == "text":
                    text = m.get("text", "")
                    self._note = text
                    self._seen_msg_ids.add(mid)
                    self._notify()
                    if self.on_note:
                        self.on_note()
                    await self._open_links(text)
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
            "no_file_reminder": self._no_file_reminder,
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
        全程持 _finish_lock：与 run_loop 掉线路径互斥，避免并发嵌套收尾。
        """
        async with self._finish_lock:
            return await self._finish_service_locked()

    async def _finish_service_locked(self) -> dict:
        """finish_service 的无锁核心，调用方必须已持有 _finish_lock。"""
        failures: list[str] = []
        try:
            await self._wx.logout()
        except Exception:
            _log.exception("登出微信失败（继续清理本地状态）")
            failures.append("logout")
        finally:
            self._opened_links.clear()
            self._greeting_sent = False
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
        """登出回到 IDLE 并刷新二维码（断线重试耗尽/服务端凭证失效共用）。

        与 finish_service 并发时让位：拿到锁时若状态已是 IDLE，说明管理员主动
        收尾已完成（息屏静默），掉线路径不得再亮屏出码（2026-08-25 实机：
        点完成服务后小屏又被并发掉线检测点亮出新码）。
        """
        async with self._finish_lock:
            if self.state == SessionState.IDLE:
                return
            await self._finish_service_locked()
            self._set_display(True)
            try:
                await self._refresh_qr()
            except Exception:
                _log.warning("掉线恢复取码失败，清空屏上旧二维码", exc_info=True)
                self._clear_stale_qr()

    async def _save_session(self) -> bool:
        try:
            return bool(await self._wx.save_session())
        except Exception:
            _log.exception("保存微信会话失败（忽略，不影响服务）")
            return False

    def _restore_file_count(self) -> None:
        """M3：启动时从 JobStore 数活跃任务，恢复 file_count（重启后不归零）。"""
        try:
            self.file_count = len(self._store.list_active())
        except Exception:
            pass

    async def restore_session(self) -> bool:
        """启动时恢复凭证：有效→直接进服务态（免扫码）；无效/异常→保持 IDLE 静默，
        不再自动出码（uuid 空映射 wait_scan），待管理员托盘/小屏触摸唤醒时出码。"""
        try:
            ok = await self._wx.restore_session()
        except Exception:
            _log.exception("恢复微信会话失败，走正常出码流程")
            ok = False
        self._restore_file_count()
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
