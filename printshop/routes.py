"""HTTP/WS 路由层：REST API + WebSocket 推送（对外接口契约见 task-6-brief 端点表）。

线程模型说明：JobStore 为长连接（check_same_thread=True），创建于事件循环线程。
所有路由 handler 均为 async def，运行在事件循环线程——内部同步调用 JobStore
（微秒级，可接受）。仅打印执行（subprocess，秒级）用 asyncio.to_thread，
且工作线程内不触碰 JobStore：先取数、打印、回事件循环后再 set_status。
"""
import asyncio
import ipaddress
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from printshop.jobs import JobStatus
from printshop.print_engine import (
    PrintOptions,
    get_page_count,
    list_printers,
    normalize_to_pdf,
    print_pdf,
    render_page_png,
)
from printshop.session_manager import SessionState

_log = logging.getLogger(__name__)


def _is_loopback_host(host: str | None) -> bool:
    """判定客户端 host 是否本机回环（含 TestClient 的 'testclient'）。

    供 main.py HTTP 中间件与 ws_admin WS 握手共用：管理端点/WS 仅允许本机。
    """
    if not host:
        return True
    if host in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True  # 无法解析的 host（如自定义 test client）视为本机


async def _ws_loopback_guard(ws: WebSocket) -> bool:
    """WS 握手 host 校验：非回环直接 close(1008) 拒绝，返回 True=放行。

    Starlette 的 HTTP 中间件不会拦截 websocket scope，必须在端点内把关。
    拒绝发生在 accept 之前（ASGI 规范允许先发 close），uvicorn 会回 HTTP 403。
    """
    host = ws.client.host if ws.client else None
    if not _is_loopback_host(host):
        await ws.close(code=1008)
        return False
    return True


class PrintOptionsBody(BaseModel):
    printer: str
    color: bool = True
    copies: int = Field(1, ge=1, le=99)
    pages: str = ""
    duplex: str = "simplex"
    paper: str = "A4"


class PrintBatchBody(BaseModel):
    ids: list[int]
    options: PrintOptionsBody


class AppContext:
    """路由共享上下文：配置、数据层、会话状态机、WS 客户端集与广播。"""

    def __init__(self, cfg, store, wx, sm, ble=None):
        self.cfg = cfg
        self.store = store
        self.wx = wx
        self.sm = sm
        self.ble = ble  # BleScreenLink（None=BLE 禁用，WiFi 轮询回滚路径）
        # 在线判定阈值随 config 走（默认 12.0s > 固件闲时轮询 5s×2），admin.html 读 threshold_seconds 渲染
        self.screen_online_threshold_seconds = float(cfg.screen_online_threshold_seconds)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        # 小屏电池最近一次上报（固件轮询 /api/screen/state 时捎带 batt_* 参数），空 = 未上报
        self.device: dict = {}
        # 小屏最近一次 WiFi 轮询时间；None = 尚未见过任何轮询
        self.screen_seen_at: datetime | None = None

    def _screen_online(self) -> bool:
        """按当前 screen_seen_at 判断小屏是否在线（未上报视为离线）。"""
        if self.screen_seen_at is None:
            return False
        age = (datetime.now() - self.screen_seen_at).total_seconds()
        return age < self.screen_online_threshold_seconds

    def mark_screen_seen(self) -> bool:
        """记录一次 WiFi 轮询；返回在线状态是否由离线变为在线（首次也算 True）。"""
        now = datetime.now()
        was_online = self._screen_online()
        self.screen_seen_at = now
        return not was_online

    def screen_payload(self) -> dict:
        """管理后台用的小屏连接状态快照。"""
        last_seen_at = (self.screen_seen_at.isoformat(timespec="seconds")
            if self.screen_seen_at is not None else None)
        return {
            "transport": "wifi",
            "online": self._screen_online(),
            "last_seen_at": last_seen_at,
            "threshold_seconds": self.screen_online_threshold_seconds,
        }

    def device_payload(self) -> dict:
        """统一 device 广播载荷：电池 + 小屏连接状态。"""
        return {
            "battery": self.battery_payload(),
            "screen": self.screen_payload(),
        }

    def update_battery(self, report: dict) -> bool:
        """存入电池上报；返回 True = 值得广播的变化（首次或 pct/充电态/USB态 变化）。

        电压(mV)随测量抖动，不参与变化判定，但最新值照常落库。
        """
        old = self.device
        old_key = (old.get("pct"), old.get("charging"), old.get("vbus")) if old else None
        self.device = report
        return old_key != (report["pct"], report["charging"], report["vbus"])

    def battery_payload(self) -> dict | None:
        if not self.device:
            return None
        payload = dict(self.device)
        # WiFi：轮询每轮捎带（电量值 60s 节奏），阈值为 180s；BLE：心跳间隔+300s
        payload["stale_after"] = (int(self.cfg.ble_heartbeat_seconds) + 300
                                  if self.ble is not None else 180)
        return payload

    def emit(self, msg_type: str, **payload) -> None:
        data = {"type": msg_type}
        data.update(payload)
        self._broadcast(json.dumps(data, ensure_ascii=False))

    def _broadcast(self, text: str) -> None:
        loop = self.loop
        if loop is None or not self.clients:
            return
        for ws in list(self.clients):
            try:
                loop.call_soon_threadsafe(self._schedule_send, ws, text)
            except RuntimeError:
                pass

    @staticmethod
    def _schedule_send(ws: WebSocket, text: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_safe_send(ws, text))


