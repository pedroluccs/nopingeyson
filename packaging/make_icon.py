"""Gera packaging/nopingeyson.ico (logo próprio: quadrado com gradiente e um "N").

Feito em Python puro (zlib + struct), sem depender do Pillow.
"""

import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
C1 = (124, 92, 255)   # roxo
C2 = (34, 211, 238)   # ciano
BG = (11, 13, 20)


def inside_round_rect(x, y, s, r):
    cx = min(max(x, r), s - 1 - r)
    cy = min(max(y, r), s - 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_n(x, y, s):
    """Letra N: duas hastes e uma diagonal."""
    m, w = s * 0.27, s * 0.13
    top, bot = s * 0.25, s * 0.75
    if not (top <= y <= bot):
        return False
    if m <= x <= m + w or s - m - w <= x <= s - m:
        return True
    t = (y - top) / (bot - top)
    cx = m + w / 2 + t * (s - 2 * m - w)
    return abs(x - cx) <= w * 0.62


def png(size: int) -> bytes:
    rows = []
    r = size * 0.22
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            if not inside_round_rect(x, y, size, r):
                row += bytes((0, 0, 0, 0))
                continue
            t = (x + y) / (2 * size)
            col = tuple(int(C1[i] + (C2[i] - C1[i]) * t) for i in range(3))
            if in_n(x + 0.5, y + 0.5, size):
                col = BG
            row += bytes((*col, 255))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data))
    ihdr = struct.pack("!IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


SOURCE = os.path.join(HERE, "icon_source.png")
# recorte quadrado (esquerda, topo, lado) do rosto na imagem de origem
CROP = (72, 0, 220)
UI_LOGO = os.path.join(HERE, "..", "nopingeyson", "ui", "logo.png")


def from_source():
    """Ícone a partir de packaging/icon_source.png (precisa do Pillow)."""
    from PIL import Image, ImageDraw
    img = Image.open(SOURCE).convert("RGBA")
    x, y, side = CROP
    side = min(side, img.width - x, img.height - y)
    img = img.crop((x, y, x + side, y + side)).resize((256, 256), Image.LANCZOS)
    mask = Image.new("L", (256, 256), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 255, 255), radius=56, fill=255)
    img.putalpha(mask)
    img.save(os.path.join(HERE, "nopingeyson.ico"),
             sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    img.save(os.path.join(HERE, "nopingeyson.png"))
    img.resize((96, 96), Image.LANCZOS).save(UI_LOGO)
    print("ícone gerado a partir de", SOURCE)


def main():
    if os.path.exists(SOURCE):
        return from_source()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path = os.path.join(HERE, "nopingeyson.ico")
    with open(path, "wb") as f:
        f.write(header + entries + b"".join(images))
    with open(os.path.join(HERE, "nopingeyson.png"), "wb") as f:
        f.write(images[-1])
    print("ícone gerado:", path)


if __name__ == "__main__":
    main()
