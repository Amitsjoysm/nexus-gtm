#!/usr/bin/env python
"""Make the directory-listing icons in ``distribution/assets/`` from the brand mark.

    pip install pillow
    python scripts/listing_icons.py

Source: ``frontend/public/infojoy-mark.png`` (512x512, transparent). What each portal asks for:

- ``icon.png`` / ``icon-dark.png``, 512x512: Anthropic's directory icon, and ChatGPT's ``logo`` and
  ``composerIcon`` with their dark variants (square, at least 48x48).
- ``color.png``, 192x192, and ``outline.png``, 32x32 white on transparent: the Microsoft 365 /
  Teams pair that MCP certification asks for.

Run it again when the mark changes; the output is deterministic for one Pillow version.
"""
from __future__ import annotations

import pathlib

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARK = ROOT / "frontend" / "public" / "infojoy-mark.png"
OUT = ROOT / "distribution" / "assets"
LIGHT = (255, 255, 255, 255)
DARK = (20, 18, 28, 255)  # the public guide's dark background


def _mark(size: int) -> Image.Image:
    mark = Image.open(MARK).convert("RGBA")
    mark = mark.crop(mark.getbbox())
    mark.thumbnail((size, size), Image.Resampling.LANCZOS)
    return mark


def _on(background: tuple, canvas: int, symbol: int) -> Image.Image:
    image = Image.new("RGBA", (canvas, canvas), background)
    mark = _mark(symbol)
    image.alpha_composite(mark, ((canvas - mark.width) // 2, (canvas - mark.height) // 2))
    return image.convert("RGB")


def _outline(canvas: int, symbol: int) -> Image.Image:
    """White on transparent. The mark's lines are thin, so the alpha is strengthened after the
    resize or the 32px icon reads as a smudge."""
    alpha = _mark(symbol).getchannel("A").point(lambda a: min(255, int(a * 2.5)))
    image = Image.new("RGBA", (canvas, canvas), (255, 255, 255, 0))
    white = Image.new("RGBA", alpha.size, (255, 255, 255, 255))
    white.putalpha(alpha)
    image.alpha_composite(white, ((canvas - alpha.width) // 2, (canvas - alpha.height) // 2))
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _on(LIGHT, 512, 432).save(OUT / "icon.png", optimize=True)
    _on(DARK, 512, 432).save(OUT / "icon-dark.png", optimize=True)
    _on(LIGHT, 192, 120).save(OUT / "color.png", optimize=True)
    _outline(32, 30).save(OUT / "outline.png", optimize=True)
    for path in sorted(OUT.glob("*.png")):
        print(path.relative_to(ROOT), Image.open(path).size)


if __name__ == "__main__":
    main()
