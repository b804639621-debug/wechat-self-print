/*
 * ============================================================================
 * 本文件原样复制自微雪官方例程仓，未做修改。
 *   仓库：  https://gitee.com/waveshare/ESP32-C6-Touch-AMOLED-2.16
 *   路径：  02_Example/Arduino-v3.3.3/09_LVGL_V9_Test/src/port_bsp/i2c_bsp.h
 *   commit：b77979b8302e1f94b81a7f18696ce60d7afde94a (2026-07-13)
 *   说明：  I2C 总线封装（lcd_touch.h 依赖本文件，故一并复制）。
 * ============================================================================
 */
#ifndef I2C_BSP_H
#define I2C_BSP_H

#include <driver/i2c_master.h>

class I2cMasterBus {
private:
  i2c_master_bus_handle_t user_i2c_handle = NULL;
public:
  I2cMasterBus(int scl_pin, int sda_pin, int i2c_port);
  ~I2cMasterBus();

  int i2c_write_buff(i2c_master_dev_handle_t dev_handle, int reg, uint8_t *buf, uint8_t len);
  int i2c_master_write_read_dev(i2c_master_dev_handle_t dev_handle, uint8_t *writeBuf, uint8_t writeLen, uint8_t *readBuf, uint8_t readLen);
  int i2c_read_buff(i2c_master_dev_handle_t dev_handle, int reg, uint8_t *buf, uint8_t len);
  i2c_master_bus_handle_t Get_I2cBusHandle();
};

#endif