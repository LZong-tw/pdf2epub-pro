"""Procedurally generate a portrait EPUB cover (1600×2400 JPEG).

No AI required — deterministic geometric design with configurable text + color
palette. Two ornament variants ("pillars" hexagonal, "graph" with overlaid
ascending line graph).
"""
import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 2400

# AWS-like default palette; override via CLI.
DEFAULT_PALETTE = {
    "bg":      (35, 47, 62),       # #232F3E navy
    "bg_end":  (15, 22, 32),       # gradient lower stop
    "accent":  (255, 153, 0),      # #FF9900 orange
    "soft":    (47, 64, 84),
    "white":   (245, 247, 250),
    "dim":     (160, 175, 195),
}

SANS_BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
SANS_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def pick_font(candidates, size):
    for f in candidates:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def hex_polygon(cx, cy, r, rot=0):
    return [(cx + r * math.cos(math.radians(60 * i + rot)),
             cy + r * math.sin(math.radians(60 * i + rot))) for i in range(6)]


def gradient_background(img, top, bottom):
    px = img.load()
    for y in range(H):
        t = y / H
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(W):
            px[x, y] = c


def draw_corner_marks(d, color):
    d.line([(80, 80), (200, 80)], fill=color, width=6)
    d.line([(80, 80), (80, 200)], fill=color, width=6)
    d.line([(W - 200, H - 80), (W - 80, H - 80)], fill=color, width=6)
    d.line([(W - 80, H - 200), (W - 80, H - 80)], fill=color, width=6)


def draw_pillars(d, cy, p):
    r = 160
    cx = W // 2
    for i in range(6):
        a = math.radians(60 * i - 30)
        ox = cx + math.cos(a) * r * math.sqrt(3)
        oy = cy + math.sin(a) * r * math.sqrt(3)
        d.polygon(hex_polygon(ox, oy, r * 0.9), outline=p["accent"], width=4)
    d.polygon(hex_polygon(cx, cy, r * 0.95), outline=p["white"], width=5)
    d.polygon(hex_polygon(cx, cy, r * 0.55), fill=p["accent"])


def draw_graph_overlay(d, cy, p):
    draw_pillars(d, cy, p)
    left, right = 200, W - 200
    top, bottom = cy + 350, cy + 700
    for i in range(6):
        y = top + (bottom - top) * i / 5
        d.line([(left, y), (right, y)], fill=p["soft"], width=2)
    points = []
    rng = right - left
    vals = [0.85, 0.7, 0.62, 0.55, 0.5, 0.38, 0.42, 0.3, 0.34, 0.2, 0.1]
    for i, v in enumerate(vals):
        points.append((left + rng * i / (len(vals) - 1),
                       top + (bottom - top) * v))
    d.line(points, fill=p["accent"], width=6)
    for px, py in points:
        d.ellipse([px - 10, py - 10, px + 10, py + 10],
                  fill=p["white"], outline=p["accent"], width=3)


def text_centered(d, text, font, y, color):
    bbox = d.textbbox((0, 0), text, font=font)
    d.text(((W - (bbox[2] - bbox[0])) // 2, y), text, font=font, fill=color)


def make_cover(out_path, *, super_title, main_title, subtitle,
               publisher, variant="pillars", palette=None):
    p = palette or DEFAULT_PALETTE
    img = Image.new("RGB", (W, H), p["bg"])
    gradient_background(img, p["bg"], p["bg_end"])
    d = ImageDraw.Draw(img)
    draw_corner_marks(d, p["accent"])

    f_super = pick_font(SANS_BOLD_CANDIDATES, 80)
    text_centered(d, super_title.upper(), f_super, 240, p["accent"])
    d.line([(W // 2 - 240, 360), (W // 2 + 240, 360)], fill=p["dim"], width=2)

    lines = list(main_title) if isinstance(main_title, (list, tuple)) else [main_title]
    f_main = pick_font(SANS_BOLD_CANDIDATES, 130 if len(lines) <= 2 else 110)
    title_top = 480
    line_h = 150 if len(lines) <= 2 else 130
    for i, line in enumerate(lines):
        text_centered(d, line, f_main, title_top + i * line_h, p["white"])

    cy = title_top + len(lines) * line_h + 360
    (draw_graph_overlay if variant == "graph" else draw_pillars)(d, cy, p)

    f_sub = pick_font(SANS_CANDIDATES, 56)
    text_centered(d, subtitle, f_sub, H - 320, p["dim"])
    f_pub = pick_font(SANS_BOLD_CANDIDATES, 44)
    text_centered(d, publisher.upper(), f_pub, H - 200, p["accent"])

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=92, optimize=True)
    print(f"wrote {out_path} ({Path(out_path).stat().st_size:,} bytes)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="pdf2epub-cover")
    p.add_argument("out_jpg")
    p.add_argument("--super-title", default="")
    p.add_argument("--title", required=True,
                   help="Main title; use '|' to split into multiple lines")
    p.add_argument("--subtitle", default="")
    p.add_argument("--publisher", default="")
    p.add_argument("--variant", default="pillars", choices=["pillars", "graph"])
    args = p.parse_args(argv)

    main_title = args.title.split("|")
    make_cover(args.out_jpg,
               super_title=args.super_title,
               main_title=main_title,
               subtitle=args.subtitle,
               publisher=args.publisher,
               variant=args.variant)


if __name__ == "__main__":
    main()
