"""Generate assets/icon.icns for the macOS app bundle.

Renders the same mark as assets/icon.svg (One Dark wireless-display logo)
onto Apple's macOS icon grid: a 1024x1024 canvas with an 824x824 rounded
square centered on it and transparent margins. Feeding PyInstaller a
full-bleed image (previously the 2400x800 banner PNG) makes the Dock icon
look oversized and misshapen next to native apps — see GitHub issue #2.

Run from the repo root:  python scripts/generate_macos_icon.py
Requires Pillow (already a project dependency).
"""
import math
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ICNS = os.path.join(ROOT, 'assets', 'icon.icns')

# Apple icon grid: artwork body is 824/1024 of the canvas, corner radius ~185.
CANVAS = 1024
BODY = 824
INSET = (CANVAS - BODY) / 2
CORNER_RADIUS = 185.4
SS = 4  # supersampling factor for clean anti-aliased edges

# One Dark palette (matches assets/icon.svg)
BG = (40, 44, 52, 255)       # #282C34
BLUE = (97, 175, 239, 255)   # #61AFEF
GREEN = (152, 195, 121, 255) # #98C379

# The SVG artwork is authored in 256-space; map it into the 824px body.
SCALE = BODY / 256
STROKE = 11  # SVG stroke width


def blend(fg, alpha):
    """Pre-blend a stroke opacity against the solid body background."""
    return tuple(round(b + alpha * (f - b)) for f, b in zip(fg[:3], BG[:3])) + (255,)


def pt(x, y):
    """256-space -> supersampled canvas coordinates."""
    return ((INSET + x * SCALE) * SS, (INSET + y * SCALE) * SS)


def px(v):
    """256-space length -> supersampled canvas length."""
    return v * SCALE * SS


def dot(draw, center, diameter, color):
    x, y = center
    r = diameter / 2
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def stroke_arc(draw, cx, cy, r, a0, a1, width, color):
    """Stroke a circular arc with round caps by stamping dots along it."""
    arc_len = abs(a1 - a0) * px(r)
    steps = max(2, int(arc_len / (px(width) / 8)))
    for i in range(steps + 1):
        a = a0 + (a1 - a0) * i / steps
        x = cx + r * math.cos(a)
        y = cy + r * math.sin(a)
        dot(draw, pt(x, y), px(width), color)


def stroke_line(draw, p0, p1, width, color):
    """Stroke a line segment with round caps."""
    draw.line([pt(*p0), pt(*p1)], fill=color, width=round(px(width)))
    dot(draw, pt(*p0), px(width), color)
    dot(draw, pt(*p1), px(width), color)


def render_master():
    size = CANVAS * SS
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Icon body: centered rounded square, transparent margins around it
    draw.rounded_rectangle(
        [INSET * SS, INSET * SS, (INSET + BODY) * SS, (INSET + BODY) * SS],
        radius=CORNER_RADIUS * SS,
        fill=BG,
    )

    # Signal arcs — SVG paths "M x0 y0 A r r 0 0 1 x1 y0" concentric on
    # (128, ~122). Opacities pre-blended against the body background.
    for x0, y0, r, alpha in ((80, 74, 68, 0.35), (95.5, 89.5, 46, 0.65), (111, 105, 24, 1.0)):
        half_chord = 128 - x0
        cy = y0 + math.sqrt(r * r - half_chord * half_chord)
        a0 = math.atan2(y0 - cy, x0 - 128)
        a1 = math.atan2(y0 - cy, (256 - x0) - 128)
        stroke_arc(draw, 128, cy, r, a0, a1, STROKE, blend(BLUE, alpha))

    # Monitor — stroked rounded rect (x78 y122 w100 h64 rx12), drawn as an
    # outer stroke-colored rounded rect with the interior knocked back to BG.
    half = STROKE / 2
    draw.rounded_rectangle(
        [*pt(78 - half, 122 - half), *pt(178 + half, 186 + half)],
        radius=px(12 + half), fill=BLUE,
    )
    draw.rounded_rectangle(
        [*pt(78 + half, 122 + half), *pt(178 - half, 186 - half)],
        radius=px(12 - half), fill=BG,
    )

    # Stand
    stroke_line(draw, (128, 192), (128, 204), STROKE, BLUE)
    stroke_line(draw, (102, 208), (154, 208), STROKE, BLUE)

    # Status LED
    dot(draw, pt(161, 169), px(12), GREEN)

    return img.resize((CANVAS, CANVAS), Image.LANCZOS)


def main():
    master = render_master()
    smaller = [master.resize((s, s), Image.LANCZOS) for s in (32, 64, 128, 256, 512)]
    master.save(OUT_ICNS, format='ICNS', append_images=smaller)
    print(f'Wrote {OUT_ICNS}')


if __name__ == '__main__':
    main()
