"""Sources, caches and prepares stock footage for use as a lyric backdrop.

Real footage is what separates an immersive lyric video from a karaoke slide,
so themes can name a *mood* and get back a graded, correctly-sized, seamlessly
looping clip.

Everything comes from Pexels, whose licence allows free commercial use with no
attribution required. Attribution is recorded in the catalogue anyway — it
costs nothing and it means the church can always answer where a clip came from.

Backdrop footage has requirements ordinary stock browsing doesn't:

  slow        fast motion fights the words. Long clips are preferred and
              played back slowed where needed.
  quiet middle  the centre of frame is where lyrics live, so busy centres are
              graded down hard.
  dark enough   white type needs somewhere to sit. Every clip is graded before
              use rather than trusted as shot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .brand import ASSETS

LIBRARY = ASSETS / "footage"
CATALOG = LIBRARY / "catalog.json"
API_ROOT = "https://api.pexels.com/videos"

#: Target spec for a prepared backdrop. 1080p is the delivery format, and
#: downloading 4K only to scale it down wastes disk and decode time.
TARGET = (1920, 1080)
TARGET_FPS = 30


class PexelsError(RuntimeError):
    pass


def api_key() -> str:
    """Read the key from the environment or the project's .env."""
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if key:
        return key
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("PEXELS_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise PexelsError(
        "No PEXELS_API_KEY. Put it in the project's .env or export it."
    )


# --------------------------------------------------------------------------
# what to search for
# --------------------------------------------------------------------------

#: Mood -> search queries. Themes name a mood; these decide what actually gets
#: pulled. Queries are deliberately specific — "clouds" alone returns a lot of
#: fast-moving, high-contrast footage that lyrics can't sit on top of.
MOODS = {
    "warm": [
        "golden hour clouds timelapse",
        "sun rays through forest",
        "warm bokeh lights blurred",
        "sunlight dust particles",
    ],
    "clean": [
        "dark blue abstract slow",
        "deep blue water surface calm",
        "minimal blue gradient motion",
    ],
    "reverent": [
        "light through stained glass",
        "cathedral light beams",
        "abstract ink water slow",
        "smoke light beam dark",
    ],
    "reflective": [
        "sunset clouds timelapse aerial",
        "dusk sky purple orange",
        "calm ocean sunset slow",
        "night sky stars timelapse",
    ],
    "bright": [
        "morning mist field sunrise",
        "sunlight through leaves",
        "soft white clouds sky",
        "wheat field wind sunlight",
    ],
    "hopeful": [
        "aerial green field",
        "meadow grass wind slow motion",
        "mountain valley sunrise aerial",
        "forest canopy sunlight",
    ],
}


@dataclass
class Clip:
    """One catalogued piece of footage."""

    id: int
    mood: str
    query: str
    author: str
    author_url: str
    page_url: str
    duration: int
    width: int
    height: int
    #: Filename inside LIBRARY/raw, set once downloaded.
    filename: str = ""
    #: Filename inside LIBRARY/prepared, set once graded and looped.
    prepared: str = ""
    tags: list = field(default_factory=list)
    #: Direct CDN link to the chosen rendition, captured from the search
    #: response. The per-video detail endpoint 403s on this key tier, and the
    #: search payload already carries every rendition, so there is no reason
    #: to ask for it twice.
    download_url: str = ""

    @property
    def raw_path(self) -> Path:
        return LIBRARY / "raw" / self.filename if self.filename else Path()

    @property
    def prepared_path(self) -> Path:
        return LIBRARY / "prepared" / self.prepared if self.prepared else Path()


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------


def _request(path: str, params: dict) -> dict:
    url = f"{API_ROOT}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": api_key(),
        "User-Agent": "hopewell-lyric-videos/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PexelsError(f"Pexels {exc.code} for {path}: {exc.reason}") from exc


def search(query: str, mood: str = "", per_page: int = 15,
           min_duration: int = 8, min_width: int = 1920) -> list:
    """Search Pexels and return Clips worth considering as a backdrop."""
    data = _request("search", {
        "query": query,
        "per_page": per_page,
        "orientation": "landscape",
        "size": "medium",
    })
    out = []
    for v in data.get("videos", []):
        if v.get("duration", 0) < min_duration:
            continue
        if not any((f.get("width") or 0) >= min_width for f in v.get("video_files", [])):
            continue
        try:
            best = _best_file(v.get("video_files", []))
        except PexelsError:
            continue
        out.append(Clip(
            id=v["id"], mood=mood, query=query,
            author=v["user"]["name"], author_url=v["user"]["url"],
            page_url=v["url"], duration=v["duration"],
            width=best["width"], height=best["height"],
            download_url=best["link"],
            filename=f"{v['id']}-{best['width']}x{best['height']}.mp4",
        ))
    return out


