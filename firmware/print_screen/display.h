#ifndef DISPLAY_H
#define DISPLAY_H
/*
 * display.h / display.cpp —— 本项目自写：无 LVGL 的轻量显示驱动封装。
 *
 * 显示芯片为 SH8601（微雪例程仓 bsp_lvgl_port.cpp 中 init 命令表为 CO5300/SH8601
 * 共用序列），用 esp_lcd 框架直绘：只提供 fillRect / 单色点阵放大 / 文本 三种原语，
 * 覆盖二维码、状态文字两种 UI 需求即可。
 */
#include <stdint.h>
#include <Arduino.h>
#include "user_config.h"
#include "./src/port_bsp/i2c_bsp.h"
#include "./src/port_bsp/axp2101_bsp.h"
#include "src/externLib/esp_lcd_sh8601.h"

/* ---- 颜色（RGB565） ---- */
#define LCD_COLOR_BLACK   0x0000
#define LCD_COLOR_WHITE   0xFFFF
#define LCD_COLOR_GRAY    0x8410   /* 50% 灰（二维码“变暗”用） */
#define LCD_COLOR_RED     0xF800

/* 烧屏防护#2：亮度上限 80%（初始化时设置一次） */
#define LCD_BACKLIGHT_PCT 80

#define LCD_W 480
#define LCD_H 480

/* 必须在 PMU（AXP2101）初始化之后调用：屏供电走 PMU，顺序不可颠倒 */
void lcd_init(I2cMasterBus& i2cbus);

/* 背光 0..100（SH8601 0x51 命令，无独立背光 GPIO） */
void lcd_set_backlight(uint8_t pct);

/* 全屏填充 */
void lcd_clear(uint16_t color);

/* 填充矩形，坐标为含端点（x0..x1, y0..y1），自动裁剪到屏内 */
void lcd_fill_rect(int x0, int y0, int x1, int y1, uint16_t color);

/* 单色点阵放大绘制：
 *   mono 为 w*h 位图，行优先、每行 (w+7)/8 字节、位序 LSB=最左像素、1=前景色；
 *   以 scale 倍放大，置于 (ox, oy)。用于二维码绘制（白底黑码，非反色）。 */
void lcd_draw_mono_scaled(int ox, int oy, int w, int h, int scale,
                          const uint8_t* mono, uint16_t fg, uint16_t bg);

/* UTF-8 文本宽度（每字形 16px，全角） */
int lcd_text_width(const char* s);

/* 在 (x, y) 绘制一行文本（16px 高，白/黑任意底色） */
void lcd_draw_text(int x, int y, const char* s, uint16_t fg, uint16_t bg);

/* 水平居中绘制一行文本 */
void lcd_draw_text_center(int y, const char* s, uint16_t fg, uint16_t bg);

/* 烧屏防护#3：全黑填充 + DISPOFF（AMOLED 黑像素不发光，物理等效息屏） */
void lcd_screen_off(void);

/* 恢复显示（DISPOFF 之前写入的内容会直接显示） */
void lcd_screen_on(void);

#endif // DISPLAY_H
