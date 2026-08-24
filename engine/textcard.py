"""Renders one lyric line to a transparent full-frame PNG.

Why cards instead of burned-in subtitles: an .ass subtitle track can do fades
and outlines, but not gradient fills, soft glows or real letter-spacing. A song
only has ~40-80 lyric lines, so rendering one card per line stays cheap while
giving each theme full control over how the type looks. ffmpeg then overlays
the cards at their timestamps (see render.py).

Everything is drawn through a single alpha mask so the gradient, glow and
shadow always agree with each other exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .brand import FontFace, Fonts, Palette, hex_to_rgb, load_font

#: Supersampling factor. Text is laid out at N x and downsampled with LANCZOS,
#: which is what keeps gradient edges and glow falloff from stair-stepping.
SSAA = 2


@dataclass(frozen=True)
class Shadow:
    """A soft drop shadow cast by the text mask."""

    color: str = "#000000"
    opacity: float = 0.55
    blur: int = 18
    offset: tuple = (0, 6)


@dataclass(frozen=True)
class Glow:
    """A soft halo bloom in the text's own colour family."""

    color: str = Palette.AMBER
    opacity: float = 0.45
    blur: int = 34
    #: Drawn this many times to deepen the bloom without a huge blur radius.
    passes: int = 2


@dataclass(frozen=True)
class TextStyle:
    """How one theme wants lyric type to look."""

    face: FontFace = Fonts.SERIF_BOLD
    size: int = 78
    #: Solid colour, or None to use `gradient`.
    color: str | None = Palette.WHITE
    #: Top-to-bottom colour ramp across the whole text block.
    gradient: tuple | None = None
    #: Rotate the gradient; 0 = vertical (top->bottom), 90 = horizontal.
    gradient_angle: float = 0.0

    line_spacing: float = 1.32
    #: Extra px between glyphs. Positive opens display type up.
    letter_spacing: float = 0.0
    uppercase: bool = False

    stroke_width: int = 0
    stroke_color: str = "#000000"

    shadow: Shadow | None = field(default_factory=Shadow)
    glow: Glow | None = None

    #: Fraction of frame width the text may occupy before wrapping.
    max_width: float = 0.78
    #: Vertical anchor of the text block, 0 = top, 0.5 = centre, 1 = bottom.
    align_y: float = 0.5
    #: Horizontal anchor, 0.5 = centre.
    align_x: float = 0.5

    opacity: float = 1.0

    def scaled(self, factor: int) -> "TextStyle":
        """Return this style with all pixel dimensions multiplied by `factor`."""
        return replace(
            self,
            size=int(round(self.size * factor)),
            letter_spacing=self.letter_spacing * factor,
            stroke_width=int(round(self.stroke_width * factor)),
            shadow=(
                None
                if self.shadow is None
                else replace(
                    self.shadow,
                    blur=int(round(self.shadow.blur * factor)),
                    offset=(
                        int(round(self.shadow.offset[0] * factor)),
                        int(round(self.shadow.offset[1] * factor)),
                    ),
                )
            ),
            glow=(
                None
                if self.glow is None
                else replace(self.glow, blur=int(round(self.glow.blur * factor)))
            ),
        )


# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------


def _text_width(draw, text: str, font, letter_spacing: float) -> float:
    """Advance width of `text`, accounting for manual letter-spacing."""
    if not text:
        return 0.0
    width = draw.textlength(text, font=font)
    if letter_spacing:
        # Spacing applies between glyphs, so one fewer gap than characters.
        width += letter_spacing * max(0, len(text) - 1)
    return width


def wrap_lines(text: str, font, draw, max_px: float, letter_spacing: float) -> list:
    """Greedy word wrap. Respects explicit newlines the lyric author wrote."""
    out = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            out.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if _text_width(draw, candidate, font, letter_spacing) <= max_px:
                current = candidate
            else:
                out.append(current)
                current = word
        out.append(current)
    return out


