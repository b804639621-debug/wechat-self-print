/*
 * ble_link.cpp —— BLE 外设层实现（Task 10B 传输通道 TRANSPORT_BLE）。
 * 从 S0 spike（firmware/ble_spike/ble_spike.ino）移植并产品化：
 *   - 广播间隔 100ms（spike 实测结论），GATT 契约/UUID 与 spike 完全一致
 *   - 电池上报节奏 5s → 60s（生产），断开重连的维护画面决策在 .ino，本层只管收发
 * 接口见 ble_link.h；WiFi 变体（TRANSPORT_WIFI）编译时本文件为空。
 */
#include "ble_link.h"

#if defined(TRANSPORT_BLE)

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <ArduinoJson.h>
#include <string.h>

/* ============================ 可配置参数 ============================ */
#define DEVICE_NAME     "PRINT-SCREEN"
/* 广播间隔 100ms（0.625ms 单位 → 160）：S0 实测 500ms 档 Windows 建连等广播 4~9s 不可用，
 * 100ms 档建连 ~2s 饱和，50ms 无进一步改善、徒增功耗——生产取 100ms（计划原定 500ms 已下修） */
#define ADV_INTERVAL_MS 100
#define BATT_NOTIFY_MS  (60UL * 1000)  /* 连接期间电池上报间隔（电量变化慢，与 WiFi 通道 60s 缓存同节奏） */
#define FW_VERSION      "print_screen-ble-1.0"

/* GATT UUID（自定义 PRINTSHOP-SCREEN 服务，契约与 S0 spike 一致） */
#define SVC_UUID  "6e410000-3f2b-4f5c-9a01-9d6a3b21c001"
#define CHR_STATE "6e410001-3f2b-4f5c-9a01-9d6a3b21c001"  /* screen_state   Write         */
#define CHR_BATT  "6e410002-3f2b-4f5c-9a01-9d6a3b21c001"  /* battery_report Notify        */
#define CHR_LINK  "6e410003-3f2b-4f5c-9a01-9d6a3b21c001"  /* link_status    Read + Notify */

/* ============================ 全局状态 ============================ */
static BLEServer*          s_server = NULL;
static BLECharacteristic*  s_chrBatt = NULL;
static BLECharacteristic*  s_chrLink = NULL;
static bool                s_connected = false;
static uint16_t            s_mtu = 23;                 /* 默认 ATT MTU，协商成功后更新 */
static unsigned long       s_lastBattNotify = 0;       /* 0=连接建立后第一次 tick 立即上报 */
static ble_state_handler_t s_stateHandler = NULL;
static ble_battery_reader_t s_battReader = NULL;

/* ============================ BLE 回调 ============================ */

/* esp32c6 @ core 3.3.3 内置 BLE 栈为 NimBLE（sdkconfig: CONFIG_BT_NIMBLE_ENABLED=y，
 * Bluedroid 未启用），回调走 BLEServer.h 的 NimBLE 分支签名（照 spike 写法）。 */
class ServerCb : public BLEServerCallbacks {
  void onConnect(BLEServer* pServer, ble_gap_conn_desc* desc) override {
    s_connected = true;
    s_lastBattNotify = 0;   /* 连接建立后第一次 tick 立即上报一次电池 */
    Serial.println("[ble] connected");
  }
  void onDisconnect(BLEServer* pServer, ble_gap_conn_desc* desc) override {
    s_connected = false;
    Serial.println("[ble] disconnected, re-advertising");
    delay(100);             /* 断连事件后稍等再重开广播（栈内处理时序，照 spike） */
    pServer->getAdvertising()->start();
  }
  void onMtuChanged(BLEServer* pServer, ble_gap_conn_desc* desc, uint16_t mtu) override {
    s_mtu = mtu;
    Serial.printf("[ble] MTU negotiated: %d\n", s_mtu);
  }
};

/* screen_state 写入：JSON 解析失败仅串口日志、不动作（不通知上层） */
class StateCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* pChar) override {
    String v = pChar->getValue();
    Serial.printf("[ble] screen_state write %dB: %s\n", v.length(), v.c_str());
    JsonDocument doc;
    if (deserializeJson(doc, v) != DeserializationError::Ok) {
      Serial.println("[ble] json parse failed, ignored");
      return;
    }
    if (!s_stateHandler) return;
    const char* state    = doc["state"] | "IDLE";
    bool        displayOn = (strcmp(doc["display"] | "on", "on") == 0);
    String      qr = doc["qr_content"] | "";
    int         fc  = doc["file_count"] | 0;
    /* message 字段契约保留但固件暂不使用：解析时容忍缺失 */
    s_stateHandler(state, displayOn, qr, fc);
  }
};

