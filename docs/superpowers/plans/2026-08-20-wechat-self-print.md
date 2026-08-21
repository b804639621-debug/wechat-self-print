# 微信扫码自助打印系统 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现"顾客微信扫码 → 文件传输助手发文件 → 管理员电脑弹界面选打印机/彩色灰度/预览 → 确认打印 → 登出清理"的店内自助打印系统。

**Architecture:** 单进程 Python 主控（FastAPI + 微信协议客户端 + 打印引擎 + 系统托盘），ESP32-C6 AMOLED 一体板轮询 `/api/screen/state` 显示二维码；管理员浏览器打开 `/admin` 操作。微信协议代码 vendor 自开源项目 wx-filehelper-api（MIT License）。

**Tech Stack:** Python 3.11+ / FastAPI / httpx / SQLite / PyMuPDF / Pillow / pywin32 / OpenCV / pystray / **XPS+PrintTicket（.NET 互操作：pythonnet 优先，PowerShell 助手兜底）** / SumatraPDF（降级为备用/调试）/ Office COM 优先 + LibreOffice 兜底；固件：Arduino (ESP32-C6) / WiFiManager / ricmoo-QRCode / ArduinoJson / XPowersLib

**Spec:** `docs/superpowers/specs/2026-08-20-wechat-self-print-design.md`（执行时先读此文档）

## Global Constraints

- 平台：管理员电脑 Windows 10/11 x64；Python ≥ 3.11；所有路径相对项目根 `E:\kaifa\自助打印`
- 依赖固定：fastapi, uvicorn[standard], httpx, pydantic, pillow, pymupdf, pywin32, opencv-python-headless, pystray, pytest, pytest-asyncio；Task 7B 起增加 pythonnet（⚠️ Python 3.14 兼容性未验证，失败则走 PowerShell 助手方案）
- **打印路径定案（m1-verification §7-9 实测定案，最高优先级约束）**：normalize → PyMuPDF 原页渲染（render_dpi 默认 300）→ XPS 打包 → `XpsDocumentWriter.Write` + PrintTicket。三条铁律：①**勿用 PyMuPDF `show_pdf_page` 做页面归一化**（丢印章，PoC 实测）②XPS `ImageBrush Viewbox` 填位图自然尺寸（像素×96/dpi）③**禁用 `PrintQueue.AddJob`**（柯美 PCL 队列挂起，统一用 XpsWriter）
- SumatraPDF 便携版放 `tools/SumatraPDF.exe`（降级为备用/调试，单面场景可用）；Office 转换双轨制：本机 Office COM（ExportAsFixedFormat，已实测）优先，无 Office 用 LibreOffice，均不可用则 Office 文件标红提示不崩溃
- 微信协议代码 vendor 自 `https://github.com/CJackHwang/wx-filehelper-api`（MIT），复制 `direct_bot.py` 到 `printshop/vendor/direct_bot.py`，文件头保留出处与 LICENSE 说明
- **隐私红线**：`finish_service()` 必须 登出微信会话 + 删除 inbox 顾客文件 + 归档任务记录，缺一不可
- 单队列：同一时刻仅服务一位顾客（协议硬约束）
- 单文件 ≤25MB（微信限制，界面文案预先告知）
- 测试约定：纯逻辑（状态机/命令组装/格式分类/数据层）必须 pytest 自动化；微信真实链路、打印实机、固件为人工验证 checklist
- git：Task 1 初始化仓库；每个任务结束 commit，commit message 格式 `type: 中文简述`

## 文件结构

```
E:\kaifa\自助打印\
├── printshop/                  # 主控程序
│   ├── config.py               # 配置（端口/路径/超时/打印机默认项记忆）
│   ├── main.py                 # FastAPI 入口 + 托盘线程 + 后台任务启动
│   ├── wechat_bot.py           # 协议适配层（包装 vendor/direct_bot.py）
│   ├── vendor/direct_bot.py    # 微信协议实现（vendor，勿改逻辑）
│   ├── session_manager.py      # 会话状态机 + 亮屏/息屏状态机
│   ├── jobs.py                 # print_jobs/print_history SQLite 数据层
│   ├── print_engine.py         # 格式分类/归一化(双轨)/预览渲染（Task 7B 起打印执行移交 xps_print.py）
│   ├── xps_print.py            # XPS 打包 + PrintTicket 提交（Task 7B 新建，以 tools/poc/file_to_xps.py 为起点）
│   ├── routes.py               # REST + WebSocket 路由
│   ├── tray.py                 # pystray 系统托盘
│   ├── static/admin.html       # 管理界面（单文件，原生 JS）
│   ├── static/screen.html      # 开发期辅助屏幕页（调试用）
│   └── tests/                  # pytest
├── firmware/print_screen/        # ESP32-C6 固件
│   │   ├── print_screen.ino      # 主程序（配网/轮询/二维码/触摸唤醒）
│   │   └── lcd_touch.cpp/.h, user_config.h  # 微雪例程 09_LVGL_V9_Test 复制（CO5300+CST9220+引脚）
├── tools/SumatraPDF.exe        # 便携版打印器（备用/调试）
├── tools/poc/                  # M1 PoC 沉淀：file_to_xps.py / make_xps.py（Task 7B 起点，届时入库）
├── assets/signage.html         # 柜台立牌（打印成 A5）
└── inbox/                      # 顾客文件收件目录（运行时生成，gitignore）
```

---

### Task 0: M1 链路验证（零编码，先验证最高风险链路）

**Files:**
- Create: `docs/m1-verification.md`（验证记录）

**Interfaces:** 无代码产出。产出=三项验证结论，全部通过才进入 Task 1。

- [ ] **Step 1: 环境准备**

安装 Python 3.11+；克隆底座并安装依赖：

```powershell
cd E:\kaifa\自助打印
git clone https://github.com/CJackHwang/wx-filehelper-api vendor-ref\wx-filehelper-api
cd vendor-ref\wx-filehelper-api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

- [ ] **Step 2: 启动底座并验证二维码接口**

```powershell
python main.py
# 另开终端：
curl http://127.0.0.1:8000/qr -o qr.png
```

预期：`qr.png` 是有效二维码图片。

- [ ] **Step 3: 真实微信扫码 + 文件收发验证（人工）**

手机微信扫 `qr.png` → 手机上点【登录】→ `curl http://127.0.0.1:8000/login/status` 显示已登录 → 手机微信"文件传输助手"发送一个 PDF 和一张图片 → 检查 `downloads/` 目录出现这两个文件。**记录：登录是否需要额外验证、文件下载是否完整可打开。**

- [ ] **Step 4: 验证二维码内容格式（供 ESP32 用）**