def _draw_spaced(draw, xy, text: str, font, fill, letter_spacing: float,
                 stroke_width: int = 0, stroke_fill=None) -> None:
    """draw.text() with manual letter-spacing support."""
    if not letter_spacing:
        draw.text(xy, text, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        return
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += draw.textlength(char, font=font) + letter_spacing


# --------------------------------------------------------------------------
# fills
# --------------------------------------------------------------------------


def _gradient_image(size: tuple, ramp: tuple, angle: float = 0.0) -> Image.Image:
    """An RGB image ramping through `ramp`, rotated by `angle` degrees."""
    import numpy as np

    width, height = size
    stops = [hex_to_rgb(c) for c in ramp]
    if len(stops) == 1:
        return Image.new("RGB", size, stops[0])

    # Build the ramp along a generous diagonal so rotation never exposes edges.
    steps = max(width, height) * 2
    positions = np.linspace(0, len(stops) - 1, steps)
    lower = np.floor(positions).astype(int)
    upper = np.clip(lower + 1, 0, len(stops) - 1)
    blend = (positions - lower)[:, None]
    ramp_px = (
        np.array(stops, dtype=float)[lower] * (1 - blend)
        + np.array(stops, dtype=float)[upper] * blend
    ).astype("uint8")

    strip = Image.fromarray(ramp_px[None, :, :].repeat(steps, axis=0), "RGB")
    if angle:
        strip = strip.rotate(angle, resample=Image.BICUBIC, expand=False)
    # The ramp runs left->right in `strip`; rotate 90 so angle=0 reads top->bottom.
    strip = strip.rotate(90, resample=Image.BICUBIC, expand=False)
    left = (strip.width - width) // 2
    top = (strip.height - height) // 2
    return strip.crop((left, top, left + width, top + height))


def _tinted(mask: Image.Image, color: str, opacity: float) -> Image.Image:
    """An RGBA layer of flat `color` shaped by `mask`."""
    layer = Image.new("RGBA", mask.size, hex_to_rgb(color) + (0,))
    alpha = mask if opacity >= 1.0 else mask.point(lambda v: int(v * opacity))
    layer.putalpha(alpha)
    return layer


# --------------------------------------------------------------------------
# the public entry point
# --------------------------------------------------------------------------


def render_card(text: str, style: TextStyle, frame: tuple = (1920, 1080)) -> Image.Image:
    """Render `text` as a transparent RGBA image the size of the video frame."""
    if not text or not text.strip():
        return Image.new("RGBA", frame, (0, 0, 0, 0))

    hi = (frame[0] * SSAA, frame[1] * SSAA)
    st = style.scaled(SSAA)
    if st.uppercase:
        text = text.upper()

    font = load_font(st.face, st.size)

    # --- lay the block out on the supersampled canvas --------------------
    mask = Image.new("L", hi, 0)
    md = ImageDraw.Draw(mask)
    lines = wrap_lines(text, font, md, hi[0] * st.max_width, st.letter_spacing)

    ascent, descent = font.getmetrics()
    line_h = (ascent + descent) * st.line_spacing
    block_h = line_h * len(lines)

    # align_y anchors the block's centre within the frame's safe area.
    top = (hi[1] - block_h) * st.align_y

    for i, line in enumerate(lines):
        w = _text_width(md, line, font, st.letter_spacing)
        x = (hi[0] - w) * st.align_x
        y = top + i * line_h
        _draw_spaced(md, (x, y), line, font, 255, st.letter_spacing,
                     stroke_width=st.stroke_width, stroke_fill=255)

    layer = Image.new("RGBA", hi, (0, 0, 0, 0))

    # --- shadow, furthest back -------------------------------------------
    if st.shadow is not None:
        sh = mask.filter(ImageFilter.GaussianBlur(st.shadow.blur))
        shadow_layer = _tinted(sh, st.shadow.color, st.shadow.opacity)
        if st.shadow.offset != (0, 0):
            shifted = Image.new("RGBA", hi, (0, 0, 0, 0))
            shifted.paste(shadow_layer, st.shadow.offset)
            shadow_layer = shifted
        layer.alpha_composite(shadow_layer)

    # --- glow, behind the glyphs but in front of the shadow --------------
    if st.glow is not None:
        gl = mask.filter(ImageFilter.GaussianBlur(st.glow.blur))
        glow_layer = _tinted(gl, st.glow.color, st.glow.opacity)
        for _ in range(max(1, st.glow.passes)):
            layer.alpha_composite(glow_layer)

    # --- the stroke, drawn from the mask difference ----------------------
    if st.stroke_width and st.stroke_color:
        core = Image.new("L", hi, 0)
        cd = ImageDraw.Draw(core)
        for i, line in enumerate(lines):
            w = _text_width(cd, line, font, st.letter_spacing)
            x = (hi[0] - w) * st.align_x
            y = top + i * line_h
            _draw_spaced(cd, (x, y), line, font, 255, st.letter_spacing)
        # mask includes the stroke, core does not — the difference is the outline.
        from PIL import ImageChops

        outline = ImageChops.subtract(mask, core)
        layer.alpha_composite(_tinted(outline, st.stroke_color, 1.0))
        glyph_mask = core
    else:
        glyph_mask = mask

    # --- the glyphs themselves -------------------------------------------
    if st.gradient:
        fill = _gradient_image(hi, st.gradient, st.gradient_angle).convert("RGBA")
        fill.putalpha(glyph_mask)
    else:
        fill = _tinted(glyph_mask, st.color or Palette.WHITE, 1.0)
    layer.alpha_composite(fill)

    out = layer.resize(frame, Image.LANCZOS)
    if st.opacity < 1.0:
        alpha = out.getchannel("A").point(lambda v: int(v * st.opacity))
        out.putalpha(alpha)
    return out


def save_card(text: str, style: TextStyle, path: Path, frame: tuple = (1920, 1080)) -> Path:
    img = render_card(text, style, frame)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path
