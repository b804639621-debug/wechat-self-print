/*
 * ============================================================================
 * print_screen.ino —— 微信扫码自助打印 · 小屏端固件（ESP32-C6 / 480x480 AMOLED）
 * 板子：微雪 ESP32-C6-Touch-AMOLED-2.16
 *   - AXP2101 PMU（XPowersLib）  ：屏供电，必须先初始化，否则屏不亮
 *   - SH8601 显示（esp_lcd）     ：显示驱动在 display.cpp，无 LVGL 直绘
 *   - CST9220 触摸：已停用（2026-08-25 决策：放弃触摸唤醒，不再初始化）
 * 传输通道（编译期二选一，见 transport_config.h）：
 *   - TRANSPORT_WIFI（默认，现役）：WiFiManager 配网 + 每 1.5s 轮询主控
 *     GET /api/screen/state（USB 常供电，不省电）。
 *   - TRANSPORT_BLE（可回切变体，Task 10B）：BLE 外设。空闲面板 ALDO3 彻底断电、
 *     WiFi 关闭、低占空比广播（100ms）；主控连接后写 screen_state 驱动亮屏/熄屏/
 *     状态画面，绘制完成 notify link_status 作 ack，电池连接期间 notify 上报。
 * 功能：
 *   - 状态机：IDLE 显示二维码 / SCANNED 二维码变暗 / 服务中状态画面 / 打印中
 *   - 烧屏防护：亮度 60%、无操作/完成服务熄屏（亮屏仅在服务期间）
 *   - 唤醒入口：仅主控推送 display=on 点亮（触摸唤醒 2026-08-25 起放弃：
 *     Task 10B 决策 BLE 改造后触摸不可用）
 *   - 电池监控：60s 读一次 AXP2101 电量计，WiFi 随轮询捎带 batt_* 参数、
 *     BLE 连接期间 notify battery_report 上报主控（管理页显示剩余电量）
 * 依赖库（Arduino 库管理器）：WiFiManager（仅 WiFi 通道）、ArduinoJson(v7)、
 *   XPowersLib（从微雪例程仓 01_Arduino_Libraries 拷入）。
 *   ricmoo/QRCode 已 vendored 到 src/ricmoo_qrcode/（esp32 core 3.3.x 内置同名头文件会抢占）。
 * ============================================================================
 */

#include "transport_config.h"

#include <Arduino.h>
#include "./src/ricmoo_qrcode/qrcode.h"     /* ricmoo/QRCode（vendored 副本：esp32 core 3.3.x 内置同名 qrcode.h 会抢占） */
#include "user_config.h"
#include "display.h"
#include "./src/port_bsp/i2c_bsp.h"
#include "./src/port_bsp/axp2101_bsp.h"

#if defined(TRANSPORT_WIFI)
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiManager.h>
#include <ArduinoJson.h>
#endif

#if defined(TRANSPORT_BLE)
#include "ble_link.h"
#endif

/* ============================ 公共部分（两通道共用） ============================ */

/* ---- 可配置参数 ---- */
#define QR_SCALE         9                   /* 二维码放大倍数（480/29≈16，留边距取 9） */
#define QR_MAX_VERSION   7                   /* 固定 v7：45 模块*9=405px 可留页脚；v7-ECC_MEDIUM 容量 122B 足够 */
#define QR_MAX_MODULES   (QR_MAX_VERSION * 4 + 17)
#define QR_MAX_BOTTOM    456                 /* 二维码下沿上限（页脚在 460） */
#define FOOTER_Y         460                 /* 页脚“微信扫码·传文件打印” */
#define TEXT_Y           232                 /* 单行状态文字垂直居中位置 */
#define STATUS_TEXT_SCALE 4                 /* 大号状态文字放大倍数：字高 64px */
#define TEXT_Y_LARGE      ((LCD_H - 16 * STATUS_TEXT_SCALE) / 2)  /* 大号状态文字垂直居中 */
#define MAINT_SHIFT_MS   (30UL * 1000)       /* 维护态白字防烧屏：每 30s 换一次绘制位置 */
#define BATT_READ_INTERVAL_MS (60UL * 1000)  /* 电池缓存读取间隔（电量变化慢，I2C 与 PMU 共总线不宜高频） */