```powershell
pip install opencv-python-headless
python -c "import cv2; d=cv2.QRCodeDetector(); img=cv2.imread('qr.png'); txt,_,_=d.detectAndDecode(img); print(repr(txt))"
```

预期输出形如 `https://login.weixin.qq.com/l/xxxx` 的 URL。**把实际格式记录到 `docs/m1-verification.md`**——若确为此格式，Task 2 的 `get_qr()` 用 uuid 直接拼接；否则用 OpenCV 解码 PNG。

- [ ] **Step 5: SumatraPDF 打印验证**

下载 SumatraPDF 便携版放 `tools/SumatraPDF.exe`，用虚拟打印机无纸验证：

```powershell
tools\SumatraPDF.exe -list-printers
tools\SumatraPDF.exe -print-to "Microsoft Print to PDF" -print-settings "monochrome,fit,paper=A4" -silent <下载的测试PDF>
echo $LASTEXITCODE   # 预期 0，弹出"另存为"对话框即证明打印管线通
```

- [ ] **Step 6: 记录结论并 commit**

`docs/m1-verification.md` 记录：登录可用性、文件下载完整性、二维码内容格式、打印退出码。任何一项失败 → 停止，回到调研报告的降级路线（浏览器自动化）。

---

### Task 1: 项目脚手架

**Files:**
- Create: `printshop/config.py`, `printshop/requirements.txt`, `printshop/tests/__init__.py`, `.gitignore`
- Test: `printshop/tests/test_config.py`

**Interfaces:**
- Produces: `Config` dataclass，字段被后续所有任务引用：

| 字段 | 类型 | 默认 | 用途 |
|---|---|---|---|
| `host` | str | `"0.0.0.0"` | 绑定地址（小屏需局域网访问） |
| `port` | int | `8000` | HTTP 端口 |
| `inbox_dir` | Path | `inbox/` | 收件目录 |
| `work_dir` | Path | `work/` | 转换/预览缓存 |
| `db_path` | Path | `printshop.db` | SQLite |
| `sumatra_path` | Path | `tools/SumatraPDF.exe` | 打印器 |
| `soffice_path` | str | `r"C:\Program Files\LibreOffice\program\soffice.exe"` | Office 转换 |
| `idle_sleep_seconds` | int | `180` | IDLE 息屏倒计时 |
| `qr_expire_seconds` | int | `240` | 二维码有效期 |
| `no_file_timeout` | int | `300` | 连接后无文件提醒 |
| `poll_interval` | float | `1.5` | 消息轮询间隔秒 |

- [ ] **Step 1: 初始化 git 仓库与目录**

```powershell
cd E:\kaifa\自助打印
git init
```

`.gitignore` 内容：

```
.venv/
__pycache__/
*.pyc
inbox/
work/
*.db
vendor-ref/
node_modules/
```

- [ ] **Step 2: 写失败测试**

`printshop/tests/test_config.py`：

```python
from printshop.config import Config

def test_config_defaults():
    c = Config()
    assert c.port == 8000
    assert c.idle_sleep_seconds == 180
    assert c.inbox_dir.name == "inbox"
```

- [ ] **Step 3: 运行确认失败** — `pytest printshop/tests/test_config.py -v`，预期 ModuleNotFoundError

- [ ] **Step 4: 实现 `printshop/config.py`**

```python
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8000
    inbox_dir: Path = field(default_factory=lambda: Path("inbox"))
    work_dir: Path = field(default_factory=lambda: Path("work"))
    db_path: Path = field(default_factory=lambda: Path("printshop.db"))
    sumatra_path: Path = field(default_factory=lambda: Path("tools/SumatraPDF.exe"))
    soffice_path: str = r"C:\Program Files\LibreOffice\program\soffice.exe"
    idle_sleep_seconds: int = 180
    qr_expire_seconds: int = 240
    no_file_timeout: int = 300
    poll_interval: float = 1.5

    def ensure_dirs(self) -> None:
        self.inbox_dir.mkdir(exist_ok=True)
        self.work_dir.mkdir(exist_ok=True)
```

`requirements.txt`：`fastapi uvicorn[standard] httpx pydantic pillow pymupdf pywin32 opencv-python-headless pystray pytest pytest-asyncio`

- [ ] **Step 5: 运行确认通过** — `pip install -r printshop/requirements.txt` 后 `pytest printshop/tests/test_config.py -v`，预期 PASS

- [ ] **Step 6: Commit** — `git add -A; git commit -m "chore: 项目脚手架与配置模块"`

---

### Task 2: 微信协议适配层 `wechat_bot.py`

**Files:**
- Create: `printshop/vendor/direct_bot.py`（从 `vendor-ref/wx-filehelper-api/direct_bot.py` 原样复制，文件头加注释 `# vendored from https://github.com/CJackHwang/wx-filehelper-api (MIT License)`）
- Create: `printshop/wechat_bot.py`
- Test: `printshop/tests/test_wechat_bot.py`

**Interfaces:**
- Consumes: vendor `WeChatHelperBot` 的方法 `_jslogin_get_uuid()`, `_poll_login_once()`, `_complete_login()`, `get_latest_messages()`, `download_message_content(msg_id)`, `_load_session()/save_session()`, `_has_auth()`
- Produces（后续任务只依赖本层，不直接碰 vendor）：

```python
class WeChatClient:
    async def get_qr(self) -> tuple[bytes, str]          # (PNG字节, 二维码内容字符串)
    async def poll_login(self) -> str                     # "wait_scan"|"scanned"|"confirmed"|"expired"
    async def fetch_messages(self) -> list[dict]          # [{id,type("text"|"image"|"file"),text,file_name,is_mine}]
    async def download(self, msg_id: str) -> bytes
    async def logout(self) -> None
    def is_authed(self) -> bool
    async def restore_session(self) -> bool               # 从 state.json 恢复，成功 True
```

- [ ] **Step 1: 复制 vendor 文件并确认可导入**

```powershell
New-Item -ItemType Directory printshop\vendor -Force
Copy-Item vendor-ref\wx-filehelper-api\direct_bot.py printshop\vendor\direct_bot.py
New-Item -ItemType File printshop\vendor\__init__.py
```

阅读 vendor 代码确认上述 Consumes 方法签名（重点是 `get_latest_messages` 返回结构和 `download_message_content` 参数）。若方法名/签名与本计划不一致，**以 vendor 实际代码为准调整适配层**。

- [ ] **Step 2: 写失败测试** `printshop/tests/test_wechat_bot.py`（全部 mock vendor 层，不碰网络）：

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from printshop.wechat_bot import WeChatClient

