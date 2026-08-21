#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 fonts.h：16x16 单色点阵字库（UTF-8 码点子集）。

用法：
    python gen_font.py [输出头文件]      # 默认输出到本文件同目录 fonts.h

字库内容覆盖 print_screen.ino 中全部界面文案所用到字符 + ASCII 可打印字符。
字形用系统 CJK 字体（黑体/微软雅黑）渲染，每字形 32 字节（16 行 x 2 字节，行内 MSB 在前）。
生成物直接提交，运行本脚本无需安装任何库以外的依赖（需 Pillow）。
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

SIZE = 16            # 字形尺寸（像素）
GLYPH_BYTES = 32     # SIZE * SIZE / 8
FALLBACK_CP = 0      # 未收录字符的占位（C 端渲染为全填方块）

CHARS = set()


def add(text):
    for ch in text:
        CHARS.add(ord(ch))


# ---- 界面文案（务必与 print_screen.ino 中的字符串保持一致） ----
add("微信扫码·传文件打印")            # 二维码页脚（页脚）
add("等待主控...")                    # 启动后等待主控
add("连接中...")                      # WiFi 已连、主控未连通
add("系统维护中")                     # 主控不可达（曾连通后断连）
add("请在手机上确认登录")             # SCANNED
add("已连接，请发送文件")             # CONNECTED
add("已接收 N 个文件，请稍后")        # RECEIVING（N 为数字）
add("打印中，请稍后")                 # PRINTING
add("服务中，请稍后")                 # 未知状态兜底
add("二维码生成失败")                 # 二维码生成异常兜底
# ---- ASCII 可打印字符全量（0x20 ~ 0x7E） ----
for cp in range(0x20, 0x7F):
    CHARS.add(cp)


def load_font():
    candidates = [
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, SIZE)
    raise RuntimeError("找不到可用的 CJK 字体，请在本机安装黑体/微软雅黑后重试")


def render_glyph(cp):
    # 先在大画布上渲染，再按字形包围盒居中放入 16x16，保证 ASCII/标点水平居中
    canvas = 32
    img = Image.new("1", (canvas, canvas), 1)  # 1 = 白（背景）
    d = ImageDraw.Draw(img)
    d.text((8, 6), chr(cp), font=load_font(), fill=0)
    bbox = img.getbbox()  # (left, upper, right, lower)
    if bbox is None:
        return Image.new("1", (SIZE, SIZE), 1)
    left, upper, right, lower = bbox
    w = right - left
    h = lower - upper
    out = Image.new("1", (SIZE, SIZE), 1)
    ox = (SIZE - w) // 2
    oy = (SIZE - h) // 2
    out.paste(img.crop(bbox), (ox, oy))
    return out


def glyph_to_bytes(img):
    out = bytearray()
    for y in range(SIZE):
        row = 0
        for x in range(SIZE):
            row = (row << 1) | (0 if img.getpixel((x, y)) else 1)
        out.append((row >> 8) & 0xFF)
        out.append(row & 0xFF)
    return bytes(out)


def main():
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts.h")
    if len(sys.argv) > 1:
        out_path = sys.argv[1]

    font = load_font()
    entries = []
    for cp in sorted(CHARS):
        img = render_glyph(cp)
        bmp = glyph_to_bytes(img)
        entries.append((cp, bmp))

    lines = []
    lines.append("/* ============================================================================")
    lines.append(" * fonts.h —— 16x16 单色点阵字库（UTF-8 码点子集）")
    lines.append(" * 由 gen_font.py 自动生成，请勿手改；界面文案变更后重新运行：")
    lines.append(" *   python gen_font.py")
    lines.append(" * 每字形 32 字节：16 行，每行 2 字节，行内 MSB 为最左像素，1=前景色。")
    lines.append(" * ============================================================================ */")
    lines.append("#ifndef FONTS_H")
    lines.append("#define FONTS_H")
    lines.append("")
    lines.append("#include <stdint.h>")
    lines.append("")
    lines.append("typedef struct {")
    lines.append("    uint32_t cp;      /* Unicode 码点 */")
    lines.append("    uint8_t  bmp[32]; /* 16x16 位图 */")
    lines.append("} glyph_t;")
    lines.append("")
    lines.append(f"#define FONT_GLYPH_COUNT ({len(entries)}u)")
    lines.append("")
    lines.append("static const glyph_t FONT_GLYPHS[] = {")
    for cp, bmp in entries:
        cols = []
        for i in range(0, len(bmp), 4):
            cols.append("0x%02X,0x%02X,0x%02X,0x%02X" % tuple(bmp[i:i + 4]))
        joined = ",".join(cols)
        lines.append("    { 0x%04X, { %s } }," % (cp, joined))
    lines.append("};")
    lines.append("")
    lines.append("/* 查表：返回码点对应字形，未收录返回 NULL（调用方画全填方块兜底） */")
    lines.append("static inline const glyph_t* font_find(uint32_t cp) {")
    lines.append("    for (uint32_t i = 0; i < FONT_GLYPH_COUNT; i++) {")
    lines.append("        if (FONT_GLYPHS[i].cp == cp) return &FONT_GLYPHS[i];")
    lines.append("    }")
    lines.append("    return 0;")
    lines.append("}")
    lines.append("")
    lines.append("#endif // FONTS_H")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("ok: %s  (%d glyphs)" % (out_path, len(entries)))


if __name__ == "__main__":
    main()
