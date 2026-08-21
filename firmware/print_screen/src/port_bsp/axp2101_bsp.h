/*
 * ============================================================================
 * 本文件原样复制自微雪官方例程仓，未做修改。
 *   仓库：  https://gitee.com/waveshare/ESP32-C6-Touch-AMOLED-2.16
 *   路径：  02_Example/Arduino-v3.3.3/09_LVGL_V9_Test/src/port_bsp/axp2101_bsp.h
 *   commit：b77979b8302e1f94b81a7f18696ce60d7afde94a (2026-07-13)
 *   说明：  AXP2101 PMU 初始化与电源开关（屏供电走 PMU，必须先初始化）。
 * ============================================================================
 */
#pragma once


#include "i2c_bsp.h"

void Custom_PmicRegisterInit(void);
void Custom_PmicPortInit(I2cMasterBus *i2cbus, uint8_t dev_addr);

void Axp2101_SetAldo2(uint8_t vol);
void Axp2101_SetAldo3(uint8_t vol);