# 交接文档：Task 10B S2~S4 主控 BLE 链路 + 打包部署验收（2026-08-26）

> ⚠️ 本交接已过时：2026-08-27 起传输通道退回 WiFi 现役，并完成 ①-⑤ 产品修改、GUI 化与日志化。**以 `docs/handoff-2026-08-27-task10b-wifi-gui.md` 为当前交接文档。** 本文保留 S2-S4 BLE 历史与长测数据备查。

> 写给后续会话：本文档打包 2026-08-25~26「Task 10B S2 主控 BLE 接入 / S3 省电调参 /
> S4 打包、停机窗口部署与长测启动」的完整上下文。项目主计划在
> `docs/superpowers/plans/2026-08-20-wechat-self-print.md`（Task 10B 段，修订记录之六~之八），
> 上一份交接（Task 9 端到端联调）在 `docs/handoff-2026-08-25-task9-e2e.md`，
> S0 spike 实测数据在 `docs/debug-logs-2026-08-25/task10b-s0-spike.md`。

## 一句话现状

Task 10B **S2（主控 BLE Central 链路）、S3（面板初始化降档 120ms）、S4 大部分（打包/冒烟/
停机窗口部署/金丝雀验收/RSSI 基线）已完成**；pytest **229 绿**；新 BLE 版 exe 已在生产运行
（2026-08-26 09:53 启动，PID 87200/90856，IDLE 息屏、HTTP 200）。
**未完成（照实列为待办，勿勾掉）**：frozen `--ble-selftest` 正式 PASS 验收（待下次停机窗口，
须先静置 5 分钟避开僵尸连接）、24h 待机长测（09:53 起计时中）、断连恢复/扫码全链路/
2h 烧屏复查/店内距离隔板实测、三工况电流表（无功率计挂起）。
**工作区改动尚未 commit**——等用户指令（计划 Task 10B Step S4 Commit 项）。

## 架构：主控 BLE Central 推送模型 vs 旧 WiFi 轮询

- **旧模型（WiFi 轮询，现为回滚变体）**：固件 WiFiManager 配网 → 每 1.5s `GET /api/screen/state`
  拉状态 → 电量随 query 捎带。问题：空闲期屏幕供电/射频全活，功耗高；唤醒只能靠触摸。
- **新模型（现役 TRANSPORT_BLE）**：主控是 BLE **Central**（winrt 直连，不走 bleak——S0 定论：
  其 Windows connect 路径冷连接 5~13s 不可用），固件是 **Peripheral**——空闲只广播不连接；
  主控有状态变化（sm.on_change / wake / finish）才**主动连接**并推 `screen_state`，固件渲染后
  notify `link_status` 作绘制完成 ack（fw_ms 延迟可测）。
- **空闲功耗形态**：面板 ALDO3 彻底断电、WiFi 关闭、100ms 广播、**CPU 常醒**（低功耗路线 A，
  估 8~20mA；路线 B「重编 esp32-arduino-libs 开 `CONFIG_PM_ENABLE`+`CONFIG_BT_LE_SLEEP_ENABLE`」
  挂后续，需功率计）。
- 固件侧：`firmware/print_screen/{ble_link,transport_config}`（契约见 `firmware/README.md` §十）；
  主控侧：`printshop/ble_link.py`；spec §6.1 已同步维护态语义与推送模型。

## BleScreenLink 行为语义（printshop/ble_link.py）

- **懒建连**：display=on 首次推送才建连；display=off 未连接时纯丢弃不建连（省电核心）。
- **120s 快速重醒窗口**（主控侧计时，`ble_fast_rewake_seconds`）：息屏后保持连接（面板仍断电），
  窗口内再亮屏免建连（仅面板冷启动 653ms）；到期主动断开回广播。固件侧无此逻辑。