def _best_file(video_files: list) -> dict:
    """Pick the smallest file that still covers 1080p.

    Downloading 4K to immediately scale to 1080 costs bandwidth, disk and a
    much slower decode during render, for no visible gain.
    """
    usable = [f for f in video_files
              if (f.get("width") or 0) >= TARGET[0] and f.get("file_type") == "video/mp4"]
    if usable:
        return min(usable, key=lambda f: f["width"])
    mp4s = [f for f in video_files if f.get("file_type") == "video/mp4"]
    if not mp4s:
        raise PexelsError("No mp4 rendition available")
    return max(mp4s, key=lambda f: f.get("width") or 0)


def download(clip: Clip) -> Path:
    """Fetch a clip's video file into the raw library. Idempotent."""
    (LIBRARY / "raw").mkdir(parents=True, exist_ok=True)
    if not clip.download_url:
        raise PexelsError(f"clip {clip.id} has no download_url (re-run search)")

    dest = clip.raw_path
    if dest.is_file() and dest.stat().st_size > 0:
        return dest

    req = urllib.request.Request(clip.download_url, headers={
        "User-Agent": "hopewell-lyric-videos/1.0",
    })
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh, length=1 << 20)
    tmp.replace(dest)
    return dest


# --------------------------------------------------------------------------
# preparing a clip for use behind lyrics
# --------------------------------------------------------------------------


def source_luma(path: Path, samples: int = 5) -> float:
    """Mean brightness (0-255) of a few frames spread across a clip."""
    import numpy as np
    from PIL import Image

    duration = 0.0
    try:
        out = subprocess.run(
            [shutil.which("ffprobe") or "ffprobe", "-v", "error",
             "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True)
        duration = float(out.stdout.strip() or 0)
    except (ValueError, OSError):
        pass

    tmp = LIBRARY / ".probe.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    values = []
    for i in range(samples):
        ts = (duration * (i + 0.5) / samples) if duration > 0 else i
        r = subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
             "-y", "-ss", f"{ts:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", "scale=160:-2", str(tmp)],
            capture_output=True, text=True)
        if r.returncode == 0 and tmp.is_file():
            values.append(float(np.asarray(Image.open(tmp).convert("RGB"),
                                           dtype=np.float32).mean()))
    tmp.unlink(missing_ok=True)
    return sum(values) / len(values) if values else 128.0


#: Source clips outside this brightness band get rejected rather than graded.
#: Stock search returns plenty of near-black night footage (measured as low as
#: 1.2/255) and blown-out skies; neither can be normalised into a usable
#: backdrop, and lifting them only amplifies compression noise.
USABLE_LUMA = (28.0, 200.0)


class UnusableClip(RuntimeError):
    """Raised when a clip's exposure can't be salvaged into a backdrop."""


