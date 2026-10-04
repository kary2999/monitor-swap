#!/usr/bin/env python3
"""Render a dark mobile-friendly hourly intelligence report image."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1540
BG = "#090d12"
CARD = "#111820"
CARD_2 = "#0e151c"
TEXT = "#f3f6f8"
MUTED = "#8f9aa6"
GREEN = "#48d597"
BORDER = "#26313c"
FONT_REG = "/System/Library/Fonts/STHeiti Light.ttc"
FONT_BOLD = "/System/Library/Fonts/STHeiti Medium.ttc"


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def wrap(draw, text, fnt, max_width):
    lines = []
    for paragraph in str(text).split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            test = current + ch
            if draw.textlength(test, font=fnt) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines


def text_block(draw, xy, text, fnt, fill, max_width, line_gap=10):
    x, y = xy
    lines = wrap(draw, text, fnt, max_width)
    bbox = draw.textbbox((0, 0), "国Ag", font=fnt)
    line_h = bbox[3] - bbox[1] + line_gap
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=fill)
        y += line_h
    return y


def main():
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    data = json.loads(src.read_text(encoding="utf-8"))
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Header accent and small signal mark.
    d.rounded_rectangle((58, 58, 1022, 64), radius=3, fill=GREEN)
    d.text((62, 92), data["eyebrow"], font=font(24, True), fill=GREEN)
    y = text_block(d, (58, 142), data["title"], font(58, True), TEXT, 930, 14)
    y += 20
    d.rounded_rectangle((58, y, 1022, y + 142), radius=22, fill=CARD_2, outline=BORDER, width=2)
    text_block(d, (88, y + 28), data["summary"], font(29), "#cbd3da", 875, 12)
    y += 176

    for item in data["items"]:
        card_h = 270
        d.rounded_rectangle((58, y, 1022, y + card_h), radius=24, fill=CARD, outline=BORDER, width=2)
        d.rounded_rectangle((82, y + 28, 142, y + 88), radius=16, fill=GREEN)
        d.text((97, y + 40), item["rank"], font=font(27, True), fill=BG)
        d.text((165, y + 35), item["tag"], font=font(23, True), fill=GREEN)
        title_y = text_block(d, (82, y + 105), item["title"], font(31, True), TEXT, 895, 7)
        body_y = text_block(d, (82, title_y + 10), item["body"], font(24), "#b9c2ca", 900, 8)
        d.text((82, min(y + card_h - 40, body_y + 10)), item["source"], font=font(18), fill=MUTED)
        y += card_h + 22

    d.rounded_rectangle((58, y, 1022, y + 92), radius=20, fill="#10231c", outline="#245a43", width=2)
    text_block(d, (84, y + 26), data["action"], font(25, True), GREEN, 885, 8)
    d.text((58, H - 60), data["footer"], font=font(18), fill="#66727e")

    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, format="PNG", optimize=True)
    print(json.dumps({"output": str(dst), "width": W, "height": H, "bytes": dst.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
