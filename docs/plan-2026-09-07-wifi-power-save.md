# WiFi 方案省电 · 一二三档详细实施方案

> 制定日期：2026-09-07。本文件为**纯规划文档，尚未实施**。
> 范围：第一档（modem sleep、闲时拉长轮询）、第二档（降 TX 功率、TWT、CPU 降频）、第三档全部（A. Auto light sleep + WiFi 保连；B. 混合架构 WiFi 断开 + BLE 唤醒门铃）。
> **永久排除项：熄屏/空闲期切 ALDO3 面板电源——会导致绿屏，任何档位均不得启用、不得复用 BLE 变体中的 ALDO3 断电语义。**
> 文中行号以 2026-09-07 工作区快照为准；TWT/PM 相关 ESP-IDF API 已对照官方头文件与例程核实。

---

# 第一部分：第一、二档实施方案

## 0. 现状与目标

现役 `TRANSPORT_WIFI` 固件（`firmware/print_screen/print_screen.ino`）**无任何省电措施**：`WiFi.setSleep(false)`（:360）、1.5s 恒定轮询（:198）、CPU 160MHz 全速、TX 满功率、无 TWT。USB 常插形态下无所谓，纯电池形态参考基线（2026-08-28 实测放电记录 `battery_history.csv`）：**93%→22% 用 6.7h ≈ 10.6%/h，全续航约 9.4h**（该次未控制屏幕状态，正式 A/B 需重跑受控基线）。

目标组合改造：开 modem sleep（DTIM）→ 熄屏闲时轮询 5s → TX 降功率 → CPU 降 80MHz → 尝试 iTWT。**明确排除：熄屏切 ALDO3 面板电（绿屏，永久放弃）**——熄屏仅"软熄屏"（DISPOFF+黑屏），屏功耗不在本次优化范围，省电收益全部来自 WiFi 射频 + CPU。

改动清单总览：

| # | 文件 | 位置（实核行号） | 内容 |
|---|---|---|---|
| T1 | `print_screen.ino` | setup() :360 | `setSleep(false)`→`setSleep(true)`（MIN_MODEM） |
| T2a | `print_screen.ino` | 宏区 :195-203、loop() :377-382 | 新增 `POLL_IDLE_INTERVAL_MS`，熄屏 5s / 亮屏 1.5s 自适应 |
| T2b | `printshop/config.py` + `routes.py` | config.py:16 附近、routes.py:78-102 | 在线阈值 10.0→12.0 并挪进 Config |
| T3 | `print_screen.ino` | setup() :366 后 | `WiFi.setTxPower(WIFI_POWER_11dBm)` 按 RSSI 起测 |
| T4 | `print_screen.ino` | setup() :359-365、新增回调 | 声明 11ax/HT20 + `esp_wifi_sta_itwt_setup()`，失败静默 |
| T5 | `print_screen.ino` | setup() :366 后 | `setCpuFrequencyMhz(80)` |

各档改动相互独立、可单独回退（见 §7 顺序与依赖表）。

实施前唯一建议的补查：确认工具链实际安装的 arduino-esp32 核心版本（决定其捆绑 IDF 是否为 5.3，从而决定 `esp_wifi_he.h` 是否存在；README 有"esp32 core 3.3.x / 3.3.3"记录，3.3.x 对应 IDF 5.3，风险低，编译一步即可证伪）。

---

## 1. 第一档：开 modem sleep（DTIM）

### 1.1 改动

**文件**：`firmware/print_screen/print_screen.ino`，setup() 内 `:359-360`。

改动前：
```cpp
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);        /* 保持 WiFi 活跃，轮询不抖动 */
```
改动后：
```cpp
  WiFi.mode(WIFI_STA);
  /* 省电档1（modem sleep / DTIM）：曾因"轮询不抖动"刻意关（handoff-2026-08-27:86）。
   * 本项目轮询为 STA→AP 上行主导（GET），下行仅小响应，DTIM 缓冲导致的
   * 额外延迟 ≤ 数个 beacon(100ms 级)，对轮询节奏无感，故恢复睡眠；
   * 若回归实测亮屏期抖动不可接受，可退化为"仅熄屏睡眠"（见 §1.5 回退 B）。 */
  WiFi.setSleep(true);         /* == esp_wifi_set_ps(WIFI_PS_MIN_MODEM)，Arduino 默认即听每个 DTIM */
```

### 1.2 是否需要 `esp_wifi_set_ps` 直调？

**不需要**。Arduino-esp32 `WiFi.setSleep(true)` 内部即 `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)`，语义一致。保留 Arduino API 便于回退。MAX_MODEM 才需要直调（见下）。

### 1.3 MIN_MODEM vs MAX_MODEM + listen_interval 取舍

| 项 | MIN_MODEM（选为默认） | MAX_MODEM + `esp_wifi_set_listen_interval(n)` |
|---|---|---|
| 行为 | 每 DTIM 醒来收 beacon/下行 | 每 n 个 DTIM 醒一次 |
| 下行延迟 | ≤ DTIM×100ms（约 100~300ms） | ≤ n×DTIM×100ms |
| 平均射频电流 | 中 | 更低（n=2~3 可再省若干 mA） |
| 风险 | 低 | 小米路由器 DTIM 未知；n 过大时 AP 侧保活/ARP 老化、掉线感知变慢；若 AP 广播风暴，省电被稀释 |

**结论**：默认 `WiFi.setSleep(true)`（MIN_MODEM）。MAX_MODEM 做成**编译期可选实验**，不默认开：

```cpp
/* 宏区（§1.1 附近）可选实验档 —— 默认注释，实测 MIN_MODEM 电流不达预期再开 */
// #define WIFI_MAX_MODEM_SLEEP 1
// #define WIFI_LISTEN_INTERVAL 2   /* 单位：beacon 周期(100ms) */

#if defined(WIFI_MAX_MODEM_SLEEP)
  esp_wifi_set_ps(WIFI_PS_MAX_MODEM);
  esp_wifi_set_listen_interval(WIFI_LISTEN_INTERVAL);
#else
  WiFi.setSleep(true);
#endif
```
（需在 .ino 顶部已 `#include <esp_wifi.h>`——`WiFi.h` 已传递包含，可直接用。）

### 1.4 验证步骤（轮询抖动回归 = 本档唯一风险点）

1. 编译烧录（§8 命令）后开串口监视器 115200，观察 `[net] WiFi connected, IP=...` 后轮询日志正常、无 `[http] 主控不可达`。
2. **轮询节奏回归**（重点，对应旧注释"轮询不抖动"）：临时插桩测 HTTP 往返，可选加宏 `-DWIFI_DEBUG_PERF`（实现见 §2.4 插桩）打印每轮 `[perf] poll=NNNms`，对比改前（`setSleep(false)` 基线）与改后：**允许平均 +50~200ms、偶发 +300ms 级延迟**（DTIM 缓冲属预期）；不允许出现 HTTP 超时（2.5s）或连续 failCount 递增。
3. PC 侧 ping 延迟观察（副证）：`ping 192.168.31.171 -t`，modem sleep 下 RTT 出现周期性 +100~300ms 尖峰属正常（beacon 节拍），要求 **0 丢包、max ≤ 600ms**。
4. 功能回归：亮屏态 10 分钟观察轮询 OK；`POST /api/screen/wake` 托盘点亮正常；主控 kill 后 ≤5s 进"系统维护中"，恢复后回正常画面。

### 1.5 风险与回退

- **回退 A（单行）**：`setSleep(true)` 改回 `setSleep(false)`。
- **回退 B（折中，保留大半收益）**：按显示状态切换——`doScreenOn()`（:323-348）末尾 `WiFi.setSleep(false)`、`doScreenOff()`（:304-321）末尾 `WiFi.setSleep(true)`。代价：亮屏期（营业）回到无睡眠，仅熄屏期省电。**先做回退 A 即可，B 仅在实测亮屏期抖动/超时确有问题时启用**；B 依赖 `esp_wifi_set_ps` 可在任意时刻调用，无额外风险。
- 若实测发现 DTIM 过长（下行延迟 >1s 级，小米系少见），日志确认后转 §1.3 实验档或回退。

---

## 2. 第二档：熄屏闲时自适应轮询（1.5s ↔ 5s）

### 2.1 固件侧改动

**文件**：`firmware/print_screen/print_screen.ino`

**(a) 宏区（:195-203 附近）**——改动后：
```cpp
#define SERVER_IP        "192.168.31.171"    /* 主控固定 IP（配网时按店内实际改） */
#define SERVER_PORT      8000
#define POLL_INTERVAL_MS 1500                /* 正常轮询间隔：亮屏期（服务/等扫码引导） */
#define POLL_IDLE_INTERVAL_MS 5000           /* 熄屏闲时轮询间隔（省电档2）：
                                              * PC 侧 screen_online_threshold_seconds 必须 > 2×此值；
                                              * 改大此值需同步改 printshop/config.py 同名字段 */
#define POLL_BACKOFF_MS  5000                /* 主控不可达退避重试间隔（不变） */
```

**(b) 判定逻辑**：`loop()` :377-378，改动前：
```cpp
  unsigned long now = millis();
  unsigned long interval = (failCount > 0) ? POLL_BACKOFF_MS : POLL_INTERVAL_MS;
```
改动后：
```cpp
  unsigned long now = millis();
  /* 自适应轮询（省电档2）："闲" = 屏幕已熄（screenOn=false）。
   * 判定依据解析：
   *  - 亮屏即 display=on（服务中/等扫码引导），PC 端必然处于可能被扫码确认的窗口，
   *    必须保持 1.5s 快轮询（扫码确认→画面反馈 ≤1.5s，与现状一致）；
   *  - display=off 只在 IDLE+无事件 180s(PC) / 360s(本地兜底) 后出现，即"无会话且已熄屏"，
   *    拉长到 5s 不影响任何交互（唯一交互=托盘 wake，最坏延迟见 §2.3）；
   *  - 本机兜底熄屏（IDLE_SLEEP_MS 360s 路径）后 screenOn=false 同样进入闲时档；
   *  - 主控不可达（failCount>0）维持 5s 退避不变（与闲时档巧合同值，无需特判）。
   *  - 不用 curState=="IDLE" 判闲：IDLE 亮屏正是"顾客扫码引导窗口"，需快轮询。 */
  unsigned long interval = (failCount > 0) ? POLL_BACKOFF_MS
                        : (screenOn ? POLL_INTERVAL_MS : POLL_IDLE_INTERVAL_MS);
```

状态机自洽性核对（逐条对照现有代码，均无副作用）：
- `setup()` 置 `screenOn=true`（:368）→ 开机首轮 1.5s 快轮询，拉到 `display=off` 后 `doScreenOff()` 置 false（:307）→ 下一轮起 5s。
- 熄屏兜底补刀：`doScreenOff()` 内部 `FORCE_OFF_RETRY_MS` 60s 守卫（:317）在 5s 轮询下照常每 60s 补一次 DISPOFF，不改变行为。
- 本地兜底熄屏（:389）计时基于 `lastStateChange`/`millis()`，与轮询间隔无关，360s 语义不变。
- 掉线亮"系统维护中"（`onServerDown()` :288-300 强制 `doScreenOn()`）→ `screenOn=true`，但 `failCount>0` 恒走 5s 退避分支，行为同现状。
- 退出闲时档的唯一路径是下一次轮询拉到 `display=on`：间隔 ≤5s（见 2.3 延迟）。

