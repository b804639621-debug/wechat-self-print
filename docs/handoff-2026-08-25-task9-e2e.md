# 交接文档：Task 9 端到端联调 + 花屏根治（2026-08-25）

> 写给后续会话：本文档打包 2026-08-25「Task 9 端到端联调 + Task 10 Step 4 剩余实机项」
> 会话的全部上下文。项目主计划在 `docs/superpowers/plans/2026-08-20-wechat-self-print.md`，
> 上一份交接（ESP32 小屏）在 `docs/handoff-2026-08-24-esp32-screen.md`。

> **2026-08-25 增补（BLE 省电改造计划）**：小屏后续方向已定为 **BLE 唤醒 + BLE 数据通道**，
> 替代 WiFi/HTTP 轮询；计划已写入 `docs/superpowers/plans/2026-08-20-wechat-self-print.md`
> 新增 **Task 10B**（仅计划态，未实施）。要点：空闲时 ALDO3 面板彻底断电、WiFi 关闭、
> CPU 尽量 light-sleep，仅维持低占空比 BLE 广播；音频 ALDO1/ALDO2 与触摸轨不在改造范围。
> 下一步是用户确认后执行 **Step S0 spike**，S0 通过才进入 S1。本交接文档本次与计划文档
> 一并提交；其余 Task 9 代码改动仍未提交。

## 一句话现状

Task 9 全部实测通过（主流程走查、双机打印矩阵、异常注入），Task 10 Step 4 仅剩
**2h 待机观察（进行中，cron 14:47 提醒）**与 **iOS 扫码（用户后续自测，无 iOS 设备）**。
当日共修复 **8 个实机缺陷**（含花屏、扫码不登录两个硬骨头，根因均已钉死并写入代码注释），
pytest **212 全绿**，修复版 `dist/printshop.exe` 已重打包冒烟通过并作为待机观察实例运行。
**工作区改动尚未 commit**——等用户确认（计划 Task 9 Step 4 提交信息 `fix: 端到端联调问题修复`）。

## 运行形态备忘

- 当前运行实例：`dist/printshop.exe`（2026-08-25 ~12:55 重打包，含当日全部修复），
  后台任务 `bash-ednicedc`（无超时）。数据目录在 `dist/`（db/state.json/inbox/work）。
- 源码调试运行：`WECHAT_TRACE_ENABLED=1 .venv/Scripts/python -m printshop.main`
  （项目根 cwd）。**trace 默认关闭**（PII 红线），仅排障时开；logout 会自动清 `trace_logs/`。
- **教训**：调试日志别放 `trace_logs/`——`finish_service` 会清整个目录，被占用的文件
  会导致"清理失败"误报（本次 com3_boot.log 被串口监听进程持锁 WinError 32 即此因）。
  当日调试日志已移至 `docs/debug-logs-2026-08-25/`。
