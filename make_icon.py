"""Generate the app icon as PNG / ICO / ICNS.

Theme — "network monitoring radar":
  · Solid dark-navy rounded square base
  · Cyan neon glow border (outer)
  · Concentric radar rings (3, fading outward)
  · Crosshairs through center (faint cyan)
  · A bright sweep line emanating from center toward upper-right (gradient
    cyan → transparent) drawn as a polygon for clean falloff
  · A single bright green "blip" dot on one of the rings
  · Tick marks at cardinal points around the outer ring

Usage:
    python make_icon.py

Outputs alongside this script:
    icon.png       1024×1024 master / fallback / window icon
    icon.icns      macOS multi-resolution bundle (only on macOS)
    icon.ico       Windows multi-resolution
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).parent

# Palette aligned with the in-app theme
PANEL    = (10, 14, 26, 255)     # dark navy base
BORDER   = (24, 33, 54, 255)     # ring stroke base
ACCENT   = (0, 212, 255)         # neon cyan
ACCENT_S = (61, 220, 151)        # neon green (the blip)
WHITE    = (255, 255, 255)


def render(size: int) -> Image.Image:
    """Draw the icon at the given pixel size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    pad    = int(size * 0.06)
    radius = int(size * 0.22)
    box    = (pad, pad, size - pad, size - pad)

    # ── 1. dark base ──
    draw.rounded_rectangle(box, radius=radius, fill=PANEL)

    # ── 2. outer neon glow border (drawn outside the base + blurred) ──
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(
        (pad - 4, pad - 4, size - pad + 4, size - pad + 4),
        radius=radius + 4, outline=(*ACCENT, 230),
        width=max(2, int(size * 0.013)))
    glow = glow.filter(ImageFilter.GaussianBlur(max(2, int(size * 0.013))))
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img, "RGBA")
    # crisp inner border on top
    draw.rounded_rectangle(box, radius=radius,
                           outline=(*ACCENT, 255),
                           width=max(1, int(size * 0.005)))

    # ── 3. concentric radar rings (3 of them, fading outward) ──
    cx = cy = size // 2
    max_r = int(size * 0.36)
    ring_w = max(1, int(size * 0.005))
    for i, frac in enumerate((0.40, 0.66, 0.93)):
        r = int(max_r * frac)
        alpha = 255 - i * 70
        draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                     outline=(*ACCENT, alpha), width=ring_w)

    # ── 4. crosshairs through center ──
    cross_len = int(max_r * 1.05)
    cross_w = max(1, int(size * 0.004))
    draw.line([(cx - cross_len, cy), (cx + cross_len, cy)],
              fill=(*ACCENT, 110), width=cross_w)
    draw.line([(cx, cy - cross_len), (cx, cy + cross_len)],
              fill=(*ACCENT, 110), width=cross_w)

    # ── 5. radar sweep wedge (cyan, fading outward) ──
    # We draw a triangular wedge from center to two arc-points then blur it.
    sweep_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sweep_layer, "RGBA")
    sweep_angle_start = -90 + 6     # almost up
    sweep_angle_end   = -90 + 60    # toward upper-right
    n = 24
    for i in range(n):
        t = i / n
        a1 = math.radians(sweep_angle_start + (sweep_angle_end - sweep_angle_start) * t)
        a2 = math.radians(sweep_angle_start + (sweep_angle_end - sweep_angle_start) * (t + 1 / n))
        x1 = cx + max_r * math.cos(a1); y1 = cy + max_r * math.sin(a1)
        x2 = cx + max_r * math.cos(a2); y2 = cy + max_r * math.sin(a2)
        # alpha falls off along sweep angle (front = bright, tail = faint)
        a = int(220 * (1 - t * 0.85))
        sd.polygon([(cx, cy), (x1, y1), (x2, y2)], fill=(*ACCENT, a))
    sweep_layer = sweep_layer.filter(ImageFilter.GaussianBlur(max(1, int(size * 0.004))))
    img = Image.alpha_composite(img, sweep_layer)
    draw = ImageDraw.Draw(img, "RGBA")

    # ── 6. leading edge of the sweep (bright cyan line) ──
    edge_a = math.radians(sweep_angle_start)
    ex = cx + max_r * math.cos(edge_a)
    ey = cy + max_r * math.sin(edge_a)
    draw.line([(cx, cy), (ex, ey)],
              fill=(*ACCENT, 240), width=max(2, int(size * 0.008)))

    # ── 7. blip dot — green, on the middle ring at ~120° ──
    blip_a = math.radians(-150)
    blip_r = int(max_r * 0.66)
    bx = cx + blip_r * math.cos(blip_a)
    by = cy + blip_r * math.sin(blip_a)
    blip_size = int(size * 0.026)
    # outer halo
    halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hd.ellipse((bx - blip_size * 2.4, by - blip_size * 2.4,
                bx + blip_size * 2.4, by + blip_size * 2.4),
               fill=(*ACCENT_S, 200))
    halo = halo.filter(ImageFilter.GaussianBlur(max(2, int(size * 0.012))))
    img = Image.alpha_composite(img, halo)
    draw = ImageDraw.Draw(img, "RGBA")
    # solid blip
    draw.ellipse((bx - blip_size, by - blip_size,
                  bx + blip_size, by + blip_size),
                 fill=(*WHITE, 255))

    # ── 8. center point (white pinpoint with cyan halo) ──
    center_halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    chd = ImageDraw.Draw(center_halo)
    chd.ellipse((cx - size * 0.04, cy - size * 0.04,
                 cx + size * 0.04, cy + size * 0.04),
                fill=(*ACCENT, 220))
    center_halo = center_halo.filter(ImageFilter.GaussianBlur(max(2, int(size * 0.01))))
    img = Image.alpha_composite(img, center_halo)
    draw = ImageDraw.Draw(img, "RGBA")
    pin_r = max(2, int(size * 0.014))
    draw.ellipse((cx - pin_r, cy - pin_r, cx + pin_r, cy + pin_r),
                 fill=(*WHITE, 255))

    # ── 9. cardinal tick marks just outside the largest ring ──
    tick_r1 = int(max_r * 1.02)
    tick_r2 = int(max_r * 1.10)
    for ang_deg in (0, 90, 180, 270):
        a = math.radians(ang_deg - 90)  # 0 = up
        x1 = cx + tick_r1 * math.cos(a)
        y1 = cy + tick_r1 * math.sin(a)
        x2 = cx + tick_r2 * math.cos(a)
        y2 = cy + tick_r2 * math.sin(a)
        draw.line([(x1, y1), (x2, y2)],
                  fill=(*ACCENT, 230), width=max(2, int(size * 0.006)))

    return img


