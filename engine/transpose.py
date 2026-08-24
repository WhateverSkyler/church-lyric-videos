"""Shift a track's key without shifting its timing.

The praise team regularly needs a song a step or two up or down to suit whoever
is leading that week. Doing that in the render is convenient — but it is also
the single most dangerous thing in this program, because the obvious ways to
change pitch also change duration, and a track that finishes even slightly
early or late has every lyric cue drifting against it.

So the rule here is absolute: pitch may change, length may not. Every shifted
track is measured afterwards and rejected if its duration moved more than a
few milliseconds. A failed transpose is a nuisance; a silently stretched one
would put the words out of step with the music in front of the congregation.

Two ways to do the shift, in order of preference:

  rubberband  a real phase-vocoder. Preserves duration by construction and
              sounds markedly better on sustained material, which is most of
              what a worship instrumental is.
  asetrate    resample to change pitch, then atempo to put the speed back.
              Universally available but only approximately length-preserving,
              which is exactly the failure this module exists to catch.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import tools

#: Practical limit. Beyond about a fourth in either direction the artefacts
#: are audible enough that re-recording the track is the better answer.
MIN_SEMITONES = -7
MAX_SEMITONES = 7

#: How far a shifted track's duration may drift from the original before it is
#: rejected. 15 ms is below the threshold of noticing and far below the point
#: where accumulated drift would move a lyric cue.
MAX_DRIFT_SECONDS = 0.015


class TransposeError(RuntimeError):
    pass


def semitone_ratio(semitones: float) -> float:
    """Frequency multiplier for a shift of `semitones`."""
    return 2.0 ** (semitones / 12.0)


def label(semitones: int) -> str:
    """How the shift is written in a filename: '+2', '-1', '' for none."""
    if not semitones:
        return ""
    return f"{semitones:+d}"


def describe(semitones: int) -> str:
    if not semitones:
        return "original key"
    direction = "up" if semitones > 0 else "down"
    count = abs(semitones)
    unit = "semitone" if count == 1 else "semitones"
    return f"{direction} {count} {unit}"


def _ffmpeg() -> str:
    try:
        return tools.ffmpeg()
    except RuntimeError as exc:
        raise TransposeError(str(exc)) from exc


def have_rubberband() -> bool:
    out = subprocess.run([_ffmpeg(), "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return any(line.split()[1:2] == ["rubberband"]
               for line in out.splitlines() if line.strip())


def duration_of(path: Path) -> float:
    exe = tools.ffprobe()
    out = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError as exc:
        raise TransposeError(f"could not read duration of {path}") from exc


def pcm_duration(path: Path, rate: int = 48000) -> float:
    """Length of the actual decoded audio, ignoring container padding.

    Container duration is the wrong thing to check a transpose against. AAC
    codes in frames of 1024 samples and pads the last one, so a perfectly
    rate-accurate result still reports up to ~23 ms long at 44.1 kHz. That
    padding is silence at the very end; it cannot move a lyric cue.

    What WOULD move every cue is a rate error, and a rate error shows up in
    the decoded sample count. So the safety check measures samples, not
    metadata, and can then afford a tight tolerance.
    """
    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True)
    if proc.returncode != 0:
        raise TransposeError(f"could not decode {path}")
    return len(proc.stdout) / 2.0 / rate


def source_rate(path: Path) -> int:
    """The file's own sample rate.

    asetrate works by reinterpreting existing samples at a new rate, so the
    speed change it produces is new_rate / SOURCE_rate. Feeding it a rate
    derived from anything but the source's own is a silent tempo error — an
    early version assumed 48 kHz against 44.1 kHz material and every transpose
    came out 2.75 s short on a 34 s track, regardless of the interval.
    """
    exe = tools.ffprobe()
    out = subprocess.run(
        [exe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return int(out.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 44100


def _atempo_chain(factor: float) -> str:
    """atempo only accepts 0.5-2.0 per instance, so large shifts chain it."""
    if factor <= 0:
        raise TransposeError("invalid tempo factor")
    stages = []
    remaining = factor
    while remaining < 0.5 or remaining > 2.0:
        step = 0.5 if remaining < 0.5 else 2.0
        stages.append(step)
        remaining /= step
    stages.append(remaining)
    return ",".join(f"atempo={s:.10f}" for s in stages)


@dataclass
class Result:
    path: Path
    semitones: int
    method: str
    original_duration: float
    new_duration: float

    @property
    def drift(self) -> float:
        return self.new_duration - self.original_duration


def transpose(src: Path, dest: Path, semitones: int,
              sample_rate: int | None = None) -> Result:
    """Shift `src` by `semitones` into `dest`, preserving its exact length."""
    if semitones == 0:
        raise ValueError("transpose(0) — copy the file instead")
    if not MIN_SEMITONES <= semitones <= MAX_SEMITONES:
        raise TransposeError(
            f"{semitones:+d} is outside the usable range "
            f"{MIN_SEMITONES:+d}..{MAX_SEMITONES:+d}")

    original = pcm_duration(src)
    rate = source_rate(src)
    out_rate = sample_rate or rate
    ratio = semitone_ratio(semitones)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if have_rubberband():
        method = "rubberband"
        # pitch alone; tempo is untouched, so duration is preserved by design.
        chain = f"rubberband=pitch={ratio:.10f}:pitchq=quality"
    else:
        method = "asetrate+atempo"
        # asetrate's speed change is new_rate / SOURCE rate, so the new rate
        # must be derived from the source's own — not from the output rate.
        chain = (f"asetrate={rate * ratio:.6f},"
                 f"aresample={out_rate},"
                 f"{_atempo_chain(1.0 / ratio)}")

    # Pad-then-trim to the original length exactly. The rate maths above is
    # already sample-accurate, but AAC codes in 1024-sample frames and pads
    # the last one, so some shifts land a single frame (23 ms at 44.1 kHz)
    # long purely as a container artefact. Rather than tolerate a window and
    # hope it stays small, the length is simply made identical.
    chain += f",apad,atrim=0:{original:.6f},asetpts=N/SR/TB"

    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-vn",
         "-af", chain,
         "-ar", str(out_rate),
         "-c:a", "aac", "-b:a", "256k",
         str(dest)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise TransposeError(
            f"transpose failed ({method}):\n"
            + "\n".join(proc.stderr.strip().splitlines()[-12:]))

    shifted = pcm_duration(dest)
    drift = shifted - original
    if abs(drift) > MAX_DRIFT_SECONDS:
        dest.unlink(missing_ok=True)
        raise TransposeError(
            f"transposing {describe(semitones)} changed the track length by "
            f"{drift * 1000:+.0f} ms ({method}). Every lyric cue would drift "
            f"against the music, so this track was discarded rather than "
            f"shipped. Install ffmpeg with rubberband support for accurate "
            f"pitch shifting."
        )

    return Result(dest, semitones, method, original, shifted)


def apply_to_title(title: str, semitones: int) -> str:
    """'Song Name' + 2 -> 'Song Name (+2)'."""
    mark = label(semitones)
    return f"{title} ({mark})" if mark else title