/* battery_report 订阅回调（T3 修复首包竞态）：
 * 竞态来源：onConnect 后第一个 ble_link_tick（~30ms）即上报电池，但客户端此时尚未
 * 完成 CCCD 订阅（服务发现+订阅约需 1~3s），NimBLE 对未订阅连接的 notify 直接丢弃，
 * 客户端首包要等 60s（S2 主控心跳要求订阅后数秒内拿到电量）。
 * 修法：订阅生效（subValue 非 0 = notify 打开）时把 s_lastBattNotify 置 0，
 * 下一个 ble_link_tick（30ms 节奏）立即补发一次；onConnect 置 0 保留（无订阅时
 * NimBLE 丢弃，无害；订阅后仍由本回调兜底补发）。 */
class BattCb : public BLECharacteristicCallbacks {
  void onSubscribe(BLECharacteristic* pCharacteristic, ble_gap_conn_desc* desc, uint16_t subValue) override {
    if (subValue > 0) {
      s_lastBattNotify = 0;
      Serial.println("[ble] battery subscribed, will re-notify on next tick");
    }
  }
};

/* ============================ 内部工具 ============================ */

/* 刷新 link_status 的 Read 值（uptime/mtu/广播间隔） */
static void refresh_link_status(unsigned long uptime_s) {
  char lk[80];
  snprintf(lk, sizeof(lk), "{\"fw\":\"%s\",\"uptime_s\":%lu,\"mtu\":%d,\"adv_ms\":%d}",
           FW_VERSION, uptime_s, s_mtu, ADV_INTERVAL_MS);
  s_chrLink->setValue((uint8_t*)lk, strlen(lk));
}

/* ============================ 对外接口 ============================ */

void ble_link_init(void) {
  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(517);               /* 本地 MTU 拉满，协商结果看 onMtuChanged（S0 恒 517） */
  s_server = BLEDevice::createServer();
  s_server->setCallbacks(new ServerCb());

  BLEService* svc = s_server->createService(SVC_UUID);

  BLECharacteristic* chrState = svc->createCharacteristic(CHR_STATE, BLECharacteristic::PROPERTY_WRITE);
  chrState->setCallbacks(new StateCb());

  s_chrBatt = svc->createCharacteristic(CHR_BATT, BLECharacteristic::PROPERTY_NOTIFY);
  s_chrBatt->addDescriptor(new BLE2902());
  s_chrBatt->setCallbacks(new BattCb());   /* 订阅生效即补发电池（修复首包竞态） */

  s_chrLink = svc->createCharacteristic(CHR_LINK,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  s_chrLink->addDescriptor(new BLE2902());

  svc->start();

  /* link_status 初始 Read 值：连接前被读到也有内容（uptime_s=0） */
  refresh_link_status(0);

  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->setScanResponse(true);           /* scan response 携带设备名 */
  uint16_t itv = ADV_INTERVAL_MS * 8 / 5;  /* 0.625ms 单位：100ms → 160 */
  adv->setMinInterval(itv);
  adv->setMaxInterval(itv);
  adv->start();

  Serial.printf("[ble] advertising as %s, interval=%dms\n", DEVICE_NAME, ADV_INTERVAL_MS);
  /* 设备 MAC 供主控绑定缓存（主控直连缓存地址、不做配对，信任模型与现状一致） */
  Serial.printf("[ble] MAC=%s\n", BLEDevice::getAddress().toString().c_str());
}

bool ble_link_connected(void) {
  return s_connected;
}

void ble_link_set_state_handler(ble_state_handler_t fn) {
  s_stateHandler = fn;
}

void ble_link_set_battery_reader(ble_battery_reader_t fn) {
  s_battReader = fn;
}

void ble_link_notify_battery(int pct, int mv, bool chg, bool vbus) {
  char b[64];
  snprintf(b, sizeof(b), "{\"pct\":%d,\"mv\":%d,\"chg\":%d,\"vbus\":%d}",
           pct, mv, chg ? 1 : 0, vbus ? 1 : 0);
  s_chrBatt->setValue((uint8_t*)b, strlen(b));
  s_chrBatt->notify();
}

void ble_link_notify_drawn(unsigned long ms) {
  char ack[48];
  snprintf(ack, sizeof(ack), "{\"drawn\":1,\"ms\":%lu}", ms);
  s_chrLink->setValue((uint8_t*)ack, strlen(ack));
  s_chrLink->notify();
}

void ble_link_tick(void) {
  unsigned long now = millis();
  if (!s_connected) return;             /* 空闲广播态不做事（低占空比省电） */
  /* s_lastBattNotify==0 表示连接刚建立（onConnect 置 0）：立即上报一次——
   * 主控周期心跳连接期望尽快拿到电量；之后按 60s 节奏（首次设置后非 0 即走间隔判断） */
  if (s_lastBattNotify == 0 || now - s_lastBattNotify >= BATT_NOTIFY_MS) {
    s_lastBattNotify = now;
    if (s_battReader) {
      int pct, mv; bool chg, vbus;
      if (s_battReader(&pct, &mv, &chg, &vbus)) {
        ble_link_notify_battery(pct, mv, chg, vbus);
      }
    }
    /* link_status 的 Read 值按同一节奏刷新（uptime/mtu 变化） */
    refresh_link_status(now / 1000);
  }
}

#endif // TRANSPORT_BLE
