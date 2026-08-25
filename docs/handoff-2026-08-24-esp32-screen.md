# 交接文档：ESP32 小屏实机调试会话（2026-08-24）

> 写给后续会话：本文档打包 2026-08-24 下午「小屏闪烁/不显示二维码」调试会话的全部上下文，
> 读完应能无缝接手。项目主计划在 `docs/superpowers/plans/2026-08-20-wechat-self-print.md`。
>
> **2026-08-25 增补**：装机锂电池，电池电量上报已实装并实机验证（提交 `81967b2`），
> 管理页状态栏可查看剩余电量。详见文末「2026-08-25 增补：电池电量上报」与 `firmware/README.md` §九。

## 一句话现状

ESP32 小屏固件（`firmware/print_screen`）**实机正常工作**：配网 ✓、轮询主控 ✓、二维码稳定显示且微信可扫 ✓、触摸唤醒 ✓、**电池电量上报 ✓（08-25 新增）**。08-24 修复的两个实机 bug（闪烁 + 无二维码）已提交 `06a5cd7`；08-25 电池电量上报已提交 `81967b2`。**工作区干净，无未提交改动。**

## 本次修复的两个实机 bug（均已在硬件上验证）

### Bug 1：屏幕每 1.5s 明暗交替闪烁

- **症状**：亮屏后屏幕白/黑来回跳，频率与轮询周期（1.5s）一致；黑底文字画面（"等待主控..."）稳定。
- **根因**：`print_screen.ino` 的 `loop()` 顶部缓存 `now = millis()`，随后 `pollScreen()` 内 HTTP 请求耗时 ~300ms 并更新 `lastStateChange = millis()`；回到 `loop()` 的熄屏兜底判断仍用**旧 `now`** 减去**新的 `lastStateChange`** → unsigned 下溢成 ~2³² → 恒 ≥ IDLE_SLEEP_MS → 每次轮询都误触发 `doScreenOff()`，与下一次轮询的 `doScreenOn()` 形成明暗循环。
- **取证过程**：串口打桩日志抓到 `[dbg] off <- idle fallback dt=4294967192`（≈2³²，下溢实锤）。
- **修复**：兜底判断改用新鲜 `millis()`（ino 中有注释说明）。

### Bug 2：白屏无二维码（文字正常）

- **症状**：白底 + 页脚文字正常，二维码整片消失；自检发现 SELFTEST 字样下方有一条不属于绘制清单的"细窄黑条"（被挤错位的行数据）。
- **根因**：`display.cpp` 的 `lcd_draw_mono_scaled` 用「1 行 × 405 次」密集小推送打 esp_lcd QSPI 异步事务——`esp_lcd_panel_io_tx_color` 是异步 DMA（返回≠完成），密集小事务会丢失/乱序；且全局 `rowbuf` 复用无同步存在 DMA/CPU 数据竞争。对照：fill_rect（32 行/次）、文字（16 行/次）的大块推送一直正常。
- **修复**（`display.cpp`）：
  1. 注册 `on_color_trans_done` 回调 + 信号量，所有 `draw_bitmap` 后调 `lcd_wait_dma()` 再复用 rowbuf；
  2. `lcd_draw_mono_scaled` 改为按 32 行条带推送（与 fill_rect 同节奏）。
- **验证**：自检四图元（黑矩形/棋盘格/硬编码 QR/文字）全出 → 正式固件二维码稳定显示、微信扫码识别出登录链接 ✓。

**防御性注释已写入代码**，经验总结写入 `firmware/README.md` §八（后来者改显示代码前必读）。

## 当前 git 状态（已全部提交，工作区干净）

- `06a5cd7` 固件实机修复：闪烁 + 无二维码（含 .gitignore、firmware/README §八踩坑、vendored ricmoo_qrcode 入库、SERVER_IP=192.168.31.171、下溢修复）
- `81967b2` 电池电量上报（2026-08-25，7 文件 +262/-5，详见文末增补节）

## 环境/工具链备忘（本次新装，后续会话直接用）

