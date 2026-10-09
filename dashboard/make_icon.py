#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 learn-coach 应用图标（纯标准库：像素绘制 + PNG 编码 + ICO 封装）。
运行：python dashboard/make_icon.py  → 输出到 dashboard/assets/
设计：靛蓝圆角底 + 白色翻开的书 + 右上角绿色"过关"圆点。
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "assets"
OUT.mkdir(parents=True, exist_ok=True)

INDIGO = (79, 70, 229, 255)     # #4f46e5
INDIGO_DARK = (67, 56, 202, 255)
WHITE = (255, 255, 255, 255)
GREEN = (34, 197, 94, 255)


def rounded_rect_mask(size, radius):
    """每像素是否在圆角矩形内（覆盖全图）。"""
    m = [[True] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            in_corner = (x < radius and y < radius) or (x >= size - radius and y < radius) or \
                        (x < radius and y >= size - radius) or (x >= size - radius and y >= size - radius)
            if in_corner:
                dx = radius - x if x < radius else x - (size - 1 - radius)
                dy = radius - y if y < radius else y - (size - 1 - radius)
                if dx * dx + dy * dy > radius * radius:
                    m[y][x] = False
    return m


def in_poly(px, py, pts):
    """点是否在多边形内（射线法）。"""
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            xin = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < xin:
                inside = not inside
    return inside


def draw(size):
    """返回 RGBA 像素缓冲 [size][size]。"""
    s = size / 256.0
    mask = rounded_rect_mask(size, int(52 * s))
    # 翻开的书：左右两页，书脊在中间
    left_page = [(30 * s, 78 * s), (122 * s, 60 * s), (122 * s, 186 * s), (30 * s, 204 * s)]
    right_page = [(226 * s, 78 * s), (134 * s, 60 * s), (134 * s, 186 * s), (226 * s, 204 * s)]
    spine_gap = [(122 * s, 60 * s), (134 * s, 60 * s), (134 * s, 186 * s), (122 * s, 186 * s)]
    # 页面上的三条"目录线"
    lines = [(44, 104, 96, 110), (44, 126, 96, 132), (44, 148, 96, 154),
             (160, 104, 212, 110), (160, 126, 212, 132), (160, 148, 212, 154)]
    badge_c, badge_r = (206 * s, 52 * s), 30 * s

    px = []
    for y in range(size):
        row = []
        for x in range(size):
            if not mask[y][x]:
                row.append((0, 0, 0, 0))
                continue
            # 底色：顶部略亮做微渐变
            t = y / size
            c = tuple(int(INDIGO[i] + (INDIGO_DARK[i] - INDIGO[i]) * t) for i in range(3)) + (255,)
            if in_poly(x, y, left_page) or in_poly(x, y, right_page):
                c = WHITE
                # 目录线（靛蓝细条）
                for lx1, ly1, lx2, ly2 in lines:
                    if lx1 * s <= x <= lx2 * s and ly1 * s <= y <= (ly1 + 4.5) * s:
                        c = INDIGO
                        break
            # 书脊阴影缝
            if in_poly(x, y, spine_gap) and 62 * s <= y <= 184 * s:
                c = (199, 210, 254, 255)
            # 右上角绿色徽章（过关）
            if (x - badge_c[0]) ** 2 + (y - badge_c[1]) ** 2 <= badge_r ** 2:
                c = GREEN
            row.append(c)
        px.append(row)
    return px


def encode_png(px, size):
    raw = b"".join(b"\x00" + bytes(v for c in row for v in c) for row in px)
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_ico(png256: bytes):
    """单图像 256px PNG-in-ICO。"""
    ico = struct.pack("<HHH", 0, 1, 1)
    ico += struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png256), 22)
    return ico + png256


if __name__ == "__main__":
    png256 = None
    for size in (256, 48, 32, 16):
        data = encode_png(draw(size), size)
        (OUT / ("app-%d.png" % size)).write_bytes(data)
        if size == 256:
            png256 = data
            (OUT / "app.png").write_bytes(data)
    (OUT / "app.ico").write_bytes(write_ico(png256))
    print("icons written to", OUT)
