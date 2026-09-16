# WiFi 省电改造 · 阶段一实施交接（2026-09-07 计划首阶段）

> 计划源文件：`docs/plan-2026-09-07-wifi-power-save.md`
> 本文件记录**已落地的源码改动、验证结果、未完成项与回退路径**，供下一班继续实机验收与 M-A1/B 决策。

## 1. 本次已实施内容

### 固件（`firmware/print_screen/print_screen.ino` + 文案）
- 档1 modem sleep：`WiFi.setSleep(false)` → `WiFi.setSleep(true)`（MIN_MODEM/DTIM）；`WIFI_MAX_MODEM_SLEEP`/`WIFI_LISTEN_INTERVAL` 为默认关闭的实验宏。
- 档2 自适应轮询：新增 `POLL_IDLE_INTERVAL_MS=5000`；`loop()` 按 `screenOn` 在 1.5s/5s 间切换，`failCount>0` 仍走 5s 退避。
- 档3 TX 降功率：开机按 RSSI 选 `WIFI_POWER_11dBm`（RSSI ≥ -68）或 `WIFI_POWER_17dBm`。
- 档4 iTWT：include `esp_wifi_he.h`/`esp_event.h`；连接前声明 11ax+HE20；注册 ITWT SETUP/TEARDOWN/SUSPEND/PROBE/STA_CONNECTED 事件；连接后尝试协商，失败静默回退 DTIM。**适配已装 IDF v5.5.1：成功判据用 `ESP_OK`（该版本没有 `ITWT_SETUP_SUCCESS`）。**
- 档5 CPU 80MHz：连接成功后 `setCpuFrequencyMhz(80)`。
- 方案 A2 源码骨架：`PM_LIGHT_SLEEP` 默认 1（可用 `-DPM_LIGHT_SLEEP=0` 构建不睡版）；连接成功后调用 `esp_pm_configure({160,80,light_sleep_enable=true})` 并打日志。**当前官方预编译库 `CONFIG_PM_ENABLE` 未开，实机预期日志为 `ESP_ERR_NOT_SUPPORTED`，系统按现状跑；重编 libs 后同一源码自动生效。**
- 文案同步：`print_screen.ino` 头注释、`transport_config.h`、`firmware/README.md` 已去掉“USB 常供电不省电”，新增省电宏速查、PM 双构建命令与 PC 阈值联动说明。

### PC（`printshop/`）
- `config.py` 新增 `screen_online_threshold_seconds: float = 12.0`。
- `routes.py`：删除 `AppContext` 类属性硬编码 10.0，改由 `cfg` 注入；电量置灰注释同步更新，逻辑不变。
- 测试：新增阈值默认值/自定义值/12.0s 边界判定测试；原 `threshold_seconds==10.0` 断言更新为 12.0。

### 工具链 M-A0
- 报告：`docs/toolchain-m-a0-report-2026-09-07.md`。
- 事实：arduino-cli 1.5.1，esp32 core **3.3.3**，预编译库 **IDF v5.5.1-418-gf1a1df9b2e**；`CONFIG_PM_ENABLE` 未开、`CONFIG_FREERTOS_USE_TICKLESS_IDLE` 未开、`CONFIG_BT_LE_SLEEP_ENABLE` 未开。
- 官方 `esp32c6` 预编译库已备份：
  `C:\arduino15\packages\esp32\tools\esp32-arduino-libs\idf-release_v5.5-f1a1df9b-v3\esp32c6.orig-2026-09-07`
- M-A1 阻塞：本机无 WSL/Linux/Docker；GitHub 可达，待安装 WSL2+Ubuntu 或另备 Linux 构建机后执行 lib-builder 重编。

## 2. 本次验证结果

| 项目 | 结果 |
|---|---|
| PC 全量 pytest | **306 passed**（改造前 302 基线 + 新增 4 条） |
| 固件默认 WiFi 省电版编译 | 通过：Flash 1312646 B (39%)，RAM 44684 B |
| 固件 `-DPM_LIGHT_SLEEP=0` 编译 | 通过：Flash 1312530 B，RAM 44684 B |
| 固件 `-DTRANSPORT_BLE` 回归编译 | 通过：Flash 834952 B (24%)，RAM 24940 B |
| 实机烧录/功能回归 | **未做**（本阶段无实体屏/主控接入） |

## 3. 下一班必须做的实机验收（按计划）
1. §1.4：modem sleep 后 HTTP 轮询节奏/超时回归；ping 0 丢包、max ≤600ms。
2. §2.4：熄屏态 20 分钟管理页在线不闪烁；托盘 wake 10 次 ≤7s、典型 ≤5.5s；扫码全链路 3 轮。
3. §3.3：`ping -n 200` 0 丢包、平均 RTT 与 20dBm 基线差 <30%；隔墙一轮。
4. §4.4：串口确认 `[twt]` 三行结果（HE20 跳过 / ESP_OK / 拒绝/超时），轮询后续正常。
5. §5.3：80MHz 下亮屏重绘 10 次目检无花屏；重绘耗时对比。
6. §A3：若先完成 M-A1 重编 libs，烧 PM 版做 2h 功能回归 + 24h WiFi 保连 + 电流量化。

## 4. 未启动项与决策关口
- **方案 B（hybrid/BLE 门铃）按计划 §4 的 Go/No-Go 关口暂缓**：需 A 全量实测数据出来后再决定。B 的固件状态机/PC 三态改动均未开始。
- **M-A1 lib-builder 重编未执行**：本机缺 Linux 构建环境；见 M-A0 报告下一步命令。
- **`dist/printshop.exe` 未重打包**：PC 端阈值改动需随固件同批发布时再执行 `PyInstaller printshop.spec` + `scripts\smoke_dist.ps1`。

## 5. 回退速查
- 档1：`WiFi.setSleep(true)` 改回 `false`（或折中：仅熄屏睡眠）。
- 档2：`POLL_IDLE_INTERVAL_MS` 改回 1500；PC 阈值可回 10.0。
- 档3：两宏改 `WIFI_POWER_20dBm` 或删 setup 段。
- 档4：`ITWT_ENABLE 0` 一行；或删 11ax 协议声明段。
- 档5：删 `setCpuFrequencyMhz(80)` 一行。
- 方案 A2：`-DPM_LIGHT_SLEEP=0` 构建不睡版；或还原 `esp32c6.orig-2026-09-07` 备份目录。
- 整体：对应提交 `git revert` 后重烧 + 重打包 exe。
