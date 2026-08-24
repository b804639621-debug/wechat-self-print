# 微信扫码自助打印 · 小屏固件（ESP32-C6 AMOLED 480×480）

本目录是**小屏端（取号屏）固件**：板载微雪 `ESP32-C6-Touch-AMOLED-2.16`，显示二维码 / 服务状态，供顾客微信扫码传文件打印。

- 硬件：微雪 ESP32-C6-Touch-AMOLED-2.16（ESP32-C6、2.16 寸 480×480 AMOLED 电容触摸屏、USB-C）
- 芯片：AXP2101 PMU（屏供电）、SH8601 显示（QSPI）、CST9220 触摸（I2C）
- 架构：Arduino + esp32 core **3.3.x**，**不使用 LVGL**（UI 极简，esp_lcd 直绘）

## 文件清单

| 文件 | 说明 |
|---|---|
| `print_screen.ino` | 主程序：配网、轮询主控、状态机、二维码、烧屏防护、触摸唤醒 |
| `display.h/.cpp` | **本项目自写**：无 LVGL 的显示驱动封装（esp_lcd 直绘 fillRect/点阵/文本） |
| `lcd_touch.cpp/.h` | 微雪例程原样复制（CST9220 触摸） |
| `user_config.h` | 微雪例程原样复制（引脚/显示参数，勿改引脚号） |
| `src/port_bsp/` | 微雪例程复制：`i2c_bsp`（I2C 总线，lcd_touch 依赖）、`axp2101_bsp`（PMU，含一处编译修复） |
| `src/externLib/` | 微雪例程复制：`esp_lcd_sh8601.c/.h`（SH8601 显示驱动，display.cpp 依赖） |
| `fonts.h` | 16×16 单色点阵字库（覆盖全部界面文案），由 `gen_font.py` 生成 |
| `gen_font.py` | 字库生成脚本（改动界面文案后重新运行，需 Pillow） |

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
   - **QRCode**（ricmoo）
   - **XPowersLib**：库管理器可能没有，从微雪仓手动装：
     - `git clone https://gitee.com/waveshare/ESP32-C6-Touch-AMOLED-2.16`
     - 把 `01_Arduino_Libraries/XPowersLib` 整个文件夹拷入 Arduino 库目录
       （Windows 默认 `%USERPROFILE%\Documents\Arduino\libraries\`）

> **AXP2101 电源管理必须装**：这块板子的屏幕供电走 AXP2101 PMU，固件里 `initPMU()` 在显示初始化之前调用，不初始化 PMU 屏不会亮（本任务最高发坑）。

## 二、编译选项

打开 `print_screen.ino`，Tools（工具）菜单对照微雪仓根目录 `Tools Configuration.png`：

- 开发板：**ESP32C6 Dev Module**
- Flash Size：**16MB**（如编译报 flash 溢出可保持默认分区表，Flash Size 设 16MB 即可）
- **USB CDC On Boot：Enabled**（C6 原生 USB 直连；不开则串口监视器无日志）
- 其余默认

## 三、编译前修改主控 IP

`print_screen.ino` 顶部 `SERVER_IP` 写死主控固定 IP（默认 `192.168.1.100`）：

```cpp
#define SERVER_IP  "192.168.1.100"   // 改成店内主控实际 IP
#define SERVER_PORT 8000
```

主控侧服务（`printshop`）默认监听 `0.0.0.0:8000`，与 `GET /api/screen/state`、`POST /api/screen/wake` 接口契约一致。

## 四、烧录

1. USB-C 直连电脑（C6 原生 USB，无需串口驱动），选对应 COM 口 → 点“上传”。
2. 上传失败 / 认不出端口 → **按住板子侧边 BOOT 键再重新上电**进入下载模式，再正常上传。
3. 烧完开串口监视器（115200）看日志；无输出先查 **USB CDC On Boot 是否 Enabled**。
4. 期望日志顺序：`[hw] AXP2101 PMU init done` → `[hw] SH8601 display init done` → `[hw] CST9220 touch init done` → `[net] WiFi connected, IP=...`。

## 五、首次配网流程

1. 首次上电固件自建热点 **PRINT-SETUP**。
2. 手机连上该热点 → 自动弹出配网页（不弹则浏览器访问 `192.168.4.1`）→ 填店内 WiFi 密码 → 保存。
3. 板子重启后自动连店内 WiFi 并轮询主控；屏幕先显示“连接中...”，拉到状态后显示二维码。
4. 以后想重配：改 `wm.setConfigPortalTimeout(180)` 前的逻辑，或按住 BOOT 烧一次空程序即可（也可给 WiFiManager 加 reset 按钮，首版未做）。

## 六、路由器 AP 隔离警示（部署前置检查）

⚠️ **店内路由器必须关闭 AP 隔离 / 客户端隔离**。开启后小屏与大屏（主控）虽然都连着店内 WiFi，但互相不可达，小屏将一直显示“连接中/系统维护中”。这是现场最高发坑，务必在部署前检查。

## 七、实机验证 checklist

- [ ] 首次配网成功（PRINT-SETUP → 店内 WiFi），重启自动回连
- [ ] 屏亮，二维码清晰、**白底黑码**（非反色），微信可扫（iOS + 安卓各一）
- [ ] 二维码与主控浏览器调试页 `/screen` 同屏对照，内容一致
- [ ] 息屏/点亮切换 ≤ 2s：待机 3 分钟自动息屏；点触屏幕唤醒（POST wake 后主控刷新二维码）
- [ ] 状态文案切换：扫码后“二维码变暗 + 请在手机上确认登录” → 连接后“已连接，请发送文件” → 收文件“已收到 N 个文件，请稍候” → “打印中，请稍候”
- [ ] 拔掉主控网线 → 屏幕显示“系统维护中”，恢复后自动回到二维码，不死机
- [ ] 连续 2 小时待机无异常（验证烧屏防护：亮度 80%、随机偏移、定时刷新、自动熄屏）

## 八、与微雪例程的差异说明

- **不使用 LVGL**：例程 `09_LVGL_V9_Test` 的 `bsp_lvgl_port.cpp` 强依赖 LVGL（渲染/刷屏/触摸事件回调），本项目 UI 只有二维码 + 单行文字，故自行写了 `display.cpp`，仅复用其中的 SPI/QSPI 初始化、SH8601 面板创建与初始化命令序列，并暴露 `lcd_fill_rect` / `lcd_draw_mono_scaled` / `lcd_draw_text*` / 背光 / 熄屏原语。
- **`axp2101_bsp.cpp` 编译修复**：例程仓该文件使用 `ESP_LOGI/ESP_LOGE(TAG, ...)` 但未定义 `TAG`，本项目补了 `static const char *TAG = "axp2101_bsp";`，其它代码未动。
- **二维码颜色**：按需求走白底黑码（非反色），微信兼容性更稳；`SCANNED` 状态用 50% 灰码实现“二维码变暗”。
