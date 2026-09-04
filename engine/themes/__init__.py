"""The Hopewell theme pack.

Six looks so a month of Sundays never repeats, all built from the same palette
sampled off the church's logo and all carrying the mark somewhere. A theme is
pure data plus two plate-builder functions — no theme owns any ffmpeg or
rendering logic, which is what keeps adding a seventh look cheap.

Add a theme by writing a module here and appending it to THEMES at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PIL import Image

from .. import background as bg
from ..brand import Fonts, Palette
from ..textanim import PRESETS as ANIM, TextAnimation
from ..textcard import Glow, Shadow, TextStyle


@dataclass(frozen=True)
class Motion:
    """How render.py animates the plates inside ffmpeg."""

    #: Ken Burns zoom on the base plate, start -> end over the whole song.
    zoom_from: float = 1.0
    zoom_to: float = 1.12
    #: Degrees per second of rotation applied to the overlay plate.
    overlay_spin: float = 0.0
    #: Overlay drift in frame-widths per minute, if it doesn't spin.
    overlay_drift: tuple = (0.0, 0.0)


@dataclass(frozen=True)
class LogoMark:
    """Where the Hopewell mark sits and how loud it is."""

    #: Anchor: one of tl, tc, tr, bl, bc, br.
    anchor: str = "bl"
    #: Width as a fraction of frame width.
    width: float = 0.185
    opacity: float = 0.88
    margin: float = 0.042
    #: Recolour the mark to flat white — for busy or light backgrounds where
    #: the full-colour sunburst competes with the lyrics.
    monochrome: bool = False
    mono_color: str = Palette.WHITE
    #: Blur radius of the halo behind the mark, so it keeps its edge over pale
    #: or busy footage. 0 disables it.
    halo: float = 16.0
    halo_color: str = "#000000"
    halo_strength: float = 1.6


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    description: str
    text: TextStyle
    build_base: Callable
    build_overlay: Callable | None = None
    overlay_opacity: float = 0.5
    #: 'screen' brightens (light effects), 'normal' composites flat.
    overlay_blend: str = "screen"
    motion: Motion = field(default_factory=Motion)
    logo: LogoMark = field(default_factory=LogoMark)
    #: Shown in the dashboard as a one-word mood tag. Also selects which
    #: bucket of stock footage this theme draws its backdrop from.
    mood: str = ""
    #: How the type arrives, sits and leaves. See textanim.PRESETS.
    animation: TextAnimation = field(default_factory=lambda: ANIM["lift"])

    def base_plate(self, frame: tuple):
        return bg.cached_plate(self.key, "base", frame,
                               lambda: self.build_base(bg.plate_size(frame)))

    def overlay_plate(self, frame: tuple):
        if self.build_overlay is None:
            return None
        return bg.cached_plate(self.key, "overlay", frame,
                               lambda: self.build_overlay(bg.plate_size(frame)))


# ==========================================================================
# 1. Cinematic Warm — the default. Dark room, gold light, modern worship.
# ==========================================================================


def _cinematic_base(size):
    base = bg.ramp(size, (Palette.INK, "#12161B", Palette.CHARCOAL), angle=90)
    # A warm pool of light spilling from above the frame.
    base = bg.screen(base, bg.tint(bg.radial(size, (0.0, -0.75), 1.5, 1.9),
                                   Palette.AMBER, 0.30))
    base = bg.screen(base, bg.tint(bg.radial(size, (-0.55, 0.55), 1.25, 2.4),
                                   Palette.EMBER, 0.10))
    base = bg.screen(base, bg.tint(bg.bokeh(size, count=30, seed=11),
                                   Palette.SAND, 0.20))
    base = base * bg.vignette(size, 0.50)[..., None]
    base += bg.grain(size)[..., None]
    return bg.blur(bg.to_image(base), 2)


def _cinematic_overlay(size):
    r = bg.rays(size, count=13, center=(0.05, -1.05), sharpness=5.5, seed=7)
    layer = bg.tint(r, Palette.WHEAT, 1.0)
    return bg.blur(bg.to_image(layer), 26)


CINEMATIC_WARM = Theme(
    key="cinematic-warm",
    name="Cinematic Warm",
    description="Dark room, soft gold light rays and drifting bokeh. The default Sunday look.",
    mood="warm",
    animation=ANIM["lift"],
    text=TextStyle(
        face=Fonts.SERIF_BLACK, size=106,
        gradient=(Palette.WHITE, "#FFF6E4", Palette.WHEAT),
        glow=Glow(color=Palette.AMBER, opacity=0.26, blur=46, passes=1),
        shadow=Shadow(opacity=0.80, blur=30, offset=(0, 8)),
        stroke_width=2, stroke_color="#0A0705",
        letter_spacing=0.5, line_spacing=1.24, max_width=0.86,
    ),
    build_base=_cinematic_base,
    build_overlay=_cinematic_overlay,
    overlay_opacity=0.30,
    motion=Motion(zoom_from=1.0, zoom_to=1.10, overlay_spin=0.35),
    logo=LogoMark(anchor="bl", width=0.19, opacity=0.92),
)


# ==========================================================================
# 2. Navy Minimal — maximum readability. Nothing competes with the words.
# ==========================================================================


def _navy_base(size):
    base = bg.ramp(size, ("#00243D", Palette.NAVY, "#00243D"), angle=90)
    base = bg.screen(base, bg.tint(bg.radial(size, (0.0, 0.0), 1.5, 2.2),
                                   Palette.BLUE, 0.30))
    base = base * bg.vignette(size, 0.38)[..., None]
    base += bg.grain(size, 0.012)[..., None]
    return bg.blur(bg.to_image(base), 3)


NAVY_MINIMAL = Theme(
    key="navy-minimal",
    name="Navy Minimal",
    description="Flat church navy, bold sans type. Easiest to read from the back row.",
    mood="clean",
    animation=ANIM["gentle"],
    text=TextStyle(
        face=Fonts.SANS_BLACK, size=100, color=Palette.WHITE,
        glow=None,
        shadow=Shadow(opacity=0.62, blur=22, offset=(0, 6)),
        stroke_width=2, stroke_color="#001B2E",
        letter_spacing=1.2, line_spacing=1.28, max_width=0.88,
    ),
    build_base=_navy_base,
    motion=Motion(zoom_from=1.0, zoom_to=1.05),
    logo=LogoMark(anchor="br", width=0.18, opacity=0.92),
)


# ==========================================================================
# 3. Stained Glass — the logo's sunburst blown up to fill the frame.
# ==========================================================================


def _glass_base(size):
    import numpy as np

    idx = bg.wedges(size, count=len(Palette.SUNBURST_RAMP), center=(0.0, 0.10))
    cols = np.array([bg.hex_to_rgb(c) for c in Palette.SUNBURST_RAMP],
                    dtype=np.float32) / 255.0
    base = cols[idx]
    # Pull the whole thing down hard — these are lyric backdrops, not posters.
    base *= 0.30
    base = base * bg.vignette(size, 0.55, radius=1.5)[..., None]
    # Dim a wide band through the middle so text always has somewhere to sit.
    band = bg.radial(size, (0.0, 0.0), 1.5, 1.2)
    base *= (1.0 - 0.45 * band)[..., None]
    base += bg.grain(size, 0.012)[..., None]
    return bg.blur(bg.to_image(base), 9)


def _glass_overlay(size):
    r = bg.rays(size, count=len(Palette.SUNBURST_RAMP), center=(0.0, 0.10),
                sharpness=2.5, seed=3)
    return bg.blur(bg.to_image(bg.tint(r, Palette.WHEAT, 0.85)), 34)


STAINED_GLASS = Theme(
    key="stained-glass",
    name="Stained Glass",
    description="The church's sunburst mark opened up across the whole frame.",
    mood="reverent",
    animation=ANIM["focus"],
    text=TextStyle(
        face=Fonts.SERIF_BLACK, size=104,
        gradient=(Palette.WHITE, "#FFF4DC"),
        glow=Glow(color=Palette.GOLD, opacity=0.22, blur=50, passes=1),
        shadow=Shadow(opacity=0.84, blur=32, offset=(0, 9)),
        stroke_width=3, stroke_color="#100A04",
        letter_spacing=0.4, line_spacing=1.24, max_width=0.84,
    ),
    build_base=_glass_base,
    build_overlay=_glass_overlay,
    overlay_opacity=0.22,
    motion=Motion(zoom_from=1.02, zoom_to=1.14, overlay_spin=-0.5),
    logo=LogoMark(anchor="bl", width=0.185, opacity=0.94, halo=20),
)


# ==========================================================================
# 4. Sanctuary Dusk — deep blue falling into ember. Slow and hymn-like.
# ==========================================================================


def _dusk_base(size):
    base = bg.ramp(size, ("#050A12", "#0B2038", Palette.NAVY, "#5A3320", "#7A3A1E"),
                   angle=90)
    base = bg.screen(base, bg.tint(bg.radial(size, (0.35, 0.85), 1.5, 2.0),
                                   Palette.EMBER, 0.34))
    base = bg.screen(base, bg.tint(bg.radial(size, (-0.6, -0.7), 1.2, 2.6),
                                   Palette.BLUE, 0.16))
    base = base * bg.vignette(size, 0.42)[..., None]
    base += bg.grain(size)[..., None]
    return bg.blur(bg.to_image(base), 6)


def _dusk_overlay(size):
    haze = bg.bokeh(size, count=16, seed=23, min_r=0.18, max_r=0.42)
    return bg.blur(bg.to_image(bg.tint(haze, Palette.SAND, 0.7)), 60)


SANCTUARY_DUSK = Theme(
    key="sanctuary-dusk",
    name="Sanctuary Dusk",
    description="Deep blue falling into ember light. Suits slower hymns and invitations.",
    mood="reflective",
    animation=ANIM["hymn"],
    text=TextStyle(
        face=Fonts.SERIF_BOLD, size=108,
        gradient=(Palette.WHITE, "#FFEFD8"),
        glow=Glow(color=Palette.SAND, opacity=0.22, blur=52, passes=1),
        shadow=Shadow(opacity=0.78, blur=32, offset=(0, 9)),
        stroke_width=2, stroke_color="#0B0A12",
        letter_spacing=0.8, line_spacing=1.26, max_width=0.86,
    ),
    build_base=_dusk_base,
    build_overlay=_dusk_overlay,
    overlay_opacity=0.30,
    motion=Motion(zoom_from=1.0, zoom_to=1.09, overlay_drift=(0.03, -0.015)),
    logo=LogoMark(anchor="bl", width=0.19, opacity=0.92),
)


# ==========================================================================
# 5. Morning Light — the one light theme, for bright rooms and blinds-up days.
# ==========================================================================


def _morning_base(size):
    base = bg.ramp(size, ("#FBF4E6", "#F5E9D2", "#EFDFC4"), angle=90)
    base = bg.screen(base, bg.tint(bg.radial(size, (0.35, -0.6), 1.4, 2.0),
                                   Palette.WHITE, 0.55))
    # Warm the lower corners so the cream doesn't read as flat paper.
    base *= (1.0 - 0.10 * bg.linear(size, 90))[..., None]
    base *= bg.vignette(size, 0.18, radius=1.6)[..., None]
    base += bg.grain(size, 0.010)[..., None]
    return bg.blur(bg.to_image(base), 4)


MORNING_LIGHT = Theme(
    key="morning-light",
    name="Morning Light",
    description="Warm cream and deep navy type. Built for bright rooms where dark themes wash out.",
    mood="bright",
    animation=ANIM["rise"],
    text=TextStyle(
        face=Fonts.SERIF_BLACK, size=104,
        color=Palette.NAVY_DEEP, gradient=None,
        glow=None,
        shadow=Shadow(color="#FFFFFF", opacity=0.55, blur=18, offset=(0, 0)),
        stroke_width=2, stroke_color="#FFF6E8",
        letter_spacing=0.4, line_spacing=1.26, max_width=0.86,
    ),
    build_base=_morning_base,
    motion=Motion(zoom_from=1.0, zoom_to=1.06),
    # A dark halo, like every other theme. This one carried a white one, and
    # the mark's wordmark is itself white — white glow behind white type on a
    # cream plate left the logo unreadable in the first real render.
    logo=LogoMark(anchor="br", width=0.185, opacity=1.0, halo=14),
)


# ==========================================================================
# 6. Hillside — the green half of the mark. Open, outdoor, hopeful.
# ==========================================================================


def _hillside_base(size):
    base = bg.ramp(size, ("#0A1410", "#12281A", "#1C3A22", "#2A4A24"), angle=90)
    base = bg.screen(base, bg.tint(bg.radial(size, (0.55, -0.8), 1.5, 2.0),
                                   Palette.WHEAT, 0.26))
    base = bg.screen(base, bg.tint(bg.radial(size, (-0.4, 0.7), 1.3, 2.4),
                                   Palette.GRASS, 0.14))
    base = base * bg.vignette(size, 0.46)[..., None]
    base += bg.grain(size)[..., None]
    return bg.blur(bg.to_image(base), 3)


def _hillside_overlay(size):
    r = bg.rays(size, count=10, center=(0.55, -0.9), sharpness=4.0, seed=19)
    return bg.blur(bg.to_image(bg.tint(r, Palette.LEAF, 0.9)), 30)


HILLSIDE = Theme(
    key="hillside",
    name="Hillside",
    description="The green half of the church mark — open and outdoor. Good for upbeat songs.",
    mood="hopeful",
    animation=ANIM["reveal"],
    text=TextStyle(
        face=Fonts.SANS_BLACK, size=102,
        gradient=(Palette.WHITE, "#F2FFE8"),
        glow=Glow(color=Palette.LEAF, opacity=0.20, blur=44, passes=1),
        shadow=Shadow(opacity=0.78, blur=28, offset=(0, 8)),
        stroke_width=2, stroke_color="#08120A",
        letter_spacing=0.6, line_spacing=1.26, max_width=0.86,
    ),
    build_base=_hillside_base,
    build_overlay=_hillside_overlay,
    overlay_opacity=0.24,
    motion=Motion(zoom_from=1.0, zoom_to=1.11, overlay_spin=0.25),
    logo=LogoMark(anchor="bl", width=0.19, opacity=0.92),
)


# ==========================================================================


THEMES = {
    t.key: t
    for t in (
        CINEMATIC_WARM,
        NAVY_MINIMAL,
        STAINED_GLASS,
        SANCTUARY_DUSK,
        MORNING_LIGHT,
        HILLSIDE,
    )
}

DEFAULT_THEME = CINEMATIC_WARM.key


def get(key: str) -> Theme:
    """Look up a theme, or pick one at random for the 'surprise me' option."""
    if key in ("random", "surprise", "any"):
        import random

        return random.choice(list(THEMES.values()))
    try:
        return THEMES[key]
    except KeyError:
        raise KeyError(f"Unknown theme {key!r}. Available: {', '.join(sorted(THEMES))}")


def listing() -> list:
    """Theme metadata for the dashboard's picker."""
    return [
        {"key": t.key, "name": t.name, "description": t.description, "mood": t.mood}
        for t in THEMES.values()
    ]
