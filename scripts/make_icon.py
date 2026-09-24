#!/usr/bin/env python3
"""Generate the application icon (``desktop/assets/icon.ico``).

Checked in as a build script rather than a binary blob so the mark can be
tweaked without a design tool. Pillow is already a kernel dependency, so this
costs nothing extra.

The mark is the same diamond used in the UI chrome: a green ring on the app's
dark background, drawn at every size Windows asks for.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (20, 21, 23, 255)
ACCENT = (91, 140, 255, 255)
ACCENT_DIM = (47, 92, 196, 255)

SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 8  # supersampling factor for smooth diagonals at small sizes


def draw_mark(size: int) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded-square background
    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=BG)

    cx = cy = s / 2
    outer = s * 0.34
    inner = outer * 0.44
    width = max(SS, int(s * 0.055))

    def diamond(r: float) -> list[tuple[float, float]]:
        return [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]

    d.polygon(diamond(outer), fill=ACCENT)
    d.polygon(diamond(inner), fill=BG)

    # A terminal caret inside the ring, so the mark reads as "agent with a
    # shell" rather than a generic gem.
    w = int(s * 0.10)
    ax, ay = cx - inner * 0.42, cy - inner * 0.30
    d.line([(ax, ay), (ax + inner * 0.52, cy)], fill=ACCENT_DIM, width=w)
    d.line([(ax, ay), (ax, cy + inner * 0.30)], fill=ACCENT_DIM, width=w)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "desktop" / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = [draw_mark(n) for n in SIZES]
    ico = out_dir / "icon.ico"
    frames[-1].save(ico, format="ICO", sizes=[(n, n) for n in SIZES])

    png = out_dir / "icon.png"
    frames[-1].save(png, format="PNG")

    print(f"wrote {ico} ({ico.stat().st_size} bytes)")
    print(f"wrote {png} ({png.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
