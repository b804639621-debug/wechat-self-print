# Task 10B Step S0 — BLE 可行性 spike 测量报告（2026-08-25）

测试环境：主控机（本机 192.168.31.171，MediaTek 蓝牙适配器，Windows， bleak 3.0.2 / winrt 3.x）；
小屏 ESP32-C6-Touch-AMOLED-2.16，固件 `firmware/ble_spike`（ble_spike-0.1，NimBLE 栈）。
工具：`tools/poc/ble_spike_pc.py`（bleak 路径）、`tools/poc/ble_probe_stages.py`（winrt 分段/e2e 探针）。
测试距离：桌面并排，RSSI ≈ -43。

## 一、Go/No-go 门槛逐项

| 门槛 | 实测 | 判定 |
|---|---|---|
| 连接成功率 ≥90% | **100%**（55+ 轮连接/断开循环，零失败） | ✅ PASS |
| MTU 协商 ≥185 | **每次均 517**（本地拉满，WinRT 侧协商一致；49 字符二维码 JSON 一次写成功） | ✅ PASS |
| 冷唤醒典型 ≤2.5s、最坏 ≤3s | **典型 3.3s / 最坏 4.1s**（详见第二节分解） | ⚠️ 超门槛，有明确优化路径（第四节） |
| 空闲平均电流 ≤3mA | **阻塞，未测**：AXP2101 无放电电流 ADC（XPowersLib 0.3.3 实查无 API），需外接 USB 功率计串电池线 | ⏸ 待硬件 |
| 主控进程重启后首次连接 | 每次测量均为新 Python 进程首连，全部成功（含 500/100/50ms 三档广播） | ✅ PASS |
| 电池 notify 链路 | 连接期间每 5s 上报 `{pct,mv,chg,vbus}`，link_status read 正常 | ✅ PASS |

## 二、冷唤醒全链路分解（生产路径原型，100ms 广播，e2e 15 轮）

| 阶段 | avg | min | max | 说明 |
|---|---|---|---|---|
| from_bluetooth_address | 0.07s | 0.06 | 0.09 | 设备对象创建，可忽略 |
| 建连 + for_uuid 服务发现 | **2.04s** | 1.81 | 2.76 | Windows 冷建连固定 ~2s（见第三节②） |
| CCCD 订阅（notify 使能） | 0.07s | — | — | 建连后 ATT 操作毫秒级 |
| write display=on | 0.03s | — | — | 同上 |
| 固件 ALDO3 上电+面板冷启动+画码 | **1.13s** | 1.13 | 1.14 | ALDO3 复位 300ms + 初始化命令表 ~700ms（0x11 后固定 600ms）+ 绘制 ~130ms |
| **全链路合计** | **3.35s** | 3.12 | 4.06 | 计划预算 1.5~2.5s，实测超 ~0.8s |

快速重醒窗口（保持连接，免建连）：write 0.03s + 固件 1.13s ≈ **1.2s**，达标。

## 三、瓶颈分析（关键架构结论）

### ① bleak 在 Windows 上的标准 connect 路径不可用（冷连接 5~13s）

`BleakClient.connect()` 内部固定流程 = 广告 watcher 扫描等待（2~9s，watcher 冷启动开销）
→ GattSession.maintain_connection 等 ACTIVE 事件（0.2~9.6s，不稳定长尾）
→ GATT 全服务发现（~1.5s）。20 轮延迟测量 conn 段 avg 7.5s / max 13.1s。

**结论：S2 主控 `ble_link.py` 不走 bleak，直接基于 winrt API**：
`from_bluetooth_address` → `get_gatt_services_for_uuid_async`（查询触发建连；连接未就绪时
Windows 立即报 0x80070016，0.2s 间隔重试 ~10 次即成功——实测稳定 ~2s）。
notify/write 用 winrt 原生 API（`add_value_changed` / `write_value_with_result_async`）。

### ② Windows 冷建连 ~2s 是固定系统行为，与广播间隔无关（≤100ms 时）

| 固件广播间隔 | 建连段实测 |
|---|---|
| 500ms | 4~9.6s（等广播包，不可接受） |
| 100ms | ~2.0s（饱和点） |
| 50ms | ~2.0s（无进一步改善） |

**计划原定"广播 500ms"须下修到 100ms**；更密广播只增功耗不提速。

### ③ Windows 不缓存未配对（unbonded）设备的 GATT 服务

`BluetoothCacheMode.CACHED` 实测无效（仍 ~1.5s 全枚举）。对策：用
`get_gatt_services_for_uuid_async` 只查自定义服务——连接建立后仅 ~65ms。

### ④ esp32c6 @ core 3.3.3 内置 BLE 栈是 NimBLE（非 Bluedroid）

sdkconfig：`CONFIG_BT_NIMBLE_ENABLED=y`、`CONFIG_BT_BLUEDROID_ENABLED` 未开。
回调签名走 NimBLE 分支（`onMtuChanged(BLEServer*, ble_gap_conn_desc*, uint16_t)`）。
NimBLE 更轻量，低功耗前景好于计划假设的 Bluedroid。

### ⑤ light-sleep + BLE 广播共存在 Arduino 默认环境下不可用（风险②实锤）

sdkconfig 同时未开 `CONFIG_PM_ENABLE` 与 **`CONFIG_BT_LE_SLEEP_ENABLE`**——后者是
light-sleep 期间维持 BLE controller 的开关。可选路线（代价递增）：
- 接受 CPU 常醒 + 广播（估 8~20mA，续航 2~5 天）；
- 重编 esp32-arduino-libs 开上述两项（需 ESP-IDF 构建环境，中等工作量，目标 ~1~3mA）；
- ESP-IDF 原生固件（迁移成本高）；
- 或退回风险⑧ WiFi modem-sleep 中间态（5~15mA，不动 Windows 侧）。
**需电流实测数据后才能定路线**——功率计为第一优先。

## 四、超门槛项的优化抓手（S3 既定 + 新增）

1. 面板初始化命令表 0x11 后 600ms → 数据手册最低 ~120ms（S3 逐档实测无花屏才降）：省 ~480ms
2. ALDO3 复位序列 100×3=300ms → 可压缩（S3 实测）：省 ~100~150ms
3. 以上落地后全链路 ≈ 2.1s（PC 建连）+ 0.5s（固件）≈ **2.6s，贴门槛**；快速重醒窗口内 ~0.6s
4. 建连 2s 的进一步压缩只有 bonding 一条未验证路径（Windows 对已配对设备的服务缓存/连接路径
   可能更快，收益不确定，改动涉及固件 NimBLE security + Windows 配对管理，建议列为可选 spike）

## 五、复现

```bash
# 固件（当前板内为 100ms 广播基线）
tools/arduino-cli/arduino-cli.exe compile --fqbn "esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB" --build-path "C:/arduino-build/ble_spike" firmware/ble_spike
tools/arduino-cli/arduino-cli.exe upload -p COM3 --fqbn ...同上... --input-dir "C:/arduino-build/ble_spike" firmware/ble_spike

# PC 侧
.venv/Scripts/python tools/poc/ble_spike_pc.py scan                      # 找 PRINT-SCREEN 地址
.venv/Scripts/python tools/poc/ble_probe_stages.py <MAC> 10 --e2e --gap5 # 全链路延迟（生产路径原型）
.venv/Scripts/python tools/poc/ble_probe_stages.py <MAC> 8 --direct      # 分段瓶颈
.venv/Scripts/python tools/poc/ble_spike_pc.py battery --addr <MAC>      # 电池 notify
```
