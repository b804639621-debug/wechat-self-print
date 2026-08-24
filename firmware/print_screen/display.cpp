/*
 * display.cpp —— 无 LVGL 的轻量显示驱动（esp_lcd 直绘）。
 * 显示面板 SH8601 的创建/初始化序列与微雪例程 bsp_lvgl_port.cpp 完全一致，
 * 仅剥离了 LVGL 相关的缓冲/刷屏/事件部分，改为提供 fillRect/点阵/文本原语。
 */
#include <Arduino.h>
#include <assert.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <driver/spi_master.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_vendor.h>
#include <esp_heap_caps.h>

#include "display.h"
#include "fonts.h"

static esp_lcd_panel_handle_t  panel = NULL;      /* 面板句柄 */
static esp_lcd_panel_io_handle_t io_handle = NULL; /* 命令/参数通道（背光走这里） */
static uint16_t* rowbuf = NULL;                    /* DMA 行缓冲 */
static SemaphoreHandle_t s_color_done = NULL;      /* 像素 DMA 完成信号 */

/* esp_lcd 的 tx_color 是异步事务：返回≠传输完成。rowbuf 是全行复用的，
 * 不等待就改写 = DMA 与 CPU 数据竞争；队列满时的丢弃/乱序也无法排除
 * （实机症状：1 行 × 几百次密集推送全部丢失或挤成一条细黑条，
 *  而 fill_rect 的 32 行大块推送一直正常）。统一等完成再复用缓冲。 */
static bool IRAM_ATTR on_color_trans_done(esp_lcd_panel_io_handle_t panel_io,
                                          esp_lcd_panel_io_event_data_t* edata, void* user_ctx) {
  BaseType_t hp_task_woken = pdFALSE;
  if (s_color_done) xSemaphoreGiveFromISR(s_color_done, &hp_task_woken);
  return hp_task_woken == pdTRUE;
}

static void lcd_wait_dma(void) {
  if (s_color_done) xSemaphoreTake(s_color_done, pdMS_TO_TICKS(200));  /* 超时兜底防死等 */
}

/* 绘图行缓冲：480 x 32 行，文本/填充/点阵共用 */
#define STRIP_H 32

/* 面板为 RGB565，期望像素按“高字节在前”传输（例程 bsp_lvgl_port.cpp 的
 * lv_draw_sw_rgb565_swap 即为同义交换）。本机小端内存直接写 uint16_t 需手动
 * 交换字节，否则灰色等非对称颜色会偏色（黑/白字节对称，不受影响）。 */
static inline uint16_t lcd_px(uint16_t c) {
  return (uint16_t)((c >> 8) | (c << 8));
}

/* 显示初始化命令表：原样取自微雪 09_LVGL_V9_Test/bsp_lvgl_port.cpp */
static const sh8601_lcd_init_cmd_t lcd_init_cmds[] = {
  { 0x11, (uint8_t[]){ 0x00 }, 0, 600 },
  { 0xFE, (uint8_t[]){ 0x20 }, 1, 0 },
  { 0x19, (uint8_t[]){ 0x10 }, 1, 0 },
  { 0x1C, (uint8_t[]){ 0xA0 }, 1, 0 },
  { 0xFE, (uint8_t[]){ 0x00 }, 1, 0 },
  { 0xC4, (uint8_t[]){ 0x80 }, 1, 0 },
  { 0x3A, (uint8_t[]){ 0x55 }, 1, 0 },
  { 0x35, (uint8_t[]){ 0x00 }, 1, 0 },
  { 0x36, (uint8_t[]){ 0x30 }, 1, 0 },
  { 0x53, (uint8_t[]){ 0x20 }, 1, 0 },
  { 0x51, (uint8_t[]){ 0xFF }, 1, 0 },
  { 0x63, (uint8_t[]){ 0xFF }, 1, 0 },
  { 0x2A, (uint8_t[]){ 0x00, 0x00, 0x01, 0xDF }, 4, 0 },
  { 0x2B, (uint8_t[]){ 0x00, 0x00, 0x01, 0xDF }, 4, 0 },
  { 0x29, (uint8_t[]){ 0x00 }, 0, 100 },
};

