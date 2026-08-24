"""How lyric type arrives, sits, and leaves.

Split into three independent stages so a theme can mix them freely:

    enter   the first `enter_dur` seconds of a line
    idle    the long middle, where the type must stay readable but not dead
    exit    the last `exit_dur` seconds

`stagger` offsets each word from the one before it, which is what makes type
feel like it is being sung rather than switched on. It is deliberately capped
against the line's own duration — a ten-word line with a 0.12 s stagger would
otherwise still be arriving when it is due to leave.

Idle motion is intentionally tiny. Anything you consciously notice while
trying to sing is too much; the point is only that the frame is never
completely static.
"""

from __future__ import annotations

from dataclasses import dataclass

from .anim import Transform, ease, mix, smoothstep

# --------------------------------------------------------------------------
# stage implementations
# --------------------------------------------------------------------------


def _enter_fade(p: float, tf: Transform, **kw) -> None:
    tf.opacity *= ease("out_cubic", p)


def _enter_rise(p: float, tf: Transform, distance: float = 46.0, **kw) -> None:
    e = ease("out_quint", p)
    tf.opacity *= ease("out_cubic", min(1.0, p * 1.35))
    tf.y += (1.0 - e) * distance


def _enter_fall(p: float, tf: Transform, distance: float = 38.0, **kw) -> None:
    e = ease("out_quint", p)
    tf.opacity *= ease("out_cubic", min(1.0, p * 1.35))
    tf.y -= (1.0 - e) * distance


def _enter_blur(p: float, tf: Transform, amount: float = 15.0, **kw) -> None:
    e = ease("out_cubic", p)
    tf.opacity *= ease("out_cubic", min(1.0, p * 1.5))
    tf.blur += (1.0 - e) * amount
    tf.scale *= mix(1.045, 1.0, e)


def _enter_zoom(p: float, tf: Transform, from_scale: float = 1.11, **kw) -> None:
    e = ease("out_expo", p)
    tf.opacity *= ease("out_cubic", min(1.0, p * 1.4))
    tf.scale *= mix(from_scale, 1.0, e)


def _enter_lift(p: float, tf: Transform, distance: float = 34.0,
                amount: float = 10.0, **kw) -> None:
    """Rise + defocus together. The richest entrance; used by the hero themes."""
    e = ease("out_quint", p)
    tf.opacity *= ease("out_cubic", min(1.0, p * 1.3))
    tf.y += (1.0 - e) * distance
    tf.blur += (1.0 - ease("out_cubic", p)) * amount
    tf.scale *= mix(1.03, 1.0, e)
    # A brief bloom as the word lands, so entrances read as lit rather than
    # merely faded in.
    tf.glow += (1.0 - smoothstep(0.35, 1.0, p)) * 0.30


ENTERS = {
    "fade": _enter_fade,
    "rise": _enter_rise,
    "fall": _enter_fall,
    "blur": _enter_blur,
    "zoom": _enter_zoom,
    "lift": _enter_lift,
}


def _exit_fade(p: float, tf: Transform, **kw) -> None:
    tf.opacity *= 1.0 - ease("in_out_sine", p)


def _exit_sink(p: float, tf: Transform, distance: float = 26.0, **kw) -> None:
    tf.opacity *= 1.0 - ease("in_out_sine", p)
    tf.y -= ease("in_out_cubic", p) * distance


def _exit_blur(p: float, tf: Transform, amount: float = 13.0, **kw) -> None:
    tf.opacity *= 1.0 - ease("in_out_sine", p)
    tf.blur += ease("in_out_cubic", p) * amount
    tf.scale *= mix(1.0, 1.03, ease("in_out_cubic", p))


EXITS = {
    "fade": _exit_fade,
    "sink": _exit_sink,
    "blur": _exit_blur,
}


def _idle_still(t: float, tf: Transform, **kw) -> None:
    return


