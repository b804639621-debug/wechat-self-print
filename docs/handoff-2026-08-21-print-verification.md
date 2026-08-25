# 交接文档：打印链路实机验证会话（2026-08-20 ~ 08-21）

> 写给后续会话：本文档打包「Task 0（M1 链路验证）执行 + Epson 双面问题根因排查 + XPS 打印路径定案 + 双机多格式实机验证」会话的全部上下文。项目主计划在 `docs/superpowers/plans/2026-08-20-wechat-self-print.md`；固件会话交接见 `docs/handoff-2026-08-24-esp32-screen.md`。
>
> 详细实验数据全部在 `docs/m1-verification.md`（§5-§9），本文档只做索引、环境变更与遗留事项，**不重复实验细节**。

## 一句话现状

打印链路已完成从「SumatraPDF 方案」到「**XPS + PrintTicket 系统打印服务方案**」的实机定案：两台打印机（EPSON L6370 / KONICA C266）× 三类文件（PDF/图片/Word）× 单双面 × 彩色/黑白**全部实机通过**；后续会话已把该方案工程化入库（`printshop/xps_print.py` + `printshop/submit_xps.ps1`，含测试）。

## 本会话时间线与提交

| 提交 | 内容 |
|---|---|
| `c0dedae` | Task 0 完成：M1 验证记录（微信扫码/文件收发/二维码格式/SumatraPDF 单面打印）+ 项目首批文档入库 |
| `9f6b738` | §7 v2：Epson 双面问题根因定案 + XPS 路径实机验证通过 |
| `dd9dc29` | §8：柯美 C266 验证通过；定案统一 XpsWriter 提交（AddJob 在柯美挂起）；PDF 直打不可行结论 |
| `24157dd` | §9：图片（contain-fit 不裁切）+ Word（Office COM 转换）双机验证通过 |

（中间 `ed0ba1d` 由另一会话提交，内容为本会话的 §7 v1 排查记录 + gitignore 隐私排除。）

## 核心结论速查（细节见 m1-verification.md 对应节）

1. **微信链路（§3/§4）**：扫码登录无额外验证；文件自动下载完整；二维码内容 = `https://login.weixin.qq.com/l/{uuid}`（uuid 直接拼接即可，无需解码图片）；`/qr` 返回的图实际是 **JPEG**（尽管声明 image/png）。
2. **Epson 双面问题（§7）**：SumatraPDF 静默 `-print-settings` 的 `duplex`/`paper` 被 Epson 驱动丢弃 → 面板"尺寸不匹配"停机 + 单面。**解法 = XPS + PrintTicket**（驱动对 PrintTicket 完全接受，`MergeAndValidatePrintTicket` NoConflict）。
3. **提交 API 坑（§8 坑 3）**：`PrintQueue.AddJob` 在柯美 PCL 队列**挂起且任务不创建**；统一用 `PrintQueue.CreateXpsDocumentWriter().Write()`。工程化时已遵守（submit_xps.ps1 顶部有注释）。
4. **渲染层两坑（§7 踩坑）**：勿用 `show_pdf_page` 归一化（丢印章等内容流）；XPS `Viewbox` 填位图**自然尺寸**（像素×96/dpi）而非像素值。
5. **PDF 直打不可行（§7 附录）**：主机渲染型打印机无 PDF 直打；所有路径都渲染，渲染 DPI 做成配置（`render_dpi`，默认 300）。
6. **图片策略（§9）**：contain（完整可见不裁切）；Word 转换双轨制（本机 Office COM 优先 / 无 Office 用 LibreOffice，都失败标红）。

## 本会话对机器环境的改动（其他会话必读）

| 改动 | 状态 | 处置建议 |
|---|---|---|
| 新增打印机队列 `EPSON L6370 (TCP)`（端口 IP_192.168.31.32:9100） | **仍在** | 保留备用（WSD 故障时切换）；不需要可删 |
| WSD 队列默认配置 | 已恢复 OneSided/Color/A4 | 无需动 |
| `tools/SumatraPDF.exe` 3.6.1 便携版 | 已入库 | 现仅作备用/调试工具 |
| `vendor-ref/wx-filehelper-api` + 其 `.venv`（Python 3.14.5） | 已 gitignore，**服务已停止**（8000 端口现归 printshop） | Task 0 验证用，已完成的使命；留作协议参考 |
| pip 清华镜像 | 本会话起统一使用 | 直连 PyPI 会超时回溯，继续沿用镜像 |
| Epson Web Config | 在 **https**://192.168.31.32（80 端口也是 TLS，需 `-k`/忽略证书） | 查面板登记/状态用 |
| Windows 打印事件日志（PrintService/Operational） | 本机默认开启 | 判"任务是否创建/送达"比看队列可靠（800/801/805/307/842） |

**打印机档案**：
- EPSON L6370：Wi-Fi（KAIXUAN_5G），IP 192.168.31.32，WSD 队列（另有 TCP 备用队列）；面板进纸器 C 登记 **A4**（改登记会破坏既有验证结论）；支持自动双面/彩色。
- KONICA C266：IP 192.168.31.167，**LPR:515 队列名 print，SNMP public**；双面 OK；实际只出黑白（PrintTicket 请求 Grayscale 会被调和为 Monochrome）。

## 会话产物与隐私边界

- PoC 源码已入库：`tools/poc/make_xps.py`、`tools/poc/file_to_xps.py`（工程化版本 = `printshop/xps_print.py`，以这两个为起点）。
- PoC 产物（png/xps/pdf、`*_work/`）与顾客/店内真实文件（`调查令-30(1).pdf`、`img20260821_0001.pdf`、`test/`、`test.pdf` 等）**均已 gitignore——隐私红线，永远不要入库**。
- 测试用的真实文件含法院文书/个人信息，删除或保留由店主决定；后续演示勿用真件。

## 遗留事项（本会话视角，供排程参考）

1. **Task 9 端到端联调**时按 §9 矩阵回归打印：真实微信来的 PDF/图片/Word × 双机 × 双面；外加 LibreOffice 缺失时 office 文件标红提示路径（本机 LibreOffice 至今未装，office 转换走的是 Word COM）。
2. 柯美 C266 若日后开启**账户跟踪/部门密码**，XPS 路径需重测（本次在未开启状态下验证）。
3. Epson 面板"打印纸尺寸检查"保持开启——现有方案靠 XPS 层把页面适配成 A4 避免停机；不要为省事去关它。
4. 本机同时存在 EPSON 的 WSD 与 TCP 两个队列，管理端打印机下拉会出现两个名字，注意引导店员区分（或删除其一）。

## 关键文件

- 实验全记录：`docs/m1-verification.md`（§5 SumatraPDF 单面 / §7 Epson 双面根因 / §8 柯美 / §9 图片+Word）
- 工程化打印：`printshop/xps_print.py`（XPS 打包 + PrintTicket 生成）、`printshop/submit_xps.ps1`（XpsWriter 提交助手）、`printshop/print_engine.py`（编排与隐私清理）
- 测试：`printshop/tests/test_xps_print.py`、`test_print_engine.py`