**(c) 可选插桩（验证用，默认关）**：`pollScreen()` 内首尾与 `doScreenOff/On` 各加一行 `#if defined(WIFI_DEBUG_PERF)` 计时打印（`Serial.printf("[perf] poll ok %lums draw=%lums\n", ...)`）。验证完可保留（默认编译不含）或删除。注意与 `Serial` 共用 115200 无冲突。

### 2.2 PC 侧联动（必须与 2.1 同批发布）

**判定依据**：`screen_online_threshold_seconds` 现为 `routes.py:82` AppContext **类属性硬编码 10.0**。闲时轮询 5s 下，10s = 2×5s 恰好只容 1 拍丢失；若叠加 HTTP 超时重试/DTIM 迟到，存在把在线屏误判"离线"的边界风险。**建议调至 12.0 并挪入 config.py**（一处配置，留足 2×间隔+2s 余量）。

**(a) `printshop/config.py`**（:16 `idle_sleep_seconds` 之后插入）：
```python
    # 小屏 WiFi 在线判定阈值（秒）：必须大于固件闲时轮询间隔 POLL_IDLE_INTERVAL_MS(5s)
    # 的 ~2 倍；若固件调大闲时间隔，此处须同步调大（firmware .ino 同注释）
    screen_online_threshold_seconds: float = 12.0
```

**(b) `printshop/routes.py`**：删 :82 类属性及注释，在 `__init__`（:89 `self.ble = ble` 之后）加：
```python
        # 在线判定阈值随 config 走（默认 12.0s > 固件闲时轮询 5s×2），admin.html 读 threshold_seconds 渲染
        self.screen_online_threshold_seconds = float(cfg.screen_online_threshold_seconds)
```
其余引用（:97-102 判定、:119 payload）不动，自动跟随。

**管理页在线指示**：`admin.html:466` 用 `s.threshold_seconds` 实时值，**无需改前端**（旧前端 `|| 10` 兜底仅对老后端，本仓同发无碍）。
**电量置灰 `stale_after`（routes.py:139-147）**：**不受影响、保持 180**。原因：`updated_at` 在每次轮询到达时刷新（routes.py:253 每次解析报告都盖时间戳）；电量数值本身由固件 60s 缓存节奏决定（`BATT_READ_INTERVAL_MS`），5s 轮询不改变 60s 更新节律，180s 阈值只约束"完全断报"场景。仅需把 :143-144 注释更新为"WiFi：轮询每轮捎带（电量值 60s 节奏），阈值为 180s"。

**其他不联动项**（已核查）：`session_manager.py` 的 `_check_idle_sleep`/`wake`/`finish_service` 均为事件驱动 + 1.5s run_loop（:543），与屏侧轮询间隔解耦；`config.py poll_interval=1.5` 是微信登录轮询，无关，不动。

### 2.3 延迟分析（托盘 wake 全链路）

| 场景 | 现状（恒 1.5s） | 改造后 | 备注 |
|---|---|---|---|
| 托盘"点亮屏幕"→屏亮 | ≈1.5s + HTTP ~0.2s + 上屏/绘制 ~0.3-1s，**最坏 ≈2.5s** | 最坏 = 闲时轮询间隔 **5s** + HTTP + 绘制 ≈ **5.5~6.5s**，典型 ≈5.3s | 唯一可感知劣化点，发生在"闲时熄屏后第一次唤醒"；营业期屏恒亮则无感 |
| 手机扫码→确认→屏变"已扫/打印中" | ≤1.5s+重绘 | **不变**（该时刻屏亮、恒 1.5s 快轮询） | |
| 完成服务/超时→自动熄屏 | ≤1.5s | **不变**（亮屏期快轮询收 display=off） | 服务期结束不会拖着 5s 才熄 |
| 掉线维护画面进出 | 5s 退避 | 不变 | |
| 本地兜底 360s 熄屏 | — | 不变 | 计时与轮询间隔无关 |
| PC 端"屏离线"告警 | 10s 无上报 | 12s（配合 §2.2） | 真掉线最坏多等 2s 才标离线 |

wake 最坏延迟的兜底解释：托盘 wake 面向管理员（营业开始时点一次），5s 级延迟不构成体验问题；若实测不可接受，后续可选"PC 收到 wake 后连发探测"或闲时降为 3s——**首版不做，保持最简**。

### 2.4 验证步骤

1. 编译烧录 + `pytest`（PC 单测，见 §8）。
2. **PC 侧手动**：等自动熄屏（wake 后 180s）→ 托盘"点亮屏幕"→ 秒表量"点击→屏亮"，记录 10 次，**要求 ≤7s、典型 ≤5.5s**；随后连续做 3 轮"扫码→确认→打印→完成服务"全链路，各环节画面反馈与改造前一致。
3. **在线指示**：屏熄屏静置 20 分钟，管理页"📡 小屏：在线"不得闪烁为离线；拔主控网线，≤15s 内转离线。
4. **电量链路**：熄屏态 10 分钟，管理页电量不置灰、pct 每 ~60s 刷新一次；拔 USB 走电池后 pct 正常下降（衔接 §9 A/B）。
5. **串口观察**：熄屏后轮询日志间隔 ~5s（无日志型固件可用插桩 `[perf]` 或 PC 侧 access log 时间戳验证——Flask 每收到 GET 即打点）；服务期间 1.5s。
6. **回归**：主控杀进程/恢复、屏强制熄屏补刀（60s 一次 DISPOFF）、IDLE 本地 360s 兜底各验证一次。

### 2.5 风险与回退

- 误判"离线"：靠 §2.2 阈值联动消除；回退=`POLL_IDLE_INTERVAL_MS` 改回 1500 或 `screenOn ?` 条件去掉（一行）。
- 熄屏期收到的 display=on 指令最坏迟到 5s（见 2.3，已量化）。
- **PC 与固件必须同批发布**（阈值 12s 与 5s 配套）；只发固件时 10s 阈值仍可用但余量减半，只发 PC 无意义。部署含 `dist/printshop.exe` 重打包（见 §8）。

---

## 3. 第三档（原文编号）：降 TX 功率

### 3.1 改动

**文件**：`print_screen.ino`。宏区新增（:203 后）：
```cpp
/* ---- 省电档3：TX 功率（-DBm 枚举由 esp_wifi 提供；开机按 RSSI 起测选档） ---- */
#define WIFI_TX_POWER_OK   WIFI_POWER_11dBm   /* RSSI ≥ WIFI_TX_RSSI_KEEP 时使用 */
#define WIFI_TX_POWER_WEAK WIFI_POWER_17dBm   /* RSSI < WIFI_TX_RSSI_KEEP 时自动升档 */
#define WIFI_TX_RSSI_KEEP  (-68)              /* 开机 RSSI 阈值（dBm） */
```
`setup()` :366（`[net] WiFi connected` 打印后）新增：
```cpp
  /* 降 TX 功率（省电档3）：店内距离近、链路余量大。RSSI 是下行测量，仅作链路对称性
   * 代理指标；真正验收以 PC ping 丢包/HTTP 轮询成功率为准（见 §3.3）。
   * 开机定档即可，运行中不动态调整；轮询连败由既有 failCount 退避兜底。 */
  int rssi = WiFi.RSSI();
  wifi_power_t txp = (rssi >= WIFI_TX_RSSI_KEEP) ? WIFI_TX_POWER_OK : WIFI_TX_POWER_WEAK;
  if (WiFi.setTxPower(txp)) {
    Serial.printf("[net] RSSI=%d dBm, TX power=%d\n", rssi, (int)txp);
  } else {
    Serial.printf("[net] TX power set fail (rssi=%d), keep default\n", rssi);
  }
```
`WiFi.setTxPower` 在 Arduino-esp32 返回 bool（失败=`esp_wifi_set_max_tx_power` 非 OK）。**建议起测 11dBm**（ESP32-C6 支持区间约 8.5~20dBm，11dBm 档余量足；店堂同室 3~8m 距离实测 RSSI 通常 -35~-60dBm）。

### 3.2 与省电档的关系（诚实预期）

本档对**平均电流贡献有限**（TX 突发仅占轮询周期的小占比，典型 1~3mA 量级），但零交互风险。真正价值在叠加 modem sleep 后减少每轮上行突发功率。若 A/B 实测（§9）delta 不可分辨，可保留（便宜）或回退（无依赖方）。

### 3.3 验证与失败回退

- **直接指标**（本档验收）：PC 对屏 `ping 192.168.31.171 -n 200 -w 1000`——**0 丢包、平均 RTT 与 20dBm 基线差异 <30%**；再压测 3 米外隔一堵墙一轮，仍 0 丢包。注意 **ping 必须在与目标相同的 PS 状态下对比**（modem sleep 的 beacon 尖峰会盖掉 TX 功率差异）。
- 代理指标：管理页在线 1 小时不闪断；`failCount` 串口日志无递增。
- **失败回退**：单行把两宏改为 `WIFI_POWER_20dBm`（或删 setup 段）。若实测个别位置连不上，先把 `WIFI_TX_RSSI_KEEP` 抬到 -60 或加 `WIFI_TX_POWER_WEAK` 档位。
- 潜在隐患自查：HTTP 上传/下载文件不走小屏（文件走微信/PC），小屏只拉 <1KB JSON，11dBm 对吞吐无感。

---

## 4. 第四档（原文编号）：iTWT（802.11ax 个体 TWT，实验性）

### 4.1 前置事实（已核实，非推测）

按 IDF release/v5.3 `esp_wifi_he.h` 与官方 `wifi/itwt` 例程（该例程仅支持 ESP32-C6）：
- **API 是结构体式**：`esp_err_t esp_wifi_sta_itwt_setup(wifi_itwt_setup_config_t *setup_config)`；协商期间阻塞至 `timeout_time_ms`。
- 唤醒间隔公式：**TWT Wake Interval = mantissa × 2^exponent，单位 µs**（例：mantissa=512、exponent=12 → 512×4096 µs ≈ **2097ms**）；`min_wake_dura=255`、单位 0（256µs）→ SP 时长 ≈65ms。
- **硬前提：协商 phymode 必须为 `WIFI_PHY_MODE_HE20`**（11ax），例程做法是 `esp_wifi_set_protocol(STA, 11B|11G|11N|11AX)` + `esp_wifi_set_bandwidth(STA, WIFI_BW_HT20)`，并 `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)`。
- 协商结果走事件：`WIFI_EVENT_ITWT_SETUP`，数据 `wifi_event_sta_itwt_setup_t{status, reason, config{...}, target_wake_time}`；status 可取 `ITWT_SETUP_SUCCESS`、`ESP_ERR_WIFI_TWT_SETUP_TIMEOUT`（AP 不回）、`ESP_ERR_WIFI_TWT_SETUP_TXFAIL`、`ESP_ERR_WIFI_TWT_SETUP_REJECT`（AP 拒绝）。事后可用 `esp_wifi_sta_itwt_get_flow_id_status(int *bitmap)` 确认 flow 是否建立（bit0=flow 0）。
- 失败是**静默可回退**的：不建立 TWT 即退回普通 DTIM modem sleep，功能无差别。

### 4.2 改动（`print_screen.ino`）

