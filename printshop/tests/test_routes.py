from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from printshop.main import create_app
from printshop.config import Config
from printshop.jobs import Job, JobStatus
from printshop.session_manager import SessionState


def _job(id=1, status=JobStatus.PENDING, page_count=0, path="inbox/a1.pdf"):
    return Job(
        id=id,
        msg_id=f"m{id}",
        file_name=f"a{id}.pdf",
        file_path=path,
        category="pdf",
        size_bytes=10,
        page_count=page_count,
        status=status,
        options_json="",
        error="",
        created_at="2026-08-20 00:00:00",
        printed_at=None,
    )


def _app(tmp_path=None, state=SessionState.CONNECTED):
    cfg = Config()
    if tmp_path:
        cfg.inbox_dir = tmp_path / "inbox"
        cfg.work_dir = tmp_path / "work"
        cfg.db_path = tmp_path / "t.db"
    store = MagicMock()
    wx = AsyncMock()
    wx.get_qr = AsyncMock(return_value=(b"J", "https://login.weixin.qq.com/l/q"))
    app = create_app(cfg=cfg, store=store, wx=wx)
    app.state.ctx.sm.state = state
    return app, app.state.ctx


def _write_src(tmp_path) -> Path:
    src_dir = tmp_path / "inbox"
    src_dir.mkdir(parents=True, exist_ok=True)
    p = src_dir / "a1.pdf"
    p.write_bytes(b"%PDF-1.4")
    return p


def _file_job(tmp_path, **kw):
    kw.setdefault("path", str(tmp_path / "inbox" / "a1.pdf"))
    return _job(**kw)


def _norm_path(tmp_path) -> Path:
    p = tmp_path / "work" / "norm.pdf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4")
    return p


def test_screen_state_contract():
    app = create_app()
    r = TestClient(app).get("/api/screen/state")
    assert r.status_code == 200
    body = r.json()
    for k in ("state", "display", "qr_content", "file_count", "message"):
        assert k in body
    assert body["display"] in ("on", "off")


