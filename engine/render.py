"""Turns a theme + timed lyrics + an audio track into a finished MP4.

The heavy lifting is one ffmpeg invocation. Backgrounds arrive as still plates
(see background.py) which ffmpeg animates with zoompan/rotate, and each lyric
line arrives as a pre-rendered transparent PNG (see textcard.py) which is faded
in and out over the top.

Each card is fed as a short `-loop 1 -t <its own duration>` input rather than a
full-length stream, so a five-minute song with sixty lines still only decodes
about four minutes of card frames in total.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import tools
from .brand import LOGO_HORIZONTAL, hex_to_rgb
from .lyrics import LyricLine
from .themes import Theme

FRAME = (1920, 1080)
FPS = 30
#: Cross-fade length, seconds, at each end of every lyric card.
FADE = 0.42


# --------------------------------------------------------------------------
# encoder selection
# --------------------------------------------------------------------------


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH. Install it and re-run.")
    return exe


def available_encoders() -> set:
    out = subprocess.run([_ffmpeg(), "-hide_banner", "-encoders"],
                         capture_output=True, text=True).stdout
    return {line.split()[1] for line in out.splitlines()
            if line.startswith(" V") and len(line.split()) > 1}


ENCODER_ARGS = {
    "h264_nvenc": ["-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"],
    "h264_videotoolbox": ["-q:v", "58", "-realtime", "0"],
    "libx264": ["-preset", "medium", "-crf", "20"],
}

#: Cache of encoder -> works, so the smoke test runs once per process.
_ENCODER_OK: dict = {}


def encoder_works(name: str) -> bool:
    """Actually encode two frames with `name` and see if it succeeds.

    Being listed by `ffmpeg -encoders` is NOT proof an encoder is usable.
    NVENC is compiled into every stock Windows build but fails at runtime when
    the driver refuses a session — the card's simultaneous-session budget is
    finite, and on a machine that is also livestreaming, OBS may already hold
    them all. The church PC failed exactly this way (return code -22) while
    still listing the encoder.
    """
    if name in _ENCODER_OK:
        return _ENCODER_OK[name]
    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.1",
         "-c:v", name, *ENCODER_ARGS.get(name, []),
         "-frames:v", "2", "-f", "null", "-"],
        capture_output=True, text=True)
    _ENCODER_OK[name] = proc.returncode == 0
    return _ENCODER_OK[name]


def pick_encoder(prefer: str | None = None, allow_hardware: bool = True) -> tuple:
    """Return (encoder_name, quality_args) for an encoder proven to work here.

    Args:
        allow_hardware: set False to force software encoding. The worker does
            this whenever the machine might be livestreaming, so a render can
            never take an NVENC session away from the service.
    """
    have = available_encoders()
    order = [prefer] if prefer else []
    if allow_hardware:
        order += ["h264_nvenc", "h264_videotoolbox"]
    order += ["libx264"]

    tried = []
    for name in order:
        if not name or name not in have:
            continue
        if name == "libx264" or encoder_works(name):
            return name, ENCODER_ARGS.get(name, ["-preset", "medium", "-crf", "20"])
        tried.append(name)

    if "libx264" in have:
        return "libx264", ENCODER_ARGS["libx264"]
    raise RuntimeError(
        f"No usable H.264 encoder. Listed: {sorted(have)}; failed smoke test: {tried}"
    )


def probe_duration(path: Path) -> float:
    """Duration of a media file in seconds."""
    exe = tools.ffprobe()
    out = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr.strip()}")
    return float(json.loads(out.stdout)["format"]["duration"])


# --------------------------------------------------------------------------
# the logo mark
# --------------------------------------------------------------------------


def logo_layer(theme: Theme, frame: tuple = FRAME) -> Image.Image:
    """A full-frame transparent layer holding just the church mark.

    The mark is carried at real strength rather than as a faint watermark.
    These videos are meant to be identifiably the church's, and a logo dimmed
    into the background reads as an accident rather than as restraint. A soft
    dark halo sits behind it so it holds its edge over pale footage without
    having to be outlined.
    """
    from PIL import ImageFilter

    spec = theme.logo
    layer = Image.new("RGBA", frame, (0, 0, 0, 0))
    if not LOGO_HORIZONTAL.is_file():
        return layer

    mark = Image.open(LOGO_HORIZONTAL).convert("RGBA")
    target_w = int(frame[0] * spec.width)
    target_h = max(1, round(mark.height * target_w / mark.width))
    mark = mark.resize((target_w, target_h), Image.LANCZOS)

    if spec.monochrome:
        flat = Image.new("RGBA", mark.size, hex_to_rgb(spec.mono_color) + (0,))
        flat.putalpha(mark.getchannel("A"))
        mark = flat

    if spec.opacity < 1.0:
        alpha = mark.getchannel("A").point(lambda v: int(v * spec.opacity))
        mark.putalpha(alpha)

    margin_x = int(frame[0] * spec.margin)
    margin_y = int(frame[1] * spec.margin)
    vertical, horizontal = spec.anchor[0], spec.anchor[1]
    x = {"l": margin_x,
         "c": (frame[0] - target_w) // 2,
         "r": frame[0] - target_w - margin_x}[horizontal]
    y = margin_y if vertical == "t" else frame[1] - target_h - margin_y

    if spec.halo > 0:
        pad = int(spec.halo * 2.5)
        glow = Image.new("RGBA", (target_w + pad * 2, target_h + pad * 2), (0, 0, 0, 0))
        shape = Image.new("RGBA", mark.size, hex_to_rgb(spec.halo_color) + (0,))
        shape.putalpha(mark.getchannel("A"))
        glow.paste(shape, (pad, pad))
        glow = glow.filter(ImageFilter.GaussianBlur(spec.halo))
        alpha = glow.getchannel("A").point(
            lambda v: min(255, int(v * spec.halo_strength)))
        glow.putalpha(alpha)
        layer.alpha_composite(glow, dest=(max(0, x - pad), max(0, y - pad)))

    layer.paste(mark, (x, y), mark)
    return layer


# --------------------------------------------------------------------------
# stills — used by the dashboard's theme picker and by `preview` on the CLI
# --------------------------------------------------------------------------


def preview_still(theme: Theme, text: str, frame: tuple = FRAME,
                  zoom: float = 1.04) -> Image.Image:
    """One composited frame, rendered entirely in PIL. No ffmpeg, no audio."""
    from .textcard import render_card

    base = Image.open(theme.base_plate(frame)).convert("RGB")
    base = _center_crop(base, frame, zoom)

    if theme.build_overlay is not None:
        over = Image.open(theme.overlay_plate(frame)).convert("RGB")
        over = _center_crop(over, frame, zoom)
        base = _blend(base, over, theme.overlay_blend, theme.overlay_opacity)

    out = base.convert("RGBA")
    out.alpha_composite(render_card(text, theme.text, frame))
    out.alpha_composite(logo_layer(theme, frame))
    return out.convert("RGB")


def _center_crop(img: Image.Image, frame: tuple, zoom: float) -> Image.Image:
    """Crop an oversized plate down to the frame at a given zoom level."""
    crop_w = int(img.width / zoom)
    crop_h = int(crop_w * frame[1] / frame[0])
    if crop_h > img.height:
        crop_h = img.height
        crop_w = int(crop_h * frame[0] / frame[1])
    left = (img.width - crop_w) // 2
    top = (img.height - crop_h) // 2
    return img.crop((left, top, left + crop_w, top + crop_h)).resize(frame, Image.LANCZOS)


def _blend(base: Image.Image, layer: Image.Image, mode: str, opacity: float) -> Image.Image:
    import numpy as np

    b = np.asarray(base, dtype=np.float32) / 255.0
    l = np.asarray(layer, dtype=np.float32) / 255.0
    mixed = 1.0 - (1.0 - b) * (1.0 - l) if mode == "screen" else l
    out = b * (1 - opacity) + mixed * opacity
    return Image.fromarray((np.clip(out, 0, 1) * 255).astype("uint8"), "RGB")


# --------------------------------------------------------------------------
# the video render
# --------------------------------------------------------------------------


@dataclass
class RenderResult:
    path: Path
    duration: float
    encoder: str
    lines: int


def _still_input(path, duration: float) -> list:
    """ffmpeg argv for holding one still image for `duration` seconds."""
    return ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}",
            "-i", str(path)]


def _bg_chain(theme: Theme, duration: float, frame: tuple) -> tuple:
    """Build the ffmpeg inputs + filter steps that produce the moving backdrop.

    Returns (inputs, filter_steps, last_label, input_count).
    """
    inputs = _still_input(theme.base_plate(frame), duration)
    count = 1
    total_frames = max(1, int(duration * FPS))
    m = theme.motion

    # zoompan walks z from zoom_from to zoom_to across the whole song, holding
    # the crop centred. 'on' is the output frame counter.
    step = (m.zoom_to - m.zoom_from) / max(1, total_frames)
    # format=gbrp everywhere: blend/overlay must run in RGB. Left to negotiate
    # on its own ffmpeg picks yuv420p, and blending 'screen' across the U/V
    # chroma planes shifts every hue (warm gold comes out magenta).
    chain = [
        f"[0:v]scale={frame[0]*2}:-2,"
        f"zoompan=z='{m.zoom_from:.5f}+{step:.8f}*on'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={frame[0]}x{frame[1]}:fps={FPS},"
        f"format=gbrp,setsar=1[bg]"
    ]
    last = "bg"

    if theme.build_overlay is not None:
        inputs += _still_input(theme.overlay_plate(frame), duration)
        count += 1
        if m.overlay_spin:
            # Rotate about the centre on an oversized canvas, then crop back so
            # the corners never sweep into frame.
            big = int(max(frame) * 1.6)
            ov = (f"[1:v]scale={big}:{big}:force_original_aspect_ratio=increase,"
                  f"crop={big}:{big},"
                  f"rotate=a='{m.overlay_spin}*PI/180*t':c=black:ow={big}:oh={big},"
                  f"crop={frame[0]}:{frame[1]}:(iw-{frame[0]})/2:(ih-{frame[1]})/2,"
                  f"format=gbrp,setsar=1[ov]")
        else:
            dx, dy = m.overlay_drift
            ov = (f"[1:v]scale={int(frame[0]*1.3)}:-2,"
                  f"crop={frame[0]}:{frame[1]}"
                  f":x='(iw-{frame[0]})/2+{dx*frame[0]:.2f}*t/60'"
                  f":y='(ih-{frame[1]})/2+{dy*frame[1]:.2f}*t/60',"
                  f"format=gbrp,setsar=1[ov]")
        chain.append(ov)
        blend = "screen" if theme.overlay_blend == "screen" else "normal"
        chain.append(
            f"[{last}][ov]blend=all_mode={blend}:all_opacity={theme.overlay_opacity},"
            f"format=gbrp[bgo]"
        )
        last = "bgo"

    return inputs, chain, last, count


def render(theme: Theme, lines: list, audio: Path, out: Path,
           frame: tuple = FRAME, encoder: str | None = None,
           title: str | None = None, progress=None) -> RenderResult:
    """Render the finished lyric video.

    Args:
        theme:   a Theme from engine.themes
        lines:   timed LyricLine objects, in order
        audio:   the instrumental track (or the source video — audio is taken from it)
        out:     destination .mp4
        title:   optional song title, shown on an opening card
    """
    from .textcard import render_card

    duration = probe_duration(audio)
    enc, quality = pick_encoder(encoder)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hopewell-render-") as tmp:
        tmpdir = Path(tmp)

        cards = []
        ordered = sorted(lines, key=lambda l: l.start)
        if title:
            # An opening title card, held until the first lyric or 6s.
            first = ordered[0].start if ordered else 6.0
            if first > 1.5:
                cards.append((0.15, max(0.9, first - 0.5), title))
        for line in ordered:
            if line.text.strip():
                cards.append((line.start, line.end, line.text))

        card_paths = []
        for i, (start, end, text) in enumerate(cards):
            path = tmpdir / f"card{i:04d}.png"
            render_card(text, theme.text, frame).save(path, "PNG")
            card_paths.append((start, end, path))
            if progress:
                progress("cards", i + 1, len(cards))

        logo_path = tmpdir / "logo.png"
        logo_layer(theme, frame).save(logo_path, "PNG")

        inputs, chain, last, idx = _bg_chain(theme, duration, frame)

        # --- the lyric cards ---------------------------------------------
        for start, end, path in card_paths:
            hold = max(0.1, end - start) + 2 * FADE
            inputs += _still_input(path, hold)
            shift = max(0.0, start - FADE)
            chain.append(
                f"[{idx}:v]format=rgba,"
                f"fade=t=in:st=0:d={FADE}:alpha=1,"
                f"fade=t=out:st={hold - FADE:.3f}:d={FADE}:alpha=1,"
                f"setpts=PTS+{shift:.3f}/TB[c{idx}]"
            )
            chain.append(
                f"[{last}][c{idx}]overlay=0:0:eof_action=pass"
                f":enable='between(t,{shift:.3f},{shift + hold:.3f})'[o{idx}]"
            )
            last = f"o{idx}"
            idx += 1

        # --- the mark, always on top --------------------------------------
        inputs += _still_input(logo_path, duration)
        chain.append(f"[{last}][{idx}:v]overlay=0:0:eof_action=pass[vout]")
        audio_idx = idx + 1

        inputs += ["-i", str(audio)]

        filtergraph = ";".join(chain)
        graph_file = tmpdir / "filtergraph.txt"
        graph_file.write_text(filtergraph)

        cmd = [_ffmpeg(), "-hide_banner", "-y", *inputs,
               "-filter_complex_script", str(graph_file),
               "-map", "[vout]", "-map", f"{audio_idx}:a",
               "-c:v", enc, *quality,
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "256k",
               "-movflags", "+faststart",
               "-t", f"{duration:.3f}",
               str(out)]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
            raise RuntimeError(f"ffmpeg render failed:\n{tail}")

    return RenderResult(path=out, duration=duration, encoder=enc, lines=len(card_paths))
