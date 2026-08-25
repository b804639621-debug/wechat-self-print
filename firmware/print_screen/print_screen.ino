/*
 * ============================================================================
 * print_screen.ino —— 微信扫码自助打印 · 小屏端固件（ESP32-C6 / 480x480 AMOLED）
 * 板子：微雪 ESP32-C6-Touch-AMOLED-2.16
 *   - AXP2101 PMU（XPowersLib）  ：屏供电，必须先初始化，否则屏不亮
 *   - SH8601 显示（esp_lcd）     ：显示驱动在 display.cpp，无 LVGL 直绘
 *   - CST9220 触摸（lcd_touch）  ：I2C 轮询，用于“触摸唤醒”
 * 功能：
 *   - WiFiManager 首次配网（AP 热点名 PRINT-SETUP）
 *   - 每 1.5s 轮询主控 GET /api/screen/state，ArduinoJson 7 解析
 *   - 状态机：IDLE 显示二维码 / SCANNED 二维码变暗 / 服务中状态画面 / 打印中
 *   - 烧屏防护：亮度 60%、无操作/完成服务熄屏（亮屏仅在服务期间）
 *   - 触摸轮询唤醒 → POST /api/screen/wake
 *   - 电池监控：60s 读一次 AXP2101 电量计缓存，随轮询捎带 batt_* 参数上报主控
 *     （管理页显示剩余电量；无电池不上报，小屏 UI 不显示）
 * 依赖库（Arduino 库管理器）：WiFiManager、ArduinoJson(v7)、
 *   XPowersLib（从微雪例程仓 01_Arduino_Libraries 拷入）。
 *   ricmoo/QRCode 已 vendored 到 src/ricmoo_qrcode/（esp32 core 3.3.x 内置同名头文件会抢占）。
 * ============================================================================
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiManager.h>
#include <ArduinoJson.h>
#include "./src/ricmoo_qrcode/qrcode.h"     /* ricmoo/QRCode（vendored 副本：esp32 core 3.3.x 内置同名 qrcode.h 会抢占） */
#include "user_config.h"
#include "lcd_touch.h"
#include "display.h"
#include "./src/port_bsp/i2c_bsp.h"
#include "./src/port_bsp/axp2101_bsp.h"

/* ============================ 可配置参数 ============================ */
#define SERVER_IP        "192.168.31.171"    /* 主控固定 IP（配网时按店内实际改） */
#define SERVER_PORT      8000
#define POLL_INTERVAL_MS 1500                /* 正常轮询间隔 */
#define POLL_BACKOFF_MS  5000                /* 主控不可达退避重试间隔 */
#define HTTP_TIMEOUT_MS  2500                /* HTTP 连接/读取超时 */
#define QR_SCALE         9                   /* 二维码放大倍数（480/29≈16，留边距取 9） */
#define QR_MAX_VERSION   7                   /* 固定 v7：45 模块*9=405px 可留页脚；v7-ECC_MEDIUM 容量 122B 足够 */
#define QR_MAX_MODULES   (QR_MAX_VERSION * 4 + 17)
#define IDLE_SLEEP_MS    (360UL * 1000)      /* 烧屏防护 本地兜底：6 分钟（主控 3 分钟先熄） */
#define QR_MAX_BOTTOM    456                 /* 二维码下沿上限（页脚在 460） */
#define FOOTER_Y         460                 /* 页脚“微信扫码·传文件打印” */
#define TEXT_Y           232                 /* 单行状态文字垂直居中位置 */
#define MAINT_SHIFT_MS   (30UL * 1000)       /* 维护态白字防烧屏：每 30s 换一次绘制位置 */
#define BATT_READ_INTERVAL_MS (60UL * 1000)  /* 电池缓存读取间隔（电量变化慢，不挤占 I2C 显示/触摸） */

/* ============================ 全局状态 ============================ */
I2cMasterBus I2cMasterBus_(GPIO_NUM_7, GPIO_NUM_8, I2C_NUM_0); /* 引脚见 user_config.h */
LcdTouchPanel* touch_dev = NULL;

String curState = "";          /* 主控下发的状态（IDLE/SCANNED/...） */
String qrShown = "";           /* 最近一次绘制的二维码内容 */
String maintLastText = "";     /* 维护画面守卫：上次绘制的文案（pollScreen 成功路径复位） */
int8_t maintLastJitter = 6;    /* 维护画面守卫：上次实际绘制的偏移 */
String lastStatusKey = "";     /* 最近一次绘制的服务状态画面（状态+文件数） */
bool screenOn = false;         /* 屏当前是否点亮 */
bool everConnected = false;    /* 是否曾成功拉到主控状态 */
int  failCount = 0;            /* 连续失败次数（驱动退避） */
unsigned long lastPoll = 0;
unsigned long lastStateChange = 0;  /* 状态/亮屏/触摸唤醒时间（本地熄屏兜底计时） */