@pytest.mark.asyncio
async def test_get_qr_returns_png_and_content():
    c = WeChatClient(entry_host="szfilehelper.weixin.qq.com")
    c._bot = MagicMock()
    c._bot._jslogin_get_uuid = AsyncMock(return_value="abc123")
    with patch("printshop.wechat_bot.httpx.AsyncClient") as mc:
        resp = MagicMock(); resp.content = b"PNG_BYTES"; resp.raise_for_status = MagicMock()
        mc.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
        png, content = await c.get_qr()
    assert png == b"PNG_BYTES"
    assert content == "https://login.weixin.qq.com/l/abc123"  # 以 Task0 Step4 实测格式为准

@pytest.mark.asyncio
async def test_poll_login_maps_codes():
    c = WeChatClient(entry_host="szfilehelper.weixin.qq.com")
    c._bot = MagicMock(); c._bot.uuid = "abc"
    for code, expect in [("408", "wait_scan"), ("201", "scanned"), ("200", "confirmed"), ("400", "expired")]:
        c._bot._poll_login_once = AsyncMock(return_value=code)
        assert await c.poll_login() == expect
```

- [ ] **Step 3: 运行确认失败** — `pytest printshop/tests/test_wechat_bot.py -v`

- [ ] **Step 4: 实现 `printshop/wechat_bot.py`**

```python
"""微信文件传输助手网页版协议适配层。包装 vendor/direct_bot.py，对上层屏蔽细节。"""
import httpx
from printshop.vendor.direct_bot import WeChatHelperBot

QR_CONTENT_TEMPLATE = "https://login.weixin.qq.com/l/{uuid}"  # 以 Task0 Step4 实测为准

class WeChatClient:
    def __init__(self, entry_host: str = "szfilehelper.weixin.qq.com"):
        self.entry_host = entry_host
        self._bot = WeChatHelperBot(entry_host=entry_host)

    async def get_qr(self) -> tuple[bytes, str]:
        uuid = await self._bot._jslogin_get_uuid()
        self._bot.uuid = uuid
        async with httpx.AsyncClient(timeout=15) as cli:
            resp = await cli.get(f"https://login.weixin.qq.com/qrcode/{uuid}")
            resp.raise_for_status()
        return resp.content, QR_CONTENT_TEMPLATE.format(uuid=uuid)

    async def poll_login(self) -> str:
        code = await self._bot._poll_login_once()
        return {"408": "wait_scan", "201": "scanned",
                "200": "confirmed", "400": "expired", "500": "expired", "0": "expired"}.get(code, "wait_scan")

    async def complete_login(self) -> None:
        await self._bot._complete_login()

    async def fetch_messages(self) -> list[dict]:
        return await self._bot.get_latest_messages()

    async def download(self, msg_id: str) -> bytes:
        return await self._bot.download_message_content(msg_id)

    async def logout(self) -> None:
        await self._bot.logout()  # 若 vendor 无 logout 方法：POST webwxlogout 后清空凭证

    def is_authed(self) -> bool:
        return self._bot._has_auth()

    async def restore_session(self) -> bool:
        ok = self._bot._load_session()
        return bool(ok and self._bot._has_auth())
```

注意 `_poll_login_once` 若 vendor 返回的是状态字符串而非 code，适配层直接透传映射，**以 vendor 实际返回为准**。

- [ ] **Step 5: 运行确认通过** — `pytest printshop/tests/test_wechat_bot.py -v`

- [ ] **Step 6: Commit** — `git commit -m "feat: 微信协议适配层"`

---

### Task 3: 打印任务数据层 `jobs.py`

**Files:**
- Create: `printshop/jobs.py`
- Test: `printshop/tests/test_jobs.py`

**Interfaces:**
- Produces:

```python
class JobStatus:  # str 常量
    PENDING = "pending"; PRINTING = "printing"; DONE = "done"
    FAILED = "failed"; DISCARDED = "discarded"

@dataclass
class Job:
    id: int; msg_id: str; file_name: str; file_path: str
    category: str            # "pdf"|"image"|"office"|"other"
    size_bytes: int; page_count: int; status: str
    options_json: str; error: str; created_at: str; printed_at: str | None

class JobStore:
    def __init__(self, db_path: Path): ...
    def add(self, msg_id, file_name, file_path, category, size_bytes, page_count) -> int
    def list_active(self) -> list[Job]          # 状态 pending/printing/failed 的未归档任务
    def get(self, job_id: int) -> Job | None
    def set_status(self, job_id: int, status: str, error: str = "", options_json: str = "") -> None
    def archive_all(self) -> int                 # 把当前所有任务移入 print_history，返回条数（收尾用）
```

- [ ] **Step 1: 写失败测试** `printshop/tests/test_jobs.py`：

```python
from pathlib import Path
from printshop.jobs import JobStore, JobStatus

def test_add_list_archive(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    jid = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 3)
    assert jid >= 1
    jobs = s.list_active()
    assert len(jobs) == 1 and jobs[0].file_name == "a.pdf" and jobs[0].status == JobStatus.PENDING
    s.set_status(jid, JobStatus.DONE, options_json='{"printer":"P1"}')
    assert s.get(jid).status == JobStatus.DONE
    assert s.archive_all() == 1
    assert s.list_active() == []
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现**（SQLite，两表 `print_jobs`/`print_history` 同构，`archive_all` 用 `INSERT INTO print_history SELECT ...` + `DELETE`）→ **Step 4: 运行确认通过** → **Step 5: Commit** `feat: 打印任务数据层`

---

### Task 4: 打印引擎 `print_engine.py`

> ⚠️ **状态注记（2026-08-21 修订）**：本任务已按原方案完成实施（SumatraPDF 打印路径 + show_pdf_page A4 归一化）。随后 Task 0 扩展排查实测定案：**SumatraPDF 静默打印的 duplex/paper 被 Epson 驱动丢弃、`show_pdf_page` 归一化丢印章**（m1-verification §7-9）。打印执行与页面归一化由 **Task 7B** 返工替换；本任务保留有效的部分：`PrintOptions`/`list_printers`/`classify_file`/`get_page_count`/`render_page_png`/格式转换骨架。以下原文留作历史记录。

**Files:**
- Create: `printshop/print_engine.py`
- Test: `printshop/tests/test_print_engine.py`

**Interfaces:**
- Consumes: `Config`（Task 1）
- Produces：

```python
@dataclass
class PrintOptions:
    printer: str; color: bool = True          # False=灰度
    copies: int = 1; pages: str = ""          # ""=全部，或 "1-3,5"
    duplex: str = "simplex"                   # simplex|duplexlong|duplexshort
    paper: str = "A4"

def list_printers() -> list[str]
def classify_file(path: Path) -> str          # "pdf"|"image"|"office"|"other"
def normalize_to_pdf(src: Path, work_dir: Path, cfg: Config) -> Path   # other 抛 ValueError
def get_page_count(pdf: Path) -> int          # PyMuPDF
def render_page_png(pdf: Path, page: int, dpi: int = 110) -> bytes
def build_print_cmd(cfg: Config, pdf: Path, o: PrintOptions) -> list[str]
def print_pdf(cfg: Config, pdf: Path, o: PrintOptions) -> tuple[bool, str]
```