async def _safe_send(ws: WebSocket, text: str) -> None:
    try:
        await ws.send_text(text)
    except Exception:
        pass


def _job_dict(job) -> dict:
    return {
        "id": job.id,
        "msg_id": job.msg_id,
        "file_name": job.file_name,
        "file_path": job.file_path,
        "category": job.category,
        "size_bytes": job.size_bytes,
        "page_count": job.page_count,
        "status": job.status,
        "options_json": job.options_json,
        "error": job.error,
        "created_at": job.created_at,
        "printed_at": job.printed_at,
    }


def _printable_status(job) -> str | None:
    """返回不可打印的原因；可打印（PENDING/FAILED/DONE）时返回 None。"""
    if job.status == JobStatus.PRINTING:
        return "任务正在打印中，请勿重复操作"
    if job.status == JobStatus.DISCARDED:
        return "任务已结束，无法打印"
    if job.status in (JobStatus.PENDING, JobStatus.FAILED, JobStatus.DONE):
        return None
    return "任务已结束，无法打印"


def _validate_job_path(cfg, file_path: str) -> Path:
    """校验文件存在且 resolve 后位于 inbox/work 之内，防止未认证端触发任意路径执行。"""
    p = Path(file_path)
    try:
        resolved = p.expanduser().resolve(strict=False)
    except Exception:
        resolved = p.absolute()
    if not resolved.is_file():
        raise FileNotFoundError("文件不存在")
    inbox = cfg.inbox_dir.resolve()
    work = cfg.work_dir.resolve()
    if not (resolved.is_relative_to(inbox) or resolved.is_relative_to(work)):
        raise PermissionError("路径越界，禁止操作")
    return resolved