/* ---- 全局状态 ---- */
I2cMasterBus I2cMasterBus_(GPIO_NUM_7, GPIO_NUM_8, I2C_NUM_0); /* 引脚见 user_config.h */

String curState = "";          /* 主控下发的状态（IDLE/SCANNED/...） */
String qrShown = "";           /* 最近一次绘制的二维码内容 */
String maintLastText = "";     /* 维护画面守卫：上次绘制的文案（主控可达/推送新状态时复位） */
int8_t maintLastJitter = 6;    /* 维护画面守卫：上次实际绘制的偏移 */
String lastStatusKey = "";     /* 最近一次绘制的服务状态画面（状态+文件数） */
bool screenOn = false;         /* 屏当前是否点亮 */

/* 电池缓存（AXP2101 电量计直读，WiFi 随轮询上报主控 / BLE 由 notify 上报） */
int  battPct = -1;                  /* 电量百分比（-1=未读到） */
int  battMv = 0;                    /* 电池电压 mV */
bool battChg = false;               /* 充电中 */
bool battVbus = false;              /* USB 供电接入 */
bool battValid = false;             /* 是否检测到电池（无可上报数据=false） */
unsigned long lastBattRead = 0;     /* 上次读 PMU 时间（0=未读，下次即读） */

/* ---- 初始化 ---- */

void initPMU(void) {
  /* AXP2101 PMU：必须先于显示（屏供电）。地址 0x34，与例程一致 */
  Custom_PmicPortInit(&I2cMasterBus_, 0x34);
  Axp2101_EnableBatteryMonitor();   /* 开电池电压 ADC（读电量前置，2026-08 装机电池） */
  Serial.println("[hw] AXP2101 PMU init done");
}

/* ---- 电池监控 ---- */

/* 60s 读一次 AXP2101 缓存到全局（电量变化慢；I2C 与 PMU 共总线，不宜高频）。
 * lastBattRead=0 时首次必读（开机首轮轮询即可上报）。 */
void updateBatteryCache(void) {
  unsigned long now = millis();
  if (lastBattRead && (now - lastBattRead < BATT_READ_INTERVAL_MS)) return;
  lastBattRead = now;
  int pct, mv; bool chg, vbus;
  if (Axp2101_ReadBattery(&pct, &mv, &chg, &vbus)) {
    if (!battValid) Serial.println("[bat] 检测到电池");
    battPct = pct; battMv = mv; battChg = chg; battVbus = vbus;
    battValid = true;
  } else {
    if (battValid) Serial.println("[bat] 电池移除");
    battValid = false;
  }
}

/* ---- 画面绘制 ---- */

/* 绘制二维码（白底黑码，非反色），dim=true 时用灰码实现“二维码变暗” */
void drawQR(const String& content, bool dim) {
  uint8_t qrcodeData[qrcode_getBufferSize(QR_MAX_VERSION)];
  QRCode qrc;
  /* 固定 v7 + ECC_MEDIUM（容量 122B，微信登录 URL 约 48 字符足够）。
   * 注意：上游 ricmoo 库 qrcode_initText 内部不做容量检查、超长也恒返回 0，
   * 因此“逐版本尝试、失败降级”的写法会让内容被 v3 截断成损坏二维码，已废弃。 */
  if (qrcode_initText(&qrc, qrcodeData, QR_MAX_VERSION, ECC_MEDIUM, content.c_str()) != 0) {
    showStatusText("二维码生成失败");
    return;
  }

  int size = qrc.size * QR_SCALE;
  /* 固定居中绘制（2026-08-25 起取消 ±10px 随机偏移：亮屏仅在服务期间、完成服务
   * 即熄屏 + 亮度降至 60%，静画停留时间短，烧屏风险低；少重绘也压缩花屏暴露面）。
   * x 起点强制偶数对齐：实机对照（2026-08-25）480 宽填充/文字（x0=0 偶）恒正常，
   * 405 宽点阵 x0=37（奇）恒出现逐行右移斜纹花屏，疑 SH8601 GRAM 按 2px 粒度寻址，
   * 奇数 x_start 触发行错位。 */
  int ox = ((LCD_W - size) / 2) & ~1;
  int oy = (LCD_H - size) / 2 - 6;
  if (oy + size > QR_MAX_BOTTOM) oy = QR_MAX_BOTTOM - size;

  uint16_t fg = dim ? LCD_COLOR_GRAY : LCD_COLOR_BLACK;   /* 变暗用 50% 灰 */
  lcd_clear(LCD_COLOR_WHITE);                              /* 白底 */

  /* 打包为行优先单色位图（LSB=最左），交给驱动放大绘制 */
  int bw = (qrc.size + 7) / 8;
  uint8_t mono[(QR_MAX_MODULES + 7) / 8 * QR_MAX_MODULES];   /* v7=45 → 6*45=270 字节 */
  memset(mono, 0, sizeof(mono));
  for (int y = 0; y < qrc.size; y++) {
    for (int x = 0; x < qrc.size; x++) {
      if (qrcode_getModule(&qrc, x, y)) mono[(size_t)y * bw + (x >> 3)] |= (uint8_t)(1 << (x & 7));
    }
  }
  lcd_draw_mono_scaled(ox, oy, qrc.size, qrc.size, QR_SCALE, mono, fg, LCD_COLOR_WHITE);
  lcd_draw_text_center(FOOTER_Y, "微信扫码·传文件打印", LCD_COLOR_BLACK, LCD_COLOR_WHITE);
}

