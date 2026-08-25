# 交接文档：SDD 主线实施会话（2026-08-21，整理于 2026-08-25）

> 写给后续会话：本文档打包「子代理驱动开发（SDD）主线实施」会话的全部上下文——
> 从 Task 7 审查闭环到 Final 全分支终审，主控侧全部代码工作的决策、定案与教训。
> 读完应能无缝接手。
>
> **互补文档**：`docs/handoff-2026-08-24-esp32-screen.md` = 固件实机调试会话
> （闪烁/无二维码修复、arduino-cli 工具链、电池电量上报、实机验证记录）。
> 小屏硬件相关问题先读那份。
>
> **单一事实源**：任务级裁决/接口事实/deferred 清单在 ledger
> `.superpowers/sdd/2026-08-20-wechat-self-print/progress.md`（R0-R8 + 各任务闭环节）。

## 一句话现状

**全部代码工作完成**：主控 13 个任务 + 固件全部实现并过审，Final 终审的 3 Critical + 8 Important 全修，**206 tests 全绿**，工作区干净（HEAD=`cca03e6`，分支 `feature/self-print` 领先 master 21 commits）。
**剩余全是现场/实机事项**（见文末待办）：exe 重打包 → Task 9 端到端联调 → 分支收尾合并。

## 本会话做了什么（SDD 流程：每个任务 = brief → implementer → 双审查 → fix loop → 复审 → ledger）

| 序 | 工作 | commits | 要点 |
|---|---|---|---|
| 0 | 计划修订第二轮 | `a1c2492` | 按 Task 0 排查定案把打印方案从 SumatraPDF 改为 XPS+PrintTicket；**新增 Task 7B**；任务重编排 |
| 1 | Task 7 管理界面审查闭环 | `ddd6bb5` | 规格 APPROVED；质量 3 Important（打印连点双发/预览竞态/blob 泄漏）修复复审通过 |
| 2 | **Task 7B 打印引擎 XPS 化** | `6605628`+`44e1976`+`f8b10a4`+`df53e2d` | 新建 xps_print.py + submit_xps.ps1；pythonnet 弃用改 PowerShell 助手；质量审查 2 Critical（XPS 不清理/异常卡死会话）+3 Important 全修 |
| 3 | Task 8 系统托盘 | `94a78ec`+`2f2a9e3` | 质量 5 Important 全修（图标去抖/无效锁/wake 非阻塞/**退出=停整个服务**（产品决策）/trust_env=False） |
| 4 | Task 10 固件审查闭环 | （实现`e72a17e`）`751afed`+`7b90eeb` | ricmoo 库 QR 版本自动升级失效（恒停 v3，微信 URL 必超容量）→ 固定 v7；SCANNED 确认文案补渲染；RGB565 字节序；维护态抖动/恢复重绘 |
| 5 | Task 11 断线重连+会话恢复 | `3a7b3ce`+`5b236e1`+`a726075`+`a6806b6` | **R8 裁定**退避耗尽第 4 次连续失败才登出；凭证失效闭环；复审抓 P0：`await` 了 sync 方法 `is_authed()` 导致检测永不触发且测试假绿 |
| 6 | Task 12 打包部署 | `3c2dc83`+`0417339` | onefile exe 57.9MB；STATIC_DIR 冻结态修复；数据目录锚定 exe 旁；冒烟脚本 scripts/smoke_dist.ps1 实测 4 端点 200 |
| 7 | Task 13 柜台立牌 | `676c90f` | assets/signage.html（A5 竖版纯静态零依赖）；README 回填 |
| 8 | **Final 全分支终审** | `c92a187`+`7b453cc`+`cafc099`+`2b6c005` | 双维度（集成+安全隐私）抓 **3 Critical**（见下节）+8 Important+复审再抓 WS 缺口，全部修复 |

## Final 终审的 3 个 Critical（全分支视角才看得到，单任务审查全部漏过）

1. **SCANNED→CONNECTED 状态机死锁**（`c92a187`）：SCANNED 分支不 poll_login 且 _handle_poll 拒收非 IDLE——顾客扫码后点【登录】永远检测不到，主流程建联必卡死。**任务级测试全直接调 _handle_poll("confirmed") 故全绿假象**。修复+run_loop 级序列测试（poll 序列驱动真实 _run_once）。
2. **work/ 顾客文件副本永不清理**（`7b453cc`）：ensure_pdf/normalize 的 work_dir 产物不在 finish_service 清理范围，与立牌"不保存"承诺冲突。现 finish 清 inbox+work+trace_logs 三目录，失败收集并广播 cleanup_failed 告警。
3. **vendor 内存消息缓存跨顾客泄漏**（`7b453cc`）：direct_bot 的 7 个缓存结构 logout 不清，下个顾客可能拉到上个顾客消息。vendor 加了 `clear_message_cache()`（文件头有本地修改标注）。

复审再抓 1 阻塞：**/ws/admin 绕过回环限制**（Starlette `@app.middleware("http")` 不拦 websocket scope）→ 端点内 `_ws_loopback_guard`（routes.py:51，非回环 close 1008）。

## 关键定案速查（接手时不可违反）

