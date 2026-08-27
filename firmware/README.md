# 微信扫码自助打印 · 小屏固件（ESP32-C6 AMOLED 480×480）

本目录是**小屏端（取号屏）固件**：板载微雪 `ESP32-C6-Touch-AMOLED-2.16`，显示二维码 / 服务状态，供顾客微信扫码传文件打印。

- 硬件：微雪 ESP32-C6-Touch-AMOLED-2.16（ESP32-C6、2.16 寸 480×480 AMOLED 电容触摸屏、USB-C）
- 芯片：AXP2101 PMU（屏供电）、SH8601 显示（QSPI）、CST9220 触摸（I2C）
- 架构：Arduino + esp32 core **3.3.x**，**不使用 LVGL**（UI 极简，esp_lcd 直绘）
- 传输通道（2026-08 Task 10B 起）：**WiFi（`TRANSPORT_WIFI`，现役默认，USB 常供电不省电）**——WiFiManager 配网 + HTTP 轮询主控；BLE 保留为可回切变体（`-DTRANSPORT_BLE`）。契约与运维见 §十

## 文件清单

| 文件 | 说明 |
|---|---|
| `print_screen.ino` | 主程序：状态机、二维码、烧屏防护；**传输通道二选一**（默认 WiFi：配网 + 轮询；BLE 可回切：主控 GATT 写驱动渲染）；**触摸唤醒已移除（2026-08-25 决策），唤醒仅主控推送 display=on（=管理员托盘），WiFi 变体亦无触摸** |
| `transport_config.h` | 传输通道编译开关（Task 10B）：默认 `TRANSPORT_WIFI`，可注入 `-DTRANSPORT_BLE` 回切 BLE（两宏同时定义编译报错），见 §二 |
| `ble_link.cpp/.h` | BLE 外设层（可回切变体）：GATT 服务/100ms 广播/60s 电池 notify/绘制完成 ack，见 §十 |
| `display.h/.cpp` | **本项目自写**：无 LVGL 的显示驱动封装（esp_lcd 直绘 fillRect/点阵/文本） |
| `lcd_touch.cpp/.h` | 微雪例程原样复制（CST9220 触摸；**2026-08-25 起停用**，保留文件备查，主程序不再 include） |
| `user_config.h` | 微雪例程原样复制（引脚/显示参数，勿改引脚号） |
| `src/port_bsp/` | 微雪例程复制：`i2c_bsp`（I2C 总线，PMU 使用）、`axp2101_bsp`（PMU，含一处编译修复 + 项目自有电池读取扩展，见 §九） |
| `src/externLib/` | 微雪例程复制：`esp_lcd_sh8601.c/.h`（SH8601 显示驱动，display.cpp 依赖） |
| `src/ricmoo_qrcode/` | ricmoo/QRCode 库**随 sketch  vendored**（qrcode.c/.h）：esp32 core 3.3.x 内置 `espressif__qrcode` 组件的同名头文件会抢占库管理器安装的 QRCode，导致编不过；vendored 后 include 走相对路径 `./src/ricmoo_qrcode/qrcode.h`，不再依赖库管理器安装 |
| `fonts.h` | 16×16 单色点阵字库（覆盖全部界面文案），由 `gen_font.py` 生成 |
| `gen_font.py` | 字库生成脚本（改动界面文案后重新运行，需 Pillow） |

另：`ble_spike/`（2026-08-25 新增）——Task 10B Step S0 的 BLE 可行性 spike 固件（一次性测量工具）：
关 WiFi 纯 BLE（NimBLE）广播 + GATT `PRINTSHOP-SCREEN` 服务（screen_state 写 / battery_report notify /
link_status read+notify），空闲 ALDO3 面板断电、写 display=on 冷启动画二维码。display.cpp 副本已拆出
可重入的 `lcd_panel_power_on/off()`（S1 整合回 print_screen 的 TRANSPORT_BLE 开关，BLE 可回切固件即此通道，
GATT 契约/UUID 与 spike 完全一致）。
PC 侧测量脚本在 `tools/poc/ble_spike_pc.py`、`tools/poc/ble_probe_stages.py`，
实测数据与结论见 `docs/debug-logs-2026-08-25/task10b-s0-spike.md`。