**(a) include**（WiFi 条件块 :36-41 内追加）：
```cpp
#include <esp_wifi.h>      /* esp_wifi_set_protocol / phymode */
#include <esp_wifi_he.h>   /* esp_wifi_sta_itwt_setup / wifi_itwt_setup_config_t（IDF≥5.2，core 3.3.x 应含） */
#include <esp_event.h>     /* esp_event_handler_instance_register */
```
> 编译如报 `esp_wifi_he.h` 缺失（core 捆绑 IDF <5.2），直接 `#if __has_include("esp_wifi_he.h")` 整体包裹本档代码，保持其余四档可用（TWT 属实验档，缺 API 环境即静默放弃）。

**(b) 宏区新增**：
```cpp
/* ---- 省电档4：iTWT（实验；路由器需支持 802.11ax TWT，失败自动静默回退 DTIM） ----
 * 唤醒间隔 = mant×2^expn µs：512<<12 = 2.097s —— 与闲时轮询 5s 对齐
 * （SP 节拍快于轮询节奏，AP 下行缓冲≤2.1s；若实测 HTTP 响应被 SP 节拍拖延，
 *  下调 expn 至 11(1.05s) 或关闭本档） */
#define ITWT_ENABLE            1
#define ITWT_WAKE_INVL_MANT    512
#define ITWT_WAKE_INVL_EXPN    12
#define ITWT_MIN_WAKE_DURA     255     /* 255×256µs ≈ 65ms SP */
#define ITWT_WAKE_DURATION_UNIT 0      /* 0=256µs 粒度 */
#define ITWT_SETUP_TIMEOUT_MS  2000    /* 协商阻塞上限，开机一次性 */
```

**(c) setup() 内、`WiFi.mode(WIFI_STA)` 之后、`wm.autoConnect()` 之前**（:359-361 之间）——**声明 11ax（iTWT 硬前提），必须在连接前生效**：
```cpp
  /* 省电档4前置：声明 STA 支持 11ax/HE20（C6 仅 HE20），是 iTWT 协商前提；
   * 路由器无 AX 时自动回落 11n 连接，无副作用（WiFiManager 配网页走 softAP，不受影响） */
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G
                                      | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_11AX);
  esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);
```

**(d) 新增 TWT 尝试函数与事件回调**（建议放在 `onServerDown` 之前或 `setup` 前的工具区）：
```cpp
/* ---- 省电档4：iTWT ---- */
static bool itwtFlowReady(void) {
  int bitmap = 0;
  if (esp_wifi_sta_itwt_get_flow_id_status(&bitmap) == ESP_OK) return (bitmap & 0x01) != 0;
  return false;
}

static void itwtTrySetup(void) {
  if (!ITWT_ENABLE || itwtFlowReady()) return;
  wifi_phy_mode_t phy;
  if (esp_wifi_sta_get_negotiated_phymode(&phy) != ESP_OK || phy != WIFI_PHY_MODE_HE20) {
    Serial.println("[twt] AP 非 11ax(HE20)，跳过 iTWT（保持 DTIM）");
    return;
  }
  wifi_itwt_setup_config_t cfg = {
    .setup_cmd = TWT_REQUEST, .flow_id = 0, .twt_id = 0,
    .flow_type = 0,                        /* 0=announced（官方例程默认；实测响应延迟大再试 unannounced=1） */
    .min_wake_dura = ITWT_MIN_WAKE_DURA,
    .wake_duration_unit = ITWT_WAKE_DURATION_UNIT,
    .wake_invl_expn = ITWT_WAKE_INVL_EXPN,
    .wake_invl_mant = ITWT_WAKE_INVL_MANT,
    .trigger = 1,                          /* trigger-enabled（官方例程默认） */
    .timeout_time_ms = ITWT_SETUP_TIMEOUT_MS,
  };
  esp_err_t e = esp_wifi_sta_itwt_setup(&cfg);
  Serial.printf("[twt] setup ret=0x%x %s\n", e,
                e == ESP_OK ? (itwtFlowReady() ? "flow ready" : "pending(见事件)" ) : "→ 静默回退 DTIM");
}

/* 事件日志：WIFI_EVENT_ITWT_SETUP/TEARDOWN/SUSPEND/PROBE 各打一行（详细字段见 IDF 例程 itwt_main.c） */
static void onWifiEvent(void* arg, esp_event_base_t base, int32_t id, void* data) {
  if (id == WIFI_EVENT_ITWT_SETUP) {
    auto* s = (wifi_event_sta_itwt_setup_t*)data;
    Serial.printf("[twt] setup event status=%d (1=成功/%s/%s)\n", s->status,
                  s->status == ESP_ERR_WIFI_TWT_SETUP_TIMEOUT ? "AP无响应" :
                  s->status == ESP_ERR_WIFI_TWT_SETUP_REJECT  ? "AP拒绝"  : "其它",
                  s->status == ITWT_SETUP_SUCCESS ? "已建立" : "未建立→DTIM");
  } else if (id == WIFI_EVENT_ITWT_TEARDOWN || id == WIFI_EVENT_ITWT_SUSPEND || id == WIFI_EVENT_ITWT_PROBE) {
    Serial.printf("[twt] event id=%d\n", (int)id);
  }
}
```
`setup()` 内注册（在 `wm.autoConnect()` 之前即可，事件循环已由 WiFi 库建好）：
```cpp
  esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_ITWT_SETUP,   onWifiEvent, NULL, NULL);
  esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_ITWT_TEARDOWN,onWifiEvent, NULL, NULL);
```
连接成功后调用一次（`:366` `[net] WiFi connected` 之后）：
```cpp
  itwtTrySetup();   /* 一次协商 ≤2s；失败仅日志，静默回退 DTIM（档1） */
```

**(e) 断线重连后的重建**：另注册 `WIFI_EVENT_STA_CONNECTED` → 调用 `itwtTrySetup()`，函数内已有 flow-ready 去重；同一次掉线重连期间系统栈通常会自行 teardown（有事件日志可观察），无需手动 teardown。**为防风暴**，在 `itwtTrySetup` 内可加"距上次尝试 ≥60s"守卫（`static unsigned long lastTry`）。

### 4.3 建议参数与理由

- 唤醒间隔 **2.1s**（512<<12）：闲时轮询 5s，SP 每 2.1s 一次，任何 AP 下行（含 HTTP 响应若被 AP 按 SP 节拍投递）最坏等 ≤2.1s，不劣化 5s 轮询节奏；比 DTIM 每 100ms 听 beacon 省掉中间 20 个 beacon 的接收。
- 若实测 HTTP 轮询往返出现"被 SP 拖延"（GET 后响应平均延迟 >1s），依次试：expn 降到 11（1.05s）→ `flow_type=1`（unannounced）→ 直接 `ITWT_ENABLE 0`。
- **检测路由器是否支持/协商是否成功**：串口 `[twt]` 三行（setup ret / flow ready / setup event status）即最终判据；`esp_wifi_sta_itwt_setup` 返回非 ESP_OK、事件 status=REJECT/TIMEOUT、或 phymode≠HE20，任一出现即**静默回退 DTIM**，不重试（除非重连）。

### 4.4 验证与回退

- 编译烧录 → 串口预期之一：① `[twt] AP 非 11ax…跳过`（路由器无 AX/非 HE20 协商）；② `setup ret=0x0 flow ready` + `setup event status=1`（成功）；③ `setup ret=0x…` 非零 + `status=…未建立→DTIM`（小米系支持 TWT 但拒绝 iTWT 的常见形态）。②/③ 均有后续 `[net]` 轮询正常即可。
- 功能回归同 §2.4（wake 亮屏最坏延迟在 TWT 下仍应 ≤6.5s；若 SP 节拍拖延上行，串口 `[perf] poll` 会暴露）。
- 电量对比见 §9（本档收益预期仅 1~5mA 级，小于测量噪声时以"协商成功日志"作为达成证据即可）。
- 回退：`ITWT_ENABLE 0` 一行；或删除 11ax 协议声明段（连带路由器回 11n 连接）。

---

## 5. 第五档（原文编号）：CPU 降频 80MHz

### 5.1 改动

**文件**：`print_screen.ino`，`setup()` :366 后：
```cpp
  /* 省电档5：CPU 160→80MHz（RISC-V 单核，Arduino 仅支持 80/160 两档，勿写 240）。
   * 电量计量/esp_timer/I2C(100k)/HTTP(无 TLS)均不依赖 160MHz；
   * QSPI 刷屏 pclk 40MHz（display.cpp:131）由 SPI 时钟源提供，不受 CPU 主频直接决定，
   * 但 80MHz 下绘制循环（点阵放大/文本合成，CPU 密集段）约慢 1 倍——
   * 全屏重绘预期仍 <300ms，对 5s 级唤醒链路无感；实际影响由验证步骤量化。 */
  setCpuFrequencyMhz(80);
  Serial.printf("[cpu] freq=%u MHz\n", getCpuFrequencyMhz());
```

### 5.2 评估（对三块的影响面）

| 子系统 | 影响评估 | 依据/验证 |
|---|---|---|
| WiFi 吞吐/HTTP | 无感：载荷 <1KB、无 TLS、轮询 ≤2.5s 超时下 80MHz 余量充足；射频由独立 PLL 驱动 | 串口 `[perf] poll` 对比改前 |
| QSPI AMOLED 刷屏 | pclk 恒 40MHz（display.cpp:131），DMA 搬运不受 CPU 主频影响；CPU 密集段（mono 点阵放大、字形合成）慢 ~2× | 亮屏重绘时长插桩对比（80 vs 160） |
| 电池读取 I2C/系统节拍 | 无感（I2C 100k、`delay(30)` 节拍由 esp_timer 保证） | 电量上报链路回归 |

### 5.3 验证与回退

1. 编译烧录 → 串口应见 `[cpu] freq=80 MHz`；随后 30 分钟功能回归：轮询、扫码全链路、亮/熄屏重绘无花屏（DMA 双缓冲路径 2026-08-25 有花屏前科，重绘慢会放大窗口，重点目检 10 次亮屏重绘）。
2. 用 §2.4 插桩对比 80/160MHz 下：HTTP 往返、二维码整幅重绘耗时（预期各 ≤300ms 增量级；若重绘 >800ms 或轮询出现超时 → 回退）。
3. 回退：删该行（或注释）。**备选折中**（若亮屏重绘不可接受）：亮屏时 `setCpuFrequencyMhz(160)`、熄屏时 80（挂 `doScreenOn/doScreenOff`），一行级代价，验证后再决定是否要。
4. 顺序注意：放 WiFi 连接成功之后（连接/配网阶段保持 160MHz，规避配网页与 WPA 握手的边缘风险）；与 TWT 无交互（射频独立）。

---

## 6. 相关文案同步（防漂移，随各档一起提交）

- `print_screen.ino` 头部注释 :9-10「USB 常供电，不省电」与 :191 段注释「现役，USB 常供电不省电」→ 改为「WiFi 省电改造后：modem sleep + 熄屏闲时 5s 轮询 + TX 11dBm 起测 + CPU 80MHz + iTWT 可选」。
- `transport_config.h:4`、`firmware/README.md:8/66` 同款文案（README 注明各宏位置与 PC 阈值联动关系）。
- `printshop/routes.py:143-144` 注释更新（§2.2）。
- `docs/handoff-2026-08-27-task10b-wifi-gui.md` 属历史交接文档，**不改**（保留 `setSleep(false)` 决策上下文）；新增本次改造的交接文档即可。

