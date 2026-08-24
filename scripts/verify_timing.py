#!/usr/bin/env python3
"""Prove the finished video cues at the same moments as the source.

This is the one thing that cannot be wrong. A singer watching for their line
mid-service has no way to recover from a cue that arrives late, so the claim
"the timing is right" has to be a measurement, not an assertion.

Two errors are measured separately, because they have different causes and
different fixes:

  extraction   the times written into the .lyr file, against a fresh reading
               of the SOURCE video. Catches a sweep that located a boundary
               imprecisely.
  render       when text actually becomes readable in the OUTPUT video,
               against those same times. Catches an entrance animation that
               makes a correctly-timed line land late anyway.

Total drift a singer would feel is the sum. Both are reported in milliseconds
against the frame duration of the source, which is the floor: you cannot
resolve a cue more precisely than one frame of the video it came from.

    python scripts/verify_timing.py SOURCE.mp4 lyrics.lyr [OUTPUT.mp4]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.lyrics import LyricTrack  # noqa: E402
from engine.ocr import native_fps, probe_size, text_mask  # noqa: E402

#: Fraction of settled ink that counts as "readable". Deliberately the same
#: perceptual definition used when extracting, so the two are comparable.
READABLE = 0.5

#: What a congregation would notice. Chosen against the ~50-80 ms that
#: research on musical synchrony puts at the edge of perception; a cue inside
#: this is indistinguishable from the source.
GOOD_MS = 60.0
ACCEPTABLE_MS = 120.0


def read_window(video: Path, start: float, duration: float, size: tuple):
    """Decode a window at native rate, yielding (index, rgb frame)."""
    w, h = size
    frame_bytes = w * h * 3
    proc = subprocess.Popen(
        [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{max(0.0, start - 2.0):.3f}", "-i", str(video),
         "-ss", f"{min(2.0, start):.3f}", "-t", f"{duration:.3f}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=frame_bytes * 4)
    try:
        n = 0
        while True:
            raw = proc.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                break
            yield n, np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
            n += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def centre_band(mask: np.ndarray) -> np.ndarray:
    """The middle of the frame, excluding the corners a logo sits in.

    Measuring the whole frame would count the watermark, which is present in
    every frame and would swamp the signal the text produces.
    """
    h, w = mask.shape
    return mask[int(h * 0.12):int(h * 0.80), int(w * 0.06):int(w * 0.94)]


def settled_mask(video: Path, at: float, size: tuple) -> np.ndarray | None:
    """What the centre of frame looks like once a card has fully arrived."""
    for _, frame in read_window(video, at, 0.05, size):
        return centre_band(text_mask(frame))
    return None


def measure_onset(video: Path, around: float, hold_until: float, size: tuple,
                  fps: float, lookback: float = 1.1,
                  lookahead: float = 1.1) -> float | None:
    """When the card that is settled mid-hold first becomes readable.

    Measuring the raw amount of ink does NOT work, and failing to notice that
    is how this check first reported 900 ms errors that were not there. Lyric
    cards frequently butt directly against one another, so the ink level never
    dips between them — there is no rise to find, and a threshold crossing
    lands wherever the measurement window happens to begin.

    So the reference is the card's own settled appearance, sampled from inside
    its hold where it is unambiguous. Each frame is then scored on how much of
    that settled shape is present, and the onset is where the score crosses
    half. An outgoing card scores near zero against the incoming one's shape,
    which is what makes back-to-back transitions resolvable at all.

    Returns None rather than a number when the answer lands against a window
    edge, since that means the transition was not actually captured.
    """
    # Sample well inside the hold, but not so late the card has begun leaving.
    sample_at = min(around + 0.5, max(around + 0.15, (around + hold_until) / 2))
    reference = settled_mask(video, sample_at, size)
    if reference is None or reference.sum() < 40:
        return None

    window_start = max(0.0, around - lookback)
    ref_total = float(reference.sum())
    series = []
    for n, frame in read_window(video, window_start, lookback + lookahead, size):
        mask = centre_band(text_mask(frame))
        if mask.shape != reference.shape:
            continue
        series.append((n, float((mask & reference).sum()) / ref_total))
    if len(series) < 5:
        return None

    values = np.array([v for _, v in series])
    ceiling = float(values.max())
    if ceiling < 0.25:
        return None

    threshold = ceiling * READABLE
    for i in range(len(values) - 1):
        if values[i] >= threshold and values[i + 1] >= threshold:
            # A crossing on the very first frame means the card was already up
            # when the window opened — the real onset is outside it.
            if i == 0:
                return None
            return window_start + series[i][0] / fps
    return None


def summarise(name: str, deltas: list, frame_ms: float) -> dict:
    if not deltas:
        return {"name": name, "n": 0}
    arr = np.array(deltas)
    absolute = np.abs(arr)
    return {
        "name": name,
        "n": len(arr),
        "mean": float(arr.mean()),
        "mean_abs": float(absolute.mean()),
        "median_abs": float(np.median(absolute)),
        "p95": float(np.percentile(absolute, 95)),
        "worst": float(absolute.max()),
        "within_frame": float((absolute <= frame_ms).mean() * 100),
        "within_good": float((absolute <= GOOD_MS).mean() * 100),
    }


def report(stats: dict, frame_ms: float) -> None:
    if not stats["n"]:
        print(f"  {stats['name']}: no measurable cues")
        return
    print(f"  {stats['name']}  ({stats['n']} cues)")
    print(f"     mean signed   {stats['mean']:+7.1f} ms   "
          f"(negative = early, positive = late)")
    print(f"     mean absolute {stats['mean_abs']:7.1f} ms")
    print(f"     median        {stats['median_abs']:7.1f} ms")
    print(f"     95th pct      {stats['p95']:7.1f} ms")
    print(f"     worst         {stats['worst']:7.1f} ms")
    print(f"     within one source frame ({frame_ms:.0f} ms): "
          f"{stats['within_frame']:.0f}%")
    print(f"     within {GOOD_MS:.0f} ms (imperceptible):      "
          f"{stats['within_good']:.0f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path, help="the original lyric video")
    ap.add_argument("lyrics", type=Path, help="the .lyr or .json produced from it")
    ap.add_argument("output", type=Path, nargs="?", help="the rendered video")
    ap.add_argument("--limit", type=int, default=0,
                    help="check only the first N cues (faster)")
    args = ap.parse_args()

    track = LyricTrack.load(args.lyrics)
    lines = sorted(track.lines, key=lambda l: l.start)
    if args.limit:
        lines = lines[: args.limit]
    if not lines:
        print("no lyric lines to check")
        return 1

    src_size, src_fps = probe_size(args.source), native_fps(args.source)
    frame_ms = 1000.0 / src_fps
    print(f"\nsource : {args.source.name}  ({src_fps:.2f} fps, "
          f"one frame = {frame_ms:.1f} ms)")
    print(f"lyrics : {args.lyrics.name}  ({len(lines)} cues checked)")
    if args.output:
        print(f"output : {args.output.name}")
    print()

    extraction, render = [], []
    out_size = probe_size(args.output) if args.output else None
    out_fps = native_fps(args.output) if args.output else None

    skipped = 0
    for i, line in enumerate(lines):
        truth = measure_onset(args.source, line.start, line.end, src_size, src_fps)
        if truth is not None:
            extraction.append((line.start - truth) * 1000.0)
        else:
            skipped += 1
        if args.output:
            shown = measure_onset(args.output, line.start, line.end,
                                  out_size, out_fps)
            if shown is not None:
                render.append((shown - line.start) * 1000.0)
        print(f"\r  measuring {i + 1}/{len(lines)}…", end="", flush=True)
    print("\r" + " " * 40 + "\r", end="")
    if skipped:
        print(f"  ({skipped} cue(s) not measurable — transition fell outside "
              f"the window; not counted either way)\n")

    print("TIMING ACCURACY\n")
    ext = summarise("extraction   (.lyr vs the source video)", extraction, frame_ms)
    report(ext, frame_ms)
    combined = ext

    if args.output:
        print()
        rnd = summarise("render       (output video vs .lyr)", render, frame_ms)
        report(rnd, frame_ms)
        if ext["n"] and rnd["n"]:
            print()
            total = ext["mean_abs"] + rnd["mean_abs"]
            print(f"  END TO END: about {total:.0f} ms of average drift "
                  f"between the source and the finished video.")
            combined = {"mean_abs": total, "worst": ext["worst"] + rnd["worst"]}

    print()
    drift = combined.get("mean_abs", 999)
    if drift <= GOOD_MS:
        print(f"  PASS — average drift {drift:.0f} ms is below the {GOOD_MS:.0f} ms "
              f"threshold where a difference becomes noticeable.")
        return 0
    if drift <= ACCEPTABLE_MS:
        print(f"  MARGINAL — average drift {drift:.0f} ms. Usable, but a "
              f"careful singer may feel it. Worth tightening.")
        return 0
    print(f"  FAIL — average drift {drift:.0f} ms is too large to use in a "
          f"service. Do not ship this.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
