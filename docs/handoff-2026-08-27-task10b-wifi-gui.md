# 交接文档：Task 10B 产品修改 + WiFi 回退 + GUI 化（2026-08-27）

> 写给后续会话：本文档是 **2026-08-27 整轮产品修改与传输通道调整后的权威交接文档**。
> 覆盖分支 `feature/task10b-fixes-1-8`（HEAD `42cbeb6`）从 `d045581` 之后的 11 个提交：
> ①-⑤ 产品需求、WiFi 回退（A）、后台小屏状态（B）、GUI 化（C）、面板稳定性加固（D）、
> SQLite 跨线程修复与窗口化/日志化。
> 上一份 S2~S4 BLE 交接 `docs/handoff-2026-08-26-task10b-s2-s4.md` 已标记“已被取代”，
> 仅作 BLE 历史与长测数据备查；更早的小屏实机资料在 `docs/handoff-2026-08-24-esp32-screen.md`。

## 一句话现状

- **传输通道：WiFi 现役（BLE 回退保留）**。固件默认 `TRANSPORT_WIFI`，USB 常供电、不省电；电池仅应急。
- **GUI 窗口化**：默认以 pywebview 原生窗口加载 `/admin`，无黑色控制台；`--web` 可回浏览器模式。
- **生产运行中**：小屏已烧录 WiFi 默认固件并自动连 `KAIXUAN`，`dist/printshop.exe` 已重建为窗口化版本并运行。
- 分支 `feature/task10b-fixes-1-8` 尚未合并 master。
- pytest **285 绿**（`pytest --collect-only` 收集 285 例）。
- 最新 exe 已窗口化运行；日志在 exe 同级 `logs/printshop.log`。

## 背景与关键决策

1. **BLE 实测稳定性较差，退回 WiFi**。BLE 路线经过 S2~S4 与 24h 长测后，虽然链路和打包可行，
   但 Windows BLE Central 存在僵尸连接、回收慢、依赖 winrt 等运维复杂度；现场形态改为 WiFi 轮询更稳。
2. **USB 常供电，不考虑省电**。纯电池实测 9~12h，路线 A 证伪；电池仅作为断电应急备份。
3. **GUI 化**：生产管理员从“浏览器打开后台”升级为“原生窗口 + 系统托盘常驻”，更贴近桌面服务形态。
4. **日志化**：windowed 下无控制台，所有应用/uvicorn/vendor print 统一落入滚动日志文件。

## 产品需求 ①-⑤ 已实现

### ① 已完成任务可重新打印（`2d0e31a`）

- `JobStore.list_recent_done()` 返回最近 `30` 条 DONE 任务（按 `printed_at DESC, id DESC`）。
- `/api/jobs` 返回 **active 任务 + 最近 30 条 DONE**，并做 id 去重合并。
- `_printable_status` 放行 `PENDING / FAILED / DONE`，仅拦截 `PRINTING` 与 `DISCARDED`。
- `/api/jobs/{id}/print` 与 `print_batch` 对 DONE 任务先置回 PENDING 再打印。
- 后台前端“开始打印”按钮在选中 DONE 任务时显示为 **“🖨 重新打印”**。

### ② 托盘“点亮屏幕”同步打开管理后台（`f438284`）

- `TrayApp._wake_worker` 先 `POST /api/screen/wake`，无论 POST 是否成功都调用 `open_admin()`。
- `open_admin` 优先使用注入的 `admin_opener`（GUI 模式指向 `GuiController.show`），否则 `os.startfile` 浏览器打开。
- 避免 pystray 线程内做 HTTP/UI 阻塞：HTTP 放到一次性 daemon 线程。

### ③ 顾客文本中的公网 http/https 链接自动打开（`afce503`）

- `session_manager.extract_public_urls()` 从文本提取有序、去重 URL。
- `_is_public_url()` 安全校验：仅接受 `http/https`，拒绝 `localhost`、`.localhost`、`.local`、
  私网/保留/非全局 IP、多播地址；域名不做 DNS 解析。