- 固件编译烧录：`tools/arduino-cli/arduino-cli.exe compile/upload --fqbn "esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB" --build-path/--input-dir "C:/arduino-build/print_screen" firmware/print_screen -p COM3`；**烧录前必须停串口监听**。
- exe 冒烟：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_dist.ps1`。

## 当日修复的 8 个实机缺陷（全部实机验证 ✓）

### 1. 花屏根治（固件，最大硬骨头）

- **症状**：二维码/画面周期性花屏（逐行斜移纹），亮屏过程中闪烁后花屏。
- **根因**：**SH8601 GRAM 寻址对奇数 x 起点错位**。405 宽二维码 x0=37（奇）必现逐行右移；
  480 宽填充/文字 x0=0（偶）恒正常。早期"偶发"是 ±10px 防烧屏随机偏移让 x 起点在奇偶间跳变。
  高清照片（码区逐行恒定右移半模块、白底和页脚清晰）+ 串口零 DMA 超时记录共同钉死。
- **修复**：`drawQR` 的 `ox = ((LCD_W - size) / 2) & ~1`（偶数对齐）；**删除防烧屏平移机制**
  （用户拍板：亮屏仅在服务期间，finish 后息屏，烧屏风险低）；亮度 80%→60%；
  `lcd_wait_dma` 加超时计数 + 连续 8 次 ESP.restart 自愈 + 串口留证。
- 详见 `firmware/README.md`（根因结论与验收清单已更新）。

### 2. 扫码确认后主控检测不到登录（"双 jslogin"之谜）

- **症状**：扫码→手机确认→微信侧进文件传输助手（登录成功），主控永远停在出码态。
- **根因**：`wake()` 无条件 `_refresh_qr()` 换码——小屏触摸/托盘点亮/调试页操作每触发一次
  wake 就 jslogin 一张新码并替换 vendor 轮询 uuid，而 run_loop 的 25s 长轮询仍挂在旧 uuid；
  用户扫的是屏幕上的旧码，确认后旧码的 200 无人接收。trace 实锤：11:59:22/11:59:32 十秒内
  连换两码（od_4sH-b7w → QfoWu09o5A），od_4sH 的轮询 11:59:52 返回 408 后永远切走。
  （曾误判"同秒双 jslogin"——实为 trace 的 request/response 双行记录，勿再踩。）
- **修复**（`wechat_bot.py` + `session_manager.py`）：
  1. `get_qr(force=False)`：uuid 年龄 < 180s（vendor 240s 判过期留 60s 余量）**复用当前码**
     不发 jslogin；`asyncio.Lock` 锁内双重检查，并发 wake 只产一次 jslogin；
  2. `_LOGIN_CODE_STATUS` 的 **0 → "wait_scan"**（原 "expired"）：0 = uuid 空/网络异常，
     不再触发自动出码——finish 后与启动无会话时**静默息屏待唤醒**（符合用户模型）；
  3. `_check_scanned_timeout` 用 `force=True`：旧码处 201 已扫待确认态，必须作废换新。
- **验证**：启动零 jslogin ✓、首次 wake 单发 ✓、重复 wake 复用零新增 ✓、
  码 325s 自然过期单发换码 ✓、完整扫码链路实机通过 ✓。

### 3. finish 并发：点完成服务后小屏又亮新码

- **根因**：`finish_service` 清凭证的同刻，run_loop 并发读到"未登录"误走
  `_logout_to_idle_with_fresh_qr`（掉线亮新码路径）。
- **修复**：`_finish_lock`（asyncio.Lock）互斥；掉线路径拿锁后见状态已 IDLE 直接让位，
  不再亮屏出码。回归测试 `test_logout_to_idle_yields_when_finish_already_idle`。

### 4. 顾客消息全灭（is_mine 误判）

- 文件传输助手是"自聊"，顾客消息 is_mine 恒 True，绝不能按通用机器人惯例过滤。
  回归测试 `test_vendor_normalize_filehelper_self_chat_marks_is_mine` 钉死。

### 5-8. 联调缺陷四枚 + open_dir

- COM 初始化/绝对路径、打印按钮卡灰（成功打印或切换文件后需切换打印机才能再打印）、
  预览二次渲染（图片/PDF 预览慢）、小屏文案；
- `open_dir` 放行 other 类（zip 标红后可打开所在目录；`test_open_dir_other_category_allowed`）。

## Task 9 验证记录

- **Step 0** 部署前置 ✓（Task 7B XPS 主路径、AP 隔离已关、Office COM 转换——用户环境已装 Office，
  此前"未安装 Office"报错实为转换路径缺陷，修复后 docx 预览/打印正常）。
- **Step 1** 主流程 ✓：扫码→确认→发 PDF/图片/docx/文字 → 后台备注横幅 → 预览 →
  **打印矩阵：Epson 彩色真双面 ✓（job 7 调查令核对实物：彩色、真双面、内容完整）、
  柯美黑白双面 ✓、图片彩色单面 ✓、调查令双面灰度 ✓** → 完成服务（inbox/work 清空、
  登出、归档 10 行、息屏不出码）✓；zip 标红 ✓；失败重打 ✓；同名冲突加后缀 ✓；
  5 分钟无文件提醒 ✓；免扫码会话恢复 ✓。
- **Step 2** 异常注入 ✓：扫码不确认 2min 超时作废出新码 ✓；断 PC 网 ~1min（小屏显示
  "系统维护中"为固件连不上主控的兜底文案）恢复后续传消息 ✓ 免重扫 ✓。
- **Step 3** 缺陷修复+回归：pytest 212 全绿（基线 208 + 新增 4 枚：get_qr 复用/force/并发去重、
  finish 并发让位；另有 0→wait_scan 映射断言修订）。

## Task 10 Step 4 剩余项

- [x] 状态文案切换（真扫码流程）✓、双屏对照 ✓、安卓扫码 ✓、花屏根治后全链路 ✓
- [x] **2h 待机观察 ✓**（12:55–14:47 修复版 exe + 熄屏待机：主控存活、小屏无自亮/花屏/死机、
  触摸唤醒正常、电量心跳实时；期间 14:15 有一次任务托管侧实例重启，日志末尾为干净轮询，
  非程序崩溃）
- [ ] **iOS 扫码**：无 iOS 设备，用户后续手动自测

## 遗留事项

1. **commit 待用户确认**：计划 Task 9 Step 4 `fix: 端到端联调问题修复`（主控侧）；
   固件花屏修复如需单独提交一并确认。本次仅提交计划文档与本交接文档（docs），代码仍不提交。
2. 待机观察到点后更新计划文档 Task 10 Step 4 进度行 + 本文档补记结论。
3. `docs/debug-logs-2026-08-25/`（com3_boot.log、task9_state_transitions.log）为当日
   调试留证，可随下次大扫除删除。
4. **Task 10B BLE 省电改造**：计划已写入计划文档，待用户确认后从 Step S0 spike 开始；
   S0 Go/No-go 未通过不得进入 S1。实施范围不含音频 ALDO1/ALDO2 与触摸轨。