- [ ] **Step 1: 写失败测试** `printshop/tests/test_print_engine.py`：

```python
from pathlib import Path
from printshop.config import Config
from printshop.print_engine import PrintOptions, build_print_cmd, classify_file

def test_classify():
    assert classify_file(Path("a.pdf")) == "pdf"
    assert classify_file(Path("a.JPG")) == "image"
    assert classify_file(Path("a.docx")) == "office"
    assert classify_file(Path("a.zip")) == "other"

def test_build_cmd_mono_duplex():
    cfg = Config()
    cmd = build_print_cmd(cfg, Path("x.pdf"),
                          PrintOptions(printer="HP 彩机", color=False, copies=2,
                                       pages="1-3", duplex="duplexlong", paper="A4"))
    s = " ".join(cmd)
    assert "-print-to" in cmd and "HP 彩机" in cmd
    assert "monochrome" in s and "2x" in s and "1-3" in s and "duplexlong" in s and "paper=A4" in s
    assert "-silent" in cmd

def test_build_cmd_color_simplex_allpages():
    cfg = Config()
    cmd = build_print_cmd(cfg, Path("x.pdf"), PrintOptions(printer="P"))
    s = " ".join(cmd)
    assert "color" in s and "monochrome" not in s and "simplex" in s
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现**，关键实现点：

```python
EXIT_CODES = {0: "", 2: "文件无法打开或格式不支持", 3: "文档禁止打印",
              4: "打印机不存在", 5: "打印机驱动/设备失败", 6: "打印被策略禁止"}

def build_print_cmd(cfg, pdf, o: PrintOptions) -> list[str]:
    settings = ["color" if o.color else "monochrome", f"{o.copies}x",
                o.duplex, f"paper={o.paper}", "fit"]
    if o.pages.strip():
        settings.insert(0, o.pages.strip())
    return [str(cfg.sumatra_path), "-print-to", o.printer,
            "-print-settings", ",".join(settings), "-silent", str(pdf)]

def print_pdf(cfg, pdf, o) -> tuple[bool, str]:
    import subprocess
    r = subprocess.run(build_print_cmd(cfg, pdf, o), capture_output=True, timeout=300)
    return (r.returncode == 0, EXIT_CODES.get(r.returncode, f"未知错误码 {r.returncode}"))
```

`list_printers` 用 `win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)` 取 `pPrinterName`；`normalize_to_pdf`：pdf 直通；image 用 Pillow `Image.open(src).convert("RGB").save(dst, "PDF")`；office 调 `subprocess.run([cfg.soffice_path, "--headless", "--convert-to", "pdf", "--outdir", str(work_dir), str(src)], timeout=180)` 并检查产物存在，soffice 不存在时抛 `FileNotFoundError("未安装 LibreOffice")`；`render_page_png` 用 `fitz.open(pdf)[page].get_pixmap(dpi=dpi).tobytes("png")`。

- [ ] **Step 4: 运行确认通过** → **Step 5: 无纸集成验证（人工）**：对真实样本（1 PDF、1 jpg、1 docx）跑 `normalize_to_pdf` + `print_pdf` 到 "Microsoft Print to PDF"，全部退出码 0 → **Step 6: Commit** `feat: 打印引擎`

---

### Task 5: 会话状态机 `session_manager.py`

**Files:**
- Create: `printshop/session_manager.py`
- Test: `printshop/tests/test_session_manager.py`

**Interfaces:**
- Consumes: `WeChatClient`（Task 2）、`JobStore`（Task 3）、`Config`（Task 1）
- Produces：

```python
class SessionState:  # str 常量
    IDLE = "IDLE"; SCANNED = "SCANNED"; CONNECTED = "CONNECTED"
    RECEIVING = "RECEIVING"; PRINTING = "PRINTING"

class SessionManager:
    state: str; display: bool; file_count: int
    qr_png: bytes | None; qr_content: str | None
    on_change: Callable[[], None] | None      # 状态变化回调（routes 层挂 WS 广播）

    async def wake(self) -> None               # 托盘调用：display=True + 强制刷新二维码
    async def run_loop(self) -> None           # 主循环：登录轮询/消息轮询/息屏计时（1.5s 周期）
    def screen_state(self) -> dict             # {"state","display","qr_content","file_count","message"}
    async def note_text(self) -> str           # 顾客最近文字备注（供 admin 横幅）
    async def finish_service(self) -> None     # 登出+删文件+archive_all+回IDLE（display=False）
