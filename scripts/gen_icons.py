#!/usr/bin/env python3
"""Generate the PWA icon set for ngrok-chat.

Design: a rounded-square gradient badge (accent -> accent-2, straight from the
app's CSS variables) carrying a white chat bubble with three dots -- the same
motif as the app's chat-bubble brand mark. The background glow colours mirror
the `.gate` backdrop in static/style.css so the icons feel part of the app.

Everything is drawn at 4x and downsampled with LANCZOS so edges are properly
antialiased at every output size.

Usage:  ./.venv/bin/python scripts/gen_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# --- brand colours (mirror static/style.css) ------------------------------
BG = (11, 14, 20)  # --bg        #0b0e14
ACCENT = (91, 140, 255)  # --accent    #5b8cff
ACCENT2 = (124, 91, 255)  # --accent-2  #7c5bff

SUPERSAMPLE = 4  # draw at 4x, downsample for antialiasing

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"


def diagonal_gradient(size: int, top_left, bottom_right) -> Image.Image:
    """Square RGB image with a linear gradient from top-left to bottom-right."""
    # PIL's linear gradient runs top(0) -> bottom(255); rotate it onto the diagonal.
    g = Image.linear_gradient("L").rotate(
        -45, expand=True, resample=Image.Resampling.BICUBIC
    )
    side = 256
    off = (g.size[0] - side) // 2
    g = g.crop((off, off, off + side, off + side))
    # Make sure the lighter (accent-2) end lands in the bottom-right corner.
    if g.getpixel((0, 0)) > g.getpixel((side - 1, side - 1)):
        g = g.transpose(Image.Transpose.ROTATE_180)
    g = g.resize((size, size), Image.Resampling.BICUBIC)
    lo = Image.new("RGB", (size, size), top_left)
    hi = Image.new("RGB", (size, size), bottom_right)
    return Image.composite(hi, lo, g)


def make_background(size: int) -> Image.Image:
    """Full-bleed dark background with two soft brand-coloured glows."""
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)
    # Accent-tinted glow top-left, purple-tinted glow bottom-right
    # (same idea/colours as the .gate radial gradients in style.css).
    d.ellipse(
        [-size * 0.30, -size * 0.35, size * 0.70, size * 0.45], fill=(30, 43, 74)
    )
    d.ellipse(
        [size * 0.45, size * 0.60, size * 1.35, size * 1.45], fill=(38, 28, 78)
    )
    return img.filter(ImageFilter.GaussianBlur(radius=size * 0.10))


def render_icon(
    size: int, *, badge_frac: float, full_bleed: bool, with_dots: bool = True
) -> Image.Image:
    """Render one icon at `size` px. Returns an RGBA image."""
    s = size * SUPERSAMPLE

    if full_bleed:
        canvas = make_background(s).convert("RGBA")
    else:
        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    side = int(s * badge_frac)
    off = (s - side) // 2
    box = (off, off, off + side, off + side)
    radius = int(side * 0.24)

    # Soft drop shadow behind the badge (only where there's transparency).
    if not full_bleed:
        sh = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        lift = int(side * 0.02)
        ImageDraw.Draw(sh).rounded_rectangle(
            [box[0], box[1] + lift, box[2], box[3] + lift],
            radius=radius,
            fill=(0, 0, 0, 150),
        )
        sh = sh.filter(ImageFilter.GaussianBlur(radius=s * 0.018))
        canvas = Image.alpha_composite(canvas, sh)

    # Gradient badge.
    badge = diagonal_gradient(s, ACCENT, ACCENT2).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    canvas.paste(badge, (0, 0), mask)

    # --- chat bubble motif -------------------------------------------------
    d = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)

    bub_w = side * 0.56
    bub_h = side * 0.42
    cx = s / 2
    cy = s / 2 - side * 0.05  # slightly above centre, room for the tail
    x0, y0 = cx - bub_w / 2, cy - bub_h / 2
    x1, y1 = cx + bub_w / 2, cy + bub_h / 2
    bub_r = bub_h * 0.34

    # Tail: small tapered triangle off the bottom-left, softened at the tip.
    tip = (x0 + bub_w * 0.06, y1 + bub_h * 0.26)
    d.polygon(
        [
            (x0 + bub_w * 0.16, y1 - bub_r * 0.10),
            tip,
            (x0 + bub_w * 0.40, y1 - bub_r * 0.10),
        ],
        fill=white,
    )
    tip_r = side * 0.013
    d.ellipse(
        [tip[0] - tip_r, tip[1] - tip_r, tip[0] + tip_r, tip[1] + tip_r], fill=white
    )
    d.rounded_rectangle([x0, y0, x1, y1], radius=bub_r, fill=white)

    # Three dots (dropped below 64px -- they'd only mud the silhouette).
    if with_dots:
        dot = (26, 34, 64, 255)
        dot_r = bub_h * 0.085
        for fx in (0.30, 0.50, 0.70):
            dx = x0 + bub_w * fx
            d.ellipse(
                [dx - dot_r, cy - dot_r, dx + dot_r, cy + dot_r], fill=dot
            )

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = [
        # name, size, render kwargs
        ("icon-192.png", 192, dict(badge_frac=0.86, full_bleed=False)),
        ("icon-256.png", 256, dict(badge_frac=0.86, full_bleed=False)),
        ("icon-512.png", 512, dict(badge_frac=0.86, full_bleed=False)),
        # Maskable: full-bleed bg, badge inside the 80% safe zone
        # (half-diagonal of a 56% square is ~39.6% of the canvas). 
        ("maskable-512.png", 512, dict(badge_frac=0.56, full_bleed=True)),
        # Apple applies its own squircle mask; keep it opaque and centred.
        ("apple-touch-icon.png", 180, dict(badge_frac=0.78, full_bleed=True)),
        # Favicons: full-bleed so they stay crisp at tiny sizes.
        ("favicon-32.png", 32, dict(badge_frac=1.0, full_bleed=True, with_dots=False)),
        ("favicon-16.png", 16, dict(badge_frac=1.0, full_bleed=True, with_dots=False)),
    ]
    for name, size, kwargs in targets:
        img = render_icon(size, **kwargs)
        if kwargs["full_bleed"]:
            img = img.convert("RGB")  # no transparency needed
        path = OUT_DIR / name
        img.save(path, "PNG", optimize=True)
        print(f"wrote static/icons/{name}  {size}x{size}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