/* 服务中状态画面（任务⑤：扫码后的服务态统一为大号“打印中”） */
void showStatus(const char* state, int fileCount, const String& qr) {
  if (strcmp(state, "SCANNED") == 0) {
    drawQR(qr, true);   /* 二维码变暗 */
    /* 确认引导文案渲染在页脚位置（覆盖二维码页脚，覆盖区同为 16px 高） */
    lcd_draw_text_center(FOOTER_Y, "请在手机上确认登录", LCD_COLOR_BLACK, LCD_COLOR_WHITE);
  } else if (strcmp(state, "CONNECTED") == 0 ||
             strcmp(state, "RECEIVING") == 0 ||
             strcmp(state, "PRINTING") == 0) {
    showStatusTextLarge("打印中");
  } else {
    showStatusTextLarge("服务中，请稍候");
  }
}

/* 白底黑字单行状态文字 */
void showStatusText(const char* s) {
  lcd_clear(LCD_COLOR_WHITE);
  lcd_draw_text_center(TEXT_Y, s, LCD_COLOR_BLACK, LCD_COLOR_WHITE);
}

/* 白底黑字大号状态文字（任务⑤：扫码后服务态统一大号“打印中”） */
void showStatusTextLarge(const char* s) {
  lcd_clear(LCD_COLOR_WHITE);
  lcd_draw_text_center_scaled(TEXT_Y_LARGE, s, STATUS_TEXT_SCALE,
                              LCD_COLOR_BLACK, LCD_COLOR_WHITE);
}

void showMaintenance(const char* s) {
  static int8_t jitter = 6;
  static unsigned long lastFlip = 0;

  unsigned long now = millis();
  if (now - lastFlip >= MAINT_SHIFT_MS) {  /* 每 30s 换一次绘制位置（防白字静态烧屏） */
    lastFlip = now;
    jitter = (int8_t)(-jitter);            /* ±6px 交替（初值 6，避免 0 恒 0） */
  }
  if (String(s) == maintLastText && jitter == maintLastJitter) return;  /* 文案/位置未变 → 不重绘 */
  maintLastText = s;
  maintLastJitter = jitter;
  lcd_clear(LCD_COLOR_BLACK);
  lcd_draw_text_center(TEXT_Y + jitter, s, LCD_COLOR_WHITE, LCD_COLOR_BLACK);
}

/* ==================== WiFi 通道（现役，USB 常供电不省电） ==================== */

#if defined(TRANSPORT_WIFI)

/* ---- 可配置参数 ---- */
#define SERVER_IP        "192.168.31.171"    /* 主控固定 IP（配网时按店内实际改） */
#define SERVER_PORT      8000
#define POLL_INTERVAL_MS 1500                /* 正常轮询间隔 */
#define POLL_BACKOFF_MS  5000                /* 主控不可达退避重试间隔 */
#define HTTP_TIMEOUT_MS  2500                /* HTTP 连接/读取超时 */
#define IDLE_SLEEP_MS    (360UL * 1000)      /* 烧屏防护 本地兜底：6 分钟（主控 3 分钟先熄） */