- `_open_links()` 在会话内去重，最多打开 `Config.auto_open_max_links=3` 条；
  `Config.auto_open_links=True` 为总开关。
- `link_opener` 可注入（默认 `webbrowser.open`），测试可替换；异常只记录，不影响收文件流程。

### ④ 扫码确认登录后发送欢迎表情（`863d715`）

- `Config.login_greeting_text="😊"`、`Config.send_login_greeting=True`。
- `WeChatClient.send_text()` 包装 vendor `send_text`；vendor 发送后把 MsgID 记入 `_send_msg_ids`，
  已有回声过滤（自己发的消息不会再被当成顾客消息）。
- `SessionManager._send_login_greeting()` 在扫码确认登录（`confirmed`）后调用；每次登录最多尝试一次，
  发送失败/超时仅 warning，**绝不影响登录状态流转**。

### ⑤ 小屏服务态统一大字“打印中”（`3fabf31`）

- `display.cpp` 新增 `lcd_draw_text_scaled()` / `lcd_draw_text_center_scaled()`，支持整数倍缩放文本。
- `print_screen.ino` 新增 `STATUS_TEXT_SCALE=4`（字高 `16*4=64px`）、`TEXT_Y_LARGE` 垂直居中。
- `showStatus()` 中 `CONNECTED / RECEIVING / PRINTING` 统一显示大号 **“打印中”**；
  `SCANNED` 保持“二维码变暗 + 请在手机上确认登录”，其余回退“服务中，请稍候”。

## WiFi 回退（子任务 A，`e316ba6`）

- **决策背景**：BLE 实测稳定性较差；退回 WiFi 后 USB 常供电，不追求省电；电池仅应急。
- `transport_config.h` 默认 `TRANSPORT_WIFI`；两宏同时定义会编译报错。
- BLE 变体编译命令（源码零改动）：

  ```powershell
  arduino-cli compile --fqbn $fqbn --build-property compiler.cpp.extra_flags=-DTRANSPORT_BLE --build-path C:\arduino-build\print_screen_ble firmware\print_screen
  ```

  **必须用 `compiler.cpp.extra_flags`，不要覆盖 `build.extra_flags`**——esp32 core 默认宏（ESP32/CDC 等）被覆盖会导致编译崩。
- PC 侧 `Config.screen_ble_address` 默认 `""`（禁用 BLE Central，走 WiFi 轮询）；
  需要回切 BLE 时填 MAC 或设环境变量 `PRINTSHOP_SCREEN_BLE_ADDRESS`（`off`/`none` 可显式禁用）。
- 固件 WiFi 参数：
  - `SERVER_IP = "192.168.31.171"`（主控固定 IP）
  - `SERVER_PORT = 8000`
  - `POLL_INTERVAL_MS = 1500`
  - `POLL_BACKOFF_MS = 5000`
  - `HTTP_TIMEOUT_MS = 2500`
- `WiFi.setSleep(false)`：保持 WiFi 活跃，轮询不抖动。
- `doScreenOff()` 在 WiFi 分支**只软熄屏（DISPOFF + 黑屏），不切 ALDO3**；保留 6 分钟本地烧屏兜底（`IDLE_SLEEP_MS=360s`）。

## 后台小屏状态（子任务 B，`0ab1ac6`）

- `AppContext` 新增：
  - `screen_seen_at`：最近一次小屏 WiFi 轮询时间。
  - `mark_screen_seen()`：记录轮询并返回“离线→在线”边沿。
  - `screen_payload()`：`{transport: "wifi", online, last_seen_at, threshold_seconds}`。
  - `device_payload()`：统一广播 `{battery, screen}`。
