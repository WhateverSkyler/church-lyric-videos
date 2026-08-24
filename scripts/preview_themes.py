#!/usr/bin/env python3
"""Render one still per theme, plus a contact sheet of all of them.

    python scripts/preview_themes.py [--text "..."] [--out samples/]

Used to eyeball the theme pack without waiting on a full video render, and to
produce the thumbnails the dashboard's theme picker shows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from engine.brand import Fonts, Palette, TAGLINE, hex_to_rgb, load_font  # noqa: E402
from engine.render import preview_still  # noqa: E402
from engine.themes import THEMES  # noqa: E402

#: Neutral stand-in text. Long enough to force a wrap and show the type at
#: a realistic length, without standing in for anybody's copyrighted lyrics.
DEFAULT_TEXT = f"{TAGLINE}\nand every voice together"


def contact_sheet(images: dict, cols: int = 2, cell_w: int = 860,
                  pad: int = 26, label_h: int = 52) -> Image.Image:
    cell_h = round(cell_w * 1080 / 1920)
    rows = (len(images) + cols - 1) // cols
    sheet_w = cols * cell_w + pad * (cols + 1)
    sheet_h = rows * (cell_h + label_h) + pad * (rows + 1)
    sheet = Image.new("RGB", (sheet_w, sheet_h), hex_to_rgb(Palette.INK))
    draw = ImageDraw.Draw(sheet)
    font = load_font(Fonts.SANS_BOLD, 26)

    for i, (name, img) in enumerate(images.items()):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + label_h + pad)
        sheet.paste(img.resize((cell_w, cell_h), Image.LANCZOS), (x, y))
        draw.text((x + 4, y + cell_h + 12), name, font=font,
                  fill=hex_to_rgb(Palette.WHEAT))
    return sheet


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--out", type=Path, default=Path("samples"))
    ap.add_argument("--only", help="render just this theme key")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    keys = [args.only] if args.only else list(THEMES)
    shots = {}

    for key in keys:
        theme = THEMES[key]
        img = preview_still(theme, args.text)
        path = args.out / f"theme-{key}.jpg"
        img.save(path, "JPEG", quality=90)
        shots[f"{theme.name}  ·  {theme.mood}"] = img
        print(f"  {theme.name:18s} -> {path}")

    if len(shots) > 1:
        sheet_path = args.out / "theme-contact-sheet.jpg"
        contact_sheet(shots).save(sheet_path, "JPEG", quality=88)
        print(f"\ncontact sheet -> {sheet_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