/* ---- 全局状态（WiFi 专用） ---- */
bool everConnected = false;    /* 是否曾成功拉到主控状态 */
int  failCount = 0;            /* 连续失败次数（驱动退避） */
unsigned long lastPoll = 0;
unsigned long lastStateChange = 0;  /* 状态/亮屏时间（本地熄屏兜底计时） */

void initDisplay(void) {
  lcd_init(I2cMasterBus_);     /* SH8601 显示 + 背光 60%（lcd_init 内部转调 lcd_panel_power_on） */
  Serial.println("[hw] SH8601 display init done");
}

/* 黑底白字提示（连接/维护，AMOLED 黑像素不发光，兼防烧屏） */
void showTextOnBlack(const char* s) {
  showMaintenance(s);
}

/* ---- 轮询与状态机 ---- */

void pollScreen(void) {
  String url = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) + "/api/screen/state";
  if (battValid) {   /* 捎带电池上报：主控存内存，管理页显示；无电池不追加（参数契约见 firmware/README §九） */
    char q[72];
    snprintf(q, sizeof(q), "?batt_pct=%d&batt_mv=%d&batt_chg=%d&batt_vbus=%d",
             battPct, battMv, battChg ? 1 : 0, battVbus ? 1 : 0);
    url += q;
  }
  HTTPClient http;
  if (!http.begin(url.c_str())) { onServerDown(); return; }
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  int code = http.GET();
  if (code != HTTP_CODE_OK) { http.end(); onServerDown(); return; }
  String payload = http.getString();
  http.end();

  /* ArduinoJson v7：JsonDocument 动态分配，勿用 v6 的 StaticJsonDocument */
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) { onServerDown(); return; }

  const char* state = doc["state"] | "IDLE";
  bool dispOn = (strcmp(doc["display"] | "on", "on") == 0);
  int  fc = doc["file_count"] | 0;
  String qr = doc["qr_content"] | "";

  failCount = 0;
  everConnected = true;
  maintLastText = "";   /* 主控可达即复位维护画面守卫：再宕机时 showMaintenance 必重绘，不留正常画面残影 */

  if (String(state) != curState) {          /* 状态变化才重置熄屏计时 */
    curState = state;
    lastStateChange = millis();
  }

  if (!dispOn) { doScreenOff(); return; }   /* 主控要求息屏（烧屏防护 主路径） */

  bool wasOff = !screenOn;
  doScreenOn();
  if (wasOff) lastStateChange = millis();

  if (curState == "IDLE") {
    lastStatusKey = "";
    /* 内容变化 / 刚点亮 → 重绘二维码（2026-08-25 起取消定时微移重绘：亮屏仅在
     * 服务期间、完成服务即熄屏，静画停留时间短；少重绘也压缩了花屏暴露面） */
    bool needRedraw = wasOff || (qr != qrShown);
    if (needRedraw) {
      drawQR(qr, false);
      qrShown = qr;
    }
  } else {
    /* 服务流程中保持常亮；仅状态/文件数变化时重绘，避免每 1.5s 全屏重绘 */
    String key = curState + "_" + fc;
    if (key != lastStatusKey) {
      lastStatusKey = key;
      qrShown = "";
      showStatus(curState.c_str(), fc, qr);
    }
  }
}

/* 主控不可达：退避重试，不死机 */
void onServerDown(void) {
  failCount++;
  if (failCount == 1) Serial.println("[http] 主控不可达，退避重试...");
  /* 清掉已绘状态，主控恢复后首次 pollScreen 强制重绘（否则 needRedraw 恒假，滞留维护画面） */
  qrShown = "";
  lastStatusKey = "";
  doScreenOn();                /* 熄屏时也要亮屏提示“系统维护中”（黑底白字，防烧屏） */
  if (everConnected) {
    showMaintenance("系统维护中");
  } else {
    showMaintenance("连接中...");
  }
}

/* ---- 亮/熄屏 ---- */

void doScreenOff(void) {
  if (!screenOn) return;
  screenOn = false;
  /* USB 常态供电：只软熄屏（DISPOFF），不切断面板电源（勿改成 ALDO3 断电）。 */
  lcd_screen_off();
  Serial.println("[lcd] screen off");
}

void doScreenOn(void) {
  if (screenOn) return;
  screenOn = true;
  lcd_screen_on();
  Serial.println("[lcd] screen on");
}