```

状态迁移规则：
- `wake()`: 仅 IDLE 有效 → `display=True` + 调 `get_qr()` 强制换新码
- run_loop 每周期：`display=False` 时仅维持凭证心跳；`display=True` 且 IDLE → `poll_login()`：`scanned`→SCANNED；`confirmed`→`complete_login()`→CONNECTED；`expired`→重新 `get_qr()`
- CONNECTED：`fetch_messages()` → 有文件消息：下载→`JobStore.add`→`file_count+=1`→短暂 RECEIVING 后回 CONNECTED；文本消息记录为备注
- IDLE 且 `display=True` 持续 ≥ `cfg.idle_sleep_seconds` 无事件 → `display=False`；SCANNED 超 120s → 回 IDLE 并换新码
- CONNECTED 超 `cfg.no_file_timeout` 无文件 → 仅置提醒标志（不自动登出）
- 任何 `PRINTING` 由 routes 层打印时设置，完成回 CONNECTED

- [ ] **Step 1: 写失败测试** `printshop/tests/test_session_manager.py`（mock WeChatClient/JobStore，直接调用内部 `_transition` 与计时逻辑，不跑真循环）：

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from printshop.session_manager import SessionManager, SessionState
from printshop.config import Config

@pytest.mark.asyncio
async def test_wake_refreshes_qr():
    sm = SessionManager(Config(), MagicMock(), MagicMock())
    sm._wx.get_qr = AsyncMock(return_value=(b"PNG", "https://login.weixin.qq.com/l/x"))
    await sm.wake()
    assert sm.display is True and sm.qr_content.endswith("/l/x") and sm.state == SessionState.IDLE

@pytest.mark.asyncio
async def test_idle_sleep_after_timeout():
    cfg = Config(); cfg.idle_sleep_seconds = 180
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm.display = True
    sm._last_event_ts -= 200          # 模拟 200 秒无事件
    sm._check_idle_sleep()
    assert sm.display is False

@pytest.mark.asyncio
async def test_finish_service_cleans_up(tmp_path):
    cfg = Config(); cfg.inbox_dir = tmp_path
    f = tmp_path / "a.pdf"; f.write_bytes(b"x")
    sm = SessionManager(cfg, MagicMock(), MagicMock())
    sm._wx.logout = AsyncMock()
    sm.state = SessionState.CONNECTED; sm.file_count = 1
    await sm.finish_service()
    sm._wx.logout.assert_awaited_once()
    assert not f.exists() and sm.state == SessionState.IDLE and sm.display is False and sm.file_count == 0
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现** → **Step 4: 运行确认通过** → **Step 5: Commit** `feat: 会话状态机`

---

### Task 6: HTTP/WS 路由 `routes.py` + `main.py`

**Files:**
- Create: `printshop/routes.py`, `printshop/main.py`, `printshop/static/screen.html`
- Test: `printshop/tests/test_routes.py`

**Interfaces:**
- Consumes: 全部前置模块
- Produces（前端/固件依赖的接口契约）：

| 端点 | 说明 |
|---|---|
| `GET /api/screen/state` | → `SessionManager.screen_state()`（固件轮询） |
| `GET /screen` | 开发期屏幕页（静态页，WS 订阅同一状态） |
| `GET /admin` | 管理页 |
| `WS /ws/admin` | 推送 `{"type": "session"\|"new_job"\|"job_update"\|"note", ...}` |
| `GET /api/jobs` | → `JobStore.list_active()` |
| `GET /api/jobs/{id}/preview?page=0` | → PNG（内部按需 normalize+缓存） |
| `POST /api/jobs/{id}/print` | body=PrintOptions 字段 → 打印，回 `{ok, error}` |
| `POST /api/jobs/print_batch` | body `{ids: [int], options: {...}}` 逐个打印 |
| `POST /api/jobs/{id}/discard` / `POST /api/jobs/{id}/retry` | 作废/重打 |
| `POST /api/jobs/{id}/open` | `os.startfile(job.file_path)` |
| `POST /api/jobs/{id}/open_dir` | `explorer /select,"path"` |
| `POST /api/service/finish` | `finish_service()` |
| `POST /api/screen/wake` | `wake()`（托盘与固件触摸唤醒共用） |
| `GET /api/printers` | `list_printers()` |

- [ ] **Step 1: 写失败测试** `printshop/tests/test_routes.py`（FastAPI TestClient/httpx ASGITransport，mock SessionManager）：

```python
from fastapi.testclient import TestClient
from printshop.main import create_app

def test_screen_state_contract():
    app = create_app()
    r = TestClient(app).get("/api/screen/state")
    assert r.status_code == 200
    body = r.json()
    for k in ("state", "display", "qr_content", "file_count", "message"):
        assert k in body
    assert body["display"] in ("on", "off")
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现**（`create_app()` 工厂组装 Config/JobStore/WeChatClient/SessionManager，uvicorn 启动 + 后台 asyncio task 跑 `run_loop()`；`screen.html` 为最小 WS 状态显示页）→ **Step 4: 运行确认通过 + 手工 `curl` 各端点** → **Step 5: Commit** `feat: HTTP/WS 路由与服务入口`

---

### Task 7: 管理界面 `static/admin.html`

**Files:**
- Create: `printshop/static/admin.html`
- Test: 无自动化，人工 checklist

**Interfaces:**
- Consumes: Task 6 全部 API 与 WS 消息格式

- [ ] **Step 1: 实现单文件页面**（原生 HTML/JS/CSS，无构建）：

布局按 spec §6.2：顶部状态条（会话状态、顾客备注横幅、声音开关、`POST /api/service/finish` 按钮）；左侧文件列表（复选框、文件名/大小/页数/时间、状态徽标、全选+批量打印按钮）；中部预览区（`<img>` 逐页加载 `/api/jobs/{id}/preview?page=N`，前后翻页）；右侧设置面板（打印机下拉来自 `GET /api/printers`、彩色/灰度单选、份数、页码、单双面、纸张，按钮：开始打印/打开文件编辑/打开所在目录/作废/失败时重打）。

关键行为：WS 收到 `new_job` → 提示音（Web Audio `new AudioContext()` 蜂鸣 200ms）+ Toast + 刷新列表；`note` → 顶部横幅显示备注（带清除按钮）；`job_update` → 更新对应卡片徽标；设置变更后 `localStorage` 记忆默认值。标红文件（category=other）只显示"打开所在目录"按钮。

- [ ] **Step 2: 人工验证 checklist**：新文件弹窗+声音 ✓ 预览翻页 ✓ 各设置项正确出现在打印命令 ✓ 作废/重打 ✓ 默认值记忆 ✓ 收尾按钮 ✓
- [ ] **Step 3: Commit** `feat: 管理界面`

---

### Task 7B: 打印引擎 XPS 化改造（返工，替代 Task 4 的 SumatraPDF 打印路径）

> 依据 m1-verification §7-9 实测定案。**对外契约不变**（`PrintOptions` 字段、`print_pdf(cfg, pdf, o) -> tuple[bool, str]` 签名、routes.py 无需改动），仅替换内部实现。

**Files:**
- Create: `printshop/xps_print.py`（XPS 打包 + PrintTicket 提交；以 `tools/poc/file_to_xps.py`、`tools/poc/make_xps.py` 为起点，勿重造）
- Modify: `printshop/print_engine.py`（删 `normalize_page_size_to_a4`/`build_print_cmd`/SumatraPDF 打印路径；`print_pdf` 改走 xps_print；`normalize_to_pdf` Office 双轨制）
- Modify: `printshop/config.py`（增加 `render_dpi: int = 300` + 测试断言）
- Modify: `printshop/requirements.txt`（+pythonnet，若选型成立）
- Modify: `printshop/tests/test_print_engine.py`（删/换 SumatraPDF 用例）
- Test: `printshop/tests/test_xps_print.py`

**Interfaces:**
- Consumes: `Config`（新增 render_dpi）、`PrintOptions`、PoC 代码 `tools/poc/`
- Produces：
  - `xps_print.build_xps(pdf: Path, out: Path, dpi: int, pages: str = "") -> Path`（渲染+打包；pages 如 "1-3,5" 在渲染层选取）
  - `xps_print.submit_xps(xps: Path, o: PrintOptions) -> tuple[bool, str]`（XpsWriter.Write + PrintTicket）
  - `print_engine.print_pdf(cfg, pdf, o)` 内部 = build_xps → submit_xps
  - PrintTicket 映射：duplex simplex→OneSided / duplexlong→TwoSidedLongEdge / duplexshort→TwoSidedShortEdge；color True→Color / False→Monochrome；copies→PrintTicket 份数；paper A4

