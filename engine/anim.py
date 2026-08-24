"""Animation primitives: easing, keyframed tracks, and sprite compositing.

This is the layer that replaced the original "one flat PNG per line, fade it
in, fade it out" approach. That reads as karaoke. Real lyric videos move —
words arrive one after another, the type drifts while it sits there, light
shifts behind it.

The cost model that makes per-frame animation affordable:

  * a word is rasterised ONCE, at full quality, into an RGBA sprite
  * every frame after that only does cheap work on it — translate, scale,
    fade, occasionally blur

So a five-minute song rasterises a few hundred sprites, not 9,000 frames of
text layout.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --------------------------------------------------------------------------
# easing
# --------------------------------------------------------------------------


def linear(t: float) -> float:
    return t


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_out_quint(t: float) -> float:
    return 1.0 - (1.0 - t) ** 5


def ease_out_expo(t: float) -> float:
    return 1.0 if t >= 1.0 else 1.0 - 2 ** (-10 * t)


def ease_in_out_cubic(t: float) -> float:
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_in_out_sine(t: float) -> float:
    return -(math.cos(math.pi * t) - 1) / 2


def ease_out_back(t: float, overshoot: float = 1.24) -> float:
    """Overshoots slightly then settles. Good for type that should feel alive."""
    c3 = overshoot + 1
    return 1 + c3 * (t - 1) ** 3 + overshoot * (t - 1) ** 2


def spring(t: float, freq: float = 3.2, damp: float = 5.2) -> float:
    """A damped oscillation settling on 1.0."""
    if t >= 1.0:
        return 1.0
    return 1.0 - math.exp(-damp * t) * math.cos(freq * math.pi * t)


EASINGS = {
    "linear": linear,
    "out_cubic": ease_out_cubic,
    "out_quint": ease_out_quint,
    "out_expo": ease_out_expo,
    "in_out_cubic": ease_in_out_cubic,
    "in_out_sine": ease_in_out_sine,
    "out_back": ease_out_back,
    "spring": spring,
}


def ease(name: str, t: float) -> float:
    return EASINGS.get(name, ease_out_cubic)(max(0.0, min(1.0, t)))


def mix(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 == edge0:
        return 0.0 if x < edge0 else 1.0
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


# --------------------------------------------------------------------------
# the state one sprite is in on one frame
# --------------------------------------------------------------------------


@dataclass
class Transform:
    """What to do to a sprite this frame."""

    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    opacity: float = 1.0
    #: Gaussian blur radius in px. 0 skips the blur entirely.
    blur: float = 0.0
    #: Extra additive glow strength, 0..1, layered under the sprite.
    glow: float = 0.0

    def visible(self) -> bool:
        return self.opacity > 0.004 and self.scale > 0.01


@dataclass
class Sprite:
    """A pre-rasterised RGBA image plus where it sits at rest."""

    rgba: np.ndarray            # (h, w, 4) uint8
    x: int = 0                  # resting top-left in frame coords
    y: int = 0
    #: Anything the animation presets want — word index, line index, width.
    meta: dict = field(default_factory=dict)

    @property
    def size(self) -> tuple:
        return (self.rgba.shape[1], self.rgba.shape[0])


# --------------------------------------------------------------------------
# compositing
# --------------------------------------------------------------------------


def alpha_over(dst: np.ndarray, src_rgb: np.ndarray, src_a: np.ndarray,
               x: int, y: int) -> None:
    """Composite `src` onto `dst` in place at (x, y), clipping at the edges.

    dst is (H, W, 3) float32 in 0..1. src_rgb matches; src_a is (h, w) 0..1.
    """
    H, W = dst.shape[:2]
    h, w = src_a.shape[:2]

    # Clip the source rect against the frame.
    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    cw = min(w - sx0, W - dx0)
    ch = min(h - sy0, H - dy0)
    if cw <= 0 or ch <= 0:
        return

    a = src_a[sy0:sy0 + ch, sx0:sx0 + cw][..., None]
    s = src_rgb[sy0:sy0 + ch, sx0:sx0 + cw]
    d = dst[dy0:dy0 + ch, dx0:dx0 + cw]
    d *= (1.0 - a)
    d += s * a


def add_light(dst: np.ndarray, src_rgb: np.ndarray, src_a: np.ndarray,
              x: int, y: int, strength: float) -> None:
    """Screen-blend a sprite's own colour as light. Used for glow passes."""
    H, W = dst.shape[:2]
    h, w = src_a.shape[:2]
    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    cw = min(w - sx0, W - dx0)
    ch = min(h - sy0, H - dy0)
    if cw <= 0 or ch <= 0 or strength <= 0:
        return

    a = src_a[sy0:sy0 + ch, sx0:sx0 + cw][..., None] * strength
    s = src_rgb[sy0:sy0 + ch, sx0:sx0 + cw]
    d = dst[dy0:dy0 + ch, dx0:dx0 + cw]
    # screen: 1-(1-d)(1-s*a)
    np.subtract(1.0, (1.0 - d) * (1.0 - s * a), out=d)


def draw_sprite(dst: np.ndarray, sprite: Sprite, tf: Transform,
                cache: dict | None = None) -> None:
    """Apply `tf` to `sprite` and composite it into the float frame `dst`."""
    if not tf.visible():
        return

    from PIL import Image, ImageFilter

    rgba = sprite.rgba
    w, h = sprite.size

    # --- scale + blur, both cached: sprites repeat these values across frames
    key = None
    if cache is not None:
        key = (id(sprite), round(tf.scale, 3), round(tf.blur, 2))
        cached = cache.get(key)
    else:
        cached = None

    if cached is None:
        img = Image.fromarray(rgba, "RGBA")
        if abs(tf.scale - 1.0) > 1e-3:
            nw = max(1, int(round(w * tf.scale)))
            nh = max(1, int(round(h * tf.scale)))
            img = img.resize((nw, nh), Image.LANCZOS)
        if tf.blur > 0.25:
            # Pad so the blur has room to bleed past the sprite's own bounds.
            pad = int(tf.blur * 2.5) + 2
            padded = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2),
                               (0, 0, 0, 0))
            padded.paste(img, (pad, pad))
            img = padded.filter(ImageFilter.GaussianBlur(tf.blur))
        arr = np.asarray(img, dtype=np.float32) / 255.0
        cached = (arr[..., :3], arr[..., 3], img.width, img.height)
        if cache is not None:
            cache[key] = cached

    rgb, alpha, cw, ch = cached

    # Keep the sprite centred on its resting box as it scales/blurs.
    ox = sprite.x + (w - cw) * 0.5 + tf.x
    oy = sprite.y + (h - ch) * 0.5 + tf.y

    if tf.glow > 0.004:
        add_light(dst, rgb, alpha, int(round(ox)), int(round(oy)), tf.glow)

    a = alpha if tf.opacity >= 0.999 else alpha * tf.opacity
    alpha_over(dst, rgb, a, int(round(ox)), int(round(oy)))


def to_bytes(frame: np.ndarray) -> bytes:
    """Float 0..1 (H, W, 3) -> raw rgb24 for ffmpeg's stdin."""
    return (np.clip(frame, 0.0, 1.0) * 255.0).astype(np.uint8).tobytes()