/* ---- 初始化与主循环 ---- */

void setup(void) {
  Serial.begin(115200);
  delay(500);                  /* USB CDC 枚举等待，避免首行日志丢失 */

  initPMU();                   /* ① 电源：不初始化屏不会亮（最高发坑） */
  initDisplay();               /* ② 显示（触摸 CST9220 已停用，不再初始化） */

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);        /* 保持 WiFi 活跃，轮询不抖动 */
  WiFiManager wm;
  wm.setConfigPortalTimeout(180);
  if (!wm.autoConnect("PRINT-SETUP")) {  /* 首次自建热点配网 */
    ESP.restart();             /* 配网超时重启重试 */
  }
  Serial.printf("[net] WiFi connected, IP=%s\n", WiFi.localIP().toString().c_str());

  screenOn = true;
  lastStateChange = millis();
  showTextOnBlack("等待主控...");
  lastPoll = millis();
}

void loop(void) {
  updateBatteryCache();        /* 电池缓存：60s 读一次 PMU（内部有间隔守卫） */

  unsigned long now = millis();
  unsigned long interval = (failCount > 0) ? POLL_BACKOFF_MS : POLL_INTERVAL_MS;
  if (now - lastPoll >= interval) {
    lastPoll = now;
    pollScreen();
  }

  /* 烧屏防护 本地兜底：IDLE 且长时间无变化 → 熄屏（正常由主控 display=off 驱动）。
   * 主控不可达（failCount>0）时让"系统维护中"常亮提示，不进入此分支。
   * 注意：必须用新鲜 millis()，不能用循环顶部的 now——pollScreen() 内 HTTP 耗时
   * 数百 ms，若期间刚亮屏更新了 lastStateChange，用旧 now 相减会无符号下溢成 ~2^32，
   * 导致每次轮询都误触发熄屏（on/off 每 1.5s 循环闪烁的根因）。 */
  if (failCount == 0 && curState == "IDLE" && screenOn && (millis() - lastStateChange >= IDLE_SLEEP_MS)) {
    doScreenOff();
  }
  delay(30);
}

#endif // TRANSPORT_WIFI

/* ==================== BLE 通道（可回切变体，Task 10B） ==================== */

#if defined(TRANSPORT_BLE)

/* ---- 全局状态（BLE 专用） ---- */
bool panelReady = false;   /* lcd_init 是否已执行（SPI/panel 句柄/DMA 缓冲仅创建一次） */
bool maintMode  = false;   /* 断连维护态：服务期断连常亮"系统维护中"（黑底白字 + 30s 抖动） */

/* 电池读取回调：ble_link 不碰 I2C，由 .ino 注册 Axp2101_ReadBattery 包装 */
bool pmuReadBattery(int* pct, int* mv, bool* chg, bool* vbus) {
  return Axp2101_ReadBattery(pct, mv, chg, vbus);
}

/* BLE 状态写入驱动（现有轮询状态机的推送版）：
 *  - display=off：清绘制守卫 + 复位状态机 + 面板彻底断电（ALDO3 off），保持 BLE 连接；
 *    2 分钟快速重醒窗口由主控侧计时，超时主控主动断连，固件不参与。
 *  - display=on：面板就绪（首次 lcd_init / 断电后 lcd_panel_power_on），按状态渲染，
 *    完成后 notify link_status 作绘制完成 ack，供主控测全链路延迟。 */