| 项 | 值 |
|---|---|
| arduino-cli | `E:\kaifa\自助打印\tools\arduino-cli\arduino-cli.exe`（1.5.1，已 gitignore） |
| esp32 core | 3.3.3（装在 arduino-cli 数据目录） |
| arduino-cli 数据目录 | **C:\arduino15**（7GB 真实目录；config 的 `directories.data` 已指向它） |
| 构建目录 | **C:\arduino-build\print_screen**（增量编译缓存，勿删） |
| FQBN | `esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB` |
| 板子串口 | **COM3**（USB CDC，115200） |
| 库目录 | `D:\OneDrive\文档\Arduino\libraries\`（**OneDrive 重定向**，非 %USERPROFILE%\Documents） |
| 库版本 | ArduinoJson 7.4.3 / WiFiManager 2.0.17 / XPowersLib 0.3.3；QRCode 不用装（已 vendored） |
| 主控 | 192.168.31.171:8000（printshop.exe，当前运行 PID 会变动） |
| 板子 | 192.168.31.84（DHCP；WiFi 凭据存 NVS，重烧固件不丢） |

**关键坑**：
- riscv32 链接器不认中文路径 → 数据目录/构建目录必须纯 ASCII（C:\arduino15、C:\arduino-build）。
- github.com 直连极慢 → esp32 包索引走 gh-proxy.com 镜像版（若需重装：索引文件在 `%TEMP%\opencode\package_esp32_index_cn.json`，需本地 HTTP 服务托管；注意 Arduino 官方索引也含 esp32 定义，缓存副本里要删掉否则冲突）。
- 默认 4MB 分区固件占 99% 贴顶 → 必须用 `PartitionScheme=default_8MB`。
- `espressif__qrcode` 内置组件的 `qrcode.h` 会抢占库管理器装的 ricmoo/QRCode → 必须 vendored 到 sketch 的 `src/` 下用相对路径 include。

## 常用操作命令

```powershell
$fqbn = "esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB"
# 编译（增量，~1.5min）
& "E:\kaifa\自助打印\tools\arduino-cli\arduino-cli.exe" compile --fqbn $fqbn --build-path "C:\arduino-build\print_screen" "E:\kaifa\自助打印\firmware\print_screen"
# 烧录（~10s）
& "E:\kaifa\自助打印\tools\arduino-cli\arduino-cli.exe" upload -p COM3 --fqbn $fqbn --input-dir "C:\arduino-build\print_screen"
# 唤醒小屏（主控 display=on + 刷新二维码，180s 后自动息屏）
Invoke-RestMethod -Method Post -Uri "http://192.168.31.171:8000/api/screen/wake"
# 查主控状态
Invoke-RestMethod -Uri "http://192.168.31.171:8000/api/screen/state"
# 查电池电量（.battery：pct/mv/charging/vbus/updated_at；需 81967b2 之后的主控代码）
(Invoke-RestMethod -Uri "http://192.168.31.171:8000/api/screen/state").battery
# 串口抓取（60s 带时间戳）：C:\arduino-build\serial_cap_ts.py（pyserial 已装在 .venv）
& "E:\kaifa\自助打印\.venv\Scripts\python.exe" "C:\arduino-build\serial_cap_ts.py"   # 输出 C:\arduino-build\serial3.log
```

**串口抓取注意**：固件稳态是静默的（无日志=正常）；要看启动日志需先开抓取再让板子重启（拔插 USB）。PowerShell 的 `Start-Process -RedirectStandardOutput` 在本机对 arduino-cli monitor 不好使，用 pyserial 脚本最稳。

## 待办（按优先级）

1. **主控 exe 重新打包**：`dist\printshop.exe` 已落后源码 6 个提交（含 C1 SCANNED 状态机死锁修复 c92a187、`81967b2` 电池电量上报的主控端 routes.py/admin.html——**不重打包管理页看不到电量**）。打包：`.venv\Scripts\python -m PyInstaller printshop.spec`，打包后再跑联调。
2. **Task 10 Step 4 剩余实机项**（需现场/真实扫码流程）：
   - 状态文案切换：扫码 → "二维码变暗+请在手机上确认登录" → "已连接，请发送文件" → "已收到 N 个文件" → "打印中"
   - 与浏览器调试页 `/screen` 双屏对照
   - iOS + 安卓各扫一台（本次只验了一台）
   - 连续 2 小时待机（烧屏防护综合验证）
3. **Task 9 端到端联调未开始**（等现场实机/微信/LibreOffice 环境齐备）。
4. 部署前置检查提醒：店内路由器**关闭 AP 隔离**（否则小屏访问不到主控）。

## 关键文件

- 固件主体：`firmware/print_screen/print_screen.ino`（轮询/状态机/烧屏防护/触摸唤醒/电池上报）
- 显示驱动：`firmware/print_screen/display.cpp`（**改前必读 README §八两个坑**）
- 构建/烧录指南：`firmware/README.md`（含 arduino-cli 路线；§九 = 电池电量上报机制与参数契约）
- 主计划：`docs/superpowers/plans/2026-08-20-wechat-self-print.md`（Task 10 = 本固件）
- 设计契约：`docs/superpowers/specs/2026-08-20-wechat-self-print-design.md` §6.1（小屏状态机与烧屏防护 4 条）

## 2026-08-25 增补：电池电量上报（提交 81967b2）

装机锂电池后实装。**充电/供电切换无需固件干预**（AXP2101 硬件自动完成，充电参数 500mA 恒流/50mA 截止在 `Custom_PmicRegisterInit()` 早已配好）；电量读取为本次新增。

- **链路**：固件 60s 读一次 AXP2101 电量计缓存（`axp2101_bsp.cpp` 末尾项目自有扩展
  `Axp2101_EnableBatteryMonitor`/`Axp2101_ReadBattery`，与例程原样复制部分有注释分隔）→
  每 1.5s 轮询 `GET /api/screen/state` 捎带 `batt_pct/batt_mv/batt_chg/batt_vbus` query 参数 →
  主控 `routes.py` 宽容解析存内存（任一字段非法整体忽略，**绝不把小屏轮询打成 422**）→
  管理页（/admin）状态栏显示；百分比/充电态变化时 WS 推 `device` 消息，连接即收快照。
- **管理页显示**：`🔋 85%·充电中` / `🔋 42%` / `🔋 98%·USB`；<20% 且非充电**红色**；
  >3 分钟未上报**置灰**（兼作板子在线指示）；悬停看电压与上报时间。
- **兼容性**：响应体新增 `battery` 字段，固件 ArduinoJson 自动忽略未知字段——旧主控配新固件不炸（但看不到电量）；无电池时固件不追加参数，主控 `battery=null`。
- **实机验证（08-25）**：编译烧录 COM3 ✓；串口外 API 全链路：板子上报 `25% / 3.81V / 充电中`（新电池补电中），`updated_at` 随轮询持续刷新 ✓；/admin 200 含电量元素 ✓；pytest 206 例全绿（新增 5 例，3 例旧 WS 用例适配 device 快照）。
- **部署注意**：`dist\printshop.exe` 不含本次主控改动——管理页要看到电量必须重打包或源码运行（见待办 1）。