---

## 7. 实施顺序与依赖关系

| 序 | 档 | 固件 | PC | 可否独立发布 | 依赖 |
|---|---|---|---|---|---|
| 1 | 档1 modem sleep | ✅ | — | **可独立**（1.5s 轮询 < 10s 阈值不变） | 无 |
| 2 | 档2 自适应轮询 | ✅ | ✅ 阈值 12s（config.py） | **必须固件+PC 同批**（5s > 旧 10s 阈值的一半余量，不联动则边缘误判） | 档1（PS 开着收益才大，非硬依赖） |
| 3 | 档3 TX 降功率 | ✅ | — | 可独立（可与 1/2 合并发） | 无 |
| 4 | 档5 CPU 80MHz | ✅ | — | 可独立 | 无 |
| 5 | 档4 TWT | ✅ | — | 可独立/实验性后置 | 档1（PS MIN_MODEM 为 TWT 前置）；参数对齐档2 的 5s |

**推荐节奏**：① 档1 单发 + 抖动回归 → ② 档2+PC 联动同发（核心省电档，A/B 测一次）→ ③ 档3+档5 合并发 + 功能回归 → ④ 档4 单独实验（串口验证协商；不达预期即 `ITWT_ENABLE 0` 收尾，不阻塞主流程）。每次发布后进入 §9 对应 A/B 段。

---

## 8. 编译 / 测试 / 部署命令（照抄可用）

固件（PowerShell，工作目录为仓库根）：
```powershell
$fqbn = "esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB"
# 默认 WiFi 变体（勿覆盖 build.extra_flags；--build-path 必须纯 ASCII，见 firmware/README §二）
tools\arduino-cli\arduino-cli.exe compile --fqbn $fqbn --build-path C:\arduino-build\print_screen_wifi firmware\print_screen
# 带性能插桩的验证构建（可选）
tools\arduino-cli\arduino-cli.exe compile --fqbn $fqbn --build-property compiler.cpp.extra_flags=-DWIFI_DEBUG_PERF --build-path C:\arduino-build\print_screen_perf firmware\print_screen
tools\arduino-cli\arduino-cli.exe upload -p COM3 --fqbn $fqbn --input-dir C:\arduino-build\print_screen_wifi
tools\arduino-cli\arduino-cli.exe monitor -p COM3 --config 115200
```
PC 单测与打包：
```powershell
.venv\Scripts\python.exe -m pytest printshop\tests\test_routes.py printshop\tests\test_ble_link.py printshop\tests\test_config.py -q
# 现有断言核查点：test_routes.py:523（wake）、:708（在线广播）、test_ble_link.py:431-449（stale_after 不变）
# 建议本档新增 1 条：AppContext 默认 threshold == cfg.screen_online_threshold_seconds（12.0），
#             及 _screen_online() 在 age=12.0s 边界的行为
# exe 重打包（如现役走 dist\printshop.exe）
.venv\Scripts\python.exe -m PyInstaller printshop.spec
scripts\smoke_dist.ps1
```

---

## 9. A/B 电池测量方案（放电回归，battery_track.py）

**原理**：无功率计时用电池斜率。`battery_track.py` 对"连续放电会话"（vbus=false 且 charging=false，:44-48）做 pct~时间 线性回归（:126-165），Windows 计划任务每 10 分钟采样一次。**A/B 只比同一电池上前后两段放电会话的斜率（%/h）与推算续航小时数**，无需知道电池容量（容量未知，绝对 mA 不可换算；以下 mA 数字仅为参考量级，最终以斜率差为准）。

**受控条件（每段必须一致）**：
1. 小屏处于**熄屏静置**态：点托盘 wake → 等 180s 自动熄屏 → 确认管理页 `display=off`（排除屏幕功耗干扰，纯测 WiFi+CPU 改造）；
2. 主控 PC 全程开机、Flask 在线（无营业流量，仅屏轮询）；
3. 屏与路由器相对位置不变（收件柜台同点），店内其他 WiFi 客户端尽量少；
4. 充电至 pct=100% 且 vbus=true → 拔 USB 开始计时；每段**从 100% 放到 ≥55%**（≥4h，10min 采样下 ≥24 点，回归才稳；pct 量化 1% + 10min 节拍的噪声需要长窗）；
5. 记录每段起止时间戳 + 固件版本/档位表。

**流程与预期**（每档重复：烧录 → 受控态就位 → 充满拔电 → 静置 ≥4h → 回填充电）：

| 段 | 固件 | 与上段差值（斜率，%/h） | 参考量级说明（待实测校准） |
|---|---|---|---|
| B0 | 现役基线（git HEAD，档1~5 未开） | — | 参考 2026-08-28 记录 ≈10.6%/h（≈9.4h 续航，当时屏幕状态未受控，正式以本次重跑 B0 为准） |
| A1 | +档1 modem sleep | **预期降 20~40%** | 射频全开 TX/RX 平均电流 ~60mA 中，DTIM 睡眠后空闲均值降 ~15~25mA；熄屏态斜率应明显低于 B0 |
| A2 | +档2 闲时 5s 轮询 | 在 A1 上再降 10~25% | TX+HTTP 突发次数降为 1/3.3，轮询相关功耗近似同比；叠加后熄屏态预期接近 modem-sleep 底噪（≤38mA 量级口径） |
| A3 | +档3 TX 11dBm | 再降 0~10% | TX 突发功耗占比低，可能落入噪声；不可分辨也接受（有协商/链路证据即可） |
| A4 | +档5 CPU 80MHz | 再降 5~15% | CPU 全速 ~12mA 量级 → 80MHz 约半，省 ~5~8mA |
| A5 | +档4 TWT（若协商成功） | 再降 0~10% | iTWT 在 MIN_MODEM 之上再省 beacon 接收；失败段跳过并记录 |

每段结束后**先回读 CSV 分段**：`battery_history.csv` 以"充电样本"自动切分会话（:112-123），段边界天然隔离。斜率提取：对目标会话区间复用 `battery_track.estimate_runtime()` 的回归式（`load_history` + 手动按区间过滤，或临时跑 `.venv\Scripts\python.exe -m printshop.battery_track` 的增量估计——同一会话的 `runtime_estimate_hours` 稳定后取值即可，无需改工具）。

**达成/失败判据**：A2 段斜率相对 B0 降幅 ≥30%（即续航 ≥ 基线×1.4）为"档1+2 达标"；若 A1 段即无显著差异，先查 PS 是否生效（串口改 `WiFi.setSleep(true)` 后加一行 `esp_wifi_get_ps()` 打印，或回 §1.3 试 MAX_MODEM）再继续。**测量完成前不宣称具体省电数字**——本仓库 2026-08-25 S0 已有"预估 2~5 天被实测 9~12h 证伪"的前科，一切以本次受控斜率差为准。

---

## 10. 风险汇总与总回退表

| 风险 | 概率/影响 | 缓解 |
|---|---|---|
| modem sleep 致轮询抖动/超时回归 | 低/中 | §1.4 步骤 2 首验；回退 A 单行；回退 B 仅熄屏睡眠 |
| 闲时 5s 轮询致 wake 变慢 | 确定发生（最坏 5s→~6.5s）/低 | §2.3 量化；仅熄屏期受影响，营业期屏恒亮无感 |
| PC 误判屏离线 | 低/中 | 阈值 12s 联动发布（§2.2）；`threshold_seconds` 已随 payload 下发，admin.html 自适应 |
| TWT 协商拖慢启动（≤2s）或引入响应延迟 | 低/低 | `timeout_time_ms=2000` 一次性；非 HE20/失败即静默回退；`ITWT_ENABLE 0` 一行关闭 |
| 80MHz 下重绘变慢/花屏窗口放大 | 低/低 | §5.3 目检 10 次重绘；回退单行；折中=按亮熄屏切频 |
| TX 11dBm 致远端连不上 | 低/低 | 开机 RSSI 分档自动升 17dBm；ping/轮询验收；回退单行 |
| 文案/阈值漂移（README、宏注释） | 低/低 | §6 随档提交 |

**总回退**：五档相互独立、每档 ≤3 行可逆；档 2 涉及 PC 端 3 行 + config 1 行。极端情况整体 `git revert` 对应提交并重烧 + 重打包 exe 即恢复现役。

### 遗留未决点（实施前可选项）
1. 工具链 esp32 core 确切版本与 `esp_wifi_he.h` 可用性——需要读 `C:\arduino15`（工作区外）才能确认；已给出 `__has_include` 编译级兜底，亦可先行忽略。
2. 小米路由器型号/HE 能力/DTIM 配置未知——档 1 与档 4 的串口日志即实测判据，无需预查。
3. ESP32-C6 SPI 时钟源与 CPU 降频的耦合关系未在代码中完全确认（display.cpp:131 只设了 pclk=40MHz）——档 5 的耗时插桩对比即判据。

---

# 第二部分：第三档（深度省电）实施方案

> 范围：**A. Auto light sleep + WiFi 保连（保轮询架构不变）** 与 **B. 混合架构（闲时 WiFi 断开 + BLE 唤醒门铃）**，两案全做。
> **全局排除项再次声明**：熄屏/空闲期切 ALDO3 面板电源（绿屏，永久放弃）。所有状态的熄屏一律维持现状语义（WiFi 现役路径 `doScreenOff()` 的"全黑填充 + DISPOFF 软熄屏"，`print_screen.ino:304-321`），**不得**复用 BLE 变体 `onScreenState` 里 `lcd_panel_power_off()`（ALDO3 断电）的语义（`print_screen.ino:424-429`）。

## 0. 阅读基线（已核实的关键代码事实）

| 事实 | 位置 |
|---|---|
| WiFi 现役：1.5s 轮询 `GET /api/screen/state`，失败退避 5s；`WiFi.setSleep(false)`；主循环 `delay(30)` | `print_screen.ino:196-203, 225-285, 359-360, 374-393` |
| 熄灭路径只软熄屏（DISPOFF），不动 ALDO3；本地 6 分钟兜底熄屏 | `print_screen.ino:304-321`；`IDLE_SLEEP_MS` 在 201 行 |
| BLE 变体完整可回切：`TRANSPORT_BLE` 编译开关、GATT 三特征、100ms 广播、60s 电池 notify、drawn ack | `transport_config.h:7-12`；`ble_link.cpp:18-31, 112-192`；`print_screen.ino:399-522` |
| BLE 变体含 ALDO3 面板断电逻辑（**本方案不复用该语义**） | `print_screen.ino:415-429, 436-441`；`display.h:38-40` |
| 显示驱动电源 API：`lcd_screen_off/on()`（DISPOFF 级，返回 bool）、`lcd_panel_power_on/off()`（ALDO3 级） | `display.h:33-40, 77-83` |
| PC 侧 BLE Central（winrt）完整：懒建连/120s 快速重醒/900s 心跳/5s 失败退避/自检 | `printshop/ble_link.py:220-582`；`config.py:23-26` |
| PC 唤醒链：托盘 `POST /api/screen/wake` → `sm.wake()` 置 display=True → 轮询拉取 | `tray.py:63-76`；`routes.py:339-342`；`session_manager.py:145-152` |
| 主控在线判定：轮询 10s 阈值；`stale_after`：BLE=心跳+300s，WiFi=180s | `routes.py:81-103, 139-147` |
| BLE 组装入口（空地址=禁用）：`main.py:104-117`；on_change 推送 113-118 | `main.py` |
| 后端在线态/电池展示 | `printshop/static/admin.html`（状态栏"📡 小屏"） |

