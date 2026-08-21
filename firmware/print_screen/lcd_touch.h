/*
 * ============================================================================
 * 本文件原样复制自微雪官方例程仓，未做修改。
 *   仓库：  https://gitee.com/waveshare/ESP32-C6-Touch-AMOLED-2.16
 *   路径：  02_Example/Arduino-v3.3.3/09_LVGL_V9_Test/lcd_touch.h
 *   commit：b77979b8302e1f94b81a7f18696ce60d7afde94a (2026-07-13)
 *   说明：  CST9220 电容触摸驱动封装，轮询读取坐标（本项目不做中断）。
 * ============================================================================
 */
#ifndef LCD_TOUCH_BSP_H
#define LCD_TOUCH_BSP_H

#include "./src/port_bsp/i2c_bsp.h"
#include "driver/gpio.h"
#include "user_config.h"

class LcdTouchPanel {
private:
  I2cMasterBus& i2cbus_;
  i2c_master_dev_handle_t touch_dev_handle = NULL;
  int touch_rst_pin_ = GPIO_NUM_NC;
  int touch_int_pin_ = GPIO_NUM_NC;
public:
  LcdTouchPanel(I2cMasterBus& i2cbus, int dev_addr, int touch_rst_pin = BSP_LCD_TOUCH_RST, int touch_int_pin = GPIO_NUM_NC);
  ~LcdTouchPanel();

  uint8_t GetCoords(uint16_t* x, uint16_t* y);
  void ResetTouch();
};


#endif
