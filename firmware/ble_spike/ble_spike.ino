/*
 * ============================================================================
 * ble_spike.ino —— Task 10B Step S0：BLE 可行性 spike 固件（一次性测量工具）
 * 板子：微雪 ESP32-C6-Touch-AMOLED-2.16
 *
 * S0 验证项（对应计划文档 Task 10B Step S0 checklist）：
 *   ① 关 WiFi 纯 BLE：低占空比广播 + GATT 服务 PRINTSHOP-SCREEN
 *        screen_state  (Write)        —— {state, display, qr_content, file_count, message}
 *        battery_report(Notify)       —— 连接期间每 5s 上报 {pct,mv,chg,vbus}（spike 加速，生产 60s）
 *        link_status   (Read + Notify)—— 固件版本/uptime/协商 MTU；兼作"绘制完成 ack"通道
 *   ② MTU 协商 ≥185（本地 BLEDevice::setMTU(517)，onMtuChanged 串口留证 + link_status 可查）
 *   ③ 空闲 ALDO3 面板断电 → 写 display=on → 冷启动面板画二维码 → link_status notify
 *        {"drawn":1,"ms":N}（N=固件侧从上电到画完耗时）→ PC 端测全链路延迟
 *   ④ 广播间隔两档编译宏（500/1000ms），供延迟/电流权衡测量
 *
 * 本 sketch 为 S0 一次性工具：display.cpp/fonts.h/user_config.h/src/* 均复制自
 * print_screen（display.cpp 已按反复上下电需要拆分 lcd_init），S1 正式改造时
 * 以 TRANSPORT_BLE 编译开关整合回 print_screen/，本目录保留作测量基线。
 * ============================================================================
 */

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <ArduinoJson.h>
#include "./src/ricmoo_qrcode/qrcode.h"
#include "user_config.h"
#include "display.h"
#include "./src/port_bsp/i2c_bsp.h"
#include "./src/port_bsp/axp2101_bsp.h"

/* ============================ 可配置参数 ============================ */
#define DEVICE_NAME        "PRINT-SCREEN"
#define ADV_INTERVAL_MS    100          /* S0 实测三档结论：500ms 档 Windows 建连等广播 4~9s（不可用）；
                                         * 100ms 档建连 ~2s 饱和；50ms 档无进一步改善、徒增功耗。
                                         * 生产建议 100ms（计划原定 500ms 需下修，见 S0 报告）。 */
#define BATT_NOTIFY_MS     (5UL * 1000) /* 连接期间电池上报间隔（spike 加速版） */
#define FW_VERSION         "ble_spike-0.1"

/* GATT UUID（自定义 PRINTSHOP-SCREEN 服务，契约草案见计划文档 Task 10B） */
#define SVC_UUID   "6e410000-3f2b-4f5c-9a01-9d6a3b21c001"
#define CHR_STATE  "6e410001-3f2b-4f5c-9a01-9d6a3b21c001"  /* screen_state   Write         */
#define CHR_BATT   "6e410002-3f2b-4f5c-9a01-9d6a3b21c001"  /* battery_report Notify        */
#define CHR_LINK   "6e410003-3f2b-4f5c-9a01-9d6a3b21c001"  /* link_status    Read + Notify */

/* 二维码绘制参数（与 print_screen.ino 一致） */
#define QR_SCALE         9
#define QR_MAX_VERSION   7
#define QR_MAX_MODULES   (QR_MAX_VERSION * 4 + 17)
#define QR_MAX_BOTTOM    456
#define FOOTER_Y         460

/* ============================ 全局状态 ============================ */
I2cMasterBus I2cMasterBus_(GPIO_NUM_7, GPIO_NUM_8, I2C_NUM_0);

static BLEServer*         s_server = NULL;
static BLECharacteristic* s_chrBatt = NULL;
static BLECharacteristic* s_chrLink = NULL;
static bool     s_connected = false;
static uint16_t s_mtu = 23;            /* 默认 ATT MTU，协商成功后更新 */
static bool     s_panelReady = false;  /* lcd_init 是否已执行（SPI/panel 仅一次） */
static unsigned long s_lastBattNotify = 0;

