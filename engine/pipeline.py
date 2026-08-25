"""Turns a request into a finished video, in two deliberate halves.

    prepare()  get the audio, recover the lyrics and their timings
    render()   burn the approved lyrics onto a Hopewell theme

They are separate on purpose. Every automatic route into this program — OCR
off a source video, forced alignment against an original — gets a word or a
timing wrong occasionally, and a wrong word is far more embarrassing on a
sanctuary screen than in a text box. So prepare() stops and hands back
something a human confirms, and only then does render() run.

The dashboard shows that middle step as an edit screen. The CLI writes a .lyr
file and waits for you to save it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from . import tools
from .lyrics import LyricTrack


class Source(str, Enum):
    """Where a job's lyrics and audio come from."""

    #: A downloaded lyric video: OCR gives both words and timings.
    LYRIC_VIDEO = "lyric_video"
    #: An instrumental plus the original recording: demucs + Whisper + DTW.
    INSTRUMENTAL = "instrumental"
    #: Audio plus lyrics somebody already timed.
    TIMED_LYRICS = "timed_lyrics"


class Stage(str, Enum):
    QUEUED = "queued"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    #: Waiting on a human to check the words before anything is rendered.
    REVIEW = "review"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    """One song, from request to finished file."""

    id: str
    title: str = ""
    artist: str = ""
    source: Source = Source.LYRIC_VIDEO
    #: A URL (YouTube etc.) or a path already on disk.
    source_ref: str = ""
    #: Phase 2 only: the original recording, used purely to borrow timings.
    original_ref: str = ""
    theme: str = "cinematic-warm"
    #: Semitones to shift the key by; 0 leaves the track alone. The shift is
    #: applied to the audio only — never the tempo — so the lyric cues stay
    #: valid, and it is verified to the sample before the render begins.
    transpose: int = 0
    stage: Stage = Stage.QUEUED
    error: str = ""
    #: Set once prepare() has run; the words awaiting or past review.
    lyrics_path: str = ""
    audio_path: str = ""
    output_path: str = ""
    requested_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: Free-form notes surfaced to whoever reviews the job.
    notes: str = ""
    alignment_confidence: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.value
        d["stage"] = self.stage.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = dict(d)
        d["source"] = Source(d.get("source", Source.LYRIC_VIDEO.value))
        d["stage"] = Stage(d.get("stage", Stage.QUEUED.value))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def is_url(ref: str) -> bool:
    return ref.startswith(("http://", "https://", "www."))


def cookie_file() -> Path | None:
    """A Netscape-format cookies file for yt-dlp, if one has been provided.

    Set HOPEWELL_COOKIES, or drop the file at the project root as
    cookies.txt. Kept optional: most videos need no cookies at all, and the
    file goes stale, so it must never be required for a download to work.
    """
    import os

    explicit = os.environ.get("HOPEWELL_COOKIES", "").strip().strip('"')
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    for candidate in (Path(__file__).resolve().parent.parent / "cookies.txt",
                      Path(__file__).resolve().parent.parent / "worker" / "cookies.txt"):
        if candidate.is_file():
            return candidate
    return None