- **15min 周期心跳**（`ble_heartbeat_seconds=900`）：空闲期未连接 → 建连 → 读 link_status →
  等电池首包（3s 窗口）→ 断开；心跳连接走 `_pending_conn`，不与 worker 写屏争用句柄。
- **latest 覆盖合并**：`push_state` 仅覆盖 `_latest` 并唤醒单 worker，串行消费，忙时合并不逐条写。
- **建连失败自动重试**：display=on 不丢——保留 latest，**5s 退避**（`_CONNECT_BACKOFF`）到期
  自动重试（防"点了没反应"黑屏直到下个状态变化）。
- **winrt 直连**：`from_addr(PUBLIC)` → `get_gatt_services_for_uuid_async` 重试建连
  （0x80070016 时 0.2s 间隔最多 ~30 次）→ 特征枚举 → 写 CCCD 订阅 battery/link_status 两个
  notify → 返回连接句柄。winrt 回调在 WinRT 线程触发，回 asyncio 用 `call_soon_threadsafe`。
- **电池首包竞态修复（实机缺陷）**：固件 CCCD 订阅后 ~30ms 即补发电池首包，回调必须由
  `_winrt_connect` 在写 CCCD **之前**挂好；曾因连接返回后才挂导致首包必丢、心跳 3s 窗口等不到
  周期包 → 电池上报实际不可用。回归单测钉死。
- **生命周期**：`stop()` 清 worker/heartbeat/`_pending_conn`/`_conn`；pytest 环境闸
  （`_in_test_env`，sys.modules 含 pytest 拒真实建连）；FakeConnector 同协议（`_Conn` 五方法）
  单测不碰实体屏。

## 配置与回滚

- `config.py`：`screen_ble_address`（默认 `20:6E:F1:16:D9:2E`）、`screen_ble_name`（`PRINT-SCREEN`，
  仅核对不阻断，不符仅 warning）、`ble_fast_rewake_seconds=120`、`ble_heartbeat_seconds=900`。
- **回滚开关（部署用）**：env `PRINTSHOP_SCREEN_BLE_ADDRESS=off`（或 `none`，或 config 空串）→
  服务侧 BLE 禁用（ble=None 分支走 WiFi 轮询）；固件侧烧 `-DTRANSPORT_WIFI` 变体
  （`transport_config.h` 默认 TRANSPORT_BLE，`--build-property` 注入覆盖；注入时必须保留
  esp32 core `build.extra_flags` 默认全量宏，只传一个宏 WiFiManager 编译崩——2026-08-25 实踩）。
- 生产实例数据目录在 `dist/`（db/state.json/inbox/work）；源码调试 `-m printshop.main` 项目根 cwd。

## 首次绑定与换主控/换屏

- **绑定**：改 `config.py screen_ble_address` 或设 env 为小屏 public MAC（本机 `20:6E:F1:16:D9:2E`）。
- **换屏**：烧录后从固件启动日志读新 MAC（`[ble] MAC=...`）更新配置。
- **换主控**：新机配置同地址即可——public 地址直连、无配对绑定。
- 查看小屏 MAC：串口日志，或 `tools/poc/ble_spike_pc.py scan`。

## 运维入口

- **`--ble-selftest`**（main.py 分支 → `ble_link.run_ble_selftest`）：等**己方连接就绪**（12s，
  须盖过「首次建连失败 + 5s 退避 + 重试」）→ 等电池首包（8s）→ 读 link_status 打印 → 亮占位屏 →
  息屏 → `BLE SELFTEST PASS`/退出码 0，任一步失败 FAIL/1，不向顶层抛栈。
  **设计缘由（2026-08-26 实机踩坑）**：强杀旧进程后的**僵尸连接**会把残留订阅的电池包转发给
  新进程，"收到电池"不能当建连证据——必须先等己方连接就绪。