/* ============================ BLE 回调 ============================ */

/* esp32c6 @ core 3.3.3 内置 BLE 栈为 NimBLE（sdkconfig: CONFIG_BT_NIMBLE_ENABLED=y，
 * Bluedroid 未启用），回调走 BLEServer.h 的 NimBLE 分支签名。 */
class ServerCb : public BLEServerCallbacks {
  void onConnect(BLEServer* pServer, ble_gap_conn_desc* desc) override {
    s_connected = true;
    Serial.println("[ble] connected");
  }
  void onDisconnect(BLEServer* pServer, ble_gap_conn_desc* desc) override {
    s_connected = false;
    Serial.println("[ble] disconnected, re-advertising");
    delay(100);                        /* 断连事件后稍等再重开广播（栈内处理时序） */
    pServer->getAdvertising()->start();
  }
  void onMtuChanged(BLEServer* pServer, ble_gap_conn_desc* desc, uint16_t mtu) override {
    s_mtu = mtu;
    Serial.printf("[ble] MTU negotiated: %d\n", s_mtu);
  }
};

/* 绘制二维码（白底黑码，逻辑同 print_screen.ino drawQR 的非 dim 路径） */
static void drawQR(const String& content) {
  uint8_t qrcodeData[qrcode_getBufferSize(QR_MAX_VERSION)];
  QRCode qrc;
  if (qrcode_initText(&qrc, qrcodeData, QR_MAX_VERSION, ECC_MEDIUM, content.c_str()) != 0) {
    lcd_clear(LCD_COLOR_WHITE);
    lcd_draw_text_center(232, "QR ERR", LCD_COLOR_BLACK, LCD_COLOR_WHITE);
    return;
  }
  int size = qrc.size * QR_SCALE;
  int ox = ((LCD_W - size) / 2) & ~1;    /* 偶数对齐（奇数 x_start 触发 SH8601 行错位花屏） */
  int oy = (LCD_H - size) / 2 - 6;
  if (oy + size > QR_MAX_BOTTOM) oy = QR_MAX_BOTTOM - size;

  lcd_clear(LCD_COLOR_WHITE);
  int bw = (qrc.size + 7) / 8;
  uint8_t mono[(QR_MAX_MODULES + 7) / 8 * QR_MAX_MODULES];
  memset(mono, 0, sizeof(mono));
  for (int y = 0; y < qrc.size; y++) {
    for (int x = 0; x < qrc.size; x++) {
      if (qrcode_getModule(&qrc, x, y)) mono[(size_t)y * bw + (x >> 3)] |= (uint8_t)(1 << (x & 7));
    }
  }
  lcd_draw_mono_scaled(ox, oy, qrc.size, qrc.size, QR_SCALE, mono, LCD_COLOR_BLACK, LCD_COLOR_WHITE);
  lcd_draw_text_center(FOOTER_Y, "微信扫码·传文件打印", LCD_COLOR_BLACK, LCD_COLOR_WHITE);
}

/* display=on：冷启动面板（ALDO3 上电 + 初始化序列）→ 画二维码 → notify drawn ack */
static void handleDisplayOn(const String& qr) {
  unsigned long t0 = millis();
  if (!s_panelReady) {
    lcd_init(I2cMasterBus_);             /* 首次：SPI/panel 创建 + 上电 + 初始化序列 */
    s_panelReady = true;
  } else {
    lcd_panel_power_on();                /* 后续：仅 ALDO3 上电 + 重发初始化序列 */
  }
  drawQR(qr);
  unsigned long ms = millis() - t0;
  Serial.printf("[scr] display=on drawn, fw_ms=%lu\n", ms);

  char ack[48];
  snprintf(ack, sizeof(ack), "{\"drawn\":1,\"ms\":%lu}", ms);
  s_chrLink->setValue((uint8_t*)ack, strlen(ack));
  s_chrLink->notify();
}

