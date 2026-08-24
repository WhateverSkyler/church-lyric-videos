"""Lays a lyric line out and rasterises each word as its own sprite.

Word-level sprites are what let type arrive a word at a time, drift
independently, or blur in from soft focus — none of which a single flat
line-image can do.

Two details worth keeping straight:

  gradient continuity  each word samples its colour from the *line's* gradient
                       at the word's own position, so a ramp still runs
                       smoothly across the whole line instead of restarting
                       inside every word.

  effect padding       glow and drop shadow bleed outside the glyph box, so
                       every sprite is padded by however much its effects
                       need. Without this the blur gets clipped into a
                       visible rectangle.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .anim import Sprite
from .brand import Palette, hex_to_rgb, load_font
from .textcard import SSAA, _draw_spaced, _gradient_image, _text_width, _tinted, wrap_lines


@dataclass
class WordBox:
    """One word's resting geometry, in final frame pixels."""

    text: str
    x: float
    y: float
    width: float
    height: float
    line_index: int
    word_index: int
    #: Index across the whole lyric line, not just this display row.
    global_index: int


@dataclass
class LineLayout:
    words: list
    block_x: float
    block_y: float
    block_w: float
    block_h: float
    rows: int

    @property
    def count(self) -> int:
        return len(self.words)


def _effect_padding(style) -> int:
    """How much room this style's glow/shadow needs outside the glyphs."""
    pad = 4 + style.stroke_width * 2
    if style.shadow is not None:
        pad = max(pad, int(style.shadow.blur * 2.5
                           + max(abs(style.shadow.offset[0]), abs(style.shadow.offset[1]))))
    if style.glow is not None:
        pad = max(pad, int(style.glow.blur * 2.5))
    return int(pad)


def layout_line(text: str, style, frame: tuple) -> LineLayout:
    """Measure `text` under `style` and return every word's resting position."""
    if style.uppercase:
        text = text.upper()

    font = load_font(style.face, style.size)
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    rows = wrap_lines(text, font, probe, frame[0] * style.max_width, style.letter_spacing)

    ascent, descent = font.getmetrics()
    line_h = (ascent + descent) * style.line_spacing
    block_h = line_h * len(rows)
    top = (frame[1] - block_h) * style.align_y

    words = []
    block_left, block_right = frame[0], 0.0
    running = 0

    for row_i, row in enumerate(rows):
        row_w = _text_width(probe, row, font, style.letter_spacing)
        x = (frame[0] - row_w) * style.align_x
        y = top + row_i * line_h
        block_left = min(block_left, x)
        block_right = max(block_right, x + row_w)

        for word_i, word in enumerate(row.split()):
            w = _text_width(probe, word, font, style.letter_spacing)
            words.append(WordBox(word, x, y, w, ascent + descent,
                                 row_i, word_i, running))
            running += 1
            # Advance past the word plus the space that followed it.
            x += w + probe.textlength(" ", font=font) + style.letter_spacing

    return LineLayout(words, block_left, top, max(1.0, block_right - block_left),
                      block_h, len(rows))


def render_word(box: WordBox, style, frame: tuple) -> Sprite:
    """Rasterise one word, with its slice of the line's gradient and effects."""
    pad = _effect_padding(style)
    sprite_w = int(box.width + pad * 2)
    sprite_h = int(box.height + pad * 2)

    hi = (sprite_w * SSAA, sprite_h * SSAA)
    font = load_font(style.face, style.size * SSAA)
    origin = (pad * SSAA, pad * SSAA)

    # --- the alpha mask, including the stroke ---------------------------
    mask = Image.new("L", hi, 0)
    md = ImageDraw.Draw(mask)
    _draw_spaced(md, origin, box.text, font, 255, style.letter_spacing * SSAA,
                 stroke_width=style.stroke_width * SSAA, stroke_fill=255)

    layer = Image.new("RGBA", hi, (0, 0, 0, 0))

    if style.shadow is not None:
        sh = mask.filter(ImageFilter.GaussianBlur(style.shadow.blur * SSAA))
        shadow = _tinted(sh, style.shadow.color, style.shadow.opacity)
        off = (style.shadow.offset[0] * SSAA, style.shadow.offset[1] * SSAA)
        if off != (0, 0):
            shifted = Image.new("RGBA", hi, (0, 0, 0, 0))
            shifted.paste(shadow, off)
            shadow = shifted
        layer.alpha_composite(shadow)

    if style.glow is not None:
        gl = mask.filter(ImageFilter.GaussianBlur(style.glow.blur * SSAA))
        glow = _tinted(gl, style.glow.color, style.glow.opacity)
        for _ in range(max(1, style.glow.passes)):
            layer.alpha_composite(glow)

    if style.stroke_width and style.stroke_color:
        core = Image.new("L", hi, 0)
        cd = ImageDraw.Draw(core)
        _draw_spaced(cd, origin, box.text, font, 255, style.letter_spacing * SSAA)
        layer.alpha_composite(_tinted(ImageChops.subtract(mask, core),
                                      style.stroke_color, 1.0))
        glyph_mask = core
    else:
        glyph_mask = mask

    # --- the glyph fill --------------------------------------------------
    if style.gradient:
        # Sample the full-frame gradient at this word's position so the ramp
        # stays continuous across the line rather than restarting per word.
        full = _gradient_image((frame[0] * SSAA, frame[1] * SSAA),
                               style.gradient, style.gradient_angle)
        left = int((box.x - pad) * SSAA)
        top = int((box.y - pad) * SSAA)
        region = full.crop((left, top, left + hi[0], top + hi[1])).convert("RGBA")
        region.putalpha(glyph_mask)
        fill = region
    else:
        fill = _tinted(glyph_mask, style.color or Palette.WHITE, 1.0)
    layer.alpha_composite(fill)

    small = layer.resize((sprite_w, sprite_h), Image.LANCZOS)
    return Sprite(
        rgba=np.asarray(small, dtype=np.uint8).copy(),
        x=int(box.x - pad),
        y=int(box.y - pad),
        meta={
            "word": box.text,
            "index": box.global_index,
            "row": box.line_index,
            "row_word": box.word_index,
        },
    )


def typeset(text: str, style, frame: tuple) -> tuple:
    """Lay out `text` and rasterise every word.

    Returns (sprites, layout). One gradient image is built per word only when
    the style actually uses a gradient, which is the expensive path — solid
    fills skip it entirely.
    """
    layout = layout_line(text, style, frame)
    return [render_word(box, style, frame) for box in layout.words], layout