/* 电池缓存（AXP2101 电量计直读，随轮询上报主控） */
int  battPct = -1;                  /* 电量百分比（-1=未读到） */
int  battMv = 0;                    /* 电池电压 mV */
bool battChg = false;               /* 充电中 */
bool battVbus = false;              /* USB 供电接入 */
bool battValid = false;             /* 是否检测到电池（无可上报数据=false） */
unsigned long lastBattRead = 0;     /* 上次读 PMU 时间（0=未读，下次即读） */

/* ============================ 初始化 ============================ */

void initPMU(void) {
  /* AXP2101 PMU：必须先于显示（屏供电）。地址 0x34，与例程一致 */
  Custom_PmicPortInit(&I2cMasterBus_, 0x34);
  Axp2101_EnableBatteryMonitor();   /* 开电池电压 ADC（读电量前置，2026-08 装机电池） */
  Serial.println("[hw] AXP2101 PMU init done");
}

void initDisplay(void) {
  lcd_init(I2cMasterBus_);     /* SH8601 显示 + 背光 60% */
  Serial.println("[hw] SH8601 display init done");
}

void initTouch(void) {
  touch_dev = new LcdTouchPanel(I2cMasterBus_, 0x5A);  /* CST9220 @ 0x5A */
  Serial.println("[hw] CST9220 touch init done");
}

void setup(void) {
  Serial.begin(115200);
  delay(500);                  /* USB CDC 枚举等待，避免首行日志丢失 */

  initPMU();                   /* ① 电源：不初始化屏不会亮（最高发坑） */
  initDisplay();               /* ② 显示 */
  initTouch();                 /* ③ 触摸 */

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

/* ============================ 主循环 ============================ */

void loop(void) {
  handleTouch();               /* 触摸轮询唤醒（每次循环） */
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

/* ==================== 电池监控 ==================== */

/* 60s 读一次 AXP2101 缓存到全局（电量变化慢；I2C 与显示/触摸共总线，不宜高频）。
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

/* ============================ 轮询与状态机 ============================ */

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

/* ============================ 画面绘制 ============================ */

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

/* 服务中状态画面（状态文案见需求 spec §6.1 表） */
void showStatus(const char* state, int fileCount, const String& qr) {
  if (strcmp(state, "SCANNED") == 0) {
    drawQR(qr, true);   /* 二维码变暗 */
    /* 确认引导文案渲染在页脚位置（覆盖二维码页脚，覆盖区同为 16px 高） */
    lcd_draw_text_center(FOOTER_Y, "请在手机上确认登录", LCD_COLOR_BLACK, LCD_COLOR_WHITE);
  } else if (strcmp(state, "CONNECTED") == 0) {
    /* 收完一批文件后主控回到 CONNECTED 等更多文件/店员操作（spec §8 的 READY 并入
     * CONNECTED 实现）；此时顾客更关心"文件已收到"，fileCount>0 与 RECEIVING 同文案 */
    if (fileCount > 0) {
      char buf[40];
      snprintf(buf, sizeof(buf), "已收到 %d 个文件，请稍候", fileCount);
      showStatusText(buf);
    } else {
      showStatusText("已连接，请发送文件");
    }
  } else if (strcmp(state, "RECEIVING") == 0) {
    char buf[40];
    snprintf(buf, sizeof(buf), "已收到 %d 个文件，请稍候", fileCount);
    showStatusText(buf);
  } else if (strcmp(state, "PRINTING") == 0) {
    showStatusText("打印中，请稍候");
  } else {
    showStatusText("服务中，请稍候");
  }
}

/* 白底黑字单行状态文字 */
void showStatusText(const char* s) {
  lcd_clear(LCD_COLOR_WHITE);
  lcd_draw_text_center(TEXT_Y, s, LCD_COLOR_BLACK, LCD_COLOR_WHITE);
}

/* 黑底白字提示（连接/维护，AMOLED 黑像素不发光，兼防烧屏） */
void showTextOnBlack(const char* s) {
  showMaintenance(s);
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

/* ============================ 亮/熄屏 ============================ */

void doScreenOff(void) {
  if (!screenOn) return;
  screenOn = false;
  lcd_screen_off();
  Serial.println("[lcd] screen off");
}

void doScreenOn(void) {
  if (screenOn) return;
  screenOn = true;
  lcd_screen_on();
  Serial.println("[lcd] screen on");
}

/* ============================ 触摸唤醒 ============================ */

void handleTouch(void) {
  static uint8_t prevPressed = 0;
  uint16_t x = 0, y = 0;
  uint8_t pressed = (touch_dev && touch_dev->GetCoords(&x, &y)) ? 1 : 0;
  if (pressed && !prevPressed) {            /* 边沿触发，避免重复上报 */
    Serial.println("[touch] 唤醒");
    httpPostWake();                         /* 上报主控刷新二维码并点亮 */
    doScreenOn();
    lastStateChange = millis();
    qrShown = "";                           /* 强制下次轮询重绘 */
  }
  prevPressed = pressed;
}

void httpPostWake(void) {
  String url = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) + "/api/screen/wake";
  HTTPClient http;
  if (!http.begin(url.c_str())) return;
  http.setConnectTimeout(1000);
  http.setTimeout(1000);
  http.POST("");                            /* 非阻塞式：忽略结果 */
  http.end();
}