static void handleDisplayOff(void) {
  if (!s_panelReady) return;
  lcd_panel_power_off();                 /* DISPOFF + ALDO3 断电 */
  Serial.println("[scr] display=off, panel powered down");
}

class StateCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* pChar) override {
    String v = pChar->getValue();
    Serial.printf("[ble] screen_state write %dB: %s\n", v.length(), v.c_str());
    JsonDocument doc;
    if (deserializeJson(doc, v) != DeserializationError::Ok) {
      Serial.println("[ble] json parse failed");
      return;
    }
    const char* disp = doc["display"] | "";
    if (strcmp(disp, "on") == 0)  handleDisplayOn(doc["qr_content"] | "");
    if (strcmp(disp, "off") == 0) handleDisplayOff();
  }
};

/* ============================ 初始化 ============================ */

void setup(void) {
  Serial.begin(115200);
  delay(500);                            /* USB CDC 枚举等待 */

  /* ① PMU：电池监控 + 确保 ALDO3 断电（空闲态面板不上电，BLE spike 的省电前提） */
  Custom_PmicPortInit(&I2cMasterBus_, 0x34);
  Axp2101_EnableBatteryMonitor();
  Axp2101_SetAldo3(0);
  Serial.println("[hw] AXP2101 PMU init done, ALDO3 off (idle)");

  /* ② BLE：关 WiFi（默认不开），纯 BLE 外设 */
  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(517);                /* 本地 MTU 拉满，协商结果看 onMtuChanged */
  s_server = BLEDevice::createServer();
  s_server->setCallbacks(new ServerCb());

  BLEService* svc = s_server->createService(SVC_UUID);

  BLECharacteristic* chrState = svc->createCharacteristic(CHR_STATE, BLECharacteristic::PROPERTY_WRITE);
  chrState->setCallbacks(new StateCb());

  s_chrBatt = svc->createCharacteristic(CHR_BATT, BLECharacteristic::PROPERTY_NOTIFY);
  s_chrBatt->addDescriptor(new BLE2902());

  s_chrLink = svc->createCharacteristic(CHR_LINK,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  s_chrLink->addDescriptor(new BLE2902());

  svc->start();

  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->setScanResponse(true);            /* scan response 携带设备名 */
  uint16_t itv = ADV_INTERVAL_MS * 8 / 5;  /* 单位 0.625ms：500ms→800 / 1000ms→1600 */
  adv->setMinInterval(itv);
  adv->setMaxInterval(itv);
  adv->start();
  Serial.printf("[ble] advertising as %s, interval=%dms\n", DEVICE_NAME, ADV_INTERVAL_MS);
}

/* ============================ 主循环 ============================ */

void loop(void) {
  unsigned long now = millis();

  /* 连接期间每 5s 上报电池；link_status 的 read 值顺带刷新（uptime/mtu） */
  if (s_connected && now - s_lastBattNotify >= BATT_NOTIFY_MS) {
    s_lastBattNotify = now;
    int pct, mv; bool chg, vbus;
    if (Axp2101_ReadBattery(&pct, &mv, &chg, &vbus)) {
      char b[64];
      snprintf(b, sizeof(b), "{\"pct\":%d,\"mv\":%d,\"chg\":%d,\"vbus\":%d}",
               pct, mv, chg ? 1 : 0, vbus ? 1 : 0);
      s_chrBatt->setValue((uint8_t*)b, strlen(b));
      s_chrBatt->notify();
    }
    char lk[80];
    snprintf(lk, sizeof(lk), "{\"fw\":\"%s\",\"uptime_s\":%lu,\"mtu\":%d,\"adv_ms\":%d}",
             FW_VERSION, now / 1000, s_mtu, ADV_INTERVAL_MS);
    s_chrLink->setValue((uint8_t*)lk, strlen(lk));
  }

  delay(50);
}
