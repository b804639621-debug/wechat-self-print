# M1 链路验证记录（Task 0）

日期：2026-08-20 ｜ 执行人：OpenCode + 店主（人工扫码/确认）｜ 机器：Windows 10/11 x64（LAPTOP-8R502RF9）

**总结论：四项关键链路全部通过，无需走降级路线（浏览器自动化），按计划进入 Task 1。**

| 验证项 | 结果 |
|---|---|
| 微信扫码登录可用性 | ✅ 通过（无额外验证） |
| 文件下载完整性 | ✅ 通过（PDF/图片均完整可打开） |
| 二维码内容格式 | ✅ 确认 `https://login.weixin.qq.com/l/{uuid}` |
| SumatraPDF 打印管线 | ✅ 通过（虚拟打印 + EPSON 实机均退出码 0） |

---

## 1. 环境

- Python：**3.14.5**（≥3.11 满足；注意比常规测试版本新，后续任务留意第三方包兼容性）
- 底座：`wx-filehelper-api` @ `cb52d3a`（克隆到 `vendor-ref/wx-filehelper-api`）
- venv 依赖安装成功：fastapi 0.141.1 / uvicorn 0.52.4 / python-multipart 0.0.32 / httpx 0.28.1 / starlette 1.6.0 / pydantic 2.13.4
- ⚠️ 环境坑：直连 PyPI 下载超时且 fastapi 版本回溯极慢；换**清华镜像** `-i https://pypi.tuna.tsinghua.edu.cn/simple` 后秒装。后续 `pip install` 统一走镜像。

## 2. `/qr` 接口（Step 2）

- 服务 `python main.py` 正常启动，监听 `0.0.0.0:8000`，`GET /` 返回服务概览（初始 `logged_in:false, status:"need_qr"`）。
- `GET /qr` 返回 38,195 字节图片，**实际为 JPEG 编码（文件头 `FF D8 FF E0`），尽管 HTTP Content-Type 声明为 `image/png`**。目视确认为有效微信登录二维码。
- 📌 对 Task 2 的影响：`get_qr()` 返回值不能假定是 PNG 字节（变量名/注释勿写死 png）；ESP32 只取内容字符串不受影响；admin 页面 `<img>` 按字节实际格式渲染即可。

## 3. 微信扫码 + 文件收发（Step 3，人工）

- 手机微信扫码 → 手机点【登录】→ 成功。**未要求任何额外验证**（无滑块/无设备确认）。
- 服务端确认：`GET /login/status?auto_poll=false` → `{"logged_in":true,"code":200,"status":"logged_in","entry_host":"szfilehelper.weixin.qq.com","login_host":"login.wx2.qq.com"}`。
- ⚠️ 注意：`/login/status` 默认 `auto_poll=true` 时长轮询，15s 超时拿不到响应；快速查状态用 `auto_poll=false`。
- 文件收发：手机"文件传输助手"发送 1 PDF + 1 图片（另早前发过 1 PDF），后台监听器数秒内自动下载到 `downloads/2026-08-20/`：
  - `1787140249950.jpeg` 117,997 B：SOI/EOI 标记完整（`FF D8`…`FF D9`），渲染正常
  - 两个 PDF（320,261 B / 11,356 B）：`%PDF-` 头 + `%%EOF` 尾完整，PyMuPDF 均可打开（各 1 页）
- **结论：登录可用 ✅；文件下载完整可打开 ✅。**

## 4. 二维码内容格式（Step 4）

OpenCV `QRCodeDetector` 解码 `/qr` 下载的图片，输出：

```
'https://login.weixin.qq.com/l/obyDMELwoQ=='
```

- **格式确认为 `https://login.weixin.qq.com/l/{uuid}`**，uuid 为 base64 风格字符串（可含 `=` 填充；登录态接口返回的 uuid 形如 `QeDctiKWNw==` 与此一致）。
- 📌 **Task 2 的 `get_qr()` 可用 uuid 直接拼接内容串**：`QR_CONTENT_TEMPLATE = "https://login.weixin.qq.com/l/{uuid}"`，无需 OpenCV 解码图片。