> 驱动文件来源：微雪官方 Gitee 仓 `https://gitee.com/waveshare/ESP32-C6-Touch-AMOLED-2.16`
> 路径 `02_Example/Arduino-v3.3.3/09_LVGL_V9_Test/`，commit `b77979b8302e1f94b81a7f18696ce60d7afde94a`。
> 除 `axp2101_bsp.cpp` 补了例程仓缺失的 `TAG` 定义（编译修复）外，其余为原样复制。

## 一、Arduino IDE 环境搭建

1. 安装 [Arduino IDE 2.x](https://www.arduino.cc/en/software)。
2. 文件 → 首选项 → 附加开发板管理器网址，添加：
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
3. 开发板管理器安装 **esp32 by Espressif Systems**，选 **3.3.x**（与微雪例程同代；ESP32-C6 必须 3.x）。
4. 安装库（库管理器）：
   - **WiFiManager**（tzapu，≥ 2.0.17，已兼容 esp32 core 3.x）
   - **ArduinoJson**（**7.x**，本固件按 v7 API 编写，勿装 v6）
   - ~~QRCode（ricmoo）~~ **不用装**：已 vendored 到 `src/ricmoo_qrcode/`（esp32 core 3.3.x 内置同名 `qrcode.h` 会抢占）
   - **XPowersLib**：库管理器可能没有，从微雪仓手动装：
     - `git clone https://gitee.com/waveshare/ESP32-C6-Touch-AMOLED-2.16`
     - 把 `01_Arduino_Libraries/XPowersLib` 整个文件夹拷入 Arduino 库目录
       （Windows 默认 `%USERPROFILE%\Documents\Arduino\libraries\`；若文档目录被 OneDrive 重定向则在 `D:\OneDrive\文档\Arduino\libraries\`）

> 依赖注记：**ArduinoJson 两通道都需要**（BLE 变体解析 screen_state JSON 也用）；**WiFiManager 仅 `TRANSPORT_WIFI` 现役变体需要**（BLE 可回切变体不配网、不开 WiFi，编译时未 include 该库）。

> **AXP2101 电源管理必须装**：这块板子的屏幕供电走 AXP2101 PMU，固件里 `initPMU()` 在显示初始化之前调用，不初始化 PMU 屏不会亮（本任务最高发坑）。

## 二、编译选项

打开 `print_screen.ino`，Tools（工具）菜单对照微雪仓根目录 `Tools Configuration.png`：

- 开发板：**ESP32C6 Dev Module**
- Flash Size：**16MB**（分区表需选 **8MB with spiffs** 及以上——默认 4MB 分区固件占 99% 贴顶；FQBN 对应 `PartitionScheme=default_8MB`）
- **USB CDC On Boot：Enabled**（C6 原生 USB 直连；不开则串口监视器无日志）
- 其余默认

**传输通道二选一（Task 10B，`transport_config.h` 编译开关）**：默认 **`TRANSPORT_WIFI`**（现役：WiFiManager 配网 + HTTP 轮询，USB 常供电不省电）；BLE **`TRANSPORT_BLE`**（可回切：BLE 广播 + GATT 收发，**不初始化 CST9220 触摸、不开 WiFi**）。用 arduino-cli `--build-property` 注入 `-DTRANSPORT_BLE` 回切即可，**无需改源码**（见下方命令）；两宏同时定义会在编译期报错。

### 命令行（arduino-cli）等效路线

Arduino IDE 可选；本仓库实机烧录用的是 arduino-cli（`tools/arduino-cli/`，已 gitignore）：

```powershell
$fqbn = "esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB"
arduino-cli compile --fqbn $fqbn --build-path C:\arduino-build\print_screen firmware\print_screen
arduino-cli upload -p COM3 --fqbn $fqbn --input-dir C:\arduino-build\print_screen
```

**BLE 可回切变体编译**：注入编译宏覆盖默认 `TRANSPORT_WIFI`（源码零改动）：

```powershell
arduino-cli compile --fqbn $fqbn --build-property compiler.cpp.extra_flags=-DTRANSPORT_BLE --build-path C:\arduino-build\print_screen_ble firmware\print_screen
```

注意：必须注入 `compiler.cpp.extra_flags`（只追加编译宏），**不要**覆盖 `build.extra_flags`——esp32 core 的 `build.extra_flags` 默认值含 ESP32/CDC 等必需宏，覆盖只传一个宏会让编译崩（2026-08-25 实踩，见计划修订记录之五）。

注意：esp32 工具链的 riscv32 链接器**不能处理中文/非 ASCII 路径**，`--build-path` 与 arduino-cli 数据目录必须用纯 ASCII 路径（如 `C:\arduino-build`、`C:\arduino15`）。

## 三、编译前修改主控 IP（仅 TRANSPORT_WIFI 现役变体）

BLE 可回切变体不开 WiFi、不连路由器，此配置不用；绑定关系在主控侧 `config.py screen_ble_address`（见 §十）。
`print_screen.ino` 的 `TRANSPORT_WIFI` 分支顶部 `SERVER_IP` 写死主控固定 IP（默认 `192.168.31.171`，当前店内实际 IP）：

```cpp
#define SERVER_IP  "192.168.31.171"  // 改成店内主控实际 IP
#define SERVER_PORT 8000
```

主控侧服务（`printshop`）默认监听 `0.0.0.0:8000`，与 `GET /api/screen/state`、`POST /api/screen/wake` 接口契约一致（BLE 变体写屏即走这两个语义的推送版，见 §十）。

## 四、烧录

1. USB-C 直连电脑（C6 原生 USB，无需串口驱动），选对应 COM 口 → 点“上传”。
2. 上传失败 / 认不出端口 → **按住板子侧边 BOOT 键再重新上电**进入下载模式，再正常上传。
3. 烧完开串口监视器（115200）看日志；无输出先查 **USB CDC On Boot 是否 Enabled**。
4. 期望日志顺序：
   - WiFi 现役变体：`[hw] AXP2101 PMU init done` → `[hw] SH8601 display init done` → `[net] WiFi connected, IP=...`（无 `[hw] CST9220 touch init done`，触摸已停用）；
   - BLE 可回切变体：`[hw] AXP2101 PMU init done` → `[ble] advertising as PRINT-SCREEN, interval=100ms` → `[ble] MAC=...`（public 地址，主控绑定用）；**开机不初始化显示**——面板首次点亮推迟到第一次 display=on 推送（冷启动 ~1.1s 已被接受，空闲期零面板功耗）；建连时见 `[ble] connected` / `[ble] MTU negotiated: 517`。

## 五、首次配网流程（仅 TRANSPORT_WIFI 现役变体）

1. 首次上电固件自建热点 **PRINT-SETUP**。
2. 手机连上该热点 → 自动弹出配网页（不弹则浏览器访问 `192.168.4.1`）→ 填店内 WiFi 密码 → 保存。
3. 板子重启后自动连店内 WiFi 并轮询主控；屏幕先显示“连接中...”，拉到状态后显示二维码。
4. 以后想重配：改 `wm.setConfigPortalTimeout(180)` 前的逻辑，或按住 BOOT 烧一次空程序即可（也可给 WiFiManager 加 reset 按钮，首版未做）。
5. BLE 可回切变体**免配网**：设备 public 地址直连，主控侧绑定见 §十与 `docs/handoff-2026-08-26-task10b-s2-s4.md`。

## 六、路由器 AP 隔离警示（仅 TRANSPORT_WIFI 现役变体需检查）

⚠️ **店内路由器必须关闭 AP 隔离 / 客户端隔离**。开启后小屏与大屏（主控）虽然都连着店内 WiFi，但互相不可达，小屏将一直显示“连接中/系统维护中”。这是现场最高发坑，务必在部署前检查。BLE 可回切通道走蓝牙直连、不经 WiFi/路由器，不受 AP 隔离影响。

## 七、实机验证 checklist

- [ ] 首次配网成功（PRINT-SETUP → 店内 WiFi），重启自动回连
- [ ] 屏亮，二维码清晰、**白底黑码**（非反色），微信可扫（iOS + 安卓各一）
- [ ] 二维码与主控浏览器调试页 `/screen` 同屏对照，内容一致
- [ ] 息屏/点亮：托盘唤醒 → 主控 BLE 写 display=on → 冷建连 + 面板冷启动 **~2.85s**（S3 降档 120ms 后）；息屏后 120s 快速重醒窗口内免建连（仅面板冷启动 ~0.65s）；待机 display=off 面板 ALDO3 断电
- [ ] 状态文案切换：扫码后“二维码变暗 + 请在手机上确认登录” → 连接后“已连接，请发送文件” → 收文件“已收到 N 个文件，请稍候” → “打印中，请稍候”
- [ ] 主控崩溃/断连（服务保持期）→ 屏幕显示“系统维护中”（黑底白字、30s ±6px 微移防烧屏），主控恢复后推送新状态自动恢复，不死机；空闲期断连保持休眠黑屏，不主动亮屏报错
- [ ] BLE 链路体检：`dist\printshop.exe --ble-selftest` 输出 `BLE SELFTEST PASS`、退出码 0（亮占位屏 → 读 link_status → 息屏）；或 `scripts/smoke_dist.ps1` 全绿
- [ ] 连续 2 小时待机无异常（验证烧屏防护：亮度 60%、完成服务/无操作自动熄屏；亮屏仅在服务期间）

## 八、与微雪例程的差异说明

- **不使用 LVGL**：例程 `09_LVGL_V9_Test` 的 `bsp_lvgl_port.cpp` 强依赖 LVGL（渲染/刷屏/触摸事件回调），本项目 UI 只有二维码 + 单行文字，故自行写了 `display.cpp`，仅复用其中的 SPI/QSPI 初始化、SH8601 面板创建与初始化命令序列，并暴露 `lcd_fill_rect` / `lcd_draw_mono_scaled` / `lcd_draw_text*` / 背光 / 熄屏原语。
- **esp_lcd 直绘两个实机踩过的坑**（2026-08-24 定位，已修）：
  1. `tx_color` 是**异步 DMA 事务**，且「1 行 × 几百次」的密集小推送会把 QSPI 推送打挂（实机症状：大块填充/文字正常，逐行推送的二维码整片消失或挤成细条）。`display.cpp` 里所有 `draw_bitmap` 后都跟 `lcd_wait_dma()`（`on_color_trans_done` 信号量同步），`lcd_draw_mono_scaled` 按 32 行条带推送（与 fill_rect 同款节奏）。**改显示代码时不要移除这个等待。**
  2. 主循环的息屏兜底曾用循环顶部缓存的 `now` 与 `pollScreen()` 内刚更新的 `lastStateChange` 相减 → 无符号下溢成 ~2^32 → 每次轮询都误触发熄屏（屏每 1.5s 明暗交替）。凡「HTTP 耗时调用前后」的时间比较，必须用新鲜 `millis()`。
- **`axp2101_bsp.cpp` 编译修复**：例程仓该文件使用 `ESP_LOGI/ESP_LOGE(TAG, ...)` 但未定义 `TAG`，本项目补了 `static const char *TAG = "axp2101_bsp";`，其它代码未动。
- **二维码颜色**：按需求走白底黑码（非反色），微信兼容性更稳；`SCANNED` 状态用 50% 灰码实现“二维码变暗”。
- **烧屏防护策略变更**（2026-08-25，替代原「随机偏移 + 定时微移 + 80% 亮度」方案）：
  亮屏仅在服务期间（扫码→打印），完成服务/3 分钟无操作即熄屏，静画停留时间短，
  亮度降至 60%，烧屏风险低。像素平移类定时重绘已删除——实机发现**重绘本身是花屏暴露面**。
- **花屏与 DMA 自愈**（2026-08-25 实机）：症状「二维码区逐行右移斜纹乱块，白底/页脚文字正常」。
  **根因已定位：SH8601 GRAM 寻址对奇数 x 起点错位**——405 宽点阵 x0=37（奇）必花，
  480 宽填充/文字 x0=0（偶）恒正常；早期"偶发"是随机偏移 ±10px 让 x 起点在奇偶间跳变。
  修复：`drawQR` 的 x 起点强制偶数对齐（`& ~1`）。**凡局部宽度绘制，x 起点必须偶数。**
  另加工程兜底：`lcd_wait_dma` 连续超时 8 次自动 `ESP.restart()` 自愈，超时与重启
  打串口日志留证（本次排查中实测零超时——传输层健康，兜底保留防未知劣化）。
  **显示异常时先抓串口日志（`tools/poc/com3_monitor.py`）再分析。**
- **面板冷启动降档**（2026-08-26 Task 10B S3 实测）：0x11（sleep-out）后延时 600ms → **120ms**
  （`display.cpp` 具名宏 `SH8601_SLEEP_OUT_DELAY_MS`，=SH8601 规格典型值；注释内含 200/150/120ms
  分档实测结论）。逐档烧录 e2e 10/10 全过、fw_ms 恒 733/683/653ms、串口零异常；
  冷唤醒全链路 3.3s → **~2.85s**。改动前先看宏注释，勿拍脑袋再提。

## 九、电池电量上报（2026-08 装机电池后新增）

板子带锂电池时，固件把电量上报主控，**管理页（/admin）状态栏显示剩余电量**；小屏 UI 不显示。现役 TRANSPORT_WIFI 随 HTTP 轮询捎带 batt_* 参数；BLE 可回切变体走 `battery_report` notify（见 §十 GATT 契约），两通道电池字段语义一致（`pct/mv/chg/vbus`）。

- **读取**：AXP2101 自带电量计寄存器直读百分比（`getBatteryPercent`，非电压估算），另读电压 mV、
  充电中（`isCharging`）、USB 接入（`isVbusIn`）。`axp2101_bsp.cpp` 末尾的两个函数
  （`Axp2101_EnableBatteryMonitor` / `Axp2101_ReadBattery`）是**项目自有扩展**，与上方例程原样复制部分有注释分隔；
  `initPMU()` 里先开电池电压 ADC 才能读 mV。
- **节奏**：`updateBatteryCache()` 每 60s 读一次 PMU 缓存（I2C 与 PMU 共总线，不宜高频）；
  上报则随每 1.5s 的 `GET /api/screen/state` 捎带 query 参数，不新增 HTTP 请求。无电池（`isBatteryConnect`=false）不追加参数。
- **参数契约**（主控 `routes.py` 宽容解析，任一字段非法则整体忽略，绝不影响状态响应）：

  | 参数 | 取值 | 说明 |
  |---|---|---|
  | `batt_pct` | 0~100 | 电量百分比（芯片电量计） |
  | `batt_mv` | 0~5000 | 电池电压 mV |
  | `batt_chg` | 0/1 | 充电中 |
  | `batt_vbus` | 0/1 | USB 供电接入 |

- **主控侧**：参数存入内存（重启即失，下次轮询即恢复）；响应体与 WS 快照带 `battery` 字段
  （固件 ArduinoJson 自动忽略未知字段，旧主控配新固件也兼容）；百分比/充电态变化时向管理页推 `device` 消息。
- **管理页显示**：`🔋 85%·充电中` / `🔋 42%` / `🔋 98%·USB`；**<20% 且非充电中红色**；
  超后端下发的 `stale_after` 未上报置灰（BLE=心跳间隔+300s=1200s，WiFi 轮询=180s；兼作板子在线指示）；悬停看电压与上报时间。
- **充电管理无需固件干预**：AXP2101 硬件自动完成 USB/电池供电切换与充电（充电参数
  500mA 恒流/50mA 截止在 `Custom_PmicRegisterInit()` 已配好）。

## 十、BLE 传输架构（Task 10B，可回切变体）

2026-08 起现役传输通道为 **TRANSPORT_WIFI**（`transport_config.h` 默认，USB 常供电不省电）；
BLE 路径完整保留为可回切变体（见 §二）。主控侧 BLE Central 实现在 `printshop/ble_link.py`
（winrt 直连，不走 bleak），运维语义（绑定/回切/自检/僵尸连接）见
`docs/handoff-2026-08-26-task10b-s2-s4.md`。

- **工作模型**：空闲期固件仅 BLE 广播（100ms，CPU 常醒——低功耗路线 A，估 8~20mA，见计划
  Task 10B 修订记录之四/之五）；主控需要亮屏时**主动连接** → 写 `screen_state` → 固件面板上电
  冷启动渲染 → notify `link_status` 绘制完成 ack。服务结束主控写 display=off → 面板 ALDO3 彻底
  断电，主控侧保持连接 120s「快速重醒窗口」覆盖连续顾客，超时主动断开回广播。
- **GATT 三特征契约**（服务 UUID `6e410000-3f2b-4f5c-9a01-9d6a3b21c001`，与 S0 spike 一致勿改）：

  | 特征 | 属性 | 方向 | 载荷 |
  |---|---|---|---|
  | `screen_state`（6e410001…） | Write | PC → 屏 | `{state, display, qr_content, file_count, message}`，变化即推；display=on 亮屏 / off 断电 |
  | `battery_report`（6e410002…） | Notify | 屏 → PC | `{"pct":N,"mv":N,"chg":0/1,"vbus":0/1}`，连接期间每 60s 上报；订阅后补发首包（T3 竞态修复） |
  | `link_status`（6e410003…） | Read + Notify | 屏 → PC | Read 值 `{"fw":..,"uptime_s":N,"mtu":N,"adv_ms":100}`；Notify 为绘制完成 ack `{"drawn":1,"ms":N}` |

- **MTU**：NimBLE 本地 `setMTU(517)`，连接协商恒 517（≥185 硬条款），单次写整条 JSON。
- **唤醒入口**：仅主控 BLE 写 `display=on`（管理员托盘 → `POST /api/screen/wake`）。**触摸唤醒
  已移除**（2026-08-25 决策：TRANSPORT_BLE 不初始化 CST9220、无 handleTouch 轮询；`lcd_touch.cpp`
  保留备查）。
- **维护态语义**：仅「服务保持期（连接存续中）断连」才常亮“系统维护中”（黑底白字、30s ±6px
  微移防烧屏）；空闲期断连保持休眠黑屏，不主动亮屏报错。
- **两段式写**：二维码过期需换新码时，先推 display=on + 空 qr（占位“二维码加载中...”，含面板
  冷启动 fw ~567ms）→ 新码到手再推覆盖（fw ~132ms）；重复推送被重绘守卫跳过（fw 0ms）。
- **面板电源状态机**：display=off → `lcd_panel_power_off()`（ALDO3 断电，空闲省电核心）；
  display=on → 首次 `lcd_init` / 断电后 `lcd_panel_power_on()` 冷启动
  （`SH8601_SLEEP_OUT_DELAY_MS=120`，全链路 ~2.85s；快速重醒窗口内免建连，仅冷启动 ~0.65s）。
  DMA 同步/自愈与烧屏防护策略与 WiFi 变体一致（见 §八）。
- **主控侧运维**：默认空串走 WiFi 轮询；BLE 回切时填 `config.py screen_ble_address`（或 env `PRINTSHOP_SCREEN_BLE_ADDRESS` 设 MAC）；诊断 `--ble-selftest`；冒烟 `scripts/smoke_dist.ps1`；电池置灰
  `stale_after` = 心跳间隔 + 300s。详见交接文档。
