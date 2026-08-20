"""会话状态机：编排微信登录→收文件→打印→收尾全流程，管理小屏亮屏/息屏。"""
import asyncio
import time
from pathlib import Path
from typing import Callable

from printshop.config import Config
from printshop.print_engine import classify_file

SCANNED_TIMEOUT_SECONDS = 120


class SessionState:
    IDLE = "IDLE"
    SCANNED = "SCANNED"
    CONNECTED = "CONNECTED"
    RECEIVING = "RECEIVING"
    PRINTING = "PRINTING"


class SessionManager:
    def __init__(self, cfg: Config, wx, store):
        self._cfg = cfg
        self._wx = wx
        self._store = store

        self.state = SessionState.IDLE
        self.display = False
        self.file_count = 0
        self.qr_png: bytes | None = None
        self.qr_content: str | None = None
        self.on_change: Callable[[], None] | None = None

        self._note = ""
        self._last_event_ts = time.monotonic()
        self._scanned_ts: float | None = None
        self._last_file_ts = time.monotonic()
        self._no_file_reminder = False
        self._seen_msg_ids: set[str] = set()

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
        if self.state != SessionState.IDLE:
            return
        if status == "scanned":
            self._scanned_ts = time.monotonic()
            self._last_event_ts = time.monotonic()
            self._transition(SessionState.SCANNED)
        elif status == "confirmed":
            self._last_event_ts = time.monotonic()
            self._last_file_ts = time.monotonic()
            self._transition(SessionState.CONNECTED)
        elif status == "expired":
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
            return
        new_msgs = []
        for m in msgs:
            mid = m.get("id")
            if mid in self._seen_msg_ids:
                continue
            self._seen_msg_ids.add(mid)
            new_msgs.append(m)
        if not new_msgs:
            return
        files = [m for m in new_msgs
                 if not m.get("is_mine", False) and m.get("type") in ("file", "image")]
        if files:
            self._transition(SessionState.RECEIVING)
        try:
            for m in new_msgs:
                if m.get("is_mine", False):
                    continue
                mtype = m.get("type")
                if mtype in ("file", "image"):
                    await self._receive_file(m)
                elif mtype == "text":
                    self._note = m.get("text", "")
        finally:
            if files:
                self._transition(SessionState.CONNECTED)
        if files:
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

    def _delete_inbox_files(self) -> None:
        for p in self._cfg.inbox_dir.glob("*"):
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass

    async def finish_service(self) -> None:
        try:
            await self._wx.logout()
        finally:
            self._delete_inbox_files()
            self._store.archive_all()
            self.file_count = 0
            self._note = ""
            self._no_file_reminder = False
            self._seen_msg_ids.clear()
            self._scanned_ts = None
            self._last_event_ts = time.monotonic()
            self._transition(SessionState.IDLE)
            self._set_display(False)

    async def run_loop(self) -> None:
        while True:
            try:
                if self.state == SessionState.IDLE:
                    if self.display:
                        self._check_idle_sleep()
                    result = await self._wx.poll_login()
                    await self._handle_poll(result)
                elif self.state == SessionState.SCANNED:
                    await self._check_scanned_timeout()
                elif self.state == SessionState.CONNECTED:
                    if self.display:
                        await self._process_messages()
                    self._check_no_file_timeout()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self._cfg.poll_interval)