外部事实（已核实）：
- IDF v5.5：`esp_pm_configure()` 要求 `CONFIG_PM_ENABLE`，否则返回 `ESP_ERR_NOT_SUPPORTED`；自动 light sleep = `esp_pm_configure({.light_sleep_enable=true})` + `CONFIG_FREERTOS_USE_TICKLESS_IDLE=y`（该选项 `depends on PM_ENABLE`）[IDF FreeRTOS Kconfig](https://raw.githubusercontent.com/espressif/esp-idf/v5.5/components/freertos/Kconfig)、[esp_pm.h](https://raw.githubusercontent.com/espressif/esp-idf/v5.5/components/esp_pm/include/esp_pm.h)。
- IDF v5.5 C6 睡眠文档：保持 WiFi 连接须走"Modem-sleep + automatic Light-sleep"（普通 light sleep 不保连接）；C6 无 USB-Serial-JTAG 唤醒源 [IDF sleep_modes (C6)](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32c6/api-reference/system/sleep_modes.html)。
- 参考电流量级（需实测复核）：WiFi 全开 ~60mA；modem sleep ≤38mA；light sleep 芯片 ≤180µA（Adafruit 板级 353µA）；deep sleep 芯片 ~7µA。

---

## 1. 共同前置条件（A、B 共用，先行 P0）

| # | 前置 | 说明 |
|---|---|---|
| P0-1 | **电流测量手段**（硬门槛） | 调优无反馈回路则一切白做。最低成本方案：USB-C 电流表（串在 USB 供电线上，读数≈整机输入电流，几十元）；纯电池工况用电池斜率法（现有 AXP2101 电量计 + 每 60s 上报，`axp2101_bsp.cpp` 已实现，参考 24h 长测 9~12h 的测量法）。两种都要记录 5 档数据：开机亮屏服务中 / IDLE 亮屏 / 熄屏轮询 / 熄屏+睡眠 / 深度睡眠。 |
| P0-2 | **工具链现场摸底**（实施第 1 步做，写进 M-A0） | `tools/arduino-cli` 的 data 目录（README 提及纯 ASCII 路径 `C:\arduino15`，需现场 `arduino-cli config dump` 确认）；确认 esp32 core 版本目录（预期 `packages/esp32/hardware/esp32/3.3.x`，代码注释为 core 3.3.3）与预编译库位置 `<platform>/tools/esp32-arduino-libs/esp32c6/`（含 `lib/`、`bootloaders/`、`sdkconfig`）；**先整体备份该目录**（回退用，见 A1.3）。 |
| P0-3 | **Linux 构建机**（仅 A1 需要） | esp32-arduino-lib-builder 官方支持 Ubuntu/RPi/macOS。现网是 Windows：用 WSL2 Ubuntu 或一台 Linux 盒子做构建，产物拷回 Windows 覆盖平台目录。实施期先在 WSL2 装好 git/cmake/ninja/python3 等依赖（照 README 的 apt 清单）。 |
| P0-4 | 现役固件/PC 快照可回滚 | 当前 `TRANSPORT_WIFI` 固件二进制与 `dist/printshop.exe` 保留不动；本次所有改动只在源码与新烧录件上进行。 |

---

## 2. 方案 A：Auto light sleep + WiFi 保连（保轮询架构不变）

### A0. 目标与边界

- 目标：CPU 空闲自动进 light sleep（芯片级 ≤180µA/板级实测目标 0.3~1mA 量级，叠加 WiFi modem-sleep 与每 1.5s（或一/二档自适应间隔）的 HTTP 轮询突发，整机平均电流从 ~60mA 压到个位数 mA）；WiFi 关联保持，**轮询架构、PC 端代码、HTTP 契约零改动**。
- 非目标：不改 PC 端；不切 ALDO3；不做 deep sleep。
- 与第一/二档的关系：A 是它们的基础设施——自适应轮询间隔拉长后，light sleep 的收益直接放大（见 A3 预期电流公式）；实现时预留"熄屏+IDLE 期间轮询间隔可放宽"的口子（`POLL_INTERVAL_MS` 处已集中），路由端 10s 在线阈值（`routes.py:82`）若把 idle 间隔放到 5s 以上需同步上调（建议 A 内保持 ≤5s 默认，不碰路由）。

### A1. 构建路径选型（对比 (a)/(b)）

**结论先行：选 (a) esp32-arduino-lib-builder 重编 arduino 预编译库；放弃 (b) ESP-IDF 工程 + arduino-as-component。**

| 维度 | (a) lib-builder 重编 arduino libs | (b) ESP-IDF 工程 + arduino-as-component |
|---|---|---|
| 与现有工具链兼容 | 高：重编产物替换 `<platform>/tools/esp32-arduino-libs/esp32c6/` 后，`tools/arduino-cli` 编译/烧录命令、FQBN、`--build-property` 注入 `-DTRANSPORT_BLE` 全部不变 | 低：整个构建体系换 IDF `idf.py`；sketch 结构（.ino 预处理）、`WiFiManager`/`XPowersLib`/`ArduinoJson`/vendored `qrcode`、微雪 `esp_lcd_sh8601.c` 都要按 IDF component 方式重排 |
| 改动面 | 只动预编译 `.a` 与 sdkconfig；sketch 源码零结构改动 | 源码工程结构全改，双变体（WIFI/BLE）各一套 component 化维护 |
| 回滚 | 拷贝还原官方 `esp32c6/` 目录即可（秒级） | 需要整条工具链回退 |
| 风险 | 低-中（构建环境、版本对齐，见 A1.1） | 中-高（移植 bug 面大，且本项目已踩过 esp32 core 内部同名头文件抢占等"换底座"坑） |
| 时间 | 环境 + 重编 ~0.5~1.5 人日（单 chip 单配置） | ≥3~5 人日且后续每个功能迭代都要双轨维护 |

### A1.1 实施步骤（照做即可）

1. **对齐版本**（M-A0 摸底项）：读出已装 core 版本 `<platform>/package.json`（或目录名 `3.3.x`），记录官方 `esp32c6/sdkconfig` 全文为基线；查 lib-builder 侧与 core 3.3.x 配套的 arduino-esp32 分支/标签与 IDF 分支（本项目此前调研结论指向 **ESP-IDF v5.5**，实施时以 `arduino-esp32` 对应 release 的 CI 记录为准——lib-builder `configs/builds.json` 里 esp32c6 的官方组合是 `idf_libs: ["qio","80m"]`，即与 FQBN 的 `qio/80m` 一致，确认我们的 libs 组合不含 `qio_ram` 等特性）。
2. **构建机准备**（WSL2 Ubuntu）：装齐 lib-builder README 依赖；`git clone https://github.com/espressif/esp32-arduino-lib-builder`。
3. **加入自定义配置**：lib-builder 以 `configs/defconfig.common` + `configs/defconfig.esp32c6` 合并成 sdkconfig 再编译。在仓库内新建 `configs/defconfig.lightsleep`（内容见 A1.2），构建命令显式带上它：
   ```bash
   ./build.sh -A <与已装 core 匹配的 arduino-esp32 标签/分支> -I release/v5.5 -t esp32c6 -b idf_libs qio 80m lightsleep
   ```
   （`-b menuconfig` 可先手工交互确认每个选项在 v5.5 中的真实名字；`-b idf_libs qio 80m` 对应官方 esp32c6 的 libs 组合；首次跑会自动装 IDF 与组件，耗时 1~2 小时量级属正常。）
4. **产物回拷**：产物在 `<repo>/out/tools/esp32-arduino-libs/esp32c6/`。整目录覆盖 Windows 侧 `<platform>/tools/esp32-arduino-libs/esp32c6/`（**覆盖前先整体备份原目录**；lib-builder 自带 `-c <arduino 目录>` 拷贝仅支持手工安装的 core，arduino-cli 包目录手动覆盖等价）。只重编 app libs（`-b idf_libs`），bootloaders 不动（官方 qio/80m 组合与我们无差异，且 bootloader 不涉及 PM）。
5. **验证生效**（两段式，写进 M-A1）：
   - 静态：`esp32c6/sdkconfig` 文本里出现 `CONFIG_PM_ENABLE=y` 与 `CONFIG_FREERTOS_USE_TICKLESS_IDLE=y`；
   - 动态（权威）：临时小 sketch 调 `esp_pm_configure(&cfg)` 打印返回值 ≠ `ESP_ERR_NOT_SUPPORTED`；固件 A2 落地后启动日志直接打 `[pm] esp_pm_configure => ESP_OK`。

### A1.2 要开的 sdkconfig 选项（新增 `defconfig.lightsleep` 内容）

| 选项 | 值 | 说明 |
|---|---|---|
| `CONFIG_PM_ENABLE` | `y` | 电源管理总开关（esp_pm_configure 前置，不开必返 NOT_SUPPORTED） |
| `CONFIG_FREERTOS_USE_TICKLESS_IDLE` | `y` | 自动 light sleep 的 FreeRTOS 支撑（依赖 PM_ENABLE） |
| `CONFIG_FREERTOS_IDLE_TIME_BEFORE_SLEEP` | `3`（默认即可） | 预期空闲 ≥3 ticks 才进 sleep（FREERTOS_HZ=1000 → 3ms） |
| （可选，A3 实测后开）`CONFIG_ESP_SLEEP_FLASH_LEAKAGE_WORKAROUND` | `y` | 压低 light sleep 期间 flash 漏电（µA 级收益，官方推荐替代 flash 断电） |
| （可选，实测后开）`CONFIG_ESP_WIFI_IRAM_OPT` / `CONFIG_ESP_WIFI_RX_IRAM_OPT` | `y` | 仅在出现 PM+WiFi 相关 panic/异常时补开 |
| **不开**：`CONFIG_PM_POWER_DOWN_PERIPHERAL_IN_LIGHT_SLEEP`、`CONFIG_PM_POWER_DOWN_CPU_IN_LIGHT_SLEEP` | — | 首版保持默认（CPU/外设不掉电、唤醒快、与 WiFi 保连兼容性最好）；作为 A3 调优候选逐项试，每项都要回到路由器保连测试 |
| 与 `CONFIG_BT_LE_SLEEP_ENABLE`（方案 B2 用） | — | A 阶段不开（WiFi 变体不启用 BT）；B2 时在同一条构建链上加 |

**对 .ino 源码的影响**：无任何 `#ifdef CONFIG_*` 需要——sketch 只调 `esp_pm_configure()`，按返回值判断，天然兼容官方 libs 与重编 libs（回退不炸编译）。

### A1.3 验证生效 & 失败回退

- 生效判据：A2 固件启动日志打印 `[pm] esp_pm_configure OK`；电流表观察到熄屏轮询期间出现 <2mA 的"谷底"电流段；`esp_pm_dump_locks`（调试期可加一行）能看到 WiFi 在突发时短时持锁、空闲无锁。
- 回退（两级）：
  1. 还原官方 `esp32c6/` 备份目录 → 编译行为完全回到现状（固件代码不用改，esp_pm_configure 会返回 NOT_SUPPORTED，代码里打 warning 继续跑）；
  2. 若重编工具链卡死（版本对齐/构建环境问题），启用 A5 的"手动 light sleep"降级实现（零构建改动），自动 light sleep 留待后续。

### A2. 固件侧改动（`firmware/print_screen/`）

改动集中在 `TRANSPORT_WIFI` 分支（`print_screen.ino:193-395`），用宏 `PM_LIGHT_SLEEP`（默认定义，`-DPM_LIGHT_SLEEP=0` 可关——调试/烧录期保留一个"不睡版"避免 CDC 断流干扰排障）包住。

**A2.1 esp_pm_configure 与触发条件**

```cpp
// print_screen.ino TRANSPORT_WIFI 分支顶部新增（约 203 行后）
#if PM_LIGHT_SLEEP
#include "esp_pm.h"
#include "esp_wifi.h"
#endif
```

- 调用点：`setup()` 里 WiFi 连接成功之后（`print_screen.ino:366` 打印 IP 后）：
  ```cpp
  esp_pm_config_t pm = { .max_freq_mhz = 160, .min_freq_mhz = 80,
                         .light_sleep_enable = true };
  esp_err_t e = esp_pm_configure(&pm);
  Serial.printf("[pm] esp_pm_configure %s\n", e == ESP_OK ? "OK" : esp_err_to_name(e));
  ```
- 触发条件语义：**全生命周期自动**——不区分熄屏/亮屏。自动 light sleep 只在无任务可跑时进入：HTTP 轮询、渲染、I2C 电池读取期间任务忙，天然不睡；1.5s 轮询间隙与 30ms `delay()` 空闲期自动睡。无需业务代码判断"何时可睡"，这正是选自动 light sleep 而非手动调度的原因。
- 亮屏服务期（SCANNED/CONNECTED/…）轮询照旧，渲染突发的空闲间隙也睡（几 ms 级），无副作用。
- `esp_pm_lock`：首版不需要显式锁。若实测发现某段外设操作被 sleep 打断（渲染 DMA 被切、HTTP 偶发超时），再在 `pollScreen()`（`print_screen.ino:225`）与 `doScreenOn()`（323 行）内临时取 `ESP_PM_NO_LIGHT_SLEEP` 锁包住关键段（esp_pm.h 的 `esp_pm_lock_create/acquire/release`，实测驱动，勿预防性加）。

**A2.2 WiFi 侧配合**

- 把 `WiFi.setSleep(false)`（`print_screen.ino:360`）改为：
  ```cpp
  WiFi.setSleep(true);            // == esp_wifi_set_ps(WIFI_PS_MIN_MODEM)
  ```
  理由：自动 light sleep + WiFi 保连的前提是 modem-sleep（IDF C6 睡眠文档明示；MIN_MODEM 保 DTIM 监听节奏，HTTP 每 1.5s 的突发会随时把 CPU 拉满速）。注意：不能省掉这行——`setSleep(false)` 会持锁阻止 light sleep。
- `listen_interval`：用 `esp_wifi_get_config`/`esp_wifi_set_config` 取默认值即可（默认 3 beacons）；因为保连的主力是**每 1.5s（退避 5s）自己的轮询突发**，不是 beacon 监听——把 listen_interval 调大（如 5~10）可再压一点电流，但会放大"主控侧先推、屏侧最迟 1 轮询周期看到"的延迟，此项目无下行实时性要求，属可调项，A3 阶段量测后定，默认不动。
- **路由器侧保连结论**：light sleep 期间 WiFi 关联靠"modem sleep + 每轮询周期的真实收发"维持；AP 的 idle disassociate 定时器（通常 300s）远大于 1.5~5s 轮询周期，不会掉线。验证见 A3。

**A2.3 delay(30) 在 tickless 下的行为 & 循环节奏**

- `delay(30)`（`print_screen.ino:392`）在 tickless idle 下照常精确：系统进入 light sleep 后由 RTC/系统定时器在 30ms 到期点唤醒，`millis()` 自动补偿，无代码改动。
- 副作用：每 30ms 一次"睡→醒"周期 ≈33 次/秒唤醒，每次唤醒有固定开销（C6 唤醒 µs~ms 级）。首版**保留 delay(30) 不动**（行为零变化，风险最小）；A3 若量测到唤醒开销显著，改"动态延时到下一个事件点"：把 `loop()`（374-393 行）改为计算距下一次 `poll`/`updateBatteryCache` 的剩余毫秒，`delay()` 到点（上限如 300ms 一段，兼顾响应），可把空闲唤醒频率降到 ~3~10 次/秒。此项列为 A-M3 的调优项，不阻塞主线。
- 与第一/二档自适应间隔的配合点：`POLL_INTERVAL_MS/POLL_BACKOFF_MS`（196-199 行）已集中；若此前档位把"熄屏 IDLE"轮询放宽到 ≥5s，light sleep 的占比直接提升，A3 的电流公式按实际 interval 代入即可。

**A2.4 USB CDC（CDCOnBoot=cdc）在 light sleep 下会断——影响与应对**

事实基线：USB-Serial-JTAG 控制台在 light sleep 期间总线被时钟门控，host 侧会观察到"串口静默/掉口"，C6 的 v5.5 唤醒源清单里没有 USB-Serial-JTAG 唤醒（已核实 IDF 文档），不能指望"串口发个字符把板子叫醒"。

应对（写进实施，按严重度）：
1. **生产部署形态不受影响**：店内小屏 USB 接充电器（无 host），无串口会话可言；日志缺失只在"开发/烧录期"发生。
2. **开发期双构建**：`PM_LIGHT_SLEEP=0` 的"不睡版"用于日常串口排障与烧录（行为 = 今天现状）；`PM_LIGHT_SLEEP=1` 为发布版。烧录发布版后如需改烧：按住 **BOOT + 重新插拔 USB 上电**进入下载模式再 `arduino-cli upload`（下载模式全速运行，不受睡眠影响）；不要依赖 DTR 自动复位。
3. 若"睡着时掉口、醒后重枚举"导致 COM 口号漂移，属预期，工具链用 `tools/poc/com3_monitor.py` 的端口发现逻辑按 VID/PID 重找。
4. 发布版**不放任何关键调试日志依赖**（现役固件日志本来就只是辅助），掉日志期间行为不受影响。

**A2.5 代码改动清单（汇总）**

| 文件 | 位置 | 改动 |
|---|---|---|
| `print_screen.ino` | TRANSPORT_WIFI 段（196-393 行） | `#if PM_LIGHT_SLEEP` include esp_pm/esp_wifi；setup 连接后 `esp_pm_configure`；`WiFi.setSleep(true)`；启动日志打印 pm 结果 |
| `print_screen.ino` | 顶部宏区（~196 行前） | `#define PM_LIGHT_SLEEP 1`（可被 `--build-property compiler.cpp.extra_flags=-DPM_LIGHT_SLEEP=0` 覆盖——注意沿用既有注记：只追加 `compiler.cpp.extra_flags`，勿覆盖 `build.extra_flags`） |
| 无 PC 端改动 | — | HTTP 契约、路由、GUI、托盘全部不动 |
| 文档 | `firmware/README.md` | 编译命令区补 PM 版/不睡版两种构建与烧录注意事项（照仓库惯例） |

### A3. 验证与量化（M-A2/M-A3 验收）

**验证步骤（每步都记电流与日志）**：

1. 功能回归（先于一切电流测量）：烧 PM 版后完整走一遍现状 checklist——轮询正常（后台 screen.online=true）、托盘唤醒 ≤1.5s 亮码、扫码→状态切换、`/api/screen/state` 带电池、主控关机→"系统维护中"→恢复→自愈、2h 待机无死机/无花屏。
2. **WiFi 保连验证（路由器侧）**：路由器管理页/`arp`/ping 观察屏端 MAC 关联时长 ≥24h 不掉线；期间人为断开主控进程、再恢复，验证屏不退避失控；必要时抓 `esp_wifi_get_ap_info` 或日志 RSSI 对照。
3. **唤醒延迟测量**：串口埋点（`PM_LIGHT_SLEEP=0` 版打 `[pm] sleep us=xxx`/`wake`），测三类延迟：空闲→HTTP 发起（预期 ms 级）、30ms tick 唤醒抖动、深度空闲（轮询放宽 5s 档）首次响应。判据：HTTP 轮询节奏不劣化（无 timeout 增长），亮屏链路与现状一致。
4. **电流量化（USB 电流表 + 电池斜率双轨）**，预期档位（待实测修正）：

| 工况 | 现状基线 | A 预期（1.5s 轮询） | A 预期（熄屏 idle 放宽 5s 档，配合一/二档） |
|---|---|---|---|
| 熄屏+轮询平均 | ~60mA（WiFi 全开） | 10~15mA（≈0.2s×70~100mA 突发 + 1.3s×0.5~1mA 谷） | 3~6mA（突发占比降到 ~4%） |
| light sleep 谷底电流 | 无 | 0.5~2mA 量级（板级含 PMU/面板静态，芯片 180µA 只是下限，**勿用芯片数承诺整机**） | 同左 |
| 亮屏服务中 | ~60mA+屏 | 基本不变（渲染/轮询忙） | 同左 |

   判据：谷底电流出现、平均电流相对基线下降 ≥50%，且 24h 电流曲线无爬升（无漏电/无失控外设）。
5. 失败回退验证：还原官方 libs 目录 → 编译 → 固件日志出现 `esp_pm_configure NOT_SUPPORTED` → 系统按现状跑（功能不回归）。

### A4. 里程碑与工作量

| 里程碑 | 内容 | 验收标准 | 人日 |
|---|---|---|---|
| M-A0 | 前置摸底 + 备份 + 电流表到位 + 基线电流表 | 上文 P0 全部就绪，基线 5 档电流有记录 | 0.5~1 |
| M-A1 | lib-builder 环境 + 重编 + 替换 + 生效验证 | sdkconfig 含 PM 两键；`esp_pm_configure` 返回 OK；官方目录可一键还原 | 1~1.5 |
| M-A2 | 固件改动（A2.5）+ 功能回归 + 保连 24h | A3 第 1、2 步全过 | 0.5~1（含烧机并行） |
| M-A3 | 量化 + 调优（delay 策略/listen_interval/flash workaround/CPU PD 逐项试）+ 结论 | A3 第 3、4 步有数据与选定档位；输出实测电流表更新到交接文档 | 1~2 |
| **A 合计** | | | **3~5.5 人日**（不含烧机等待） |

### A5. 风险与回退

| 风险 | 等级 | 应对 |
|---|---|---|
| 重编 libs 版本对齐失败（IDF/arduino 分支与已装 core 不完全一致） | 中 | M-A0 先记录官方 sdkconfig 与产物日期做对照；以 `-b menuconfig` 交互确认选项名；失败走"手动 light sleep"降级（下条） |
| 自动 light sleep + WiFi 在个别路由器/信道下异常（社区有 IDFGH-12637 类高功耗个例 [esp-idf#13634](https://github.com/espressif/esp-idf/issues/13634)） | 中 | 保连验证跨店路由做 24h；电流异常时逐项关 CPU PD/flash workaround 复测 |
| **降级路线（不动构建的替代实现）**：`esp_light_sleep_start()` 手动调度（每轮循环算好距下次 poll 的时间→`esp_sleep_enable_timer_wakeup`→`esp_light_sleep_start`），配合 `WiFi.setSleep(true)` | — | 该路线不需要 CONFIG_PM_ENABLE（显式 light sleep 是普通 API），保连靠自身轮询突发维持；M-A1 卡住时直接启用，自动 light sleep 作为后续升级。**注意**：手动路线需自行处理每段睡眠 ≤1~2s、保证醒来窗口收 WiFi，复杂度在固件侧但可控 |
| USB CDC 掉线影响烧录/排障 | 低 | 不睡版/发布版双构建 + BOOT 键进下载模式（A2.4） |
| 电流数字达不到芯片级预期（PMU/面板/DISPOFF 静态未知） | 中 | 目标以"整机实测"为准（A3 表），分项排查：面板 IC sleep 电流、PMU 静态、QSPI 上拉；不承诺 ≤180µA 整机 |

---

## 3. 方案 B：混合架构（闲时 WiFi 断开 + BLE 唤醒门铃）

### B0. 目标与边界

- 目标：空闲期固件**断开 WiFi**（modem 全关，消灭最大的功耗项），降功耗运行；PC 需要服务时经 BLE 把屏叫醒，屏快速重连 WiFi 进入服务会话（会话内 = 现役 WiFi 轮询，一字不改）；会话结束 + 空闲超时自动回低功耗态。预期空闲电流：B1（CPU 常醒）8~20mA → B2（叠加 light sleep）整机 2~6mA 量级（待实测）。
- 边界与排除：
  - 熄灭一律 DISPOFF 软熄屏，**不切 ALDO3**（同全局排除项；B 不复用 BLE 变体 `onScreenState` 的 `lcd_panel_power_off()`）。
  - BLE 只承担"唤醒门铃 + 心跳/电池读取"；**状态数据主通道永远是 HTTP 轮询**，BLE 不承载服务期状态流（与旧纯 BLE 变体角色不同，规避 Windows BLE Central 稳定性风险敞口）。
  - 不新增硬件（无按钮/无 802.15.4 dongle）；不做 deep sleep（RAM 保持是重连 WiFi 的前提）。
- 新传输模式命名：`TRANSPORT_HYBRID`（第三个编译开关，`transport_config.h` 现为二选一互斥校验，见 `transport_config.h:7-12`，需扩成三选一；现役 `TRANSPORT_WIFI` 保持默认与回滚路径）。

### B1. 固件状态机设计

新文件建议：`firmware/print_screen/power_state.h/.cpp`（状态机 + 低功耗态逻辑），`ble_link.cpp` 改条件编译为 `TRANSPORT_BLE || TRANSPORT_HYBRID` 后**直接复用**（BLE 外设层与传输语义无关）。

```
         ┌────────────────────────────────────────────────────────────┐
         │ LOW_POWER（WiFi off, BLE 广播, 屏 DISPOFF 黑）              │
         └──────┬─────────────────────────────────────────────────────┘
                │ ① BLE 写 screen_state{display:on}（门铃）→ 置 wake_pending + 预渲染(可选)
                │ ② 自检定时器到期（巡检）                              │
                ▼                                                     │
         WIFI_CONNECTING（带信道缓存的快速重连，≤8s 超时）               │
                │ 成功                                失败(计数<3)        │
                ▼                                                     │
         SERVING（≡ 现役 WiFi 轮询态：pollScreen/onServerDown 原样）    │
                │ 拿到 display=off 且状态回 IDLE 且空闲宽限 30s（可配）   │
                │ （本地 6 分钟兜底熄屏 IDLE_SLEEP_MS 触发后同样进入）    │
                └──────────────────────→ 回 LOW_POWER（断 WiFi 前先清  ──┘
                    ble 停广播? 否——LOW_POWER 全程广播）
```

状态明细：

| 状态 | 无线电 | 屏幕 | 电池读取 | 进入条件 | 迁出条件 |
|---|---|---|---|---|---|
| LOW_POWER | WiFi off（`WiFi.mode(WIFI_OFF)` / `esp_wifi_stop` 二选一，见下）；BLE 广播 100ms（复用 `ble_link.cpp:136-142`） | DISPOFF 黑（保持） | 降频：每 10min 读一次（`BATT_READ_INTERVAL_MS` 可加睡眠感知分支） | 见上（display=off + 宽限 30s） | ①门铃写 display=on；②巡检定时器（`LOW_POWER_CHECKIN_MIN` 默认 10min）；③BLE 连接但只读（心跳）→ 不出状态 |
| WIFI_CONNECTING | WiFi 开，BLE 保持广播（C6 双栈共存，收发时隙共享） | 保持黑 | — | 上述 ①② | 关联+首轮 poll 成功 → SERVING；超时 8s→ 失败计数+1，≤2 次直接重试；≥3 次→回 LOW_POWER 并把下次巡检提前到 1min（退避巡检） |
| SERVING | WiFi 常开（同现役） | 同现役逻辑 | 60s（现状） | — | display=off+IDLE+30s 宽限（`LOW_POWER_GRACE_MS`）；或首次巡检 poll 返回 display=off（见下） |

超时建议初值（全部收敛为具名宏，一处可调）：`WIFI_JOIN_TIMEOUT_MS=8000`、`WIFI_JOIN_FAIL_MAX=3`、`LOW_POWER_GRACE_MS=30000`、`LOW_POWER_CHECKIN_MIN=10`（分钟）、巡检窗口 `CHECKIN_WINDOW_MS=15000`、`BATT_SLEEP_READ_MIN=10`。

**关键细节决策（已定，实施照做）**：

1. **巡检（self check-in）**：LOW_POWER 每 10min 醒来一次 → 快速连 WiFi → poll 一轮 → 若 `display=off` 且 IDLE 立即回去（顺带捎带电池上报）；若 `display=on`（PC 曾点过唤醒但门铃失败）→ 自然转 SERVING。**这构成 BLE 全挂时的最终降级**：管理员点击的 display=True 在 PC 侧已生效，屏最迟 10min 内自愈亮屏；巡检轮询也会刷新 `screen_seen_at`，PC 侧据此可把状态从"休眠"纠正为"在线"。
2. **快速重连**：`WiFi.disconnect()` 会主动去关联并清缓存，LOW_POWER 期间用 `esp_wifi_stop()`/`WiFi.mode(WIFI_OFF)` 关射频保 RAM。**把上次成功会话的 `SSID/BSSID/channel` 缓存到 Preferences(NVS)**（`setup` 连上后与每次 SERVING 成功后更新），重连走 `WiFi.begin(ssid, pass, channel, bssid)` 免扫描直联（实测目标 ≤1s）；缓存缺失/失败再退化为无参 `WiFi.begin()`。WiFiManager 配网逻辑（`print_screen.ino:361-365`）只在"从未配网/连不上且重试耗尽"时才出现（复用它现有 `configPortalTimeout` 分支）。
3. **BLE 心跳期间的"不要唤醒"判定**：旧 PC 心跳（`ble_link.py:_heartbeat`）连接后只读 `link_status`+订阅电池，不写 `screen_state`。固件 `onConnect` 后开 500ms 窗口：窗口内无 `screen_state` 写 → 判定为心跳连接，留在 LOW_POWER（`ble_link_tick` 照常发电池 notify）；有写且 `display=on` → 转 WIFI_CONNECTING。窗口避免每次心跳把屏"点醒"。
4. **门铃预渲染（二期优化，B2 内）**：M-B1 门铃只置 wake 标志，画面由重连后的 poll 渲染（全链路 = BLE 建连 ~2s + WiFi 重连 ~1s + poll ≤1.5s ≈ 3.5~5s）；B2 再考虑门铃 payload 直接驱动渲染（复用 BLE 变体渲染核心，把 `onScreenState`（415-483 行）中"面板上电/断电"段替换为 DISPOFF 级 `lcd_screen_on()`——面板一直有电，GRAM 有效，`lcd_screen_on` 即亮），把感知延迟压到 ~2s。**注意：不要直接照搬 `onScreenState` 原函数**（含 ALDO3 语义），需抽"重绘守卫渲染核心"供 WiFi poll 与 BLE 门铃共用（qrShown/lastStatusKey 守卫变量在 66-69 行，两路共用同一套即可）。
5. **B 状态机不触碰 failCount 语义**：SERVING 内主控不可达（`onServerDown`）维持现状（亮维护画面、5s 退避、不死机、不因主控不可达而降功耗——降功耗只能由"display=off 且 IDLE"驱动，防止 PC 短暂故障时屏进入唤不醒的歧义态）。

### B2. 代码复用盘点（核实过的可用/需改清单）

**固件侧（firmware/print_screen/）**：

| 资产 | 状态 | 说明 |
|---|---|---|
| `ble_link.cpp/.h`（广播/GATT/notify/tick） | **直接可用**，仅条件编译改动 | 把 `#if defined(TRANSPORT_BLE)`（`ble_link.h:20`、`ble_link.cpp:10`）扩为 `TRANSPORT_BLE \|\| TRANSPORT_HYBRID`；GATT 契约与 UUID 不动（PC 侧代码零协议改动） |
| `ble_link.cpp:136-142` 广播启动 / 断连重开广播（52-57 行） | 直接可用 | LOW_POWER 全程广播 100ms（沿用 S0 甜点结论：500ms 档 Windows 建连 4~9s 不可用） |
| 电池 notify / 订阅补发（91-98, 161-167 行） | 直接可用 | 心跳连接期间自动上报（onSubscribe 置 0 → 30ms tick 补发），正好复用旧 PC 心跳语义 |
| `onScreenState`（`print_screen.ino:415-483`） | **不可整函数复用** | 含 ALDO3 断电/上电（424-441 行）。B 只抽其中渲染段（443-479 行重绘守卫逻辑），面板操作改为 DISPOFF 级 |
| `TRANSPORT_BLE` 的 setup/loop（485-522 行） | 仅作参照 | B 的 setup = 现役 WiFi setup + `ble_link_init()`（无 ALDO3 处理，panel 一直有电） |
| WiFiManager 配网 / NVS 网络信息 | 现有可用 | 增加"成功会话 BSSID/channel 缓存"（Preferences，新代码 ~20 行） |

**PC 侧（printshop/）**：

| 文件 | 改动 |
|---|---|
| `config.py:23-26` | 语义从"BLE 主传输开关"改为三态：`screen_transport: "wifi"（默认）/ "hybrid"`；`screen_ble_address` 非空 + `hybrid` 才启用 BLE 门铃（`hybrid` 而地址为空 → 启动时 warning 并回落 wifi，防呆） |
| `ble_link.py` | **主体原样复用**（懒建连/worker/心跳/重试/僵尸连接经验全部保留）。角色降级为门铃只需一处行为调整：push_state 只在"PC 判定屏在休眠"时触发（见 routes），并放宽旧"快速重醒窗口"语义（120s 保持连接在 hybrid 下无意义——屏醒后走 WiFi，建议 hybrid 下 `ble_fast_rewake_seconds` 不起作用/置 0，连接随写随断，缩短僵尸窗口）。新增一个小方法/回调供 routes 查询"最近一次门铃推送结果"，用于日志与 UI |
| `routes.py` | ① `screen_payload()`（111-120 行）增 `transport: "hybrid"` 与 `sleeping` 状态字段；② 在线判定加三态：poll 新鲜（≤10s）→ `awake`；poll 静默 > `SLEEP_GRACE_THRESHOLD`（= `LOW_POWER_GRACE_MS/1000 + margin`，建议 45s）且最后一次已知 display=off → `sleeping`；poll 静默且 display=on 或未知 → `offline`（真故障告警路径不变）；③ `battery_payload` 的 `stale_after`（139-147 行）：hybrid 下沿用 BLE 公式（心跳 900s+300s），心跳由既有 `_heartbeat_loop` 免费提供；④ `screen_state`/`wake` 端点不变，`wake`（339-342 行）响应里带当前 `sleeping` 态供托盘日志 |
| `session_manager.py` | **零改动**（wake/display/QR 刷新语义与传输无关） |
| `main.py:104-117` | 组装条件从 `if cfg.screen_ble_address` 改为 `if cfg.screen_transport == "hybrid" and cfg.screen_ble_address`；`on_change`（113-118 行）加闸：`if ble and screen_is_sleeping: ble.push_state(state)`——屏醒着（WiFi 在线）时状态走轮询，不产生无谓 BLE 连接（这是与旧 BLE 变体的本质区别，也是稳定性隔离的核心） |
| `tray.py` | 零改动（仍是 POST /api/screen/wake；返回体可带 sleeping 提示给托盘日志，可选） |
| `admin.html` | 状态栏"📡 小屏"增加第四态：**休眠中（蓝牙待命）**；唤醒失败提示"若 10 分钟内未亮，请检查屏端供电或重新插拔 USB" |
| 测试 | `tests/test_ble_link.py` 补 hybrid 门铃单测（FakeConnector 同协议，已有基座）；`test_routes.py` 补三态判定与 stale_after；`test_config.py` 补三态解析与防呆 |

### B3. 关键风险

| 风险 | 隔离/缓解 |
|---|---|
| **Windows BLE Central 历史稳定性（僵尸连接/回收慢/依赖 winrt）** | ① BLE 只承担唤醒与心跳：失败**不阻塞任何服务路径**——屏睡着时 BLE 叫不醒，PC 端 display=True 已置位，10min 巡检自愈兜底，管理员 UI 明确提示；② 屏醒后整个服务会话走 WiFi，BLE 连接随写随断（hybrid 下关闭 120s 保连窗口），僵尸窗口从"分钟级"缩到秒级；③ 沿用既有运维纪律：强杀进程后静置 5 分钟再建连（交接文档记录）；④ 失败风暴抑制：沿用 `_CONNECT_BACKOFF=5s` 与最多 30 次服务发现重试，hybrid 门铃额外加"单次点击最多 3 次推送尝试、间隔 2s，之后转 UI 提示等巡检" |
| **WiFi 重连耗时 → 亮屏延迟** | 预算是核心风险点：门铃→亮码全链路 3.5~5s（M-B1 渲染不预载）/ ~2s（B2 预渲染）。缓解：信道缓存直联（A2 决策 2）、巡检窗口设计、B2 门铃预渲染；验收时以实测定论（≤5s 判过，参照旧纯 BLE 冷唤醒 2.85s 的用户接受度） |
| **误入 LOW_POWER 导致唤不醒（display 失同步）** | 进 LOW_POWER 的唯一钥匙是"SERVING 内 poll 亲眼看到 display=off 且 IDLE"（+30s 宽限），杜绝本地猜测；PC 端 `sleeping` 态同样以"poll 静默 + 最后 display=off"判定，两侧判定独立但收敛一致；巡检每轮都重新同步，任何一侧误判 ≤10min 自愈 |
| **B 与 A 是否互斥/可叠加** | 结论：**空闲态策略互斥，工程产物可叠加**。A 是"WiFi 常挂 + CPU 睡"，B 是"WiFi 也关 + BLE 门铃"，同一时刻只取一个作为 idle 策略（B 严格更省）。但 B2（BLE 广播 + light sleep）必须复用 A 建立的重编 libs 工具链（加 `CONFIG_BT_LE_SLEEP_ENABLE` 等 BLE 睡眠键），且 B 的 SERVING 态如果还想让 WiFi 段也省，可以叠加 A 的 esp_pm_configure（SERVING 内同样开自动 light sleep，代码上就是两段配置共存）。若 BLE 门铃在店内实测不稳定，**一键回退 = 烧回 `TRANSPORT_WIFI`（= A 或现状）**，PC 端改 `screen_transport=wifi`，固件/PC 双回退与 2026-08-27 的 WiFi 回退先例同构 |
| 双栈（WiFi+BLE 同开于 C6）RAM/共存 | SERVING 态 BLE 只广播不连接（心跳连接在 LOW_POWER 态发生，不与 WiFi 并发），共存压力小；M-B1 验收打 `esp_get_free_heap_min()`，异常再降 BLE 广播间隔或延长巡检 |
| 旧纯 BLE 变体代码腐化 | `TRANSPORT_BLE` 保留不动（历史回切资产），新 `TRANSPORT_HYBRID` 与其共享 ble_link 层；README §十 的 GATT 契约不改 |

### B4. 实施拆分（可独立验收的 milestone）

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| M-B1（核心链路，**先不追求省电**） | `transport_config.h` 三选一；固件状态机 + LOW_POWER（WiFi off + BLE adv + CPU 常醒，`esp_wifi_stop` 级）；门铃唤醒→快速重连→SERVING；巡检回 LOW_POWER；PC 端三态判定/UI/推送闸（B2 全表）；pytest 全绿 + 既有 285 例不回归 | e2e：托盘点击 → ≤5s 亮码出二维码（对照 /screen 内容一致）；display=off 后 30s 屏进 LOW_POWER（串口日志 + 电流表出现 8~20mA 平台）；10min 巡检自动回连并上报电池；拔掉/停用主控→SERVING 不降功耗不乱跑；模拟 BLE 推送全失败 → UI 提示 + 10min 内巡检自愈亮屏；24h 待机无死机 |
| M-B2（省电叠加，依赖 A 的重编产物） | 重编 libs 追加 BLE 睡眠键（`CONFIG_BT_LE_SLEEP_ENABLE`，名字以 menuconfig 实查为准）+ `esp_pm_configure` light sleep；LOW_POWER 进入"广播间隙自动睡"；门铃预渲染（可选）；调优（巡检间隔/广播间隔/电池读取降频） | LOW_POWER 整机电流 ≤6mA（USB 电流表实测，多档记录）；门铃唤醒 ≤2.5s（预渲染档）或 ≤5s（poll 渲染档）；BLE 广播在睡眠下不断（PC 探针可随时连上，复用 `ble_link.selftest_read_link` 类探针）；功能回归同 M-B1 全过；sleep 期间旧版 PC 心跳（900s）仍能拉到电池 |
| M-B3（稳定化与长测） | 僵尸连接/强杀重启场景复测；PC 睡眠唤醒、exe 重启；巡检与门铃竞态收口；纯电池放电曲线（与 A 对照）；交接文档 + README 更新 | 48~72h 长测：屏侧零死机、PC 侧零 BLE 异常日志风暴；电池斜率续航 ≥ A 版对应工况 2 倍以上（预期目标）；全部场景有日志证据 |

### B5. 工作量粗估

- M-B1：2.5~3.5 人日（固件 1.5~2 + PC/测试 1~1.5）
- M-B2：2~3 人日（含 BLE 睡眠键重编增量与调优；若 A 已就绪则主要是 sdkconfig 追加一轮 + 固件睡眠适配）
- M-B3：1.5~2 人日 + 长测等待（可与其它任务并行）
- **B 合计约 6~8.5 人日**。

---

## 4. A、B 关系与推荐先后（汇总）

1. **推荐顺序：先 A（含 M-A0 前置），后 B；B2 依赖 A1 的构建链，B1 虽不依赖但同样受益于 A 阶段沉淀的电流基线与测量手段。**
2. 决策依据：
   - A 不动 PC、不动传输架构、无 Windows BLE 风险敞口，风险最小；按 A3 预期（熄屏 3~6mA 平均），对"USB 常插、电池仅应急"的店内形态已解决绝大部分功耗问题；
   - B 的增量价值只在"纯电池/停电续航"场景才凸显（把空闲电流再压一个量级），但它引入 PC 侧改动与 BLE Central 历史风险——用 B 的 10min 巡检兜底把风险敞口从"服务可用性"降级为"最长 10 分钟唤醒延迟"，再决定是否值得；
   - B 实施前设 **Go/No-Go 关口**：A 全量验收数据出来后，若 A 档电流已满足用户续航预期，B 可暂缓（代码方案本文件保留）；若用户要求电池整日续航，按 M-B1→B2→B3 推进。
3. 两案都预留的退路：固件 `TRANSPORT_WIFI`（现状语义）始终可烧回；PC 端 `screen_transport` 默认 `wifi`，env/config 可秒切，与 2026-08-27 WiFi 回退先例一致。

---

## 5. 实施前尚需现场确认的事项（不阻塞本方案定稿）

1. **工具链摸底**：arduino-cli data 目录与 esp32 core 实际版本号、`tools/esp32-arduino-libs/esp32c6/` 目录结构、官方 sdkconfig 全文备份——M-A0 第一步执行，含探路命令。
2. **版本对齐事实**：已装 core 3.3.x 对应的 arduino-esp32 标签与 IDF 分支（先按 v5.5 准备，以 lib-builder 与 core 的 CI/package 记录为准）。
3. **硬件实测项**（全部列为里程碑验收）：light sleep 谷底电流、面板/PMU 静态电流、BLE 睡眠下广播保持、Windows 僵尸连接在"随写随断"策略下的实际回收时长。

---

## 6. 参考

- 既有决策与数据：`docs/handoff-2026-08-26-task10b-s2-s4.md`、`docs/handoff-2026-08-27-task10b-wifi-gui.md`、`docs/superpowers/plans/2026-08-20-wechat-self-print.md` Task 10B 修订记录之三~之九（路线 A/B 定义、sdkconfig 实锤、9~12h 纯电池长测、僵尸连接纪律）
- [IDF v5.5 Power Management / sleep（C6）](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32c6/api-reference/system/sleep_modes.html)、[esp_pm.h](https://raw.githubusercontent.com/espressif/esp-idf/v5.5/components/esp_pm/include/esp_pm.h)、[FreeRTOS Kconfig（tickless）](https://raw.githubusercontent.com/espressif/esp-idf/v5.5/components/freertos/Kconfig)
- [esp32-arduino-lib-builder README](https://github.com/espressif/esp32-arduino-lib-builder/blob/master/README.md) 与 [configs/builds.json](https://raw.githubusercontent.com/espressif/esp32-arduino-lib-builder/master/configs/builds.json)（esp32c6 = qio/80m 组合）
- [ESP-IDF 低功耗模式总览（C6）](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-guides/low-power-mode/index.html)
- [TWT 实测指南（Hubble）](https://hubble.com/community/guides/using-target-wake-time-to-extend-esp32-battery/)、[ESP32-C6 功耗实测（cyril.by）](https://blog.cyril.by/en/free-speech/esp32-c6-power-consumption)、[Adafruit Feather C6 实测](https://learn.adafruit.com/adafruit-esp32-c6-feather?view=all)
- 自动 light sleep 高功耗个例：[espressif/esp-idf#13634](https://github.com/espressif/esp-idf/issues/13634)
