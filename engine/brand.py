"""Hopewell Baptist Church brand tokens.

Every colour here was sampled from the church's own logo
(assets/logo/hopewell-horizontal.png) or read out of the live site's Elementor
global-colour variables, so themes never invent a palette.

Source of truth:
  logo      -> hopewellmoultrie.com/wp-content/uploads/2024/11/hopewell-transparent-horizontal-1.png
  site vars -> --e-global-color-accent #04508C, typography Merriweather / Lato
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
FONT_DIR = ASSETS / "fonts"
LOGO_DIR = ASSETS / "logo"

LOGO_HORIZONTAL = LOGO_DIR / "hopewell-horizontal.png"

TAGLINE = "Living Life Together In Christ"
CHURCH_NAME = "Hopewell Baptist Church"


RGB = tuple

#: OpenType axis tag -> the display name Pillow reports from get_variation_axes().
AXIS_NAMES = {
    "wght": "weight",
    "wdth": "width",
    "opsz": "optical size",
    "ital": "italic",
    "slnt": "slant",
}


def hex_to_rgb(value: str) -> RGB:
    """'#04508C' -> (4, 80, 140)."""
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def rgba(value: str, alpha: float = 1.0) -> RGB:
    """'#04508C', 0.5 -> (4, 80, 140, 127)."""
    return hex_to_rgb(value) + (max(0, min(255, round(alpha * 255))),)


class Palette:
    """Hopewell's colours, grouped by where they come from in the mark."""

    # --- The wordmark / site chrome -------------------------------------
    NAVY = "#004878"          # "BAPTIST CHURCH" lockup text, cross outline
    NAVY_DEEP = "#00304F"     # darkened navy, for backgrounds
    BLUE = "#04508C"          # site accent (--e-global-color-accent), CTA buttons

    # --- The sunburst, warm side (top-left of the mark) ------------------
    RED = "#C01818"
    EMBER = "#D84830"
    CORAL = "#D87860"
    AMBER = "#F0A818"
    GOLD = "#F0A848"
    SAND = "#F0C078"
    WHEAT = "#F0D890"
    BRASS = "#D8C060"

    # --- The hill / sunburst, cool side (lower-right of the mark) --------
    GRASS = "#90C060"
    SAGE = "#A8C078"
    MOSS = "#A8C060"
    LEAF = "#A8D878"
    OLIVE = "#909060"

    # --- Neutrals ---------------------------------------------------------
    WHITE = "#FFFFFF"
    OFFWHITE = "#F0F0F0"
    INK = "#0A0F14"
    CHARCOAL = "#161C22"

    #: Warm sunburst ramp, red -> wheat. Used for gradient text and light rays.
    WARM_RAMP = (RED, EMBER, AMBER, GOLD, SAND, WHEAT)
    #: Cool hill ramp.
    COOL_RAMP = (OLIVE, MOSS, GRASS, SAGE, LEAF)
    #: The full stained-glass sweep, in the order the wedges appear in the mark.
    SUNBURST_RAMP = (RED, EMBER, AMBER, GOLD, WHEAT, BRASS, MOSS, GRASS, SAGE)


@dataclass(frozen=True)
class FontFace:
    """A resolved font file plus the variable-font axes to apply, if any."""

    path: Path
    #: Variable-font axis values, e.g. {"wght": 700}. Empty for static faces.
    axes: dict

    def exists(self) -> bool:
        return self.path.is_file()


class Fonts:
    """The two families the church already uses on its own site."""

    #: Merriweather ships as a single variable file (opsz, wdth, wght).
    _MERRIWEATHER_VF = FONT_DIR / "Merriweather-VF.ttf"

    SERIF_LIGHT = FontFace(_MERRIWEATHER_VF, {"wght": 300})
    SERIF_REGULAR = FontFace(_MERRIWEATHER_VF, {"wght": 400})
    SERIF_BOLD = FontFace(_MERRIWEATHER_VF, {"wght": 700})
    SERIF_BLACK = FontFace(_MERRIWEATHER_VF, {"wght": 900})

    SANS_LIGHT = FontFace(FONT_DIR / "Lato-Light.ttf", {})
    SANS_REGULAR = FontFace(FONT_DIR / "Lato-Regular.ttf", {})
    SANS_BOLD = FontFace(FONT_DIR / "Lato-Bold.ttf", {})
    SANS_BLACK = FontFace(FONT_DIR / "Lato-Black.ttf", {})

    @classmethod
    def all_faces(cls) -> list:
        return [
            getattr(cls, name)
            for name in dir(cls)
            if name.isupper() and isinstance(getattr(cls, name), FontFace)
        ]


def load_font(face: FontFace, size: int):
    """Open a FontFace at `size`, applying variable axes when the file has them.

    Kept here rather than in the renderer so every theme resolves fonts the
    same way — including the axis fallback for Pillow builds without variable
    font support.
    """
    from PIL import ImageFont

    if not face.exists():
        raise FileNotFoundError(
            f"Missing font {face.path.name}. Run scripts/fetch_assets.sh to restore assets/fonts/."
        )

    font = ImageFont.truetype(str(face.path), size)
    if face.axes:
        try:
            axes = font.get_variation_axes()
            # Pillow exposes the axis *display name* (b'Weight'), not the OpenType
            # tag ('wght'), so match through AXIS_NAMES. set_variation_by_axes is
            # positional, so every axis needs a value — default for ones we don't set.
            values = []
            for axis in axes:
                name = axis["name"]
                if isinstance(name, bytes):
                    name = name.decode("ascii", "ignore")
                name = name.strip().lower()
                override = None
                for tag, val in face.axes.items():
                    if AXIS_NAMES.get(tag.lower()) == name:
                        override = val
                        break
                value = float(override if override is not None else axis["default"])
                # Clamp — an out-of-range axis value makes FreeType reject the whole set.
                values.append(max(float(axis["minimum"]), min(float(axis["maximum"]), value)))
            font.set_variation_by_axes(values)
        except (OSError, AttributeError):
            # Static build of Pillow/FreeType, or a non-variable file. The
            # default instance still renders — just without the weight change.
            pass
    return font


def verify_assets() -> list:
    """Return a list of human-readable problems with the bundled assets."""
    problems = []
    if not LOGO_HORIZONTAL.is_file():
        problems.append(f"missing logo: {LOGO_HORIZONTAL}")
    for face in Fonts.all_faces():
        if not face.exists():
            problems.append(f"missing font: {face.path}")
    return problems