def main():
    print(f"writing icon assets to {ROOT}")

    png = render(1024)
    png_path = ROOT / "icon.png"
    png.save(png_path)
    print(f"  ✓ {png_path.relative_to(ROOT)}")

    ico_path = ROOT / "icon.ico"
    render(256).save(ico_path, format="ICO",
                     sizes=[(16, 16), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
    print(f"  ✓ {ico_path.relative_to(ROOT)}")

    if sys.platform == "darwin":
        iconset = ROOT / "icon.iconset"
        if iconset.exists():
            shutil.rmtree(iconset)
        iconset.mkdir()
        spec = [
            (16,    "icon_16x16.png"),
            (32,    "icon_16x16@2x.png"),
            (32,    "icon_32x32.png"),
            (64,    "icon_32x32@2x.png"),
            (128,   "icon_128x128.png"),
            (256,   "icon_128x128@2x.png"),
            (256,   "icon_256x256.png"),
            (512,   "icon_256x256@2x.png"),
            (512,   "icon_512x512.png"),
            (1024,  "icon_512x512@2x.png"),
        ]
        for sz, name in spec:
            render(sz).save(iconset / name)
        icns_path = ROOT / "icon.icns"
        try:
            subprocess.check_call(
                ["iconutil", "-c", "icns", str(iconset),
                 "-o", str(icns_path)])
            print(f"  ✓ {icns_path.relative_to(ROOT)}")
            shutil.rmtree(iconset)
        except FileNotFoundError:
            print("  ! iconutil not found")
        except Exception as e:
            print(f"  ! iconutil failed: {e}")
    else:
        print("  ! .icns skipped (only generated on macOS)")


if __name__ == "__main__":
    main()
