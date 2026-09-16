# WiFi 省电改造 · 实机验收交接（2026-09-09）

> 计划：`docs/plan-2026-09-07-wifi-power-save.md`；实施交接：`docs/handoff-2026-09-07-wifi-power-save-stage1.md`
> 本文件记录**实机验收结果、遗留物理项与发布待办**。验收用固件为带 `-DWIFI_DEBUG_PERF` 插桩构建（源码相同，仅多 `[perf]` 串口行），收尾已刷回无插桩正式版。

## 1. 验收结论：量化项全部 PASS

| 项 | 判据 | 实测 | 结论 |
|---|---|---|---|
| §1.4 ping | 0 丢包、max ≤600ms | 200/200，min 2 / max 530 / avg 100ms | PASS |
| §1.4 轮询节奏 | 无 HTTP 超时、failCount 不递增 | 1216 轮全部 code=200，http 24~96ms 典型 | PASS |
| §1.4 kill 主控 | ≤5s 进维护画面，恢复后自愈 | kill→维护 4.16s；服务重启后 ~2-3s 恢复 200，屏回熄屏 | PASS |
| §3.3 降功率 | 0 丢包；avg RTT 与 20dBm 基线差 <30% | 11dBm avg 100ms vs 20dBm avg 86ms（同 PS 态对比），+16% | PASS |
| §2.4 唤醒时延 ×10 | ≤7s、典型 ≤5.5s | min 0.21 / 中位 4.94 / max 5.75s | PASS |
| §2.4 在线不闪烁 | 熄屏静置 >20min 无假离线（轮询间隔 >12s 次数=0，阈值 12s） | 36 分钟 1328 次到达，max 间隔 7.05s，>12s = 0；WS 全程仅 1 条 device 快照 online=True | PASS |
| §2.4 阈值/电量 | thr=12.0 生效；电池 pct 非空、stale_after=180 | WS 快照确认 thr=12.0、pct=100、stale_after=180 | PASS |
| §4.4 iTWT | 三种结局之一且轮询正常 | AP（小米 KAIXUAN）**不支持 iTWT**：`setup ret=0x106(ESP_ERR_NOT_SUPPORTED)` 静默回退 DTIM，轮询正常 | PASS（回退路径） |
| §5.3 重绘耗时 | drawQR <800ms（预期 <300ms） | n=11，206~208ms（80MHz） | PASS |
| 回归 | 无重启/无 code=-1/无 screen on fail | 全程 0 次 | PASS |
| A2 骨架 | 官方库下 esp_pm_configure 返回 NOT_SUPPORTED 且系统照常 | 串口 `[pm] esp_pm_configure ESP_ERR_NOT_SUPPORTED`，功能无影响 | PASS（符合预期） |

数据文件：`trace_logs/wake_cycles.log`（1271 行）、`trace_logs/ws_watch.log`、`trace_logs/kill_recovery.log`、`trace_logs/reset_boot.log`、`logs/printshop.log`（access log）。
新增验收工具（仓库内）：`tools/poc/reset_capture.py`（RTS 脉冲复位+抓启动）、`capture_ts.py`（时间戳捕获）、`wake_cycles.py`（唤醒时延×N 自动测）、`ws_watch.py`（WS 事件记录）、`run_server.py`（无托盘源码服务，**需 PYTHONPATH=仓库根运行**）。

## 2. 备注（非阻塞）

- 唤醒时延含服务端 wake 端点内**同步微信换码 ~2.4-2.6s**（jslogin+qrcode），设备侧真值更短；如在意可改预刷新/异步，另行优化。
- 串口 `[perf]` 打印存在同毫秒聚簇（打印排队），轮询真实节拍以 access log 为准：1.5s/5s 干净两档。
- 熄屏判定由服务端 `_check_idle_sleep` 1.5s 节拍量化，off_after 实测 185.8~191.5s（≈180s+节拍+一拍），正常。
- 档4 iTWT 在本店路由器上不可用属预期形态③；换 WiFi 6 路由器后可重试，`ITWT_ENABLE 1` 保持开启即可自动协商。

## 3. 遗留物理项（需人工）—— 2026-09-16 全部通过

- [x] **亮屏目检**：验收期 10 次自动亮屏（drawQR 208ms）需人工确认无花屏/残影（§5.3 最后一步）。**2026-09-16 通过，无花屏/残影。**
- [x] **扫码全链路 3 轮**：手机扫码→确认→打印→完成服务，各环节画面反馈与改造前一致（§2.4 步骤 2 后半）。**2026-09-16 通过。**
- [x] **隔墙 ping**：屏在隔墙位置 `ping -n 200` 0 丢包（§3.3 后半）。**2026-09-16 通过。**
- [x] **拔网线/断网转离线**：停服/断网 >12s 管理页转离线（逻辑已有单测覆盖，可顺带目验）。**2026-09-16 通过。**

## 4. 发布待办（决定后执行）

1. ~~`dist\printshop.exe` 重打包~~ **已完成 2026-09-09 16:40**：PyInstaller 重打包 + `smoke_dist.ps1 -SkipBle` 冒烟通过（4 端点 200）；新 exe（PID 23448）已作为生产实例运行，屏 5s 节拍轮询正常，WS 确认 `thr=12.0`、电池 `stale_after=180`。自启 Run 键无需改动（仍指向 dist\printshop.exe，已即新版）。
2. ~~源码版服务~~ 已停止（冒烟前腾端口），验收期间使用的 `tools/poc/run_server.py` 仅作留存工具。
3. ~~§9 A/B 电池放电测量~~ **已完成 2026-09-16**：以多日日志版替代受控单次放电（覆盖 09-07~09-16 共 4 段会话、含 100%→18% 完整放电），结论定稿见 `docs/battery-runtime-multiday-2026-09-16.md`——**纯电池可用续航 15~18 小时，10h 营业日仅耗 45~55%**，不再需要 USB 常插；≤30% 建议即充电。
4. M-A1（重编 arduino libs 开 CONFIG_PM_ENABLE）仍阻塞于缺 Linux 构建机；方案 B（BLE 门铃）继续暂缓——按 09-16 续航定稿，A 方案已满足整日营业，**B 的 Go/No-Go 关口倾向 No-Go（不需要）**，待店主最终确认。

## 5. 回退速查

同 stage1 交接 §5；本机另存 20dBm 基线构建 `C:\arduino-build\print_screen_baseline`（源码副本 `C:\arduino-build\baseline_src\print_screen`）可供再次对比。
