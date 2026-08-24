"""Procedural background plates for the theme pack.

A theme does not ship a video file. It ships a *recipe* for one or two still
"plates", generated here at higher-than-frame resolution:

    base plate     the backdrop itself
    overlay plate  an optional translucent layer (light rays, glass wedges)

render.py then animates them inside ffmpeg — a slow zoom/pan on the base and a
slow rotation on the overlay — so a five-minute song costs two PNGs instead of
9,000 rendered frames, and the motion is free.

Plates are cached in assets/backgrounds/ keyed by theme + size, so the second
render of any theme skips this module entirely.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from .brand import ASSETS, hex_to_rgb

CACHE_DIR = ASSETS / "backgrounds"

#: Plates are generated larger than the output frame so a Ken Burns drift has
#: somewhere to travel without exposing an edge.
OVERSCAN = 1.35


def plate_size(frame: tuple) -> tuple:
    return (int(frame[0] * OVERSCAN), int(frame[1] * OVERSCAN))


# --------------------------------------------------------------------------
# primitives — all return float arrays in 0..1, shape (h, w)
# --------------------------------------------------------------------------


def _coords(size: tuple):
    """Normalised x/y grids where the frame spans roughly -1..1."""
    w, h = size
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    x = (x / w - 0.5) * 2.0
    y = (y / h - 0.5) * 2.0
    return x, y


def radial(size: tuple, center=(0.0, 0.0), radius=1.0, falloff=2.0) -> np.ndarray:
    """A soft radial blob, 1.0 at `center` decaying to 0 at `radius`."""
    x, y = _coords(size)
    # Correct for aspect so the blob stays circular on a 16:9 frame.
    aspect = size[0] / size[1]
    d = np.sqrt(((x - center[0]) * aspect) ** 2 + (y - center[1]) ** 2)
    v = np.clip(1.0 - d / max(radius, 1e-6), 0.0, 1.0)
    return v**falloff


def linear(size: tuple, angle: float = 90.0) -> np.ndarray:
    """A 0..1 ramp across the frame. angle=90 runs top (0) to bottom (1)."""
    x, y = _coords(size)
    rad = math.radians(angle)
    v = x * math.cos(rad) + y * math.sin(rad)
    return np.clip((v + 1.0) / 2.0, 0.0, 1.0)


def rays(size: tuple, count: int = 14, center=(0.0, -1.1), sharpness: float = 5.0,
         seed: int = 7) -> np.ndarray:
    """Angular light streaks radiating from `center`, for god-ray effects."""
    x, y = _coords(size)
    aspect = size[0] / size[1]
    dx = (x - center[0]) * aspect
    dy = y - center[1]
    theta = np.arctan2(dy, dx)

    rng = np.random.default_rng(seed)
    # Sum a few offset sine bands so the rays are uneven rather than a fan.
    out = np.zeros_like(theta)
    for _ in range(3):
        phase = rng.uniform(0, math.tau)
        freq = count * rng.uniform(0.6, 1.4)
        out += (np.sin(theta * freq + phase) * 0.5 + 0.5) ** sharpness
    out /= 3.0

    # Fade the rays out with distance from the source.
    dist = np.sqrt(dx**2 + dy**2)
    out *= np.clip(1.0 - dist / 2.6, 0.0, 1.0)
    return np.clip(out, 0.0, 1.0)


def wedges(size: tuple, count: int = 9, center=(0.0, 0.0), seed: int = 3) -> np.ndarray:
    """Hard-edged pie wedges — the stained-glass sunburst from the logo.

    Returns an index map (0..count-1) rather than a 0..1 mask, so the caller
    can assign a different colour to each wedge.
    """
    x, y = _coords(size)
    aspect = size[0] / size[1]
    theta = np.arctan2(y - center[1], (x - center[0]) * aspect)
    idx = ((theta + math.pi) / math.tau * count).astype(int) % count
    return idx


def bokeh(size: tuple, count: int = 26, seed: int = 11,
          min_r: float = 0.02, max_r: float = 0.13) -> np.ndarray:
    """Scattered soft orbs, brighter toward the top of the frame."""
    w, h = size
    rng = np.random.default_rng(seed)
    acc = np.zeros((h, w), dtype=np.float32)
    x, y = _coords(size)
    aspect = w / h
    for _ in range(count):
        cx = rng.uniform(-1.15, 1.15)
        cy = rng.uniform(-1.15, 1.15)
        r = rng.uniform(min_r, max_r)
        strength = rng.uniform(0.25, 1.0) * (1.0 - (cy + 1) / 2 * 0.55)
        d = np.sqrt(((x - cx) * aspect) ** 2 + (y - cy) ** 2)
        orb = np.clip(1.0 - d / r, 0.0, 1.0)
        # Squared falloff reads as a defocused highlight rather than a dot.
        acc += (orb**2) * strength
    return np.clip(acc, 0.0, 1.0)


def grain(size: tuple, amount: float = 0.015, seed: int = 5) -> np.ndarray:
    """Fine monochrome noise. Kills banding in the big smooth gradients."""
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, amount, (size[1], size[0])).astype(np.float32)


def vignette(size: tuple, strength: float = 0.45, radius: float = 1.35) -> np.ndarray:
    """A 0..1 multiplier that darkens the frame's corners."""
    return 1.0 - strength * (1.0 - radial(size, radius=radius, falloff=1.4))


# --------------------------------------------------------------------------
# composition helpers
# --------------------------------------------------------------------------


def solid(size: tuple, color: str) -> np.ndarray:
    """An (h, w, 3) float array of one colour, 0..1."""
    rgb = np.array(hex_to_rgb(color), dtype=np.float32) / 255.0
    return np.ones((size[1], size[0], 3), dtype=np.float32) * rgb


def ramp(size: tuple, stops: tuple, angle: float = 90.0) -> np.ndarray:
    """An (h, w, 3) gradient blending through `stops` across the frame."""
    t = linear(size, angle)
    cols = np.array([hex_to_rgb(c) for c in stops], dtype=np.float32) / 255.0
    pos = t * (len(cols) - 1)
    lo = np.floor(pos).astype(int)
    hi = np.clip(lo + 1, 0, len(cols) - 1)
    f = (pos - lo)[..., None]
    return cols[lo] * (1 - f) + cols[hi] * f


def screen(base: np.ndarray, layer: np.ndarray) -> np.ndarray:
    """Screen blend — brightens without clipping, right for light effects."""
    return 1.0 - (1.0 - base) * (1.0 - layer)


def tint(mask: np.ndarray, color: str, strength: float = 1.0) -> np.ndarray:
    """Turn a 0..1 mask into an (h, w, 3) coloured layer."""
    rgb = np.array(hex_to_rgb(color), dtype=np.float32) / 255.0
    return mask[..., None] * rgb * strength


def to_image(arr: np.ndarray) -> Image.Image:
    """Clamp a float (h, w, 3) array to an 8-bit RGB image."""
    return Image.fromarray((np.clip(arr, 0.0, 1.0) * 255).astype("uint8"), "RGB")


def blur(img: Image.Image, radius: float) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(radius))


def cached_plate(theme_key: str, kind: str, frame: tuple, build) -> Path:
    """Return the path to a plate, generating it on first use.

    `build` is a zero-arg callable returning a PIL Image.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{theme_key}-{kind}-{frame[0]}x{frame[1]}.png"
    if not path.is_file():
        build().save(path, "PNG")
    return path