- 在线判定阈值：`screen_online_threshold_seconds = 10.0`（WiFi 正常 1.5s、退避 5s，留足余量）。
- `/api/screen/state` 每次轮询先 `mark_screen_seen()`，屏幕状态变化或电池变化时广播 `device`。
- `/ws/admin` 连接后先发 `session` 快照，再发 `device` 快照，管理页打开即有状态。
- `admin.html` 状态栏新增 **“📡 小屏”** 指示：在线/离线/等待，前端每 5s 按 `last_seen_at` 定时重算离线。

## GUI 化（子任务 C，`3c911e7`）

- 使用 **pywebview 6.2.1 + Edge WebView2（edgechromium）**，加载 `http://127.0.0.1:{port}/admin`。
- 线程模型：
  - uvicorn 在 **daemon 线程**运行；
  - pystray 在 **daemon 线程**运行；
  - `webview.start(func=command_worker)` 启动 worker 线程消费 UI 命令；
  - **主线程阻塞在 GUI 事件循环**。
- 窗口行为：
  - 点窗口 **X**：`_on_closing` 拦截，隐藏到托盘，不退出服务；
  - 托盘“退出”：`on_quit` → `GuiController.quit()` → 向命令队列投 `QUIT` → worker 调 `window.destroy()` → `webview.start` 返回 → `server.should_exit=True` → uvicorn 优雅停机。
- `--web` 参数保留浏览器兜底模式（旧行为：托盘 + 主线程阻塞 uvicorn）。
- WebView2 缺失/初始化异常：捕获异常并 `MessageBoxW` 提示安装 WebView2 Runtime 或使用 `--web`。
- `printshop.spec` 增补 pywebview/pythonnet/clr/bottle/proxy_tools 等 hiddenimports；
  WebView2 相关 DLL 由 PyInstaller 自动收集。

## 面板稳定性加固（子任务 D，`b2ab83f`）

- `lcd_screen_on()/lcd_screen_off()` 改为返回 `bool`，失败 `ESP_LOGW` 告警，不重启、由调用方决定恢复。
- 新增 `lcd_panel_power_on_checked()`：与 `lcd_panel_power_on()` 相同的 ALDO3 电源时序，
  但 `esp_lcd_panel_init()` 失败不 abort，返回 `false` 供上层退避。
- WiFi 分支新增：
  - `FORCE_OFF_RETRY_MS = 60s`：熄屏状态下周期性强制补一次 DISPOFF+黑屏，兜底“状态脱同步导致屏幕常亮”；
  - `PANEL_REINIT_RETRY_MS = 30s`：亮屏失败后安全重初始化面板，并做 30s 退避防止反复电源循环。

## 已修复的关键缺陷

### GUI 模式 SQLite 跨线程（`93cb007`）

- 现象：GUI 模式下连接在主线程创建、由 uvicorn 事件循环线程使用，`/api/jobs` 与 finish 曾 500（`ProgrammingError`）。
- 修复：`JobStore` 使用 `sqlite3.connect(..., check_same_thread=False)`；应用层访问仍集中在单一事件循环线程，序列化安全。
- 提交：`93cb007`，并新增回归测试。

### 窗口化/日志化（`42cbeb6`）

- `printshop/logging_setup.py`：
  - 根 logger 挂 `RotatingFileHandler`：`logs/printshop.log`，单文件 5MB、备份 5 个；
  - `uvicorn_log_config()` 关闭 uvicorn 自带 handler，全部 `propagate` 到根 logger；
  - `redirect_std_streams_to_logging()` 在 `sys.stdout/stderr is None` 时替换为 `LogStream`，
    避免 windowed 下 vendor 裸 `print()` 崩溃，并让 print 输出也进日志文件。
- `printshop.spec`：`console=False`（窗口化无控制台）。
- 托盘菜单新增 **“打开运行日志”**：`os.startfile(log_path)` 打开 exe 同级日志。
- 提交：`42cbeb6`。

## 当前生产状态（2026-08-27 实机）