- **`scripts/smoke_dist.ps1`**：HTTP 探活（/admin /screen /api/screen/state /static/screen.html）→
  强杀 HTTP 实例 → **独立进程**跑 `--ble-selftest` → 汇总；`-SkipBle` 回退。注意：
  ①强杀按**父子关系**（ParentProcessId）清 onefile 子进程，**绝不按进程名扫杀**——会误杀生产实例
  （主代理审阅修复）；②脚本必须保留 **UTF-8 BOM**（PS 5.1 无 BOM 按 GBK 解析会语法错误）；
  ③跑前确认无生产 printshop.exe 在跑（两个 BLE Central 抢小屏互相干扰）。
- **电池指示**：`/api/screen/state` 的 `battery.stale_after` 由后端下发——BLE 模式
  = 心跳间隔 + 300s（900+300=**1200**），WiFi 轮询回退 = 180s；admin.html 按此置灰（兼板子在线指示）。

## 僵尸连接运维注意（强杀场景）

- Windows 强杀进程后底层 BLE 连接**延迟回收**，残留存活 **8 分钟以上**（远超 S0 测的 4~18s——
  **建连尝试会给残留续命**），期间新进程特征枚举空、首连可能失败。
- S2 的 5s 退避自动重试可恢复（2026-08-26 晨隔夜首连 `from_addr` 返回 None 即被退避重试接住）。
- **验收/建连前**：停生产后**静置 5 分钟完全不碰 BLE**（含探针——`close()` 也制造残留）再操作。

## 实测数据汇总

- **冷唤醒全链路：3.3s → ~2.85s**（S3 面板初始化 600→120ms 降档：fw_ms 逐档恒 733/683/653ms，
  三档 e2e 10/10 全过、串口零异常，选定 **120ms**=SH8601 规格典型值，宏 `SH8601_SLEEP_OUT_DELAY_MS`）。
- **快速重醒窗口**：免建连，仅面板冷启动 ~0.65s。
- **两段式写**（降档后抽验）：空 qr 占位冷启动 567ms → qr 覆盖 132ms → 同码重推守卫跳过 0ms。
- **RSSI 基线**（当前摆位，屏贴机柜）：**avg -34.8 / min -40 / max -31 dBm**；广播 100ms
  到达率 **3.08 pkt/s**（12s 37 包）。
- **金丝雀（frozen 验收最终证据）**：生产 exe 09:53 启动，**首轮周期心跳电池上报 10:08:31 到达**
  （`/api/screen/state` 返回 `battery:{pct:100, mv:4143, stale_after:1200}`）——frozen winrt
  建连/订阅/notify 全链路生产环境实证。
- **打包**：`printshop.spec` 增 **9 个 winrt hiddenimports** + `collect_dynamic_libs("winrt")`
  （兜底 msvcp140.dll）；`collect_submodules("winrt")` 对命名空间包无效（实测）；exe 57.9→59.0MB。
- **测试**：pytest 229 绿（226 + selftest 硬化/重试恢复新增）。

## 停机窗口时间线（2026-08-26 09:44~09:53）

- 09:44 强杀旧生产（`Stop-Process -Force`，**避免优雅退出触发 finish_service 登出微信**）→
  重建 exe → 09:53 恢复生产（`Start-Process`，等价 HKCU Run 键 `PrintShop` 登录自启）。
- 生产现态：PID 87200/90856，IDLE 息屏，HTTP 200。

## 24h 长测结果（2026-08-26 09:53 ~ 08-27 09:53，已完结）

**主控/生产服务侧——全勤**：24h 内 BLE 链路、HTTP、WS 全程正常。期间三次进程事件均非软件故障：
09:53 初始部署 → **17:57 PC 意外重启**（~4 分钟服务断档；根因：HKCU Run 自启项缺失，已运行
`scripts/install_autostart.ps1` 修复，18:01 手动拉起）→ 20:15 用户要求再重启一次。frozen 金丝雀
（10:08 心跳电池）、RSSI 基线、frozen 打包全部在真实生产环境验证通过。电池遥测经心跳持续
10:08/12:39/15:55/16:40/18:31，后因小屏离线而停。