void onScreenState(const char* state, bool displayOn, const String& qr, int fileCount) {
  unsigned long t0 = millis();

  if (!displayOn) {
    /* 熄屏：清绘制守卫 + 状态机复位；面板已上电则彻底断电（ALDO3 off，空闲省电核心） */
    qrShown = "";
    lastStatusKey = "";
    curState = "";
    maintMode = false;
    if (panelReady && screenOn) {
      lcd_panel_power_off();
      screenOn = false;
      Serial.println("[scr] display=off, panel powered down");
    }
    return;
  }

  /* ---- 亮屏：面板就绪 ---- */
  maintMode = false;          /* 主控可推送即清维护态 */
  maintLastText = "";         /* 复位维护画面守卫：下次断连 showMaintenance 必重绘，不留正常画面残影 */
  bool wasOff = !screenOn;    /* 面板刚上电：ALDO3 断电后 GRAM 已丢失，必须整屏重绘 */
  if (!panelReady) {
    lcd_init(I2cMasterBus_);  /* 首次：SPI/panel 创建 + 上电 + 初始化序列（冷启动 ~1.1s，已被接受） */
    panelReady = true;
  } else if (!screenOn) {
    lcd_panel_power_on();     /* 断电后重上电 + 重发初始化序列 */
  }
  screenOn = true;

  curState = state;

  /* ---- 按状态渲染（沿用 WiFi 路径的重绘守卫：重复推送同状态不整屏重绘） ---- */
  if (curState == "IDLE") {
    if (qr.length() > 0) {
      lastStatusKey = "";
      bool needRedraw = wasOff || (qr != qrShown);  /* 内容变化 / 刚上电 → 重绘 */
      if (needRedraw) {
        drawQR(qr, false);
        qrShown = qr;
      }
    } else {
      /* 二维码未就绪：亮屏占位文案。主控两段式写：先 display=on+空 qr 亮屏占位，
       * 新码到手再推 display=on+qr 覆盖（旧码过期场景） */
      qrShown = "";
      if (wasOff || lastStatusKey != "LOADING") {
        lastStatusKey = "LOADING";
        showStatusText("二维码加载中...");
      }
    }
  } else {
    /* 服务流程中：仅状态/文件数变化时重绘；wasOff 强制重绘（GRAM 已丢失）。
     * CONNECTED/RECEIVING/PRINTING 文案统一为大号“打印中”，不再依赖 fileCount，
     * 因此这些状态的 key 只取状态名，避免收到第 2 个文件时无意义整屏重绘。 */
    String key;
    if (curState == "CONNECTED" || curState == "RECEIVING" || curState == "PRINTING") {
      key = curState;
    } else {
      key = String(curState) + "_" + fileCount;
    }
    if (wasOff || key != lastStatusKey) {
      lastStatusKey = key;
      qrShown = "";
      showStatus(curState.c_str(), fileCount, qr);
    }
  }

  /* 绘制完成 ack（无论是否跳过重绘都回：主控按每次 display=on 写等待 ack） */
  ble_link_notify_drawn(millis() - t0);
}

void setup(void) {
  Serial.begin(115200);
  delay(500);                  /* USB CDC 枚举等待，避免首行日志丢失 */

  initPMU();                   /* ① 电源：不初始化屏不会亮（最高发坑） */
  Axp2101_SetAldo3(0);         /* ② 确保面板断电：空闲态不上电，首次 display=on 才 lcd_init */

  /* ③ BLE 外设：纯 BLE（不开 WiFi），低占空比广播；设备 MAC 打印在 ble_link_init 内 */
  ble_link_set_state_handler(onScreenState);
  ble_link_set_battery_reader(pmuReadBattery);
  ble_link_init();

  /* 注意：不调用 lcd_init——面板首次点亮推迟到第一次 display=on
   * （冷启动 ~1.1s 已被接受，换取空闲态零面板功耗）；屏幕全程黑/断电，无"等待主控..."画面 */
}

void loop(void) {
  ble_link_tick();             /* 连接期间：60s 电池 notify + link_status Read 值刷新 */

  /* 断连维护画面（边沿检测）：服务保持期断连（screenOn）→ 常亮"系统维护中"黑底白字；
   * 空闲期（screenOn=false）断连 → 保持休眠黑屏，不主动亮屏。
   * 重连后主控推送新 state，onScreenState 正常渲染并清 maintMode。 */
  static bool wasConnected = false;
  bool conn = ble_link_connected();
  if (wasConnected && !conn && screenOn) {
    maintMode = true;
    /* 清绘制守卫（同 WiFi 路径 onServerDown）：否则重连后主控推送同状态同二维码时
     * needRedraw 恒假，维护画面会滞留、永不刷新回正常画面 */
    qrShown = "";
    lastStatusKey = "";
    showMaintenance("系统维护中");
  }
  wasConnected = conn;

  if (maintMode) showMaintenance("系统维护中");  /* 内部 30s 换位守卫：文案/位置未变不重绘 */

  delay(30);                   /* 无轮询、无触摸、无 IDLE_SLEEP 本地兜底（熄屏由主控 display=off 驱动） */
}

#endif // TRANSPORT_BLE