- **小屏固件**：已烧录 WiFi 默认固件，build path `C:/arduino-build/print_screen_wifi_d`。
- **配网**：板子自动连店内 WiFi `KAIXUAN`；小屏 IP `192.168.31.84`，主控 `192.168.31.171`。
- **主控 exe**：`dist/printshop.exe` 已重建为窗口化版本并运行；GUI 窗口标题“打印管理后台”。
- **日志**：`dist/logs/printshop.log`（已存在并滚动）。
- **后台状态**：`screen.online=true`、电池 `100%/USB`；`/api/service/finish` 与 `/api/screen/wake` 已验证返回 200。

## 部署 / 回滚操作速查

### WiFi 默认编译（现役，无额外宏）

```powershell
$fqbn = "esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB"
arduino-cli compile --fqbn $fqbn --build-path C:\arduino-build\print_screen_wifi firmware\print_screen
arduino-cli upload -p COM3 --fqbn $fqbn --input-dir C:\arduino-build\print_screen_wifi firmware\print_screen
```

### BLE 回切编译

```powershell
arduino-cli compile --fqbn $fqbn --build-property compiler.cpp.extra_flags=-DTRANSPORT_BLE --build-path C:\arduino-build\print_screen_ble firmware\print_screen
arduino-cli upload -p COM3 --fqbn $fqbn --input-dir C:\arduino-build\print_screen_ble firmware\print_screen
```

- 烧录前先停主控监听/避免占用 COM3；烧录约 10s。
- PC 回切 BLE：`config.py` 的 `screen_ble_address` 填小屏 MAC，或设 `PRINTSHOP_SCREEN_BLE_ADDRESS` 环境变量。
- 日志：exe 旁 `logs/printshop.log`，托盘“打开运行日志”直达。
- PyInstaller 重建：

  ```powershell
  .venv\Scripts\python -m PyInstaller --noconfirm --clean printshop.spec
  ```

## 遗留待办

- [ ] **真实顾客扫码全链路**：尚未用真实微信顾客完成“扫码→确认登录→发文件→打印→完成”全流程验证。
- [ ] **48~72h USB 循环长测**：未执行；当前仅有生产运行观察，需长时间循环/压力验证。
- [ ] **电池仅应急 2~4h 基本流程验证**：电池形态只作断电备份，仍建议验证短时应急可用。
- [ ] **PC 睡眠→唤醒**：重启路径已验证，睡眠唤醒未直接测。
- [ ] **GUI 人工冒烟**：已完成基础项（窗口/托盘/退出代码路径），真实点击流程仍建议再点一遍。
- [ ] **BLE 回切实机复测**：代码保留但未实机回切验证（冻结 exe 仍含 winrt 依赖）。

## 风险与注意

- **WiFi 依赖路由器**：店内必须关闭 AP 隔离/客户端隔离，否则小屏与主控互不可达。
- **主控固定 IP**：固件写死 `192.168.31.171`，主控 IP 变更需同步改固件并重烧。
- **windowed 无控制台**：排障必须看 `logs/printshop.log`（含 uvicorn 与 print 输出）。
- **BLE 代码保留但冻结 exe 仍包含 winrt 依赖**：体积与依赖仍在；若最终不再用 BLE 可后续裁剪。
- **分支未合并 master**：当前成果都在 `feature/task10b-fixes-1-8`，合并前不要丢失。

## 参考

- 本分支提交：`d045581` 为上一份交接基线；`2d0e31a` ~ `42cbeb6` 为本轮 11 个提交。
- 固件：`firmware/README.md`（WiFi 现役/BLE 回切/编译/烧录/AP 隔离）。
- 主控：`printshop/main.py`、`printshop/gui.py`、`printshop/logging_setup.py`、`printshop/config.py`、
  `printshop/session_manager.py`、`printshop/routes.py`、`printshop/jobs.py`、`printshop.spec`。
- 旧交接：`docs/handoff-2026-08-26-task10b-s2-s4.md`（BLE 历史与长测备查）。