**小屏/供电侧——两次断电 = 硬件接触不良，非软件**：
- 放电曲线（纯电池）：10:08=100%(USB) → 12:39=91% → 15:55=78% → 16:40=74% →
  **16:49 第一次断电**（表显 74%）→ 17:44 插电恢复（74% 充电）→ 17:55 用户刻意拔线纯电池 →
  18:31 最后遥测 75% → **08-27 02:59 第二次断电**（75% 再跑 7~9h）→ 08-27 09:0x 用户重插电池
  连接器 + 接 USB → 恢复（pct=0/3349mV/充电中/vbus=true，display=on 二维码，自动重连）。
- **根因**：AXP2101 `isBatteryConnect()` 间歇性 flap false（电池检测线/连接器接触不良）→ 固件
  静默跳过电池上报（`ble_link.cpp:185` + `axp2101_bsp.cpp:128`）。两次断电也是同一接触不良所致
  （表显 74% 时瞬间断供，插电恢复后电池仍 74%）。用户重插连接器后遥测恢复，坐实硬件问题。
- **纯电池实测续航 ≈ 9~12 小时**（100%→断电约 6.7h 受接触干扰、75%→7~9h 较纯）。路线 A
  （CPU 常醒）实测**证伪"2~5 天"预估**。

**结论与建议**：①店内小屏形态应为 **USB 常插、电池当断电备份**（纯电池撑不满一个工作日）；
②路线 B（重编 esp32-arduino-libs 开 `CONFIG_PM_ENABLE`+`CONFIG_BT_LE_SLEEP_ENABLE`，目标 1~3mA）
  若需电池长续航有意义，但**功率计到位前调优无反馈回路**——建议功率计到位后优先排期；
③固件待办：**BLE 模式开机显式关 ALDO3**（防断电恢复后白屏，08-26 17:44 实况）；可选"电池未接入"
  上报而非静默（让后台电池指示明确）。

## 待办清单（更新于 2026-08-27 汇总，部分已验证）

- [x] **24h 待机长测**（主机侧全勤；小屏侧为纯电池续航+供电可靠性测试并暴露了硬件问题）
- [x] **断连恢复**（两次真实事件验证：16:49→17:44、02:59→09:07 断电后插电自动恢复重连）
- [x] **exe 进程重启重连**（08-26 历经 3 次进程重启，frozen 建连均正常；冻结态首连已由金丝雀生产实证）
- [ ] **frozen `--ble-selftest` 正式 PASS 验收**：仍未做——建议下次停机窗口（停生产后**静置 5 分钟
      完全不碰 BLE** 再跑；注：僵尸连接干扰下曾误报 FAIL，selftest 已硬化但未在干净环境复测）
- [ ] **扫码全链路**（S2 V2 冒烟为 mock wx，尚无真实顾客扫码→打印验证）
- [ ] **店内距离/隔板实测**（仅 RSSI 基线 -35dBm 屏贴机柜；真实距离/隔板未测）
- [ ] **多设备干扰**（24h 内无干扰迹象，但无刻意 staging）
- [ ] **PC 睡眠恢复**（08-26 18:01 的 PC 重启覆盖了"重启后恢复"路径，但"睡眠→唤醒"未直接测）
- [ ] **2h 烧屏复查**（S3 降档后白天多次冷启动/显示无烧屏迹象，但未做定时 2h 专项）
- [ ] 三工况电流表（空闲广播/快速重醒窗口/服务亮屏）——无功率计挂起。

## 参考

- 计划：`docs/superpowers/plans/2026-08-20-wechat-self-print.md` Task 10B（修订记录之六~之八）
- 固件：`firmware/README.md` §十（BLE 架构/GATT 契约/编译命令）
- spec §6.1（维护态语义/传输契约/主控侧推送模型）
- S0 spike：`docs/debug-logs-2026-08-25/task10b-s0-spike.md`
