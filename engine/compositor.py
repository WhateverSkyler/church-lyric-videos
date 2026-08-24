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
from . import splash
from .render import FRAME, FPS, pick_encoder, probe_duration
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
    """One lyric line, rasterised and ready to animate.

    `anim_start` is normally EARLIER than the lyric's own start time. The cue
    a singer reacts to is the moment the words are readable, not the moment
    they begin fading up — so the entrance is run ahead of the cue and timed
    to settle exactly on it. Without this the line lands one whole entrance
    duration late, which is precisely the drift that makes someone come in
    behind the music.
    """

    line: LyricLine
    sprites: list
    #: id(sprite) -> {(scale, blur): (rgb, alpha, w, h)}
    cache: dict
    #: Absolute time the entrance begins.
    anim_start: float = 0.0
    #: Length of the whole animation, entrance through exit.
    anim_span: float = 1.0

    @property
    def visible_from(self) -> float:
        return self.anim_start

    @property
    def visible_until(self) -> float:
        return self.anim_start + self.anim_span


def plan_line(line: LyricLine, sprites: list, animation: TextAnimation,
              previous_end: float | None, max_overlap: float = 0.34) -> tuple:
    """Work out when a line's animation should start and how long it runs.

    The entrance is pulled earlier so the words settle on `line.start`, but
    never so far that it crowds the line before it. Where the gap is tight the
    lead-in is trimmed, which shortens the entrance rather than delaying the
    cue — being slightly less animated is always preferable to being late.
    """
    span = max(0.25, line.end - line.start)
    lead = animation.total_lead_in(len(sprites), span)

    if previous_end is not None:
        available = line.start - previous_end + max_overlap
        lead = max(0.0, min(lead, available))

    anim_start = max(0.0, line.start - lead)
    # Exit begins at line.end: entrance lead + the held portion + the exit.
    anim_span = lead + (line.end - line.start) + animation.exit_dur
    return anim_start, max(0.3, anim_span)


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