### 打印三铁律（m1-verification §7-9 实测定案）
1. **禁用 PyMuPDF `show_pdf_page` 做归一化**——丢印章；原页 get_pixmap 渲染，尺寸适配送 XPS 层 Viewport/Viewbox
2. **XPS ImageBrush Viewbox 填位图自然尺寸**（像素×96/dpi，单位 1/96 英寸）
3. **禁用 PrintQueue.AddJob**（柯美 PCL 队列挂起），统一 `CreateXpsDocumentWriter().Write(ticket)`

### 产品/设计决策
- **R8**：网络退避 5s/15s/60s 三档耗尽后**第 4 次**连续失败才登出（字面"3 次"会让 60s 档永不达；规格文件已改对齐）
- 托盘"退出"= 停整个服务（uvicorn should_exit 优雅停机），非仅隐藏
- .NET 互操作 = **PowerShell 助手**（pythonnet 在 Python 3.14 无法按名解析 System.Printing）
- 纸张**仅支持 A4**（非 A4 明确报错；管理端下拉只剩 A4）；copies ≤99；文件 ≤25MB 服务端强制；Office COM 转换 120s 超时转 LibreOffice 轨
- **LAN 回环限制**：管理端点仅 127.0.0.1；LAN 只开放 /screen、/api/screen/*、/static（ESP32 必需）；/ws/admin 端点内校验回环
- trace_logs 默认**关闭**（PII 落盘风险）；finish_service 顺带清理
- 店内信任假设（已接受）：二维码 qr_content 对 LAN 可见；evil.docx 伪装宏残余风险（open 限回环后可接受）

## 环境/工具约定

- Python **3.14.5**，venv=`.venv`；测试 `.venv\Scripts\python.exe -m pytest printshop/tests -q`（当前 **206 passed**）
- pip 一律走镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple`
- PowerShell **5.1**：不支持 `&&`；ps1 脚本文件必须 **UTF-8 带 BOM**（否则中文注释按 GBK 解析炸）；heredoc 双引号会被截断（ledger 更新用 edit 工具）
- SDD 脚本：`C:\Users\陈晨\.config\opencode\node_modules\superpowers\skills\subagent-driven-development\scripts\`（task-brief/review-package，经 git bash `C:\Program Files\Git\bin\bash.exe` 调用）
- esptool 5.3.1 已装 venv；板子 COM3（USB-Serial/JTAG）；arduino-cli 工具链见 08-24 文档
- 隐私文件勿入库：test/、img*.pdf、*.db 已 gitignore；工作区提交前 `git status` 自查

## 教训（审查方法论，后续会话直接受益）

1. **状态机必须有 run_loop 级序列测试**——只测单函数会漏分支级死锁（C1 就是这么漏过两轮任务级审查的）
2. **隐私清理审计要覆盖全部产物目录**——inbox 清了不算完，work/trace/vendor 内存缓存都是边界
3. **中间件 scope 类型意识**——`middleware("http")` 管不到 WebSocket
4. **mock 的 sync/async 形式必须与真类一致**——AsyncMock 化 sync 方法会把 `await sync()` 的 TypeError 变成假绿（Task 11 P0）；防回归用真类绑定测试
5. **修复复审有价值**——本会话多次在复审/终审环节抓到 fix loop 新引入的问题（包括一次 controller 自己顺手修引入的测试失败）

## 待办（全部需现场/实机，按优先级）

1. **exe 重打包**：dist\printshop.exe 落后源码 6+ commits（含 C1 死锁修复、电池上报主控端）。`.venv\Scripts\python -m PyInstaller printshop.spec` → `scripts\smoke_dist.ps1` 验证
2. **Task 9 端到端联调**（主流程现场走查）：起服务 → 微信扫码 → 发 PDF+图片+docx+文字 → 管理端预览 → **双机 XPS 回归**（Epson 彩色双面 + 柯美黑白双面 + 含印章 PDF 保真 + Epson XpsWriter 补验）→ 完成服务 → 确认 inbox/work 清空+微信登出+归档。注意：联调前确认店内路由器**关 AP 隔离**
3. **Task 10 Step 4 剩余实机项**：状态文案切换全链路、/screen 双屏对照、iOS+安卓各扫一台、2 小时待机烧屏验证（详见 08-24 文档待办）
4. **分支收尾**：联调通过后走 finishing-a-development-branch（合并 master 决策——用户尚未批准合并）
5. Deferred minors 全清单在 ledger progress.md（WS 退避/日志限频已修，剩余均不阻塞）

## 关键文件索引

- 计划：`docs/superpowers/plans/2026-08-20-wechat-self-print.md`（含修订记录两条 + R8 措辞）
- spec：`docs/superpowers/specs/2026-08-20-wechat-self-print-design.md`（§7 打印引擎已过时，以 m1-verification 定案为准）
- 实测定案：`docs/m1-verification.md`（§7-9 打印排查全过程）
- ledger：`.superpowers/sdd/2026-08-20-wechat-self-print/progress.md`（R0-R8、任务闭环节、deferred）
- 主控源码：`printshop/`（main/config/wechat_bot/jobs/session_manager/print_engine/xps_print/routes/tray + submit_xps.ps1 + static/admin.html+screen.html + vendor/direct_bot.py）
- 部署：printshop.spec / scripts/install_autostart.ps1 / scripts/smoke_dist.ps1 / README.md
- 固件：`firmware/`（实机细节见 08-24 文档）
- 立牌：`assets/signage.html`（浏览器打开 Ctrl+P 打印 A5）