def test_screen_state_mirrors_session(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.sm.display = True
    ctx.sm.file_count = 3
    ctx.sm.qr_content = "https://login.weixin.qq.com/l/x"
    r = TestClient(app).get("/api/screen/state")
    body = r.json()
    assert body["state"] == SessionState.CONNECTED
    assert body["display"] == "on"
    assert body["file_count"] == 3
    assert body["qr_content"] == "https://login.weixin.qq.com/l/x"


def test_printers():
    app, _ = _app()
    with patch("printshop.routes.list_printers", return_value=["EPSON L6370"]):
        r = TestClient(app).get("/api/printers")
    assert r.json() == {"printers": ["EPSON L6370"]}


def test_jobs_list(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.list_active.return_value = [_job(1), _job(2)]
    r = TestClient(app).get("/api/jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert [j["id"] for j in jobs] == [1, 2]
    assert jobs[0]["status"] == JobStatus.PENDING


def test_print_success(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path, page_count=2)
    _write_src(tmp_path)
    norm = _norm_path(tmp_path)
    with patch("printshop.routes.normalize_to_pdf", return_value=norm) as nz, \
         patch("printshop.routes.print_pdf", return_value=(True, "")) as pp:
        r = TestClient(app).post("/api/jobs/1/print", json={"printer": "EPSON"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "error": ""}
    nz.assert_called_once()
    pp.assert_called_once()
    opts = pp.call_args[0][2]
    assert opts.printer == "EPSON" and opts.copies == 1
    ctx.store.set_status.assert_called_once()
    assert ctx.store.set_status.call_args[0][1] == JobStatus.DONE
    ctx.store.mark_printed.assert_called_once_with(1)
    ctx.store.set_page_count.assert_not_called()
    assert ctx.sm.state == SessionState.CONNECTED


def test_print_failure(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path)
    _write_src(tmp_path)
    with patch("printshop.routes.normalize_to_pdf", return_value=_norm_path(tmp_path)), \
         patch("printshop.routes.print_pdf", return_value=(False, "打印机不存在")):
        r = TestClient(app).post("/api/jobs/1/print", json={"printer": "EPSON"})
    assert r.json() == {"ok": False, "error": "打印机不存在"}
    ctx.store.set_status.assert_called_once()
    assert ctx.store.set_status.call_args[0][1] == JobStatus.FAILED
    assert ctx.store.set_status.call_args.kwargs["error"] == "打印机不存在"
    ctx.store.mark_printed.assert_not_called()
    assert ctx.sm.state == SessionState.CONNECTED


def test_print_normalize_error_returns_clean_json(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path)
    _write_src(tmp_path)
    with patch("printshop.routes.normalize_to_pdf",
               side_effect=ValueError("不支持的文件类型: bad.exe")):
        r = TestClient(app).post("/api/jobs/1/print", json={"printer": "EPSON"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "不支持" in body["error"]
    ctx.store.set_status.assert_called_once()
    assert ctx.store.set_status.call_args[0][1] == JobStatus.FAILED
    ctx.store.mark_printed.assert_not_called()
    assert ctx.sm.state == SessionState.CONNECTED


def test_print_backfills_page_count(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path, page_count=0)
    _write_src(tmp_path)
    with patch("printshop.routes.normalize_to_pdf", return_value=_norm_path(tmp_path)), \
         patch("printshop.routes.get_page_count", return_value=5), \
         patch("printshop.routes.print_pdf", return_value=(True, "")):
        r = TestClient(app).post("/api/jobs/1/print", json={"printer": "EPSON"})
    assert r.json()["ok"] is True
    ctx.store.set_page_count.assert_called_once_with(1, 5)


def test_print_normalize_cached_across_calls(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path)
    _write_src(tmp_path)
    norm = _norm_path(tmp_path)
    with patch("printshop.routes.normalize_to_pdf", return_value=norm) as nz, \
         patch("printshop.routes.print_pdf", return_value=(True, "")):
        c = TestClient(app)
        assert c.post("/api/jobs/1/print", json={"printer": "EPSON"}).json()["ok"] is True
        assert c.post("/api/jobs/1/print", json={"printer": "EPSON"}).json()["ok"] is True
    nz.assert_called_once()


def test_print_missing_job_404(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = None
    r = TestClient(app).post("/api/jobs/99/print", json={"printer": "EPSON"})
    assert r.status_code == 404
    assert r.json() == {"ok": False, "error": "任务不存在"}


def test_print_already_done_rejected(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path, status=JobStatus.DONE)
    r = TestClient(app).post("/api/jobs/1/print", json={"printer": "EPSON"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "任务已结束，无法打印"}
    ctx.store.mark_printed.assert_not_called()


def test_print_batch(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.side_effect = [_file_job(tmp_path, id=1), _file_job(tmp_path, id=2)]
    _write_src(tmp_path)
    with patch("printshop.routes.normalize_to_pdf", return_value=_norm_path(tmp_path)), \
         patch("printshop.routes.print_pdf", return_value=(True, "")):
        r = TestClient(app).post("/api/jobs/print_batch",
                                 json={"ids": [1, 2], "options": {"printer": "EPSON"}})
    body = r.json()
    assert body["ok"] is True
    assert body["results"] == [
        {"id": 1, "ok": True, "error": ""},
        {"id": 2, "ok": True, "error": ""},
    ]
    assert ctx.store.mark_printed.call_count == 2


def test_print_batch_partial_failure(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.side_effect = [_file_job(tmp_path, id=1), _file_job(tmp_path, id=2)]
    _write_src(tmp_path)
    with patch("printshop.routes.normalize_to_pdf", return_value=_norm_path(tmp_path)), \
         patch("printshop.routes.print_pdf", side_effect=[(True, ""), (False, "打印机不存在")]):
        r = TestClient(app).post("/api/jobs/print_batch",
                                 json={"ids": [1, 2], "options": {"printer": "EPSON"}})
    body = r.json()
    assert body["ok"] is False
    assert body["results"][0]["ok"] is True
    assert body["results"][1] == {"id": 2, "ok": False, "error": "打印机不存在"}


def test_preview_returns_png_and_backfills_page_count(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path, page_count=0)
    _write_src(tmp_path)
    norm = _norm_path(tmp_path)
    with patch("printshop.routes.normalize_to_pdf", return_value=norm), \
         patch("printshop.routes.get_page_count", return_value=3), \
         patch("printshop.routes.render_page_png", return_value=b"PNGDATA") as rp:
        r = TestClient(app).get("/api/jobs/1/preview?page=0")
    assert r.status_code == 200
    assert r.content == b"PNGDATA"
    assert r.headers["content-type"] == "image/png"
    ctx.store.set_page_count.assert_called_once_with(1, 3)
    rp.assert_called_once()
    assert rp.call_args[0][0] == tmp_path / "work" / "job_1.pdf"


def test_preview_out_of_range(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path, page_count=2)
    _write_src(tmp_path)
    with patch("printshop.routes.normalize_to_pdf", return_value=_norm_path(tmp_path)), \
         patch("printshop.routes.get_page_count", return_value=2):
        r = TestClient(app).get("/api/jobs/1/preview?page=5")
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_preview_normalize_error_clean_json(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path)
    _write_src(tmp_path)
    with patch("printshop.routes.normalize_to_pdf",
               side_effect=FileNotFoundError("源文件不存在")):
        r = TestClient(app).get("/api/jobs/1/preview?page=0")
    assert r.status_code == 400
    assert "源文件不存在" in r.json()["error"]
    ctx.store.set_status.assert_not_called()


def test_discard_job(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path)
    r = TestClient(app).post("/api/jobs/1/discard")
    assert r.json() == {"ok": True}
    ctx.store.set_status.assert_called_once_with(1, JobStatus.DISCARDED)


def test_discard_missing_404(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = None
    r = TestClient(app).post("/api/jobs/1/discard")
    assert r.status_code == 404


def test_retry_job(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path, status=JobStatus.FAILED)
    r = TestClient(app).post("/api/jobs/1/retry")
    assert r.json() == {"ok": True}
    ctx.store.set_status.assert_called_once_with(1, JobStatus.PENDING, "")


def test_open_job(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path, path=str(tmp_path / "a1.pdf"))
    with patch("os.startfile") as sf:
        r = TestClient(app).post("/api/jobs/1/open")
    assert r.json() == {"ok": True}
    sf.assert_called_once_with(str(tmp_path / "a1.pdf"))


def test_open_dir(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path, path=str(tmp_path / "a1.pdf"))
    with patch("subprocess.Popen") as popen:
        r = TestClient(app).post("/api/jobs/1/open_dir")
    assert r.json() == {"ok": True}
    popen.assert_called_once()


def test_service_finish(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.sm.state = SessionState.CONNECTED
    r = TestClient(app).post("/api/service/finish")
    assert r.json() == {"ok": True}
    ctx.wx.logout.assert_awaited_once()
    ctx.store.archive_all.assert_called_once()
    assert ctx.sm.state == SessionState.IDLE


def test_screen_wake(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.sm.state = SessionState.IDLE
    r = TestClient(app).post("/api/screen/wake")
    assert r.json() == {"ok": True}
    ctx.wx.get_qr.assert_awaited_once()
    assert ctx.sm.display is True


def test_screen_page_served():
    app = create_app()
    r = TestClient(app).get("/screen")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_admin_page_served():
    app = create_app()
    r = TestClient(app).get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ws_admin_receives_initial_session(tmp_path):
    app, ctx = _app(tmp_path)
    with TestClient(app) as c:
        with c.websocket_connect("/ws/admin") as ws:
            msg = ws.receive_json()
    assert msg["type"] == "session"
    assert set(msg.keys()) >= {"type", "state", "display", "qr_content", "file_count", "message"}
    assert msg["display"] in ("on", "off")


def test_ws_admin_broadcasts_on_change(tmp_path):
    app, ctx = _app(tmp_path)
    with TestClient(app) as c:
        with c.websocket_connect("/ws/admin") as ws:
            ws.receive_json()  # 初始 session
            ctx.sm.display = True
            ctx.sm.on_change()
            msg = ws.receive_json()
    assert msg["type"] == "session"
    assert msg["display"] == "on"


def test_ws_admin_new_job_via_store_hook(tmp_path):
    app, ctx = _app(tmp_path)
    with TestClient(app) as c:
        with c.websocket_connect("/ws/admin") as ws:
            ws.receive_json()
            ctx.store.on_job_added(7)
            msg = ws.receive_json()
    assert msg == {"type": "new_job", "id": 7}


def test_print_broadcasts_job_update(tmp_path):
    app, ctx = _app(tmp_path)
    ctx.store.get.return_value = _file_job(tmp_path)
    _write_src(tmp_path)
    with TestClient(app) as c:
        with c.websocket_connect("/ws/admin") as ws:
            ws.receive_json()
            with patch("printshop.routes.normalize_to_pdf", return_value=_norm_path(tmp_path)), \
                 patch("printshop.routes.print_pdf", return_value=(True, "")):
                r = c.post("/api/jobs/1/print", json={"printer": "EPSON"})
            assert r.json()["ok"] is True
            msg = None
            for _ in range(10):
                m = ws.receive_json()
                if m["type"] == "job_update":
                    msg = m
                    break
    assert msg is not None
    assert msg["id"] == 1
    assert msg["status"] == JobStatus.DONE