class BrandMark:
    """Draws the church mark, the opening title and the closing tagline.

    The mark is one continuous element across the whole video: it opens large
    and centred, travels to its corner, sits there through the song, and comes
    back at the end. Sizes are quantised before scaling so the travel — the
    only moment the mark changes size — reuses a few dozen cached bitmaps
    rather than resampling the logo on every frame.
    """

    #: Scaled logo widths are rounded to this many pixels before caching.
    SIZE_STEP = 6

    def __init__(self, theme: Theme, frame: tuple, duration: float,
                 first_lyric: float, last_lyric_end: float, title: str = ""):
        from PIL import Image

        from .brand import LOGO_HORIZONTAL

        self.frame = frame
        self.theme = theme
        self._cache: dict = {}

        self.source = (Image.open(LOGO_HORIZONTAL).convert("RGBA")
                       if LOGO_HORIZONTAL.is_file() else None)

        self.plan = splash.build_plan(
            duration, first_lyric, last_lyric_end,
            corner=self._corner_state(), centre_width=0.44)

        self.title_card = self._text_card(splash.title_text(title), size=0.62,
                                          y=0.655) if self.plan.has_intro else None
        self.tagline_card = self._text_card(splash.closing_text(), size=0.46,
                                            y=0.655, italic=True) \
            if self.plan.has_outro else None

    # -- geometry -------------------------------------------------------

    def _corner_state(self) -> splash.LogoState:
        """Turn the theme's anchor+margin into a centre-point fraction."""
        spec = self.theme.logo
        w, h = self.frame
        if self.source is None:
            return splash.LogoState(0.5, 0.9, spec.width, spec.opacity)
        target_w = w * spec.width
        target_h = target_w * self.source.height / self.source.width
        vertical, horizontal = spec.anchor[0], spec.anchor[1]
        cx = {"l": (w * spec.margin + target_w / 2) / w,
              "c": 0.5,
              "r": (w - w * spec.margin - target_w / 2) / w}[horizontal]
        cy = ((h * spec.margin + target_h / 2) / h if vertical == "t"
              else (h - h * spec.margin - target_h / 2) / h)
        return splash.LogoState(cx, cy, spec.width, spec.opacity)

    # -- pieces ---------------------------------------------------------

    def _text_card(self, text: str, size: float, y: float,
                   italic: bool = False):
        """Pre-render one line of splash text as a full-frame RGBA array."""
        from dataclasses import replace

        from .textcard import Shadow, render_card

        base = self.theme.text
        style = replace(
            base,
            size=int(base.size * size),
            align_y=y,
            max_width=0.80,
            glow=None,
            shadow=Shadow(opacity=0.75, blur=24, offset=(0, 6)),
            letter_spacing=base.letter_spacing + (2.0 if italic else 0.6),
        )
        arr = np.asarray(render_card(text, style, self.frame),
                         dtype=np.float32) / 255.0
        return (np.ascontiguousarray(arr[..., :3]),
                np.ascontiguousarray(arr[..., 3]))

    def _logo_at(self, width_px: int):
        """A cached RGB/alpha pair for the mark at a given pixel width."""
        key = max(self.SIZE_STEP, int(round(width_px / self.SIZE_STEP)) * self.SIZE_STEP)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        from PIL import Image, ImageFilter

        from .brand import hex_to_rgb

        spec = self.theme.logo
        height = max(1, round(self.source.height * key / self.source.width))
        mark = self.source.resize((key, height), Image.LANCZOS)

        if spec.monochrome:
            flat = Image.new("RGBA", mark.size, hex_to_rgb(spec.mono_color) + (0,))
            flat.putalpha(mark.getchannel("A"))
            mark = flat

        pad = int(spec.halo * 2.5) if spec.halo > 0 else 0
        canvas = Image.new("RGBA", (key + pad * 2, height + pad * 2), (0, 0, 0, 0))
        if pad:
            shape = Image.new("RGBA", mark.size, hex_to_rgb(spec.halo_color) + (0,))
            shape.putalpha(mark.getchannel("A"))
            glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            glow.paste(shape, (pad, pad))
            glow = glow.filter(ImageFilter.GaussianBlur(spec.halo))
            alpha = glow.getchannel("A").point(
                lambda v: min(255, int(v * spec.halo_strength)))
            glow.putalpha(alpha)
            canvas.alpha_composite(glow)
        canvas.paste(mark, (pad, pad), mark)

        arr = np.asarray(canvas, dtype=np.float32) / 255.0
        out = (np.ascontiguousarray(arr[..., :3]),
               np.ascontiguousarray(arr[..., 3]),
               canvas.width, canvas.height)
        self._cache[key] = out
        return out

    # -- per frame ------------------------------------------------------

    def draw(self, canvas: np.ndarray, t: float) -> None:
        if self.source is None:
            return

        if self.title_card is not None:
            alpha = splash.title_opacity(self.plan, t)
            if alpha > 0.004:
                blend_rgba(canvas, self.title_card[0], self.title_card[1],
                           0, 0, opacity=alpha)

        if self.tagline_card is not None:
            alpha = splash.tagline_opacity(self.plan, t)
            if alpha > 0.004:
                blend_rgba(canvas, self.tagline_card[0], self.tagline_card[1],
                           0, 0, opacity=alpha)

        state = splash.logo_state(self.plan, t)
        if state.opacity <= 0.004:
            return
        rgb, alpha, w, h = self._logo_at(int(self.frame[0] * state.width))
        x = int(round(self.frame[0] * state.cx - w / 2))
        y = int(round(self.frame[1] * state.cy - h / 2))
        blend_rgba(canvas, rgb, alpha, x, y, opacity=state.opacity)


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
    cards = sorted((l for l in lines if l.text.strip()), key=lambda l: l.start)

    prepared = []
    previous_end = None
    for i, line in enumerate(cards):
        sprites, _ = typeset(line.text, theme.text, frame)
        anim_start, anim_span = plan_line(line, sprites, animation, previous_end)
        prepared.append(PreparedLine(line, sprites,
                                     {id(s): {} for s in sprites},
                                     anim_start, anim_span))
        previous_end = line.end
        if progress:
            progress("typeset", i + 1, len(cards))

    # --- the branded open and close --------------------------------------
    brand = BrandMark(theme, frame,
                      duration=duration,
                      first_lyric=cards[0].start if cards else duration,
                      last_lyric_end=cards[-1].end if cards else 0.0,
                      title=title or "")

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
                if t < pl.visible_from or t > pl.visible_until:
                    continue
                t_local = t - pl.anim_start
                span = pl.anim_span
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

            brand.draw(canvas, t)
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
