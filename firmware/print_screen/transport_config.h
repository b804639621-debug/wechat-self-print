#ifndef TRANSPORT_CONFIG_H
#define TRANSPORT_CONFIG_H
/* 传输通道二选一（Task 10B）：
 *   TRANSPORT_WIFI—— 现役：WiFiManager 配网 + HTTP 轮询主控（USB 常供电，不省电）
 *   TRANSPORT_BLE —— 可回切变体：BLE 唤醒+数据通道，空闲面板断电、WiFi 关闭（低功耗）
 * 也可用 arduino-cli --build-property 注入 -DTRANSPORT_BLE 覆盖默认值。 */
#if !defined(TRANSPORT_BLE) && !defined(TRANSPORT_WIFI)
#define TRANSPORT_WIFI
#endif
#if defined(TRANSPORT_BLE) && defined(TRANSPORT_WIFI)
#error "TRANSPORT_BLE 与 TRANSPORT_WIFI 只能二选一"
#endif
#endif
