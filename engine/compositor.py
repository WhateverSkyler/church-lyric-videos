"""Per-frame renderer: real footage in, animated lyric video out.

Replaces the original ffmpeg-overlay-per-line approach, which could only fade
a flat image in and out. Here Python owns every frame, so words can arrive
individually, drift, defocus and bloom.

The pipeline is two ffmpeg processes with Python in the middle:

    ffmpeg (decode footage, loop it)  ->  stdout rawvideo
        -> Python composites word sprites and the church mark
            -> stdin rawvideo  ->  ffmpeg (encode, mux the audio)

What keeps that affordable at 1080p30:

  * frames stay uint8; only each sprite's own bounding box is promoted to
    float for the blend, so per-frame cost tracks the amount of *text*
    on screen rather than the size of the frame
  * every word is rasterised once per line, then only transformed
  * scaled/blurred variants are memoised, because a word holds the same
    transform for most of the frames it is visible
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import footage as footage_mod
from .anim import Transform
from .lyrics import LyricLine
from .render import FRAME, FPS, logo_layer, pick_encoder, probe_duration
from .textanim import TextAnimation
from .themes import Theme
from .typeset import typeset


# --------------------------------------------------------------------------
# fast localised compositing
# --------------------------------------------------------------------------


def blend_rgba(dst: np.ndarray, rgb: np.ndarray, alpha: np.ndarray,
               x: int, y: int, opacity: float = 1.0, screen: bool = False) -> None:
    """Composite a premultiplied-ready RGBA sprite into a uint8 RGB frame.

    Only the overlapping rectangle is touched, and only that rectangle is
    promoted to float — the rest of the frame is never read or written.
    """
    H, W = dst.shape[:2]
    h, w = alpha.shape[:2]

    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    cw = min(w - sx0, W - dx0)
    ch = min(h - sy0, H - dy0)
    if cw <= 0 or ch <= 0:
        return

    a = alpha[sy0:sy0 + ch, sx0:sx0 + cw]
    if opacity < 0.999:
        a = a * opacity
    a = a[..., None]
    s = rgb[sy0:sy0 + ch, sx0:sx0 + cw]

    region = dst[dy0:dy0 + ch, dx0:dx0 + cw]
    base = region.astype(np.float32) / 255.0
    if screen:
        out = 1.0 - (1.0 - base) * (1.0 - s * a)
    else:
        out = base * (1.0 - a) + s * a
    np.clip(out * 255.0, 0, 255, out=out)
    region[:] = out.astype(np.uint8)


@dataclass
class PreparedLine:
    """One lyric line, rasterised and ready to animate."""

    line: LyricLine
    sprites: list
    #: id(sprite) -> {(scale, blur): (rgb, alpha, w, h)}
    cache: dict


def _sprite_variant(sprite, tf: Transform, cache: dict):
    """Scaled/blurred float form of a sprite, memoised on (scale, blur)."""
    from PIL import Image, ImageFilter

    key = (round(tf.scale, 3), round(tf.blur, 1))
    hit = cache.get(key)
    if hit is not None:
        return hit

    img = Image.fromarray(sprite.rgba, "RGBA")
    if abs(tf.scale - 1.0) > 1e-3:
        img = img.resize((max(1, round(img.width * tf.scale)),
                          max(1, round(img.height * tf.scale))), Image.LANCZOS)
    if tf.blur > 0.25:
        pad = int(tf.blur * 2.5) + 2
        padded = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2),
                           (0, 0, 0, 0))
        padded.paste(img, (pad, pad))
        img = padded.filter(ImageFilter.GaussianBlur(tf.blur))

    arr = np.asarray(img, dtype=np.float32) / 255.0
    out = (np.ascontiguousarray(arr[..., :3]), np.ascontiguousarray(arr[..., 3]),
           img.width, img.height)
    cache[key] = out
    return out


# --------------------------------------------------------------------------
# background sources
# --------------------------------------------------------------------------


class FootageSource:
    """Streams looping, already-graded footage as raw RGB frames."""

    def __init__(self, path: Path, frame: tuple = FRAME, fps: int = FPS,
                 zoom_from: float = 1.0, zoom_to: float = 1.08,
                 duration: float = 0.0):
        self.frame = frame
        self.bytes_per_frame = frame[0] * frame[1] * 3

        total = max(1, int(duration * fps))
        step = (zoom_to - zoom_from) / total
        # A slow push keeps even a static clip from feeling like a still.
        vf = (f"scale={int(frame[0] * 1.25)}:-2,"
              f"zoompan=z='{zoom_from:.5f}+{step:.8f}*on'"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d=1:s={frame[0]}x{frame[1]}:fps={fps},"
              f"format=rgb24")

        self.proc = subprocess.Popen(
            [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
             "-stream_loop", "-1", "-i", str(path),
             "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=self.bytes_per_frame * 4,
        )
        self._last = np.zeros((frame[1], frame[0], 3), dtype=np.uint8)

    def read(self) -> np.ndarray:
        raw = self.proc.stdout.read(self.bytes_per_frame)
        if raw is None or len(raw) < self.bytes_per_frame:
            # Source ended early (shouldn't with -stream_loop -1, but a decode
            # hiccup shouldn't abort a Sunday render) — hold the last frame.
            return self._last.copy()
        self._last = np.frombuffer(raw, dtype=np.uint8).reshape(
            (self.frame[1], self.frame[0], 3))
        return self._last.copy()

    def close(self) -> None:
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class PlateSource:
    """Falls back to a theme's procedural plate when no footage is available."""

    def __init__(self, theme: Theme, frame: tuple = FRAME, fps: int = FPS,
                 duration: float = 0.0):
        from PIL import Image

        from .render import _center_crop

        self.frame = frame
        self.fps = fps
        self.duration = max(1e-3, duration)
        self.motion = theme.motion
        base = Image.open(theme.base_plate(frame)).convert("RGB")
        self.base = base
        self.overlay = (Image.open(theme.overlay_plate(frame)).convert("RGB")
                        if theme.build_overlay is not None else None)
        self.overlay_opacity = theme.overlay_opacity
        self._crop = _center_crop
        self.n = 0

    def read(self) -> np.ndarray:
        t = self.n / self.fps
        self.n += 1
        p = min(1.0, t / self.duration)
        zoom = self.motion.zoom_from + (self.motion.zoom_to - self.motion.zoom_from) * p
        img = self._crop(self.base, self.frame, zoom)
        arr = np.asarray(img, dtype=np.uint8).copy()
        if self.overlay is not None:
            ov = np.asarray(self._crop(self.overlay, self.frame, zoom),
                            dtype=np.float32) / 255.0
            base = arr.astype(np.float32) / 255.0
            mixed = 1.0 - (1.0 - base) * (1.0 - ov)
            out = base * (1 - self.overlay_opacity) + mixed * self.overlay_opacity
            arr = (np.clip(out, 0, 1) * 255).astype(np.uint8)
        return arr

    def close(self) -> None:
        return


