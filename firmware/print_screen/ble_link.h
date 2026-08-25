#ifndef BLE_LINK_H
#define BLE_LINK_H
/*
 * ble_link.h / ble_link.cpp —— BLE 外设层（Task 10B 传输通道 TRANSPORT_BLE）。
 *
 * 职责：BLE 广播/GATT 收发 + screen_state JSON 解析，把状态变化转交 .ino 注册的
 * state handler；不直接调用 display/lcd 函数（亮/熄屏与渲染决策在 .ino 的 handler 里做）。
 *
 * GATT 契约（与 S0 spike 完全一致，勿改 UUID）：
 *   设备名 PRINT-SCREEN；Service 6e410000-3f2b-4f5c-9a01-9d6a3b21c001
 *   screen_state 6e410001-...（Write）   ：{state, display, qr_content, file_count, message}
 *   battery_report 6e410002-...（Notify）：{"pct":N,"mv":N,"chg":0/1,"vbus":0/1}
 *   link_status 6e410003-...（Read+Notify）：Read 值 {"fw":..,"uptime_s":N,"mtu":N,"adv_ms":100}；
 *     Notify 用作绘制完成 ack {"drawn":1,"ms":N}
 *
 * WiFi 变体编译时本文件为空（TRANSPORT_WIFI 下 #if 不命中）。
 */
#include "transport_config.h"

#if defined(TRANSPORT_BLE)

#include <Arduino.h>

/* 状态写入回调：BLE 收到 screen_state 写请求并解析成功后调用（同步、回调栈内）：
 *   state / qr 仅在回调返回前有效（指向解析文档的存储）；
 *   display=on/off 的渲染决策由 .ino 的 handler 负责。
 * message 字段契约保留但固件暂不使用，解析时容忍缺失。 */
typedef void (*ble_state_handler_t)(const char* state, bool displayOn, const String& qr, int fileCount);

/* 电池读取回调：由 .ino 注册 Axp2101_ReadBattery 包装（ble_link 不碰 I2C） */
typedef bool (*ble_battery_reader_t)(int* pct, int* mv, bool* chg, bool* vbus);

/* 初始化 BLE（设备名/MTU/服务/广播）；setup 期串口打印设备名与 MAC，供主控绑定缓存 */
void ble_link_init(void);

bool ble_link_connected(void);

void ble_link_set_state_handler(ble_state_handler_t fn);
void ble_link_set_battery_reader(ble_battery_reader_t fn);

/* 电池上报（battery_report notify） */
void ble_link_notify_battery(int pct, int mv, bool chg, bool vbus);

/* 绘制完成 ack（link_status notify {"drawn":1,"ms":N}），供主控测全链路延迟 */
void ble_link_notify_drawn(unsigned long ms);

/* loop 里调用：连接期间每 60s 自动上报电池 + 刷新 link_status 的 Read 值 */
void ble_link_tick(void);

#endif // TRANSPORT_BLE
#endif // BLE_LINK_H