void lcd_init(I2cMasterBus& i2cbus) {
  /* ---- SPI（QSPI 四线）总线初始化：引脚见 user_config.h ---- */
  spi_bus_config_t buscfg = {};
  buscfg.sclk_io_num = BSP_LCD_PCLK;
  buscfg.data0_io_num = BSP_LCD_DATA0;
  buscfg.data1_io_num = BSP_LCD_DATA1;
  buscfg.data2_io_num = BSP_LCD_DATA2;
  buscfg.data3_io_num = BSP_LCD_DATA3;
  buscfg.max_transfer_sz = BSP_LCD_H_RES * BSP_LCD_V_RES * BSP_LCD_BITS_PER_PIXEL / 8;
  ESP_ERROR_CHECK(spi_bus_initialize((spi_host_device_t)BSP_LCD_SPI_NUM, &buscfg, SPI_DMA_CH_AUTO));

  /* ---- 面板 IO（QSPI，32bit 命令字，与例程一致） ---- */
  esp_lcd_panel_io_spi_config_t io_config = {};
  io_config.cs_gpio_num = BSP_LCD_CS;
  io_config.dc_gpio_num = -1;
  io_config.spi_mode = 0;
  io_config.pclk_hz = 40 * 1000 * 1000;
  io_config.trans_queue_depth = 2;
  io_config.lcd_cmd_bits = 32;
  io_config.lcd_param_bits = 8;
  io_config.flags.quad_mode = true;
  io_config.on_color_trans_done = on_color_trans_done;   /* DMA 完成回调（lcd_wait_dma 用） */
  ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)BSP_LCD_SPI_NUM, &io_config, &io_handle));

  sh8601_vendor_config_t vendor_config = {};
  vendor_config.init_cmds = lcd_init_cmds;
  vendor_config.init_cmds_size = sizeof(lcd_init_cmds) / sizeof(lcd_init_cmds[0]);
  vendor_config.flags.use_qspi_interface = 1;

  esp_lcd_panel_dev_config_t panel_config = {};
  panel_config.reset_gpio_num = BSP_LCD_RST;      /* NC：用 PMU ALDO3 做屏复位 */
  panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
  panel_config.bits_per_pixel = BSP_LCD_BITS_PER_PIXEL;
  panel_config.vendor_config = &vendor_config;

  ESP_ERROR_CHECK(esp_lcd_new_panel_sh8601(io_handle, &panel_config, &panel));

  /* 屏复位：PMU ALDO3 掉电重上电（必须 PMU 已初始化） */
  Axp2101_SetAldo3(1); vTaskDelay(pdMS_TO_TICKS(100));
  Axp2101_SetAldo3(0); vTaskDelay(pdMS_TO_TICKS(100));
  Axp2101_SetAldo3(1); vTaskDelay(pdMS_TO_TICKS(100));

  ESP_ERROR_CHECK(esp_lcd_panel_init(panel));   /* 下发初始化序列（含 0x11 开眠 + 0x29 开显） */

  rowbuf = (uint16_t*)heap_caps_malloc(BSP_LCD_H_RES * STRIP_H * sizeof(uint16_t), MALLOC_CAP_DMA);
  assert(rowbuf && "rowbuf DMA alloc failed");
  s_color_done = xSemaphoreCreateBinary();
  assert(s_color_done && "dma sem alloc failed");

  /* 烧屏防护#2：亮度上限 80% */
  lcd_set_backlight(LCD_BACKLIGHT_PCT);
}

void lcd_set_backlight(uint8_t pct) {
  if (pct > 100) pct = 100;
  uint8_t bl_val = (uint8_t)((pct * 255) / 100);
  uint32_t cmd = 0x51;                 /* 0x51 亮度命令 */
  cmd &= 0xFF;
  cmd <<= 8;
  cmd |= (uint32_t)0x02 << 24;         /* QSPI 写命令 opcode 0x02 */
  esp_lcd_panel_io_tx_param(io_handle, cmd, &bl_val, 1);
}

void lcd_fill_rect(int x0, int y0, int x1, int y1, uint16_t color) {
  if (x0 < 0) x0 = 0;
  if (x1 > LCD_W - 1) x1 = LCD_W - 1;
  if (y0 < 0) y0 = 0;
  if (y1 > LCD_H - 1) y1 = LCD_H - 1;
  if (x0 > x1 || y0 > y1) return;

  int width = x1 - x0 + 1;
  for (int y = y0; y <= y1; ) {
    int h = STRIP_H;
    if (y + h - 1 > y1) h = y1 - y + 1;
    int n = width * h;
    for (int i = 0; i < n; i++) rowbuf[i] = lcd_px(color);
    esp_lcd_panel_draw_bitmap(panel, x0, y, x1 + 1, y + h, rowbuf);
    lcd_wait_dma();
    y += h;
  }
}

void lcd_clear(uint16_t color) {
  lcd_fill_rect(0, 0, LCD_W - 1, LCD_H - 1, color);
}

