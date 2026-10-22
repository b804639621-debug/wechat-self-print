# PrinterService 调研与优化建议

> 调研日期：2026-09-22
> 对象：<https://github.com/cp9no1/PrinterService>（1104★，2025-09 创建，master 分支，基于
> [a937750307/lan-printing](https://github.com/a937750307/lan-printing) 的修改版；Python，
> 最近一次 push 2025-09-18）
> 决策：**本轮只出文档不动代码**。以下"借鉴点"均为待办建议，实施顺序见文末。

---

## 1. 结论先行

PrinterService 是"同一需求的最简原型"：单文件 Flask 应用（`print_server.py` 约 30KB）
+ Bootstrap 静态页 + PyInstaller 单文件 exe，解决"内网任意设备浏览器上传 → 自动转 PDF
→ 静默打印"。

与本项目对比，**printshop 在几乎所有架构维度上领先**（微信协议接入 + 会话状态机、
SQLite 任务存储、XPS + PrintTicket 真双面/纸张/彩色、Office/LibreOffice 双轨转换、
管理端 loopback 门禁、隐私自动清理、178 个单测）。

因此：

- ❌ **不建议整体引入或替换架构**——那是明确降级；
- ✅ **可点状吸收 5 个功能点**（第 4 节），按性价比排序；
- ⚠️ 有一个反面教材必须记录：它的 `duplex`/`paper_size`/`quality` 参数收下后
  **函数体根本没用**（假双面），任何借鉴时都要核对参数是否真正生效。

---

## 2. PrinterService 项目画像

### 2.1 组成（仓库全部文件）

| 文件 | 说明 |
|---|---|
| `print_server.py` | 全部服务端逻辑，一个文件约 30KB |
| `static/index.html` + bootstrap | 单页 UI（拖拽上传、文件列表、预览、删除、打印按钮），样式全部离线打包 |
| `PrinterService.spec` | PyInstaller 打包配置 |
| `dist/内网打印服务.exe` | 直接提交的 41MB 成品 exe（仓库里带二进制产物） |

### 2.2 功能与实现要点

- **上传/转换**：Flask `/upload` 接收 → 按扩展名分流：
  - PDF：原样复制；
  - 图片（jpg/png/bmp/gif/tiff）：reportlab 画到 A4（等比 0.8 缩放居中）；
  - txt/md/log：reportlab + 注册中文字体（宋体/黑体/微软雅黑，失败退 Courier）；
  - Office（doc/docx/xls/xlsx/ppt/pptx）：`comtypes` COM
    `ExportAsFixedFormat → SaveAs2 → SaveAs` 多级回退链（Word/Excel/PowerPoint 各一套）。
- **打印**：PyMuPDF 固定 `Matrix(2.0, 2.0)` 渲染 → `win32ui` 打印机 DC + PIL `Dib.draw`
  贴到页面（GDI 路线）；份数用外层 StartDoc 循环；另有一个 `print /D:` 命令行兜底
  （对 PDF 基本无效）。
- **托盘**：pystray，菜单显示 `局域网IP:5000`（UDP 连 8.8.8.8 取本机 IP）、
  HKCU `...\CurrentVersion\Run` 开机自启开关（菜单内即点即切）、退出。
- **清理**：两个守护线程每 600s 扫一遍 uploads/ 和 pdfs/，mtime 超 3600s 即删。
- **日志**：纯文本追加，首页展示最近 10 条。

### 2.3 短板（借鉴时必须避开）

| 短板 | 细节 |
|---|---|
| 完全无鉴权 | 同网段任何人可上传/打印/删除/预览 |
| `duplex` 假参数 | `silent_print_pdf(pdf, printer, copies, duplex)` 收下 duplex 后从未使用——**选双面实际打单面**；paper_size/quality 仅写日志 |
| Flask dev server | `app.run(host='0.0.0.0')` 单进程跑生产；COM 转换阻塞在请求线程，转换期间整个服务无响应 |
| 渲染质量 | 固定 2× 缩放 + 0.9 等比贴图，无 DPI 适配，文字发虚 |
| 工程化缺失 | 无任务队列、无持久化、无状态机、零测试 |

---

## 3. 与 printshop 逐项对比

| 维度 | PrinterService | printshop（现状） |
|---|---|---|
| Web 框架 | Flask dev server | FastAPI + uvicorn |
| 接入通道 | 局域网浏览器直传 | 微信 filehelper 协议（vendored `direct_bot.py`） |
| 任务管理 | 内存 + 目录扫描 | SQLite `JobStore` + 会话状态机 |
| 打印引擎 | PyMuPDF→GDI Dib | PyMuPDF→XPS→PrintTicket（`submit_xps.ps1`，双面/纸张/彩色真实生效） |
| Office 转换 | comtypes COM 单轨 | Office COM（win32com DispatchEx）↔ LibreOffice 双轨（COM 超时强制走 Libre 轨） |
| 安防 | 无任何鉴权 | 管理端 loopback 门禁 + 会话生命周期 |
| 隐私 | 1 小时 TTL 清扫 | 服务态 finish 全量清理（logout + inbox/work/trace）+ XPS 产物 try/finally 删除 |
| 周边硬件 | 无 | ESP32 小屏固件 + BLE 链路 + 托盘 + pywebview GUI |
| 测试 | 无 | 178 例 |
| 文本文件 | ✅ 支持 txt/md/log | ❌ `classify_file` 归 "other"，直接不支持 |
| 浏览器直传 | ✅ 核心功能 | ❌ 无 B 通道（微信挂了就没有入口） |
| 托盘自启开关 | ✅ 菜单内切换 | ❌ 只有 `scripts\install_autostart.ps1` |
| 托盘显示局域网地址 | ✅ | ❌ |

---

## 4. 借鉴点（按性价比排序，均为建议未实施）

### 4.1 txt/md/log 文本支持（推荐先做）

- **差距**：`printshop/print_engine.py` `_OFFICE_EXTS`/`_IMAGE_EXTS` 均不含文本扩展名，
  `classify_file()` 返回 `other` → `normalize_to_pdf` 抛 `ValueError`。打印店场景顾客发
  记事本/日志/Markdown 很常见。
- **借什么**：reportlab + 从 `C:\Windows\Fonts` 注册宋体/黑体/微软雅黑的思路，以及
  多编码尝试序列（utf-8 → gbk → gb2312 → utf-8-sig）。
- **要改掉它的坑**：它的换行按固定字符数硬切（不区分中文宽字符、无词边界）。
  建议改为 PyMuPDF `insert_textbox`（自带 CJK 断行）或 reportlab + 字宽感知换行。
- **落点**：`print_engine.py` 新增 `_TEXT_EXTS` 与 `_convert_text_to_pdf`，
  `classify_file` 增加 `"text"` 分支；补 `tests/test_print_engine.py` 用例。

### 4.2 Office COM 转换加固

- **差距**：`printshop/print_engine.py` `_convert_with_office_com` 只设了 `Visible=False`，
  未设 `DisplayAlerts`/`EnableEvents`；导出失败即整体失败。
- **风险**：Word 对损坏/受保护文档会弹模态对话框，无人值守时转换线程挂起直到 COM 超时
  才降级 LibreOffice。
- **借什么**：
  1. `app.DisplayAlerts = False`（Word）/`app.DisplayAlerts = False` + `ScreenUpdating=False`（Excel）；
  2. `ExportAsFixedFormat` 失败后 `SaveAs2(FileFormat=17/57/32)` → `SaveAs(...)` 兜底链
     （Word=17、Excel=57、PPT=32），任一成功即可。
- **落点**：仅改 `_convert_with_office_com` 内部，签名与返回语义不变；现有
  `test_print_engine.py` 的 COM mock 用例需同步适配。

### 4.3 托盘自启开关 + 局域网地址显示

- **差距**：自启只能跑 `scripts\install_autostart.ps1`；托盘菜单（`printshop/tray.py`）
  不显示局域网 IP，现场部署排查"手机访问不到"时得另开 cmd 敲 ipconfig。
- **借什么**：HKCU Run 键读写开关（菜单内即点即切，逻辑 ~20 行）；UDP connect
  `8.8.8.8:80` 取本机 IP 的写法（失败退 127.0.0.1）。
- **落点**：`tray.py` 菜单加两项；`--ble-selftest` 与测试路径不受影响。

### 4.4 顾客浏览器直传兜底通道（工作量最大）

- **差距**：入口完全绑定微信 filehelper；微信侧异常（协议改版/风控/账号受限）时顾客
  无任何上传途径。
- **借什么**：它证明"店内 WiFi 浏览器拖拽上传"可行且体验好。做法：
  - screen 页在二维码旁加一个店内局域网直传入口（`http://主控IP:8000/u?token=...`）；
  - token 与当前会话绑定、随会话生成/销毁，文件直接进 inbox 走同一套 JobStore 与清理红线；
  - 路由层注意：`main.py` 的 loopback_guard 中间件对非本机请求拦截管理端点，
    新上传端点必须走 `_PUBLIC_PREFIXES` 白名单并在端点内校验 token
    （参照 `routes.ws_admin` 的 `_ws_loopback_guard` 思路）。
- **前提**：README 已写明路由器需关闭 AP 隔离——该通道与微信扫码同受此约束。
- **优先级**：建议在 4.1~4.3 验证后再排期，属新功能而非移植。

### 4.5 XPS 失败时的自动打印兜底

- **现状**：`print_pdf` XPS 路径失败仅返回错误文本；SumatraPDF（`tools/`）只是备用
  调试手段，未接入自动兜底。
- **建议**：与其抄它的 GDI Dib 直打（清晰度差），不如把 SumatraPDF
  `-print-settings` 接成 XPS 抛错后的自动第二轨——调研报告第 5 节本来就按
  SumatraPDF 参数表设计过，等于回归原备选方案。仅在 XPS 连续失败时触发并上报告警。

---

## 5. 明确不借鉴的点

1. **无鉴权的局域网开放**——违背本项目隐私红线与 loopback 门禁设计；
2. **Flask dev server 模式**——uvicorn 已就位；
3. **固定 2× GDI Dib 渲染路线**——XPS 路线全分辨率且 PrintTicket 生效；
4. **假 duplex 实现**——反面教材，任何参数都要有测试证明其生效；
5. **TTL 扫描式清理**——本项目的会话绑定清理更严格（打印完成即清，不留 1 小时窗口）。

---

## 6. 待办排序建议

```
4.1 文本支持        → 半天级，纯增益
4.2 COM 加固        → 半天级，消除实机卡死风险
4.3 托盘增强        → 半天级，部署体验
4.5 SumatraPDF 兜底 → 一天级
4.4 直传通道        → 需求评审后再排期（涉及安防面）
```
