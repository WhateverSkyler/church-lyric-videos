"""Finds ffmpeg, ffprobe, yt-dlp and tesseract without depending on PATH.

The render machine is also the church's livestream machine, and it already has
its own copies of these tools in a `tools\\` folder beside the streaming setup.
Adding them to the system PATH to suit this program would be an unnecessary
change to a machine that has to work on Sunday morning, and could shadow a
different version something else there depends on.

So each binary is resolved in this order:

    1. an explicit environment variable  (HOPEWELL_FFMPEG, ...)
    2. worker/.env, so the installer can record what it found
    3. PATH, which is the normal case on a development machine
    4. a short list of the usual places, including the sibling tools\\ folder

PATH is especially unreliable on the render machine: the worker runs there as
a SYSTEM scheduled task, which inherits none of a user's PATH, so a perfectly
good installation is invisible to shutil.which() alone.

Resolution is cached, because these are looked up on nearly every subprocess.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: tool name -> (environment variable, extra places worth looking)
_SPEC = {
    "ffmpeg": ("HOPEWELL_FFMPEG", (
        "tools/ffmpeg.exe", "tools/ffmpeg",
        "C:/ffmpeg/bin/ffmpeg.exe",
        "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg",
    )),
    "ffprobe": ("HOPEWELL_FFPROBE", (
        "tools/ffprobe.exe", "tools/ffprobe",
        "C:/ffmpeg/bin/ffprobe.exe",
        "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe",
    )),
    "yt-dlp": ("HOPEWELL_YTDLP", (
        "tools/yt-dlp.exe", "tools/yt-dlp",
        "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp",
    )),
    "deno": ("HOPEWELL_DENO", (
        "tools/deno.exe",
        "C:/Program Files/deno/deno.exe",
        "~/.deno/bin/deno.exe", "~/.deno/bin/deno",
        "/opt/homebrew/bin/deno", "/usr/local/bin/deno",
    )),
    "tesseract": ("HOPEWELL_TESSERACT", (
        "tools/tesseract.exe",
        "C:/Program Files/Tesseract-OCR/tesseract.exe",
        "C:/Program Files (x86)/Tesseract-OCR/tesseract.exe",
        "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
        "/usr/bin/tesseract",
    )),
}

_cache: dict = {}
_env_file_loaded = False


def _load_env_file() -> None:
    """Pick up tool paths the installer recorded, without overriding the shell."""
    global _env_file_loaded
    if _env_file_loaded:
        return
    _env_file_loaded = True
    for candidate in (ROOT / "worker" / ".env", ROOT / ".env"):
        if not candidate.is_file():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
        except OSError:
            pass


def find(name: str) -> str | None:
    """Absolute path to `name`, or None if it genuinely isn't installed."""
    if name in _cache:
        return _cache[name]
    _load_env_file()

    env_var, extras = _SPEC.get(name, (None, ()))

    if env_var:
        override = os.environ.get(env_var, "").strip().strip('"')
        if override and Path(override).is_file():
            _cache[name] = str(Path(override).resolve())
            return _cache[name]

    found = shutil.which(name)
    if found:
        _cache[name] = found
        return found

    for extra in extras:
        candidate = Path(extra)
        if not candidate.is_absolute() and not extra.startswith("~"):
            # Relative entries are searched beside the project AND beside the
            # worker's own folder, which is where the church PC keeps its copy.
            for base in (ROOT, ROOT.parent, Path.cwd()):
                probe = base / candidate
                if probe.is_file():
                    _cache[name] = str(probe.resolve())
                    return _cache[name]
        else:
            candidate = candidate.expanduser()
            if candidate.is_file():
                _cache[name] = str(candidate)
                return _cache[name]

    _cache[name] = None
    return None


def require(name: str) -> str:
    path = find(name)
    if not path:
        env_var = _SPEC.get(name, (None, ()))[0]
        raise RuntimeError(
            f"{name} not found. Install it, or point {env_var} at it "
            f"(it can go in worker/.env)."
        )
    return path


def ffmpeg() -> str:
    return require("ffmpeg")


def ffprobe() -> str:
    # An ffmpeg build almost always ships ffprobe beside it.
    path = find("ffprobe")
    if path:
        return path
    sibling = Path(require("ffmpeg")).with_name(
        "ffprobe.exe" if os.name == "nt" else "ffprobe")
    if sibling.is_file():
        _cache["ffprobe"] = str(sibling)
        return _cache["ffprobe"]
    return require("ffprobe")


def yt_dlp() -> str:
    return require("yt-dlp")


def tesseract() -> str | None:
    """Optional: the fallback OCR engine. None when it isn't installed."""
    return find("tesseract")


def deno() -> str | None:
    """Optional: the JavaScript runtime yt-dlp needs for YouTube.

    yt-dlp finds this on PATH, which the worker does not have - it runs as a
    SYSTEM scheduled task, and Deno installs itself onto the *user* PATH. So
    the path is resolved here and handed to yt-dlp explicitly.
    """
    return find("deno")


def report() -> dict:
    """What was found where — for the worker's startup log."""
    return {name: find(name) or "NOT FOUND" for name in _SPEC}
