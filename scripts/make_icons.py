#!/usr/bin/env python3
"""Draw cADRe's icons (stdlib only) into cadre/static/:

  icon.svg, icon-192.png, icon-512.png   browser tab and installed-app icons
  tray.ico          Windows tray: watching (blue ring)
  tray-waiting.ico  an agent is waiting for you (amber ring)
  tray-off.ico      dashboard down or hook events paused (grey ring)
  menubar.png          macOS menu bar: the mark alone, black, used as a template image
  menubar-waiting.png  an agent is waiting for you (amber, not a template)
  menubar-off.png      dashboard down or paused: the ring and dots without the sensor
  indicator-{24,48}.png, indicator-waiting-*.png, indicator-off-*.png
                       Linux status icon (StatusNotifierItem): the tray's three states as PNGs

The mark is "Formation": the c of cADRe drawn as a scope ring, a teal sensor at its centre and
three agents in formation at its open edge, on a dark rounded tile. Small sizes (tray) drop
the three agents and thicken the ring so it still reads at 16 px. Everything is rasterised
from the same geometry with supersampling. Re-run after changing the design:
    python3 scripts/make_icons.py
"""
import math
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cadre", "static")
INK = (0x0E, 0x14, 0x24)                    # tile
BLUE, AMBER, GREY = (0x5B, 0x7C, 0xFF), (0xF5, 0x9E, 0x0B), (0x8A, 0x94, 0xA6)
TEAL, MUTED, LIGHT = (0x2D, 0xD4, 0xBF), (0x6B, 0x72, 0x80), (0xE6, 0xEB, 0xF5)
RX = 22                                     # corner radius of the 100x100 tile
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64)   # tray: 16 px at 100 % display scaling, 24 at 150 %, 32 at 200 %
GAP = math.radians(40)                      # the ring is open from -40 to +40 degrees (the "c")


def mark(size):
    """Geometry in tile units (0..100), from the 64-unit logo scaled by 1.25 and centred.
    Returns ring radius, half stroke, sensor radius and the formation dots."""
    if size <= 18:
        return 27.5, 7.5, 11.25, []
    if size <= 26:
        return 27.5, 5.6, 10.0, [(80.0, 50.0, 5.6)]
    return 27.5, 4.375, 8.125, [(75.0, 41.25, 4.125), (81.875, 50.0, 4.125), (75.0, 58.75, 4.125)]


def in_tile(x, y):
    if not (0 <= x < 100 and 0 <= y < 100):
        return False
    cx = min(max(x, RX), 100 - RX)
    cy = min(max(y, RX), 100 - RX)
    return (x - cx) ** 2 + (y - cy) ** 2 <= RX * RX


def in_ring(x, y, r, half):
    dx, dy = x - 50, y - 50
    d = math.hypot(dx, dy)
    if abs(d - r) <= half and abs(math.atan2(-dy, dx)) >= GAP:
        return True
    for s in (1, -1):                       # round caps at both ends of the arc
        ex, ey = 50 + r * math.cos(GAP), 50 - s * r * math.sin(GAP)
        if (x - ex) ** 2 + (y - ey) ** 2 <= half * half:
            return True
    return False


MENUBAR_VIEW = (15.0, 14.0, 72.0)          # x0, y0, width of the tile area the menu bar icon shows


def pixels(size, ring=BLUE, sensor=TEAL, dot=LIGHT, tile=True, view=(0.0, 0.0, 100.0), geom=None):
    """Rows of straight (not premultiplied) RGBA bytes, top row first. tile=False draws the mark
    alone on transparency; view crops the 100x100 tile; geom picks the geometry by another size;
    sensor=None leaves the centre out."""
    r, half, sr, dots = mark(geom or size)
    sub = 4 if size <= 64 else 2
    x0, y0, w = view
    s = w / size
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for j in range(sub):
                y = y0 + (py + (j + 0.5) / sub) * s
                for i in range(sub):
                    x = x0 + (px + (i + 0.5) / sub) * s
                    if tile and not in_tile(x, y):
                        continue
                    c = INK if tile else None
                    if sensor and (x - 50) ** 2 + (y - 50) ** 2 <= sr * sr:
                        c = sensor
                    elif in_ring(x, y, r, half):
                        c = ring
                    else:
                        for dx, dy, dr in dots:
                            if (x - dx) ** 2 + (y - dy) ** 2 <= dr * dr:
                                c = dot
                                break
                    if c is None:
                        continue
                    acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 1
            n = sub * sub
            a = acc[3] / n
            if a <= 0:
                row += bytes(4)
            else:
                row += bytes(round(v / acc[3]) for v in acc[:3]) + bytes([round(255 * a)])
        rows.append(bytes(row))
    return rows