def _parse_battery_report(pct: str | None, mv: str | None,
                          chg: str | None, vbus: str | None) -> dict | None:
    """解析小屏轮询捎带的电量 query 参数；无上报或任一字段非法 → None（整体忽略）。

    宽容解析（声明 str 而非 int）：参数畸形也绝不能把小屏的状态轮询打成 422。
    """
    if pct is None:
        return None

    def _int(s: str | None) -> int | None:
        try:
            return int(s) if s is not None else None
        except ValueError:
            return None

    p, m, c, v = _int(pct), _int(mv), _int(chg), _int(vbus)
    if p is None or not (0 <= p <= 100):
        return None
    if mv is not None and (m is None or not (0 <= m <= 5000)):
        return None
    if chg is not None and c not in (0, 1):
        return None
    if vbus is not None and v not in (0, 1):
        return None
    return {
        "pct": p,
        "mv": m or 0,
        "charging": bool(c),
        "vbus": bool(v),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    cfg = ctx.cfg
    store = ctx.store
    sm = ctx.sm

    def emit_job_update(job_id: int, status: str, error: str = "") -> None:
        payload = {"id": job_id, "status": status}
        if error:
            payload["error"] = error
        ctx.emit("job_update", **payload)

    async def ensure_pdf(job) -> Path:
        """normalize 产物按 job_id 缓存在 work_dir，已转换过的不重复转换。

        I6：Office COM 转换可能挂起——asyncio.wait_for 120s 超时后转 LibreOffice 轨再兜底。
        """
        cached = cfg.work_dir / f"job_{job.id}.pdf"
        if cached.exists():
            return cached
        src = Path(job.file_path)
        if not src.exists():
            raise FileNotFoundError(f"源文件不存在：{job.file_name}")
        try:
            out = await asyncio.wait_for(
                asyncio.to_thread(normalize_to_pdf, src, cfg.work_dir, cfg),
                timeout=cfg.office_convert_timeout)
        except asyncio.TimeoutError:
            _log.warning("Office 转换超时（>%.0fs），转 LibreOffice 轨", cfg.office_convert_timeout)
            out = await asyncio.to_thread(normalize_to_pdf, src, cfg.work_dir, cfg,
                                          office_com=False)
        shutil.copy(out, cached)
        return cached

    def backfill_page_count(job, pdf: Path) -> None:
        if job.page_count:
            return
        try:
            pc = get_page_count(pdf)
        except Exception:
            return
        store.set_page_count(job.id, pc)
        job.page_count = pc

    async def do_print(job, body: PrintOptionsBody) -> dict:
        try:
            pdf = await ensure_pdf(job)
        except Exception as exc:
            store.set_status(job.id, JobStatus.FAILED, error=str(exc))
            emit_job_update(job.id, JobStatus.FAILED, error=str(exc))
            return {"ok": False, "error": f"文件转换失败：{exc}"}
        backfill_page_count(job, pdf)
        opts = PrintOptions(printer=body.printer, color=body.color, copies=body.copies,
                            pages=body.pages, duplex=body.duplex, paper=body.paper)
        prev = sm.state
        if prev in (SessionState.CONNECTED, SessionState.RECEIVING):
            sm._transition(SessionState.PRINTING)
        ok, error = await asyncio.to_thread(print_pdf, cfg, pdf, opts)
        if prev in (SessionState.CONNECTED, SessionState.RECEIVING):
            sm._transition(SessionState.CONNECTED)
        if ok:
            store.set_status(job.id, JobStatus.DONE,
                             options_json=json.dumps(body.model_dump(), ensure_ascii=False))
            store.mark_printed(job.id)
            emit_job_update(job.id, JobStatus.DONE)
        else:
            store.set_status(job.id, JobStatus.FAILED, error=error)
            emit_job_update(job.id, JobStatus.FAILED, error=error)
        return {"ok": ok, "error": "" if ok else error}

    @router.get("/api/screen/state")
    async def screen_state(batt_pct: str | None = None, batt_mv: str | None = None,
                           batt_chg: str | None = None, batt_vbus: str | None = None):
        """小屏轮询主状态；固件捎带 batt_* 电量参数时更新设备电量（变化时广播 device）。"""
        # 无论是否捎带电量，轮询本身即代表小屏 WiFi 在线，先记录上报时间。
        screen_changed = ctx.mark_screen_seen()
        report = _parse_battery_report(batt_pct, batt_mv, batt_chg, batt_vbus)
        battery_changed = report is not None and ctx.update_battery(report)
        if screen_changed or battery_changed:
            ctx.emit("device", **ctx.device_payload())
        return {**sm.screen_state(), "battery": ctx.battery_payload()}

    @router.post("/api/screen/wake")
    async def screen_wake():
        await sm.wake()
        return {"ok": True}

    @router.get("/api/printers")
    async def printers():
        return {"printers": list_printers()}

    @router.get("/api/jobs")
    async def jobs_list():
        active = store.list_active()
        recent_done = store.list_recent_done(30)
        seen = set()
        jobs = []
        for job in active + recent_done:
            if job.id not in seen:
                seen.add(job.id)
                jobs.append(job)
        return [_job_dict(j) for j in jobs]

    @router.get("/api/jobs/{job_id}/preview")
    async def job_preview(job_id: int, page: int = 0):
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "任务不存在"})
        try:
            pdf = await ensure_pdf(job)
        except Exception as exc:
            return JSONResponse(status_code=400,
                                content={"ok": False, "error": f"文件转换失败：{exc}"})
        backfill_page_count(job, pdf)
        if job.page_count and (page < 0 or page >= job.page_count):
            return JSONResponse(status_code=400, content={"ok": False, "error": "页码超出范围"})
        try:
            png = render_page_png(pdf, page, dpi=110)
        except Exception as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": f"渲染失败：{exc}"})
        # 回传总页数，前端免一次 /api/jobs 刷新+二次预览（预览首屏曾因此双倍渲染）
        return Response(content=png, media_type="image/png",
                        headers={"X-Page-Count": str(job.page_count or 0)})

    @router.post("/api/jobs/{job_id}/print")
    async def print_job(job_id: int, body: PrintOptionsBody):
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "任务不存在"})
        err = _printable_status(job)
        if err:
            return {"ok": False, "error": err}
        if job.status == JobStatus.DONE:
            store.set_status(job.id, JobStatus.PENDING)
        return await do_print(job, body)

    @router.post("/api/jobs/print_batch")
    async def print_batch(body: PrintBatchBody):
        results = []
        for jid in body.ids:
            job = store.get(jid)
            if job is None:
                results.append({"id": jid, "ok": False, "error": "任务不存在"})
                continue
            err = _printable_status(job)
            if err:
                results.append({"id": jid, "ok": False, "error": err})
                continue
            if job.status == JobStatus.DONE:
                store.set_status(job.id, JobStatus.PENDING)
            res = await do_print(job, body.options)
            results.append({"id": jid, **res})
        return {"ok": all(r["ok"] for r in results), "results": results}

    @router.post("/api/jobs/{job_id}/discard")
    async def discard_job(job_id: int):
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "任务不存在"})
        store.set_status(job_id, JobStatus.DISCARDED)
        emit_job_update(job_id, JobStatus.DISCARDED)
        return {"ok": True}

    @router.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: int):
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "任务不存在"})
        store.set_status(job_id, JobStatus.PENDING, "")
        emit_job_update(job_id, JobStatus.PENDING)
        return {"ok": True}

    @router.post("/api/jobs/{job_id}/open")
    async def open_job(job_id: int):
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "任务不存在"})
        if job.category == "other":
            return JSONResponse(status_code=403,
                                content={"ok": False, "error": "该文件类型不支持打开（other 类禁止执行）"})
        try:
            target = _validate_job_path(cfg, job.file_path)
        except FileNotFoundError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except PermissionError as exc:
            return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
        try:
            os.startfile(str(target))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    @router.post("/api/jobs/{job_id}/open_dir")
    async def open_job_dir(job_id: int):
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "任务不存在"})
        # other 类允许 open_dir：explorer /select 只定位不执行，是 spec §5.2 标红文件的
        # 人工处理入口（/open 才真正执行文件，仍对 other 类 403）。路径越界防护不变。
        try:
            target = _validate_job_path(cfg, job.file_path)
        except FileNotFoundError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except PermissionError as exc:
            return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
        try:
            subprocess.Popen(["explorer", f"/select,{target}"])
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    @router.post("/api/service/finish")
    async def service_finish():
        return await sm.finish_service()

    @router.websocket("/ws/admin")
    async def ws_admin(ws: WebSocket):
        # 回环校验必须在端点内（HTTP 中间件不拦 websocket scope）——LAN 设备连 /ws/admin
        # 会持续收到顾客文件名/备注/二维码广播，属 PII 泄漏面。
        if not await _ws_loopback_guard(ws):
            return
        await ws.accept()
        ctx.clients.add(ws)
        try:
            await ws.send_text(
                json.dumps({"type": "session", **sm.screen_state()}, ensure_ascii=False))
            # device 快照紧随 session：管理页打开即有电池 + 小屏连接状态
            # （未上报时 battery 为 null、screen.last_seen_at 为 null）
            await ws.send_text(
                json.dumps({"type": "device", **ctx.device_payload()},
                           ensure_ascii=False))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ctx.clients.discard(ws)

    return router
