/*
 * ============================================================================
 * 本文件复制自微雪官方例程仓，仅做一处最小编译修复（见文末注释）。
 *   仓库：  https://gitee.com/waveshare/ESP32-C6-Touch-AMOLED-2.16
 *   路径：  02_Example/Arduino-v3.3.3/09_LVGL_V9_Test/src/port_bsp/axp2101_bsp.cpp
 *   commit：b77979b8302e1f94b81a7f18696ce60d7afde94a (2026-07-13)
 *   说明：  AXP2101 PMU 驱动，基于 XPowersLib（需另行安装到 Arduino 库目录）。
 * ============================================================================
 */
#include <stdio.h>
#include <freertos/FreeRTOS.h>
#include "axp2101_bsp.h"
#define XPOWERS_CHIP_AXP2101
#include "XPowersLib.h"

/* 最小编译修复：例程仓中本文件使用 TAG 但未定义（ESP_LOGI/ESP_LOGE 需要），
 * 此处补上，与其它例程文件的写法一致。 */
static const char *TAG = "axp2101_bsp";


static XPowersPMU axp2101;
static I2cMasterBus *i2cbus_ = NULL;
static i2c_master_dev_handle_t i2cPMICdev = NULL;
static uint8_t i2cPMICAddress;


static int AXP2101_SLAVE_Read(uint8_t devAddr, uint8_t regAddr, uint8_t *data, uint8_t len) {
  int ret;
  uint8_t count = 3;
  do {
    ret = (i2cbus_->i2c_read_buff(i2cPMICdev, regAddr, data, len) == ESP_OK) ? 0 : -1;
    if (ret == 0)
      break;
    vTaskDelay(pdMS_TO_TICKS(100));
    count--;
  } while (count);
  return ret;
}

static int AXP2101_SLAVE_Write(uint8_t devAddr, uint8_t regAddr, uint8_t *data, uint8_t len) {
  int ret;
  uint8_t count = 3;
  do {
    ret = (i2cbus_->i2c_write_buff(i2cPMICdev, regAddr, data, len) == ESP_OK) ? 0 : -1;
    if (ret == 0)
      break;
    vTaskDelay(pdMS_TO_TICKS(100));
    count--;
  } while (count);
  return ret;
}

void Custom_PmicPortInit(I2cMasterBus *i2cbus, uint8_t dev_addr) {
  if (i2cbus_ == NULL) {
    i2cbus_ = i2cbus;
  }
  if (i2cPMICdev == NULL) {
    i2c_master_bus_handle_t BusHandle = i2cbus_->Get_I2cBusHandle();
    i2c_device_config_t dev_cfg = {};
    dev_cfg.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    dev_cfg.scl_speed_hz = 100000;
    dev_cfg.device_address = dev_addr;
    ESP_ERROR_CHECK(i2c_master_bus_add_device(BusHandle, &dev_cfg, &i2cPMICdev));
    i2cPMICAddress = dev_addr;
  }
  if (axp2101.begin(i2cPMICAddress, AXP2101_SLAVE_Read, AXP2101_SLAVE_Write)) {
    ESP_LOGI(TAG, "Init PMU SUCCESS!");
  } else {
    ESP_LOGE(TAG, "Init PMU FAILED!");
  }
  Custom_PmicRegisterInit();
}

void Custom_PmicRegisterInit(void) {
  axp2101.setVbusCurrentLimit(XPOWERS_AXP2101_VBUS_CUR_LIM_2000MA);

  if (axp2101.getDC1Voltage() != 3300) {
    axp2101.setDC1Voltage(3300);
    ESP_LOGW("axp2101_init_log", "Set DCDC1 to output 3V3");
  }
  if (axp2101.getALDO1Voltage() != 3300) {
    axp2101.setALDO1Voltage(3300);
    ESP_LOGW("axp2101_init_log", "Set ALDO1 to output 3V3");
  }
  if (axp2101.getALDO2Voltage() != 3300) {
    axp2101.setALDO2Voltage(3300);
    ESP_LOGW("axp2101_init_log", "Set ALDO2 to output 3V3");
  }
  if (axp2101.getALDO3Voltage() != 3300) {
    axp2101.setALDO3Voltage(3300);
    ESP_LOGW("axp2101_init_log", "Set ALDO3 to output 3V3");
  }
  if (axp2101.getALDO4Voltage() != 3300) {
    axp2101.setALDO4Voltage(3300);
    ESP_LOGW("axp2101_init_log", "Set ALDO4 to output 3V3");
  }

  axp2101.setPrechargeCurr(XPOWERS_AXP2101_PRECHARGE_50MA);
  axp2101.setChargerConstantCurr(XPOWERS_AXP2101_CHG_CUR_500MA);
  axp2101.setChargerTerminationCurr(XPOWERS_AXP2101_CHG_ITERM_50MA);
}

void Axp2101_SetAldo3(uint8_t vol) {
  if(vol) {
    axp2101.enableALDO3();
  } else {
    axp2101.disableALDO3();
  }
}

void Axp2101_SetAldo2(uint8_t vol) {
  if(vol) {
    axp2101.enableALDO2();
  } else {
    axp2101.disableALDO2();
  }
}

/* ==================== 以下为项目自有扩展（非例程原样复制） ====================
 * 电池电量读取：AXP2101 自带电量计寄存器（getBatteryPercent 直读 0~100）
 * 与电池电压 ADC（getBattVoltage，需先 EnableBatteryMonitor 开通道）。
 * 2026-08 装机电池后新增，供主控管理页显示剩余电量。 */
void Axp2101_EnableBatteryMonitor(void) {
  axp2101.enableBattVoltageMeasure();
}

bool Axp2101_ReadBattery(int *pct, int *mv, bool *charging, bool *vbus) {
  if (!axp2101.isBatteryConnect()) {
    return false;
  }
  *pct = axp2101.getBatteryPercent();
  *mv = (int)axp2101.getBattVoltage();
  *charging = axp2101.isCharging();
  *vbus = axp2101.isVbusIn();
  return true;
}