## 5. SumatraPDF 打印（Step 5）

- 版本：**SumatraPDF 3.6.1 便携版**（20,292,984 B），位于 `tools/SumatraPDF.exe`。
- 打印命令（计划同款）：`SumatraPDF.exe -print-to <打印机> -print-settings "monochrome,fit,paper=A4" -silent <pdf>`

| 目标打印机 | 退出码 | 结果 |
|---|---|---|
| Microsoft Print to PDF | **0** | 弹出"另存为"对话框，输出 745,400 B 合法 PDF（`%PDF-`/`%%EOF` 完整） |
| EPSON85171C (L6370 Series)（实机，WSD） | **0** | **实际出纸，内容完整正常**（灰度/A4，店主现场确认） |

- 退出码语义与官方文档一致（0 成功 / 2 打不开 / 3 禁打 / 4 无此机 / 5 驱动失败 / 6 策略禁止），与计划 Task 4 的 `EXIT_CODES` 表吻合。
- 本机可用打印机（`Get-Printer`）：`EPSON85171C (L6370 Series)`、`KONICA MINOLTA C266SeriesPCL`（192.168.31.167）、`Microsoft Print to PDF`、`Microsoft XPS Document Writer`、`Fax`、`HP Smart Printing`、`OneNote` 系列。
- ⚠️ **异常记录（不阻塞）**：`-list-printers` 在本机退出码 1 且无打印机列表输出（直接运行 / `cmd /c` 重定向 / 加 `-silent` 三种方式均如此，仅输出启动日志）。官方文档称加 `-console`/`-silent` 输出到控制台，实测 3.6.1 未生效，疑似该版本回归或无控制台会话的兼容问题。**不影响项目**：计划 Task 4 的 `list_printers()` 走 `win32print.EnumPrinters`，不依赖此子命令；`-print-to` 指定打印机名工作正常。
- 验证样本：`tools/test-print.pdf`（微信实收的 PDF 复制件，11,356 B），后续 Task 4 无纸集成验证可复用。

## 6. 其他观测

- 后台监听器在未登录状态下持续轮询登录状态，扫码确认后自动转登录态并开始收消息，无需手动触发。
- 心跳/会话保存任务正常（`save_session` 周期执行），重启恢复可行性留待 Task 11 验证。
- 中文路径 `E:\kaifa\自助打印` 下 Python/uvicorn/SumatraPDF 均工作正常（SumatraPDF 设置文件正常落盘）。

---

## 7. 追加：EPSON L6370 彩色双面实机测试（2026-08-20 下午，调查令-30(1).pdf）

> ⚠️ **重要未决问题**：SumatraPDF 静默打印在 EPSON L6370 上**双面失效 + 触发面板"尺寸不匹配"停机**。彩色正常。单面链路（§5）不受影响。

### 测试样本与环境

- 样本：`调查令-30(1).pdf`（582,881 B，2 页，页面尺寸 **Letter 612×792pt**，含红章）
- 面板登记：进纸器 C = **A4**（用户面板确认）
- 驱动能力（.NET PrintCapabilities 实测）：双面 ✅（OneSided/TwoSidedLongEdge/TwoSidedShortEdge）、彩色 ✅、A4/Letter ✅
- 打印机网络：Wi-Fi（SSID KAIXUAN_5G），IP 192.168.31.32，Epson Web Config 在 **https**://192.168.31.32（80 端口也是 TLS，需 `curl -k`）

### 实验记录（每次均打印同一文件，彩色）