void lcd_draw_mono_scaled(int ox, int oy, int w, int h, int scale,
                          const uint8_t* mono, uint16_t fg, uint16_t bg) {
  int dst_w = w * scale;
  if (ox + dst_w > LCD_W) dst_w = LCD_W - ox;
  if (dst_w <= 0 || ox >= LCD_W || ox < 0) return;
  int row_stride = (w + 7) / 8;
  int total = h * scale;
  /* 按 STRIP_H 行条带渲染推送（与 fill_rect 同款的大块 DMA 节奏，
   * 避免 1 行 × 几百次密集小事务把 esp_lcd QSPI 推送打挂） */
  for (int y0 = 0; y0 < total; y0 += STRIP_H) {
    int rows = STRIP_H;
    if (y0 + rows > total) rows = total - y0;
    int yy = oy + y0;
    int skip = 0;
    if (yy < 0) skip = -yy;                    /* 上沿裁剪 */
    if (skip >= rows) continue;
    int out = rows - skip;
    int yscr = yy + skip;
    if (yscr >= LCD_H) break;                  /* 已过屏底 */
    if (yscr + out > LCD_H) out = LCD_H - yscr;
    for (int r = 0; r < out; r++) {
      int mr = (y0 + skip + r) / scale;
      const uint8_t* rowb = mono + (size_t)mr * row_stride;
      for (int c = 0; c < dst_w; c++) {
        int mc = c / scale;
        uint8_t bit = (rowb[mc >> 3] >> (mc & 7)) & 1;
        rowbuf[(size_t)r * dst_w + c] = lcd_px(bit ? fg : bg);
      }
    }
    esp_lcd_panel_draw_bitmap(panel, ox, yscr, ox + dst_w, yscr + out, rowbuf);
    lcd_wait_dma();
  }
}

/* 解码 UTF-8 下一个码点 */
static uint32_t utf8_next(const char*& p) {
  uint8_t c = (uint8_t)*p++;
  if (c < 0x80) return c;
  int extra;
  uint32_t cp;
  if ((c & 0xE0) == 0xC0) { extra = 1; cp = c & 0x1F; }
  else if ((c & 0xF0) == 0xE0) { extra = 2; cp = c & 0x0F; }
  else if ((c & 0xF8) == 0xF0) { extra = 3; cp = c & 0x07; }
  else { return 0xFFFD; }
  for (int i = 0; i < extra; i++) {
    uint8_t d = (uint8_t)*p++;
    if ((d & 0xC0) != 0x80) return 0xFFFD;
    cp = (cp << 6) | (d & 0x3F);
  }
  return cp;
}

int lcd_text_width(const char* s) {
  int n = 0;
  while (*s) { utf8_next(s); n++; }
  return n * 16;
}

void lcd_draw_text(int x, int y, const char* s, uint16_t fg, uint16_t bg) {
  if (y < 0 || y >= LCD_H) return;
  int h = 16;
  if (y + h > LCD_H) h = LCD_H - y;

  /* 先铺底色，再逐字形写入前景像素（rowbuf 行 0..h-1） */
  lcd_fill_rect(0, y, LCD_W - 1, y + h - 1, bg);

  const char* p = s;
  int cx = x;
  while (*p && cx < LCD_W) {
    uint32_t cp = utf8_next(p);
    const glyph_t* g = font_find(cp);
    for (int r = 0; r < h; r++) {
      int bits;
      if (g) bits = ((int)g->bmp[r * 2] << 8) | g->bmp[r * 2 + 1];
      else   bits = 0xFFFF;   /* 未收录字形：全填方块兜底 */
      for (int c = 0; c < 16 && cx + c < LCD_W; c++) {
        if (bits & (0x8000 >> c)) rowbuf[r * LCD_W + cx + c] = lcd_px(fg);
      }
    }
    cx += 16;
  }
  esp_lcd_panel_draw_bitmap(panel, 0, y, LCD_W, y + h, rowbuf);
  lcd_wait_dma();
}

void lcd_draw_text_center(int y, const char* s, uint16_t fg, uint16_t bg) {
  int w = lcd_text_width(s);
  int x = (LCD_W - w) / 2;
  if (x < 0) x = 0;
  lcd_draw_text(x, y, s, fg, bg);
}

void lcd_screen_off(void) {
  lcd_clear(LCD_COLOR_BLACK);                 /* AMOLED 黑像素不发光，物理等效息屏 */
  esp_lcd_panel_disp_on_off(panel, false);    /* 更彻底的 displayOff 命令 */
}

void lcd_screen_on(void) {
  esp_lcd_panel_disp_on_off(panel, true);
}