**硬约束（全部实测定案，违反即实机故障）：**
1. **勿用 PyMuPDF `show_pdf_page` 做页面归一化**——丢印章（透明组内容流不随 Form XObject 迁移）。原页直接 `get_pixmap(dpi=render_dpi)` 渲染，尺寸适配在 XPS 层做（Viewport/Viewbox）
2. **XPS `ImageBrush Viewbox` 填位图自然尺寸**（像素×96/dpi，单位 1/96 英寸），填像素值会按比例缩小（PoC v1 缩到 64% 的成因）
3. **统一 `PrintQueue.CreateXpsDocumentWriter().Write(fixedDocSeq, ticket)` 提交**——`AddJob` 在柯美 PCL 队列挂起且任务不创建（两次复现）；Epson 上 XpsWriter 需本任务补测
4. 灰度请求在柯美被驱动调和为 Monochrome（ConflictResolved）属正常，不算失败
5. 图片打印默认 contain-fit（等比、居中、不裁切、白底、去透明通道）
6. Office 转换双轨制：Office COM（ExportAsFixedFormat，本机 Office16 已实测）优先 → soffice 兜底 → 均不可用抛 FileNotFoundError（界面标红）
7. .NET 互操作选型：优先 **pythonnet**（pip 走镜像）；⚠️ Python 3.14 兼容性未验证——若无 wheel 或加载失败，降级为 **PowerShell 助手脚本**（生成 XPS 后 subprocess 调 ps1 提交）。选型过程与结论记入报告
8. 测试约定：XPS 打包结构可自动化验证（产物是合法 ZIP/OPC、含 FixedDocumentSequence、页数正确、PrintTicket XML 含指定双面/彩色/份数）；提交层 mock；双机实机为人工 checklist

- [ ] **Step 1: 失败测试** `test_xps_print.py`（打包结构/页数/DPI/PrintTicket 参数映射）+ 更新 test_print_engine（删 build_cmd 用例，改 print_pdf 为 mock xps_print 路径）
- [ ] **Step 2: 实现 xps_print.py**（以 PoC 为起点）→ 测试 GREEN
- [ ] **Step 3: print_engine 改造**（删 show_pdf_page 归一化与 SumatraPDF 路径；normalize 双轨制；print_pdf 接 XPS）→ 全量测试 GREEN
- [ ] **Step 4: 人工集成验证 checklist**：Epson XpsWriter 双面彩色（坑 3 补验）✓ 柯美 XpsWriter 双面黑白 ✓ 图片 contain ✓ Word COM 转换打印 ✓ 调查令类含印章 PDF 印章保真 ✓
- [ ] **Step 5: tools/poc/file_to_xps.py 与 make_xps.py 入库**（PoC 产物 png/xps 不入库，gitignore 排除）→ Commit `feat: 打印引擎 XPS 化（PrintTicket 路径，替代 SumatraPDF）`

---

### Task 8: 系统托盘 `tray.py`

**Files:**
- Create: `printshop/tray.py`；Modify: `printshop/main.py`（启动托盘线程）

**Interfaces:**
- Consumes: `POST /api/screen/wake`、`/admin` URL、会话状态

- [ ] **Step 1: 实现**（pystray，主进程内线程）：

```python
"""系统托盘：左键点亮小屏+刷新二维码；右键菜单；图标随状态变色。"""
import threading, pystray, requests
from PIL import Image, ImageDraw
from pystray import MenuItem as Item, Menu

BASE = "http://127.0.0.1:8000"

def _icon(color: str) -> Image.Image:
    img = Image.new("RGB", (64, 64), (0, 0, 0))
    d = ImageDraw.Draw(img); d.ellipse((8, 8, 56, 56), fill=color)
    return img

class TrayApp:
    def __init__(self):
        self.icon = pystray.Icon("printshop", _icon("gray"), "自助打印",
                                 Menu(Item("点亮屏幕", self.wake, default=True),
                                      Item("打开管理界面", lambda: __import__("os").startfile(f"{BASE}/admin")),
                                      Item("退出", self.quit)))
    def wake(self, *_): requests.post(f"{BASE}/api/screen/wake", timeout=5)
    def set_state(self, color: str): self.icon.icon = _icon(color)
    def quit(self, *_): self.icon.stop()
    def run_thread(self): threading.Thread(target=self.icon.run, daemon=True).start()
```

main.py 中实例化并 `run_thread()`；后台任务定期按 SessionManager.state 调 `set_state`（gray/green/red）。

- [ ] **Step 2: 人工验证**：托盘出现 ✓ 单击点亮（小屏/调试页变化）✓ 菜单项 ✓
- [ ] **Step 3: Commit** `feat: 系统托盘`

---

### Task 9: 端到端联调（真实微信全流程，人工）

**Files:** Modify: 联调中发现的缺陷修复

- [ ] **Step 0: 部署前置检查**：**Task 7B 必须已完成**（打印主路径为 XPS+PrintTicket）；店内路由器**关闭 AP 隔离**（否则小屏/手机访问不到主控，最高发坑）；Office 转换主路径为本机 Office COM（Office16 已实测），LibreOffice 可选安装作兜底；若 Task 10 已完成，小屏与调试页 `/screen` 同屏对照
- [ ] **Step 1: 按 spec §3.1 主流程走查**：点托盘 → 小屏/调试页出码 → 手机扫码 → 确认登录 → 发 3 个文件（PDF+图片+docx）+ 1 条文字 → 管理端弹窗/备注横幅 → 预览 → **双机打印验证（Epson 彩色双面 + 柯美黑白双面，均走 XpsWriter）** → 完成服务 → 确认 inbox 清空、会话登出、任务归档
- [ ] **Step 2: 按 spec §3.2 异常表逐项注入**：断网/掉线恢复、扫码不确认超时、5 分钟无文件提醒、打印失败重打、zip 标红
- [ ] **Step 3: 缺陷修复并补回归测试** → **Step 4: Commit** `fix: 端到端联调问题修复`

---

### Task 10: ESP32 固件 `firmware/print_screen/print_screen.ino`

**Files:**
- Create: `firmware/print_screen/print_screen.ino`
- Create: `firmware/print_screen/lcd_touch.cpp`、`lcd_touch.h`、`user_config.h`（从微雪例程仓 `02_Example/Arduino-v3.3.3/09_LVGL_V9_Test/` 原样复制——CO5300 显示 + CST9220 触摸 + 全部引脚定义都在其中，勿改）
- Test: 无自动化，烧录人工 checklist

**Interfaces:**
- Consumes: `GET http://<主控IP>:8000/api/screen/state` → `{state, display, qr_content, file_count, message}`；触摸唤醒时 `POST /api/screen/wake`

**并行性**：本任务只依赖 Task 6 的接口契约，不等 Task 9——可用 Python stub 返回固定 JSON 先行开发固件，与 Task 7/8/9 并行。