def prepare(clip: Clip, target_luma: float = 78.0, saturation: float = 0.85,
            blur: float = 0.0, slow: float = 1.0, seconds: float = 0.0,
            pingpong: bool = True, force: bool = False,
            max_gain: float = 1.6) -> Path:
    """Grade, resize and loop a raw clip into something lyrics can sit on.

    Exposure is normalised, not scaled by a fixed amount. A constant darken
    multiplier cannot work across stock footage — clips arrive anywhere from
    near-black to blown out, so the same multiplier that tames a bright sky
    crushes an already-dark interior to solid black (measured: luma 4 on a
    night clip, i.e. the footage was gone entirely). Instead each clip is
    measured and pushed toward `target_luma`.

    Args:
        target_luma: desired mean brightness, 0-255. ~78 suits white type;
                     raise it for themes that set dark type on light ground.
        saturation:  pulled below 1 so the backdrop never competes with the
                     church's own colours in the type.
        blur:        light defocus. Makes busy footage usable behind text.
        slow:        playback rate divisor; 2.0 plays at half speed.
        seconds:     trim to this length (0 keeps whatever remains).
        pingpong:    append a reversed copy so the clip loops with no visible
                     cut. Doubles the length, which is usually welcome.
        max_gain:    cap on brightening. Lifting a very dark clip too far just
                     amplifies its compression noise.
    """
    src = clip.raw_path
    if not src.is_file():
        raise FileNotFoundError(f"raw clip missing: {src}")

    (LIBRARY / "prepared").mkdir(parents=True, exist_ok=True)
    clip.prepared = f"{clip.id}-prepared.mp4"
    dest = clip.prepared_path
    if dest.is_file() and not force:
        return dest

    vf = [
        f"scale={TARGET[0]}:{TARGET[1]}:force_original_aspect_ratio=increase",
        f"crop={TARGET[0]}:{TARGET[1]}",
        f"fps={TARGET_FPS}",
    ]
    if slow and abs(slow - 1.0) > 1e-3:
        vf.insert(0, f"setpts={slow}*PTS")
    if blur > 0.1:
        vf.append(f"gblur=sigma={blur}")
    # One exposure pass only. Stacking eq's additive brightness on top of a
    # multiplicative colorlevels pass compounds to near-black, throwing away
    # the footage the theme exists to show.
    if abs(saturation - 1.0) > 1e-3:
        vf.append(f"eq=saturation={saturation}")

    measured = source_luma(src)
    if not USABLE_LUMA[0] <= measured <= USABLE_LUMA[1]:
        raise UnusableClip(
            f"clip {clip.id} source luma {measured:.1f} is outside "
            f"{USABLE_LUMA[0]:.0f}-{USABLE_LUMA[1]:.0f}; no grade recovers it"
        )
    # gain > 1 brightens, < 1 darkens. colorlevels' output maximum is the
    # inverse: a romax below 1 scales highlights down.
    gain = max(0.12, min(max_gain, target_luma / max(measured, 1.0)))
    if abs(gain - 1.0) > 0.02:
        omax = max(0.05, min(1.0, 1.0 / gain))
        if gain > 1.0:
            # Brightening needs the input white point pulled down instead,
            # since colorlevels' output max cannot exceed 1.
            vf.append(f"colorlevels=rimax={omax:.3f}:gimax={omax:.3f}:bimax={omax:.3f}")
        else:
            vf.append(f"colorlevels=romax={gain:.3f}"
                      f":gomax={gain:.3f}:bomax={gain:.3f}")

    filter_chain = ",".join(vf)
    if pingpong:
        # Trim BEFORE the split, not after the concat: an output-side -t would
        # chop off the reversed half and leave a hard cut every loop, which is
        # glaring behind lyrics. Each half runs seconds/2 so the pair lands on
        # the requested total.
        if seconds:
            filter_chain += f",trim=duration={seconds / 2:.3f},setpts=PTS-STARTPTS"
        # reverse buffers the whole segment in memory, so keep halves short.
        filter_chain = (
            f"[0:v]{filter_chain},split[fwd][rev];"
            f"[rev]reverse,setpts=PTS-STARTPTS[r];"
            f"[fwd][r]concat=n=2:v=1:a=0[vout]"
        )
        graph = ["-filter_complex", filter_chain]
        maps = ["-map", "[vout]"]
        trim_args = []
    else:
        graph = ["-vf", filter_chain]
        maps = []
        trim_args = ["-t", f"{seconds:.2f}"] if seconds else []

    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
           "-y", "-i", str(src), *graph, *maps, "-an", *trim_args,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", str(dest)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"prepare failed for {clip.id}:\n"
            + "\n".join(proc.stderr.strip().splitlines()[-12:])
        )
    return dest


# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------


def load_catalog() -> dict:
    if not CATALOG.is_file():
        return {}
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    return {int(k): Clip(**v) for k, v in raw.items()}


def save_catalog(clips: dict) -> Path:
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(
        json.dumps({str(k): asdict(v) for k, v in clips.items()}, indent=2),
        encoding="utf-8",
    )
    return CATALOG


def for_mood(mood: str, catalog: dict | None = None) -> list:
    """Every prepared clip catalogued under `mood`."""
    catalog = load_catalog() if catalog is None else catalog
    return [c for c in catalog.values()
            if c.mood == mood and c.prepared and c.prepared_path.is_file()]


def pick(mood: str, seed: int = 0, catalog: dict | None = None) -> Clip | None:
    """Deterministically choose one clip for a mood.

    Seeded by song, so re-rendering the same song twice gives the same video
    rather than surprising anyone on a Sunday morning.
    """
    options = sorted(for_mood(mood, catalog), key=lambda c: c.id)
    if not options:
        return None
    return options[seed % len(options)]


def credits(catalog: dict | None = None) -> str:
    """A plain-text credit list for everything in the library."""
    catalog = load_catalog() if catalog is None else catalog
    rows = ["Stock footage via Pexels (free for commercial use, no attribution required).",
            ""]
    for clip in sorted(catalog.values(), key=lambda c: (c.mood, c.id)):
        rows.append(f"  [{clip.mood}] {clip.author} — {clip.page_url}")
    return "\n".join(rows)