| # | 路径 | 关键参数 | 结果 |
|---|---|---|---|
| 1 | SumatraPDF 静默 → WSD 队列 | `color,duplexlong,fit,paper=A4` | ❌ 面板停机（尺寸不匹配），强制继续后**单面**；彩色正常 |
| 2 | 同上 | `paper=letter` | ❌ 同 1（面板报错照片留存） |
| 3 | SumatraPDF `-print-dialog` 驱动对话框手动选双面 | A4+双面长边 | ✅ **一张纸双面，无报警** |
| 4 | 先 `Set-PrintConfiguration` 默认=双面/A4/彩色，静默仅带 `fit` | — | ❌ 仍报警+单面 |
| 5 | 新建 TCP/IP:9100 队列（排除 WSD 因素），同 1 参数 | `color,duplexlong,fit,paper=A4` | ❌ 仍报警+单面 |
| 6 | **XPS + PrintTicket 系统打印路径**（`PrintQueue.AddJob`，A4+双面长边+彩色） | v1 PoC | ✅ **双面成功、无报警**；❌ 尺寸缩至 ~64%、红印章丢失（PoC 自身两个 bug，见下） |
| 7 | 同 6，修复 PoC bug 后 | v2 PoC | ✅✅ **双面+彩色+印章+尺寸全部正常，面板无报警** |

### 结论（systematic-debugging）

- **硬件+驱动+面板链路完好**：驱动对话框手动双面成功（#3）。
- **SumatraPDF 3.6.1 静默打印的 `-print-settings` 中 `duplex`/`paper` 对该 Epson 驱动不生效**（duplex/paper 确认被丢弃），任务始终以"文档自身尺寸 Letter + 单面"下发 → 与面板登记 A4 不匹配 → 停机 → 强制继续后保持单面。
- 排除项：纸张尺寸值本身（#1 A4 也失败）、驱动默认配置继承（#4）、WSD 端口（#5 换 TCP/IP 无效）。
- **解法已实测定论：走系统打印服务的 PrintTicket 路径（XPS）**——`MergeAndValidatePrintTicket` 零纸张验证驱动接受 TwoSidedLongEdge+Color+A4（NoConflict），实机 #7 全通过。**问题回答：能解决。**
- 测试后已恢复 WSD 队列默认配置（OneSided/Color/A4）；新增队列 `EPSON L6370 (TCP)`（端口 IP_192.168.31.32:9100）保留备用。
- PoC 产物：`tools/poc/make_xps.py`、`tools/poc/poc-duplex-v2.xps`（未提交）。

### PoC 踩坑（Task 4 实现必读）

1. **❌ 勿用 PyMuPDF `show_pdf_page` 做页面归一化**——它只搬内容流主体，丢失了本文件的红印章（印章是内容流中的透明组图像，非注释）。✅ 正确做法：原始页面直接 `get_pixmap()` 渲染，尺寸适配放在 XPS 层（Viewport）。
2. **XPS `ImageBrush` 的 `Viewbox` 单位坑**：PyMuPDF 输出的 PNG 带 pHYs（如 150/300dpi），XPS 按 dpi 换算自然尺寸（像素×96/dpi，单位 1/96 英寸）。Viewbox 必须填**自然尺寸**而非像素值，否则内容按比例缩小（v1 因此缩到 64%）。

### 对项目的影响（Task 4 打印引擎调整）

- **打印主路径改为：normalize（格式归一到 PDF）→ PyMuPDF 原页渲染 PNG（300dpi，含印章/注释）→ 打包 XPS → `PrintQueue.AddJob` + PrintTicket（彩色/灰度、单双面、A4、份数）**。页码范围在渲染层选取。python 侧调 .NET：pythonnet，或生成 XPS 后由一小段 PowerShell/C# 助手提交（Task 4 定夺，优先 pythonnet）。
- SumatraPDF 降级为备用/调试用（`-print-to` 单面场景本机已验证可用）。
- **KONICA MINOLTA C266 未测**——Task 4 应对其做同款 XPS 双面验证（预期同样工作，但需实测）。