- [ ] **Step 1: 环境搭建（版本与仓库结构已核实，照做）**

1. Arduino IDE 2.x；开发板管理器 URL 加 `https://espressif.github.io/arduino-esp32/package_esp32_index.json`，安装 **esp32 core 3.3.x**（与例程同代，C6 必须 3.x）
2. 板型选 **ESP32C6 Dev Module**；Tools 菜单对照例程仓根目录的 `Tools Configuration.png` 截图：Flash Size 16MB，**USB CDC On Boot = Enabled**（C6 原生 USB 直连，不开则串口监视器无日志）
3. 库管理器装：`WiFiManager`（tzapu）、`ArduinoJson`、`ricmoo/QRCode`。⚠️ 伪码 `StaticJsonDocument<512>` 是 ArduinoJson **v6** 写法；装到 v7 就改成 `JsonDocument doc`（二选一，别混）
4. 克隆 `https://gitee.com/waveshare/ESP32-C6-Touch-AMOLED-2.16`（结构：`01_Arduino_Libraries` = XPowersLib+lvgl8/9；`02_Example/Arduino-v3.3.3` = 9 个例程）：
   - 把 `01_Arduino_Libraries/XPowersLib` 拷进 Arduino 库目录——**AXP2101 电源管理必须**：这块板子屏幕供电走 AXP2101，**不初始化 PMU 屏不会亮**（最高发坑）
   - 从 `02_Example/Arduino-v3.3.3/09_LVGL_V9_Test/` 复制 `lcd_touch.cpp/.h` + `user_config.h` 到本 sketch；**LVGL 本身不装**（本项目 UI 极简，直绘即可）
5. 先跑通两个官方例程验硬件：`01_AXP2101_Test` → `09_LVGL_V9_Test`（屏亮、触摸有反应 = 硬件与驱动 OK）

- [ ] **Step 2: 实现**，结构（已按板子实况修订）：

```cpp
// 依赖：WiFiManager, ArduinoJson(v6 写法), qrcode(ricmoo), XPowersLib(AXP2101)
// + 微雪例程 lcd_touch.cpp/.h、user_config.h（CO5300 显示 + CST9220 触摸 + 引脚定义）
// 全局：lastPoll、String qrShown

void setup() {
  // 初始化顺序严格照 09_LVGL_V9_Test 例程：AXP2101(PMU) → CO5300 显示 → CST9220 触摸
  initPMU();              // XPowersLib；不初始化 PMU 屏不会亮
  initDisplay();          // lcd_touch.cpp 封装；亮度 80%（烧屏防护#2）
  initTouch();            // CST9220
  wifiManager.autoConnect("PRINT-SETUP");  // 首次配网
  showText("等待主控...");
}

void loop() {
  if (millis() - lastPoll < 1500) { handleTouch(); return; }  // 1.5s 轮询
  lastPoll = millis();
  StaticJsonDocument<512> doc;   // ArduinoJson v7 则改为 JsonDocument doc
  if (!httpGetJson("http://" SERVER_IP ":8000/api/screen/state", doc)) { showText("系统维护中"); return; }
  bool display = doc["display"] == "on";
  String state = doc["state"].as<String>();
  if (!display) { screenOff(); return; }   // 优先用 lcd_touch.cpp 封装的 CO5300 displayOff/sleep（若有），否则全黑填充（烧屏防护#3）
  if (state == "IDLE") {
    String qr = doc["qr_content"].as<String>();
    if (qr != qrShown) { qrShown = qr; drawQR(qr); }
  } else {
    qrShown = "";
    showStatus(state, (int)doc["file_count"]);  // SCANNED/CONNECTED/RECEIVING/PRINTING 文案见 spec §6.1 表
  }
}

void drawQR(const String& content) {
  QRCode qrc; uint8_t qrcodeData[qrcode_getBufferSize(3)];
  qrcode_initText(&qrc, qrcodeData, 3, ECC_MEDIUM, content.c_str());
  const int scale = 9;                          // 480px/2.16寸≈222ppi：scale=6 实物仅约2cm，柜台距离难扫；9~10 → 约3cm+
  int size = qrc.size * scale;
  int ox = (480 - size) / 2 + random(-10, 10);  // 随机偏移（烧屏防护#1），注意偏移后静区不出屏
  int oy = (480 - size) / 2 + random(-10, 10);
  gfx->fillScreen(WHITE);                       // 白底黑码：微信对反色码兼容性因手机而异，正色更保险（AMOLED 白底功耗高，但亮屏短+亮度80%+偏移，烧屏风险可控；iOS+安卓扫码验证兜底）
  for (int y = 0; y < qrc.size; y++)            // 静区：码四周 ≥4 模块宽白边，白底天然满足
    for (int x = 0; x < qrc.size; x++)
      if (qrcode_getModule(&qrc, x, y)) gfx->fillRect(ox + x*scale, oy + y*scale, scale, scale, BLACK);
  drawFooter("微信扫码·传文件打印");            // 黑字
}

void handleTouch() {  // CST9220 轮询读坐标即可（不必中断）→ POST /api/screen/wake
  if (touchTapped()) { httpPost("http://" SERVER_IP ":8000/api/screen/wake"); delay(800); }
}
```

- `SERVER_IP` 写死主控固定 IP 即可满足 MVP；可选增强：WiFiManager 自定义参数把主控 IP 做成配网时可填（**不做进首版**）

- [ ] **Step 3: 烧录与配网**

1. USB-C 直连电脑，**C6 原生 USB 无需串口驱动**，选对应 COM 口直接上传
2. 上传失败/认不出端口 → **按住板子侧边 BOOT 键再重新上电**进下载模式，然后正常上传
3. 烧完开串口监视器（115200）看日志；无输出先查 USB CDC On Boot 是否 Enabled
4. 首次启动固件自建热点 `PRINT-SETUP` → 手机连上 → 填店内 WiFi 密码存 Flash（WiFiManager 标准流程）
5. ⚠️ **店内路由器必须关闭 AP 隔离**，否则小屏永远访问不到主控（现场最高发坑，列入部署前置检查）

- [ ] **Step 4: 人工验证 checklist**：首次配网 ✓ 息屏/点亮切换 ≤2s ✓ 二维码微信可扫（iOS+安卓各一，顺带确认白底黑码极性决定）✓ 状态文案切换 ✓ 触摸唤醒 ✓ 连续 2 小时待机无异常 ✓ **与浏览器调试页 /screen 同屏对照（固件显示与主控状态一致）** ✓
- [ ] **Step 5: Commit** `feat: ESP32 小屏固件`

---

### Task 11: 加固与恢复