def _idle_drift(t: float, tf: Transform, speed: float = 2.4, **kw) -> None:
    """A slow upward crawl. Keeps the frame alive without pulling the eye."""
    tf.y -= t * speed


def _idle_breathe(t: float, tf: Transform, amount: float = 0.009,
                  period: float = 7.5, **kw) -> None:
    import math

    tf.scale *= 1.0 + amount * math.sin(t / period * math.tau)


def _idle_float(t: float, tf: Transform, amount: float = 3.0,
                period: float = 6.0, index: int = 0, **kw) -> None:
    """Per-word bob, phase-offset so words don't move in lockstep."""
    import math

    tf.y += amount * math.sin((t / period + index * 0.17) * math.tau)


IDLES = {
    "still": _idle_still,
    "drift": _idle_drift,
    "breathe": _idle_breathe,
    "float": _idle_float,
}


# --------------------------------------------------------------------------
# the spec a theme carries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TextAnimation:
    enter: str = "lift"
    exit: str = "fade"
    idle: str = "breathe"
    enter_dur: float = 0.62
    exit_dur: float = 0.44
    #: Seconds between consecutive words entering.
    stagger: float = 0.075
    #: Multiplies the stagger for the exit, so lines leave together-ish even
    #: when they arrived one word at a time.
    exit_stagger_scale: float = 0.35
    params: tuple = ()

    def _kw(self) -> dict:
        return dict(self.params)

    def transform_for(self, index: int, count: int, t_local: float,
                      duration: float) -> Transform:
        """The Transform for word `index` at `t_local` seconds into its line."""
        tf = Transform()
        kw = self._kw()
        kw["index"] = index

        # Cap total stagger so the last word still has time to fully arrive
        # before the line starts leaving.
        usable = max(0.05, duration - self.enter_dur - self.exit_dur)
        stagger = self.stagger
        if count > 1:
            stagger = min(stagger, usable / (count - 1))
        stagger = max(0.0, stagger)

        enter_at = index * stagger
        exit_at = duration - self.exit_dur - index * stagger * self.exit_stagger_scale

        # --- enter ---------------------------------------------------------
        if t_local < enter_at:
            tf.opacity = 0.0
            return tf
        p_in = min(1.0, (t_local - enter_at) / max(1e-3, self.enter_dur))
        ENTERS.get(self.enter, _enter_fade)(p_in, tf, **kw)

        # --- idle ----------------------------------------------------------
        IDLES.get(self.idle, _idle_still)(max(0.0, t_local - enter_at), tf, **kw)

        # --- exit ----------------------------------------------------------
        if t_local > exit_at:
            p_out = min(1.0, (t_local - exit_at) / max(1e-3, self.exit_dur))
            EXITS.get(self.exit, _exit_fade)(p_out, tf, **kw)

        return tf

    def total_lead_in(self, count: int, duration: float) -> float:
        """How long after a line starts before every word has arrived."""
        usable = max(0.05, duration - self.enter_dur - self.exit_dur)
        stagger = min(self.stagger, usable / max(1, count - 1)) if count > 1 else 0.0
        return stagger * max(0, count - 1) + self.enter_dur


#: Ready-made combinations themes can point at.
PRESETS = {
    "gentle":    TextAnimation("fade", "fade", "breathe", 0.70, 0.50, 0.045),
    "lift":      TextAnimation("lift", "fade", "breathe", 0.62, 0.44, 0.075),
    "rise":      TextAnimation("rise", "sink", "drift", 0.58, 0.42, 0.065),
    "focus":     TextAnimation("blur", "blur", "breathe", 0.66, 0.46, 0.055),
    "reveal":    TextAnimation("zoom", "fade", "still", 0.54, 0.40, 0.090),
    "hymn":      TextAnimation("fade", "fade", "float", 0.85, 0.60, 0.030),
}


def get(key: str) -> TextAnimation:
    return PRESETS.get(key, PRESETS["lift"])