# --------------------------------------------------------------------------
# the render
# --------------------------------------------------------------------------


@dataclass
class Result:
    path: Path
    duration: float
    encoder: str
    lines: int
    frames: int
    background: str


def render_animated(theme: Theme, lines: list, audio: Path, out: Path,
                    animation: TextAnimation | None = None,
                    frame: tuple = FRAME, fps: int = FPS,
                    encoder: str | None = None, title: str | None = None,
                    clip_seed: int = 0, use_footage: bool = True,
                    progress=None) -> Result:
    """Render a fully animated lyric video."""
    duration = probe_duration(audio)
    enc, quality = pick_encoder(encoder)
    animation = animation or theme.animation
    out.parent.mkdir(parents=True, exist_ok=True)

    # --- background -------------------------------------------------------
    source = None
    background_desc = "plate"
    if use_footage:
        clip = footage_mod.pick(theme.mood, clip_seed)
        if clip is not None:
            source = FootageSource(clip.prepared_path, frame, fps,
                                   theme.motion.zoom_from, theme.motion.zoom_to,
                                   duration)
            background_desc = f"pexels:{clip.id} ({clip.author})"
    if source is None:
        source = PlateSource(theme, frame, fps, duration)

    # --- rasterise every line --------------------------------------------
    ordered = sorted((l for l in lines if l.text.strip()), key=lambda l: l.start)
    cards = []
    if title:
        first = ordered[0].start if ordered else 6.0
        if first > 1.8:
            cards.append(LyricLine(title, 0.4, max(1.4, first - 0.55)))
    cards.extend(ordered)

    prepared = []
    for i, line in enumerate(cards):
        sprites, _ = typeset(line.text, theme.text, frame)
        prepared.append(PreparedLine(line, sprites, {id(s): {} for s in sprites}))
        if progress:
            progress("typeset", i + 1, len(cards))

    logo = logo_layer(theme, frame)
    logo_arr = np.asarray(logo, dtype=np.float32) / 255.0
    logo_rgb = np.ascontiguousarray(logo_arr[..., :3])
    logo_a = np.ascontiguousarray(logo_arr[..., 3])

    # --- encoder ----------------------------------------------------------
    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{frame[0]}x{frame[1]}", "-r", str(fps), "-i", "-",
           "-i", str(audio),
           "-map", "0:v", "-map", "1:a",
           "-c:v", enc, *quality, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "256k",
           "-movflags", "+faststart", "-shortest", str(out)]
    sink = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    total_frames = int(duration * fps)
    try:
        for n in range(total_frames):
            t = n / fps
            canvas = source.read()

            for pl in prepared:
                line = pl.line
                if t < line.start - 0.05 or t > line.end + 0.05:
                    continue
                t_local = t - line.start
                span = max(0.2, line.end - line.start)
                count = len(pl.sprites)
                for idx, sprite in enumerate(pl.sprites):
                    tf = animation.transform_for(idx, count, t_local, span)
                    if not tf.visible():
                        continue
                    rgb, alpha, cw, ch = _sprite_variant(sprite, tf, pl.cache[id(sprite)])
                    ox = int(round(sprite.x + (sprite.size[0] - cw) * 0.5 + tf.x))
                    oy = int(round(sprite.y + (sprite.size[1] - ch) * 0.5 + tf.y))
                    if tf.glow > 0.004:
                        blend_rgba(canvas, rgb, alpha, ox, oy,
                                   opacity=tf.glow, screen=True)
                    blend_rgba(canvas, rgb, alpha, ox, oy, opacity=tf.opacity)

            blend_rgba(canvas, logo_rgb, logo_a, 0, 0)
            sink.stdin.write(canvas.tobytes())

            if progress and n % (fps * 5) == 0:
                progress("render", n, total_frames)
    finally:
        source.close()
        if sink.stdin:
            sink.stdin.close()
        sink.wait()

    if sink.returncode != 0:
        err = (sink.stderr.read() or b"").decode("utf-8", "replace")
        raise RuntimeError("encode failed:\n"
                           + "\n".join(err.strip().splitlines()[-20:]))

    return Result(out, duration, enc, len(cards), total_frames, background_desc)
