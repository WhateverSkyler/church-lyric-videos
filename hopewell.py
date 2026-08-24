#!/usr/bin/env python3
"""Hopewell lyric videos — command line.

    # Phase 1: rebuild a downloaded lyric video in the church's own styling
    ./hopewell.py prepare "<url or file>" --title "Song Name"
    #   ...check the .lyr file it writes, fix anything OCR misread...
    ./hopewell.py render work/<job>/lyrics.lyr work/<job>/audio.m4a --theme cinematic-warm

    # Phase 2: instrumental only, timings borrowed from the original
    ./hopewell.py prepare "<instrumental>" --original "<original recording>" \
        --source instrumental --title "Song Name"

    # everything else
    ./hopewell.py themes
    ./hopewell.py preview --theme hillside --text "..."
    ./hopewell.py footage --per-mood 4
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine.lyrics import LyricTrack  # noqa: E402
from engine.pipeline import Job, Source, Stage, prepare, render  # noqa: E402
from engine.themes import THEMES, get as get_theme, listing  # noqa: E402

WORK = ROOT / "work"
OUT = ROOT / "output"


def _progress(stage: str, n: int, total: int) -> None:
    if total:
        pct = 100.0 * n / max(1, total)
        print(f"\r  {stage:<10} {n}/{total} ({pct:5.1f}%)", end="", flush=True)
        if n >= total:
            print()
    else:
        print(f"\r  {stage:<10} {n}", end="", flush=True)


# --------------------------------------------------------------------------


def cmd_themes(args) -> int:
    print(f"{'key':<16} {'mood':<11} name")
    print("-" * 72)
    for t in listing():
        print(f"{t['key']:<16} {t['mood']:<11} {t['name']}")
        print(f"{'':<28} {t['description']}")
    return 0


def cmd_preview(args) -> int:
    from engine.render import preview_still

    theme = get_theme(args.theme)
    img = preview_still(theme, args.text)
    out = Path(args.out or f"samples/preview-{theme.key}.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=92)
    print(f"{theme.name} -> {out}")
    return 0


def cmd_footage(args) -> int:
    import subprocess

    return subprocess.call([sys.executable, str(ROOT / "scripts" / "fetch_footage.py"),
                            "-n", str(args.per_mood)])


def cmd_prepare(args) -> int:
    job = Job(
        id=args.id or uuid.uuid4().hex[:10],
        title=args.title,
        artist=args.artist,
        source=Source(args.source),
        source_ref=args.source_ref,
        original_ref=args.original or "",
        theme=args.theme,
    )
    workdir = WORK / job.id
    print(f"job {job.id}  ({job.source.value})")
    job = prepare(job, workdir, _progress)

    if job.stage == Stage.FAILED:
        print(f"\nFAILED: {job.error}")
        return 1

    track = LyricTrack.load(Path(job.lyrics_path))
    suspect = sum(1 for l in track.lines if l.suspect)
    print(f"\nrecovered {len(track)} lines"
          + (f", {suspect} flagged for checking" if suspect else ""))
    if job.alignment_confidence:
        print(f"alignment confidence: {job.alignment_confidence:.2f}")
    if job.notes:
        print(f"note: {job.notes}")

    (workdir / "job.json").write_text(json.dumps(job.to_dict(), indent=2))
    print(f"\nlyrics : {job.lyrics_path}")
    print(f"audio  : {job.audio_path}")
    print("\nCheck the lyrics file, fix anything misread, then:")
    print(f"  ./hopewell.py render {job.lyrics_path} {job.audio_path} --theme {job.theme}")
    return 0


def cmd_render(args) -> int:
    lyrics = Path(args.lyrics)
    audio = Path(args.audio)
    if not lyrics.is_file():
        print(f"no such lyrics file: {lyrics}")
        return 1
    if not audio.is_file():
        print(f"no such audio file: {audio}")
        return 1

    track = LyricTrack.load(lyrics)
    job = Job(
        id=args.id or uuid.uuid4().hex[:10],
        title=args.title or track.title,
        artist=args.artist or track.artist,
        theme=args.theme,
        transpose=args.transpose,
        lyrics_path=str(lyrics),
        audio_path=str(audio),
        stage=Stage.REVIEW,
    )
    theme = get_theme(job.theme)
    print(f"rendering {len(track)} lines · theme {theme.name}"
          + ("" if not args.no_footage else " · procedural background"))

    started = time.time()
    job = render(job, Path(args.out or OUT), _progress,
                 use_footage=not args.no_footage)
    if job.stage == Stage.FAILED:
        print(f"\nFAILED: {job.error}")
        return 1
    print(f"\ndone in {time.time() - started:.0f}s -> {job.output_path}")
    return 0


def cmd_extract(args) -> int:
    """OCR only, for checking what a source video gives up."""
    from engine.ocr import extract

    track = extract(Path(args.video), skip_head=args.skip_head, progress=_progress)
    out = Path(args.out or "lyrics.lyr")
    track.to_lyr(out)
    print(f"\n{len(track)} lines -> {out}")
    return 0


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="hopewell", description="Hopewell Baptist Church lyric videos")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("themes", help="list the theme pack")
    p.set_defaults(func=cmd_themes)

    p = sub.add_parser("preview", help="render one still of a theme")
    p.add_argument("--theme", default="cinematic-warm", choices=sorted(THEMES))
    p.add_argument("--text", default="Living Life Together In Christ")
    p.add_argument("--out")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("footage", help="refresh the stock footage library")
    p.add_argument("--per-mood", type=int, default=3)
    p.set_defaults(func=cmd_footage)

    p = sub.add_parser("prepare", help="fetch a source and recover timed lyrics")
    p.add_argument("source_ref", help="URL or local file")
    p.add_argument("--source", default=Source.LYRIC_VIDEO.value,
                   choices=[s.value for s in Source])
    p.add_argument("--original", help="Phase 2: the original recording with vocals")
    p.add_argument("--title", default="")
    p.add_argument("--artist", default="")
    p.add_argument("--theme", default="cinematic-warm")
    p.add_argument("--id")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("render", help="render approved lyrics to a video")
    p.add_argument("lyrics")
    p.add_argument("audio")
    p.add_argument("--theme", default="cinematic-warm")
    p.add_argument("--title", default="")
    p.add_argument("--artist", default="")
    p.add_argument("--out")
    p.add_argument("--id")
    p.add_argument("--transpose", type=int, default=0, metavar="SEMITONES",
                   help="shift the key, e.g. 2 or -1. Tempo is never changed.")
    p.add_argument("--no-footage", action="store_true",
                   help="use the procedural backdrop instead of stock footage")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("extract", help="OCR a video's lyrics without rendering")
    p.add_argument("video")
    p.add_argument("--skip-head", type=float, default=0.0)
    p.add_argument("--out")
    p.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
