#!/usr/bin/env python3
"""Render a tall Chinese intel card: up to N items, auto-height, no overlap.

Key design: measurement and drawing share ONE wrap() implementation and one
line-height rule. Card height is derived from the same wrap result used to
draw, so text can never overflow or stack.
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W = 1080
MARGIN = 52
PAD = 26
GAP_TITLE = 8
GAP_BODY = 9
GAP_META = 14
GAP_CARD = 16

PAGE_BG = "#0d1117"
CARD_A = "#161b22"
CARD_B = "#1b222b"
BORDER = "#2a333d"
TEXT = "#eef2f6"
BODY = "#b6c0c9"
MUTED = "#78838f"
GREEN = "#3fb950"
BLUE = "#58a6ff"

FONT_PATH = "/System/Library/Fonts/Supplemental/Songti.ttc"
FALLBACK = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def F(size, bold=False):
    for p in (FONT_PATH, FALLBACK):
        try:
            return ImageFont.truetype(p, size, index=1 if bold else 0)
        except Exception:
            continue
    return ImageFont.load_default()


_probe_draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))


def wrap(text, fnt, max_w):
    """CJK-safe wrap. Returns list of lines."""
    out = []
    for para in str(text).split("\n"):
        cur = ""
        for ch in para:
            if _probe_draw.textlength(cur + ch, font=fnt) <= max_w or not cur:
                cur += ch
            else:
                out.append(cur)
                cur = ch
        out.append(cur)
    return out


def lh(fnt, gap):
    b = fnt.getbbox("国Ag")
    return (b[3] - b[1]) + gap


def draw_lines(d, x, y, text, fnt, fill, max_w, gap):
    for line in wrap(text, fnt, max_w):
        d.text((x, y), line, font=fnt, fill=fill)
        y += lh(fnt, gap)
    return y


def text_h(text, fnt, max_w, gap):
    return len(wrap(text, fnt, max_w)) * lh(fnt, gap)


SIZE = dict(eyebrow=20, title=40, summary=22, rank=19, tag=17,
            it_title=24, body=20, meta=15, action=20, footer=15)


def build(data, max_items=10):
    items = data.get("items", [])[:max_items]
    inner = W - 2 * MARGIN - 2 * PAD

    # ---------- measure ----------
    y = 70
    y += text_h(data.get("title", ""), F(SIZE["title"], True), W - 2 * MARGIN, 12) + 20
    y += text_h(data.get("summary", ""), F(SIZE["summary"]), W - 2 * MARGIN, 10) + 26

    card_h = []
    for it in items:
        h = PAD
        h += lh(F(SIZE["it_title"], True), GAP_TITLE)          # rank row
        h += text_h(it.get("title", ""), F(SIZE["it_title"], True), inner, GAP_TITLE) + GAP_BODY
        h += text_h(it.get("body", ""), F(SIZE["body"]), inner, GAP_BODY) + GAP_META
        h += lh(F(SIZE["meta"]), 0)                            # source
        h += PAD
        card_h.append(h)
        y += h + GAP_CARD

    y += 8
    act_h = 24 + text_h(data.get("action", ""), F(SIZE["action"], True), W - 2 * MARGIN - 40, 9)
    y += act_h + 34
    H = max(int(y + 56), 900)

    # ---------- draw ----------
    img = Image.new("RGB", (W, H), PAGE_BG)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 62], fill="#11161c")
    d.text((MARGIN, 19), data.get("eyebrow", ""), font=F(SIZE["eyebrow"], True), fill=GREEN)
    stamp = str(data.get("generated_at", ""))[:16]
    d.text((W - MARGIN - 150, 21), stamp, font=F(SIZE["eyebrow"] - 4), fill=MUTED)

    y = 70
    y = draw_lines(d, MARGIN, y, data.get("title", ""), F(SIZE["title"], True), TEXT, W - 2 * MARGIN, 12) + 20
    y = draw_lines(d, MARGIN, y, data.get("summary", ""), F(SIZE["summary"]), "#c9d2da", W - 2 * MARGIN, 10) + 26

    for i, it in enumerate(items):
        h = card_h[i]
        d.rounded_rectangle([MARGIN, y, W - MARGIN, y + h], radius=14,
                            fill=CARD_A if i % 2 == 0 else CARD_B, outline=BORDER, width=1)
        ix, cy = MARGIN + PAD, y + PAD

        d.rounded_rectangle([ix, cy + 1, ix + 32, cy + 28], radius=8, fill=GREEN)
        d.text((ix + 9, cy + 4), str(it.get("rank", i + 1)), font=F(SIZE["rank"], True), fill="#0d1117")
        d.text((ix + 44, cy + 5), it.get("tag", ""), font=F(SIZE["tag"], True), fill=BLUE)
        cy += lh(F(SIZE["it_title"], True), GAP_TITLE)

        cy = draw_lines(d, ix, cy, it.get("title", ""), F(SIZE["it_title"], True), TEXT, inner, GAP_TITLE)
        cy += GAP_BODY
        cy = draw_lines(d, ix, cy, it.get("body", ""), F(SIZE["body"]), BODY, inner, GAP_BODY)
        cy += GAP_META
        d.text((ix, cy), it.get("source", ""), font=F(SIZE["meta"]), fill=MUTED)

        y += h + GAP_CARD

    y += 8
    d.rounded_rectangle([MARGIN, y, W - MARGIN, y + act_h], radius=12,
                        fill="#12261a", outline="#1f4d2b", width=1)
    draw_lines(d, MARGIN + 20, y + 13, data.get("action", ""), F(SIZE["action"], True), GREEN,
               W - 2 * MARGIN - 40, 9)

    d.text((MARGIN, H - 40), data.get("footer", ""), font=F(SIZE["footer"]), fill="#66727e")
    return img


def main():
    if len(sys.argv) < 3:
        print("usage: render_intel_long.py <json> <png> [--max-items N]", file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    n = 10
    if "--max-items" in sys.argv:
        n = int(sys.argv[sys.argv.index("--max-items") + 1])
    data = json.loads(src.read_text())
    img = build(data, n)
    img.save(dst)
    print(json.dumps({"output": str(dst), "width": img.width, "height": img.height,
                      "items": len(data.get("items", [])[:n]), "bytes": dst.stat().st_size},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