**Files:**
- Modify: `printshop/session_manager.py`（断线重连、凭证恢复、看门狗）、`printshop/main.py`（启动时 `restore_session()`）
- Test: `printshop/tests/test_recovery.py`

- [ ] **Step 1: 失败测试**：凭证有效时启动恢复免扫码 ✓；`fetch_messages` 抛网络异常时指数退避重试（5s/15s/60s）且状态不变 ✓；连续失败 3 次 → 登出回 IDLE 出新码 ✓
- [ ] **Step 2: 实现**（run_loop 包 try/except，重连计数器；`save_session` 在登录成功与每 10 分钟定时执行）
- [ ] **Step 3: 测试通过 + Commit** `feat: 断线重连与会话恢复`

---

### Task 12: 打包部署

**Files:**
- Create: `printshop.spec`（PyInstaller）、`scripts/install_autostart.ps1`、`README.md`（部署说明）

- [ ] **Step 1: PyInstaller 打包**：`pyinstaller printshop.spec`（onefile，含 static/、tools/SumatraPDF.exe 数据文件），产出 `dist/printshop.exe`。⚠️ **Task 7B 选型联动**：若用 pythonnet，spec 需处理 .NET 运行时依赖（pythonnet 打包有已知坑，需实测；目标机需 .NET Runtime）；若用 PowerShell 助手方案，把助手 ps1 作为数据文件打入并在代码中解析其解压路径
- [ ] **Step 2: 开机自启**：`install_autostart.ps1` 写注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 或启动文件夹快捷方式
- [ ] **Step 3: 干净环境验证**：在无 Python 的 Windows 机器上跑 exe，主流程走查一遍（**含双机实机打印一次**——.NET 互操作在干净环境的可用性是本步骤重点）
- [ ] **Step 4: Commit** `build: 打包与开机自启`

---

### Task 13: 柜台立牌

**Files:**
- Create: `assets/signage.html`（浏览器打印成 A5 PDF）

- [ ] **Step 1: 实现**：A5 竖版 HTML：标题"微信扫码·自助打印" + 三步图文（①微信扫屏幕二维码 ②手机上点【登录】③在微信"文件传输助手"发送文件）+ 须知（单文件≤25MB；支持 PDF/图片/Word/Excel/PPT；发完请告知店员）+ 隐私公示（打印完成后立即退出登录并删除文件）
- [ ] **Step 2: 浏览器打印为 PDF 试印一张核对** → **Step 3: Commit** `docs: 柜台立牌`

---

## Self-Review 结论

- **Spec 覆盖**：§2 基线 1-7 → 无收费模块（符合）；打开文件编辑（Task 6 open/open_dir）；格式全收+标红（Task 4 classify/normalize + Task 7B 双轨转换 + Task 7 标红）；多打印机+彩色灰度（Task 7B XPS/PrintTicket + Task 7）；单队列（Task 5）；隐私红线（Task 5 finish_service + Task 9 验证）；小屏硬件+烧屏 4 条+立牌（Task 10/13）；托盘唤醒+强制换码（Task 5 wake + Task 8）。§3 流程与异常（Task 5/9/11）。§6.1/6.2 界面（Task 6/7/10）。§7 打印引擎（Task 4 骨架 + **Task 7B XPS 化返工**）。§9 数据模型（Task 3）。§10 测试策略（各任务 TDD + Task 0/7B/9 人工）。§12 里程碑 M1-M4 → Task 0 / 1-7 / 7B+8-10 / 11-13。
- **类型一致性**：`SessionState` 常量、`screen_state()` 字段名、`PrintOptions` 字段（Task 7B 保持不变）、`JobStore` 方法名在 Task 2-10 间一致；`print_pdf(cfg, pdf, o) -> tuple[bool, str]` 签名 Task 7B 前后一致（routes.py 无感）；`WeChatClient` 接口以 vendor 实际代码为准的调整规则已在 Task 2 注明。
- **执行顺序（2026-08-21 重编排）**：~~Task 0~~（已完成）→ Task 1-7（已完成）→ **Task 7B（打印引擎 XPS 化，Task 9 的前置）** → Task 8 → Task 9 → Task 10（可与 8/9 并行，只依赖 Task 6 契约）→ Task 11 → Task 12 → Task 13。已完成任务的编号与 brief/report/ledger 记录保持不变。
- **已知风险点**（执行时关注）：vendor `direct_bot.py` 的方法签名与本计划假设可能有出入（Task 2 Step 1 先核对）；`qr_content` 拼接格式以 Task 0 Step 4 实测为准（M1 已确认 `https://login.weixin.qq.com/l/{uuid}`）；固件最高发坑=**AXP2101 PMU 未初始化导致屏不亮**与**路由器 AP 隔离**（Task 10 Step 1/3 已标注）；打印三铁律（勿 show_pdf_page / Viewbox 自然尺寸 / 禁用 AddJob 用 XpsWriter，Global Constraints）；pythonnet 对 Python 3.14 兼容性未验证（Task 7B 选型决策点）。

## 修订记录

- 2026-08-21（之一）：依据 esp32-c6-touch-amoled-2-16 开发板专项会话（`esp32-c6-touch-amoled-2-16.json`，含微雪 Gitee 例程仓结构核实）修订 **Task 10**：环境搭建具体化（esp32 core 3.3.x / USB CDC On Boot / XPowersLib 必装 / 驱动文件来源 09_LVGL_V9_Test 的 lcd_touch.cpp/.h+user_config.h）；初始化顺序 AXP2101→CO5300→CST9220；二维码 scale 6→9~10、反色改白底黑码；触摸唤醒改轮询；补烧录与配网步骤（BOOT 键、AP 隔离）；checklist 加 /screen 同屏对照；注明可与 Task 7/8/9 并行。联动：**文件结构段**补固件驱动文件；**Task 9** 加 Step 0 部署前置检查。
- 2026-08-21（之二）：依据 Task 0 执行会话（`task-0.json`，147 条消息）的扩展排查定案（m1-verification §7 v2/§8/§9）修订**打印方案**：SumatraPDF 主路径废弃 → **PyMuPDF 原页渲染 → XPS 打包 → XpsDocumentWriter.Write + PrintTicket**（三铁律：勿 show_pdf_page / Viewbox 自然尺寸 / 禁用 AddJob）。**新增 Task 7B**（打印引擎 XPS 化返工，含 Config.render_dpi、Office COM/LibreOffice 双轨、图片 contain-fit、pythonnet 选型决策点、双机人工验证）；Task 4 加废弃注记（对外契约不变）；联动修订 Tech Stack / Global Constraints / 文件结构 / Task 9（前置依赖 7B + 双机回归）/ Task 12（pythonnet 打包注记）/ Self-Review 执行顺序。Task 10 不受打印方案影响，上一修订保持有效。