def png(size, **kw):
    rows = [b"\x00" + r for r in pixels(size, **kw)]           # filter byte "none" per row

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def ico(ring, sensor, sizes=ICO_SIZES):
    """A .ico of 32-bit BGRA bitmaps (no PNG entries, so every LoadImage version reads it)."""
    images = []
    for n in sizes:
        rows = pixels(n, ring, sensor)[::-1]                   # bitmaps are stored bottom-up
        xor = b"".join(bytes(c for i in range(0, len(r), 4) for c in (r[i + 2], r[i + 1], r[i], r[i + 3]))
                       for r in rows)                          # RGBA -> BGRA
        stride = (n + 31) // 32 * 4                            # 1-bit AND mask, rows padded to 32 bits
        mask = bytearray()
        for r in rows:
            line = bytearray(stride)
            for x in range(n):
                if r[x * 4 + 3] == 0:                          # fully transparent
                    line[x // 8] |= 0x80 >> (x % 8)
            mask += line
        header = struct.pack("<IiiHHIIiiII", 40, n, 2 * n, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
        images.append((n, header + xor + bytes(mask)))
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for n, data in images:
        out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(data for _, data in images)


def hexc(c):
    return "#%02x%02x%02x" % c


def svg():
    r, half, sr, dots = mark(64)
    ex, ey1, ey2 = 50 + r * math.cos(GAP), 50 - r * math.sin(GAP), 50 + r * math.sin(GAP)
    circles = "".join('<circle cx="%g" cy="%g" r="%g" fill="%s"/>' % (x, y, d, hexc(LIGHT)) for x, y, d in dots)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n'
            '<rect width="100" height="100" rx="%d" fill="%s"/>\n'
            '<path d="M%.2f %.2f A%g %g 0 1 0 %.2f %.2f" fill="none" stroke="%s" stroke-width="%g" '
            'stroke-linecap="round"/>\n'
            '<circle cx="50" cy="50" r="%g" fill="%s"/>\n%s\n</svg>\n'
            % (RX, hexc(INK), ex, ey1, r, r, ex, ey2, hexc(BLUE), 2 * half, sr, hexc(TEAL), circles))


if __name__ == "__main__":
    with open(os.path.join(OUT, "icon.svg"), "w", encoding="utf-8", newline="\n") as f:
        f.write(svg())
    for n in (192, 512):
        with open(os.path.join(OUT, "icon-%d.png" % n), "wb") as f:
            f.write(png(n))
    for name, ring, sensor in (("tray", BLUE, TEAL), ("tray-waiting", AMBER, TEAL), ("tray-off", GREY, MUTED)):
        with open(os.path.join(OUT, name + ".ico"), "wb") as f:
            f.write(ico(ring, sensor))
    # 36 px = 18 pt on a Retina menu bar (menubar.js sets the point size); full geometry with the dots
    black = (0, 0, 0)
    for name, colour, sensor in (("menubar", black, black), ("menubar-waiting", AMBER, AMBER),
                                 ("menubar-off", black, None)):
        with open(os.path.join(OUT, name + ".png"), "wb") as f:
            f.write(png(36, ring=colour, sensor=sensor, dot=colour, tile=False, view=MENUBAR_VIEW, geom=64))
    # Linux top bars are 16-24 px tall; 48 is the same small-size geometry at twice the pixels
    for name, ring, sensor in (("indicator", BLUE, TEAL), ("indicator-waiting", AMBER, TEAL),
                               ("indicator-off", GREY, MUTED)):
        for n in (24, 48):
            with open(os.path.join(OUT, "%s-%d.png" % (name, n)), "wb") as f:
                f.write(png(n, ring=ring, sensor=sensor, geom=24))
    print("wrote icon.svg, icon-192.png, icon-512.png, tray*.ico, menubar*.png, indicator*.png to", OUT)
