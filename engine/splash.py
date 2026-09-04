"""The branded open and close, and the logo's journey between them.

Every video opens on the church's mark at full size with the song's name under
it, then the mark travels to its corner and stays there for the body of the
song, and at the end it returns to the middle with the tagline. One continuous
element rather than three separate ones — the corner logo is the same mark that
opened the video, just parked.

The whole sequence is adaptive. Where a song starts singing almost immediately
there is no room for an opening, so it shortens, and below a useful minimum it
is skipped entirely. A splash that runs over the first line would be worse than
no splash at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from .anim import ease, mix
from .brand import CHURCH_NAME, TAGLINE

#: Ideal shape of the opening, in seconds.
LOGO_IN = 0.85          # mark fades and settles
TITLE_IN = 0.55         # song name arrives under it
HOLD = 1.70             # both sit still and readable
TRAVEL = 1.05           # mark moves to its corner, title leaves
IDEAL_INTRO = LOGO_IN + TITLE_IN + HOLD + TRAVEL

#: Below this there isn't room to read anything, so the opening is dropped.
MIN_INTRO = 2.2

#: Shape of the close.
OUTRO_TRAVEL = 1.10     # mark returns to the middle
OUTRO_HOLD = 2.20       # tagline sits with it
OUTRO_FADE = 0.90
IDEAL_OUTRO = OUTRO_TRAVEL + OUTRO_HOLD + OUTRO_FADE


@dataclass
class LogoState:
    """Where the mark is, how big, and how visible, on one frame."""

    #: Centre position as a fraction of the frame.
    cx: float
    cy: float
    #: Width as a fraction of frame width.
    width: float
    opacity: float


@dataclass
class Plan:
    """The timeline of the open and close for one song."""

    intro_end: float          # when the mark has finished parking
    intro_title_from: float
    intro_title_until: float
    outro_start: float        # when the mark begins returning
    outro_hold_from: float
    duration: float
    has_intro: bool
    has_outro: bool
    #: Resting corner position, taken from the theme.
    corner: LogoState
    #: Centre position used by the open and close.
    centre: LogoState


def build_plan(duration: float, first_lyric: float, last_lyric_end: float,
               corner: LogoState, centre_width: float = 0.44) -> Plan:
    """Fit the open and close around the song's actual lyrics."""
    centre = LogoState(0.5, 0.43, centre_width, 1.0)

    available_in = max(0.0, first_lyric - 0.25)
    has_intro = available_in >= MIN_INTRO
    if has_intro:
        span = min(IDEAL_INTRO, available_in)
        scale = span / IDEAL_INTRO
        logo_in = LOGO_IN * scale
        title_in = TITLE_IN * scale
        hold = HOLD * scale
        travel = TRAVEL * scale
        intro_end = span
        title_from = logo_in * 0.75
        title_until = logo_in + title_in + hold
    else:
        intro_end = 0.0
        title_from = title_until = 0.0

    available_out = max(0.0, duration - last_lyric_end - 0.35)
    has_outro = available_out >= 2.0
    if has_outro:
        span = min(IDEAL_OUTRO, available_out)
        outro_start = duration - span
        outro_hold_from = outro_start + OUTRO_TRAVEL * (span / IDEAL_OUTRO)
    else:
        outro_start = duration + 1.0
        outro_hold_from = outro_start

    return Plan(intro_end, title_from, title_until, outro_start,
                outro_hold_from, duration, has_intro, has_outro,
                corner, centre)


def logo_state(plan: Plan, t: float) -> LogoState:
    """Where the mark should be drawn at time `t`."""
    corner, centre = plan.corner, plan.centre

    # --- the open --------------------------------------------------------
    if plan.has_intro and t < plan.intro_end:
        scale = plan.intro_end / IDEAL_INTRO
        logo_in = LOGO_IN * scale
        travel = TRAVEL * scale
        travel_from = plan.intro_end - travel

        if t < logo_in:
            p = ease("out_cubic", t / max(1e-3, logo_in))
            return LogoState(centre.cx, centre.cy,
                             centre.width * mix(0.88, 1.0, p),
                             centre.opacity * p)
        if t < travel_from:
            return centre
        # Travelling to its corner.
        p = ease("in_out_cubic", (t - travel_from) / max(1e-3, travel))
        return LogoState(
            mix(centre.cx, corner.cx, p),
            mix(centre.cy, corner.cy, p),
            mix(centre.width, corner.width, p),
            mix(centre.opacity, corner.opacity, p),
        )

    # --- the close -------------------------------------------------------
    if plan.has_outro and t >= plan.outro_start:
        travel = plan.outro_hold_from - plan.outro_start
        if t < plan.outro_hold_from:
            p = ease("in_out_cubic", (t - plan.outro_start) / max(1e-3, travel))
            return LogoState(
                mix(corner.cx, centre.cx, p),
                mix(corner.cy, centre.cy, p),
                mix(corner.width, centre.width, p),
                mix(corner.opacity, centre.opacity, p),
            )
        # Held in the middle, then fading with the video.
        fade_from = plan.duration - OUTRO_FADE
        if t >= fade_from:
            p = ease("in_out_sine", (t - fade_from) / max(1e-3, OUTRO_FADE))
            return LogoState(centre.cx, centre.cy, centre.width,
                             centre.opacity * (1.0 - p))
        return centre

    # --- the long middle -------------------------------------------------
    return corner


def title_opacity(plan: Plan, t: float) -> float:
    """How visible the song title is during the open."""
    if not plan.has_intro or t >= plan.intro_title_until + 0.5:
        return 0.0
    if t < plan.intro_title_from:
        return 0.0
    fade_in = max(1e-3, TITLE_IN * (plan.intro_end / IDEAL_INTRO))
    if t < plan.intro_title_from + fade_in:
        return ease("out_cubic", (t - plan.intro_title_from) / fade_in)
    if t <= plan.intro_title_until:
        return 1.0
    return 1.0 - ease("in_out_sine", (t - plan.intro_title_until) / 0.5)


def tagline_opacity(plan: Plan, t: float) -> float:
    """How visible the tagline is during the close."""
    if not plan.has_outro or t < plan.outro_hold_from:
        return 0.0
    fade = 0.6
    fade_out_from = plan.duration - OUTRO_FADE
    if t < plan.outro_hold_from + fade:
        return ease("out_cubic", (t - plan.outro_hold_from) / fade)
    if t >= fade_out_from:
        return max(0.0, 1.0 - ease("in_out_sine",
                                   (t - fade_out_from) / OUTRO_FADE))
    return 1.0


def title_text(song_title: str) -> str:
    return song_title.strip() or CHURCH_NAME


def closing_text() -> str:
    """Nothing. The closing card is the mark alone.

    The tagline used to be set underneath it, which put "Living Life Together
    in Christ" on screen twice: the mark already carries it, and on the navy
    panel the two sat inches apart reading the same words. The mark says it
    once, properly, in the church's own lettering.
    """
    return ""