def fetch(ref: str, workdir: Path, audio_only: bool = False,
          progress=None, fetch_upload=None) -> Path:
    """Resolve a URL, an uploaded file, or a local path to a media file.

    `fetch_upload` is supplied by the worker and knows how to pull a file the
    team uploaded to the dashboard. Uploading is the dependable route: YouTube
    refuses a great deal of music to anything that is not a browser, and the
    team usually already has the file they were going to play.
    """
    workdir.mkdir(parents=True, exist_ok=True)

    if ref.startswith("upload:"):
        if fetch_upload is None:
            raise RuntimeError(
                "This song was uploaded as a file, but there is no way to "
                "collect it from here. Run it from the worker."
            )
        if progress:
            progress("fetch", 0, 1)
        return fetch_upload(ref.split(":", 1)[1], workdir)

    if not is_url(ref):
        path = Path(ref).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"source not found: {ref}")
        return path

    template = str(workdir / "%(id)s.%(ext)s")
    cmd = [tools.yt_dlp(), "--no-playlist"]

    # YouTube extraction needs a JavaScript runtime, and yt-dlp only looks for
    # one on PATH. The worker runs as a SYSTEM scheduled task with no user
    # PATH, so the location is passed explicitly or every download 403s.
    runtime = tools.deno()
    if runtime:
        cmd += ["--js-runtimes", f"deno:{runtime}"]

    # Optional, and deliberately never the first suggestion. A signed-in
    # session does defeat some 403s, but a 403 on one video while others
    # download fine is a restriction on that upload, not an auth problem -
    # and the obvious cookies to reach for on the render machine belong to
    # the church's own Google account, which owns the channel the Sunday
    # service is streamed to. Replaying those from an unattended SYSTEM
    # service to get around a licensing restriction risks the broadcast
    # channel to save one download. Use a separate throwaway profile if this
    # is ever needed at all; the right first move is another upload.
    cookies = cookie_file()
    if cookies:
        cmd += ["--cookies", str(cookies)]

    # Ask for the clients least likely to be refused before falling back to
    # the default set.
    cmd += ["--extractor-args", "youtube:player_client=default,tv,web_safari"]

    cmd += ["-o", template, "--print", "after_move:filepath"]
    if audio_only:
        cmd += ["-f", "bestaudio/best", "-x", "--audio-format", "m4a"]
    else:
        # Cap at 1080p: the source video is only ever read for its text and
        # its audio, and 4K makes the OCR sweep much slower for no gain.
        cmd += ["-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                "--merge-output-format", "mp4"]
    cmd.append(ref)

    if progress:
        progress("fetch", 0, 1)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(explain_download_failure(proc.stderr))

    for line in reversed(proc.stdout.strip().splitlines()):
        candidate = Path(line.strip())
        if candidate.is_file():
            return candidate
    files = sorted(workdir.glob("*"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise RuntimeError("yt-dlp reported success but produced no file")
    return files[-1]


#: yt-dlp's own wording, and what it actually means for whoever queued the
#: song. Without this the dashboard shows a wall of stack trace to somebody
#: who only pasted a link, and there is nothing they can do with it.
_DOWNLOAD_HINTS = (
    ("No supported JavaScript runtime",
     "The church computer is missing a piece YouTube now requires. "
     "Install Deno on it (winget install DenoLand.Deno) and try again - "
     "nothing is wrong with the link."),
    ("Sign in to confirm your age",
     "YouTube is age-restricting this video, so it can't be downloaded. "
     "Find another upload of the same song."),
    ("Private video",
     "That video is private. Find another upload of the same song."),
    ("Video unavailable",
     "That video isn't available any more. Find another upload."),
    ("members-only",
     "That video is members-only, so it can't be downloaded."),
    ("This live event",
     "That's a live stream rather than a finished video. Wait until it's "
     "posted properly, or use another upload."),
    ("HTTP Error 403",
     "YouTube refused to send this particular video. Other videos download "
     "fine, so it is this upload rather than a problem with the setup - "
     "usually a licensing or Content-ID restriction on a commercially "
     "released track. Find another upload of the same song."),
    ("HTTP Error 429",
     "YouTube is rate-limiting the church computer. Wait an hour and retry."),
    ("Requested format is not available",
     "That video has no downloadable format. Try another upload."),
)


def explain_download_failure(stderr: str) -> str:
    """A message worth showing on the dashboard, plus the raw tail."""
    text = stderr or ""
    for needle, human in _DOWNLOAD_HINTS:
        if needle.lower() in text.lower():
            return human
    tail = "\n".join(text.strip().splitlines()[-6:])
    return f"The track could not be downloaded.\n{tail}"


def extract_audio(media: Path, out: Path) -> Path:
    """Pull a clean audio track out of whatever was fetched."""
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [tools.ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-y", "-i", str(media), "-vn", "-c:a", "aac", "-b:a", "256k", str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("audio extraction failed:\n" + proc.stderr[-500:])
    return out


# --------------------------------------------------------------------------
# the two halves
# --------------------------------------------------------------------------


def prepare(job: Job, workdir: Path, progress=None,
            fetch_upload=None) -> Job:
    """Fetch the media and recover timed lyrics. Leaves the job in REVIEW."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    job.stage = Stage.FETCHING
    job.updated_at = time.time()

    try:
        if job.source == Source.LYRIC_VIDEO:
            media = fetch(job.source_ref, workdir, audio_only=False, progress=progress,
                          fetch_upload=fetch_upload)
            audio = extract_audio(media, workdir / "audio.m4a")

            job.stage = Stage.EXTRACTING
            from .ocr import extract as ocr_extract

            track = ocr_extract(media, progress=progress)
            track.title = job.title or track.title

        elif job.source == Source.INSTRUMENTAL:
            audio = fetch(job.source_ref, workdir, audio_only=True, progress=progress,
                          fetch_upload=fetch_upload)
            if not job.original_ref:
                raise ValueError(
                    "Phase 2 needs the original recording as well, to borrow "
                    "its timings from. Add original_ref, or supply timed lyrics."
                )
            original = fetch(job.original_ref, workdir / "original",
                             audio_only=True, progress=progress)

            job.stage = Stage.EXTRACTING
            from .align import align_from_original

            result = align_from_original(original, audio, progress=progress)
            track = result.track
            job.alignment_confidence = result.confidence
            if result.note:
                job.notes = result.note

        elif job.source == Source.TIMED_LYRICS:
            audio = fetch(job.source_ref, workdir, audio_only=True, progress=progress,
                          fetch_upload=fetch_upload)
            if not job.lyrics_path:
                raise ValueError("TIMED_LYRICS needs lyrics_path set.")
            track = LyricTrack.load(Path(job.lyrics_path))
        else:
            raise ValueError(f"unknown source {job.source}")

        track.title = track.title or job.title
        track.artist = track.artist or job.artist

        lyrics_file = workdir / "lyrics.lyr"
        track.to_lyr(lyrics_file)
        track.to_json(workdir / "lyrics.json")

        job.audio_path = str(audio)
        job.lyrics_path = str(lyrics_file)
        job.stage = Stage.REVIEW
        job.error = ""
    except Exception as exc:
        job.stage = Stage.FAILED
        job.error = str(exc)
    job.updated_at = time.time()
    return job


def render(job: Job, out_dir: Path, progress=None,
           use_footage: bool = True, force_software: bool = False) -> Job:
    """Render the approved lyrics. Expects the job to have passed REVIEW."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    job.stage = Stage.RENDERING
    job.updated_at = time.time()

    try:
        from .compositor import render_animated
        from .themes import get as get_theme
        from .transpose import apply_to_title, label, transpose as shift_key

        track = LyricTrack.load(Path(job.lyrics_path))
        theme = get_theme(job.theme)
        # Re-record which theme actually ran, so a "random" pick is reproducible.
        job.theme = theme.key

        audio = Path(job.audio_path)
        if job.transpose:
            # Shift the key before rendering, and let a failed length check
            # abort the whole job — shipping a track whose length moved would
            # walk every lyric cue out of step with the music.
            shifted = audio.with_name(f"{audio.stem}{label(job.transpose)}.m4a")
            result_shift = shift_key(audio, shifted, job.transpose)
            audio = result_shift.path
            job.notes = (job.notes + f"\nkey: {label(job.transpose)} "
                         f"({result_shift.method}, "
                         f"{result_shift.drift * 1000:+.0f} ms)").strip()

        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in
                       (job.title or job.id)).strip() or job.id
        safe = apply_to_title(safe, job.transpose)
        out = out_dir / f"{safe} - {theme.name}.mp4"

        result = render_animated(
            theme, track.lines, audio, out,
            title=apply_to_title(job.title or track.title, job.transpose),
            clip_seed=abs(hash(job.id)) % 997,
            use_footage=use_footage,
            force_software=force_software,
            progress=progress,
        )
        job.output_path = str(result.path)
        job.notes = (job.notes + f"\nbackground: {result.background}").strip()
        job.stage = Stage.DONE
        job.error = ""
    except Exception as exc:
        job.stage = Stage.FAILED
        job.error = str(exc)
    job.updated_at = time.time()
    return job


def run(job: Job, workdir: Path, out_dir: Path, progress=None,
        auto_approve: bool = False) -> Job:
    """prepare() then, if approved, render(). Used by the CLI's one-shot mode."""
    job = prepare(job, workdir, progress)
    if job.stage != Stage.REVIEW:
        return job
    if not auto_approve:
        return job
    return render(job, out_dir, progress)
