"""HTTP/WS 路由层：REST API + WebSocket 推送（对外接口契约见 task-6-brief 端点表）。

线程模型说明：JobStore 为长连接（check_same_thread=True），创建于事件循环线程。
所有路由 handler 均为 async def，运行在事件循环线程——内部同步调用 JobStore
（微秒级，可接受）。仅打印执行（subprocess，秒级）用 asyncio.to_thread，
且工作线程内不触碰 JobStore：先取数、打印、回事件循环后再 set_status。
"""
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

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


class PrintOptionsBody(BaseModel):
    printer: str
    color: bool = True
    copies: int = 1
    pages: str = ""
    duplex: str = "simplex"
    paper: str = "A4"


class PrintBatchBody(BaseModel):
    ids: list[int]
    options: PrintOptionsBody


class AppContext:
    """路由共享上下文：配置、数据层、会话状态机、WS 客户端集与广播。"""

    def __init__(self, cfg, store, wx, sm):
        self.cfg = cfg
        self.store = store
        self.wx = wx
        self.sm = sm
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()

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


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    cfg = ctx.cfg
    store = ctx.store
    sm = ctx.sm

    def emit_job_update(job_id: int, status: str) -> None:
        ctx.emit("job_update", id=job_id, status=status)

    async def ensure_pdf(job) -> Path:
        """normalize 产物按 job_id 缓存在 work_dir，已转换过的不重复转换。"""
        cached = cfg.work_dir / f"job_{job.id}.pdf"
        if cached.exists():
            return cached
        src = Path(job.file_path)
        if not src.exists():
            raise FileNotFoundError(f"源文件不存在：{job.file_name}")
        out = await asyncio.to_thread(normalize_to_pdf, src, cfg.work_dir, cfg)
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
            emit_job_update(job.id, JobStatus.FAILED)
            return {"ok": False, "error": f"文件转换失败：{exc}"}
        backfill_page_count(job, pdf)
        opts = PrintOptions(printer=body.printer, color=body.color, copies=body.copies,
                            pages=body.pages, duplex=body.duplex, paper=body.paper)
        prev = sm.state
        if prev == SessionState.CONNECTED:
            sm._transition(SessionState.PRINTING)
        ok, error = await asyncio.to_thread(print_pdf, cfg, pdf, opts)
        if prev == SessionState.CONNECTED:
            sm._transition(SessionState.CONNECTED)
        if ok:
            store.set_status(job.id, JobStatus.DONE,
                             options_json=json.dumps(body.model_dump(), ensure_ascii=False))
            store.mark_printed(job.id)
            emit_job_update(job.id, JobStatus.DONE)
        else:
            store.set_status(job.id, JobStatus.FAILED, error=error)
            emit_job_update(job.id, JobStatus.FAILED)
        return {"ok": ok, "error": "" if ok else error}

    @router.get("/api/screen/state")
    async def screen_state():
        return sm.screen_state()

    @router.post("/api/screen/wake")
    async def screen_wake():
        await sm.wake()
        return {"ok": True}

    @router.get("/api/printers")
    async def printers():
        return {"printers": list_printers()}

    @router.get("/api/jobs")
    async def jobs_list():
        return [_job_dict(j) for j in store.list_active()]

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
        return Response(content=png, media_type="image/png")

    @router.post("/api/jobs/{job_id}/print")
    async def print_job(job_id: int, body: PrintOptionsBody):
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "任务不存在"})
        if job.status not in JobStatus._ACTIVE:
            return {"ok": False, "error": "任务已结束，无法打印"}
        return await do_print(job, body)

    @router.post("/api/jobs/print_batch")
    async def print_batch(body: PrintBatchBody):
        results = []
        for jid in body.ids:
            job = store.get(jid)
            if job is None:
                results.append({"id": jid, "ok": False, "error": "任务不存在"})
                continue
            if job.status not in JobStatus._ACTIVE:
                results.append({"id": jid, "ok": False, "error": "任务已结束，无法打印"})
                continue
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
        await ws.accept()
        ctx.clients.add(ws)
        try:
            await ws.send_text(
                json.dumps({"type": "session", **sm.screen_state()}, ensure_ascii=False))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ctx.clients.discard(ws)

    return router
